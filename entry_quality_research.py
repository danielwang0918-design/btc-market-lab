#!/usr/bin/env python3
"""ENTRY-QUALITY-01: paired, diagnostic-only OB retest / rejection study.

WARNING: the cohort contains ONLY trades that the baseline engine actually filled.
An alternative strategy can generate a different signal universe. Therefore the
counterfactual calculations here are NOT a valid full-strategy backtest.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = 'entry-quality-ob-v1'
STEP = 3600000
FEE_BPS = 10.0
SLIP_BPS = 5.0
MIN_NET_RR = 1.5


def _ms(value: str) -> int:
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Naive datetime is prohibited')
    return int(d.timestamp() * 1000)


def _utc(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _number(raw: Any) -> float:
    n = float(raw)
    if not math.isfinite(n):
        raise ValueError('NaN or infinity')
    return n


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda: f.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def _metric(rows: list[dict[str, Any]], key: str = 'r_multiple') -> dict[str, Any]:
    values = [_number(r[key]) for r in rows if r.get(key) is not None]
    if not values:
        return {'trades': 0, 'wins': 0, 'win_rate_percent': None,
                'expectancy_r': None, 'profit_factor': None, 'max_drawdown_r': None}
    positive = sum(x for x in values if x > 0)
    negative = -sum(x for x in values if x < 0)
    wins = sum(x > 0 for x in values)
    equity = 0.0
    peak = 0.0
    dd = 0.0
    # Trade order is chronological in the caller; this metric is on closed trades.
    for value in values:
        equity += value
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return {'trades': len(values), 'wins': wins,
            'win_rate_percent': round(100 * wins / len(values), 4),
            'expectancy_r': round(sum(values) / len(values), 6),
            'profit_factor': round(positive / negative, 6) if negative else None,
            'max_drawdown_r': round(dd, 6)}


def _atr14(hourly: list[dict[str, str]]) -> list[float | None]:
    """Wilder ATR14 on contiguous segments, no cross-gap historical values."""
    out: list[float | None] = [None] * len(hourly)
    step = 0
    trs: list[float] = []
    prev_close: float | None = None
    value: float | None = None
    for i, row in enumerate(hourly):
        current = int(row['open_time'])
        if i == 0 or current - int(hourly[i - 1]['open_time']) != STEP:
            step = 0
            trs = []
            prev_close = None
            value = None
        high, low, close = (_number(row[k]) for k in ('high', 'low', 'close'))
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close)) if prev_close is not None else high - low
        if tr < 0:
            raise ValueError('Invalid TR')
        trs.append(tr)
        step += 1
        if step == 14:
            value = sum(trs) / 14
            out[i] = value
        elif step > 14:
            assert value is not None
            value = (13 * value + tr) / 14
            out[i] = value
        prev_close = close
    return out


def _info_setup(t: dict[str, Any], by_open: dict[int, dict[str, str]],
                hour_bars: list[dict[str, str]], times: list[int], atr: list[float | None]) -> dict[str, Any]:
    """Only evidence visible at signal close; never use post-signal candle here."""
    parts = str(t['setup_id']).split(':')
    if len(parts) < 7 or parts[2] != 'order_block':
        raise ValueError('Unrecognized OB event identity')
    formed_ms = int(parts[-3])
    confirmed_close_ms = int(parts[-2])
    signal = _ms(t['signal_time_utc'])
    if signal < confirmed_close_ms + 1:
        raise ValueError('Unconfirmed OB used as signal')
    ob = by_open.get(formed_ms)
    if ob is None:
        raise ValueError('Missing OB formation candle')
    high, low, op, close = (_number(ob[x]) for x in ('high', 'low', 'open', 'close'))
    if not (0 < low <= min(op, close) <= max(op, close) <= high):
        raise ValueError('Invalid OB candle')
    body_ratio = abs(close - op) / (high - low) if high > low else 0.0
    # Signal is a bar close+1, e.g. 10:00 = last usable candle opened at 09:00.
    signal_i = bisect.bisect_left(times, signal) - 1
    if signal_i < 0 or times[signal_i] + STEP != signal:
        raise ValueError('Signal not aligned with an actually closed 1h candle')
    atr_value = atr[signal_i]
    stop_dist = abs(_number(t['raw_entry_price']) - _number(t['stop_price']))
    # Count DISTINCT consecutive overlapping OB-zone candle runs after confirmation.
    # All these bars MUST have closed before signal decision. No post-signal outcome.
    first = bisect.bisect_left(times, confirmed_close_ms + 1)
    prior_touches = 0
    in_touch = False
    prev = None
    for j in range(first, signal_i + 1):
        if prev is not None and times[j] - prev != STEP:
            in_touch = False
        previous = hour_bars[j]
        hit = _number(previous['low']) <= high and _number(previous['high']) >= low
        if hit and not in_touch:
            prior_touches += 1
        in_touch = hit
        prev = times[j]
    age_h = (signal - (confirmed_close_ms + 1)) / STEP
    return {'ob_confirmed_age_at_signal_hours': round(age_h, 4),
            'ob_age_bucket': '<=24h' if age_h <= 24 else '24-72h' if age_h <= 72 else '>72h',
            'ob_body_fraction': round(body_ratio, 6),
            'ob_body_bucket': '<0.5' if body_ratio < 0.5 else '>=0.5',
            'prior_touch_episodes_asof': prior_touches,
            'prior_touch_bucket': '0' if prior_touches == 0 else '1' if prior_touches == 1 else '2+',
            'stop_distance_over_1h_atr14_asof': round(stop_dist / atr_value, 6) if atr_value and atr_value > 0 else None,
            'stop_atr_bucket': 'unknown' if not atr_value or atr_value <= 0 else '<=1' if stop_dist / atr_value <= 1 else '1-2' if stop_dist / atr_value <= 2 else '>2',
            'atr_signal_candle_open_time': times[signal_i],
            'signal_was_closed': True}


def _costed_r(side: str, raw_entry: float, raw_exit: float, stop: float,
              *, fee_bps: float = FEE_BPS, slip_bps: float = SLIP_BPS) -> float:
    slip = slip_bps / 10000
    fee = fee_bps / 10000
    ef = raw_entry * (1 + slip if side == 'long' else 1 - slip)
    xf = raw_exit * (1 - slip if side == 'long' else 1 + slip)
    sf = stop * (1 - slip if side == 'long' else 1 + slip)
    gross = xf - ef if side == 'long' else ef - xf
    net = gross - (ef + xf) * fee
    risk = abs(ef - sf) + (ef + sf) * fee
    if risk <= 0:
        raise ValueError('Non-positive risk')
    return net / risk


def _net_rr(side: str, entry: float, stop: float, target: float) -> float:
    stop_r = _costed_r(side, entry, stop, stop)
    if stop_r > -0.999999:
        raise ValueError('Unexpected stop risk')
    return _costed_r(side, entry, target, stop)


def _evaluate_confirmation(t: dict[str, Any], hourly: list[dict[str, str]],
                           times: list[int], split_limit_ms: int, mode: str) -> dict[str, Any]:
    """Matched baseline-fills counterfactual. Next-hour open after touch bar closes.

    Incomplete candidate universe: a rejected baseline fill can free up future
    setups not represented by the baseline-filled trade cohort.
    """
    entry = _ms(t['entry_time_utc'])
    at = bisect.bisect_left(times, entry)
    if at >= len(times) or times[at] != entry or at + 1 >= len(times) or times[at + 1] != entry + STEP:
        return {'outcome': 'no_next_contiguous_bar'}
    touch = hourly[at]
    following = hourly[at + 1]
    side = t['direction']
    raw = _number(t['raw_entry_price'])
    stop = _number(t['stop_price'])
    target = _number(t['target_price'])
    op, cl, hi, lo = (_number(touch[k]) for k in ('open', 'close', 'high', 'low'))
    if not lo <= raw <= hi:
        return {'outcome': 'reference_not_in_touch_bar'}
    if side == 'long':
        favorable = cl > op and cl > raw
        stronger = favorable and at > 0 and times[at] - times[at-1] == STEP and cl > _number(hourly[at-1]['high'])
        stop_touched = lo <= stop
    else:
        favorable = cl < op and cl < raw
        stronger = favorable and at > 0 and times[at] - times[at-1] == STEP and cl < _number(hourly[at-1]['low'])
        stop_touched = hi >= stop
    approved = favorable if mode == 'rejection_close' else stronger
    if not approved:
        return {'outcome': 'rejected_no_confirmation'}
    # If OB stop was breached before confirmation, its original structural level is invalid.
    if stop_touched:
        return {'outcome': 'rejected_stop_breached_on_touch_candle'}
    if times[at+1] >= split_limit_ms:
        return {'outcome': 'rejected_cross_split_at_entry'}
    new_entry = _number(following['open'])
    if side == 'long' and not stop < new_entry < target:
        return {'outcome': 'rejected_open_outside_stop_target'}
    if side == 'short' and not target < new_entry < stop:
        return {'outcome': 'rejected_open_outside_stop_target'}
    rr = _net_rr(side, new_entry, stop, target)
    if rr < MIN_NET_RR:
        return {'outcome': 'rejected_net_rr_under_1_5', 'net_rr': round(rr, 6)}
    for j in range(at + 1, len(hourly)):
        if times[j] >= split_limit_ms:
            if j == at + 1:
                return {'outcome': 'rejected_cross_split_at_entry'}
            previous_close = _number(hourly[j-1]['close'])
            return {'outcome': 'simulated_split_end_exit',
                    'r': round(_costed_r(side, new_entry, previous_close, stop), 8),
                    'exit_time_ms': times[j-1]+STEP, 'bars_held': j-(at+1),
                    'net_rr': round(rr, 6)}
        if j > at + 1 and times[j] - times[j-1] != STEP:
            # Force exit at prior bar close, as the formal simulator does.
            prev_close = _number(hourly[j-1]['close'])
            result = _costed_r(side, new_entry, prev_close, stop)
            return {'outcome': 'simulated_data_gap_exit', 'r': round(result, 8),
                    'exit_time_ms': times[j-1]+STEP, 'bars_held': j-(at+1),
                    'net_rr': round(rr, 6)}
        bar = hourly[j]
        bo, bh, bl = (_number(bar[k]) for k in ('open', 'high', 'low'))
        if side == 'long':
            stop_gap = bo <= stop
            stopped = bl <= stop
            target_hit = bh >= target
        else:
            stop_gap = bo >= stop
            stopped = bh >= stop
            target_hit = bl <= target
        if stop_gap or stopped or target_hit:
            raw_exit = bo if stop_gap else stop if stopped else target
            reason = 'stop_gap' if stop_gap else 'stop' if stopped else 'target'
            result = _costed_r(side, new_entry, raw_exit, stop)
            return {'outcome': 'simulated_'+reason, 'r': round(result, 8),
                    'exit_time_ms': times[j]+STEP, 'bars_held': j-at,
                    'net_rr': round(rr, 6)}
    if not hourly or times[-1] < times[at+1]:
        return {'outcome': 'rejected_unclosed_at_dataset_end'}
    final_close = _number(hourly[-1]['close'])
    return {'outcome': 'simulated_dataset_end_exit',
            'r': round(_costed_r(side, new_entry, final_close, stop), 8),
            'exit_time_ms': times[-1]+STEP, 'bars_held': len(hourly)-(at+1),
            'net_rr': round(rr, 6)}


def build_study(data_dir: Path, output_dir: Path) -> dict[str, Any]:
    source_path = data_dir / 'BTCUSDT_trade_diagnostics.json'
    hour_path = data_dir / 'BTCUSDT_1h.csv'
    stop_path = data_dir / 'BTCUSDT_stop_loss_research.json'
    report = json.loads(source_path.read_text(encoding='utf-8'))
    stop_report = json.loads(stop_path.read_text(encoding='utf-8'))
    if report.get('strategy') != 'mtf-bos-ob-retest-v1' or stop_report.get('strategy') != report.get('strategy'):
        raise ValueError('Unknown or mismatched formal strategy')
    p = stop_report['parameters']
    if (p.get('fee_bps_per_side') != FEE_BPS or p.get('slippage_bps_per_side') != SLIP_BPS or
        p.get('minimum_net_rr_after_costs') != MIN_NET_RR):
        raise ValueError('Cost or RR assumptions mismatch; abort study')
    hourly = _read(hour_path)
    times = [int(r['open_time']) for r in hourly]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError('Unsorted or duplicate hourly bars')
    by_open = dict(zip(times, hourly))
    atr = _atr14(hourly)
    baseline = report['variants']['ob_1h_baseline']['trades']
    rows = []
    modes = ('rejection_close', 'rejection_above_prior_extreme')
    for t in baseline:
        split = t['split']
        period = stop_report['variants']['ob_1h_baseline']['splits'][split]
        end = _ms(period['end_exclusive_utc'])
        features = _info_setup(t, by_open, hourly, times, atr)
        row = {'setup_id': t['setup_id'], 'split':split, 'direction':t['direction'],
               'signal_time_utc':t['signal_time_utc'], 'original_entry_time_utc':t['entry_time_utc'],
               'original_exit_time_utc':t['exit_time_utc'], 'baseline_r':_number(t['r_multiple']),
               'baseline_bars_held':int(t['bars_held']), 'baseline_exit_reason':t['exit_reason'], **features}
        for mode in modes:
            result = _evaluate_confirmation(t, hourly, times, end, mode)
            row[mode + '_outcome'] = result['outcome']
            row[mode + '_r'] = result.get('r')
            row[mode + '_exit_time_utc'] = _utc(result['exit_time_ms']) if 'exit_time_ms' in result else ''
            row[mode + '_held_bars'] = result.get('bars_held')
            row[mode + '_rr'] = result.get('net_rr')
        rows.append(row)
    output_dir.mkdir(parents=True, exist_ok=True)
    csvpath = output_dir/'BTCUSDT_entry_quality_features.csv'
    with csvpath.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    results = {}
    for split in ('train', 'validation', 'test'):
        cohort = [r for r in rows if r['split']==split]
        # Sorted originally by baseline exit; the counterfactual may exit in a different order.
        matched = {}
        for mode in modes:
            accepted = [r for r in cohort if r[mode+'_r'] is not None]
            ordered = sorted(accepted, key=lambda r:(r[mode+'_exit_time_utc'],r['setup_id']))
            overlaps = 0
            last_exit = 0
            # Report overlaps without hiding them. No portfolio inference or reallocation.
            for r in sorted(accepted,key=lambda r:(r['original_entry_time_utc'],r['setup_id'])):
                start = _ms(r['original_entry_time_utc']) + STEP
                if start < last_exit: overlaps+=1
                last_exit = max(last_exit, _ms(r[mode+'_exit_time_utc']))
            matched[mode] = {'outcomes': dict(sorted((k,sum(r[mode+'_outcome']==k for r in cohort)) for k in set(r[mode+'_outcome'] for r in cohort))),
                             'accepted_baseline_cohort': _metric(accepted, 'baseline_r'),
                             'counterfactual_matched': _metric(ordered, mode+'_r'),
                             'overlapping_counterfactuals': overlaps,
                             'not_a_full_strategy_backtest': True}
        group_summary = {}
        for feature, categories in [('ob_age_bucket', ('<=24h','24-72h','>72h')),
                                    ('prior_touch_bucket', ('0','1','2+')),
                                    ('ob_body_bucket', ('<0.5','>=0.5')),
                                    ('stop_atr_bucket', ('<=1','1-2','>2','unknown'))]:
            group_summary[feature] = {c:_metric([r for r in cohort if r[feature]==c], 'baseline_r') for c in categories}
        results[split] = {'baseline':_metric(cohort,'baseline_r'),
                          'asof_ob_features_baseline_only':group_summary,
                          'paired_counterfactuals':matched}
    final = {'version':VERSION, 'formal_strategy_unmodified':True,
             'source_provenance_sha256':{p.name:_sha(p) for p in (source_path,stop_path,hour_path)},
             'fixed_assumptions':{'fee_bps_per_side':FEE_BPS,'slippage_bps_per_side':SLIP_BPS,
                                  'minimum_net_rr':MIN_NET_RR,'timeframe':'1h (4H baseline unchanged)',
                                  'modes':{'rejection_close':'After original touch 1h closes: favorable body and close beyond original entry reference, with no stop breach. Enter next 1h OPEN only if net-RR >=1.5.',
                                           'rejection_above_prior_extreme':'Same plus favorable close beyond preceding fully-closed 1h high/low.'}},
             'splits':results,
             'critical_limitations':[
                 'The cohort contains ONLY previously executed baseline trades; alternative confirmation may expose new setups absent from this cohort. NOT a full-strategy backtest.',
                 'Counterfactuals can overlap and are not a position-size-aware chronological portfolio simulation.',
                 'OB age, OB body, prior zone touches and ATR14 use only bars closed by original signal; entry-touch-bar confirmation is not used until NEXT 1h open.',
                 'Candidate touch/stop within same 1h bar may have unknown sequence; rejection requires no stop breach by candle close. Alternative stops use conservative stop-first treatment.',
                 'Train/validation/test groups represent retrospective slices. These same Test outcomes must not be optimized against or presented as an untouched test of any newly selected settings.',
                 'No ATR/body/age subgroup is yet a validated trading filter; no profitability or 80% win-rate claim.',
                 'For a valid strategy evaluation, rerun the ORIGINAL full signal generator with alternative order states and keep a NEW untouched forward period.'
             ]}
    (output_dir/'BTCUSDT_entry_quality_research.json').write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding='utf-8')
    return final


def main(argv: list[str] | None = None) -> int:
    p=argparse.ArgumentParser(description='Research-only OB entry confirmation matched cohort; no strategy changes')
    p.add_argument('--data-dir',type=Path,default=Path('data/historical'))
    p.add_argument('--output-dir',type=Path,default=Path('data/historical'))
    a=p.parse_args(argv)
    r=build_study(a.data_dir,a.output_dir)
    print(json.dumps({'version':r['version'],'splits':r['splits'],'warnings':r['critical_limitations']},ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())