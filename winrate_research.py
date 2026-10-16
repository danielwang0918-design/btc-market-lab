#!/usr/bin/env python3
"""Research win-rate improvements on the formal MTF strategy without test-set selection."""
from __future__ import annotations

import argparse
import bisect
import copy
import json
import math
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import historical_backtest as hb
from market_structure import INTERVAL_MILLISECONDS

TARGETS: tuple[str, ...] = ("swing", "0.25R", "0.5R", "0.75R", "1.0R", "1.25R", "1.5R")
FILTERS: tuple[tuple[str, str], ...] = (
    ("none", "No additional entry filter; formal strategy baseline."),
    ("ema4h_position", "Trade direction agrees with the last closed 4H close vs EMA20."),
    ("ema1d_position", "Trade direction agrees with the last closed daily close vs EMA20."),
    ("ema4h1d_position", "Both last closed 4H and daily closes agree with EMA20 direction."),
    ("ema_stack_position", "Last closed 1H, 4H, and daily closes all agree with EMA20 direction."),
    ("ema4h1d_trend", "4H and daily EMA20 position and four-bar EMA slope agree with trade direction."),
    ("rsi50", "1H RSI(14) is at least 50 for long or at most 50 for short."),
    ("rsi55", "1H RSI(14) is at least 55 for long or at most 45 for short."),
    ("rsi60", "1H RSI(14) is at least 60 for long or at most 40 for short."),
    ("ema4h1d_rsi55", "4H and daily EMA20 position plus directional 1H RSI 55/45."),
    ("ema_stack_rsi55", "1H/4H/daily EMA20 position plus directional 1H RSI 55/45."),
    ("relative_volume12", "Closed signal-bar volume is at least 1.2 times the preceding 20-bar mean."),
    ("ema4h1d_volume12", "4H/daily EMA20 position plus relative volume of at least 1.2."),
    ("stop_atr05", "OB-edge stop distance is no more than 0.5 of signal-time 1H ATR(14)."),
    ("original_rr2", "Original formal swing-target net RR is at least 2.0."),
    ("original_rr25", "Original formal swing-target net RR is at least 2.5."),
    ("age24", "The OB setup is no older than 24 hours at signal time."),
    ("age48", "The OB setup is no older than 48 hours at signal time."),
    ("age72", "The OB setup is no older than 72 hours at signal time."),
    ("ema4h1d_rsi55_age72", "4H/daily EMA20 position, directional RSI 55/45, and setup age at most 72 hours."),
)
TARGET_WIN_RATE = 70.0
MIN_VALIDATION_TRADES_FOR_PROFITABILITY = 12
ENTRY_MODES: tuple[tuple[str, str], ...] = (
    ("touch", "Existing immediate entry on first eligible OB-zone touch."),
    ("directional_close_next_open", "After the OB-zone touch, require a directional 1H candle close, then enter at the next 1H open."),
    ("edge_reclaim_next_open", "After the OB-zone touch, require a directional 1H close beyond the far OB edge, then enter at the next 1H open."),
)


def _ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        current = alpha * values[index] + (1.0 - alpha) * current
        result[index] = current
    return result


def _rsi(values: list[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, period + 1)]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        result[index] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return result


