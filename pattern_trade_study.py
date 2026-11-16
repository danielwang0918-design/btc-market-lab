#!/usr/bin/env python3
"""As-of pattern features and boundary-safe MFE audit. Never changes trading rules."""
from __future__ import annotations

import argparse
import bisect
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from collections import defaultdict
from typing import Any, Iterable

from pattern_research import INTERVAL_MS, OUTPUT_COLUMNS, VERSION as PATTERN_VERSION, analyze_patterns

STUDY_VERSION = "pattern-trade-observation-v1"
DEFAULT_LOOKBACK_HOURS = 168  # A predefined observation window; not selected on Test.
TIMEFRAMES = ("1h", "4h", "1d")
FAMILIES = ("elliott", "harmonic")


def _ms(value: str) -> int:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Trade timestamp must include a timezone")
    return int(parsed.timestamp() * 1000)


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _numeric(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric {label}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Non-finite {label}")
    return result


def _mfe_interior(trade: dict[str, Any], hourly: list[dict[str, Any]],
                  open_times: list[int]) -> dict[str, Any]:
    """Conservative independently observed MFE, excluding both boundary candles.

    Neither the entry-bar pre-fill path nor exit-bar post-fill path is observable
    from 1h OHLC. This is a *lower bound*, not the actual intratrade MFE.
    """
    entry_ms, exit_ms = _ms(trade["entry_time_utc"]), _ms(trade["exit_time_utc"])
    entry_idx = bisect.bisect_left(open_times, entry_ms)
    exit_idx = bisect.bisect_left(open_times, exit_ms)
    if entry_ms > exit_ms:
        raise ValueError("Exit precedes entry")
    if (entry_idx >= len(hourly) or open_times[entry_idx] != entry_ms
            or exit_idx >= len(hourly) or open_times[exit_idx] != exit_ms):
        raise ValueError("Entry/exit hourly candle missing or not aligned")
    span = hourly[entry_idx:exit_idx + 1]
    if any(int(b["open_time"]) != entry_ms + k * INTERVAL_MS["1h"] for k, b in enumerate(span)):
        raise ValueError("Candle gap within the holding period")
    if len(span) != 1 + (exit_ms - entry_ms) // INTERVAL_MS["1h"]:
        raise ValueError("Candle gap within holding period")
    interior = hourly[entry_idx + 1:exit_idx]  # complete, entirely held bars
    raw_entry = _numeric(trade["raw_entry_price"], "raw_entry_price")
    risk = abs(raw_entry - _numeric(trade["stop_price"], "stop_price"))
    if risk <= 0 or raw_entry <= 0:
        raise ValueError("Invalid positive trade risk or raw entry")
    if trade["direction"] == "long":
        favorable = max((float(b["high"]) - raw_entry for b in interior), default=0.0)
    elif trade["direction"] == "short":
        favorable = max((raw_entry - float(b["low"]) for b in interior), default=0.0)
    else:
        raise ValueError("Unsupported trade direction")
    lower_bound = max(0.0, favorable / risk)
    reported = _numeric(trade["mfe_r"], "mfe_r")
    if reported + 1e-5 < lower_bound:
        raise ValueError("Reported full-candle MFE smaller than interior-only MFE")
    return {
        "interior_candle_count": len(interior),
        "mfe_r_reported_full_boundary_candles": round(reported, 6),
        "mfe_r_interior_lower_bound": round(lower_bound, 6),
        "mfe_ge_0_5r_confirmed_interior": lower_bound >= 0.5,
        "mfe_ge_1r_confirmed_interior": lower_bound >= 1.0,
        "mfe_ge_1r_reported_full": reported >= 1.0,
        "mfe_1r_boundary_ambiguous": reported >= 1 and lower_bound < 1,
    }


def _latest_patterns(events: list[dict[str, Any]], asof_ms: int,
                     max_age_ms: int) -> dict[str, Any]:
    """Last *confirmed* candidate by timeframe/family, at decision time."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in events:
        by_key[(item["interval"], item["family"])].append(item)
    output: dict[str, Any] = {}
    for interval in TIMEFRAMES:
        for family in FAMILIES:
            key = f"{interval}_{family}"
            group = sorted(by_key.get((interval, family), []),
                           key=lambda e: (int(e["usable_from_open_time"]), e["pattern_id"]))
            positions = [int(e["usable_from_open_time"]) for e in group]
            idx = bisect.bisect_right(positions, asof_ms) - 1
            if idx < 0:
                output[key] = None
                continue
            selected = group[idx]
            age_ms = asof_ms - int(selected["usable_from_open_time"])
            if age_ms < 0:
                raise AssertionError("Future event selected")
            output[key] = {
                "pattern_id": selected["pattern_id"],
                "pattern": selected["pattern"],
                "direction": selected["direction"],
                "confirmed_at_close_time": int(selected["confirmed_at_close_time"]),
                "usable_from_open_time": int(selected["usable_from_open_time"]),
                "age_hours": round(age_ms / 3_600_000, 4),
                "recent": age_ms <= max_age_ms,
            }
    return output


def _prepare_patterns(data_dir: Path, output_dir: Path,
                      left: int, right: int, tolerance: float) -> tuple[list[dict[str, Any]], dict[str, str]]:
    all_patterns: list[dict[str, Any]] = []
    fingerprints: dict[str, str] = {}
    for interval in TIMEFRAMES:
        source = data_dir / f"BTCUSDT_{interval}.csv"
        fingerprints[source.name] = _fingerprint(source)
        events = analyze_patterns(_csv(source), left_bars=left, right_bars=right,
                                  ratio_tolerance=tolerance)
        path = output_dir / f"BTCUSDT_{interval}_pattern_candidates.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=OUTPUT_COLUMNS)
            writer.writeheader()
            writer.writerows(events)
        all_patterns.extend(events)
    return all_patterns, fingerprints


def _performance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda t: (_ms(t["exit_time_utc"]), t["setup_id"]))
    gains = [float(t["r_multiple"]) for t in ordered]
    wins = sum(value > 0 for value in gains)
    gross_profit = sum(value for value in gains if value > 0)
    gross_loss = sum(value for value in gains if value < 0)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in gains:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": len(gains), "wins": wins,
        "win_rate_percent": round(wins / len(gains) * 100, 4) if gains else None,
        "expectancy_r": round(sum(gains) / len(gains), 6) if gains else None,
        "profit_factor": round(gross_profit / abs(gross_loss), 6) if gross_loss < 0 else None,
        "maximum_closed_trade_drawdown_r": round(max_dd, 6) if gains else None,
    }


def build_study(data_dir: Path, diagnostics_path: Path,
                output_dir: Path, *, max_age_hours: int = DEFAULT_LOOKBACK_HOURS,
                left_bars: int = 2, right_bars: int = 2,
                ratio_tolerance: float = 0.04) -> dict[str, Any]:
    if not isinstance(max_age_hours, int) or isinstance(max_age_hours, bool) or max_age_hours <= 0:
        raise ValueError("max_age_hours must be positive integer")
    if not isinstance(left_bars, int) or not isinstance(right_bars, int) or min(left_bars, right_bars) < 1:
        raise ValueError("left/right bars must be positive integers")
    if not isinstance(ratio_tolerance, (int, float)) or not 0 <= ratio_tolerance <= 0.1:
        raise ValueError("Invalid ratio_tolerance")
    output_dir.mkdir(parents=True, exist_ok=True)
    report = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    if report.get("strategy") != "mtf-bos-ob-retest-v1":
        raise ValueError("Unexpected formal baseline strategy")
    patterns, fingerprints = _prepare_patterns(data_dir, output_dir, left_bars, right_bars, ratio_tolerance)
    fingerprints[diagnostics_path.name] = _fingerprint(diagnostics_path)
    hourly = _csv(data_dir / "BTCUSDT_1h.csv")
    open_times = [int(r["open_time"]) for r in hourly]
    age_ms = max_age_hours * 3_600_000
    features: list[dict[str, Any]] = []
    grouped: dict[str, Any] = {}
    for variant_name, variant in report["variants"].items():
        variant_rows = []
        for trade in variant["trades"]:
            signal_ms = _ms(trade["signal_time_utc"])
            entry_ms = _ms(trade["entry_time_utc"])
            if signal_ms > entry_ms:
                raise ValueError("Signal occurs after entry")
            history = _latest_patterns(patterns, signal_ms, age_ms)
            interior = _mfe_interior(trade, hourly, open_times)
            item = {"variant": variant_name, **trade, **interior,
                    "decision_time_ms": signal_ms, "patterns_asof": history}
            variant_rows.append(item)
            flat = {"variant": variant_name, "setup_id": trade["setup_id"],
                    "signal_time_utc": trade["signal_time_utc"],
                    "entry_time_utc": trade["entry_time_utc"],
                    "exit_time_utc": trade["exit_time_utc"],
                    "split": trade["split"], "direction": trade["direction"],
                    "exit_reason": trade["exit_reason"], "bars_held": trade["bars_held"],
                    "r_multiple": trade["r_multiple"], **interior}
            for interval in TIMEFRAMES:
                for family in FAMILIES:
                    key = f"{interval}_{family}"
                    most_recent = history[key]
                    recent = most_recent if most_recent and most_recent["recent"] else None
                    flat[f"{key}_recent"] = bool(recent)
                    flat[f"{key}_pattern"] = recent["pattern"] if recent else ""
                    flat[f"{key}_direction"] = recent["direction"] if recent else ""
                    flat[f"{key}_alignment"] = ("no_recent_pattern" if not recent else
                        "aligned" if (recent["direction"] == "bullish") == (trade["direction"] == "long") else "opposed")
                    flat[f"{key}_age_hours"] = most_recent["age_hours"] if most_recent else ""
                    flat[f"{key}_confirmed_at_close_time"] = most_recent["confirmed_at_close_time"] if most_recent else ""
            features.append(flat)
        by_split = {}
        for split in ("train", "validation", "test"):
            subset = [item for item in variant_rows if item["split"] == split]
            losses = [item for item in subset if float(item["r_multiple"]) <= 0]
            case = {"performance": _performance(subset), "mfe_boundary_audit": {
                "losing_trades": len(losses),
                "reported_losers_ge_0_5r": sum(float(t["mfe_r"]) >= .5 for t in losses),
                "interior_confirmed_losers_ge_0_5r": sum(t["mfe_ge_0_5r_confirmed_interior"] for t in losses),
                "reported_losers_ge_1r": sum(t["mfe_ge_1r_reported_full"] for t in losses),
                "interior_confirmed_losers_ge_1r": sum(t["mfe_ge_1r_confirmed_interior"] for t in losses),
                "boundary_ambiguous_losers_ge_1r": sum(t["mfe_1r_boundary_ambiguous"] for t in losses),
                "rapid_stops_le_2_bars": sum(t["exit_reason"] in {"stop", "stop_gap"} and int(t["bars_held"]) <= 2 for t in subset),
            }, "pattern_strata": {}}
            for interval in TIMEFRAMES:
                for family in FAMILIES:
                    key = f"{interval}_{family}"
                    by_alignment = {}
                    for alignment in ("aligned", "opposed", "no_recent_pattern"):
                        selected = [item for item in subset if (
                            "no_recent_pattern" if not item["patterns_asof"][key] or not item["patterns_asof"][key]["recent"]
                            else "aligned" if (item["patterns_asof"][key]["direction"] == "bullish") == (item["direction"] == "long")
                            else "opposed") == alignment]
                        by_alignment[alignment] = _performance(selected)
                    case["pattern_strata"][key] = by_alignment
            by_split[split] = case
        grouped[variant_name] = by_split
    csv_path = output_dir / "BTCUSDT_pattern_trade_features.csv"
    if features:
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            keys = list(features[0])
            writer = csv.DictWriter(stream, fieldnames=keys)
            writer.writeheader()
            writer.writerows(features)
    event_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event in patterns:
        event_counts[event["interval"]][event["pattern"]] += 1
    summary = {
        "study_version": STUDY_VERSION, "strategy": "mtf-bos-ob-retest-v1",
        "pattern_detector_version": PATTERN_VERSION,
        "original_diagnostics_version": report.get("report_version"),
        "parameters": {"max_pattern_age_hours": max_age_hours, "left_bars": left_bars,
                       "right_bars": right_bars, "ratio_tolerance": ratio_tolerance},
        "provenance_sha256": fingerprints,
        "event_counts": {k: dict(v) for k, v in event_counts.items()},
        "variants": grouped,
        "critical_warnings": [
            "Observational only; no change to formal entry/exit or sizing.",
            "Pattern features are evaluated as-of the signal decision time, NOT after entry.",
            "Full-boundary-candle MFE can include pre-fill and post-exit prices; interior lower bound avoids those bars but may undercount actual MFE.",
            "Overlapping labels and tiny subgroups cannot demonstrate an edge; no Test-based parameter selection.",
            "Profit factor and closed-trade drawdown are descriptive, do not assume capital allocation, and are not directly comparable to independent strategy portfolio equity.",
        ],
    }
    (output_dir / "BTCUSDT_pattern_trade_study.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pattern-as-of and conservative MFE research (no trading)")
    parser.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    parser.add_argument("--diagnostics", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-pattern-age-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument("--left-bars", type=int, default=2)
    parser.add_argument("--right-bars", type=int, default=2)
    parser.add_argument("--ratio-tolerance", type=float, default=.04)
    args = parser.parse_args(argv)
    summary = build_study(args.data_dir, args.diagnostics or args.data_dir / "BTCUSDT_trade_diagnostics.json",
                          args.output_dir or args.data_dir, max_age_hours=args.max_pattern_age_hours,
                          left_bars=args.left_bars, right_bars=args.right_bars, ratio_tolerance=args.ratio_tolerance)
    print(json.dumps({"study_version": summary["study_version"], "event_counts": summary["event_counts"],
                      "baseline_test": summary["variants"].get("ob_1h_baseline", {}).get("test")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())