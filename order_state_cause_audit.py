#!/usr/bin/env python3
"""P0-ORDER-STATE-03: causal reasons for pending signal-change cancellation.

RESEARCH ONLY: closed 1H/4H/1D bars; no subhour data, optimization or orders.
Use only original time-of-cancellation snapshots, never subsequent hindsight.
"""
from __future__ import annotations
import argparse
import bisect
import csv
import hashlib
import importlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = 'pending-ob-cancel-causal-asof-v1'
HOUR = 3_600_000
INTERVALS = ('1d', '4h', '1h')
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
CANCEL = 'pending_signal_changed_cancel'
ACCEPT = 'signal_accepted_pending_from_closed_1h'
SUPPORTED_TERMINAL = frozenset({
    'pending_target_without_zone_cancel',
    'pending_signal_changed_cancel',
    'pending_target_and_zone_same_hour_cancel',
    'pending_expired_or_integrity_cancel',
    'pending_stop_already_breached_at_open_cancel',
    'pending_data_gap_cancel',
    'pending_ob_wick_touch_modeled_fill',
    'pending_split_end_unresolved',
})

REASON_PREFIX = (
    ('K 線資料有缺口', 'data_integrity'),
    ('無法確認所有週期', 'missing_closed_candles'),
    ('現貨報價不可用', 'quote_unavailable'),
    ('日線、4H、1H 未全部找到', 'missing_confirmed_mtf_bos'),
    ('日線、4H、1H 最近已確認的結構突破方向不一致', 'mtf_bos_direction_mismatch'),
    ('最新 1H 同向結構突破後沒有', 'no_ob_after_latest_1h_bos'),
    ('同向 1H OB 已超過', 'ob_age_exceeded'),
    ('同向 1H OB 不存在', 'no_fresh_ob_or_retested'),
    ('OB 確認後沒有', 'no_eligible_1h_swing_target'),
    ('現價已到達或越過規則設定的目標波段', 'price_at_or_beyond_target_at_close'),
    ('現價已到達或越過 OB 外緣止損參考', 'price_at_or_beyond_stop_at_close'),
    ('扣除設定費率與滑價後的風險報酬比未達', 'net_rr_below_required'),
)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _utc_millis(raw: str) -> int:
    parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Must supply timezone-aware UTC time')
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _load_json(path: Path) -> dict[str, Any]:
    body = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(body, dict):
        raise ValueError('Expected JSON object')
    return body


def _load_events(path: Path, symbol: str) -> list[dict[str,str]]:
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        expected = {'symbol','split','event','setup_id','at_1h_open_utc'}
        if not reader.fieldnames or not expected.issubset(reader.fieldnames):
            raise ValueError('Missing archived order-state event columns')
        events = list(reader)
    for item in events:
        if item['symbol'] != symbol or item['split'] not in ('train','validation','test') or not item['setup_id']:
            raise ValueError('Malformed archived order-state event')
        millis = _utc_millis(item['at_1h_open_utc'])
        if millis < 0 or millis % HOUR:
            raise ValueError('Archived event is not a 1H open')
    return events


def collect_cancel_pairs(events: list[dict[str, str]], split: str = 'test') -> list[dict[str, Any]]:
    """Pair each accepted pending setup with its terminal, without changing baseline.

    Note: lifecycle terminal can occur at the same or a later open. Compare
    candle-open timestamps only; the strategy evaluates the snapshot at that bar's CLOSE.
    """
    accepted: dict[str, dict[str, str]] = {}
    terminals: dict[str, dict[str, str]] = {}
    for event in events:
        if event['split'] != split:
            continue
        setup = event['setup_id']
        if event['event'] == ACCEPT:
            if setup in accepted:
                raise ValueError('Duplicate accepted setup in split')
            accepted[setup] = event
        elif event['event'] in SUPPORTED_TERMINAL:
            if setup in terminals:
                raise ValueError('Duplicate terminal for accepted setup')
            terminals[setup] = event
    if accepted.keys() != terminals.keys():
        raise ValueError('Missing or orphan terminal of an accepted setup')
    pairs = []
    for setup, end in terminals.items():
        if end['event'] != CANCEL:
            continue
        start = accepted[setup]
        accepted_open = _utc_millis(start['at_1h_open_utc'])
        canceled_open = _utc_millis(end['at_1h_open_utc'])
        if canceled_open <= accepted_open or (canceled_open - accepted_open) % HOUR:
            raise ValueError('Cancellation must occur at a later 1H candle')
        pairs.append({
            'split': split, 'setup_id': setup,
            'accepted_open_ms': accepted_open, 'canceled_open_ms': canceled_open,
            'accepted_open_utc': start['at_1h_open_utc'],
            'canceled_open_utc': end['at_1h_open_utc'],
            'pending_hours': (canceled_open - accepted_open) // HOUR,
        })
    pairs.sort(key=lambda r:(r['canceled_open_ms'],r['setup_id']))
    return pairs


