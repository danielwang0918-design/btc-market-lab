#!/usr/bin/env python3
"""Compare wider stop-loss rules on the frozen BTC multi-timeframe strategy."""
from __future__ import annotations

import argparse
import bisect
import math
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import historical_backtest as hb
import winrate_research as wr
from market_structure import INTERVAL_MILLISECONDS

VARIANTS = {
    "ob_1h_baseline": "Original 1H OB-edge stop.",
    "atr_4h_buffer": "Original stop widened by 0.25 x closed 4H ATR(14).",
    "swing_4h_buffer": "Beyond the latest confirmed 4H swing low/high, plus/minus 0.25 x 4H ATR(14).",
}
DEFAULT_ATR_MULTIPLE = 0.25


def _atr_series(rows: list[dict[str, Any]], period: int) -> list[float | None]:
    values: list[float | None] = [None] * len(rows)
    if len(rows) < period:
        return values
    true_ranges: list[float] = []
    previous_close: float | None = None
    for row in rows:
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        true_ranges.append(
            high - low if previous_close is None
            else max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
        previous_close = close
    current = sum(true_ranges[:period]) / period
    values[period - 1] = current
    for i in range(period, len(rows)):
        current = (current * (period - 1) + true_ranges[i]) / period
        values[i] = current
    return values


def build_4h_contexts(
    rows_4h: list[dict[str, Any]], atr_period: int = 14
) -> tuple[list[int], list[dict[str, Any]]]:
    """ATR and swing levels at each 4H close; swing levels only appear on confirmation."""
    if not rows_4h:
        raise ValueError("4H 歷史 K 線不可為空。")
    if atr_period < 1:
        raise ValueError("ATR 週期必須是正整數。")
    close_after_ms = [int(row["close_time"]) + 1 for row in rows_4h]
    contexts = [{"atr": None, "swing_low": None, "swing_high": None} for _ in rows_4h]
    step = INTERVAL_MILLISECONDS["4h"]
    start = 0
    while start < len(rows_4h):
        end = start + 1
        while end < len(rows_4h) and int(rows_4h[end]["open_time"]) - int(rows_4h[end - 1]["open_time"]) == step:
            end += 1
        segment = rows_4h[start:end]
        atrs = _atr_series(segment, atr_period)
        close_to_i = {int(row["close_time"]): i for i, row in enumerate(segment)}
        events_at_i: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for event in hb._strategy_events(segment, "4h"):
            i = close_to_i.get(int(event["confirmed_at_close_time"]))
            if i is not None and event["event_type"] in {"swing_low", "swing_high"}:
                events_at_i[i].append(event)
        swing_low = swing_high = None
        for i in range(len(segment)):
            for event in events_at_i.get(i, []):
                if event["event_type"] == "swing_low":
                    swing_low = float(event["price_low"])
                else:
                    swing_high = float(event["price_high"])
            contexts[start + i] = {
                "atr": atrs[i], "swing_low": swing_low, "swing_high": swing_high
            }
        start = end
    return close_after_ms, contexts


def transform_candidate(
    candidate: dict[str, Any],
    context: dict[str, Any],
    variant: str,
    *,
    atr_multiple: float = DEFAULT_ATR_MULTIPLE,
    fee_bps: Decimal = Decimal("10"),
    slippage_bps: Decimal = Decimal("5"),
) -> tuple[dict[str, Any], str | None]:
    result = dict(candidate)
    if variant == "ob_1h_baseline":
        return result, None
    if variant not in VARIANTS:
        raise ValueError(f"未知停損方案：{variant}")
    try:
        entry = Decimal(str(result["entry_reference"]))
        original_stop = Decimal(str(result["stop"]))
        target = Decimal(str(result["target"]))
        side = str(result["side"])
    except (KeyError, ValueError, TypeError):
        return result, "invalid_candidate"

    atr = context.get("atr")
    if not isinstance(atr, (int, float)) or not math.isfinite(float(atr)) or atr <= 0:
        result["net_rr"] = "-999"
        return result, "missing_4h_atr"
    buffer = Decimal(str(float(atr) * atr_multiple))
    if variant == "atr_4h_buffer":
        stop = original_stop - buffer if side == "long" else original_stop + buffer
    else:
        swing = context.get("swing_low" if side == "long" else "swing_high")
        if not isinstance(swing, (int, float)) or not math.isfinite(float(swing)):
            result["net_rr"] = "-999"
            return result, "missing_confirmed_4h_swing"
        structural = Decimal(str(swing)) - buffer if side == "long" else Decimal(str(swing)) + buffer
        stop = min(original_stop, structural) if side == "long" else max(original_stop, structural)

    if stop <= 0 or (side == "long" and stop >= entry) or (side == "short" and stop <= entry):
        result["net_rr"] = "-999"
        return result, "invalid_stop_geometry"
    result["stop"] = format(stop, "f")
    result["_original_stop"] = format(original_stop, "f")
    result["_atr4h_at_signal"] = float(atr)
    result["_stop_variant"] = variant
    try:
        result["net_rr"] = format(wr._target_net_rr(result, target, fee_bps, slippage_bps), "f")
    except (ArithmeticError, ValueError, KeyError):
        result["net_rr"] = "-999"
        return result, "invalid_net_rr"
    return result, None


def transform_snapshots(
    snapshots: list[dict[str, Any]],
    close_after_ms: list[int],
    contexts: list[dict[str, Any]],
    variant: str,
    *,
    atr_multiple: float,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    minimum_net_rr: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    output = []
    counts: dict[str, int] = {"candidate_snapshots": 0, "transformed": 0, "below_minimum_net_rr": 0}
    for snapshot in snapshots:
        item = dict(snapshot)
        candidate = snapshot.get("candidate")
        if candidate:
            counts["candidate_snapshots"] += 1
            if variant == "ob_1h_baseline":
                item["candidate"] = dict(candidate)
            else:
                asof = int(candidate["signal_time_ms"])
                i = bisect.bisect_right(close_after_ms, asof) - 1
                context = contexts[i] if 0 <= i < len(contexts) else {}
                updated, reason = transform_candidate(
                    candidate, context, variant, atr_multiple=atr_multiple,
                    fee_bps=fee_bps, slippage_bps=slippage_bps,
                )
                item["candidate"] = updated
                if reason:
                    counts[reason] = counts.get(reason, 0) + 1
                else:
                    counts["transformed"] += 1
                if float(updated.get("net_rr", -999)) < minimum_net_rr:
                    counts["below_minimum_net_rr"] += 1
        output.append(item)
    return output, counts


def trade_diagnostics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    stops = [t for t in trades if t["exit_reason"] in {"stop", "stop_gap"}]
    rapid = [t for t in stops if int(t["bars_held"]) <= 2]
    distances = []
    for trade in trades:
        entry = float(trade["raw_entry_price"])
        if entry > 0:
            distances.append(abs(entry - float(trade["stop_price"])) / entry * 100)
    return {
        "trade_count": len(trades),
        "stop_trade_count": len(stops),
        "rapid_stops_within_2_bars": len(rapid),
        "rapid_stops_percent_of_stopped_trades": round(100 * len(rapid) / len(stops), 4) if stops else 0.0,
        "average_initial_stop_distance_percent": round(sum(distances) / len(distances), 4) if distances else 0.0,
    }


def run_research(
    symbol: str,
    data_dir: Path,
    *,
    fee_bps_per_side: float = 10.0,
    slippage_bps_per_side: float = 5.0,
    minimum_net_rr: float = 1.5,
    max_setup_age_hours: int = 168,
    atr_multiple: float = DEFAULT_ATR_MULTIPLE,
    output_path: Path | None = None,
) -> dict[str, Any]:
    if not math.isfinite(atr_multiple) or atr_multiple < 0:
        raise ValueError("ATR 緩衝倍數必須是非負有限數值。")
    rows, snapshots, data_summary = hb._prepare_snapshots(
        data_dir, symbol, fee_bps_per_side=fee_bps_per_side,
        slippage_bps_per_side=slippage_bps_per_side,
        minimum_net_rr=minimum_net_rr, max_setup_age_hours=max_setup_age_hours,
    )
    rows_4h = hb._closed_rows(data_dir / f"{symbol}_4h.csv", symbol, "4h")
    close_after_ms, contexts = build_4h_contexts(rows_4h)
    fee, slip = Decimal(str(fee_bps_per_side)), Decimal(str(slippage_bps_per_side))
    variants: dict[str, Any] = {}
    split_ranges = hb._split_ranges(rows, 0.6, 0.2)

    for name, description in VARIANTS.items():
        if name == "ob_1h_baseline":
            variant_snapshots = snapshots
            counts = {"candidate_snapshots": sum(bool(s.get("candidate")) for s in snapshots), "transformed": 0}
        else:
            variant_snapshots, counts = transform_snapshots(
                snapshots, close_after_ms, contexts, name, atr_multiple=atr_multiple,
                fee_bps=fee, slippage_bps=slip, minimum_net_rr=minimum_net_rr,
            )
        backtest = hb.run_configuration(
            rows, variant_snapshots, minimum_net_rr=minimum_net_rr,
            max_setup_age_hours=max_setup_age_hours,
            fee_bps_per_side=fee_bps_per_side, slippage_bps_per_side=slippage_bps_per_side,
            include_test=True,
        )
        split_details = {}
        for split in ("train", "validation", "test"):
            trades = [t for t in backtest["trades"] if t["split"] == split]
            split_details[split] = {
                "start_utc": backtest["splits"][split]["start_utc"],
                "end_exclusive_utc": backtest["splits"][split]["end_exclusive_utc"],
                "metrics": backtest["splits"][split]["metrics"],
                "stop_diagnostics": trade_diagnostics(trades),
            }
        variants[name] = {
            "description": description, "snapshot_counts": counts,
            "splits": split_details, "trades": backtest["trades"],
        }
        if name != "ob_1h_baseline":
            del variant_snapshots

    report = {
        "report_type": "btc_mtf_stop_loss_comparison",
        "report_version": 1,
        "strategy": "mtf-bos-ob-retest-v1",
        "data": data_summary,
        "parameters": {
            "fee_bps_per_side": fee_bps_per_side,
            "slippage_bps_per_side": slippage_bps_per_side,
            "minimum_net_rr_after_costs": minimum_net_rr,
            "maximum_setup_age_hours": max_setup_age_hours,
            "atr_period_4h": 14,
            "atr_buffer_multiple": atr_multiple,
            "split_fractions": {"train": 0.6, "validation": 0.2, "test": 0.2},
        },
        "variants": variants,
        "limitations": [
            "Only stop placement changes; entry and target rules otherwise remain fixed.",
            "Wider stops must still satisfy the same minimum net RR after transaction costs, so some setups become ineligible.",
            "Only closed 4H candles and confirmed 4H swing points are used.",
            "A candle touching both stop and target resolves to stop first under the existing conservative simulator.",
            "Historical simulations do not guarantee future performance.",
        ],
    }
    if output_path:
        hb._atomic_json(output_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare current 1H OB stops with wider 4H ATR/structure stops.")
    parser.add_argument("symbol", nargs="?", default="BTCUSDT")
    parser.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--fee-bps-per-side", type=float, default=10.0)
    parser.add_argument("--slippage-bps-per-side", type=float, default=5.0)
    parser.add_argument("--minimum-net-rr", type=float, default=1.5)
    parser.add_argument("--max-setup-age-hours", type=int, default=168)
    parser.add_argument("--atr-multiple", type=float, default=DEFAULT_ATR_MULTIPLE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        symbol = hb.validate_symbol(args.symbol)
        output = args.output or args.data_dir / f"{symbol}_stop_loss_research.json"
        report = run_research(
            symbol, args.data_dir, fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
            minimum_net_rr=args.minimum_net_rr, max_setup_age_hours=args.max_setup_age_hours,
            atr_multiple=args.atr_multiple, output_path=output,
        )
        print(f"停損比較報告：{output}")
        print(f"1H 資料：{report['data']['intervals']['1h']['start_utc']} 至 {report['data']['intervals']['1h']['end_utc']}")
        for name, variant in report["variants"].items():
            print(f"\n[{name}] {variant['description']}")
            for split in ("train", "validation", "test"):
                m = variant["splits"][split]["metrics"]
                d = variant["splits"][split]["stop_diagnostics"]
                print(
                    f"{split}: trades={m['trade_count']}, win_rate={m['win_rate_percent']}%, "
                    f"expectancy={m['expectancy_r']}R, PF={m['profit_factor']}, "
                    f"maxDD={m['max_drawdown_r']}R, rapid_stops={d['rapid_stops_within_2_bars']}/{d['stop_trade_count']}"
                )
        return 0
    except (ValueError, OSError, ArithmeticError) as exc:
        parser.exit(2, f"錯誤：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
