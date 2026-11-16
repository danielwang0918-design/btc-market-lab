#!/usr/bin/env python3
"""Causal Elliott-style and harmonic candidate labels; research only, never orders.

Strict closed-candle pivots, explicit confirmation latency, segment isolation and
predeclared Fibonacci tolerances. No profitability or entry signal is implied.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

VERSION = "causal-pattern-research-v1"
INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
DEFAULT_LEFT = 2
DEFAULT_RIGHT = 2
DEFAULT_TOLERANCE = 0.04  # Absolute Fibonacci-ratio tolerance, fixed before evaluation.

# B retracement of XA, D retracement/extension of XA, C of AB, CD extension of BC.
# This is an explicitly selected subset of published harmonic definitions.
HARMONICS = {
    "gartley": {"b": (0.618, 0.618), "d": 0.786, "c": (0.382, 0.886), "cd_bc": (1.13, 1.618)},
    "bat": {"b": (0.382, 0.500), "d": 0.886, "c": (0.382, 0.886), "cd_bc": (1.618, 2.618)},
    "butterfly": {"b": (0.786, 0.786), "d": 1.270, "c": (0.382, 0.886), "cd_bc": (1.618, 2.240)},
    "crab": {"b": (0.382, 0.618), "d": 1.618, "c": (0.382, 0.886), "cd_bc": (2.240, 3.618)},
}
OUTPUT_COLUMNS = [
    "pattern_id", "symbol", "interval", "family", "pattern", "direction", "source",
    "formed_at_open_time", "confirmed_at_close_time", "usable_from_open_time",
    "pivot_open_times_json", "pivot_prices_json", "ratios_json", "evidence_json",
    "parameters_json", "detector_version",
]


def methodology() -> dict[str, Any]:
    return {
        "version": VERSION, "timeframes": list(INTERVAL_MS), "closed_candles_only": True,
        "pivot": "Strict local high/low with left and right fully closed candles; same-type pivots are compressed online to the more extreme pivot.",
        "confirmation": "A pattern is available only after the final pivot's right-side confirmation closes; usable at the next interval open.",
        "elliott": "Only coarse 1-5 impulse-shaped pivot candidates, not subjective nested wave counts. Wave 2 does not pass origin; wave 3 not shortest; wave 4 no wave-1 territory overlap; fifth must extend wave 3 (conservative subset).",
        "harmonic": "XABCD Gartley/Bat/Butterfly/Crab candidate subsets using predeclared XA/AB/BC ratios; D is CONFIRMED, not forecast, and the output does not claim a valid tradable PRZ.",
        "gap_handling": "No pivots or patterns cross missing hourly/four-hour/daily bars; invalid timestamps and prices fail closed.",
        "evaluation": "Annotation-only. Compare out-of-sample performance after separating training/validation/test and controlling for overlapping candidate events and multiple comparisons.",
        "harmonic_definitions": HARMONICS,
        "defaults": {"left_bars": DEFAULT_LEFT, "right_bars": DEFAULT_RIGHT, "ratio_tolerance": DEFAULT_TOLERANCE},
    }


def _numeric(row: dict[str, Any], name: str) -> float:
    try:
        result = float(row[name])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid candle field: {name}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Non-finite candle field: {name}")
    return result


def _integer(row: dict[str, Any], name: str) -> int:
    value = row.get(name)
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError(f"Invalid integer candle field: {name}")
    return int(value)


def _closed(row: dict[str, Any]) -> bool:
    return row.get("is_closed") is True or str(row.get("is_closed", "")).lower() in ("true", "1", "yes")


def _clean_groups(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str], list[list[dict[str, Any]]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for original in rows:
        row = dict(original)
        symbol, interval = str(row.get("symbol", "")), str(row.get("interval", ""))
        if not symbol or interval not in INTERVAL_MS:
            raise ValueError("Missing symbol or unsupported interval (1h/4h/1d only)")
        groups[(symbol, interval)].append(row)
    output: dict[tuple[str, str], list[list[dict[str, Any]]]] = {}
    for key, group in groups.items():
        interval = key[1]
        step = INTERVAL_MS[interval]
        segments: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        previous_open: int | None = None
        unclosed_seen = False
        current_source: str | None = None
        for row in group:
            open_ms, close_ms = _integer(row, "open_time"), _integer(row, "close_time")
            if open_ms % step or close_ms != open_ms + step - 1:
                raise ValueError("Unaligned or invalid candle close time")
            if previous_open is not None and open_ms <= previous_open:
                raise ValueError("Unsorted or duplicate candle open time")
            if previous_open is not None and (open_ms - previous_open) % step:
                raise ValueError("Irregular candle interval")
            previous_open = open_ms
            o, h, l, c = (_numeric(row, f) for f in ("open", "high", "low", "close"))
            v = _numeric(row, "volume")
            if l <= 0 or l > min(o, c) or h < max(o, c) or v < 0:
                raise ValueError("Invalid OHLC or volume")
            source = str(row.get("source", ""))
            if not source:
                raise ValueError("Missing provenance source")
            if not _closed(row):
                unclosed_seen = True
                continue
            if unclosed_seen:
                raise ValueError("Closed candle follows an unclosed candle")
            if current and (open_ms - _integer(current[-1], "open_time") != step or source != current_source):
                segments.append(current)
                current = []
            current_source = source
            current.append(row)
        if current:
            segments.append(current)
        output[key] = segments
    return output


def _pivots(rows: list[dict[str, Any]], left: int, right: int):
    """Yield causal swing events ordered by the candle that confirms them."""
    for i in range(left, len(rows) - right):
        window = rows[i - left:i + right + 1]
        high, low = _numeric(rows[i], "high"), _numeric(rows[i], "low")
        is_high = all(high > _numeric(x, "high") for k, x in enumerate(window) if k != left)
        is_low = all(low < _numeric(x, "low") for k, x in enumerate(window) if k != left)
        # An outside bar with both extreme flags cannot be a unique alternating turn.
        if is_high == is_low:
            continue
        kind = "high" if is_high else "low"
        yield {
            "kind": kind, "price": high if is_high else low,
            "index": i, "confirmed_index": i + right,
            "open_time": _integer(rows[i], "open_time"),
            "confirmed_close_time": _integer(rows[i + right], "close_time"),
        }


def _candidate(pivots: list[dict[str, Any]], family: str, name: str,
               direction: str, ratios: dict[str, float], rows: list[dict[str, Any]],
               left: int, right: int, tolerance: float) -> dict[str, Any]:
    last = pivots[-1]
    symbol, interval, source = str(rows[0]["symbol"]), str(rows[0]["interval"]), str(rows[0]["source"])
    open_times = [p["open_time"] for p in pivots]
    prices = [p["price"] for p in pivots]
    evidence = [{
        "role": ("XABCD" if family == "harmonic" else "012345")[j],
        "kind": p["kind"], "pivot_open_time": p["open_time"],
        "pivot_price": p["price"], "confirmed_at_close_time": p["confirmed_close_time"],
    } for j, p in enumerate(pivots)]
    return {
        "pattern_id": f"{VERSION}:{symbol}:{interval}:{name}:{direction}:" + ":".join(str(n) for n in open_times),
        "symbol": symbol, "interval": interval, "family": family, "pattern": name,
        "direction": direction, "source": source,
        "formed_at_open_time": last["open_time"],
        "confirmed_at_close_time": last["confirmed_close_time"],
        "usable_from_open_time": last["confirmed_close_time"] + 1,
        "pivot_open_times_json": json.dumps(open_times, separators=(",", ":")),
        "pivot_prices_json": json.dumps(prices, separators=(",", ":")),
        "ratios_json": json.dumps(ratios, sort_keys=True, separators=(",", ":")),
        "evidence_json": json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        "parameters_json": json.dumps({"left_bars": left, "right_bars": right, "ratio_tolerance": tolerance}, sort_keys=True, separators=(",", ":")),
        "detector_version": VERSION,
    }


def _elliott(points: list[dict[str, Any]], rows: list[dict[str, Any]],
             left: int, right: int, tolerance: float) -> dict[str, Any] | None:
    if len(points) != 6 or [p["kind"] for p in points] not in (["low", "high", "low", "high", "low", "high"], ["high", "low", "high", "low", "high", "low"]):
        return None
    direction = "bullish" if points[0]["kind"] == "low" else "bearish"
    sign = 1 if direction == "bullish" else -1
    p = [sign * x["price"] for x in points]
    w1, w3, w5 = p[1] - p[0], p[3] - p[2], p[5] - p[4]
    if min(w1, w3, w5) <= 0:
        return None
    if not (p[0] < p[2] < p[1] < p[4] < p[3] < p[5]):
        return None
    if w3 < min(w1, w5):
        return None
    return _candidate(points, "elliott", "impulse_5_candidate", direction, {
        "wave2_retracement": (p[1] - p[2]) / w1,
        "wave4_retracement": (p[3] - p[4]) / w3,
        "wave3_to_wave1": w3 / w1,
        "wave5_to_wave1": w5 / w1,
    }, rows, left, right, tolerance)


def _harmonic(points: list[dict[str, Any]], rows: list[dict[str, Any]],
              left: int, right: int, tolerance: float) -> list[dict[str, Any]]:
    if len(points) != 5 or [p["kind"] for p in points] not in (["low", "high", "low", "high", "low"], ["high", "low", "high", "low", "high"]):
        return []
    direction = "bullish" if points[0]["kind"] == "low" else "bearish"
    sign = 1 if direction == "bullish" else -1
    x, a, b, c, d = [sign * p["price"] for p in points]
    if not (x < a and x < b < a and b < c < a and d < b and d < c):
        return []
    xa, ab, bc, cd = a - x, a - b, c - b, c - d
    if min(xa, ab, bc, cd) <= 0:
        return []
    ratios = {"b_xa": ab / xa, "c_ab": bc / ab, "d_xa": (a - d) / xa, "cd_bc": cd / bc}
    results = []
    for name, rules in HARMONICS.items():
        if not (rules["b"][0] - tolerance <= ratios["b_xa"] <= rules["b"][1] + tolerance):
            continue
        if not (rules["c"][0] <= ratios["c_ab"] <= rules["c"][1]):
            continue
        if abs(ratios["d_xa"] - rules["d"]) > tolerance:
            continue
        if not (rules["cd_bc"][0] <= ratios["cd_bc"] <= rules["cd_bc"][1]):
            continue
        if (name in ("gartley", "bat") and d <= x) or (name in ("butterfly", "crab") and d >= x):
            continue
        results.append(_candidate(points, "harmonic", name, direction, ratios, rows, left, right, tolerance))
    return results


def analyze_patterns(candles: Iterable[dict[str, Any]], *, left_bars: int = DEFAULT_LEFT,
                     right_bars: int = DEFAULT_RIGHT, ratio_tolerance: float = DEFAULT_TOLERANCE
                     ) -> list[dict[str, Any]]:
    """Return only causally confirmed research labels for 1h/4h/1d closed bars."""
    if isinstance(left_bars, bool) or isinstance(right_bars, bool) or not isinstance(left_bars, int) or not isinstance(right_bars, int) or min(left_bars, right_bars) < 1:
        raise ValueError("Pivot left/right bars must be positive integers")
    if not isinstance(ratio_tolerance, (int, float)) or isinstance(ratio_tolerance, bool) or not math.isfinite(ratio_tolerance) or not 0 <= ratio_tolerance <= .10:
        raise ValueError("Fibonacci tolerance must be a finite number in [0, 0.10]")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, segments in sorted(_clean_groups(candles).items()):
        for rows in segments:
            alternating: list[dict[str, Any]] = []
            for pivot in _pivots(rows, left_bars, right_bars):
                if alternating and alternating[-1]["kind"] == pivot["kind"]:
                    extreme = pivot["price"] > alternating[-1]["price"] if pivot["kind"] == "high" else pivot["price"] < alternating[-1]["price"]
                    if not extreme:
                        continue
                    alternating[-1] = pivot
                else:
                    alternating.append(pivot)
                matches = _harmonic(alternating[-5:], rows, left_bars, right_bars, ratio_tolerance) if len(alternating) >= 5 else []
                if len(alternating) >= 6:
                    result = _elliott(alternating[-6:], rows, left_bars, right_bars, ratio_tolerance)
                    if result:
                        matches.append(result)
                for result in matches:
                    if result["pattern_id"] not in seen:
                        seen.add(result["pattern_id"])
                        out.append(result)
    return sorted(out, key=lambda r: (r["symbol"], r["interval"], r["confirmed_at_close_time"], r["pattern_id"]))


def patterns_available_at(events: Iterable[dict[str, Any]], asof_open_time_ms: int) -> list[dict[str, Any]]:
    """Use only already-confirmed labels available at the supplied candle open."""
    return [e for e in events if int(e["usable_from_open_time"]) <= asof_open_time_ms]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research-only causal Elliott/harmonic candidates (no trading)")
    parser.add_argument("input_csv", type=Path, help="Binance canonical historical candles CSV")
    parser.add_argument("output_csv", type=Path, help="Confirmed candidate annotations CSV")
    parser.add_argument("--left-bars", type=int, default=DEFAULT_LEFT)
    parser.add_argument("--right-bars", type=int, default=DEFAULT_RIGHT)
    parser.add_argument("--ratio-tolerance", type=float, default=DEFAULT_TOLERANCE)
    args = parser.parse_args(argv)
    with args.input_csv.open("r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    events = analyze_patterns(rows, left_bars=args.left_bars, right_bars=args.right_bars,
                              ratio_tolerance=args.ratio_tolerance)
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(events)
    print(json.dumps({"output": str(args.output_csv), "event_count": len(events), "methodology": methodology()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
