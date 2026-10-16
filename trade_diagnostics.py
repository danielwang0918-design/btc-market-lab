#!/usr/bin/env python3
"""Per-trade post-mortem for the formal BTC multi-timeframe backtest."""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import historical_backtest as hb
import winrate_research as wr

INDICATOR_FIELDS = (
    "1h_ema_position", "1h_ema_slope",
    "4h_ema_position", "4h_ema_slope",
    "1d_ema_position", "1d_ema_slope",
    "rsi14", "atr14", "atr_pct", "relative_volume20",
)


def _epoch_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _direction_aligned(value: Any, direction: str) -> bool | None:
    if not _finite(value):
        return None
    return float(value) > 0 if direction == "long" else float(value) < 0


def _indicator_slice(feature: dict[str, Any] | None) -> dict[str, Any]:
    if not feature:
        return {key: None for key in INDICATOR_FIELDS}
    return {
        key: (float(feature[key]) if _finite(feature.get(key)) else None)
        for key in INDICATOR_FIELDS
    }


def _trade_excursion(
    rows: list[dict[str, Any]],
    entry_index: int | None,
    trade: dict[str, Any],
) -> dict[str, Any]:
    if entry_index is None:
        return {
            "mfe_r": None, "mae_r": None, "bars_with_both_stop_and_target_touched": None,
            "entry_bar_range_atr": None, "entry_bar_body_fraction": None,
            "entry_bar_close_location": None, "entry_bar_closed_against_direction": None,
        }
    entry = float(trade["raw_entry_price"])
    stop = float(trade["stop_price"])
    target = float(trade["target_price"])
    side = trade["direction"]
    risk = abs(entry - stop)
    exit_ms = _epoch_ms(trade["exit_time_utc"])
    exit_index = bisect.bisect_left([int(row["close_time"]) + 1 for row in rows], exit_ms)
    exit_index = min(max(exit_index, entry_index), len(rows) - 1)
    window = rows[entry_index:exit_index + 1]
    if not window or risk <= 0:
        return {
            "mfe_r": None, "mae_r": None, "bars_with_both_stop_and_target_touched": None,
            "entry_bar_range_atr": None, "entry_bar_body_fraction": None,
            "entry_bar_close_location": None, "entry_bar_closed_against_direction": None,
        }
    highs = [float(row["high"]) for row in window]
    lows = [float(row["low"]) for row in window]
    if side == "long":
        mfe = (max(highs) - entry) / risk
        mae = (entry - min(lows)) / risk
    else:
        mfe = (entry - min(lows)) / risk
        mae = (max(highs) - entry) / risk

    both = 0
    for row in window:
        high, low = float(row["high"]), float(row["low"])
        if side == "long":
            stop_hit, target_hit = low <= stop, high >= target
        else:
            stop_hit, target_hit = high >= stop, low <= target
        if stop_hit and target_hit:
            both += 1

    entry_bar = rows[entry_index]
    op, high, low, close = (
        float(entry_bar[key]) for key in ("open", "high", "low", "close")
    )
    candle_range = high - low
    entry_bar_body_fraction = abs(close - op) / candle_range if candle_range > 0 else None
    close_location = (close - low) / candle_range if candle_range > 0 else None
    if candle_range <= 0:
        adverse_close = None
    else:
        adverse_close = close < op if side == "long" else close > op
    return {
        "mfe_r": round(mfe, 5),
        "mae_r": round(mae, 5),
        "bars_with_both_stop_and_target_touched": both,
        "entry_bar_range_atr": None,
        "entry_bar_body_fraction": round(entry_bar_body_fraction, 5) if entry_bar_body_fraction is not None else None,
        "entry_bar_close_location": round(close_location, 5) if close_location is not None else None,
        "entry_bar_closed_against_direction": adverse_close,
    }


def _primary_label(trade: dict[str, Any], flags: dict[str, bool]) -> str:
    if trade["exit_reason"] == "target":
        return "target_hit"
    if trade["exit_reason"] in {"data_gap_exit", "integrity_review_exit", "split_or_segment_end"}:
        return trade["exit_reason"]
    if trade["exit_reason"] in {"stop", "stop_gap"} and trade["bars_held"] <= 2:
        return "rapid_stop_within_2_bars"
    if flags.get("same_bar_stop_target_ambiguity", False):
        return "stop_target_both_touched_same_bar"
    if flags.get("directional_rsi_exhaustion_at_entry", False):
        return "directional_rsi_exhaustion_at_entry"
    if flags.get("higher_timeframe_conflict_at_entry", False):
        return "higher_timeframe_ema_conflict"
    if flags.get("high_atr_at_entry", False):
        return "high_volatility_at_entry"
    if flags.get("entry_bar_closed_against_direction", False):
        return "entry_bar_closed_against_position"
    return "other_stop_or_loss"