def _classify(report: dict[str, Any], old: dict[str, Any], new: dict[str, Any] | None) -> str:
    """Exactly the first failed check or a replacement setup, not a causal claim."""
    if new is not None:
        if str(new['setup_id']) == str(old['setup_id']):
            raise ValueError('Reconstructed candidate still matches cancelled pending setup')
        if new['side'] != old['side']:
            return 'replacement_opposite_side_ob'
        return 'replacement_same_side_new_ob'
    reasons = report.get('reasons')
    if not isinstance(reasons, list) or not reasons:
        raise ValueError('Missing exact original rejected-candidate reason')
    for prefix, category in REASON_PREFIX:
        if any(str(r).startswith(prefix) for r in reasons):
            return category
    return 'unclassified_original_reason'


def _snapshot_builder(data_dir: Path, symbol: str, asof_times: set[int], *, engine: Any, market: Any):
    """Rebuild event indexes once, and only the CLOSED-BAR snapshots actually needed.

    Reuses exact event, 200-bar window and quote/strategy logic from original
    frozen engine, never the 80K-hour complete snapshot loop.
    """
    if tuple(engine.INTERVALS) != INTERVALS or tuple(market.INTERVALS) != INTERVALS:
        raise ValueError('Cannot audit: formal engine timeframes differ from 1D/4H/1H')
    rows = {iv: engine._closed_rows(data_dir / f'{symbol}_{iv}.csv',symbol,iv) for iv in INTERVALS}
    by_event = {}
    for interval in INTERVALS:
        x=[]
        for segment in engine._contiguous_segments(rows[interval], interval):
            x.extend(engine._strategy_events(segment, interval))
        x.sort(key=lambda item:(int(item['confirmed_at_close_time']),item['event_type'],item['event_id']))
        by_event[interval]=x
    records, event_time = {}, {}
    for interval, events in by_event.items():
        rec=[]
        for event in events:
            try:
                evidence=json.loads(event['evidence_json'])
                opens=frozenset(int(e['open_time']) for e in evidence)
                t=int(event['confirmed_at_close_time'])
            except (KeyError,TypeError,ValueError,json.JSONDecodeError):
                continue
            rec.append((t,event,opens))
        rec.sort(key=lambda x:(x[0],x[1]['event_type'],x[1]['event_id']))
        records[interval]=rec
        event_time[interval]=[x[0] for x in rec]
    close_times={iv:[int(r['close_time']) for r in rows[iv]] for iv in INTERVALS}
    hour_opens=[int(r['open_time']) for r in rows['1h']]
    cfg={
        'config_version':market.CONFIG_VERSION,
        'strategy_version':market.STRATEGY_VERSION,
        'minimum_net_rr':'1.5', 'max_quote_age_seconds':3600,
        'fee_bps_per_side':'10', 'slippage_bps_per_side':'5',
        'max_setup_age_hours':168, 'rules':market._rules(),
    }
    cache={}
    for asof in sorted(asof_times):
        if asof <= 0 or asof % HOUR:
            raise ValueError('Expected 1H close+1 boundary')
        ix=bisect.bisect_left(hour_opens, asof-HOUR)
        if ix >= len(hour_opens) or hour_opens[ix]!=asof-HOUR:
            raise ValueError('No exact frozen 1H candle at original decision time')
        windows, integrity, active = engine._make_windows(asof,rows,close_times,records,event_time,symbol)
        all_ok=all(x.get('status')=='complete' and not x.get('gaps') and not x.get('errors') for x in integrity['intervals'].values())
        if not all(len(windows[i])>=8 for i in INTERVALS):
            raise ValueError('Unexpected insufficient history for archived accepted order')
        metadata={'symbol':symbol, 'binance_server_time':asof,
                  'binance_server_time_utc':engine._utc(asof),
                  'sources':{'spot':engine.SPOT_PUBLIC_DATA_BASE,'futures':None},
                  'data_integrity':integrity}
        spots=[{'symbol':symbol,'price':str(rows['1h'][ix]['close']),
                'captured_at_utc':engine._utc(asof),'source':engine.SOURCE_SPOT}]
        rep=market.build_analysis_from_records(
            metadata,[dict(row) for iv in INTERVALS for row in windows[iv]],
            spots,active,cfg,archive_name='historical-asof',archive_sha256='',
            now=datetime.fromtimestamp(asof/1000,timezone.utc))
        candidate=None
        scene=rep.get('scenario')
        if all_ok and scene and rep.get('status') in ('候選做多','候選做空'):
            ob=rep.get('evidence',{}).get('setup_order_block') or {}
            candidate={
                'setup_id':scene['entry_event_id'],
                'side':scene['side'], 'target_event_id':scene['target_event_id'],
                'entry_reference':str(scene['conservative_entry_reference']),
                'zone_low':str(scene['entry_zone']['low']),
                'zone_high':str(scene['entry_zone']['high']),
                'stop':str(scene['stop_price']),'target':str(scene['target_price']),
                'net_rr':str(scene['risk_reward']['net_reward_to_risk_ratio']),
                'setup_age_hours':float(rep['checks']['setup_freshness']['age_hours']),
                'setup_confirm_ms':int(ob['confirmed_at_close_time']),
            }
            if not engine._candidate_allowed(candidate,1.5,168):
                raise ValueError('Candidate fails original frozen acceptance logic')
        cache[asof]={'report':rep,'candidate':candidate,'integrity_ok':all_ok}
    return cache


