#!/usr/bin/env python3
"""P0 observational order-lifecycle replay using ONLY 1h,4h,1d closed-bar signals.

No <1h K-lines, no order placement, no strategy modifications, no retrospective
filter selection. Strictly checks replayed trades against frozen baseline evidence.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

VERSION = 'order-state-1h-ob-observational-v1'
DECISION_TIMEFRAMES = ('1h', '4h', '1d')
FIELDS = ('setup_id', 'entry_time_utc', 'exit_time_utc', 'direction', 'exit_reason', 'bars_held', 'r_multiple')


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    tmp.replace(path)


def _num(value: Any) -> Decimal:
    n = Decimal(str(value))
    if not n.is_finite() or n <= 0:
        raise ValueError('Non-finite or nonpositive price')
    return n


def _overlap_classification(pending: dict[str, Any], bar: dict[str, Any]) -> dict[str, Any]:
    """1H OHLC may bound chronology but NEVER establishes a fill."""
    op = _num(bar['open'])
    low, high = _num(bar['low']), _num(bar['high'])
    zone_low, zone_high = _num(pending['zone_low']), _num(pending['zone_high'])
    target, stop = _num(pending['target']), _num(pending['stop'])
    side = pending['side']
    if side == 'long':
        target_at_open = op >= target
        stop_touched = low <= stop
    elif side == 'short':
        target_at_open = op <= target
        stop_touched = high >= stop
    else:
        raise ValueError('Unknown side')
    zone_at_open = zone_low <= op <= zone_high
    if target_at_open:
        ordering = 'target_already_at_open_before_zone_approach'
    elif zone_at_open:
        ordering = 'zone_present_at_open_but_reference_fill_unproven'
    else:
        ordering = 'intrabar_target_vs_zone_order_unknown'
    return {
        'ordering': ordering, 'target_at_open': bool(target_at_open),
        'zone_at_open': bool(zone_at_open), 'also_stop_touched': bool(stop_touched),
        'reference_price_in_hour': bool(low <= _num(pending['entry_reference']) <= high),
        'intra_hour_fill_observed': False,
    }


def trace_split(rows: list[dict[str, Any]], snapshots: list[dict[str, Any]],
                start: int, end: int, *, engine: Any, minimum_net_rr: float = 1.5,
                max_setup_age_hours: int = 168,
                fee_bps: Decimal = Decimal('10'), slippage_bps: Decimal = Decimal('5'),
                split: str = 'test') -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Independent 1H state tracer matching the formal simulator's decision order.

    Captures ALL accepted orders, including canceled/missed opportunities.
    No forward-looking features are used for trading; OHLC is inspected only
    at the same simulated bar where original model also processes that bar.
    """
    if len(rows) != len(snapshots) or not 0 <= start <= end <= len(rows):
        raise ValueError('Invalid rows/snapshot boundaries')
    events: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    consumed: set[str] = set()

    def emit(kind: str, bar: dict[str, Any], setup: dict[str, Any], **extra: Any) -> None:
        events.append({'event': kind, 'split': split,
                       'setup_id': str(setup['setup_id']),
                       'at_1h_open_utc': engine._utc(int(bar['open_time'])), **extra})

    for i in range(start, end):
        bar = rows[i]
        previous = rows[i-1] if i > start else None
        if previous is not None and int(bar['open_time']) - int(previous['open_time']) != 3_600_000:
            if position is not None:
                trades.append(engine._make_trade(position, engine._number(previous['close'], 'close', positive=True),
                              int(previous['close_time']) + 1, 'data_gap_exit', i - 1, fee_bps, slippage_bps))
                emit('position_data_gap_exit', previous, position)
                position = None
            if pending is not None:
                emit('pending_data_gap_cancel', bar, pending)
                pending = None
        snap = snapshots[i]
        if position is not None:
            exit_signal = engine._exit_on_bar(position, bar)
            if exit_signal:
                raw, reason = exit_signal
                trades.append(engine._make_trade(position, raw, int(bar['close_time']) + 1,
                              reason, i, fee_bps, slippage_bps))
                emit('position_exit', bar, position, exit_reason=reason)
                position = None
            elif not snap.get('integrity_ok'):
                trades.append(engine._make_trade(position, engine._number(bar['close'], 'close', positive=True),
                              int(bar['close_time']) + 1, 'integrity_review_exit', i, fee_bps, slippage_bps))
                emit('position_integrity_exit', bar, position)
                position = None
        elif pending is not None:
            age = (int(bar['open_time']) - int(pending['setup_confirm_ms'])) / 3_600_000
            current_candidate = snap.get('candidate')
            if age > max_setup_age_hours or not snap.get('integrity_ok'):
                emit('pending_expired_or_integrity_cancel', bar, pending,
                     reason='age' if age > max_setup_age_hours else 'data_integrity')
                pending = None
            else:
                op, high, low = (engine._number(bar[k], k, positive=True) for k in ('open','high','low'))
                zone_low = engine._number(pending['zone_low'], 'zone_low', positive=True)
                zone_high = engine._number(pending['zone_high'], 'zone_high', positive=True)
                stop = engine._number(pending['stop'], 'stop', positive=True)
                target = engine._number(pending['target'], 'target', positive=True)
                side = pending['side']
                if (side == 'long' and op <= stop) or (side == 'short' and op >= stop):
                    emit('pending_stop_already_breached_at_open_cancel', bar, pending)
                    pending = None
                else:
                    zone_touch = low <= zone_high and high >= zone_low
                    target_touch = high >= target if side == 'long' else low <= target
                    if target_touch and not zone_touch:
                        emit('pending_target_without_zone_cancel', bar, pending)
                        pending = None
                    elif target_touch and zone_touch:
                        emit('pending_target_and_zone_same_hour_cancel', bar, pending,
                             **_overlap_classification(pending, bar))
                        pending = None
                    elif zone_touch:
                        position = {
                            'setup_id': pending['setup_id'],
                            'target_event_id': pending['target_event_id'],
                            'side': side,
                            'signal_time_ms': int(pending['signal_time_ms']),
                            'entry_time_ms': int(bar['open_time']),
                            'entry_index': i,
                            'raw_entry': engine._number(pending['entry_reference'], 'entry_reference', positive=True),
                            'stop': stop,
                            'target': target,
                        }
                        emit('pending_ob_wick_touch_modeled_fill', bar, pending,
                             reference_price_in_hour=bool(low <= position['raw_entry'] <= high),
                             open_time_is_fill_proxy=True, actual_fill_observed=False)
                        consumed.add(pending['setup_id'])
                        pending = None
                        hit = engine._exit_on_bar(position, bar)
                        if hit:
                            raw, reason = hit
                            trades.append(engine._make_trade(position, raw, int(bar['close_time']) + 1,
                                          reason, i, fee_bps, slippage_bps))
                            emit('position_same_hour_exit', bar, position, exit_reason=reason)
                            position = None
                    elif (not engine._candidate_allowed(current_candidate, minimum_net_rr, max_setup_age_hours)
                          or current_candidate.get('setup_id') != pending.get('setup_id')):
                        emit('pending_signal_changed_cancel', bar, pending)
                        pending = None

        candidate = snap.get('candidate')
        if position is None and pending is None and snap.get('integrity_ok'):
            if engine._candidate_allowed(candidate, minimum_net_rr, max_setup_age_hours):
                setup_id = str(candidate['setup_id'])
                if setup_id not in consumed:
                    pending = dict(candidate)
                    pending['signal_index'] = i
                    consumed.add(setup_id)
                    emit('signal_accepted_pending_from_closed_1h', bar, pending,
                         available_only_after_1h_close=True)

    if position is not None:
        last = rows[end - 1]
        trades.append(engine._make_trade(position, engine._number(last['close'], 'close', positive=True),
                      int(last['close_time']) + 1, 'split_or_segment_end', end - 1, fee_bps, slippage_bps))
        emit('position_split_end_exit', last, position)
    if pending is not None:
        emit('pending_split_end_unresolved', rows[end - 1], pending)
    return events, trades


