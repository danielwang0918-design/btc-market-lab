#!/usr/bin/env python3
"""Historical, close-only backtesting for the existing BTC Market Lab rules.

This tool is research-only. It never authenticates with an exchange or places orders.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import binance_market_data
import market_research
from market_research import INTERVALS, _load_archive
from market_structure import inspect_candle_integrity


def _number(value: Any, label: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not number.is_finite() or (positive and number <= 0) or (nonnegative and number < 0):
        raise ValueError(f"{label} has an invalid value")
    return number


def _iso_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


def _asof_archive(
    path: Path,
    metadata: dict[str, Any],
    candles: list[dict[str, str]],
    events: list[dict[str, str]],
    symbol: str,
    asof_ms: int,
    current_price: str,
) -> Path:
    """Build a temporary data package containing only information known by asof_ms."""
    past_candles = [row for row in candles if row.get("symbol") == symbol and int(row["close_time"]) < asof_ms]
    past_events = [row for row in events if row.get("symbol") == symbol and int(row.get("confirmed_at_close_time", "-1")) < asof_ms]
    past_spot = [{"symbol": symbol, "price": current_price, "captured_at_utc": _iso_ms(asof_ms), "source": binance_market_data.SOURCE_SPOT}]
    asof_metadata = dict(metadata)
    asof_metadata["binance_server_time"] = asof_ms
    asof_metadata["binance_server_time_utc"] = _iso_ms(asof_ms)
    intervals: dict[str, Any] = {}
    for interval in INTERVALS:
        interval_rows = [row for row in past_candles if row.get("interval") == interval]
        intervals[interval] = inspect_candle_integrity(interval_rows, interval, asof_ms)
    asof_metadata["data_integrity"] = {"intervals": intervals}

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.json", json.dumps(asof_metadata, ensure_ascii=False))
        for member, rows in (("klines.csv", past_candles), ("market_structure.csv", past_events), ("spot_price.csv", past_spot)):
            fields = list(rows[0]) if rows else {
                "klines.csv": binance_market_data.KLINE_COLUMNS,
                "market_structure.csv": binance_market_data.EVENT_COLUMNS,
                "spot_price.csv": ["symbol", "price", "captured_at_utc", "source"],
            }[member]
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(member, "\ufeff" + output.getvalue())
    return path


def _fill_prices(side: str, raw_entry: Decimal, raw_exit: Decimal, fee: Decimal, slip: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    slip_rate = slip / Decimal(10_000)
    fee_rate = fee / Decimal(10_000)
    entry = raw_entry * (Decimal(1) + slip_rate if side == "long" else Decimal(1) - slip_rate)
    exit_price = raw_exit * (Decimal(1) - slip_rate if side == "long" else Decimal(1) + slip_rate)
    entry_fee = entry * fee_rate
    exit_fee = exit_price * fee_rate
    return entry, exit_price, entry_fee, exit_fee


def _simulate_trade(
    *, side: str, signal: dict[str, Any], zone_low: Decimal, zone_high: Decimal,
    stop: Decimal, target: Decimal, candles: list[dict[str, str]], entry_index: int,
    fee_bps: Decimal, slippage_bps: Decimal,
) -> dict[str, Any] | None:
    """Fill a one-bar entry order, then walk OHLC bars with stop-first ambiguity handling."""
    first = candles[entry_index]
    first_low, first_high = _number(first["low"], "low", positive=True), _number(first["high"], "high", positive=True)
    if first_high < zone_low or first_low > zone_high:
        return None
    open_price = _number(first["open"], "open", positive=True)
    # A gap through the invalidation level is treated as invalid before entry.
    if side == "long" and open_price <= stop or side == "short" and open_price >= stop:
        return None
    raw_entry = zone_high if side == "long" else zone_low
    entry, stop_exit, entry_fee, stop_fee = _fill_prices(side, raw_entry, stop, fee_bps, slippage_bps)
    risk_per_unit = abs(entry - stop_exit) + entry_fee + stop_fee
    if risk_per_unit <= 0:
        return None
    exit_raw: Decimal | None = None
    exit_time: str | None = None
    exit_reason = "end_of_data"
    exit_bar_index = len(candles) - 1
    for index in range(entry_index, len(candles)):
        bar = candles[index]
        low, high = _number(bar["low"], "low", positive=True), _number(bar["high"], "high", positive=True)
        op = _number(bar["open"], "open", positive=True)
        stop_hit = low <= stop if side == "long" else high >= stop
        target_hit = high >= target if side == "long" else low <= target
        if stop_hit:
            # If the open gaps through the stop, use the worse open as the raw fill.
            exit_raw = min(stop, op) if side == "long" else max(stop, op)
            exit_reason = "stop"
        elif target_hit:
            # Limit target fills at the target; no optimistic gap improvement is assumed.
            exit_raw = target
            exit_reason = "target"
        if exit_raw is not None:
            exit_time = _iso_ms(int(bar["close_time"]) + 1)
            exit_bar_index = index
            break
    if exit_raw is None:
        bar = candles[-1]
        exit_raw = _number(bar["close"], "close", positive=True)
        exit_time = _iso_ms(int(bar["close_time"]) + 1)
    _, exit_price, entry_fee, exit_fee = _fill_prices(side, raw_entry, exit_raw, fee_bps, slippage_bps)
    gross = (exit_price - entry) if side == "long" else (entry - exit_price)
    net = gross - entry_fee - exit_fee
    r_multiple = net / risk_per_unit
    return {
        "setup_id": signal["scenario"]["entry_event_id"],
        "target_event_id": signal["scenario"]["target_event_id"],
        "direction": side,
        "signal_time_utc": signal["generated_at_utc"],
        "entry_time_utc": _iso_ms(int(first["open_time"])),
        "exit_time_utc": exit_time,
        "entry_price": format(entry, "f"),
        "exit_price": format(exit_price, "f"),
        "stop_price": format(stop, "f"),
        "target_price": format(target, "f"),
        "exit_reason": exit_reason,
        "bars_held": exit_bar_index - entry_index + 1,
        "gross_pnl_per_unit": format(gross, "f"),
        "fees_paid_per_unit": format(entry_fee + exit_fee, "f"),
        "net_pnl_per_unit": format(net, "f"),
        "r_multiple": format(r_multiple, ".8f"),
    }


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    rs = [float(_number(trade["r_multiple"], "R multiple")) for trade in trades]
    winners = [value for value in rs if value > 0]
    losers = [value for value in rs if value < 0]
    gross_wins, gross_losses = sum(winners), -sum(losers)
    equity = peak = 0.0
    max_drawdown = 0.0
    consecutive = max_consecutive = 0
    for value in rs:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        consecutive = consecutive + 1 if value < 0 else 0
        max_consecutive = max(max_consecutive, consecutive)
    count = len(rs)
    return {
        "trade_count": count,
        "win_rate_percent": round(100 * len(winners) / count, 4) if count else 0.0,
        "average_win_r": round(sum(winners) / len(winners), 8) if winners else 0.0,
        "average_loss_r": round(sum(losers) / len(losers), 8) if losers else 0.0,
        "expectancy_r": round(sum(rs) / count, 8) if count else 0.0,
        "profit_factor": round(gross_wins / gross_losses, 8) if gross_losses else (None if not gross_wins else "infinite"),
        "max_drawdown_r": round(max_drawdown, 8),
        "max_consecutive_losses": max_consecutive,
    }


def _group_summary(trades: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[str(trade.get(key) or "unknown")].append(trade)
    return {name: summarize(items) for name, items in sorted(groups.items())}


def run_backtest(
    archive_path: Path, *, fee_bps_per_side: Any, slippage_bps_per_side: Any,
    minimum_net_rr: Any, max_setup_age_hours: int = 168,
    out_of_sample_fraction: float = 0.3,
) -> dict[str, Any]:
    fee = _number(fee_bps_per_side, "fee_bps_per_side", nonnegative=True)
    slip = _number(slippage_bps_per_side, "slippage_bps_per_side", nonnegative=True)
    min_rr = _number(minimum_net_rr, "minimum_net_rr", positive=True)
    if not 0 < out_of_sample_fraction < 1:
        raise ValueError("out_of_sample_fraction must be between 0 and 1")
    if type(max_setup_age_hours) is not int or max_setup_age_hours <= 0:
        raise ValueError("max_setup_age_hours must be a positive integer")
    metadata, candles, _, raw_events = _load_archive(archive_path)
    symbol = str(metadata.get("symbol", "")).upper()
    if not symbol:
        raise ValueError("metadata.json is missing symbol")
    if metadata.get("sources", {}).get("spot") != binance_market_data.SPOT_BASE:
        raise ValueError("metadata.json does not verify the Binance Spot source")
    rows_1h = sorted(
        [row for row in candles if row.get("symbol") == symbol and row.get("interval") == "1h" and row.get("is_closed", "").lower() == "true"],
        key=lambda row: int(row["open_time"]),
    )
    if len(rows_1h) < 3:
        raise ValueError("At least three closed 1h candles are required")
    split_at = max(1, min(len(rows_1h) - 1, math.floor(len(rows_1h) * (1 - out_of_sample_fraction))))
    split_time = int(rows_1h[split_at]["open_time"])
    config = {
        "config_version": market_research.CONFIG_VERSION,
        "strategy_version": market_research.STRATEGY_VERSION,
        "minimum_net_rr": format(min_rr, "f"),
        "max_quote_age_seconds": 3600,
        "fee_bps_per_side": format(fee, "f"),
        "slippage_bps_per_side": format(slip, "f"),
        "max_setup_age_hours": max_setup_age_hours,
        "rules": market_research._rules(),
    }
    trades: list[dict[str, Any]] = []
    consumed_setups: set[str] = set()
    start_index = 0
    with tempfile.TemporaryDirectory(prefix="btc-market-backtest-") as temp:
        asof_path = Path(temp) / "asof.zip"
        for index, candle in enumerate(rows_1h[:-1]):
            asof = int(candle["close_time"]) + 1
            asof_rows = [row for row in candles if int(row["close_time"]) < asof]
            if not all(any(row.get("interval") == interval for row in asof_rows) for interval in INTERVALS):
                continue
            _asof_archive(archive_path, metadata, candles, raw_events, symbol, asof, candle["close"])
            signal = market_research.build_analysis(asof_path, config, now=datetime.fromtimestamp(asof / 1000, timezone.utc))
            scenario = signal.get("scenario")
            if not scenario or signal.get("execution_state") != "目前已觸發":
                continue
            setup_id = scenario.get("entry_event_id", "")
            if setup_id in consumed_setups:
                continue
            side = scenario["side"]
            zone = scenario["entry_zone"]
            zone_low = _number(zone["low"], "entry zone low", positive=True)
            zone_high = _number(zone["high"], "entry zone high", positive=True)
            stop = _number(scenario["stop_price"], "stop", positive=True)
            target = _number(scenario["target_price"], "target", positive=True)
            # Only the next candle may fill this one-candle historical order.
            next_bar = next((j for j in range(index + 1, len(rows_1h)) if int(rows_1h[j]["open_time"]) >= asof), None)
            if next_bar is None:
                continue
            trade = _simulate_trade(
                side=side, signal=signal, zone_low=zone_low, zone_high=zone_high,
                stop=stop, target=target, candles=rows_1h, entry_index=next_bar,
                fee_bps=fee, slippage_bps=slip,
            )
            if trade:
                trades.append(trade)
                consumed_setups.add(setup_id)
    trades.sort(key=lambda trade: (trade["entry_time_utc"], trade["setup_id"]))
    train = [trade for trade in trades if int(datetime.fromisoformat(trade["entry_time_utc"]).timestamp() * 1000) < split_time]
    test = [trade for trade in trades if int(datetime.fromisoformat(trade["entry_time_utc"]).timestamp() * 1000) >= split_time]
    return {
        "report_type": "historical_strategy_backtest",
        "report_version": "1",
        "symbol": symbol,
        "strategy_version": market_research.STRATEGY_VERSION,
        "input_archive": archive_path.name,
        "fees_and_slippage": {"fee_bps_per_side": format(fee, "f"), "slippage_bps_per_side": format(slip, "f")},
        "chronological_split": {"train_fraction": round(1 - out_of_sample_fraction, 6), "out_of_sample_fraction": round(out_of_sample_fraction, 6), "split_at_utc": _iso_ms(split_time)},
        "summary": summarize(trades),
        "train_summary": summarize(train),
        "out_of_sample_summary": summarize(test),
        "by_setup": _group_summary(trades, "setup_id"),
        "by_direction": _group_summary(trades, "direction"),
        "trades": trades,
        "execution_assumptions": [
            "Signals are evaluated only after fully closed candles; an order becomes eligible at the next 1h candle open.",
            "The strategy's conservative OB edge is used as the raw entry; adverse slippage and fees are applied to entry and exit.",
            "If stop and target are both touched in one candle, stop is filled first. Stop gaps fill at the worse opening price; target gaps fill at target.",
            "Orders remain eligible for the next candle only. A setup can produce at most one filled trade.",
            "Drawdown is measured on cumulative net R per trade, without compounding position size.",
        ],
        "limitations": [
            "Historical bars cannot reveal intrabar price order, queue position, liquidity, spread, funding, taxes, or market impact.",
            "The existing strategy blocks all candidates when the as-of data window has a reported gap.",
            "The strategy's current setup and target selection rules are applied unchanged; no rule tuning is performed.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="以歷史已收盤 Binance K 線回測既有 BTC Market Lab 策略（研究用途，不下單）。")
    parser.add_argument("archive", type=Path, help="market data ZIP generated by binance_market_data.py")
    parser.add_argument("--fee-bps-per-side", required=True, help="Fee assumption per fill in basis points")
    parser.add_argument("--slippage-bps-per-side", required=True, help="Adverse slippage assumption per fill in basis points")
    parser.add_argument("--minimum-net-rr", required=True, help="Existing strategy's user-defined minimum net reward/risk")
    parser.add_argument("--max-setup-age-hours", type=int, default=168, help="Maximum setup age, matching the existing strategy setting")
    parser.add_argument("--out-of-sample-fraction", type=float, default=0.3, help="Chronological final holdout fraction (default: 0.3)")
    parser.add_argument("--output", type=Path, help="Report path (default: <archive>_backtest.json)")
    args = parser.parse_args(argv)
    try:
        report = run_backtest(
            args.archive, fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
            minimum_net_rr=args.minimum_net_rr,
            max_setup_age_hours=args.max_setup_age_hours,
            out_of_sample_fraction=args.out_of_sample_fraction,
        )
        output = args.output or args.archive.with_name(f"{args.archive.stem}_backtest.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    print(f"回測報告已建立：{output}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
