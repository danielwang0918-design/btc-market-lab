#!/usr/bin/env python3
"""Fixed-rule, calendar-split Binance multi-asset baseline research, never orders.

Runs the EXISTING mtf-bos-ob-retest-v1 snapshot builder and trade simulator,
without modifying entry/stop/target logic.  Not a point-in-time universe study;
the assets were selected using later/current 24-hour volume.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import importlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

VERSION = 'multi-asset-calendar-baseline-v1'
SYMBOL_RE = re.compile(r'^[A-Z0-9]{2,24}USDT$')
SPLIT_ENDS = {'train': '2023-01-01', 'validation': '2025-01-01'}
ASSUMPTIONS = {
    'formal_strategy': 'mtf-bos-ob-retest-v1',
    'fee_bps_per_side': 10.0,
    'slippage_bps_per_side': 5.0,
    'minimum_net_rr': 1.5,
    'max_setup_age_hours': 168,
    'entry_rules_unchanged': True,
    'stop_first_same_bar': True,
    'no_15m_data': True,
}
MIN_HOURLY_PER_SPLIT = 1000
MIN_TEST_TRADES_FOR_EXPLORATORY_READOUT = 20  # Not a statistical validation threshold.


def _date_ms(date_text: str) -> int:
    try:
        date = datetime.strptime(date_text, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError('Split boundaries must be YYYY-MM-DD UTC dates') from exc
    return int(date.timestamp() * 1000)


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(body, encoding='utf-8')
    temp.replace(path)


def _write_json(path: Path, body: dict[str, Any]) -> None:
    _write(path, json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _manifest(coverage: dict[str, Any]) -> tuple[dict[str, Any], list[str], int]:
    if coverage.get('version') != 'multi-asset-history-v1' or coverage.get('status') != 'complete':
        raise ValueError('Frozen coverage must be a complete multi-asset-history-v1 report')
    universe = coverage.get('universe')
    if not isinstance(universe, dict) or universe.get('version') != 'multi-asset-history-v1':
        raise ValueError('Invalid frozen universe in coverage')
    if universe.get('selection_mode') not in ('top_24h_spot_usdt_volume', 'explicit'):
        raise ValueError('Unsupported universe selection provenance')
    entries = universe.get('symbols')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
        raise ValueError('Universe must include 1..50 symbols')
    names = [e.get('symbol') if isinstance(e, dict) else None for e in entries]
    if any(not isinstance(n, str) or not SYMBOL_RE.fullmatch(n) for n in names) or len(set(names)) != len(names):
        raise ValueError('Malformed or duplicated symbol in universe')
    if set(names) != set(coverage.get('symbols', {})):
        raise ValueError('Coverage symbols differ from frozen universe')
    timestamp = universe.get('selected_at_server_ms')
    if type(timestamp) is not int or timestamp <= 0:
        raise ValueError('Missing reliable frozen selection time')
    test_end = timestamp // 86_400_000 * 86_400_000  # last fully completed UTC day
    return universe, names, test_end


def _hour_opens(path: Path, symbol: str, *, expected_count: int) -> list[int]:
    result: list[int] = []
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not {'symbol', 'interval', 'open_time', 'is_closed'}.issubset(set(reader.fieldnames or [])):
            raise ValueError(f'Incomplete CSV header: {path}')
        previous = None
        for row in reader:
            if row['symbol'] != symbol or row['interval'] != '1h' or row['is_closed'].strip().lower() != 'true':
                raise ValueError('Symbol mismatch or unclosed row in 1h history')
            val = int(row['open_time'])
            if val < 0 or val % 3_600_000 or (previous is not None and val <= previous):
                raise ValueError('Invalid or unsorted hourly candle opening')
            previous = val
            result.append(val)
    if len(result) != expected_count:
        raise ValueError(f'1h candle count mismatch: manifest {expected_count}, actual {len(result)}')
    return result


def _interval_bounds(open_times: list[int], train_end: int, validation_end: int, test_end: int) -> dict[str, tuple[int, int]]:
    if not train_end < validation_end < test_end:
        raise ValueError('Invalid fixed calendar split boundaries')
    a = bisect.bisect_left(open_times, train_end)
    b = bisect.bisect_left(open_times, validation_end)
    c = bisect.bisect_left(open_times, test_end)
    return {'train': (0, a), 'validation': (a, b), 'test': (b, c)}


def _minimum_history(bounds: dict[str, tuple[int, int]], minimum: int) -> dict[str, int]:
    if type(minimum) is not int or minimum < 1:
        raise ValueError('Minimum hourly history must be positive')
    return {key: end - start for key, (start, end) in bounds.items()}


def _source_digest_check(data_dir: Path, symbol: str, meta: dict[str, Any]) -> dict[str, str]:
    if meta.get('status') not in ('complete', 'limited_history_under_365_days'):
        raise ValueError(f'Incomplete historical data for {symbol}')
    output = {}
    for interval in ('1d', '4h', '1h'):
        detail = meta.get('intervals', {}).get(interval)
        if not isinstance(detail, dict) or type(detail.get('bars')) is not int or detail['bars'] < 1:
            raise ValueError('Missing/empty candle interval in frozen coverage')
        path = data_dir / f'{symbol}_{interval}.csv'
        actual = _sha(path)
        if actual != detail.get('csv_sha256'):
            raise ValueError(f'{symbol} {interval}: source SHA256 mismatch')
        output[path.name] = actual
    return output


def _wilson(wins: int, n: int) -> list[float] | None:
    if n <= 0:
        return None
    z = 1.959963984540054
    phat = wins / n
    denom = 1 + z * z / n
    mid = (phat + z * z / (2 * n)) / denom
    half = z / denom * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return [round(max(0, 100 * (mid - half)), 3), round(min(100, 100 * (mid + half)), 3)]


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(t['r_multiple']) for t in trades]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    return {
        'trade_count': len(values),
        'wins': len(wins),
        'win_rate_percent': round(100 * len(wins) / len(values), 4) if values else None,
        'win_rate_95pct_wilson_unadjusted': _wilson(len(wins), len(values)),
        'expectancy_r': round(sum(values) / len(values), 8) if values else None,
        'profit_factor': round(sum(wins) / -sum(losses), 8) if losses else ('infinite' if wins else None),
        'sum_trade_r': round(sum(values), 8),
        'sample_under_20_trades': len(values) < MIN_TEST_TRADES_FOR_EXPLORATORY_READOUT,
    }


def run_symbol(data_dir: Path, coverage_path: Path, symbol: str, output_dir: Path,
               *, minimum_hourly: int = MIN_HOURLY_PER_SPLIT, engine: Any = None) -> dict[str, Any]:
    """Run a frozen 1d/4h/1h formal strategy for one eligible symbol.

    All 3 splits use identical global UTC dates. No optimization/search on Test.
    Historical current-volume selection is NOT a point-in-time universe.
    """
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError('Only uppercase Binance USDT spot symbols supported')
    coverage = _json(coverage_path)
    universe, symbols, test_end = _manifest(coverage)
    if symbol not in symbols:
        raise ValueError('Symbol not in frozen recorded universe')
    source_hashes = _source_digest_check(data_dir, symbol, coverage['symbols'][symbol])
    starts = _hour_opens(data_dir / f'{symbol}_1h.csv', symbol,
                         expected_count=coverage['symbols'][symbol]['intervals']['1h']['bars'])
    train_end, validation_end = (_date_ms(SPLIT_ENDS[k]) for k in ('train', 'validation'))
    bounds = _interval_bounds(starts, train_end, validation_end, test_end)
    counts = _minimum_history(bounds, minimum_hourly)
    result = {
        'version': VERSION, 'symbol': symbol, 'strategy': ASSUMPTIONS['formal_strategy'],
        'universe_selection_asof_utc': universe['selected_at_utc'],
        'selection_mode': universe['selection_mode'],
        'fixed_utc_bounds': {'train_end_exclusive': _utc(train_end),
                             'validation_end_exclusive': _utc(validation_end),
                             'test_end_exclusive': _utc(test_end)},
        'hourly_candles_by_split': counts,
        'minimum_hourly_per_split': minimum_hourly,
        'assumptions': ASSUMPTIONS,
        'input_sha256': {coverage_path.name: _sha(coverage_path), **source_hashes},
        'selected_with_future_information': True,
        'warnings': [
            'Current-volume-selected assets are survivors, not a historical investable point-in-time universe.',
            'Assets and trades can be correlated; Wilson intervals do not adjust for dependence.',
            'Trades are simulated from 1H OHLC wick-touch, not a validated order fill.',
            'No backtest tuning or strategy rules changed; this is research only, not investment advice.',
        ],
        'splits': {}, 'trades': [],
    }
    if any(n < minimum_hourly for n in counts.values()):
        result['status'] = 'excluded_insufficient_calendar_coverage'
        result['reason'] = f'At least {minimum_hourly} observed hourly bars required in EACH fixed calendar split'
    else:
        if engine is None:
            engine = importlib.import_module('historical_backtest')
        rows, snapshots, data_summary = engine._prepare_snapshots(
            data_dir, symbol,
            fee_bps_per_side=ASSUMPTIONS['fee_bps_per_side'],
            slippage_bps_per_side=ASSUMPTIONS['slippage_bps_per_side'],
            minimum_net_rr=ASSUMPTIONS['minimum_net_rr'],
            max_setup_age_hours=ASSUMPTIONS['max_setup_age_hours'],
            progress_every=20000,
        )
        actual_times = [int(t['open_time']) for t in rows]
        if actual_times != starts or len(snapshots) != len(starts):
            raise ValueError('Snapshot 1h timestamps diverge from audited input data')
        all_trades = []
        for split, (start_i, end_i) in bounds.items():
            trades = engine._simulate_slice(
                rows, snapshots, start_i, end_i,
                minimum_net_rr=ASSUMPTIONS['minimum_net_rr'],
                max_setup_age_hours=ASSUMPTIONS['max_setup_age_hours'],
                fee_bps=Decimal(str(ASSUMPTIONS['fee_bps_per_side'])),
                slippage_bps=Decimal(str(ASSUMPTIONS['slippage_bps_per_side'])),
            )
            trades.sort(key=lambda t: (t['entry_time_utc'], t['setup_id']))
            all_trades.extend({'symbol': symbol, 'split': split, **t} for t in trades)
            result['splits'][split] = {
                'start_utc': _utc(starts[start_i]) if start_i < end_i else None,
                'last_candle_end_exclusive_utc': _utc(starts[end_i - 1] + 3_600_000) if start_i < end_i else None,
                'hourly_bars': counts[split],
                'metrics': _metrics(trades),
            }
        result['status'] = 'complete'
        result['data_summary'] = data_summary
        result['trades'] = all_trades
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / f'{symbol}_fixed_calendar_baseline.json', result)
    return result


def aggregate(coverage_path: Path, results_dir: Path, output_dir: Path) -> dict[str, Any]:
    coverage = _json(coverage_path)
    universe, symbols, test_end = _manifest(coverage)
    by_symbol = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    incomplete = []
    for symbol in symbols:
        path = results_dir / f'{symbol}_fixed_calendar_baseline.json'
        if not path.exists():
            incomplete.append(symbol)
            by_symbol[symbol] = {'status': 'missing_result'}
            continue
        body = _json(path)
        if body.get('version') != VERSION or body.get('symbol') != symbol or body.get('universe_selection_asof_utc') != universe['selected_at_utc'] or body.get('input_sha256', {}).get(coverage_path.name) != _sha(coverage_path):
            raise ValueError(f'Invalid, inconsistent or stale symbol result for {symbol}')
        status = body.get('status')
        by_symbol[symbol] = {
            'status': status, 'hourly_candles_by_split': body['hourly_candles_by_split'],
            'splits': {k: v['metrics'] for k, v in body['splits'].items()} if status == 'complete' else None,
        }
        if status == 'complete':
            for trade in body['trades']:
                if trade['symbol'] != symbol or trade['split'] not in ('train', 'validation', 'test'):
                    raise ValueError('Trade belongs to a different symbol/split')
                groups[trade['split']].append(trade)
        elif status not in ('excluded_insufficient_calendar_coverage',):
            incomplete.append(symbol)
    test_day_sets: dict[str, set[str]] = defaultdict(set)
    for trade in groups['test']:
        day = trade['entry_time_utc'][:10]
        test_day_sets[day].add(trade['symbol'])
    day_sizes = [len(v) for v in test_day_sets.values()]
    out = {
        'version': VERSION, 'report_type': 'multi_asset_fixed_baseline_aggregate',
        'status': 'complete' if not incomplete else 'partial_failure',
        'universe_selection_asof_utc': universe['selected_at_utc'],
        'selection_mode': universe['selection_mode'], 'symbols_in_universe': symbols,
        'fixed_utc_bounds': {'train_end_exclusive': f"{SPLIT_ENDS['train']}T00:00:00+00:00",
                             'validation_end_exclusive': f"{SPLIT_ENDS['validation']}T00:00:00+00:00",
                             'test_end_exclusive': _utc(test_end)},
        'missing_or_failed_symbols': incomplete,
        'complete_symbols': [s for s in symbols if by_symbol[s]['status'] == 'complete'],
        'excluded_short_history_symbols': [s for s in symbols if by_symbol[s]['status'] == 'excluded_insufficient_calendar_coverage'],
        'by_symbol': by_symbol,
        'pooled_observational': {k: _metrics(v) for k, v in groups.items() if k in ('train', 'validation', 'test')},
        'overlapping_test_entry_days': {
            'unique_test_entry_utc_days': len(day_sizes),
            'utc_days_with_two_or_more_symbols': sum(n >= 2 for n in day_sizes),
            'maximum_distinct_symbols_entering_same_utc_day': max(day_sizes, default=0),
        },
        'input_sha256': {coverage_path.name: _sha(coverage_path)},
        'limitations': [
            'Historical group selected using LATER current-volume rankings: survivorship/hindsight bias.',
            'Cross-symbol pooled R is NOT a portfolio return or proof of independent trades.',
            'The binomial Wilson interval is unadjusted for common-market correlation, repeated setups, and selection.',
            'These are fixed baseline rules only; no candidate parameter search or out-of-sample strategy claims.',
            'No funding, futures, order book or real fill evidence is present: Binance Spot OHLC only.',
            'Every excluded symbol remains in the report; missing or failed artifacts do not silently disappear.',
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / 'multi_asset_fixed_calendar_baseline.json', out)
    lines = ['# Multi-asset fixed-rule BTC/altcoin benchmark', '',
             f"Selection at: {universe['selected_at_utc']}",
             f"Calendar Train ends {SPLIT_ENDS['train']}, Validation ends {SPLIT_ENDS['validation']}, Test ends {_utc(test_end)} (UTC exclusive).", '',
             '| Symbol | Status | Train n / win% | Validation n / win% | Test n / win% | Test expectancy (R) | Test PF |',
             '|---|---|---:|---:|---:|---:|---:|']
    for symbol in symbols:
        entry = by_symbol[symbol]
        if entry.get('splits'):
            split = entry['splits']
            def cell(k: str) -> str:
                m = split[k]
                return f"{m['trade_count']} / {m['win_rate_percent']}%"
            test = split['test']
            lines.append(f"| {symbol} | {entry['status']} | {cell('train')} | {cell('validation')} | {cell('test')} | {test['expectancy_r']} | {test['profit_factor']} |")
        else:
            lines.append(f"| {symbol} | {entry['status']} | — | — | — | — | — |")
    lines += ['', '## Pooled observations (NOT portfolio returns)', '',
              '| Split | Trade count | Win rate | Expectancy (R) | PF | Unadjusted Wilson 95% |',
              '|---|---:|---:|---:|---:|---|']
    for split in ('train', 'validation', 'test'):
        m = out['pooled_observational'].get(split, _metrics([]))
        lines.append(f"| {split} | {m['trade_count']} | {m['win_rate_percent']} | {m['expectancy_r']} | {m['profit_factor']} | {m['win_rate_95pct_wilson_unadjusted']} |")
    lines += ['', '## Data quality interpretation', '',
              f"- Excluded short-history symbols: {', '.join(out['excluded_short_history_symbols']) or 'none'}",
              f"- Missing or failed symbol reports: {', '.join(incomplete) or 'none'}",
              f"- Distinct Test entry days: {len(day_sizes)}; with >=2 symbols: {sum(n >= 2 for n in day_sizes)}.",
              '- Current-volume-selected winners may have survivorship bias and are NOT a historical point-in-time universe.',
              '- Correlated token pairs are NOT independent tests; sample size and Wilson intervals are descriptive only.',
              '- No changes to mtf-bos-ob-retest-v1 formal entry, exit or risk rules.', '']
    _write(output_dir / 'multi_asset_fixed_calendar_baseline.md', '\n'.join(lines))
    flat = []
    for split, values in groups.items():
        for trade in values:
            flat.append({k: trade.get(k, '') for k in ('symbol', 'split', 'setup_id', 'direction', 'signal_time_utc',
                      'entry_time_utc', 'exit_time_utc', 'exit_reason', 'r_multiple', 'bars_held')})
    with (output_dir / 'multi_asset_fixed_calendar_trades.csv').open('w', encoding='utf-8-sig', newline='') as f:
        fields = ['symbol', 'split', 'setup_id', 'direction', 'signal_time_utc', 'entry_time_utc', 'exit_time_utc', 'exit_reason', 'r_multiple', 'bars_held']
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(flat, key=lambda r: (r['split'], r['entry_time_utc'], r['symbol'])))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Binance multi-asset fixed baseline. Research only, never trading.')
    sub = parser.add_subparsers(dest='command', required=True)
    one = sub.add_parser('symbol', help='Run one frozen eligible symbol (the same formal MTF rules)')
    one.add_argument('symbol')
    one.add_argument('--data-dir', type=Path, default=Path('data/multi_asset'))
    one.add_argument('--coverage', type=Path)
    one.add_argument('--output-dir', type=Path, default=Path('research/multi_asset/individual'))
    one.add_argument('--minimum-hourly', type=int, default=MIN_HOURLY_PER_SPLIT)
    total = sub.add_parser('aggregate', help='Aggregate precomputed frozen symbol baselines')
    total.add_argument('--coverage', type=Path, default=Path('data/multi_asset/multi_asset_coverage.json'))
    total.add_argument('--results-dir', type=Path, default=Path('research/multi_asset/individual'))
    total.add_argument('--output-dir', type=Path, default=Path('research/multi_asset/summary'))
    args = parser.parse_args(argv)
    try:
        if args.command == 'symbol':
            coverage = args.coverage or args.data_dir / 'multi_asset_coverage.json'
            report = run_symbol(args.data_dir, coverage, args.symbol, args.output_dir,
                                minimum_hourly=args.minimum_hourly)
            print(json.dumps({'symbol': args.symbol, 'status': report['status'],
                              'counts': report['hourly_candles_by_split'],
                              'test': report['splits'].get('test', {}).get('metrics')}, ensure_ascii=False, allow_nan=False), flush=True)
        else:
            report = aggregate(args.coverage, args.results_dir, args.output_dir)
            print(json.dumps({'status': report['status'], 'complete_symbols': report['complete_symbols'],
                              'pooled_test': report['pooled_observational'].get('test')}, ensure_ascii=False, allow_nan=False), flush=True)
            if report['status'] != 'complete':
                return 2
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, ImportError) as exc:
        parser.exit(1, f'ERROR multi-asset fixed baseline: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())