def _read_inputs(data_dir: Path, replay_dir: Path, symbol: str, *, baseline_dir:Path|None=None):
    if symbol not in SYMBOLS:
        raise ValueError('Unsupported requested audit symbol')
    audit_path=replay_dir/f'{symbol}_order_state_audit.json'
    events_path=replay_dir/f'{symbol}_order_state_events.csv'
    body=_load_json(audit_path)
    if body.get('symbol')!=symbol or body.get('version')!='order-state-1h-ob-observational-v1':
        raise ValueError('Unmatched archived order-state version or symbol')
    if body.get('subhour_price_data_used') is not False or tuple(body.get('decision_timeframes_only',[]))!=('1h','4h','1d'):
        raise ValueError('Subhour data forbidden for this study')
    coverage_path=data_dir/'multi_asset_coverage.json'
    if _sha(coverage_path)!=body.get('coverage_source_sha256'):
        raise ValueError('Frozen coverage SHA differs from independently passed replay')
    baseline = None
    if baseline_dir is not None:
        baseline_path=baseline_dir/f'{symbol}_fixed_calendar_baseline.json'
        if _sha(baseline_path)!=body.get('baseline_source_sha256'):
            raise ValueError('Frozen per-asset baseline SHA differs from passed replay')
        baseline = _load_json(baseline_path)
        if baseline.get('symbol') != symbol or baseline.get('status') != 'complete':
            raise ValueError('Frozen baseline symbol or status inconsistent')
    # The source files themselves must agree with the immutable upstream
    # Binance coverage manifest. Merely checking the manifest hash does NOT
    # protect against a local modification of one of the historic candle CSVs.
    coverage = _load_json(coverage_path)
    symbol_coverage = coverage.get('symbols',{}).get(symbol,{})
    intervals = symbol_coverage.get('intervals',{})
    for interval in INTERVALS:
        candle_path = data_dir / f'{symbol}_{interval}.csv'
        actual = _sha(candle_path)
        manifest_hash = intervals.get(interval,{}).get('csv_sha256')
        if not manifest_hash or actual != manifest_hash:
            raise ValueError(f'{symbol} {interval}: frozen candle SHA256 differs from coverage manifest')
        if baseline is not None and actual != baseline.get('input_sha256',{}).get(candle_path.name):
            raise ValueError(f'{symbol} {interval}: frozen candle SHA256 differs from baseline report')
    count=body['splits']['test']['event_counts'].get(CANCEL,0)
    ev=_load_events(events_path,symbol)
    pairs=collect_cancel_pairs(ev)
    if len(pairs)!=count:
        raise ValueError('Cancellations count differs from archived provenance')
    return body,ev,pairs