def _signature(trade: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(trade[k]) for k in FIELDS)


def run_symbol(data_dir: Path, baseline_path: Path, output_dir: Path, *,
               coverage_path: Path | None = None, engine: Any = None,
               baseline_module: Any = None) -> dict[str, Any]:
    engine = engine or importlib.import_module('historical_backtest')
    base = baseline_module or importlib.import_module('multi_asset_baseline')
    report = json.loads(baseline_path.read_text(encoding='utf-8'))
    symbol = report['symbol']
    if report.get('status') != 'complete' or report.get('strategy') != 'mtf-bos-ob-retest-v1':
        raise ValueError('Frozen baseline must be a complete formal v1 strategy report')
    coverage_path = coverage_path or data_dir / 'multi_asset_coverage.json'
    coverage = json.loads(coverage_path.read_text(encoding='utf-8'))
    universe, symbols, test_end = base._manifest(coverage)
    if symbol not in symbols:
        raise ValueError('Symbol is not in frozen universe')
    if report['universe_selection_asof_utc'] != universe['selected_at_utc']:
        raise ValueError('Universe freeze time differs between reports')
    if report.get('input_sha256', {}).get(coverage_path.name) != _sha(coverage_path):
        raise ValueError('Frozen manifest SHA256 mismatch')
    if tuple(engine.INTERVALS) != ('1d', '4h', '1h'):
        raise ValueError('Formal engine intervals diverge from locked 1D/4H/1H requirements')
    expected_assumptions = {
        'formal_strategy': 'mtf-bos-ob-retest-v1',
        'fee_bps_per_side': 10.0, 'slippage_bps_per_side': 5.0,
        'minimum_net_rr': 1.5, 'max_setup_age_hours': 168,
        'entry_rules_unchanged': True,
    }
    for key, val in expected_assumptions.items():
        if report.get('assumptions', {}).get(key) != val:
            raise ValueError(f'Strategy assumptions changed: {key}')
    checked = base._source_digest_check(data_dir, symbol, coverage['symbols'][symbol])
    for file_name, digest in checked.items():
        if report.get('input_sha256', {}).get(file_name) != digest:
            raise ValueError(f'Frozen per-symbol source digest mismatched: {file_name}')
    rows, snapshots, summary = engine._prepare_snapshots(
        data_dir, symbol, fee_bps_per_side=10.0, slippage_bps_per_side=5.0,
        minimum_net_rr=1.5, max_setup_age_hours=168, progress_every=20000,
    )
    starts = [int(r['open_time']) for r in rows]
    if starts != base._hour_opens(data_dir / f'{symbol}_1h.csv',symbol,
                  expected_count=coverage['symbols'][symbol]['intervals']['1h']['bars']):
        raise ValueError('Snapshot timeline differs from frozen 1H source')
    bounds = base._interval_bounds(starts, base._date_ms('2023-01-01'),
                                  base._date_ms('2025-01-01'), test_end)
    out_events: list[dict[str, Any]] = []
    by_split: dict[str, dict[str, Any]] = {}
    for split, (start, end) in bounds.items():
        if end - start < 1000:
            raise ValueError('Expected eligible symbol with >=1000 1H in every split')
        events, trades = trace_split(rows, snapshots, start, end, engine=engine, split=split)
        expected = [t for t in report['trades'] if t['split'] == split]
        ours = sorted([_signature(t) for t in trades])
        theirs = sorted([_signature(t) for t in expected])
        if ours != theirs:
            raise ValueError(f'{symbol} {split}: ORDER STATE REPLAY DOES NOT MATCH FROZEN BASELINE'
                             f' (replayed={len(ours)}, baseline={len(theirs)}); no conclusions produced')
        counts = dict(Counter(x['event'] for x in events))
        overlap = [x for x in events if x['event'] == 'pending_target_and_zone_same_hour_cancel']
        by_split[split] = {
            'baseline_trades_verified': len(theirs), 'observed_signal_count': counts.get('signal_accepted_pending_from_closed_1h', 0),
            'modeled_fills': counts.get('pending_ob_wick_touch_modeled_fill', 0),
            'no_fill_pending_cancellations': sum(v for k,v in counts.items() if k.startswith('pending_') and k not in ('pending_ob_wick_touch_modeled_fill','pending_split_end_unresolved')),
            'target_zone_same_1h_cancellations': len(overlap),
            'target_zone_sequence_classification': dict(Counter(x['ordering'] for x in overlap)),
            'pending_at_split_end_unresolved': counts.get('pending_split_end_unresolved', 0),
            'event_counts': counts,
        }
        out_events.extend(events)
    result = {
        'report_type': 'frozen_order_state_transition_audit', 'version': VERSION,
        'symbol': symbol, 'strategy_unchanged': True,
        'decision_timeframes_only': list(DECISION_TIMEFRAMES),
        'subhour_price_data_used': False, 'orders_placed': False,
        'baseline_source_sha256': _sha(baseline_path),
        'coverage_source_sha256': _sha(coverage_path),
        'time_window': report.get('fixed_utc_bounds'),
        'splits': by_split, 'strategy_data_summary': summary,
        'limitations': [
            'Only 1D, 4H, 1H fully closed candles can produce decisions. No subhour candle is read.',
            'A 1H candle showing both OB and target does not reveal the intrabar order; the existing strategy conservatively cancels.',
            '1H wick touch is not a verified limit fill at the modeled reference price.',
            'The results are an observational transition audit, NOT an optimized alternative backtest.',
            'Test data are not an unused holdout and must not select a profitable new decision rule.',
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / f'{symbol}_order_state_audit.json', result)
    events_path = output_dir / f'{symbol}_order_state_events.csv'
    keys = ['symbol','split','event','setup_id','at_1h_open_utc','ordering','target_at_open','zone_at_open',
            'also_stop_touched','reference_price_in_hour','intra_hour_fill_observed','open_time_is_fill_proxy',
            'actual_fill_observed','reason','exit_reason','available_only_after_1h_close']
    with events_path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=keys,extrasaction='ignore');writer.writeheader()
        writer.writerows({'symbol':symbol,**x} for x in out_events)
    return result



