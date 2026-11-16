#!/usr/bin/env python3
"""Observational 1H OB-touch fill timing / immediate-stop audit; never places orders.

This diagnostic uses fully realized hourly OHLC and therefore MUST NOT be used
as a signal-time trade filter or as intrabar trade-path evidence.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

VERSION = 'ob-entry-execution-audit-v1'
STEP_MS = 3_600_000
STOPS = {'stop', 'stop_gap'}


def _ms(raw: str) -> int:
    parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp must be timezone aware')
    return int(parsed.timestamp() * 1000)


def _num(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f'Invalid {label}') from exc
    if not math.isfinite(number):
        raise ValueError(f'Non-finite {label}')
    return number


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [_num(t['r_multiple'], 'r_multiple') for t in rows]
    wins = sum(x > 0 for x in vals)
    stop_count = sum(t['exit_reason'] in STOPS for t in rows)
    equity = max_equity = max_dd = 0.0
    for t in sorted(rows, key=lambda z: (_ms(z['exit_time_utc']), z['setup_id'])):
        equity += _num(t['r_multiple'], 'r_multiple')
        max_equity = max(max_equity, equity)
        max_dd = max(max_dd, max_equity - equity)
    gp = sum(x for x in vals if x > 0)
    gl = sum(-x for x in vals if x < 0)
    distances = [t['open_to_reference_abs_bps'] for t in rows]
    return {
        'trades': len(rows), 'wins': wins,
        'win_rate_percent': round(100 * wins / len(rows), 4) if rows else None,
        'expectancy_r': round(sum(vals) / len(vals), 6) if vals else None,
        'profit_factor': round(gp / gl, 6) if gl else None,
        'maximum_closed_trade_drawdown_r': round(max_dd, 6) if rows else None,
        'stop_count': stop_count,
        'one_bar_stops': sum(t['entry_bar_stop'] and t['bars_held'] == 1 and t['exit_reason'] in STOPS for t in rows),
        'stop_within_two_held_bars': sum(t['bars_held'] <= 2 and t['exit_reason'] in STOPS for t in rows),
        'lag_over_one_hour': sum(t['signal_to_entry_bar_hours'] > 1 for t in rows),
        'entry_reference_outside_entry_bar': sum(not t['entry_reference_within_entry_bar'] for t in rows),
        'median_open_to_reference_bps': round(statistics.median(distances), 3) if distances else None,
        'one_bar_stop_open_on_approach_side': sum(t['one_bar_stop_open_on_approach_side'] for t in rows),
        'one_bar_stop_order_indeterminate': sum(t['one_bar_stop_order_indeterminate'] for t in rows),
    }


def analyze_trade(trade: dict[str, Any], hourly: list[dict[str, str]],
                  open_times: list[int]) -> dict[str, Any]:
    """Trade fills are modeled by OB wick touch, but the exact hour-internal fill time is unknown."""
    entry = _ms(trade['entry_time_utc'])
    signal = _ms(trade['signal_time_utc'])
    exit_ms = _ms(trade['exit_time_utc'])
    if entry % STEP_MS or signal % STEP_MS or exit_ms % STEP_MS or not signal <= entry < exit_ms:
        raise ValueError('Signal, entry and exit time boundaries inconsistent')
    entry_i = bisect.bisect_left(open_times, entry)
    exit_open = exit_ms - STEP_MS
    exit_i = bisect.bisect_left(open_times, exit_open)
    if entry_i >= len(hourly) or exit_i >= len(hourly) or open_times[entry_i] != entry or open_times[exit_i] != exit_open:
        raise ValueError('Missing entry or exit candle')
    held = hourly[entry_i:exit_i + 1]
    if any(int(bar['open_time']) != entry + i * STEP_MS for i, bar in enumerate(held)):
        raise ValueError('Missing hourly candle within holding interval')
    held_count = int(trade['bars_held'])
    if held_count != len(held):
        raise ValueError('bars_held conflicts with the actual exit close boundary')
    bar = held[0]
    op, hi, lo, cl = (_num(bar[c], c) for c in ('open', 'high', 'low', 'close'))
    raw = _num(trade['raw_entry_price'], 'raw_entry_price')
    stop = _num(trade['stop_price'], 'stop_price')
    target = _num(trade['target_price'], 'target_price')
    if min(op, hi, lo, cl, raw, stop, target) <= 0 or not lo <= min(op, cl) <= max(op, cl) <= hi:
        raise ValueError('Invalid OHLC or positive trade prices')
    if trade['direction'] == 'long':
        if not stop < raw < target:
            raise ValueError('Long stop/entry/target order invalid')
        approach = op >= raw
        touched_stop = lo <= stop
        touched_target = hi >= target
    elif trade['direction'] == 'short':
        if not target < raw < stop:
            raise ValueError('Short stop/entry/target order invalid')
        approach = op <= raw
        touched_stop = hi >= stop
        touched_target = lo <= target
    else:
        raise ValueError('Unsupported trade direction')
    near = lo <= raw <= hi
    ref_lag = (entry - signal) // STEP_MS
    # IMPORTANT: OB confirmation is historically observable before entry, but
    # execution OHLC, lag and stop flags are POST-TRADE diagnostics only.
    try:
        confirmed_at_close_ms = int(str(trade['setup_id']).split(':')[-2])
        ob_age = (entry - (confirmed_at_close_ms + 1)) / STEP_MS
    except (ValueError, TypeError, IndexError):
        ob_age = None
    stop_this_hour = bool(touched_stop and held_count == 1 and trade['exit_reason'] in STOPS)
    return {
        'variant': trade.get('variant', ''), 'setup_id': trade['setup_id'],
        'split': trade['split'], 'direction': trade['direction'],
        'signal_time_utc': trade['signal_time_utc'],
        'entry_time_utc': trade['entry_time_utc'], 'exit_time_utc': trade['exit_time_utc'],
        'exit_reason': trade['exit_reason'], 'r_multiple': trade['r_multiple'],
        'bars_held': held_count, 'signal_to_entry_bar_hours': int(ref_lag),
        'signal_to_entry_bucket': '0-1h' if ref_lag <= 1 else '2-3h' if ref_lag <= 3 else '4+h',
        'ob_age_at_entry_hours': round(ob_age, 3) if ob_age is not None else None,
        'entry_bar_open': op, 'entry_reference': raw,
        'entry_reference_within_entry_bar': bool(near),
        'open_to_reference_abs_bps': round(abs(op - raw) / raw * 10000, 6),
        'open_on_approach_side_of_entry': bool(approach),
        'entry_bar_stop': bool(touched_stop), 'entry_bar_target': bool(touched_target),
        'one_bar_stop_open_on_approach_side': bool(stop_this_hour and approach and near),
        'one_bar_stop_order_indeterminate': bool(stop_this_hour and not approach and near),
        'entry_timestamp_is_candle_open_proxy': True,
        'intrabar_fill_sequence_observed': False,
    }


def build_audit(data_dir: Path, output_dir: Path, diagnostics: Path | None = None) -> dict[str, Any]:
    diagnostic_path = diagnostics or data_dir / 'BTCUSDT_trade_diagnostics.json'
    hour_path = data_dir / 'BTCUSDT_1h.csv'
    report = json.loads(diagnostic_path.read_text(encoding='utf-8'))
    if report.get('strategy') != 'mtf-bos-ob-retest-v1':
        raise ValueError('Unknown formal strategy version')
    hourly = _load_csv(hour_path)
    times = [int(z['open_time']) for z in hourly]
    if any(b - a <= 0 for a, b in zip(times, times[1:])):
        raise ValueError('Hourly timestamps not strictly increasing')
    rows: list[dict[str, Any]] = []
    by_variant = {}
    for variant, data in report['variants'].items():
        for t in data['trades']:
            row = analyze_trade(t, hourly, times)
            row['variant'] = variant
            rows.append(row)
        by_variant[variant] = {}
        for split in ('train', 'validation', 'test'):
            part = [r for r in rows if r['variant'] == variant and r['split'] == split]
            by_variant[variant][split] = {
                'all': _stats(part),
                'by_signal_to_entry_hours': {key: _stats([r for r in part if r['signal_to_entry_bucket'] == key])
                                             for key in ('0-1h', '2-3h', '4+h')},
            }
    output_dir.mkdir(parents=True, exist_ok=True)
    csvpath = output_dir / 'BTCUSDT_entry_execution_audit.csv'
    if rows:
        with csvpath.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    result = {
        'audit_version': VERSION, 'strategy': report['strategy'],
        'provenance_sha256': {hour_path.name: _sha(hour_path), diagnostic_path.name: _sha(diagnostic_path)},
        'definitions': {
            'exit_timestamp': 'backtest uses actual exiting 1h candle CLOSE + 1 ms, i.e. NEXT hour open',
            'entry_timestamp': 'backtest timestamps a historical OHLC OB-zone wick touch at that candle open, not observed intrabar fill time',
            'signal_to_entry_bar_hours': '>=0 integer hours between signal decision as-of and bar labeled as entry; observational after fill',
            'one_bar_stop_open_on_approach_side': 'opening price on approach side of entry reference; same-bar stop after crossing entry is consistent with continuous price paths but tick path unavailable',
            'interpretation': 'No inference that an entry fill happened at 1H open or that stop/target order was observed from OHLC',
        },
        'variants': by_variant,
        'warnings': [
            'POST-TRADE observational diagnosis ONLY, never join entry-bar OHLC/lag to signal-time trading filters.',
            'Do not treat first-hour stop as proof of early entry; 1H bar path is unknown.',
            'Actual backtest fill price is the conservative_entry_reference when zone wick is touched, and recorded timestamp is bar open.',
            'This audit does not rerun a corrected execution engine or assert revised profitability.',
            'All subgroup comparisons are exploratory and not preselected validation evidence.',
        ],
    }
    (output_dir / 'BTCUSDT_entry_execution_audit.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Research-only BTC OB entry/exit 1H timing audit')
    parser.add_argument('--data-dir', type=Path, default=Path('data/historical'))
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--diagnostics', type=Path)
    args = parser.parse_args(argv)
    result = build_audit(args.data_dir, args.output_dir or args.data_dir, args.diagnostics)
    print(json.dumps({'audit_version': result['audit_version'],
                      'baseline_test': result['variants'].get('ob_1h_baseline', {}).get('test')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())