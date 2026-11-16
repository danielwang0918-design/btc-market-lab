#!/usr/bin/env python3
"""P0-ORDER-STATE-04: explain frozen MTF BOS mismatch cancellations as of 1H closes.

RESEARCH ONLY. No signal, stop, target, position size, order, or subhour bars.
Never classify an already-seen Test set as a new holdout or positive edge.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import statistics
from bisect import bisect_left
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = 'p0-ob-bos-first-divergence-v1'
INTERVALS = ('1d', '4h', '1h')
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
HOUR = 3_600_000
CATEGORY = 'mtf_bos_direction_mismatch'
SOURCE_VERSION = 'pending-ob-cancel-causal-asof-v1'
EXPECTED_BOS = 94  # Frozen 2026-11-16 research cohort; not a performance threshold.


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def parse_ms(value: str) -> int:
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('UTC timestamp must be timezone-aware')
    d = d.astimezone(timezone.utc)
    m = int(d.timestamp() * 1000)
    if m < 0 or m % HOUR:
        raise ValueError('Timestamps must be aligned to a 1H UTC boundary')
    return m


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def read_causes(cause_dir: Path, symbol: str, data_dir: Path,
                replay_dir: Path, baseline_dir: Path | None = None,
                prior: Any = None) -> list[dict[str, Any]]:
    """Reconcile every row to the accepted/terminal order ledger and source SHAs."""
    if symbol not in SYMBOLS:
        raise ValueError('Unsupported symbol')
    prior = prior or importlib.import_module('order_state_cause_audit')
    ledger, _, pairs = prior._read_inputs(data_dir, replay_dir, symbol, baseline_dir=baseline_dir)
    cause_json = cause_dir / f'{symbol}_cancellation_causes.json'
    cause_csv = cause_dir / f'{symbol}_cancellation_causes.csv'
    summary = json.loads(cause_json.read_text(encoding='utf-8'))
    if (summary.get('version') != SOURCE_VERSION or summary.get('symbol') != symbol or
        summary.get('status') != 'verified_reconstructed_test_cancellation_snapshots' or
        summary.get('strategy_changed') is not False or
        summary.get('uses_subhour_data') is not False or
        tuple(summary.get('trade_decision_timeframes', ())) != INTERVALS):
        raise ValueError('Wrong or unverified stage-03 input')
    if summary.get('test_canceled_signals') != len(pairs):
        raise ValueError('Stage-03 cancellation count differs from frozen ledger')
    if summary.get('source_sha256', {}).get('coverage') != sha(data_dir / 'multi_asset_coverage.json'):
        raise ValueError('Prior source coverage SHA mismatch')
    for interval in INTERVALS:
        name = f'{symbol}_{interval}.csv'
        if summary['source_sha256'].get(name) != sha(data_dir / name):
            raise ValueError(f'Frozen {interval} candles do not match prior stage-03 audit')
    if summary['source_sha256'].get('order_state_events_csv') != sha(replay_dir / f'{symbol}_order_state_events.csv'):
        raise ValueError('Prior order-state events source SHA mismatch')
    if summary['source_sha256'].get('order_state_json') != sha(replay_dir / f'{symbol}_order_state_audit.json'):
        raise ValueError('Prior order-state JSON SHA mismatch')
    with cause_csv.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        required = {'symbol','split','setup_id','accepted_open_utc','canceled_open_utc',
                    'pending_hours','category','original_side','new_mtf_status','snapshot_close_utc'}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError('Stage-03 per-trade evidence is incomplete')
        records = list(reader)
    ledger_pairs = {p['setup_id']: p for p in pairs}
    if len(ledger_pairs) != len(pairs) or len(records) != len(pairs):
        raise ValueError('Duplicate setup or dropped cancellation')
    if set(r['setup_id'] for r in records) != set(ledger_pairs):
        raise ValueError('CSV setup IDs differ from frozen accepted-to-terminal lifecycle')
    for row in records:
        p = ledger_pairs[row['setup_id']]
        if (row['symbol'] != symbol or row['split'] != 'test' or
            parse_ms(row['accepted_open_utc']) != p['accepted_open_ms'] or
            parse_ms(row['canceled_open_utc']) != p['canceled_open_ms'] or
            int(row['pending_hours']) != p['pending_hours'] or
            parse_ms(row['snapshot_close_utc']) != p['canceled_open_ms'] + HOUR or
            row['original_side'] not in ('long', 'short')):
            raise ValueError('Stage-03 row does not agree with original order state')
    counts = Counter(x['category'] for x in records)
    if dict(sorted(counts.items())) != summary['reason_counts']:
        raise ValueError('Cause CSV distribution does not equal stage-03 summary')
    mismatches = [r for r in records if r['category'] == CATEGORY]
    if any(r['new_mtf_status'] != 'mixed' for r in mismatches):
        raise ValueError('First-failed condition and MTF mixed status conflict')
    return mismatches


class BosAsOfIndex:
    """Reuse original closed-candle event builder, 200-bar windows, validation.

    Never changes trading decisions. The 1H boundary query only describes the
    three already-confirmed BOS events the ORIGINAL strategy would have seen.
    """
    def __init__(self, data_dir: Path, symbol: str, *, engine: Any = None, market: Any = None):
        self.engine = engine or importlib.import_module('historical_backtest')
        self.market = market or importlib.import_module('market_research')
        if tuple(self.engine.INTERVALS) != INTERVALS or tuple(self.market.INTERVALS) != INTERVALS:
            raise ValueError('Decision timeframe lock violated')
        self.symbol = symbol
        self.rows = {iv:self.engine._closed_rows(data_dir / f'{symbol}_{iv}.csv', symbol,iv)
                     for iv in INTERVALS}
        self.closes = {iv:[int(row['close_time']) for row in self.rows[iv]] for iv in INTERVALS}
        self.opens = {iv:[int(row['open_time']) for row in self.rows[iv]] for iv in INTERVALS}
        self.times = {}
        self.events = {}
        for iv in INTERVALS:
            produced = []
            for seg in self.engine._contiguous_segments(self.rows[iv],iv):
                produced.extend(self.engine._strategy_events(seg,iv))
            produced.sort(key=lambda x:(int(x['confirmed_at_close_time']),x['event_type'],x['event_id']))
            rec = []
            for ev in produced:
                if ev['event_type'] != 'structure_break':
                    continue
                evidence = json.loads(ev['evidence_json'])
                opens = frozenset(int(x['open_time']) for x in evidence)
                rec.append((int(ev['confirmed_at_close_time']), ev, opens))
            rec.sort(key=lambda x:(x[0],x[1]['event_type'],x[1]['event_id']))
            self.events[iv]=rec
            self.times[iv]=[x[0] for x in rec]
        self.cache: dict[int,dict[str,dict[str,Any]|None]] = {}

    def at(self, asof: int) -> dict[str,dict[str,Any] | None]:
        if asof in self.cache:
            return self.cache[asof]
        if asof <= 0 or asof % HOUR:
            raise ValueError('As-of must be a 1H closed-bar boundary')
        output = {}
        for iv in INTERVALS:
            closes, opens = self.closes[iv], self.opens[iv]
            end = bisect_left(closes, asof)
            start = max(0, end - 200)
            if not end or not start < end:
                output[iv]=None;continue
            bar_opens = frozenset(opens[start:end]); left = opens[start]
            event_times = self.times[iv]; records = self.events[iv]
            lo, hi = bisect_left(event_times, left), bisect_left(event_times, asof)
            active = [ev for t, ev, witnesses in records[lo:hi]
                      if witnesses.issubset(bar_opens)]
            # market_research._latest_event uses max(), which retains the FIRST
            # matching event for equal timestamps; preserve its tie semantics.
            latest = max(active, key=lambda ev:int(ev['confirmed_at_close_time']), default=None)
            output[iv] = None if latest is None else {
                'direction':latest['direction'],
                'event_id':latest['event_id'],
                'confirmed_at_close_ms':int(latest['confirmed_at_close_time']),
            }
        self.cache[asof] = output
        return output


def directions(state: dict[str,dict[str,Any]|None]) -> dict[str,str|None]:
    return {iv: (state[iv]['direction'] if state[iv] else None) for iv in INTERVALS}


def first_divergence(record: dict[str,Any], index: Any) -> tuple[dict[str,Any],list[dict[str,Any]]]:
    start = parse_ms(record['accepted_open_utc']) + HOUR
    end = parse_ms(record['canceled_open_utc']) + HOUR
    if not start < end or (end-start)%HOUR:
        raise ValueError('Invalid pending lifecycle timeline')
    expected = 'bullish' if record['original_side']=='long' else 'bearish'
    initial = index.at(start)
    initial_dirs = directions(initial)
    if any(initial_dirs[iv] != expected for iv in INTERVALS):
        raise ValueError('Archived accepted OB does not have aligned 1D/4H/1H BOS')
    path = []
    first = None
    for at in range(start+HOUR,end+1,HOUR):
        state = index.at(at)
        vector = directions(state)
        mismatch = [iv for iv in INTERVALS if vector[iv] != expected]
        if first is None and mismatch:
            first = (at,state,vector,mismatch)
        path.append({'symbol':record['symbol'],'setup_id':record['setup_id'],
                     'asof_1h_close_utc':utc(at),'hours_after_signal':(at-start)//HOUR,
                     **{f'{iv}_bos':vector[iv] or 'missing' for iv in INTERVALS},
                     'aligned_to_accepted_side':not bool(mismatch)})
    cancel_state = index.at(end)
    ending = directions(cancel_state)
    opposing = [iv for iv in INTERVALS if ending[iv] != expected]
    if not first or len(set(ending.values()))<2 or any(v not in ('bullish','bearish') for v in ending.values()):
        raise ValueError('Original MTF mixed cancellation not reproduced by independent BOS event replay')
    at,state,vector,first_changed=first
    first_changes=[]
    for iv in first_changed:
        old=initial[iv];new=state[iv]
        cause = ('no_recent_bos_in_rolling_window' if new is None else
                 'new_opposite_confirmed_bos' if new['direction'] != old['direction']
                      and new['confirmed_at_close_ms'] >= start-1 else
                 'opposite_bos_was_already_confirmed' if new['direction'] != old['direction']
                      else 'other_event_state_change')
        first_changes.append({'interval':iv,'type':cause,'old_bos_event':old['event_id'] if old else None,
                              'new_bos_event':new['event_id'] if new else None,
                              'new_bos_confirmed_at_utc':utc(new['confirmed_at_close_ms']+1) if new else None})
    outcome = {
        'symbol':record['symbol'],'setup_id':record['setup_id'],'original_side':record['original_side'],
        'accepted_open_utc':record['accepted_open_utc'],'canceled_open_utc':record['canceled_open_utc'],
        'waiting_hours':int(record['pending_hours']),
        'first_bos_mismatch_asof_utc':utc(at),
        'first_bos_mismatch_delay_hours':(at-start)//HOUR,
        'detected_before_formal_cancel':at<end,
        'first_deviating_intervals':first_changed,
        'first_deviation_detail':first_changes,
        'cancel_deviating_intervals':opposing,
        'accepted_bos_directions':initial_dirs,'canceled_bos_directions':ending,
        'earliest_snapshot_source':'closed_1h_4h_1d_only',
        'not_trade_simulation':True,
    }
    return outcome,path


def write_result(data_dir:Path, replay_dir:Path,cause_dir:Path,symbol:str,output_dir:Path,
                 baseline_dir:Path|None=None, *, prior:Any=None, index_factory:Any=None)->dict[str,Any]:
    rows=read_causes(cause_dir,symbol,data_dir,replay_dir,baseline_dir,prior=prior)
    index=(index_factory or BosAsOfIndex)(data_dir,symbol)
    outputs=[];paths=[]
    for r in rows:
        x,p=first_divergence(r,index)
        outputs.append(x);paths.extend(p)
    count=Counter(iv for x in outputs for iv in x['first_deviating_intervals'])
    exclusive=Counter(x['first_deviating_intervals'][0] if len(x['first_deviating_intervals'])==1 else 'simultaneous' for x in outputs)
    kinds=Counter(c['type'] for x in outputs for c in x['first_deviation_detail'])
    ending=Counter('+'.join(x['cancel_deviating_intervals']) for x in outputs)
    early=sum(x['detected_before_formal_cancel'] for x in outputs)
    if early:
        # Earlier MTF signal change should have already ended the accepted pending
        # under the original simulated state machine. Protect against lookahead/drift.
        raise ValueError(f'{symbol}: {early} MTF mismatches found before archived cancellation; investigation required')
    report={'version':VERSION,'symbol':symbol,'status':'verified_from_closed_formal_bos_events',
            'decision_timeframes':list(INTERVALS),'uses_subhour_candles':False,
            'strategy_changed':False,'test_cancellations_stage03_reason_mtf':len(outputs),
            'first_deviating_interval_counts_nonexclusive':dict(sorted(count.items())),
            'first_deviating_interval_counts_exclusive':dict(sorted(exclusive.items())),
            'first_deviation_types':dict(sorted(kinds.items())),
            'cancellation_opposing_interval_combinations':dict(sorted(ending.items())),
            'first_mismatch_delay_hours_median':statistics.median(x['first_bos_mismatch_delay_hours'] for x in outputs) if outputs else None,
            'source_sha256':{
                'stage03_causes_csv':sha(cause_dir/f'{symbol}_cancellation_causes.csv'),
                'stage03_causes_json':sha(cause_dir/f'{symbol}_cancellation_causes.json'),
                'coverage':sha(data_dir/'multi_asset_coverage.json'),
                'order_state_events':sha(replay_dir/f'{symbol}_order_state_events.csv'),
                **{f'{symbol}_{iv}.csv':sha(data_dir/f'{symbol}_{iv}.csv') for iv in INTERVALS},
            },
            'warnings':[
                'The event replay is a descriptive reconstruction of the already-viewed Test cancellations, not a new holdout.',
                'BOS direction mismatch is the first rejected rule, not proof that it economically caused a loss.',
                'A canceled, unfilled OB order is not a missed winning trade.',
                'Only fully closed 1H/4H/1D bars; 1m/5m/15m never read for decisions.',
                'No actual order fills, changes to strategy, parameter selection, or live trading.',
            ]}
    output_dir.mkdir(parents=True,exist_ok=True)
    (output_dir/f'{symbol}_bos_timeline.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    for suffix,content in [('bos_timeline',outputs),('bos_path',paths)]:
        if content:
            path=output_dir/f'{symbol}_{suffix}.csv'
            flat=[]
            for row in content:
                converted={k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v
                           for k,v in row.items()}
                flat.append(converted)
            with path.open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(flat[0]));w.writeheader();w.writerows(flat)
    return report


def aggregate(results_dir:Path, symbols:tuple[str,...]=SYMBOLS)->dict[str,Any]:
    if not symbols or len(set(symbols))!=len(symbols):
        raise ValueError('Invalid symbols')
    reports=[]
    for symbol in symbols:
        body=json.loads((results_dir/f'{symbol}_bos_timeline.json').read_text(encoding='utf-8'))
        if (body.get('version')!=VERSION or body.get('symbol')!=symbol or
            body.get('status')!='verified_from_closed_formal_bos_events' or
            body.get('uses_subhour_candles') is not False or
            body.get('strategy_changed') is not False or
            tuple(body.get('decision_timeframes',()))!=INTERVALS):
            raise ValueError('Unverified per-symbol BOS results')
        reports.append(body)
    if sum(x['test_cancellations_stage03_reason_mtf'] for x in reports)!=EXPECTED_BOS:
        raise ValueError('Frozen mismatch cancellation cohort changed; no totals can be claimed')
    counts=Counter();types=Counter();exclusive=Counter();endings=Counter()
    for body in reports:
        counts.update(body['first_deviating_interval_counts_nonexclusive'])
        exclusive.update(body['first_deviating_interval_counts_exclusive'])
        types.update(body['first_deviation_types'])
        endings.update(body['cancellation_opposing_interval_combinations'])
    result={'version':VERSION,'status':'all_three_symbols_verified','symbols':{r['symbol']:r['test_cancellations_stage03_reason_mtf'] for r in reports},
            'test_mtf_mismatch_cancellations':EXPECTED_BOS,
            'first_deviating_interval_counts_nonexclusive':dict(sorted(counts.items())),
            'first_deviating_interval_counts_exclusive':dict(sorted(exclusive.items())),
            'first_deviation_types':dict(sorted(types.items())),
            'cancellation_opposing_interval_combinations':dict(sorted(endings.items())),
            'uses_only_closed_1h_4h_1d':True,'strategy_changed':False,
            'warning':'Previously viewed Test, not a new out-of-sample performance result. No alternate trading rules evaluated.'}
    (results_dir/'bos_timeline_summary.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    lines=['# P0-ORDER-STATE-04｜94 筆多週期 BOS 方向失配：首次變化來源','',
           '僅 1H／4H／1D 已收盤 K 線；對已知 Test 做描述性稽核，沒有調參或改策略。','',
           '| 幣種 | MTF BOS 不一致取消 |','|---|---:|']
    for s,n in result['symbols'].items():lines.append(f'| {s} | {n} |')
    lines += [f'| **合計** | **{EXPECTED_BOS}** |','','| 首次失配週期（可多選） | 計數 |','|---|---:|']
    for k,n in counts.most_common():lines.append(f'| {k} | {n} |')
    lines += ['','| 首次變化種類 | 計數 |','|---|---:|']
    for k,n in types.most_common():lines.append(f'| {k} | {n} |')
    lines += ['', '**重要：這些是原始策略取消原因的結構時間軸，並非進場規則建議，也不能把已取消交易當作獲利。**']
    (results_dir/'bos_timeline_summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return result


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('analyze')
    p.add_argument('symbol',choices=SYMBOLS)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--replay-dir',type=Path,required=True)
    p.add_argument('--causes-dir',type=Path,required=True)
    p.add_argument('--baseline-dir',type=Path)
    p.add_argument('--output-dir',type=Path,required=True)
    g=sub.add_parser('aggregate');g.add_argument('--results-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.cmd=='aggregate':r=aggregate(args.results_dir)
    else:r=write_result(args.data_dir,args.replay_dir,args.causes_dir,args.symbol,args.output_dir,args.baseline_dir)
    print(json.dumps(r,ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())