def run_symbol(data_dir:Path,replay_dir:Path,symbol:str,output_dir:Path,*,
               baseline_dir:Path|None=None,engine:Any=None,market:Any=None)->dict[str,Any]:
    body,events,pairs=_read_inputs(data_dir,replay_dir,symbol,baseline_dir=baseline_dir)
    if engine is None:
        engine=importlib.import_module('historical_backtest')
    if market is None:
        market=importlib.import_module('market_research')
    required=set()
    for item in pairs:
        required.update((item['accepted_open_ms']+HOUR,item['canceled_open_ms']+HOUR))
    frozen=_snapshot_builder(data_dir,symbol,required,engine=engine,market=market)
    counts=Counter()
    records=[]
    for pair in pairs:
        original=frozen[pair['accepted_open_ms']+HOUR]
        current=frozen[pair['canceled_open_ms']+HOUR]
        old=original['candidate']
        if not old or old['setup_id']!=pair['setup_id']:
            raise ValueError('Accepted order does not reconstruct exact original OB candidate')
        # The existing strategy only cancels for candidate change AFTER ruling
        # out stop-at-open, OB wick touch, target-without-zone, expiry and integrity.
        if not original['integrity_ok'] or not current['integrity_ok']:
            raise ValueError('Inconsistent archived cancellation integrity')
        classification=_classify(current['report'],old,current['candidate'])
        counts[classification]+=1
        records.append({
            'symbol':symbol,'split':'test', 'setup_id':pair['setup_id'],
            'accepted_open_utc':pair['accepted_open_utc'],
            'canceled_open_utc':pair['canceled_open_utc'],
            'pending_hours':pair['pending_hours'],
            'original_side':old['side'], 'original_target_event_id':old['target_event_id'],
            'original_ob_confirm_ms':old['setup_confirm_ms'],
            'original_rr':old['net_rr'],
            'new_status':current['report'].get('status'),
            'new_setup_id':current['candidate']['setup_id'] if current['candidate'] else '',
            'new_side':current['candidate']['side'] if current['candidate'] else '',
            'new_target_event_id':current['candidate']['target_event_id'] if current['candidate'] else '',
            'category':classification,
            'original_rejection_reasons':' | '.join(str(s) for s in current['report'].get('reasons',[])),
            'new_mtf_status':current['report'].get('checks',{}).get('multi_timeframe_structure',{}).get('status'),
            'snapshot_close_utc':engine._utc(pair['canceled_open_ms']+HOUR),
        })
    if sum(counts.values())!=body['splits']['test']['event_counts'][CANCEL]:
        raise ValueError('Missing some archived Test cancellations')
    output_dir.mkdir(parents=True,exist_ok=True)
    summary={
        'version':VERSION,'symbol':symbol,'status':'verified_reconstructed_test_cancellation_snapshots',
        'strategy_changed':False,'trade_decision_timeframes':list(INTERVALS),
        'uses_subhour_data':False, 'real_orders':False,
        'test_canceled_signals':len(pairs),
        'reason_counts':dict(sorted(counts.items())),
        'wait_hours_median':statistics.median(p['pending_hours'] for p in pairs) if pairs else None,
        'wait_hours_mean':round(statistics.mean(p['pending_hours'] for p in pairs),3) if pairs else None,
        'source_sha256':{
            'coverage':_sha(data_dir/'multi_asset_coverage.json'),
            'order_state_json':_sha(replay_dir/f'{symbol}_order_state_audit.json'),
            'order_state_events_csv':_sha(replay_dir/f'{symbol}_order_state_events.csv'),
            **{f'{symbol}_{iv}.csv':_sha(data_dir/f'{symbol}_{iv}.csv') for iv in INTERVALS}},
        'limitations':[
            'Classification is the first failed original candidate rule at the cancellation bar CLOSE, not a proof of economic causality.',
            'Only closed 1D/4H/1H candles used; never 1m or 15m; no actual limit fills.',
            'Cancellation is not a missed profitable trade or permission to loosen the original strategy.',
            'Previously viewed Test is NOT new independent out-of-sample evidence.',
            'Original execution state does not test alternate policy; no strategy R or win-rate modification.',
        ],
    }
    (output_dir/f'{symbol}_cancellation_causes.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    if records:
        with (output_dir/f'{symbol}_cancellation_causes.csv').open('w',newline='',encoding='utf-8-sig') as f:
            w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)
    return summary


