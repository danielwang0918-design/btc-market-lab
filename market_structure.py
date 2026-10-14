"""Deterministic, evidence-backed market-structure labels for closed Binance candles.

These labels describe past price action. They are not trading signals or advice.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

DETECTOR_VERSION = "market-structure-v2"
INTERVAL_MILLISECONDS = {"1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000}
DEFAULT_LEFT_BARS = 2
DEFAULT_RIGHT_BARS = 2
DEFAULT_OB_LOOKBACK = 10
DEFAULT_2B_RETURN_BARS = 2

EVENT_COLUMNS = [
    "event_id", "symbol", "interval", "event_type", "direction",
    "formed_at_open_time", "formed_at_utc",
    "confirmed_at_open_time", "confirmed_at_open_time_utc",
    "confirmed_at_close_time", "confirmed_at_utc",
    "price_low", "price_high", "reference_price",
    "evidence_json", "detector_version", "parameters_json", "source",
]


def methodology() -> dict[str, Any]:
    return {
        "detector_version": DETECTOR_VERSION,
        "default_parameters": {"left_bars": DEFAULT_LEFT_BARS, "right_bars": DEFAULT_RIGHT_BARS, "order_block_lookback_bars": DEFAULT_OB_LOOKBACK, "two_b_return_bars": DEFAULT_2B_RETURN_BARS},
        "closed_candles_only": True,
        "swing": "Strict local high/low over a centered window; confirmed only after the configured number of right-side closed candles.",
        "order_block": "The last opposing-color candle within the lookback before a candle closes beyond the latest confirmed swing; zone is that candle's full low-to-high range.",
        "fair_value_gap": "Three-candle wick gap: first high below third low (bullish), or first low above third high (bearish). Zone is the gap.",
        "two_b_fakeout": "The first wick breach of a confirmed swing that closes back inside on the breach candle or within the configured subsequent closed candles. A breach with no timely return is consumed and not relabeled later.",
        "structure_break": "First close beyond the latest confirmed swing high or low; included as evidence for order-block labels.",
        "gap_handling": "Missing intervals split the candle series; no event may use evidence from both sides of a gap.",
        "limits": "Definitions are explicit implementation conventions, not universal market terminology. Labels describe historical candles and do not imply direction forecasts, entries, or exits.",
    }


def _is_closed(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "1", "yes"}


def _num(candle: dict[str, Any], field: str) -> Decimal:
    try:
        number = Decimal(str(candle[field]))
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError(f"K 線欄位 {field} 缺失或不是數值。") from exc
    if not number.is_finite():
        raise ValueError(f"K 線欄位 {field} 必須為有限數值。")
    return number


def _candle_key(candle: dict[str, Any]) -> int:
    try:
        return int(candle["open_time"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("K 線缺少有效的 open_time。") from exc


def _timestamp(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"K 線欄位 {field} 必須是非負整數毫秒時間戳。")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise ValueError(f"K 線欄位 {field} 必須是非負整數毫秒時間戳。")
    if result < 0:
        raise ValueError(f"K 線欄位 {field} 必須是非負整數毫秒時間戳。")
    return result


def _utc_timestamp(value: int) -> str:
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def inspect_candle_integrity(
    rows: Iterable[dict[str, Any]], interval: str, server_time: int | None = None
) -> dict[str, Any]:
    """Report ordering, timestamp validity, and missing opens in the returned window."""
    if interval not in INTERVAL_MILLISECONDS:
        raise ValueError(f"不支援的 K 線週期：{interval}。")
    step = INTERVAL_MILLISECONDS[interval]
    candles = list(rows)
    errors: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    closed_count = 0
    previous_open: int | None = None

    for index, row in enumerate(candles):
        try:
            open_time = _timestamp(row.get("open_time"), "open_time")
            close_time = _timestamp(row.get("close_time"), "close_time")
        except ValueError as exc:
            errors.append({"kind": "invalid_timestamp", "row_index": index, "detail": str(exc)})
            continue

        if open_time % step != 0:
            errors.append({"kind": "unaligned_open_time", "row_index": index, "open_time": open_time, "open_time_utc": _utc_timestamp(open_time)})
        if close_time != open_time + step - 1:
            errors.append({"kind": "invalid_close_time", "row_index": index, "open_time": open_time, "open_time_utc": _utc_timestamp(open_time), "close_time": close_time, "close_time_utc": _utc_timestamp(close_time), "expected_close_time": open_time + step - 1})
        if server_time is not None and open_time > server_time:
            errors.append({"kind": "future_open_time", "row_index": index, "open_time": open_time, "open_time_utc": _utc_timestamp(open_time), "server_time": server_time, "server_time_utc": _utc_timestamp(server_time)})
        if _is_closed(row.get("is_closed")):
            closed_count += 1

        if previous_open is not None:
            delta = open_time - previous_open
            if delta == 0:
                errors.append({"kind": "duplicate_open_time", "row_index": index, "open_time": open_time, "open_time_utc": _utc_timestamp(open_time)})
            elif delta < 0:
                errors.append({"kind": "out_of_order_open_time", "row_index": index, "previous_open_time": previous_open, "previous_open_time_utc": _utc_timestamp(previous_open), "open_time": open_time, "open_time_utc": _utc_timestamp(open_time)})
            elif delta > step:
                if delta % step:
                    errors.append({"kind": "irregular_open_time_spacing", "row_index": index, "previous_open_time": previous_open, "previous_open_time_utc": _utc_timestamp(previous_open), "open_time": open_time, "open_time_utc": _utc_timestamp(open_time), "delta_milliseconds": delta})
                else:
                    first_missing = previous_open + step
                    last_missing = open_time - step
                    gaps.append({
                        "previous_open_time": previous_open,
                        "previous_open_time_utc": _utc_timestamp(previous_open),
                        "first_missing_open_time": first_missing,
                        "first_missing_open_time_utc": _utc_timestamp(first_missing),
                        "last_missing_open_time": last_missing,
                        "last_missing_open_time_utc": _utc_timestamp(last_missing),
                        "next_open_time": open_time,
                        "next_open_time_utc": _utc_timestamp(open_time),
                        "missing_candle_count": delta // step - 1,
                    })
        previous_open = open_time

    missing_count = sum(gap["missing_candle_count"] for gap in gaps)
    if errors:
        status = "invalid"
        suitability = "unavailable"
    elif gaps:
        status = "gaps_detected"
        suitability = "partial_gap_segmented"
    elif not candles:
        status = "empty"
        suitability = "unavailable"
    else:
        status = "complete"
        suitability = "complete_within_returned_window"
    return {
        "interval": interval,
        "interval_milliseconds": step,
        "received_candle_count": len(candles),
        "closed_candle_count": closed_count,
        "unclosed_candle_count": len(candles) - closed_count,
        "missing_candle_count": missing_count,
        "is_contiguous": bool(candles) and not errors and not gaps,
        "status": status,
        "analysis_suitability": suitability,
        "gaps": gaps,
        "errors": errors,
    }


def _split_contiguous(rows: list[dict[str, Any]], interval: str) -> list[list[dict[str, Any]]]:
    report = inspect_candle_integrity(rows, interval)
    if report["errors"]:
        first = report["errors"][0]
        raise ValueError(f"{interval} K 線完整性檢查失敗：{first['kind']}。")
    if not rows:
        return []
    step = INTERVAL_MILLISECONDS[interval]
    segments: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_open: int | None = None
    for row in rows:
        open_time = _timestamp(row.get("open_time"), "open_time")
        if previous_open is not None and open_time - previous_open != step:
            segments.append(current)
            current = []
        current.append(row)
        previous_open = open_time
    if current:
        segments.append(current)
    return segments


def _evidence(role_candles: list[tuple[str, dict[str, Any]]]) -> str:
    evidence = []
    for role, candle in role_candles:
        evidence.append({
            "role": role,
            "symbol": candle["symbol"],
            "interval": candle["interval"],
            "open_time": int(candle["open_time"]),
            "open_time_utc": candle.get("open_time_utc", ""),
            "close_time": int(candle["close_time"]),
            "close_time_utc": candle.get("close_time_utc", ""),
            "open": str(candle["open"]),
            "high": str(candle["high"]),
            "low": str(candle["low"]),
            "close": str(candle["close"]),
            "volume": str(candle.get("volume", "")),
            "is_closed": str(candle["is_closed"]).lower(),
            "source": candle.get("source", ""),
        })
    return json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))


def _event(
    symbol: str,
    interval: str,
    event_type: str,
    direction: str,
    formed: dict[str, Any],
    confirmed: dict[str, Any],
    evidence: list[tuple[str, dict[str, Any]]],
    params: dict[str, Any],
    low: Decimal | None = None,
    high: Decimal | None = None,
    reference: Decimal | None = None,
) -> dict[str, Any]:
    formed_time = _candle_key(formed)
    confirmed_open_time = _candle_key(confirmed)
    confirmed_close_time = int(confirmed["close_time"])
    source_values = {str(candle.get("source", "")) for _, candle in evidence}
    if len(source_values) != 1 or "" in source_values:
        raise ValueError("同一結構事件的證據必須有一致且非空的資料來源。")
    return {
        "event_id": f"{symbol}:{interval}:{event_type}:{direction}:{formed_time}:{confirmed_close_time}:{reference if reference is not None else ''}",
        "symbol": symbol,
        "interval": interval,
        "event_type": event_type,
        "direction": direction,
        "formed_at_open_time": formed_time,
        "formed_at_utc": formed.get("open_time_utc", ""),
        "confirmed_at_open_time": confirmed_open_time,
        "confirmed_at_open_time_utc": confirmed.get("open_time_utc", ""),
        "confirmed_at_close_time": confirmed_close_time,
        "confirmed_at_utc": confirmed.get("close_time_utc", ""),
        "price_low": "" if low is None else format(low, "f"),
        "price_high": "" if high is None else format(high, "f"),
        "reference_price": "" if reference is None else format(reference, "f"),
        "evidence_json": _evidence(evidence),
        "detector_version": DETECTOR_VERSION,
        "parameters_json": json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "source": next(iter(source_values)),
    }


def _analyze_interval(
    rows: list[dict[str, Any]],
    left_bars: int,
    right_bars: int,
    ob_lookback: int,
    two_b_return_bars: int,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    symbol, interval = str(rows[0]["symbol"]), str(rows[0]["interval"])
    seen: set[int] = set()
    previous_open: int | None = None
    for row in rows:
        if row.get("symbol") != symbol or row.get("interval") != interval:
            raise ValueError("同一分析批次不可混合不同交易對或週期。")
        open_time = _candle_key(row)
        if open_time in seen:
            raise ValueError(f"重複的 K 線 open_time：{open_time}。")
        if previous_open is not None and open_time < previous_open:
            raise ValueError("K 線 open_time 順序錯誤。")
        seen.add(open_time)
        previous_open = open_time
        if not _is_closed(row.get("is_closed")):
            raise ValueError("市場結構只能使用已收盤 K 線。")
        open_price, high, low, close, volume = (_num(row, field) for field in ("open", "high", "low", "close", "volume"))
        if volume < 0 or low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise ValueError("K 線 OHLC 或成交量完整性檢查失敗。")

    params = {
        "left_bars": left_bars,
        "right_bars": right_bars,
        "order_block_lookback_bars": ob_lookback,
        "two_b_return_bars": two_b_return_bars,
        "fvg_method": "three_candle_wick_gap",
    }
    events: list[dict[str, Any]] = []
    pivot_highs: dict[int, tuple[int, float]] = {}
    pivot_lows: dict[int, tuple[int, float]] = {}

    # A pivot at p is only available to downstream logic at p + right_bars.
    for pivot in range(left_bars, len(rows) - right_bars):
        window = rows[pivot - left_bars:pivot + right_bars + 1]
        high = _num(rows[pivot], "high")
        low = _num(rows[pivot], "low")
        if all(high > _num(c, "high") for j, c in enumerate(window) if j != left_bars):
            confirmed_idx = pivot + right_bars
            pivot_highs[confirmed_idx] = (pivot, high)
            events.append(_event(symbol, interval, "swing_high", "high", rows[pivot], rows[confirmed_idx],
                                 [("swing_point", rows[pivot]), ("right_side_confirmation", rows[confirmed_idx])],
                                 params, low=high, high=high, reference=high))
        if all(low < _num(c, "low") for j, c in enumerate(window) if j != left_bars):
            confirmed_idx = pivot + right_bars
            pivot_lows[confirmed_idx] = (pivot, low)
            events.append(_event(symbol, interval, "swing_low", "low", rows[pivot], rows[confirmed_idx],
                                 [("swing_point", rows[pivot]), ("right_side_confirmation", rows[confirmed_idx])],
                                 params, low=low, high=low, reference=low))

    # Three-candle gap labels become known at the close of the third candle.
    for i in range(2, len(rows)):
        first, middle, third = rows[i - 2], rows[i - 1], rows[i]
        first_high, first_low = _num(first, "high"), _num(first, "low")
        third_high, third_low = _num(third, "high"), _num(third, "low")
        if first_high < third_low:
            events.append(_event(symbol, interval, "fair_value_gap", "bullish", third, third,
                                 [("first_candle", first), ("middle_candle", middle), ("third_candle", third)],
                                 params, low=first_high, high=third_low))
        if third_high < first_low:
            events.append(_event(symbol, interval, "fair_value_gap", "bearish", third, third,
                                 [("first_candle", first), ("middle_candle", middle), ("third_candle", third)],
                                 params, low=third_high, high=first_low))

    # Confirmed swings are tracked chronologically; each level breaks at most once.
    latest_high: tuple[int, int, float] | None = None
    latest_low: tuple[int, int, float] | None = None
    broken_highs: set[int] = set()
    broken_lows: set[int] = set()
    for i, candle in enumerate(rows):
        if i in pivot_highs:
            pivot_idx, price = pivot_highs[i]
            latest_high = (pivot_idx, i, price)
        if i in pivot_lows:
            pivot_idx, price = pivot_lows[i]
            latest_low = (pivot_idx, i, price)

        close = _num(candle, "close")
        if latest_high and latest_high[0] not in broken_highs and i > latest_high[1] and close > latest_high[2]:
            pivot_idx, confirmed_idx, level = latest_high
            broken_highs.add(pivot_idx)
            events.append(_event(symbol, interval, "structure_break", "bullish", candle, candle,
                                 [("broken_swing", rows[pivot_idx]), ("swing_confirmation", rows[confirmed_idx]), ("breakout_close", candle)],
                                 params, reference=level))
            start = max(confirmed_idx, i - ob_lookback)
            opposing = next((j for j in range(i - 1, start - 1, -1) if _num(rows[j], "close") < _num(rows[j], "open")), None)
            if opposing is not None:
                ob = rows[opposing]
                events.append(_event(symbol, interval, "order_block", "bullish", ob, candle,
                                     [("last_opposing_candle", ob), ("broken_swing", rows[pivot_idx]), ("swing_confirmation", rows[confirmed_idx]), ("structure_break", candle)],
                                     params, low=_num(ob, "low"), high=_num(ob, "high"), reference=level))
        if latest_low and latest_low[0] not in broken_lows and i > latest_low[1] and close < latest_low[2]:
            pivot_idx, confirmed_idx, level = latest_low
            broken_lows.add(pivot_idx)
            events.append(_event(symbol, interval, "structure_break", "bearish", candle, candle,
                                 [("broken_swing", rows[pivot_idx]), ("swing_confirmation", rows[confirmed_idx]), ("breakout_close", candle)],
                                 params, reference=level))
            start = max(confirmed_idx, i - ob_lookback)
            opposing = next((j for j in range(i - 1, start - 1, -1) if _num(rows[j], "close") > _num(rows[j], "open")), None)
            if opposing is not None:
                ob = rows[opposing]
                events.append(_event(symbol, interval, "order_block", "bearish", ob, candle,
                                     [("last_opposing_candle", ob), ("broken_swing", rows[pivot_idx]), ("swing_confirmation", rows[confirmed_idx]), ("structure_break", candle)],
                                     params, low=_num(ob, "low"), high=_num(ob, "high"), reference=level))

    # 2B candidates are only examined after the reference swing is confirmed.
    for confirmed_idx, (pivot_idx, level) in pivot_highs.items():
        breach_idx = next((i for i in range(confirmed_idx + 1, len(rows)) if _num(rows[i], "high") > level), None)
        if breach_idx is None:
            continue
        return_idx = next((i for i in range(breach_idx, min(len(rows), breach_idx + two_b_return_bars + 1)) if _num(rows[i], "close") <= level), None)
        if return_idx is not None:
            evidence = [("reference_swing", rows[pivot_idx]), ("swing_confirmation", rows[confirmed_idx]), ("break_and_return" if return_idx == breach_idx else "breakout_candle", rows[breach_idx])]
            if return_idx != breach_idx:
                evidence.append(("return_inside_candle", rows[return_idx]))
            events.append(_event(symbol, interval, "two_b_fakeout", "bearish", rows[breach_idx], rows[return_idx], evidence,
                                 params, reference=level))
    for confirmed_idx, (pivot_idx, level) in pivot_lows.items():
        breach_idx = next((i for i in range(confirmed_idx + 1, len(rows)) if _num(rows[i], "low") < level), None)
        if breach_idx is None:
            continue
        return_idx = next((i for i in range(breach_idx, min(len(rows), breach_idx + two_b_return_bars + 1)) if _num(rows[i], "close") >= level), None)
        if return_idx is not None:
            evidence = [("reference_swing", rows[pivot_idx]), ("swing_confirmation", rows[confirmed_idx]), ("break_and_return" if return_idx == breach_idx else "breakout_candle", rows[breach_idx])]
            if return_idx != breach_idx:
                evidence.append(("return_inside_candle", rows[return_idx]))
            events.append(_event(symbol, interval, "two_b_fakeout", "bullish", rows[breach_idx], rows[return_idx], evidence,
                                 params, reference=level))

    events.sort(key=lambda row: (int(row["confirmed_at_close_time"]), row["event_type"], row["event_id"]))
    return events


def analyze_candles(
    candles: Iterable[dict[str, Any]],
    *,
    left_bars: int = DEFAULT_LEFT_BARS,
    right_bars: int = DEFAULT_RIGHT_BARS,
    ob_lookback: int = DEFAULT_OB_LOOKBACK,
    two_b_return_bars: int = DEFAULT_2B_RETURN_BARS,
) -> list[dict[str, Any]]:
    """Label closed candles independently per symbol and interval, with raw evidence attached."""
    for name, value in (("left_bars", left_bars), ("right_bars", right_bars), ("ob_lookback", ob_lookback), ("two_b_return_bars", two_b_return_bars)):
        if not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} 必須是大於 0 的整數。")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candle in candles:
        if not _is_closed(candle.get("is_closed")):
            continue
        grouped[(str(candle.get("symbol", "")), str(candle.get("interval", "")))].append(dict(candle))
    result: list[dict[str, Any]] = []
    for key in sorted(grouped):
        for segment in _split_contiguous(grouped[key], key[1]):
            result.extend(_analyze_interval(segment, left_bars, right_bars, ob_lookback, two_b_return_bars))
    result.sort(key=lambda row: (row["symbol"], row["interval"], int(row["confirmed_at_close_time"]), row["event_type"], row["event_id"]))
    return result
