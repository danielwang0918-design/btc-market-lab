#!/usr/bin/env python3
"""P0 audit of OB wick-touch entry proxies against frozen, matching Binance 1H data.

Observational research only: does not recalculate returns, place trades or rewrite
mtf-bos-ob-retest-v1. Optional independent 1-minute bar order check does NOT
prove actual limit/market order executions or tick-level sequencing.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

VERSION = 'execution-audit-02-v1'
SPOT_PUBLIC_DATA_BASE = 'https://data-api.binance.vision'
HOUR = 3_600_000
MINUTE = 60_000
REQUIRED = ('symbol', 'split', 'direction', 'entry_time_utc', 'exit_time_utc',
            'signal_time_utc', 'raw_entry_price', 'stop_price', 'target_price',
            'exit_reason', 'bars_held', 'setup_id')


def ms(raw: str) -> int:
    try:
        dt = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('naive timestamp')
        return int(dt.timestamp() * 1000)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError('Invalid UTC trade timestamp') from exc


def dec(raw: Any, label: str) -> Decimal:
    try:
        v = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('Invalid '+label) from exc
    if not v.is_finite() or v <= 0:
        raise ValueError('Non-positive/nonfinite '+label)
    return v


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_archive(history: Path, report_archives: list[Path], symbols: list[str]) -> tuple[dict, dict, dict]:
    """Fail closed on missing or mismatched frozen 1H and formal-trade sources."""
    reports: dict[str, dict] = {}
    report_paths: dict[str, Path] = {}
    for f in report_archives:
        with zipfile.ZipFile(f) as z:
            entries = [n for n in z.namelist() if n.endswith('_fixed_calendar_baseline.json')]
            if len(entries) != 1:
                raise ValueError(f'Expected one per-symbol report in {f}')
            report = json.loads(z.read(entries[0]))
        name = report.get('symbol')
        if name in reports or name not in symbols or f'{name}_fixed_calendar_baseline.json' != entries[0]:
            raise ValueError('Unexpected or repeated per-symbol research artifact')
        if report.get('status') != 'complete' or report.get('strategy') != 'mtf-bos-ob-retest-v1':
            raise ValueError('Incomplete/incompatible formal strategy result')
        reports[name] = report
        report_paths[name] = f
    if set(reports) != set(symbols):
        raise ValueError('Requested symbols and supplied artifacts differ')
    candles: dict[str, dict[int, dict]] = {}
    with zipfile.ZipFile(history) as z:
        cov_bytes = z.read('multi_asset_coverage.json')
        cov = json.loads(cov_bytes)
        if cov.get('status') != 'complete':
            raise ValueError('Historical coverage not complete')
        for symbol in symbols:
            rep = reports[symbol]
            if rep.get('input_sha256', {}).get('multi_asset_coverage.json') != sha(cov_bytes):
                raise ValueError(f'{symbol}: frozen coverage SHA256 mismatch')
            name = f'{symbol}_1h.csv'
            raw = z.read(name)
            expected = rep.get('input_sha256', {}).get(name)
            coverage_expected = cov.get('symbols', {}).get(symbol, {}).get('intervals', {}).get('1h', {}).get('csv_sha256')
            if not expected or sha(raw) != expected or expected != coverage_expected:
                raise ValueError(f'{symbol}: frozen hourly SHA256 mismatch')
            desired = {ms(t['entry_time_utc']) for t in rep['trades']}
            opened = {}
            with io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8-sig', newline='') as stream:
                for row in csv.DictReader(stream):
                    opened_at = int(row['open_time'])
                    if opened_at in desired:
                        if opened_at in opened or row.get('symbol') != symbol or row.get('interval') != '1h' or row.get('is_closed') != 'true':
                            raise ValueError('Duplicated or incorrect entry hourly candle')
                        opened[opened_at] = row
            if set(opened) != desired:
                raise ValueError(f'{symbol}: historical hour missing for a recorded entry')
            candles[symbol] = opened
    return reports, candles, {'historical_zip_sha256': sha(history.read_bytes()),
                               'coverage_sha256': sha(cov_bytes),
                               'report_zip_sha256': {sym: sha(report_paths[sym].read_bytes()) for sym in symbols}}


def audit_trade(t: dict, c: dict) -> dict:
    for field in REQUIRED:
        if field not in t:
            raise ValueError('Missing trade field: '+field)
    entry = ms(t['entry_time_utc']); exit_ = ms(t['exit_time_utc']); signal = ms(t['signal_time_utc'])
    if entry != int(c['open_time']) or entry % HOUR or exit_ <= entry or (exit_ - entry) % HOUR or signal > entry or signal % HOUR:
        raise ValueError('Trade/execution candle time mismatch')
    held = int(t['bars_held'])
    if held != (exit_ - entry)//HOUR or held < 1:
        raise ValueError('Reported holding period inconsistent with exit boundary')
    o, h, l, close = [dec(c[k],k) for k in ('open','high','low','close')]
    if not l <= min(o,close) <= max(o,close) <= h:
        raise ValueError('Invalid entry-hour OHLC')
    ref, stop, target = [dec(t[k],k) for k in ('raw_entry_price','stop_price','target_price')]
    side = t['direction']
    if side == 'long':
        if not stop < ref < target: raise ValueError('Inconsistent long stop/target')
        approach = o >= ref
        stop_hour = l <= stop
        target_hour = h >= target
    elif side == 'short':
        if not target < ref < stop: raise ValueError('Inconsistent short stop/target')
        approach = o <= ref
        stop_hour = h >= stop
        target_hour = l <= target
    else:
        raise ValueError('Unexpected direction')
    in_range = l <= ref <= h
    one_hour_stop = held == 1 and str(t['exit_reason']) in ('stop','stop_gap')
    if one_hour_stop and not stop_hour:
        raise ValueError('Recorded same-hour stop is not present in OHLC')
    return {
        'symbol':t['symbol'], 'split':t['split'], 'setup_id':t['setup_id'],
        'entry_time_utc':t['entry_time_utc'], 'exit_time_utc':t['exit_time_utc'],
        'direction':side, 'exit_reason':t['exit_reason'], 'r_multiple':t['r_multiple'],
        'bars_held':held, 'entry_reference':str(ref), 'stop_price':str(stop), 'target_price':str(target),
        'entry_bar_open':str(o), 'entry_bar_high':str(h), 'entry_bar_low':str(l), 'entry_bar_close':str(close),
        'reference_in_hourly_range':in_range,
        'hour_open_approach_side_of_reference':approach,
        'hourly_open_reference_abs_bps':round(float(abs(o-ref)/ref)*10000,3),
        'entry_hour_stop_touched':stop_hour,
        'entry_hour_target_touched':target_hour,
        'entry_hour_both_stop_and_target':bool(stop_hour and target_hour),
        'same_hour_stop':one_hour_stop,
        'first_hour_stop_before_reference_possible':bool(one_hour_stop and not approach and in_range),
        'first_hour_stop_reference_crossing_before_stop_under_continuous_path':bool(one_hour_stop and approach and in_range),
        'entry_timestamp_is_hour_open_proxy':True,
        'actual_limit_fill_unproven':True,
        'minute_status':'not_requested',
        'minute_reference_first_at':None,'minute_stop_first_at':None,
        'minute_stop_vs_reference':'not_checked',
        'minute_sha256':None,
    }


def summarize(rows: list[dict]) -> dict:
    n=len(rows); wins=sum(float(t['r_multiple'])>0 for t in rows)
    return {'trades':n, 'wins':wins, 'win_rate_pct':round(100*wins/n,3) if n else None,
            'same_hour_stops':sum(t['same_hour_stop'] for t in rows),
            'stops_within_two_hours':sum(t['bars_held']<=2 and t['exit_reason'] in ('stop','stop_gap') for t in rows),
            'entry_reference_not_in_1h_range':sum(not t['reference_in_hourly_range'] for t in rows),
            'same_hour_stop_with_order_unresolved_1h':sum(t['first_hour_stop_before_reference_possible'] for t in rows),
            'same_hour_stop_with_approach_side_open':sum(t['first_hour_stop_reference_crossing_before_stop_under_continuous_path'] for t in rows),
            'entry_hour_both_stop_target':sum(t['entry_hour_both_stop_and_target'] for t in rows),
            'minute_coverage':dict(Counter(t['minute_status'] for t in rows)),
            'minute_stop_vs_reference':dict(Counter(t['minute_stop_vs_reference'] for t in rows if t['minute_status']=='complete_verified'))}


def _validate_1m(raw: Any, hourly: dict, entry_ms: int) -> list[dict] | None:
    """Validate exact complete 60 x 1m partition and 1h aggregate equality."""
    if not isinstance(raw,list) or len(raw)!=60:
        return None
    result=[]
    for i, rec in enumerate(raw):
        if not isinstance(rec, list) or len(rec)<7 or type(rec[0]) is not int or rec[0]!=entry_ms+i*MINUTE:
            return None
        if type(rec[6]) is not int or rec[6]!=rec[0]+MINUTE-1:
            return None
        try:
            o,h,l,c=[dec(rec[k],f'minute_{k}') for k in (1,2,3,4)]
        except ValueError:
            return None
        if not l<=min(o,c)<=max(o,c)<=h: return None
        result.append({'time':rec[0],'open':o,'high':h,'low':l,'close':c})
    check={'open':result[0]['open'],'close':result[-1]['close'],
           'high':max(r['high'] for r in result), 'low':min(r['low'] for r in result)}
    if any(check[k]!=dec(hourly[k],k) for k in check):
        return None
    return result


def minute_order(row: dict, minute: list[dict]) -> dict:
    ref=dec(row['entry_reference'],'ref'); stop=dec(row['stop_price'],'stop')
    side=row['direction']
    def touch(x: dict, level: Decimal) -> bool:
        return x['low']<=level<=x['high']
    def stop_hit(x: dict) -> bool:
        return x['low']<=stop if side=='long' else x['high']>=stop
    ref_candidates=[i for i,m in enumerate(minute) if touch(m,ref)]
    stop_candidates=[i for i,m in enumerate(minute) if stop_hit(m)]
    ref_i=ref_candidates[0] if ref_candidates else None
    stop_i=stop_candidates[0] if stop_candidates else None
    if ref_i is None:
        order='reference_not_seen_in_1m'
    elif stop_i is None:
        order='stop_not_seen_in_entry_hour'
    elif stop_i < ref_i:
        order='stop_first_observed_before_reference_minute'
    elif stop_i == ref_i:
        order='same_1m_bar_order_unresolved'
    else:
        order='reference_minute_before_stop_minute'
    # A stop earlier than a potential reference minute does not prove a real
    # post-reference stop would never occur. Flag any additional later touch.
    later=bool(ref_i is not None and any(i>ref_i for i in stop_candidates))
    return {'minute_reference_first_at':ref_i,'minute_stop_first_at':stop_i,
            'minute_stop_vs_reference':order,'minute_stop_again_after_reference_minute':later}


def verify_minute(rows: list[dict], candles: dict, cache_dir: Path, *, client: Any=None,
                  throttle_seconds: float=.1, test_only: bool=True, max_requests: int=150) -> None:
    """Do not use 1m candles to introduce new trading signals. Audit entry-hour path only."""
    if client is None:
        from binance_market_data import BinanceClient
        client=BinanceClient()
    todo=[r for r in rows if (not test_only or r['split']=='test')]
    if len(todo)>max_requests or max_requests<=0:
        raise ValueError('1m API request budget exceeded; no calls attempted')
    cache_dir.mkdir(parents=True,exist_ok=True)
    for row in todo:
        at=ms(row['entry_time_utc']); sym=row['symbol']
        cache=cache_dir / f'{sym}_{at}_1m.json'
        raw=None
        if cache.exists():
            try: raw=json.loads(cache.read_text(encoding='utf-8'))
            except (ValueError,OSError): raw=None
        if raw is None:
            try:
                raw=client.get_json(SPOT_PUBLIC_DATA_BASE,'/api/v3/klines',{
                    'symbol':sym,'interval':'1m','startTime':at,
                    'endTime':at+HOUR-1,'limit':60})
                if isinstance(raw,list):
                    raw_data=json.dumps(raw,ensure_ascii=False,separators=(',',':')).encode('utf-8')
                    tmp=cache.with_suffix('.json.tmp');tmp.write_bytes(raw_data);tmp.replace(cache)
            except Exception as ex:
                row['minute_status']='api_error:'+type(ex).__name__
                continue
            if throttle_seconds>0:time.sleep(throttle_seconds)
        candles_1m=_validate_1m(raw,candles[sym][at],at)
        if candles_1m is None:
            row['minute_status']='incomplete_or_hourly_mismatch'
            continue
        row['minute_status']='complete_verified'
        row['minute_sha256']=sha(cache.read_bytes())
        row.update(minute_order(row,candles_1m))


def run(history_zip: Path, report_zips: list[Path], symbols: list[str], output_dir: Path,
        *, minute_verify: bool=False, client: Any=None, request_budget: int=150) -> dict:
    if not symbols or len(set(symbols))!=len(symbols) or any(not s.endswith('USDT') for s in symbols):
        raise ValueError('Invalid symbol list')
    reports,candles,provenance=load_archive(history_zip,report_zips,symbols)
    rows=[]
    for symbol in symbols:
        rep=reports[symbol]
        for trade in rep['trades']:
            if trade.get('symbol')!=symbol or trade.get('split') not in ('train','validation','test'):
                raise ValueError('Trade symbol/split does not match frozen baseline')
            rows.append(audit_trade(trade,candles[symbol][ms(trade['entry_time_utc'])]))
    if minute_verify:
        verify_minute(rows,candles,output_dir/'1m_evidence',client=client,max_requests=request_budget)
    summary={split:{sym:summarize([r for r in rows if r['split']==split and r['symbol']==sym]) for sym in symbols}
             for split in ('train','validation','test')}
    summary['test_all_symbols']=summarize([r for r in rows if r['split']=='test'])
    report={'version':VERSION,'formal_strategy':'mtf-bos-ob-retest-v1','mode':'observational_fill_time_audit_only',
            'timeframes_used':'1h OHLC plus optional same-entry-hour 1m only',
            'minute_verification_requested':minute_verify,
            'symbols':symbols,'provenance':provenance,'summary':summary,
            'limitations':[
                '1H timestamp records candle open, NOT actual fill time; even 1m ordering cannot prove executions, queue priority, spread, tick jumps, slippage or trading account eligibility.',
                'OB zone boundaries and queued order type are not included in frozen trade artifacts: reference price touch is checked, full OB zone order placement cannot be reconstructed.',
                'The simulator cancels pending trade if target and OB zone both touch the same 1H candle: this audit only inspects RECORDED trades and is not a complete signal/opportunity reconstruction.',
                'A 1m candle that contains both stop and reference remains unresolved internally; earlier stop touches do not exclude later stop touches after reference.',
                'Checking intrabar data AFTER backtest does not create a new historically causal trading signal or legitimize applying future-information filters.',
                'Current-volume universe selected after the historical evaluation; multi-asset results are correlated and contain selection/survivorship bias.',
                'No modifications of original trade PnL, trade decisions, strategy or risk settings.'
            ]}
    output_dir.mkdir(parents=True,exist_ok=True)
    (output_dir/'execution_audit_02.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    if rows:
        keys=list(dict.fromkeys(k for row in rows for k in row))
        with (output_dir/'execution_audit_02_trades.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=keys);writer.writeheader();writer.writerows(rows)
    lines=['# EXECUTION-AUDIT-02｜跨幣種 OB 成交與當根停損可信度稽核','',
           '僅觀察既有策略歷史交易，並非重新回測或真實交易成交證據。','',
           '| 幣種 | Test 交易 | 當根停損 | 兩根內停損 | 參考價未落在當根範圍 | 1H 先後順序可能顛倒 | 已完成 1m 驗證 |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for sym in symbols:
        item=summary['test'][sym]
        lines.append(f'| {sym} | {item["trades"]} | {item["same_hour_stops"]} | {item["stops_within_two_hours"]} | {item["entry_reference_not_in_1h_range"]} | {item["same_hour_stop_with_order_unresolved_1h"]} | {item["minute_coverage"].get("complete_verified",0)} |')
    minute_test=summary['test_all_symbols']
    lines += ['', '## Test 1m 查核覆蓋率', '',
              '- 1m 資料狀態：' + json.dumps(minute_test['minute_coverage'], ensure_ascii=False),
              '- 1m 首次觸價先後：' + json.dumps(minute_test['minute_stop_vs_reference'], ensure_ascii=False),
              '']
    lines+=['','## 解讀','',
            '- 1H 統計的「先後順序可能顛倒」是：回測當根開盤價格已位於參考價另一側，且當根記錄停損。只有 OHLC 無法證明先碰參考價還是停損。',
            '- 1m 只縮小同時觸價的不確定性；同一分鐘兩價都被觸及時，仍不知次序。',
            '- 停損快速發生不代表策略可透過剔除這些交易改善，事後篩選會有前視偏差。',
            '- 同一根 1H 裡先觸及獲利目標與 OB 的未成交機會不在這份交易明細內，必須另行稽核全部掛單候選。',
            '- 此報告沒有提供實際交易勝率的修正版，也不能推導 1m 真實成交率。']
    (output_dir/'execution_audit_02.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return report


def main(argv: list[str] | None=None) -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history-zip',type=Path,required=True)
    p.add_argument('--report-zip',type=Path,action='append',required=True)
    p.add_argument('--symbols',default='BTCUSDT,ETHUSDT,SOLUSDT')
    p.add_argument('--output-dir',type=Path,default=Path('research/execution_audit_02'))
    p.add_argument('--minute-verify',action='store_true')
    p.add_argument('--request-budget',type=int,default=150)
    args=p.parse_args(argv)
    r=run(args.history_zip,args.report_zip,[x.strip() for x in args.symbols.split(',')],args.output_dir,
          minute_verify=args.minute_verify,request_budget=args.request_budget)
    print(json.dumps({'output_dir':str(args.output_dir),'summary':r['summary']['test_all_symbols']},ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())