def aggregate_reports(report_dir: Path, output_dir: Path,
                      symbols: tuple[str, ...] = ('BTCUSDT','ETHUSDT','SOLUSDT')) -> dict[str, Any]:
    """Require every frozen per-symbol replay to have passed parity before summarizing."""
    if len(set(symbols)) != len(symbols) or not symbols:
        raise ValueError('Unique nonempty symbol list required')
    combined: Counter[str] = Counter()
    splits: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        path = report_dir / f'{symbol}_order_state_audit.json'
        item = json.loads(path.read_text(encoding='utf-8'))
        if item.get('symbol') != symbol or item.get('version') != VERSION or item.get('strategy_unchanged') is not True:
            raise ValueError(f'Invalid audit provenance: {symbol}')
        if item.get('subhour_price_data_used') is not False or tuple(item.get('decision_timeframes_only', [])) != DECISION_TIMEFRAMES:
            raise ValueError('This project forbids trading decisions on sub-hour bars')
        x = item['splits']['test']
        if x['modeled_fills'] != x['baseline_trades_verified']:
            raise ValueError(f'Unmatched trade counts in audit {symbol}')
        splits[symbol] = x
        for k in ('baseline_trades_verified','observed_signal_count', 'modeled_fills',
                  'no_fill_pending_cancellations','target_zone_same_1h_cancellations',
                  'pending_at_split_end_unresolved'):
            combined[k] += x[k]
        for k,v in x['target_zone_sequence_classification'].items():
            combined[f'target_zone_{k}'] += v
    result = {
        'version': VERSION,
        'type': 'three_asset_ob_pending_order_state_audit',
        'status': 'verified_against_frozen_baselines',
        'trade_decision_timeframes': list(DECISION_TIMEFRAMES),
        'uses_subhour_signals': False,
        'assets': splits,
        'test_totals_observational': dict(combined),
        'note': 'Counts are from the same 1H strategy state replay. Canceled signals are not simulated profits.',
        'limits': 'No actual fills, subhour prices, alternative backtest or independent out-of-sample test.',
    }
    output_dir.mkdir(parents=True,exist_ok=True)
    _write_json(output_dir/'order_state_test_summary.json',result)
    lines = [
        '# P0-ORDER-STATE-01｜只用 1H／4H／1D 的 OB 掛單狀態稽核', '',
        '這是對正式回測的完全相同交易逐筆核對；不改進出場規則、不使用 1m/15m、也不進行實盤下單。', '',
        '| 幣種 | Test 已配對成交 | 已接受候選訊號 | 同根 OB＋目標價而取消 | 沒有成交的取消 |',
        '|---|---:|---:|---:|---:|',
    ]
    for sym,x in splits.items():
        lines.append(f"| {sym} | {x['baseline_trades_verified']} | {x['observed_signal_count']} | {x['target_zone_same_1h_cancellations']} | {x['no_fill_pending_cancellations']} |")
    lines += ['', '## 總體（已配對交易，不是獨立樣本）', '']
    for k,v in combined.items():
        lines.append(f'- {k}: {v}')
    lines += ['', '**重要**：1H OHLC 不足以判斷同根內 OB 與目標價的先後，取消個案不可直接視為原策略漏掉的獲利機會。',
              '「已觸及 OB」是模型假設，不代表真實掛單成交。',
              '不重新使用 Test 來調參，不依此直接交易。']
    (output_dir/'order_state_test_summary.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('symbol', choices=('BTCUSDT','ETHUSDT','SOLUSDT','aggregate'))
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--baseline-dir',type=Path,required=False)
    p.add_argument('--output-dir',type=Path,default=Path('research/order_state_01'))
    a = p.parse_args(argv)
    if a.symbol == 'aggregate':
        data = aggregate_reports(a.data_dir,a.output_dir)
        print(json.dumps({'status':data['status'],'totals':data['test_totals_observational']},ensure_ascii=False))
        return 0
    if a.baseline_dir is None:
        p.error('--baseline-dir is required for individual symbol tracing')
    report = run_symbol(a.data_dir,a.baseline_dir/f'{a.symbol}_fixed_calendar_baseline.json',a.output_dir)
    x=report['splits']['test']
    print(json.dumps({'symbol':a.symbol,'test':x,'decision_timeframes':report['decision_timeframes_only']},ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())