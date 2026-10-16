#!/usr/bin/env python3
"""Benchmark independently implemented public BTC strategy rules on our historical candles.

The reference logic is attributed in the report. This is a research benchmark, not
a claim that public strategies are profitable or a live trading system.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import historical_backtest as hb
from market_structure import INTERVAL_MILLISECONDS

MACD_SOURCE = "https://github.com/VivensB/strategy-backtests/blob/main/backtest.py"
SUPERTREND_SOURCE = "https://github.com/VivensB/strategy-backtests/blob/main/supertrend.py"


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _ewilder(values: list[float | None], period: int) -> list[float | None]:
    """Recursive Wilder smoothing, matching an EWM alpha of 1 / period."""
    result: list[float | None] = [None] * len(values)
    current: float | None = None
    alpha = 1.0 / period
    for index, value in enumerate(values):
        if value is None or not math.isfinite(value):
            continue
        current = value if current is None else alpha * value + (1.0 - alpha) * current
        result[index] = current
    return result


def _segment_ranges(rows: list[dict[str, Any]]) -> list[tuple[int, int]]:
    if not rows:
        return []
    step = INTERVAL_MILLISECONDS["1h"]
    ranges: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(rows)):
        if int(rows[index]["open_time"]) - int(rows[index - 1]["open_time"]) != step:
            ranges.append((start, index))
            start = index
    ranges.append((start, len(rows)))
    return ranges


def build_macd_events(rows: list[dict[str, Any]]) -> list[int]:
    """Return MACD(12,26,9) cross events at candle close: +1 up, -1 down, 0 none."""
    events = [0] * len(rows)
    for start, end in _segment_ranges(rows):
        close = [float(row["close"]) for row in rows[start:end]]
        if len(close) < 2:
            continue
        fast = _ema(close, 12)
        slow = _ema(close, 26)
        line = [a - b for a, b in zip(fast, slow)]
        signal = _ema(line, 9)
        for local in range(1, len(close)):
            previous_above = line[local - 1] > signal[local - 1]
            current_above = line[local] > signal[local]
            index = start + local
            if current_above and not previous_above:
                events[index] = 1
            elif not current_above and previous_above:
                events[index] = -1
    return events


def _heikin_ashi(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    ha_close: list[float] = []
    ha_open: list[float] = []
    ha_high: list[float] = []
    ha_low: list[float] = []
    for index, row in enumerate(rows):
        op = float(row["open"])
        hi = float(row["high"])
        lo = float(row["low"])
        close = float(row["close"])
        hc = (op + hi + lo + close) / 4.0
        ho = (op + close) / 2.0 if index == 0 else (ha_open[-1] + ha_close[-1]) / 2.0
        ha_close.append(hc)
        ha_open.append(ho)
        ha_high.append(max(hi, ho, hc))
        ha_low.append(min(lo, ho, hc))
    return {"open": ha_open, "high": ha_high, "low": ha_low, "close": ha_close}


def _true_range(high: list[float], low: list[float], close: list[float]) -> list[float]:
    result: list[float] = []
    for index, (hi, lo) in enumerate(zip(high, low)):
        if index == 0:
            result.append(hi - lo)
        else:
            previous_close = close[index - 1]
            result.append(max(hi - lo, abs(hi - previous_close), abs(lo - previous_close)))
    return result


def _atr(high: list[float], low: list[float], close: list[float], period: int) -> list[float]:
    tr = _true_range(high, low, close)
    smoothed = _ewilder([float(value) for value in tr], period)
    return [0.0 if value is None else float(value) for value in smoothed]


def _adx(high: list[float], low: list[float], close: list[float], period: int = 14) -> list[float | None]:
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    for index in range(1, len(close)):
        up = high[index] - high[index - 1]
        down = low[index - 1] - low[index]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    atr = _atr(high, low, close, period)
    plus = _ewilder(plus_dm, period)
    minus = _ewilder(minus_dm, period)
    dx: list[float | None] = []
    for index in range(len(close)):
        if atr[index] <= 0 or plus[index] is None or minus[index] is None:
            dx.append(None)
            continue
        pdi = 100.0 * float(plus[index]) / atr[index]
        mdi = 100.0 * float(minus[index]) / atr[index]
        denom = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else None)
    return _ewilder(dx, period)


def build_supertrend_signals(
    rows: list[dict[str, Any]],
    *,
    period: int = 14,
    multiplier: float = 3.0,
    adx_min: float = 20.0,
) -> tuple[list[int], list[float], list[float | None]]:
    """Reference-style HA SuperTrend flips with ADX gate and ATR levels.

    The public reference explicitly documents its non-textbook SuperTrend seed.
    This function reproduces that seed instead of silently substituting a different
    indicator implementation. It resets state after gaps in the hourly series.
    """
    events = [0] * len(rows)
    atr_by_index = [0.0] * len(rows)
    adx_by_index: list[float | None] = [None] * len(rows)
    for start, end in _segment_ranges(rows):
        segment = rows[start:end]
        ha = _heikin_ashi(segment)
        atr = _atr(ha["high"], ha["low"], ha["close"], period)
        adx = _adx(ha["high"], ha["low"], ha["close"], period)
        trend: str | None = None
        upper_band: float | None = None
        lower_band: float | None = None
        directions: list[str | None] = []
        for local, close in enumerate(ha["close"]):
            upper = close + multiplier * atr[local]
            lower = close - multiplier * atr[local]
            if trend == "up" and lower_band is not None and close < lower_band:
                trend = "down"
            elif trend == "down" and upper_band is not None and close > upper_band:
                trend = "up"
            if trend == "up":
                upper_band = upper
                lower_band = max(lower_band if lower_band is not None else lower, lower)
            elif trend == "down":
                lower_band = lower
                upper_band = min(upper_band if upper_band is not None else upper, upper)
            else:
                trend = "up" if close > upper else "down"
                upper_band, lower_band = upper, lower
            directions.append(trend)
            global_index = start + local
            atr_by_index[global_index] = atr[local]
            adx_by_index[global_index] = adx[local]
        for local in range(1, len(segment)):
            if directions[local] != directions[local - 1]:
                adx_value = adx[local]
                if adx_value is not None and adx_value > adx_min:
                    events[start + local] = 1 if directions[local] == "up" else -1
    return events, atr_by_index, adx_by_index


def _fill(side: int, raw_price: float, slip_rate: float, *, entry: bool) -> float:
    if side > 0:
        return raw_price * (1.0 + slip_rate if entry else 1.0 - slip_rate)
    return raw_price * (1.0 - slip_rate if entry else 1.0 + slip_rate)


def _close_bracket_trade(
    position: dict[str, Any],
    raw_exit: float,
    exit_time_ms: int,
    reason: str,
    exit_index: int,
    fee_bps: Decimal,
    slip_bps: Decimal,
) -> dict[str, Any]:
    trade = hb._make_trade(
        {
            "setup_id": position["setup_id"],
            "target_event_id": position["target_event_id"],
            "side": "long" if position["side"] > 0 else "short",
            "signal_time_ms": position["signal_time_ms"],
            "entry_time_ms": position["entry_time_ms"],
            "entry_index": position["entry_index"],
            "raw_entry": Decimal(str(position["raw_entry"])),
            "stop": Decimal(str(position["stop"])),
            "target": Decimal(str(position["target"])),
        },
        Decimal(str(raw_exit)),
        exit_time_ms,
        reason,
        exit_index,
        fee_bps,
        slip_bps,
    )
    entry_fill = float(trade["entry_price"])
    if entry_fill > 0:
        trade["net_return_percent"] = round(100.0 * float(trade["net_pnl_per_unit"]) / entry_fill, 8)
    return trade


def _close_macd_trade(
    position: dict[str, Any],
    raw_exit: float,
    exit_time_ms: int,
    reason: str,
    exit_index: int,
    rows: list[dict[str, Any]],
    fee_rate: float,
    slip_rate: float,
) -> dict[str, Any]:
    entry_fill = _fill(position["side"], position["raw_entry"], slip_rate, entry=True)
    exit_fill = _fill(position["side"], raw_exit, slip_rate, entry=False)
    gross = position["side"] * (exit_fill - entry_fill)
    fees = fee_rate * (entry_fill + exit_fill)
    net = gross - fees
    return {
        "strategy": position["strategy"],
        "direction": "long" if position["side"] > 0 else "short",
        "signal_time_utc": hb._utc(position["signal_time_ms"]),
        "entry_time_utc": hb._utc(position["entry_time_ms"]),
        "exit_time_utc": hb._utc(exit_time_ms),
        "raw_entry_price": str(position["raw_entry"]),
        "entry_fill_price": round(entry_fill, 8),
        "exit_fill_price": round(exit_fill, 8),
        "bars_held": max(1, exit_index - position["entry_index"]),
        "exit_reason": reason,
        "net_pnl_per_unit": round(net, 8),
        "net_return_percent": round(100.0 * net / entry_fill, 8) if entry_fill else 0.0,
    }


def _trade_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(trade["net_return_percent"]) / 100.0 for trade in trades]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return {
        "trade_count": len(trades),
        "win_rate_percent": round(100.0 * len(wins) / len(trades), 4) if trades else 0.0,
        "expectancy_return_percent": round(100.0 * sum(returns) / len(returns), 8) if returns else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 8) if losses else ("infinite" if wins else None),
        "compounded_return_percent": round(100.0 * (equity - 1.0), 8),
        "max_drawdown_percent": round(100.0 * max_drawdown, 8),
        "average_bars_held": round(sum(int(t["bars_held"]) for t in trades) / len(trades), 4) if trades else 0.0,
        "exit_reasons": {
            name: sum(1 for trade in trades if trade["exit_reason"] == name)
            for name in sorted({str(trade["exit_reason"]) for trade in trades})
        },
    }


def run_macd_slice(
    rows: list[dict[str, Any]],
    events: list[int],
    start: int,
    end: int,
    *,
    long_short: bool,
    fee_bps: Decimal,
    slip_bps: Decimal,
) -> list[dict[str, Any]]:
    """MACD crossover position strategy; closed-bar signal executes next bar open."""
    trades: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    fee_rate = float(fee_bps) / 10000.0
    slip_rate = float(slip_bps) / 10000.0
    for index in range(start + 1, end):
        previous = rows[index - 1]
        current = rows[index]
        contiguous = int(current["open_time"]) - int(previous["open_time"]) == INTERVAL_MILLISECONDS["1h"]
        if not contiguous:
            if position is not None:
                trades.append(_close_macd_trade(
                    position, float(previous["close"]), int(previous["close_time"]) + 1,
                    "data_gap_exit", index - 1, rows, fee_rate, slip_rate,
                ))
                position = None
            continue
        event = events[index - 1]
        if event == 0:
            continue
        desired = event if long_short else (1 if event > 0 else 0)
        if position is not None and position["side"] != desired:
            trades.append(_close_macd_trade(
                position, float(current["open"]), int(current["open_time"]),
                "opposite_macd_cross", index, rows, fee_rate, slip_rate,
            ))
            position = None
        if desired != 0 and position is None:
            position = {
                "strategy": "macd_long_short" if long_short else "macd_long_only",
                "side": desired,
                "raw_entry": float(current["open"]),
                "signal_time_ms": int(previous["close_time"]) + 1,
                "entry_time_ms": int(current["open_time"]),
                "entry_index": index,
            }
    if position is not None and end > start:
        last = rows[end - 1]
        trades.append(_close_macd_trade(
            position, float(last["close"]), int(last["close_time"]) + 1,
            "split_end", end - 1, rows, fee_rate, slip_rate,
        ))
    return trades


def run_supertrend_slice(
    rows: list[dict[str, Any]],
    events: list[int],
    atr_values: list[float],
    start: int,
    end: int,
    *,
    long_only: bool,
    fee_bps: Decimal,
    slip_bps: Decimal,
    stop_atr: float = 1.5,
    target_atr: float = 2.0,
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    for index in range(start, end):
        bar = rows[index]
        if index > start:
            prior = rows[index - 1]
            contiguous = int(bar["open_time"]) - int(prior["open_time"]) == INTERVAL_MILLISECONDS["1h"]
            if not contiguous:
                if position is not None:
                    trades.append(_close_bracket_trade(
                        position, float(prior["close"]), int(prior["close_time"]) + 1,
                        "data_gap_exit", index - 1, fee_bps, slip_bps,
                    ))
                    position = None
                continue
        if position is not None:
            op = float(bar["open"])
            hi = float(bar["high"])
            lo = float(bar["low"])
            side = int(position["side"])
            stop = float(position["stop"])
            target = float(position["target"])
            hit_stop = (op <= stop or lo <= stop) if side > 0 else (op >= stop or hi >= stop)
            hit_target = (op >= target or hi >= target) if side > 0 else (op <= target or lo <= target)
            if hit_stop or hit_target:
                if hit_stop:
                    raw_exit = op if ((side > 0 and op <= stop) or (side < 0 and op >= stop)) else stop
                    reason = "stop"
                else:
                    raw_exit = target
                    reason = "target"
                trades.append(_close_bracket_trade(
                    position, raw_exit, int(bar["close_time"]) + 1,
                    reason, index, fee_bps, slip_bps,
                ))
                position = None
        if position is None and index + 1 < end and events[index] != 0:
            next_bar = rows[index + 1]
            if int(next_bar["open_time"]) - int(bar["open_time"]) != INTERVAL_MILLISECONDS["1h"]:
                continue
            signal_side = events[index]
            if long_only and signal_side < 0:
                continue
            side = signal_side
            entry = float(next_bar["open"])
            atr_value = float(atr_values[index])
            if not math.isfinite(atr_value) or atr_value <= 0:
                continue
            stop = entry - side * stop_atr * atr_value
            target = entry + side * target_atr * atr_value
            if stop <= 0 or target <= 0:
                continue
            position = {
                "setup_id": f"supertrend-{index}-{side}",
                "target_event_id": f"supertrend-target-{index}-{side}",
                "side": side,
                "signal_time_ms": int(bar["close_time"]) + 1,
                "entry_time_ms": int(next_bar["open_time"]),
                "entry_index": index + 1,
                "raw_entry": entry,
                "stop": stop,
                "target": target,
            }
    if position is not None and end > start:
        last = rows[end - 1]
        trades.append(_close_bracket_trade(
            position, float(last["close"]), int(last["close_time"]) + 1,
            "split_end", end - 1, fee_bps, slip_bps,
        ))
    return trades


def run_benchmarks(
    symbol: str,
    data_dir: Path,
    *,
    fee_bps_per_side: float = 10.0,
    slippage_bps_per_side: float = 5.0,
    output_path: Path | None = None,
) -> dict[str, Any]:
    fee_bps = Decimal(str(fee_bps_per_side))
    slip_bps = Decimal(str(slippage_bps_per_side))
    if fee_bps < 0 or slip_bps < 0:
        raise ValueError("Fees and slippage must be non-negative.")
    rows = hb._closed_rows(data_dir / f"{symbol}_1h.csv", symbol, "1h")
    if len(rows) < 500:
        raise ValueError("At least 500 closed hourly candles are required for a meaningful exploratory comparison.")
    macd_events = build_macd_events(rows)
    st_events, st_atr, st_adx = build_supertrend_signals(rows)
    train_end = int(len(rows) * 0.6)
    validation_end = int(len(rows) * 0.8)
    splits = {
        "train": (0, train_end),
        "validation": (train_end, validation_end),
        "test": (validation_end, len(rows)),
    }
    strategies: list[dict[str, Any]] = []
    methods = [
        (
            "macd_long_only",
            MACD_SOURCE,
            "MACD(12,26,9) crossover; long on upward cross, flat on downward cross; next-hour open execution; exit-only signal logic as in the public reference.",
            lambda start, end: run_macd_slice(
                rows, macd_events, start, end, long_short=False,
                fee_bps=fee_bps, slip_bps=slip_bps,
            ),
        ),
        (
            "macd_long_short",
            MACD_SOURCE,
            "MACD(12,26,9) crossover; long on upward cross, short on downward cross; next-hour open execution. Shorting requires a suitable derivatives/margin product.",
            lambda start, end: run_macd_slice(
                rows, macd_events, start, end, long_short=True,
                fee_bps=fee_bps, slip_bps=slip_bps,
            ),
        ),
        (
            "supertrend_adx_long_only",
            SUPERTREND_SOURCE,
            "Heikin-Ashi-based SuperTrend(14,3), ADX(14)>20; enter long on upward trend flip, exit on 1.5x ATR stop or 2.0x ATR target. Spot-oriented adaptation; a downward flip does not open a short.",
            lambda start, end: run_supertrend_slice(
                rows, st_events, st_atr, start, end, long_only=True,
                fee_bps=fee_bps, slip_bps=slip_bps,
            ),
        ),
        (
            "supertrend_adx_long_short",
            SUPERTREND_SOURCE,
            "Heikin-Ashi-based SuperTrend(14,3), ADX(14)>20; trend-flip entry, 1.5x ATR stop, 2.0x ATR target; next-hour open execution. Shorting requires a suitable derivatives/margin product.",
            lambda start, end: run_supertrend_slice(
                rows, st_events, st_atr, start, end, long_only=False,
                fee_bps=fee_bps, slip_bps=slip_bps,
            ),
        ),
    ]
    for name, source, rule, run_slice in methods:
        split_reports: dict[str, Any] = {}
        for split_name, (start, end) in splits.items():
            trades = run_slice(start, end)
            split_reports[split_name] = {
                "metrics": _trade_metrics(trades),
                "trades": trades,
            }
        strategies.append({
            "name": name,
            "source_reference": source,
            "rule": rule,
            "splits": split_reports,
        })
    report = {
        "report_type": "public_reference_strategy_benchmarks",
        "report_version": 1,
        "data": {
            "symbol": symbol,
            "interval": "1h",
            "source": "Binance Spot historical OHLCV from historical_backtest.py",
            "closed_candle_count": len(rows),
            "start_utc": rows[0].get("open_time_utc"),
            "end_utc": rows[-1].get("close_time_utc"),
            "gap_count": len(hb.inspect_candle_integrity(rows, "1h")["gaps"]),
        },
        "method": {
            "split": "Chronological 60% train / 20% validation / 20% test. Each split starts flat and trades are liquidated at its end; there is no parameter tuning.",
            "execution": "Signals use closed hourly candles and execute at the next hourly open. No trade is carried across gaps or split boundaries.",
            "costs": {
                "fee_bps_per_side": float(fee_bps),
                "slippage_bps_per_side": float(slip_bps),
            },
            "risk": "SuperTrend exits at ATR-based stop/target; if both are touched in the same bar, stop wins. A stop gap exits at the bar open where that is worse than the stop level. MACD exits only on the opposite cross or split/data-gap end, matching the reference's signal-exit style.",
            "metrics": "Per-trade net percentage return, compounded return assuming full notional is reinvested per sequential trade, win rate, profit factor and drawdown. These are not directly interchangeable with the formal OB strategy's R-multiple statistics.",
            "indicator_notes": "EMA recursion is causal. Indicators are re-initialized at hourly data gaps. The SuperTrend state seed follows the explicitly documented quirk in its public reference; it is not claimed to be the universally canonical SuperTrend implementation.",
        },
        "strategies": strategies,
        "limitations": [
            "The cited reference study used Binance USDT-M futures data for a limited 2025-10-01 to 2026-08-30 period and its own fee assumptions; this report independently retests reference rules on this repository's longer Binance Spot history and configured costs.",
            "Short-side results assume short capability and are not directly executable as spot trades.",
            "Publicly shared source code is a reproducible starting point, not proof of profitability. No parameters were selected using the test split.",
            "A historical backtest is not evidence of future returns or investment advice.",
        ],
    }
    output = output_path or data_dir / f"{symbol}_reference_strategy_benchmarks.json"
    hb._atomic_json(output, report)
    readable = {
        name: {split: data["metrics"] for split, data in strategy["splits"].items()}
        for strategy in strategies
        for name in [strategy["name"]]
    }
    print(json.dumps({
        "output": str(output),
        "data": report["data"],
        "fees_bps_per_side": float(fee_bps),
        "slippage_bps_per_side": float(slip_bps),
        "results": readable,
    }, ensure_ascii=False, indent=2, allow_nan=False, default=hb._json_default))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark public MACD and SuperTrend strategy rules on historical BTC candles.")
    parser.add_argument("symbol", nargs="?", default="BTCUSDT")
    parser.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--fee-bps-per-side", type=float, default=10.0)
    parser.add_argument("--slippage-bps-per-side", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        run_benchmarks(
            args.symbol,
            args.data_dir,
            fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
            output_path=args.output,
        )
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
