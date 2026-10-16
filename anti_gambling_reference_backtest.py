#!/usr/bin/env python3
"""Reproduce the explicit 5/20-SMA example and default risk settings in Anti-Gambling Trader.

Reference:
https://github.com/mars-tw/anti-gambling-trader-tw/blob/main/core/scaffold/templates.py

This is a benchmark of that repository's example code, not a claim that the
repository ships a complete or profitable BTC trading strategy.
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

SOURCE_STRATEGY = (
    "https://github.com/mars-tw/anti-gambling-trader-tw/blob/main/core/scaffold/templates.py"
)
SOURCE_README = "https://github.com/mars-tw/anti-gambling-trader-tw"


def _segments(rows: list[dict[str, Any]]) -> list[tuple[int, int]]:
    if not rows:
        return []
    step = INTERVAL_MILLISECONDS["1d"]
    output: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(rows)):
        if int(rows[index]["open_time"]) - int(rows[index - 1]["open_time"]) != step:
            output.append((start, index))
            start = index
    output.append((start, len(rows)))
    return output


def build_features(
    rows: list[dict[str, Any]], fast_period: int = 5, slow_period: int = 20
) -> list[dict[str, float | bool | None]]:
    """Compute close-based SMA signals, resetting at missing daily candles."""
    if fast_period < 1 or slow_period <= fast_period:
        raise ValueError("SMA periods must satisfy 1 <= fast_period < slow_period.")
    result: list[dict[str, float | bool | None]] = [
        {"sma_fast": None, "sma_slow": None, "long_condition": False}
        for _ in rows
    ]
    for start, end in _segments(rows):
        closes = [float(row["close"]) for row in rows[start:end]]
        prefix = [0.0]
        for close in closes:
            prefix.append(prefix[-1] + close)
        for local in range(slow_period - 1, len(closes)):
            fast = (prefix[local + 1] - prefix[local + 1 - fast_period]) / fast_period
            slow = (prefix[local + 1] - prefix[local + 1 - slow_period]) / slow_period
            result[start + local] = {
                "sma_fast": fast,
                "sma_slow": slow,
                "long_condition": fast > slow,
            }
    return result


def _position(
    index: int, rows: list[dict[str, Any]], entry_signal_time_ms: int,
    allocation: float, stop_loss_pct: float, take_profit_pct: float
) -> dict[str, Any]:
    entry = Decimal(str(rows[index]["open"]))
    stop = entry * Decimal(str(1.0 - stop_loss_pct))
    target = entry * Decimal(str(1.0 + take_profit_pct))
    return {
        "setup_id": f"agm-sma520-{int(rows[index]['open_time'])}",
        "target_event_id": f"agm-sma520-exit-{int(rows[index]['open_time'])}",
        "side": "long",
        "signal_time_ms": entry_signal_time_ms,
        "entry_time_ms": int(rows[index]["open_time"]),
        "entry_index": index,
        "raw_entry": entry,
        "stop": stop,
        "target": target,
        "allocation_pct": allocation,
    }


def _finish_trade(
    position: dict[str, Any], raw_exit: float | Decimal, exit_time_ms: int,
    reason: str, exit_index: int, fee_bps: Decimal, slippage_bps: Decimal,
) -> dict[str, Any]:
    trade = hb._make_trade(
        position, Decimal(str(raw_exit)), exit_time_ms, reason, exit_index,
        fee_bps, slippage_bps,
    )
    entry_fill = float(trade["entry_price"])
    net_pnl = float(trade["net_pnl_per_unit"])
    trade["net_return_on_exposed_capital_percent"] = (
        round(100.0 * net_pnl / entry_fill, 8) if entry_fill > 0 else 0.0
    )
    trade["allocation_pct"] = float(position["allocation_pct"])
    trade["portfolio_return_percent"] = round(
        trade["net_return_on_exposed_capital_percent"] * float(position["allocation_pct"]), 8
    )
    return trade


def simulate_slice(
    rows: list[dict[str, Any]],
    features: list[dict[str, float | bool | None]],
    start: int,
    end: int,
    *,
    stop_loss_pct: float = 0.05,
    take_profit_pct: float = 0.15,
    allocation_pct: float = 0.20,
    fee_bps_per_side: float = 10.0,
    slippage_bps_per_side: float = 5.0,
) -> list[dict[str, Any]]:
    """Close-based template interpretation; signals fill on the next daily open.

    The source template checks the latest close against the 5%/15% levels and
    returns a sell signal. It does not define broker-native intrabar bracket
    orders, so this backtest deliberately follows that close-confirmed behavior.
    """
    if not (0 < stop_loss_pct < 1 and take_profit_pct > 0):
        raise ValueError("Stop loss must be in (0, 1), and take profit must be positive.")
    if not (0 < allocation_pct <= 1):
        raise ValueError("Allocation pct must be in (0, 1].")
    if not (0 <= start <= end <= len(rows)) or len(rows) != len(features):
        raise ValueError("Slice bounds or feature lengths are invalid.")

    fees = Decimal(str(fee_bps_per_side))
    slippage = Decimal(str(slippage_bps_per_side))
    if fees < 0 or slippage < 0:
        raise ValueError("Fees and slippage must be non-negative.")

    trades: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    pending_action: str | None = None
    pending_reason: str | None = None
    pending_signal_time_ms: int | None = None

    for index in range(start, end):
        row = rows[index]
        if index > start:
            prior = rows[index - 1]
            contiguous = (
                int(row["open_time"]) - int(prior["open_time"])
                == INTERVAL_MILLISECONDS["1d"]
            )
            if not contiguous:
                if position is not None:
                    trades.append(_finish_trade(
                        position, float(prior["close"]), int(prior["close_time"]) + 1,
                        "data_gap_exit", index - 1, fees, slippage,
                    ))
                    position = None
                pending_action = None
                pending_reason = None
                pending_signal_time_ms = None

        # Action raised at the previous daily close is executed at this daily open.
        if pending_action == "buy" and position is None:
            signal_ms = pending_signal_time_ms
            if signal_ms is not None:
                position = _position(
                    index, rows, signal_ms, allocation_pct,
                    stop_loss_pct, take_profit_pct,
                )
        elif pending_action == "sell" and position is not None:
            trades.append(_finish_trade(
                position, float(row["open"]), int(row["open_time"]),
                pending_reason or "close_exit_signal", index, fees, slippage,
            ))
            position = None
        pending_action = None
        pending_reason = None
        pending_signal_time_ms = None

        close = float(row["close"])
        if position is not None:
            entry = float(position["raw_entry"])
            if close <= entry * (1.0 - stop_loss_pct):
                pending_action = "sell"
                pending_reason = "stop_loss_close_signal"
                pending_signal_time_ms = int(row["close_time"]) + 1
            elif close >= entry * (1.0 + take_profit_pct):
                pending_action = "sell"
                pending_reason = "take_profit_close_signal"
                pending_signal_time_ms = int(row["close_time"]) + 1
        else:
            feature = features[index]
            if feature.get("long_condition") is True:
                pending_action = "buy"
                pending_signal_time_ms = int(row["close_time"]) + 1

    if position is not None and end > start:
        last = rows[end - 1]
        trades.append(_finish_trade(
            position, float(last["close"]), int(last["close_time"]) + 1,
            "split_end", end - 1, fees, slippage,
        ))
    return trades


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    net_returns = [
        float(trade["net_return_on_exposed_capital_percent"]) / 100.0
        for trade in trades
    ]
    wins = [value for value in net_returns if value > 0]
    losses = [value for value in net_returns if value < 0]
    gross_wins = sum(wins)
    gross_losses = -sum(losses)
    equity = peak = 1.0
    max_drawdown = 0.0
    for trade in trades:
        portfolio_return = float(trade["portfolio_return_percent"]) / 100.0
        equity *= max(0.0, 1.0 + portfolio_return)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return {
        "trade_count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": round(100.0 * len(wins) / len(trades), 4) if trades else 0.0,
        "average_win_percent_on_exposed_capital": round(100.0 * sum(wins) / len(wins), 8) if wins else 0.0,
        "average_loss_percent_on_exposed_capital": round(100.0 * sum(losses) / len(losses), 8) if losses else 0.0,
        "expectancy_percent_on_exposed_capital": round(100.0 * sum(net_returns) / len(net_returns), 8) if net_returns else 0.0,
        "profit_factor": round(gross_wins / gross_losses, 8) if gross_losses else ("infinite" if wins else None),
        "compounded_portfolio_return_percent": round(100.0 * (equity - 1.0), 8),
        "max_drawdown_percent": round(100.0 * max_drawdown, 8),
        "average_days_held": round(sum(int(t["bars_held"]) for t in trades) / len(trades), 4) if trades else 0.0,
        "exit_reasons": {
            reason: sum(1 for trade in trades if trade["exit_reason"] == reason)
            for reason in sorted({str(trade["exit_reason"]) for trade in trades})
        },
    }


def run_benchmark(
    symbol: str,
    data_dir: Path,
    *,
    fee_bps_per_side: float = 10.0,
    slippage_bps_per_side: float = 5.0,
    fast_period: int = 5,
    slow_period: int = 20,
    stop_loss_pct: float = 0.05,
    take_profit_pct: float = 0.15,
    allocation_pct: float = 0.20,
    output_path: Path | None = None,
) -> dict[str, Any]:
    rows = hb._closed_rows(data_dir / f"{symbol}_1d.csv", symbol, "1d")
    if len(rows) < 250:
        raise ValueError("At least 250 closed daily candles are required.")
    features = build_features(rows, fast_period, slow_period)
    n = len(rows)
    split_bounds = {
        "train": (0, int(n * 0.6)),
        "validation": (int(n * 0.6), int(n * 0.8)),
        "test": (int(n * 0.8), n),
    }
    splits: dict[str, Any] = {}
    all_trades: dict[str, list[dict[str, Any]]] = {}
    for split_name, (start, end) in split_bounds.items():
        trades = simulate_slice(
            rows, features, start, end,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            allocation_pct=allocation_pct,
            fee_bps_per_side=fee_bps_per_side,
            slippage_bps_per_side=slippage_bps_per_side,
        )
        all_trades[split_name] = trades
        splits[split_name] = {"metrics": _metrics(trades), "trades": trades}

    integrity = hb.inspect_candle_integrity(rows, "1d")
    report = {
        "report_type": "anti_gambling_trader_5_20_sma_example_backtest",
        "report_version": 1,
        "source_project": SOURCE_README,
        "source_strategy_file": SOURCE_STRATEGY,
        "source_parameters": {
            "fast_sma_days": fast_period,
            "slow_sma_days": slow_period,
            "stop_loss_percent": stop_loss_pct * 100.0,
            "take_profit_percent": take_profit_pct * 100.0,
            "max_position_pct_of_equity": allocation_pct * 100.0,
            "paper_fee_rate_per_side_percent": fee_bps_per_side / 100.0,
            "paper_slippage_per_side_percent": slippage_bps_per_side / 100.0,
        },
        "data": {
            "symbol": symbol,
            "interval": "1d",
            "source": "Binance Spot historical OHLCV",
            "closed_candles": len(rows),
            "start_utc": rows[0].get("open_time_utc"),
            "end_utc": rows[-1].get("close_time_utc"),
            "gap_count": len(integrity["gaps"]),
            "gap_details": integrity["gaps"],
        },
        "method": {
            "entry": "Faithful to the source example: when flat and the latest fully closed candle has SMA(5) > SMA(20), create a buy signal and enter at the next daily open. It is a state condition, not a cross-only event.",
            "exit": "Faithful to the provided Strategy.on_bar template: evaluate the latest daily close against entry price -5% / +15%; once crossed at close, sell at the next daily open. The source does not define broker-native intrabar stop/target orders.",
            "split": "Chronological 60% train / 20% validation / 20% test. The same fixed parameters are used in all splits, with no tuning. Every split starts flat and closes any remaining position at its final close.",
            "costs": "Paper defaults interpreted as 0.10% fee and 0.05% slippage per side. Costs are applied to both entries and exits.",
            "position_size": "Paper template max_position_pct=20% is modeled as 20% of current equity allocated to each sequential position for portfolio-return and drawdown estimates.",
            "limitations": [
                "This is the only explicit MA example found in the source strategy scaffold, not a complete officially backtested strategy published by the project.",
                "The source scaffold's rule is example text and its actual entry is left TODO; this independent reconstruction turns the example into a reproducible testable specification.",
                "Exit thresholds are close-confirmed, so realized losses can exceed 5% and gains can differ from 15% after overnight gaps and slippage.",
                "Spot long-only; no leverage or short positions.",
                "Historical results do not establish future profitability."
            ],
        },
        "splits": splits,
    }
    output = output_path or data_dir / f"{symbol}_anti_gambling_5_20_sma_backtest.json"
    hb._atomic_json(output, report)
    print(json.dumps({
        "output": str(output),
        "data": report["data"],
        "parameters": report["source_parameters"],
        "results": {name: section["metrics"] for name, section in splits.items()},
    }, ensure_ascii=False, indent=2, allow_nan=False, default=hb._json_default))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest Anti-Gambling Trader's explicit 5/20-SMA example on BTCUSDT daily spot candles.")
    parser.add_argument("symbol", nargs="?", default="BTCUSDT")
    parser.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--fee-bps-per-side", type=float, default=10.0)
    parser.add_argument("--slippage-bps-per-side", type=float, default=5.0)
    parser.add_argument("--fast-period", type=int, default=5)
    parser.add_argument("--slow-period", type=int, default=20)
    parser.add_argument("--stop-loss-percent", type=float, default=5.0)
    parser.add_argument("--take-profit-percent", type=float, default=15.0)
    parser.add_argument("--allocation-percent", type=float, default=20.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        run_benchmark(
            args.symbol, args.data_dir,
            fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
            fast_period=args.fast_period,
            slow_period=args.slow_period,
            stop_loss_pct=args.stop_loss_percent / 100.0,
            take_profit_pct=args.take_profit_percent / 100.0,
            allocation_pct=args.allocation_percent / 100.0,
            output_path=args.output,
        )
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
