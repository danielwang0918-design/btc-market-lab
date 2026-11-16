#!/usr/bin/env python3
"""Frozen forward-validation journal for unchanged 1D/4H/1H MTF strategy.

The initial run is only a registration preflight. Observations can begin at
2026-11-17 00:00 UTC, never from the old exploratory Test sample.
No live orders, sub-hour signals, strategy tuning, or future-data fabrication.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import io
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

VERSION = 'p0-forward-prospective-ledger-v1'
LOCK_SHA256 = '42c0d049a8f277934b2908767869e1c6830009c47f346463140c9abac7549dbf'
ALLOWED_FRAMES = ('1d', '4h', '1h')
MS_PER = {'1d': 86_400_000, '4h': 14_400_000, '1h': 3_600_000}
START = '2026-11-17T00:00:00+00:00'
END = '2028-01-01T00:00:00+00:00'
SOURCE = 'Binance Spot REST API'
BASELINE_RUN = 43820018876


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def parse_utc(ts: str) -> int:
    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    if not dt.tzinfo:
        raise ValueError('UTC timestamp must contain timezone')
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(obj, dict):
        raise ValueError(f'Expected object: {path}')
    return obj


def dump_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    tmp.replace(path)


def lock_check(path: Path, repo: Path, *, baseline_aggregate: Path | None = None) -> dict[str, Any]:
    if sha_file(path) != LOCK_SHA256:
        raise ValueError('PRE-REGISTERED PROTOCOL HASH DRIFT: hard stop')
    lock = read_json(path)
    if (lock.get('protocol_version') != 'p0-forward-v1' or
            lock.get('forward_start_utc') != START or lock.get('forward_end_exclusive_utc') != END or
            lock.get('baseline_run_id') != BASELINE_RUN or
            lock.get('formal_engine', {}).get('allowed_decision_timeframes') != list(ALLOWED_FRAMES) or
            lock.get('formal_engine', {}).get('real_order_placement') is not False):
        raise ValueError('Frozen study boundary, horizon, or 1H+ decision constraint changed')
    if (len(lock.get('symbols', [])) != 8 or len(set(lock['symbols'])) != 8 or
            len(lock.get('frozen_data_prefixes', {})) != 8 * len(ALLOWED_FRAMES)):
        raise ValueError('Frozen eligible universe/interval count changed')
    if lock['symbols'] != ['BTCUSDT','ETHUSDT','SOLUSDT','ZECUSDT','XRPUSDT','BNBUSDT','NEARUSDT','DOGEUSDT']:
        raise ValueError('Symbols changed: avoid post-selection of winners')
    for name, oid in lock['original_source_git_blob_sha1'].items():
        file = repo / name
        if not file.is_file() or git_blob_sha(file) != oid:
            raise ValueError(f'FROZEN STRATEGY CODE DRIFT: {name}; do not score forward observations')
    if baseline_aggregate:
        if sha_file(baseline_aggregate) != lock['baseline_aggregate_sha256']:
            raise ValueError('Frozen baseline aggregate hash differs')
        agg = read_json(baseline_aggregate)
        if agg.get('status') != 'complete' or agg.get('complete_symbols') != lock['symbols']:
            raise ValueError('Frozen baseline eligible symbols changed')
    return lock


def _verify_ohlcv_tail(name: str, meta: dict[str, Any], tail: bytes, header: bytes,
                       *, asof_ms: int | None) -> tuple[int, int, list[int]]:
    if not tail:
        return 0, int(meta['frozen_last_close_exclusive_ms']), []
    if not tail.endswith(b'\n'):
        raise ValueError(f'{name}: Partial appended candle line')
    try:
        hdr = header.decode('utf-8-sig').rstrip('\r\n')
        csv_text = hdr + '\n' + tail.decode('utf-8')
        records = csv.DictReader(io.StringIO(csv_text, newline=''))
        last_close = int(meta['frozen_last_close_exclusive_ms'])
        opens: list[int] = []
        for row in records:
            if (row.get('symbol') != meta['symbol'] or row.get('interval') != meta['interval'] or
                row.get('is_closed', '').lower() != 'true' or row.get('source') != SOURCE):
                raise ValueError(f'{name}: Untrusted interval, symbol, source or open candle')
            op_ms = int(row['open_time'])
            step = MS_PER[meta['interval']]
            if op_ms != last_close or op_ms % step:
                raise ValueError(f'{name}: Missing, duplicated or unsorted appended closed K-line')
            if int(row['close_time']) != op_ms + step - 1:
                raise ValueError(f'{name}: Bad end-exclusive timestamp')
            if asof_ms is not None and op_ms + step > asof_ms:
                raise ValueError(f'{name}: Future or currently unclosed candle is present')
            values = [float(row[k]) for k in ('open','high','low','close','volume')]
            if any(not math.isfinite(v) for v in values):
                raise ValueError(f'{name}: Non-finite OHLCV')
            o, h, l, c, vol = values
            if not (l > 0 and vol >= 0 and l <= min(o,c) <= max(o,c) <= h):
                raise ValueError(f'{name}: Invalid OHLCV')
            if row.get('open_time_utc') != utc(op_ms) or row.get('close_time_utc') != utc(op_ms + step - 1):
                raise ValueError(f'{name}: UTC candle timestamp labels mismatched')
            last_close = op_ms + step
            opens.append(op_ms)
    except (UnicodeError, ValueError, KeyError, csv.Error) as e:
        raise ValueError(f'{name}: Rejected appended evidence: {e}') from e
    return len(opens), last_close, opens


def validate_sources(lock: dict[str, Any], directory: Path, *, asof_ms: int | None,
                     previous: dict[str, Any] | None = None, require_current: bool = False) -> dict[str, Any]:
    """Verify exact immutable frozen bytes, then only contiguous closed 1D/4H/1H CSV appended rows."""
    if previous is not None:
        verify_record(previous, lock)
    out: dict[str, Any] = {}
    for name, meta in lock['frozen_data_prefixes'].items():
        path = directory / name
        if not path.is_file():
            raise ValueError(f'Missing prospective candle file: {name}')
        prefix_length = int(meta['bytes'])
        hasher = hashlib.sha256()
        with path.open('rb') as f:
            prefix = f.read(prefix_length)
            if len(prefix) != prefix_length or sha_bytes(prefix) != meta['sha256']:
                raise ValueError(f'{name}: original frozen Binance candle prefix CHANGED')
            tail = f.read()
        header = prefix.splitlines(keepends=True)[0]
        added, last_exclusive, new_open_times = _verify_ohlcv_tail(name, meta, tail, header, asof_ms=asof_ms)
        full_bytes = prefix + tail
        if previous is not None:
            prior = previous['files'].get(name)
            if prior is None or len(full_bytes) < prior['full_bytes']:
                raise ValueError(f'{name}: Historical forward rows truncated')
            if sha_bytes(full_bytes[:prior['full_bytes']]) != prior['full_sha256']:
                raise ValueError(f'{name}: Already observed prospective bars were REWRITTEN')
        if require_current:
            assert asof_ms is not None
            required_close = asof_ms // MS_PER[meta['interval']] * MS_PER[meta['interval']]
            if last_exclusive < required_close:
                raise ValueError(f'{name}: Stale or missing most recent closed {meta["interval"]} candles')
        hasher.update(full_bytes)
        out[name] = {
            'full_bytes': len(full_bytes), 'full_sha256': hasher.hexdigest(),
            'appended_closed_candles': added, 'total_bars': meta['frozen_bars'] + added,
            'last_closed_end_exclusive_utc': utc(last_exclusive),
            'new_first_open_utc': utc(new_open_times[0]) if added else None,
        }
    return out


def record_hash(manifest: dict[str, Any]) -> str:
    body = {k:v for k,v in manifest.items() if k != 'record_sha256'}
    return sha_bytes(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())


def verify_record(previous: dict[str, Any], lock: dict[str, Any]) -> None:
    if (previous.get('version') != VERSION or
            previous.get('lock_sha256') != LOCK_SHA256 or
            previous.get('record_sha256') != record_hash(previous) or
            set(previous.get('files', {})) != set(lock['frozen_data_prefixes'])):
        raise ValueError('Invalid prior forward evidence record, chain or frozen universe')


def summary_trades(trades_by_symbol: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    all_trades: list[dict[str, Any]] = []
    by_symbol = {}
    for s, trades in trades_by_symbol.items():
        values = [float(t['r_multiple']) for t in trades]
        gains = sum(max(0.,v) for v in values)
        losses = -sum(min(0.,v) for v in values)
        by_symbol[s] = {'count':len(values),'wins':sum(v>0 for v in values),
                        'win_rate_percent':round(100*sum(v>0 for v in values)/len(values),4) if values else None,
                        'expectancy_r':round(sum(values)/len(values),8) if values else None,
                        'profit_factor':round(gains/losses,8) if losses else ('infinite' if gains else None)}
        all_trades.extend({'symbol':s,**t} for t in trades)
    values = [float(t['r_multiple']) for t in all_trades]
    entry_weeks = {(datetime.fromisoformat(t['entry_time_utc']).isocalendar().year,
                    datetime.fromisoformat(t['entry_time_utc']).isocalendar().week) for t in all_trades}
    return {'observational_completed_trades': len(all_trades),
            'entry_utc_iso_weeks':len(entry_weeks),
            'pooled_expectancy_r_descriptive_only':round(sum(values)/len(values),8) if values else None,
            'by_symbol':by_symbol,
            'readout_gate_200_trades_26_weeks_met':len(all_trades)>=200 and len(entry_weeks)>=26,
            'portfolio_return_claim':False,'positive_edge_claim':False}


def simulate_forward(lock:dict[str,Any],data_dir:Path,asof_ms:int, *,engine=None)->dict[str,Any]:
    """Recompute original formal simulator from fixed forward boundary; never carry 2025 Test orders.

    Time-consuming full as-of rebuild is deliberate: do not replace original
    1H/4H/1D event logic with a faster approximated signal engine.
    """
    if engine is None:
        import historical_backtest as historical
        engine=historical
    if tuple(engine.INTERVALS)!=ALLOWED_FRAMES:
        raise ValueError('Only previously frozen 1D/4H/1H engine is permitted')
    start_ms = parse_utc(START)
    horizon_ms = min(asof_ms,parse_utc(END))
    trades_by_symbol:dict[str,list[dict[str,Any]]]={}
    for symbol in lock['symbols']:
        rows,snapshots,_ = engine._prepare_snapshots(
            data_dir,symbol,
            fee_bps_per_side=10.0,slippage_bps_per_side=5.0,
            minimum_net_rr=1.5,max_setup_age_hours=168,progress_every=20000)
        opens=[int(row['open_time']) for row in rows]
        start=bisect.bisect_left(opens,start_ms)
        end=bisect.bisect_left(opens,horizon_ms)
        if not start<end or any(int(row['close_time'])+1>horizon_ms for row in rows[start:end]):
            raise ValueError(f'{symbol}: no fully closed forward 1H candles')
        trades=engine._simulate_slice(rows,snapshots,start,end,
            minimum_net_rr=1.5,max_setup_age_hours=168,
            fee_bps=Decimal('10'),slippage_bps=Decimal('5'))
        # Original engine forcibly settles at a slice boundary. The daily
        # checkpoint is NOT a genuine exit. It must not count as a closed trade.
        completed=[t for t in trades if t['exit_reason']!='split_or_segment_end']
        for t in completed:
            if not (parse_utc(START) <= parse_utc(t['signal_time_utc']) <= parse_utc(t['entry_time_utc']) < parse_utc(t['exit_time_utc']) <= horizon_ms):
                raise ValueError(f'{symbol}: a pre-registration or unclosed trade leaked into forward results')
        trades_by_symbol[symbol]=completed
    return {'status':'forward_observations_only',
            'metrics':summary_trades(trades_by_symbol),
            'completed_trades':[{**t,'symbol':s} for s in lock['symbols'] for t in trades_by_symbol[s]],
            'caveats':['Every refresh recomputes with the unchanged frozen simulator; unclosed trades are not counted.',
                       'Any future old results are invalid if original engine code or frozen historical prefix changed.',
                       'No orders executed; fills are OHLC approximations; correlated crypto symbols are not independent.',
                       'Not a newly untouched holdout for hypotheses invented after start.']}


def make_record(lock: dict[str, Any], files: dict[str,Any], asof_ms: int,
                *, previous:dict[str,Any]|None = None, payload:dict[str,Any]|None=None)->dict[str,Any]:
    if previous is not None and asof_ms <= parse_utc(previous['asof_utc']):
        raise ValueError('Cannot replay or backdate an already observed checkpoint')
    record={
        'version':VERSION,'lock_sha256':LOCK_SHA256,'asof_utc':utc(asof_ms),
        'previous_record_sha256':previous['record_sha256'] if previous else None,
        'files':files,
        'result':payload or {'status':'preregistered_not_started','completed_trades':[]},
        'warning':'Hash-chained, append-only relative to supplied prior record; requires externally archived immutable checkpoints to resist whole-chain replacement.',
    }
    record['record_sha256']=record_hash(record)
    return record


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description='Frozen 1D/4H/1H prospective evaluation; never places orders')
    p.add_argument('mode',choices=['preflight','observe'])
    p.add_argument('--lock',type=Path,default=Path('PROSPECTIVE_LOCK_V1.json'))
    p.add_argument('--repo-dir',type=Path,default=Path('.'))
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--baseline-aggregate',type=Path)
    p.add_argument('--prior-record',type=Path)
    p.add_argument('--asof',type=str)
    p.add_argument('--out',type=Path,default=Path('research/prospective_07/checkpoint.json'))
    a=p.parse_args(argv)
    lock=lock_check(a.lock,a.repo_dir,baseline_aggregate=a.baseline_aggregate)
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    asof_ms=parse_utc(a.asof) if a.asof else now // MS_PER['1h'] * MS_PER['1h']
    if asof_ms%MS_PER['1h'] or asof_ms>now:
        raise ValueError('Observations must use an already closed UTC 1H time boundary, never the future')
    start_ms=parse_utc(START)
    if a.mode=='preflight':
        if asof_ms>=start_ms:
            raise ValueError('PRE-REGISTRATION TOO LATE: future window already opened')
        if a.prior_record is not None:
            raise ValueError('Pre-registration cannot carry a prior record')
        files=validate_sources(lock,a.data_dir,asof_ms=None)
        if any(x['appended_closed_candles'] for x in files.values()):
            raise ValueError('Initial frozen research data already modified/appended')
        report={'status':'preregistered_not_started','decision_timeframes':['1d','4h','1h'],
                'future_trade_count':0,'eligible_symbols':lock['symbols'],
                'protocol_start_utc':START,'protocol_end_exclusive_utc':END}
        record=make_record(lock,files,asof_ms,payload=report)
    else:
        if asof_ms<start_ms or asof_ms>parse_utc(END):
            raise ValueError('Outside preregistered evaluation period')
        if not a.prior_record:
            raise ValueError('A prior verified checkpoint is required for append-only forward observations')
        prev=read_json(a.prior_record)
        files=validate_sources(lock,a.data_dir,asof_ms=asof_ms,previous=prev,require_current=True)
        report=simulate_forward(lock,a.data_dir,asof_ms)
        record=make_record(lock,files,asof_ms,previous=prev,payload=report)
    dump_json(a.out,record)
    print(json.dumps({'status':record['result']['status'],'lock_sha256':LOCK_SHA256,
                      'record_sha256':record['record_sha256'],'asof_utc':record['asof_utc'],
                      'completed_trade_count':record['result'].get('metrics',{}).get('observational_completed_trades',0)},ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())