def aggregate(output_dir:Path, symbols:tuple[str,...]=SYMBOLS)->dict[str,Any]:
    if not symbols or len(set(symbols))!=len(symbols):
        raise ValueError('Invalid symbol list')
    counts=Counter();total=0;each={}
    for s in symbols:
        item=_load_json(output_dir/f'{s}_cancellation_causes.json')
        if item.get('symbol')!=s or item.get('version')!=VERSION or item.get('uses_subhour_data') is not False or item.get('status')!='verified_reconstructed_test_cancellation_snapshots':
            raise ValueError(f'Invalid/missing per-symbol result: {s}')
        if sum(item['reason_counts'].values())!=item['test_canceled_signals']:
            raise ValueError('Reason distribution does not close')
        for name,n in item['reason_counts'].items():
            counts[name]+=n
        each[s]=item['test_canceled_signals']
        total+=item['test_canceled_signals']
    result={'version':VERSION,'status':'all_symbols_verified',
            'symbols':each,'test_canceled_signals':total,'reason_counts':dict(sorted(counts.items())),
            'uses_only_closed_1h_4h_1d':True,'strategy_modified':False,
            'limitations':'Observed rejection reasons only; not proven alternative strategy or new out-of-sample validation.'}
    (output_dir/'cancel_cause_summary.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    lines=['# P0-ORDER-STATE-03｜136 筆訊號改變取消原因','',
           '研究-only；1H／4H／1D 已收盤 K 線；未修改正式做單規則。','',
           '| 幣種 | 訊號改變取消 |','|---|---:|']
    for sym,n in each.items(): lines.append(f'| {sym} | {n} |')
    lines += [f'| **合計** | **{total}** |','','| 取消快照中的第一個不符合條件 | 次數 |','|---|---:|']
    for reason,n in counts.most_common():lines.append(f'| {reason} | {n} |')
    lines += ['', '**所有分類描述原策略在取消當時的狀態，不代表取消錯誤或應立即進場。**',
              '1H K 線不能證明限價單實際成交。已研究 Test 不能重複當成全新樣本外驗證。']
    (output_dir/'cancel_cause_summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return result


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='cmd',required=True)
    q=sub.add_parser('causes',help='Exact as-of primary cancellation reason reconstruction')
    q.add_argument('symbol',choices=SYMBOLS)
    q.add_argument('--data-dir',type=Path,required=True)
    q.add_argument('--replay-dir',type=Path,required=True)
    q.add_argument('--baseline-dir',type=Path)
    q.add_argument('--output-dir',type=Path,required=True)
    ag=sub.add_parser('aggregate');ag.add_argument('--output-dir',type=Path,required=True)
    quick=sub.add_parser('timeline');quick.add_argument('symbol',choices=SYMBOLS)
    quick.add_argument('--data-dir',type=Path,required=True)
    quick.add_argument('--replay-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.cmd=='aggregate': result=aggregate(args.output_dir)
    elif args.cmd=='timeline':
        body,ev,pairs=_read_inputs(args.data_dir,args.replay_dir,args.symbol)
        result={'symbol':args.symbol,'test_signal_changed_cancel':len(pairs),
                'median_wait_hours':statistics.median(x['pending_hours'] for x in pairs),
                'max_wait_hours':max(x['pending_hours'] for x in pairs)}
    else: result=run_symbol(args.data_dir,args.replay_dir,args.symbol,args.output_dir,baseline_dir=args.baseline_dir)
    print(json.dumps(result,ensure_ascii=False,default=str))
    return 0

if __name__=='__main__':raise SystemExit(main())