def _atr(rows: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(rows)
    true_ranges: list[float] = []
    previous_close: float | None = None
    for row in rows:
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        true_ranges.append(high - low if previous_close is None else
                           max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = close
    if len(rows) < period:
        return result
    current = sum(true_ranges[:period]) / period
    result[period - 1] = current
    for index in range(period, len(rows)):
        current = (current * (period - 1) + true_ranges[index]) / period
        result[index] = current
    return result


def _interval_indicators(rows: list[dict[str, Any]], interval: str) -> dict[str, Any]:
    count = len(rows)
    close = [float(row["close"]) for row in rows]
    volume = [float(row["volume"]) for row in rows]
    ema20: list[float | None] = [None] * count
    rsi14: list[float | None] = [None] * count
    atr14: list[float | None] = [None] * count
    segment_start = [0] * count
    step = INTERVAL_MILLISECONDS[interval]
    boundaries = [0]
    for index in range(1, count):
        if int(rows[index]["open_time"]) - int(rows[index - 1]["open_time"]) != step:
            boundaries.append(index)
    boundaries.append(count)
    for start, end in zip(boundaries, boundaries[1:]):
        for index in range(start, end):
            segment_start[index] = start
        local_rows = rows[start:end]
        local_close = close[start:end]
        local_ema = _ema(local_close, 20)
        local_rsi = _rsi(local_close, 14)
        local_atr = _atr(local_rows, 14)
        ema20[start:end] = local_ema
        rsi14[start:end] = local_rsi
        atr14[start:end] = local_atr
    return {
        "close": close,
        "ema20": ema20,
        "rsi14": rsi14,
        "atr14": atr14,
        "volume": volume,
        "segment_start": segment_start,
        "close_after_ms": [int(row["close_time"]) + 1 for row in rows],
    }


def _last_index(close_after_ms: list[int], asof_ms: int) -> int:
    return bisect.bisect_right(close_after_ms, asof_ms) - 1


def build_feature_rows(
    rows_by_interval: dict[str, list[dict[str, Any]]],
    hourly_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Features are computed only from candles fully closed at each hourly signal time."""
    indicators = {key: _interval_indicators(value, key) for key, value in rows_by_interval.items()}
    output: list[dict[str, Any]] = []
    for hourly in hourly_rows:
        asof_ms = int(hourly["close_time"]) + 1
        feature: dict[str, Any] = {"asof_ms": asof_ms}
        indices: dict[str, int] = {}
        for interval in ("1h", "4h", "1d"):
            item = indicators[interval]
            index = _last_index(item["close_after_ms"], asof_ms)
            indices[interval] = index
            if index < 0:
                feature[f"{interval}_ema_position"] = None
                feature[f"{interval}_ema_slope"] = None
                continue
            close = item["close"][index]
            ema20 = item["ema20"][index]
            feature[f"{interval}_ema_position"] = (
                None if ema20 is None else (close / ema20 - 1.0) * 100.0
            )
            slope_index = index - 3
            slope_old = item["ema20"][slope_index] if slope_index >= 0 else None
            feature[f"{interval}_ema_slope"] = (
                None if ema20 is None or slope_old is None or slope_old == 0
                else (ema20 / slope_old - 1.0) * 100.0
            )
        i = indices["1h"]
        one = indicators["1h"]
        feature["rsi14"] = one["rsi14"][i] if i >= 0 else None
        feature["atr14"] = one["atr14"][i] if i >= 0 else None
        # The current signal candle is closed; its volume is compared with the prior 20 bars.
        if i >= 0 and i - one["segment_start"][i] >= 20:
            prior = one["volume"][i - 20:i]
            mean_prior = sum(prior) / len(prior)
            feature["relative_volume20"] = one["volume"][i] / mean_prior if mean_prior > 0 else None
        else:
            feature["relative_volume20"] = None
        output.append(feature)
    return output


def _aligned(feature: dict[str, Any], interval: str, side: str, *, slope: bool = False) -> bool:
    key = f"{interval}_ema_slope" if slope else f"{interval}_ema_position"
    value = feature.get(key)
    if value is None:
        return False
    return value > 0 if side == "long" else value < 0


def entry_filter_passes(
    name: str,
    candidate: dict[str, Any],
    feature: dict[str, Any],
) -> bool:
    side = str(candidate.get("side", ""))
    if side not in {"long", "short"}:
        return False
    p1 = _aligned(feature, "1h", side)
    p4 = _aligned(feature, "4h", side)
    pd = _aligned(feature, "1d", side)
    s4 = _aligned(feature, "4h", side, slope=True)
    sd = _aligned(feature, "1d", side, slope=True)
    rsi = feature.get("rsi14")
    rv = feature.get("relative_volume20")
    atr = feature.get("atr14")
    original_rr = float(candidate.get("_original_net_rr", candidate.get("net_rr", 0)))
    age = float(candidate.get("setup_age_hours", math.inf))
    stop_distance = abs(float(candidate.get("entry_reference", 0)) - float(candidate.get("stop", 0)))
    stop_atr = stop_distance / atr if atr is not None and atr > 0 else math.inf

    if name == "none":
        return True
    if name == "ema4h_position":
        return p4
    if name == "ema1d_position":
        return pd
    if name == "ema4h1d_position":
        return p4 and pd
    if name == "ema_stack_position":
        return p1 and p4 and pd
    if name == "ema4h1d_trend":
        return p4 and pd and s4 and sd
    if name == "rsi50":
        return rsi is not None and (rsi >= 50 if side == "long" else rsi <= 50)
    if name == "rsi55":
        return rsi is not None and (rsi >= 55 if side == "long" else rsi <= 45)
    if name == "rsi60":
        return rsi is not None and (rsi >= 60 if side == "long" else rsi <= 40)
    if name == "ema4h1d_rsi55":
        return p4 and pd and rsi is not None and (rsi >= 55 if side == "long" else rsi <= 45)
    if name == "ema_stack_rsi55":
        return p1 and p4 and pd and rsi is not None and (rsi >= 55 if side == "long" else rsi <= 45)
    if name == "relative_volume12":
        return rv is not None and rv >= 1.2
    if name == "ema4h1d_volume12":
        return p4 and pd and rv is not None and rv >= 1.2
    if name == "stop_atr05":
        return stop_atr <= 0.5
    if name == "original_rr2":
        return original_rr >= 2.0
    if name == "original_rr25":
        return original_rr >= 2.5
    if name == "age24":
        return age <= 24
    if name == "age48":
        return age <= 48
    if name == "age72":
        return age <= 72
    if name == "ema4h1d_rsi55_age72":
        return p4 and pd and age <= 72 and rsi is not None and (
            rsi >= 55 if side == "long" else rsi <= 45
        )
    raise ValueError(f"Unknown entry filter: {name}")


def _target_net_rr(candidate: dict[str, Any], target: Decimal, fee_bps: Decimal,
                   slippage_bps: Decimal) -> Decimal:
    side = candidate["side"]
    entry = hb._number(candidate["entry_reference"], "entry_reference", positive=True)
    stop = hb._number(candidate["stop"], "stop", positive=True)
    slip = slippage_bps / Decimal(10000)
    fee = fee_bps / Decimal(10000)
    if side == "long":
        entry_fill = entry * (Decimal(1) + slip)
        stop_fill = stop * (Decimal(1) - slip)
        target_fill = target * (Decimal(1) - slip)
        gross_reward = target_fill - entry_fill
    else:
        entry_fill = entry * (Decimal(1) - slip)
        stop_fill = stop * (Decimal(1) + slip)
        target_fill = target * (Decimal(1) + slip)
        gross_reward = entry_fill - target_fill
    entry_fee = entry_fill * fee
    stop_fee = stop_fill * fee
    target_fee = target_fill * fee
    risk = abs(entry_fill - stop_fill) + entry_fee + stop_fee
    net_reward = gross_reward - entry_fee - target_fee
    return net_reward / risk if risk > 0 else Decimal("-1")


def make_target_snapshots(
    snapshots: list[dict[str, Any]],
    target_mode: str,
    *,
    fee_bps: Decimal,
    slippage_bps: Decimal,
) -> list[dict[str, Any]]:
    transformed: list[dict[str, Any]] = []
    for item in snapshots:
        result = dict(item)
        candidate = item.get("candidate")
        if candidate:
            candidate = dict(candidate)
            candidate["_original_net_rr"] = str(candidate["net_rr"])
            candidate["_original_target"] = str(candidate["target"])
            if target_mode != "swing":
                rr = Decimal(target_mode.removesuffix("R"))
                entry = hb._number(candidate["entry_reference"], "entry_reference", positive=True)
                stop = hb._number(candidate["stop"], "stop", positive=True)
                distance = abs(entry - stop)
                target = entry + rr * distance if candidate["side"] == "long" else entry - rr * distance
                if target <= 0:
                    candidate = None
                else:
                    candidate["target"] = format(target, "f")
                    candidate["net_rr"] = str(_target_net_rr(candidate, target, fee_bps, slippage_bps))
            result["candidate"] = candidate
        transformed.append(result)
    return transformed


def _entry_confirmation_passes(
    entry_mode: str,
    side: str,
    bar: dict[str, Any],
    zone_low: Decimal,
    zone_high: Decimal,
) -> bool:
    if entry_mode == "touch":
        return True
    op = hb._number(bar["open"], "open", positive=True)
    close = hb._number(bar["close"], "close", positive=True)
    directional = close > op if side == "long" else close < op
    if not directional:
        return False
    if entry_mode == "directional_close_next_open":
        return True
    if entry_mode == "edge_reclaim_next_open":
        return close >= zone_high if side == "long" else close <= zone_low
    raise ValueError(f"Unknown entry mode: {entry_mode}")


def simulate_filtered_slice(
    rows: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    features: list[dict[str, Any]],
    start: int,
    end: int,
    *,
    filter_name: str,
    minimum_net_rr: float,
    max_setup_age_hours: int,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    entry_mode: str = "touch",
    target_mode: str = "swing",
) -> list[dict[str, Any]]:
    """Same conservative order/exit rules, optionally waiting for a closed-candle confirmation."""
    trades: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    confirmed_for_next_open: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    consumed_setups: set[str] = set()
    for index in range(start, end):
        bar = rows[index]
        prior = rows[index - 1] if index > start else None
        if prior is not None and int(bar["open_time"]) - int(prior["open_time"]) != INTERVAL_MILLISECONDS["1h"]:
            if position:
                close_time = int(prior["close_time"]) + 1
                trades.append(hb._make_trade(
                    position, hb._number(prior["close"], "close", positive=True),
                    close_time, "data_gap_exit", index - 1, fee_bps, slippage_bps,
                ))
                position = None
            pending = None
            confirmed_for_next_open = None
        current_snapshot = snapshots[index]

        if confirmed_for_next_open is not None:
            candidate = confirmed_for_next_open
            confirmed_for_next_open = None
            if current_snapshot.get("integrity_ok"):
                op = hb._number(bar["open"], "open", positive=True)
                stop = hb._number(candidate["stop"], "stop", positive=True)
                side = str(candidate["side"])
                invalid_at_open = (side == "long" and op <= stop) or (side == "short" and op >= stop)
                if not invalid_at_open:
                    swing_target = hb._number(
                        candidate.get("_original_target", candidate["target"]),
                        "swing target", positive=True,
                    )
                    if target_mode == "swing":
                        target = swing_target
                    else:
                        target_r = Decimal(target_mode.removesuffix("R"))
                        risk_distance = abs(op - stop)
                        proposed = (
                            op + target_r * risk_distance if side == "long"
                            else op - target_r * risk_distance
                        )
                        target = min(proposed, swing_target) if side == "long" else max(proposed, swing_target)
                    favorable_target = target > op if side == "long" else target < op
                    if favorable_target:
                        rr_candidate = dict(candidate)
                        rr_candidate["entry_reference"] = format(op, "f")
                        actual_rr = _target_net_rr(rr_candidate, target, fee_bps, slippage_bps)
                        if actual_rr >= Decimal(str(minimum_net_rr)):
                            position = {
                                "setup_id": candidate["setup_id"],
                                "target_event_id": candidate["target_event_id"],
                                "side": side,
                                "signal_time_ms": int(candidate["signal_time_ms"]),
                                "entry_time_ms": int(bar["open_time"]),
                                "entry_index": index,
                                "raw_entry": op,
                                "stop": stop,
                                "target": target,
                            }

        if position:
            exit_result = hb._exit_on_bar(position, bar)
            if exit_result:
                raw_exit, reason = exit_result
                trades.append(hb._make_trade(
                    position, raw_exit, int(bar["close_time"]) + 1,
                    reason, index, fee_bps, slippage_bps,
                ))
                position = None
            elif not current_snapshot.get("integrity_ok"):
                trades.append(hb._make_trade(
                    position, hb._number(bar["close"], "close", positive=True),
                    int(bar["close_time"]) + 1, "integrity_review_exit", index, fee_bps, slippage_bps,
                ))
                position = None
        elif pending:
            age_hours = (int(bar["open_time"]) - int(pending["setup_confirm_ms"])) / 3_600_000
            candidate_now = current_snapshot.get("candidate")
            if age_hours > max_setup_age_hours or not current_snapshot.get("integrity_ok"):
                pending = None
            else:
                op = hb._number(bar["open"], "open", positive=True)
                high = hb._number(bar["high"], "high", positive=True)
                low = hb._number(bar["low"], "low", positive=True)
                zone_low = hb._number(pending["zone_low"], "zone_low", positive=True)
                zone_high = hb._number(pending["zone_high"], "zone_high", positive=True)
                stop = hb._number(pending["stop"], "stop", positive=True)
                target = hb._number(pending["target"], "target", positive=True)
                side = pending["side"]
                if (side == "long" and op <= stop) or (side == "short" and op >= stop):
                    pending = None
                else:
                    zone_touch = low <= zone_high and high >= zone_low
                    target_before_entry = high >= target if side == "long" else low <= target
                    if target_before_entry:
                        pending = None
                    elif zone_touch:
                        if entry_mode == "touch":
                            position = {
                                "setup_id": pending["setup_id"],
                                "target_event_id": pending["target_event_id"],
                                "side": side,
                                "signal_time_ms": int(pending["signal_time_ms"]),
                                "entry_time_ms": int(bar["open_time"]),
                                "entry_index": index,
                                "raw_entry": hb._number(pending["entry_reference"], "entry_reference", positive=True),
                                "stop": stop,
                                "target": target,
                            }
                            consumed_setups.add(str(pending["setup_id"]))
                            pending = None
                            exit_result = hb._exit_on_bar(position, bar)
                            if exit_result:
                                raw_exit, reason = exit_result
                                trades.append(hb._make_trade(
                                    position, raw_exit, int(bar["close_time"]) + 1,
                                    reason, index, fee_bps, slippage_bps,
                                ))
                                position = None
                        else:
                            setup_id = str(pending["setup_id"])
                            consumed_setups.add(setup_id)
                            if _entry_confirmation_passes(entry_mode, side, bar, zone_low, zone_high):
                                confirmed_for_next_open = dict(pending)
                            pending = None
                    elif (not hb._candidate_allowed(candidate_now, minimum_net_rr, max_setup_age_hours)
                          or candidate_now.get("setup_id") != pending.get("setup_id")):
                        pending = None
        candidate = current_snapshot.get("candidate")
        if (position is None and pending is None and confirmed_for_next_open is None
                and current_snapshot.get("integrity_ok")):
            if hb._candidate_allowed(candidate, minimum_net_rr, max_setup_age_hours):
                if candidate and entry_filter_passes(filter_name, candidate, features[index]):
                    setup_id = str(candidate["setup_id"])
                    if setup_id not in consumed_setups:
                        pending = dict(candidate)
                        pending["signal_index"] = index
    if position:
        last = rows[end - 1]
        trades.append(hb._make_trade(
            position, hb._number(last["close"], "close", positive=True),
            int(last["close_time"]) + 1, "split_or_segment_end", end - 1, fee_bps, slippage_bps,
        ))
    return trades


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    return hb._metrics(trades)


def _run_variant(
    rows: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    features: list[dict[str, Any]],
    *,
    target_mode: str,
    filter_name: str,
    minimum_net_rr: float,
    max_setup_age_hours: int,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    target_snaps = make_target_snapshots(snapshots, target_mode, fee_bps=fee_bps, slippage_bps=slippage_bps)
    bounds = hb._split_ranges(rows, train_fraction, validation_fraction)
    split_results: dict[str, Any] = {}
    trades_by_split: dict[str, list[dict[str, Any]]] = {}
    for split_name, (start, end) in bounds.items():
        trades = simulate_filtered_slice(
            rows, target_snaps, features, start, end,
            filter_name=filter_name, minimum_net_rr=minimum_net_rr,
            max_setup_age_hours=max_setup_age_hours,
            fee_bps=fee_bps, slippage_bps=slippage_bps,
        )
        trades_by_split[split_name] = trades
        split_results[split_name] = _metrics(trades)
    return split_results, trades_by_split


def _sort_profit_factor(value: Any) -> float:
    if value == "infinite":
        return math.inf
    if value is None:
        return -1.0
    return float(value)


def _select_candidate(candidates: list[dict[str, Any]], minimum_validation_trades: int) -> tuple[dict[str, Any], str]:
    eligible = [
        row for row in candidates
        if row["validation"]["trade_count"] >= minimum_validation_trades
    ]
    if not eligible:
        baseline = next(row for row in candidates if row["target_mode"] == "swing" and row["filter"] == "none")
        return baseline, "fallback_baseline_insufficient_validation_trades"

    profitable = [
        row for row in eligible
        if row["validation"]["trade_count"] >= max(
            minimum_validation_trades, MIN_VALIDATION_TRADES_FOR_PROFITABILITY
        )
        and row["validation"]["expectancy_r"] > 0
        and _sort_profit_factor(row["validation"]["profit_factor"]) > 1
    ]
    if profitable:
        selected = max(
            profitable,
            key=lambda row: (
                row["validation"]["win_rate_percent"],
                row["validation"]["expectancy_r"],
                _sort_profit_factor(row["validation"]["profit_factor"]),
                row["validation"]["trade_count"],
            ),
        )
        return selected, "selected_by_validation_win_rate_with_positive_expectancy_and_pf"
    selected = max(
        eligible,
        key=lambda row: (
            row["validation"]["win_rate_percent"],
            row["validation"]["expectancy_r"],
            _sort_profit_factor(row["validation"]["profit_factor"]),
            row["validation"]["trade_count"],
        ),
    )
    return selected, "no_validation_candidate_passed_profitability_gate_exploratory_only"


def run_research(
    symbol: str,
    data_dir: Path,
    *,
    fee_bps_per_side: float = 10.0,
    slippage_bps_per_side: float = 5.0,
    minimum_validation_trades: int = 8,
    output_path: Path | None = None,
) -> dict[str, Any]:
    fee_bps = Decimal(str(fee_bps_per_side))
    slippage_bps = Decimal(str(slippage_bps_per_side))
    rows, snapshots, data_summary = hb._prepare_snapshots(
        data_dir, symbol,
        fee_bps_per_side=fee_bps_per_side,
        slippage_bps_per_side=slippage_bps_per_side,
        minimum_net_rr=1.5,
        max_setup_age_hours=168,
    )
    rows_by_interval = {
        interval: hb._closed_rows(data_dir / f"{symbol}_{interval}.csv", symbol, interval)
        for interval in ("1h", "4h", "1d")
    }
    features = build_feature_rows(rows_by_interval, rows)
    baseline = hb.run_configuration(
        rows, snapshots,
        minimum_net_rr=1.5, max_setup_age_hours=168,
        fee_bps_per_side=fee_bps_per_side,
        slippage_bps_per_side=slippage_bps_per_side,
    )
    baseline_report = {
        "report_type": "formal_mtf_strategy_backtest",
        "report_version": 1,
        "data": data_summary,
        **baseline,
        "limitations": [
            "Formal strategy mtf-bos-ob-retest-v1 with closed candles and conservative OHLC simulation.",
            "Fees and slippage are explicit assumptions; no live order placement.",
        ],
    }
    hb._atomic_json(data_dir / f"{symbol}_mtf_backtest.json", baseline_report)

    candidates: list[dict[str, Any]] = []
    for target_mode in TARGETS:
        # For fixed-R targets, entry candidates must still pass the formal strategy's original 1.5 net-RR rule.
        filter_threshold = 1.5 if target_mode == "swing" else 0.01
        target_snaps = make_target_snapshots(snapshots, target_mode, fee_bps=fee_bps, slippage_bps=slippage_bps)
        bounds = hb._split_ranges(rows, 0.6, 0.2)
        for filter_name, description in FILTERS:
            split_metrics: dict[str, Any] = {}
            age_limit = 24 if filter_name == "age24" else 48 if filter_name == "age48" else 72 if filter_name in {"age72", "ema4h1d_rsi55_age72"} else 168
            for split_name, (start, end) in bounds.items():
                if split_name == "test":
                    continue
                trades = simulate_filtered_slice(
                    rows, target_snaps, features, start, end,
                    filter_name=filter_name,
                    minimum_net_rr=filter_threshold,
                    max_setup_age_hours=age_limit,
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                )
                split_metrics[split_name] = _metrics(trades)
            candidates.append({
                "target_mode": target_mode,
                "entry_mode": "touch",
                "filter": filter_name,
                "filter_description": description,
                "train": split_metrics["train"],
                "validation": split_metrics["validation"],
                "test_was_used_for_selection": False,
                "eligible_for_selection": split_metrics["validation"]["trade_count"] >= minimum_validation_trades,
            })

    confirmation_filters = {"none", "ema4h1d_position", "rsi55", "original_rr25", "relative_volume12"}
    for target_mode in TARGETS:
        target_snaps = make_target_snapshots(
            snapshots, target_mode, fee_bps=fee_bps, slippage_bps=slippage_bps
        )
        filter_threshold = 1.5 if target_mode == "swing" else 0.01
        bounds = hb._split_ranges(rows, 0.6, 0.2)
        for entry_mode, entry_description in ENTRY_MODES[1:]:
            for filter_name, description in FILTERS:
                if filter_name not in confirmation_filters:
                    continue
                split_metrics: dict[str, Any] = {}
                age_limit = (
                    24 if filter_name == "age24"
                    else 48 if filter_name == "age48"
                    else 72 if filter_name in {"age72", "ema4h1d_rsi55_age72"}
                    else 168
                )
                for split_name, (start, end) in bounds.items():
                    if split_name == "test":
                        continue
                    trades = simulate_filtered_slice(
                        rows, target_snaps, features, start, end,
                        filter_name=filter_name,
                        minimum_net_rr=filter_threshold,
                        max_setup_age_hours=age_limit,
                        fee_bps=fee_bps,
                        slippage_bps=slippage_bps,
                        entry_mode=entry_mode,
                        target_mode=target_mode,
                    )
                    split_metrics[split_name] = _metrics(trades)
                candidates.append({
                    "target_mode": target_mode,
                    "entry_mode": entry_mode,
                    "filter": filter_name,
                    "filter_description": description,
                    "entry_mode_description": entry_description,
                    "train": split_metrics["train"],
                    "validation": split_metrics["validation"],
                    "test_was_used_for_selection": False,
                    "eligible_for_selection": split_metrics["validation"]["trade_count"] >= minimum_validation_trades,
                })

    selected, selection_status = _select_candidate(candidates, minimum_validation_trades)
    selected_target = selected["target_mode"]
    selected_filter = selected["filter"]
    selected_entry_mode = selected.get("entry_mode", "touch")
    selected_threshold = 1.5 if selected_target == "swing" else 0.01
    selected_snaps = make_target_snapshots(snapshots, selected_target, fee_bps=fee_bps, slippage_bps=slippage_bps)
    test_start, test_end = hb._split_ranges(rows, 0.6, 0.2)["test"]
    selected_age_limit = 24 if selected_filter == "age24" else 48 if selected_filter == "age48" else 72 if selected_filter in {"age72", "ema4h1d_rsi55_age72"} else 168
    selected_test_trades = simulate_filtered_slice(
        rows, selected_snaps, features, test_start, test_end,
        filter_name=selected_filter,
        minimum_net_rr=selected_threshold,
        max_setup_age_hours=selected_age_limit,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        entry_mode=selected_entry_mode,
        target_mode=selected_target,
    )
    selected_test = _metrics(selected_test_trades)
    baseline_test = baseline["splits"]["test"]["metrics"]
    meets_goal = (
        selected_test["trade_count"] >= 20
        and selected_test["win_rate_percent"] >= TARGET_WIN_RATE
        and selected_test["expectancy_r"] > 0
        and _sort_profit_factor(selected_test["profit_factor"]) > 1
    )
    candidates_sorted = sorted(
        candidates,
        key=lambda row: (
            row["validation"]["win_rate_percent"],
            row["validation"]["expectancy_r"],
            _sort_profit_factor(row["validation"]["profit_factor"]),
            row["validation"]["trade_count"],
        ),
        reverse=True,
    )
    report = {
        "report_type": "formal_mtf_win_rate_filter_research",
        "report_version": 1,
        "data": data_summary,
        "method": {
            "strategy": "mtf-bos-ob-retest-v1",
            "candidate_pool": "Only causal candidates produced by the formal strategy and passing its original minimum net RR of 1.5 are considered.",
            "splits": "Chronological 60% train / 20% validation / 20% test; no pending order or position crosses a split.",
            "selection": "Rank on validation win rate among candidates meeting the minimum validation trade count. Prefer positive validation expectancy and profit factor above 1; otherwise the selected candidate is exploratory only.",
            "test_isolation": "Test metrics are calculated only after selection for the one selected candidate and the fixed formal baseline. Candidate grid rows do not contain test metrics.",
            "conservative_rules": "Next-1H-bar retest orders; adverse fees/slippage; stop-first when stop and target both touch; no fills when a fixed target was already reached during the retest bar before entry can be confirmed.",
            "fixed_R_targets": "Experimental exit-only variants. The formal entry, OB edge stop, and original swing-target eligibility gate remain unchanged; fixed-R targets are not claimed to be superior.",
            "feature_timing": "EMA, RSI, ATR, and volume filters use only candles fully closed by the signal timestamp.",
            "minimum_validation_trades": minimum_validation_trades,
            "minimum_validation_trades_for_profitability_gate": MIN_VALIDATION_TRADES_FOR_PROFITABILITY,
            "target_win_rate_percent": TARGET_WIN_RATE,
        },
        "baseline": {
            "parameters": baseline["parameters"],
            "splits": baseline["splits"],
            "summary": baseline["summary"],
        },
        "selection": {
            "status": selection_status,
            "selected_target_mode": selected_target,
            "selected_entry_mode": selected_entry_mode,
            "selected_entry_mode_description": next(description for name, description in ENTRY_MODES if name == selected_entry_mode),
            "selected_filter": selected_filter,
            "selected_filter_description": next(desc for name, desc in FILTERS if name == selected_filter),
            "selected_train_metrics": selected["train"],
            "selected_validation_metrics": selected["validation"],
            "selected_test_metrics": selected_test,
            "selected_test_trade_count": selected_test["trade_count"],
            "test_win_rate_point_estimate_at_least_70_percent": selected_test["win_rate_percent"] >= TARGET_WIN_RATE,
            "test_sample_at_least_20_trades": selected_test["trade_count"] >= 20,
            "test_meets_win_rate_and_profitability_gate": meets_goal,
            "test_trades": selected_test_trades,
        },
        "candidates": candidates_sorted,
        "limitations": [
            "70% is a research target, not a guarantee. A high win rate with too few trades or negative expectancy is not considered success.",
            "A fixed-R exit variant changes the formal strategy's exit target and is reported as experimental, not as the unchanged formal baseline.",
            "The predeclared entry-filter and exit-target grid is a controlled research set, but validation selection can still overfit; the holdout is evaluated only once for the selected candidate.",
            "Fees and slippage are assumptions and must be recalibrated to the exact venue and order type.",
            "This is a historical simulation, not investment advice or live trading.",
        ],
    }
    output = output_path or data_dir / f"{symbol}_winrate_research.json"
    hb._atomic_json(output, report)
    print(json.dumps({
        "output": str(output),
        "strategy": data_summary["strategy"],
        "baseline_test": baseline_test,
        "selection_status": selection_status,
        "selected_target_mode": selected_target,
        "selected_entry_mode": selected_entry_mode,
        "selected_filter": selected_filter,
        "selected_validation": selected["validation"],
        "selected_test": selected_test,
        "test_meets_70_percent_and_profitability_gate": meets_goal,
        "candidate_count": len(candidates),
    }, ensure_ascii=False, indent=2, allow_nan=False, default=hb._json_default))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Research entry filters and exit targets for the formal BTC MTF strategy; no trading."
    )
    parser.add_argument("symbol", nargs="?", default="BTCUSDT")
    parser.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--fee-bps-per-side", type=float, default=10.0)
    parser.add_argument("--slippage-bps-per-side", type=float, default=5.0)
    parser.add_argument("--minimum-validation-trades", type=int, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.minimum_validation_trades < 1:
            raise ValueError("minimum-validation-trades must be at least 1.")
        run_research(
            args.symbol, args.data_dir,
            fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
            minimum_validation_trades=args.minimum_validation_trades,
            output_path=args.output,
        )
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