def diagnose(
    symbol: str,
    data_dir: Path,
    input_report: Path | None = None,
    output_json: Path | None = None,
    output_csv: Path | None = None,
) -> dict[str, Any]:
    hourly = hb._closed_rows(data_dir / f"{symbol}_1h.csv", symbol, "1h")
    rows_by_interval = {
        interval: hb._closed_rows(data_dir / f"{symbol}_{interval}.csv", symbol, interval)
        for interval in ("1h", "4h", "1d")
    }
    report_path = input_report or data_dir / f"{symbol}_mtf_backtest.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    trades = report.get("trades", [])
    if not trades:
        raise ValueError(f"No trade records found in {report_path}")

    features = wr.build_feature_rows(rows_by_interval, hourly)
    asof_times = [int(item["asof_ms"]) for item in features]
    open_times = [int(row["open_time"]) for row in hourly]
    closes_plus_one = [int(row["close_time"]) + 1 for row in hourly]
    atr_cutoff = wr._training_percentile(
        [
            float(feature["atr_pct"])
            for feature in features[:int(len(features) * 0.6)]
            if _finite(feature.get("atr_pct"))
        ],
        0.75,
    )

    diagnostic_rows: list[dict[str, Any]] = []
    for trade in trades:
        signal_ms = _epoch_ms(trade["signal_time_utc"])
        entry_ms = _epoch_ms(trade["entry_time_utc"])
        signal_index = bisect.bisect_left(asof_times, signal_ms)
        if signal_index >= len(asof_times) or asof_times[signal_index] != signal_ms:
            signal_index = bisect.bisect_right(asof_times, signal_ms) - 1
        signal_feature = features[signal_index] if signal_index >= 0 else None

        entry_index = bisect.bisect_left(open_times, entry_ms)
        if entry_index >= len(open_times) or open_times[entry_index] != entry_ms:
            entry_index = None
        entry_feature_index = bisect.bisect_right(asof_times, entry_ms) - 1
        entry_feature = features[entry_feature_index] if entry_feature_index >= 0 else None
        feature_age_minutes = (
            (entry_ms - asof_times[entry_feature_index]) / 60000
            if entry_feature_index >= 0 else None
        )

        direction = trade["direction"]
        signal = _indicator_slice(signal_feature)
        entry = _indicator_slice(entry_feature)
        entry_4h = _direction_aligned(entry.get("4h_ema_position"), direction)
        entry_1d = _direction_aligned(entry.get("1d_ema_position"), direction)
        rsi = entry.get("rsi14")
        rsi_exhaustion = (
            (direction == "long" and rsi is not None and rsi >= 65)
            or (direction == "short" and rsi is not None and rsi <= 35)
        )
        entry_atr_high = (
            entry.get("atr_pct") is not None and atr_cutoff is not None
            and entry["atr_pct"] > atr_cutoff
        )
        excursion = _trade_excursion(hourly, entry_index, trade)
        flags = {
            "rapid_stop_within_2_bars": trade["exit_reason"] in {"stop", "stop_gap"} and int(trade["bars_held"]) <= 2,
            "directional_rsi_exhaustion_at_signal": (
                (direction == "long" and signal.get("rsi14") is not None and signal["rsi14"] >= 65)
                or (direction == "short" and signal.get("rsi14") is not None and signal["rsi14"] <= 35)
            ),
            "directional_rsi_exhaustion_at_entry": bool(rsi_exhaustion),
            "higher_timeframe_conflict_at_entry": entry_4h is False or entry_1d is False,
            "higher_timeframes_both_aligned_at_entry": entry_4h is True and entry_1d is True,
            "high_atr_at_entry": bool(entry_atr_high),
            "stale_signal_24h_or_more": (entry_ms - signal_ms) >= 24 * 3_600_000,
            "same_bar_stop_target_ambiguity": (
                excursion.get("bars_with_both_stop_and_target_touched") is not None
                and excursion["bars_with_both_stop_and_target_touched"] > 0
                and trade["exit_reason"] in {"stop", "stop_gap"}
            ),
            "entry_bar_closed_against_direction": excursion.get("entry_bar_closed_against_direction") is True,
        }
        flags["entry_features_older_than_4h"] = feature_age_minutes is not None and feature_age_minutes > 240
        flags["profitable_trade"] = float(trade["r_multiple"]) > 0
        flags["losing_trade"] = float(trade["r_multiple"]) <= 0
        primary = _primary_label(trade, flags)
        diagnostic_rows.append({
            **trade,
            "signal_features": signal,
            "entry_time_features_last_closed_candle": entry,
            "entry_feature_age_minutes": feature_age_minutes,
            "training_75th_percentile_atr_pct": atr_cutoff,
            "diagnostic_flags": flags,
            "primary_diagnostic_label": primary,
            **excursion,
        })

    summaries: dict[str, Any] = {}
    all_splits = sorted({str(trade.get("split", "all")) for trade in diagnostic_rows})
    for split in all_splits:
        subset = [row for row in diagnostic_rows if str(row.get("split", "all")) == split]
        wins = [row for row in subset if row["flags_for_summary"] if False]
        loss_rows = [row for row in subset if row["diagnostic_flags"]["losing_trade"]]
        flag_summary = {}
        flag_names = [
            "rapid_stop_within_2_bars",
            "directional_rsi_exhaustion_at_signal",
            "directional_rsi_exhaustion_at_entry",
            "higher_timeframe_conflict_at_entry",
            "higher_timeframes_both_aligned_at_entry",
            "high_atr_at_entry",
            "stale_signal_24h_or_more",
            "same_bar_stop_target_ambiguity",
            "entry_bar_closed_against_direction",
            "entry_features_older_than_4h",
        ]
        for flag in flag_names:
            flagged = [row for row in subset if row["diagnostic_flags"][flag]]
            flagged_losses = [row for row in loss_rows if row["diagnostic_flags"][flag]]
            flag_summary[flag] = {
                "flagged_trade_count": len(flagged),
                "flagged_loss_count": len(flagged_losses),
                "percent_of_losses_with_flag": round(100 * len(flagged_losses) / len(loss_rows), 2) if loss_rows else 0.0,
                "flagged_win_rate_percent": round(
                    100 * sum(float(row["r_multiple"]) > 0 for row in flagged) / len(flagged), 2
                ) if flagged else None,
                "flagged_mean_r": round(
                    sum(float(row["r_multiple"]) for row in flagged) / len(flagged), 5
                ) if flagged else None,
            }
        summaries[split] = {
            "trade_count": len(subset),
            "win_count": sum(float(row["r_multiple"]) > 0 for row in subset),
            "loss_count": sum(float(row["r_multiple"]) <= 0 for row in subset),
            "win_rate_percent": round(
                100 * sum(float(row["r_multiple"]) > 0 for row in subset) / len(subset), 2
            ) if subset else 0.0,
            "expectancy_r": round(
                sum(float(row["r_multiple"]) for row in subset) / len(subset), 5
            ) if subset else 0.0,
            "exit_reasons": dict(Counter(row["exit_reason"] for row in subset)),
            "primary_diagnostic_labels": dict(Counter(row["primary_diagnostic_label"] for row in subset)),
            "failure_flags": flag_summary,
        }

    result = {
        "report_type": "btc_formal_strategy_trade_diagnostics",
        "report_version": 1,
        "strategy": report.get("data", {}).get("strategy", "mtf-bos-ob-retest-v1"),
        "source_backtest_report": str(report_path),
        "method": {
            "indicator_timing": "Signal features use candles closed by signal timestamp. Entry features use the last candle fully closed at or before entry open; no unfinished entry candle is used for entry filters.",
            "atr_threshold": "75th percentile of ATR/price fitted on first 60% of chronological hourly feature rows only.",
            "primary_label_is_heuristic": True,
            "warning": "Flags describe associations and path symptoms, not proven causal explanations. OHLC candles cannot reveal exact intrabar event order.",
        },
        "training_75th_percentile_atr_pct": atr_cutoff,
        "split_summaries": summaries,
        "trades": diagnostic_rows,
    }
    out_json = output_json or data_dir / f"{symbol}_trade_diagnostics.json"
    out_csv = output_csv or data_dir / f"{symbol}_trade_diagnostics.csv"
    hb._atomic_json(out_json, result)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    flat_rows = []
    for row in diagnostic_rows:
        flat = {key: value for key, value in row.items()
                if key not in {"signal_features", "entry_time_features_last_closed_candle", "diagnostic_flags"}}
        for prefix in ("signal_features", "entry_time_features_last_closed_candle"):
            for key, value in row[prefix].items():
                flat[f"{prefix}_{key}"] = value
        for key, value in row["diagnostic_flags"].items():
            flat[f"flag_{key}"] = value
        flat_rows.append(flat)
    fields = list(dict.fromkeys(key for row in flat_rows for key in row))
    with out_csv.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(flat_rows)

    print(json.dumps({
        "output_json": str(out_json),
        "output_csv": str(out_csv),
        "strategy": result["strategy"],
        "training_75th_percentile_atr_pct": atr_cutoff,
        "split_summaries": summaries,
        "trade_records": len(diagnostic_rows),
    }, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose every trade from the formal BTC MTF backtest.")
    parser.add_argument("symbol", nargs="?", default="BTCUSDT")
    parser.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--input-report", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-csv", type=Path)
    args = parser.parse_args()
    diagnose(
        args.symbol, args.data_dir,
        input_report=args.input_report,
        output_json=args.output_json,
        output_csv=args.output_csv,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
