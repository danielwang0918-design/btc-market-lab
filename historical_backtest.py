#!/usr/bin/env python3
"""Historical Binance spot candles and an auditable, conservative P0 backtest."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from binance_market_data import BinanceAPIError, BinanceClient, SPOT_BASE, SOURCE_SPOT, validate_symbol
from market_structure import INTERVAL_MILLISECONDS, inspect_candle_integrity, _event, DETECTOR_VERSION
from decimal import Decimal
import bisect
import market_research

DEFAULT_START = "2017-08-17"
INTERVALS = ("1d", "4h", "1h")
COLUMNS = ["symbol", "interval", "open_time", "open_time_utc", "open", "high", "low", "close", "volume", "close_time", "close_time_utc", "exchange_close_time", "exchange_close_time_utc", "is_closed", "source"]
SOURCE = SOURCE_SPOT


def parse_time(value: str, *, end: bool = False) -> int:
    """Parse a UTC date or ISO timestamp; a date end means the next midnight (exclusive)."""
    try:
        if len(value) == 10:
            dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if end:
                dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError as exc:
        raise ValueError(f"日期格式無效：{value}。請用 YYYY-MM-DD 或 ISO 8601。") from exc


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _server_time(client: BinanceClient) -> int:
    response = client.get_json(SPOT_BASE, "/api/v3/time")
    if not isinstance(response, dict) or type(response.get("serverTime")) is not int:
        raise BinanceAPIError("Binance server time 回應格式錯誤。")
    return response["serverTime"]


def _parse_page(raw: Any, symbol: str, interval: str, server_time: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise BinanceAPIError("Binance K 線回應不是清單。")
    step = INTERVAL_MILLISECONDS[interval]
    result: list[dict[str, Any]] = []
    previous = -1
    for item in raw:
        if not isinstance(item, list) or len(item) < 7:
            raise BinanceAPIError("K 線欄位不足。")
        try:
            open_time, close_time = int(item[0]), int(item[6])
            o, h, low, c, volume = (float(item[i]) for i in (1, 2, 3, 4, 5))
        except (TypeError, ValueError, OverflowError) as exc:
            raise BinanceAPIError("K 線含有無效時間或數值。") from exc
        if open_time <= previous:
            raise BinanceAPIError("Binance 分頁內的 K 線重複或未按時間排序。")
        scheduled_close = open_time + step - 1
        if open_time % step or not open_time <= close_time <= scheduled_close:
            raise BinanceAPIError("K 線時間戳未對齊週期，或交易所 close_time 超出該根 K 線時段。")
        if not all(math.isfinite(x) for x in (o, h, low, c, volume)) or min(o, h, low, c) <= 0 or volume < 0 or low > min(o, c) or h < max(o, c) or low > h:
            raise BinanceAPIError("K 線 OHLCV 完整性檢查失敗。")
        if open_time > server_time:
            raise BinanceAPIError("K 線開盤時間晚於 Binance 伺服器時間。")
        result.append({"symbol": symbol, "interval": interval, "open_time": open_time,
                       "open_time_utc": _utc(open_time), "open": str(item[1]), "high": str(item[2]),
                       "low": str(item[3]), "close": str(item[4]), "volume": str(item[5]),
                       # Binance may timestamp the last trade rather than the scheduled
                       # interval boundary. Preserve it, and use a canonical boundary for
                       # continuity checks and closed-candle decisions.
                       "close_time": scheduled_close, "close_time_utc": _utc(scheduled_close),
                       "exchange_close_time": close_time, "exchange_close_time_utc": _utc(close_time),
                       "is_closed": str(scheduled_close < server_time).lower(), "source": SOURCE})
        previous = open_time
    return result


def fetch_historical_candles(
    symbol: str, interval: str, start_time: int, end_time: int,
    client: BinanceClient | None = None, *, page_limit: int = 1000,
) -> list[dict[str, Any]]:
    """Fetch [start_time, end_time) using Binance's 1000-candle maximum per request."""
    symbol = validate_symbol(symbol)
    if interval not in INTERVALS:
        raise ValueError(f"不支援週期 {interval}；可用：{', '.join(INTERVALS)}。")
    if type(start_time) is not int or type(end_time) is not int or start_time < 0 or end_time <= start_time:
        raise ValueError("開始時間與結束時間無效；結束時間必須晚於開始時間。")
    if not 1 <= page_limit <= 1000:
        raise ValueError("每次 API 請求數量必須介於 1 到 1000。")
    client = client or BinanceClient()
    step = INTERVAL_MILLISECONDS[interval]
    cursor = (start_time // step) * step
    exclusive_end = ((end_time + step - 1) // step) * step
    server_time = _server_time(client)
    dedup: dict[int, dict[str, Any]] = {}
    while cursor < exclusive_end and cursor < server_time:
        page = client.get_json(SPOT_BASE, "/api/v3/klines", {
            "symbol": symbol, "interval": interval, "startTime": cursor,
            "endTime": min(exclusive_end - 1, server_time), "limit": page_limit,
        })
        rows = _parse_page(page, symbol, interval, server_time)
        if not rows:
            break
        for row in rows:
            opened = int(row["open_time"])
            if opened >= start_time and opened < end_time and row["is_closed"] == "true":
                dedup[opened] = row
        next_cursor = int(rows[-1]["open_time"]) + step
        if next_cursor <= cursor:
            raise BinanceAPIError("K 線分頁游標未前進，已中止以避免無限循環。")
        cursor = next_cursor
        if len(rows) < page_limit:
            break
    return [dedup[key] for key in sorted(dedup)]


def load_csv(path: Path, symbol: str, interval: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    if any(row.get("symbol") != symbol or row.get("interval") != interval for row in rows):
        raise ValueError(f"{path} 已存在其他交易對或週期的資料，請移動該檔案或指定不同目錄。")
    return rows


def save_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def download_to_csv(
    symbol: str, interval: str, output: Path, *, start: str | None = None,
    end: str | None = None, client: BinanceClient | None = None,
) -> dict[str, Any]:
    symbol = validate_symbol(symbol)
    step = INTERVAL_MILLISECONDS.get(interval)
    if step is None:
        raise ValueError(f"不支援週期 {interval}；可用：{', '.join(INTERVALS)}。")
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{symbol}_{interval}.csv"
    existing = load_csv(path, symbol, interval)
    # Without --start, resume after the last stored candle. New datasets use Binance's first BTC spot date.
    start_ms = parse_time(start) if start else (max((int(r["open_time"]) for r in existing), default=parse_time(DEFAULT_START)) + step if existing else parse_time(DEFAULT_START))
    end_ms = parse_time(end, end=True) + 1 if end else int(time.time() * 1000)
    if end_ms <= start_ms:
        raise ValueError("結束日期必須晚於起始日期或最後一根已儲存 K 線。")
    fetched = fetch_historical_candles(symbol, interval, start_ms, end_ms, client)
    combined = {int(row["open_time"]): row for row in existing}
    combined.update({int(row["open_time"]): row for row in fetched})
    rows = [combined[key] for key in sorted(combined)]
    for row in rows:
        # Normalize imported CSV values and reject corrupt local edits before persisting.
        row["open_time"] = int(row["open_time"])
        row["close_time"] = int(row["close_time"])
    report = inspect_candle_integrity(rows, interval)
    if report["errors"]:
        raise ValueError(f"本機或下載 K 線無效：{report['errors'][0]['kind']}。原 CSV 未變更。")
    save_csv(path, rows)
    return {"path": str(path), "symbol": symbol, "interval": interval,
            "downloaded_count": len(fetched), "total_count": len(rows),
            "normalized_exchange_close_time_count": sum(int(row.get("exchange_close_time") or row["close_time"]) != int(row["close_time"]) for row in fetched),
            "data_integrity": report, "requested_start_utc": _utc(start_ms),
            "requested_end_exclusive_utc": _utc(end_ms)}




def _contiguous_segments(rows: list[dict[str, Any]], interval: str) -> list[list[dict[str, Any]]]:
    step = INTERVAL_MILLISECONDS[interval]
    segments: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous: int | None = None
    for row in rows:
        opened = int(row["open_time"])
        if previous is not None and opened - previous != step:
            if current:
                segments.append(current)
            current = []
        current.append(row)
        previous = opened
    if current:
        segments.append(current)
    return segments


def _json_default(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    raise TypeError(f"無法序列化 {type(value).__name__}")


def _number(value: Any, label: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    from decimal import Decimal, InvalidOperation
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效數值。") from exc
    if not number.is_finite() or (positive and number <= 0) or (nonnegative and number < 0):
        raise ValueError(f"{label} 數值不合理。")
    return number


def _closed_rows(path: Path, symbol: str, interval: str) -> list[dict[str, Any]]:
    rows = load_csv(path, symbol, interval)
    rows = [row for row in rows if str(row.get("is_closed", "")).strip().lower() == "true"]
    if not rows:
        raise ValueError(f"找不到 {symbol} {interval} 已收盤歷史資料：{path}")
    report = inspect_candle_integrity(rows, interval)
    if report["errors"]:
        raise ValueError(f"{path} K 線時間戳錯誤：{report['errors'][0]['kind']}。")
    for row in rows:
        for field in ("open_time", "close_time"):
            int(row[field])
        for field in ("open", "high", "low", "close", "volume"):
            value = _number(row.get(field), f"K 線 {field}", nonnegative=(field == "volume"), positive=(field != "volume"))
            row[field] = format(value, "f")
        if _number(row["low"], "low", positive=True) > min(_number(row["open"], "open", positive=True), _number(row["close"], "close", positive=True)):
            raise ValueError(f"{path} 的 K 線 low 高於 open/close。")
        if _number(row["high"], "high", positive=True) < max(_number(row["open"], "open", positive=True), _number(row["close"], "close", positive=True)):
            raise ValueError(f"{path} 的 K 線 high 低於 open/close。")
        if row.get("source") != SOURCE_SPOT:
            raise ValueError(f"{path} 存在非預期的資料來源欄位。")
    rows.sort(key=lambda row: int(row["open_time"]))
    return rows


def _strategy_events(rows: list[dict[str, Any]], interval: str, *, left_bars: int = 2,
                     right_bars: int = 2, ob_lookback: int = 10) -> list[dict[str, Any]]:
    """Create only causal swing, BOS and OB events needed by the frozen P0 strategy."""
    if not rows:
        return []
    symbol = str(rows[0]["symbol"])
    params = {
        "left_bars": left_bars,
        "right_bars": right_bars,
        "order_block_lookback_bars": ob_lookback,
        "two_b_return_bars": 2,
        "fvg_method": "three_candle_wick_gap",
    }
    pivot_highs: dict[int, tuple[int, Decimal]] = {}
    pivot_lows: dict[int, tuple[int, Decimal]] = {}
    events: list[dict[str, Any]] = []
    n = len(rows)
    for pivot in range(left_bars, n - right_bars):
        window = rows[pivot - left_bars:pivot + right_bars + 1]
        high = _number(rows[pivot]["high"], "swing high", positive=True)
        low = _number(rows[pivot]["low"], "swing low", positive=True)
        if all(high > _number(item["high"], "high", positive=True) for j, item in enumerate(window) if j != left_bars):
            confirmed = pivot + right_bars
            pivot_highs[confirmed] = (pivot, high)
            events.append(_event(
                symbol, interval, "swing_high", "high", rows[pivot], rows[confirmed],
                [("swing_point", rows[pivot]), ("right_side_confirmation", rows[confirmed])],
                params, low=high, high=high, reference=high,
            ))
        if all(low < _number(item["low"], "low", positive=True) for j, item in enumerate(window) if j != left_bars):
            confirmed = pivot + right_bars
            pivot_lows[confirmed] = (pivot, low)
            events.append(_event(
                symbol, interval, "swing_low", "low", rows[pivot], rows[confirmed],
                [("swing_point", rows[pivot]), ("right_side_confirmation", rows[confirmed])],
                params, low=low, high=low, reference=low,
            ))

    latest_high: tuple[int, int, Decimal] | None = None
    latest_low: tuple[int, int, Decimal] | None = None
    broken_highs: set[int] = set()
    broken_lows: set[int] = set()
    for index, candle in enumerate(rows):
        if index in pivot_highs:
            pivot, level = pivot_highs[index]
            latest_high = (pivot, index, level)
        if index in pivot_lows:
            pivot, level = pivot_lows[index]
            latest_low = (pivot, index, level)
        close = _number(candle["close"], "close", positive=True)
        candidates: list[tuple[str, tuple[int, int, Decimal]]] = []
        if latest_high and latest_high[0] not in broken_highs and index > latest_high[1] and close > latest_high[2]:
            candidates.append(("bullish", latest_high))
        if latest_low and latest_low[0] not in broken_lows and index > latest_low[1] and close < latest_low[2]:
            candidates.append(("bearish", latest_low))
        for direction, (pivot, confirmed, level) in candidates:
            if direction == "bullish":
                broken_highs.add(pivot)
            else:
                broken_lows.add(pivot)
            events.append(_event(
                symbol, interval, "structure_break", direction, candle, candle,
                [("broken_swing", rows[pivot]), ("swing_confirmation", rows[confirmed]), ("breakout_close", candle)],
                params, reference=level,
            ))
            start = max(confirmed, index - ob_lookback)
            if direction == "bullish":
                opposing = next((j for j in range(index - 1, start - 1, -1)
                                 if _number(rows[j]["close"], "close") < _number(rows[j]["open"], "open")), None)
            else:
                opposing = next((j for j in range(index - 1, start - 1, -1)
                                 if _number(rows[j]["close"], "close") > _number(rows[j]["open"], "open")), None)
            if opposing is not None:
                ob = rows[opposing]
                events.append(_event(
                    symbol, interval, "order_block", direction, ob, candle,
                    [("last_opposing_candle", ob), ("broken_swing", rows[pivot]),
                     ("swing_confirmation", rows[confirmed]), ("structure_break", candle)],
                    params, low=_number(ob["low"], "OB low", positive=True),
                    high=_number(ob["high"], "OB high", positive=True), reference=level,
                ))
    events.sort(key=lambda row: (int(row["confirmed_at_close_time"]), row["event_type"], row["event_id"]))
    return events


def _make_windows(
    asof_ms: int,
    rows_by_interval: dict[str, list[dict[str, Any]]],
    close_times_by_interval: dict[str, list[int]],
    event_records_by_interval: dict[str, list[tuple[int, dict[str, Any], frozenset[int]]]],
    event_times_by_interval: dict[str, list[int]],
    symbol: str,
    window_size: int = 200,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], list[dict[str, Any]]]:
    windows: dict[str, list[dict[str, Any]]] = {}
    integrity: dict[str, Any] = {}
    active_events: list[dict[str, Any]] = []
    for interval in INTERVALS:
        all_rows = rows_by_interval[interval]
        closes = close_times_by_interval[interval]
        end_index = bisect.bisect_left(closes, asof_ms)
        begin_index = max(0, end_index - window_size)
        window = all_rows[begin_index:end_index]
        windows[interval] = window
        integrity[interval] = inspect_candle_integrity(window, interval, asof_ms)
        if not window:
            continue
        open_set = {int(row["open_time"]) for row in window}
        times = event_times_by_interval[interval]
        records = event_records_by_interval[interval]
        left_event_index = bisect.bisect_left(times, int(window[0]["open_time"]))
        right_event_index = bisect.bisect_left(times, asof_ms)
        for _, event, evidence_opens in records[left_event_index:right_event_index]:
            if evidence_opens.issubset(open_set):
                active_events.append(event)
    candles = [dict(row) for interval in INTERVALS for row in windows[interval]]
    return windows, {"intervals": integrity}, active_events


def _prepare_snapshots(
    data_dir: Path,
    symbol: str,
    *,
    fee_bps_per_side: float,
    slippage_bps_per_side: float,
    minimum_net_rr: float,
    max_setup_age_hours: int,
    progress_every: int = 10000,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    rows_by_interval = {
        interval: _closed_rows(data_dir / f"{symbol}_{interval}.csv", symbol, interval)
        for interval in INTERVALS
    }
    events_by_interval: dict[str, list[dict[str, Any]]] = {}
    for interval, rows in rows_by_interval.items():
        all_events: list[dict[str, Any]] = []
        for segment in _contiguous_segments(rows, interval):
            all_events.extend(_strategy_events(segment, interval))
        all_events.sort(key=lambda item: (int(item["confirmed_at_close_time"]), item["event_type"], item["event_id"]))
        events_by_interval[interval] = all_events

    event_records_by_interval: dict[str, list[tuple[int, dict[str, Any], frozenset[int]]]] = {}
    event_times_by_interval: dict[str, list[int]] = {}
    for interval, events in events_by_interval.items():
        records = []
        for event in events:
            try:
                evidence = json.loads(event["evidence_json"])
                evidence_opens = frozenset(int(item["open_time"]) for item in evidence)
                confirmed = int(event["confirmed_at_close_time"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            records.append((confirmed, event, evidence_opens))
        records.sort(key=lambda item: (item[0], item[1]["event_type"], item[1]["event_id"]))
        event_records_by_interval[interval] = records
        event_times_by_interval[interval] = [record[0] for record in records]

    hour_rows = rows_by_interval["1h"]
    close_times_by_interval = {
        interval: [int(row["close_time"]) for row in rows]
        for interval, rows in rows_by_interval.items()
    }
    config = {
        "config_version": market_research.CONFIG_VERSION,
        "strategy_version": market_research.STRATEGY_VERSION,
        "minimum_net_rr": format(_number(minimum_net_rr, "minimum_net_rr", positive=True), "f"),
        "max_quote_age_seconds": 3600,
        "fee_bps_per_side": format(_number(fee_bps_per_side, "fee_bps_per_side", nonnegative=True), "f"),
        "slippage_bps_per_side": format(_number(slippage_bps_per_side, "slippage_bps_per_side", nonnegative=True), "f"),
        "max_setup_age_hours": max_setup_age_hours,
        "rules": market_research._rules(),
    }
    snapshots: list[dict[str, Any]] = []
    for index, candle in enumerate(hour_rows):
        asof = int(candle["close_time"]) + 1
        windows, integrity, active_events = _make_windows(
            asof, rows_by_interval, close_times_by_interval,
            event_records_by_interval, event_times_by_interval, symbol,
        )
        all_integrity_ok = all(item.get("status") == "complete" and not item.get("gaps") and not item.get("errors")
                               for item in integrity["intervals"].values())
        candidate = None
        if all(len(windows[interval]) >= 8 for interval in INTERVALS):
            metadata = {
                "symbol": symbol,
                "binance_server_time": asof,
                "binance_server_time_utc": _utc(asof),
                "sources": {"spot": SPOT_BASE, "futures": None},
                "data_integrity": integrity,
            }
            spot = [{
                "symbol": symbol, "price": str(candle["close"]),
                "captured_at_utc": _utc(asof), "source": SOURCE_SPOT,
            }]
            report = market_research.build_analysis_from_records(
                metadata, [dict(row) for interval in INTERVALS for row in windows[interval]],
                spot, active_events, config, archive_name="historical-asof",
                archive_sha256="", now=datetime.fromtimestamp(asof / 1000, timezone.utc),
            )
            scenario = report.get("scenario")
            if scenario and report.get("status") in {"候選做多", "候選做空"}:
                ob = report.get("evidence", {}).get("setup_order_block") or {}
                try:
                    candidate = {
                        "setup_id": scenario["entry_event_id"],
                        "target_event_id": scenario["target_event_id"],
                        "side": scenario["side"],
                        "signal_index": index,
                        "signal_time_ms": asof,
                        "setup_confirm_ms": int(ob["confirmed_at_close_time"]),
                        "setup_age_hours": float(report["checks"]["setup_freshness"]["age_hours"]),
                        "zone_low": str(scenario["entry_zone"]["low"]),
                        "zone_high": str(scenario["entry_zone"]["high"]),
                        "entry_reference": str(scenario["conservative_entry_reference"]),
                        "stop": str(scenario["stop_price"]),
                        "target": str(scenario["target_price"]),
                        "net_rr": str(scenario["risk_reward"]["net_reward_to_risk_ratio"]),
                    }
                except (KeyError, TypeError, ValueError):
                    candidate = None
        snapshots.append({"candidate": candidate, "integrity_ok": all_integrity_ok})
        if progress_every and (index + 1) % progress_every == 0:
            print(f"歷史策略重建：{index + 1:,}/{len(hour_rows):,} 根 1H K 線", file=sys.stderr)
    data_summary = {
        "symbol": symbol,
        "intervals": {
            interval: {
                "closed_candles": len(rows_by_interval[interval]),
                "start_utc": rows_by_interval[interval][0].get("open_time_utc") if rows_by_interval[interval] else None,
                "end_utc": rows_by_interval[interval][-1].get("close_time_utc") if rows_by_interval[interval] else None,
                "gaps": inspect_candle_integrity(rows_by_interval[interval], interval)["gaps"],
            }
            for interval in INTERVALS
        },
        "strategy": market_research.STRATEGY_VERSION,
        "historical_hourly_candles": len(hour_rows),
        "window_candles_per_interval": 200,
    }
    return hour_rows, snapshots, data_summary


def _candidate_allowed(candidate: dict[str, Any] | None, minimum_net_rr: float, max_setup_age_hours: int) -> bool:
    if not candidate:
        return False
    return (float(candidate["net_rr"]) >= minimum_net_rr
            and float(candidate["setup_age_hours"]) <= max_setup_age_hours)


def _fill_price(side: str, raw_price: Decimal, slippage_bps: Decimal, *, entry: bool) -> Decimal:
    slip = slippage_bps / Decimal(10000)
    if side == "long":
        factor = Decimal(1) + slip if entry else Decimal(1) - slip
    else:
        factor = Decimal(1) - slip if entry else Decimal(1) + slip
    return raw_price * factor


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["r_multiple"]) for row in trades]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_wins = sum(wins)
    gross_losses = -sum(losses)
    equity = peak = max_dd = 0.0
    current_losses = max_losses = 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if value < 0:
            current_losses += 1
            max_losses = max(max_losses, current_losses)
        else:
            current_losses = 0
    count = len(values)
    return {
        "trade_count": count,
        "win_rate_percent": round(100 * len(wins) / count, 4) if count else 0.0,
        "average_win_r": round(sum(wins) / len(wins), 8) if wins else 0.0,
        "average_loss_r": round(sum(losses) / len(losses), 8) if losses else 0.0,
        "expectancy_r": round(sum(values) / count, 8) if count else 0.0,
        "profit_factor": round(gross_wins / -sum(losses), 8) if losses else ("infinite" if wins else None),
        "max_drawdown_r": round(max_dd, 8),
        "max_consecutive_losses": max_losses,
    }


def _make_trade(position: dict[str, Any], raw_exit: Decimal, exit_time_ms: int,
                exit_reason: str, exit_index: int, fee_bps: Decimal, slip_bps: Decimal) -> dict[str, Any]:
    side = position["side"]
    raw_entry = position["raw_entry"]
    fee_rate = fee_bps / Decimal(10000)
    entry_fill = _fill_price(side, raw_entry, slip_bps, entry=True)
    exit_fill = _fill_price(side, raw_exit, slip_bps, entry=False)
    entry_fee = entry_fill * fee_rate
    exit_fee = exit_fill * fee_rate
    gross = exit_fill - entry_fill if side == "long" else entry_fill - exit_fill
    net = gross - entry_fee - exit_fee
    raw_stop = position["stop"]
    normal_stop_fill = _fill_price(side, raw_stop, slip_bps, entry=False)
    stop_fee = normal_stop_fill * fee_rate
    risk = abs(entry_fill - normal_stop_fill) + entry_fee + stop_fee
    r_value = net / risk if risk > 0 else Decimal("0")
    entry_index = position["entry_index"]
    bars_held = exit_index - entry_index + 1
    return {
        "setup_id": position["setup_id"],
        "target_event_id": position["target_event_id"],
        "direction": side,
        "signal_time_utc": _utc(int(position["signal_time_ms"])),
        "entry_time_utc": _utc(int(position["entry_time_ms"])),
        "exit_time_utc": _utc(exit_time_ms),
        "raw_entry_price": format(raw_entry, "f"),
        "entry_price": format(entry_fill, "f"),
        "exit_price": format(exit_fill, "f"),
        "stop_price": format(raw_stop, "f"),
        "target_price": format(position["target"], "f"),
        "exit_reason": exit_reason,
        "bars_held": bars_held,
        "gross_pnl_per_unit": format(gross, "f"),
        "fees_paid_per_unit": format(entry_fee + exit_fee, "f"),
        "net_pnl_per_unit": format(net, "f"),
        "r_multiple": format(r_value, ".8f"),
    }


def _exit_on_bar(position: dict[str, Any], bar: dict[str, Any]) -> tuple[Decimal, str] | None:
    side = position["side"]
    op = _number(bar["open"], "open", positive=True)
    high = _number(bar["high"], "high", positive=True)
    low = _number(bar["low"], "low", positive=True)
    stop = position["stop"]
    target = position["target"]
    if side == "long":
        if op <= stop:
            return op, "stop_gap"
        stop_hit, target_hit = low <= stop, high >= target
        if stop_hit:
            return stop, "stop"
        if target_hit:
            return target, "target"
    else:
        if op >= stop:
            return op, "stop_gap"
        stop_hit, target_hit = high >= stop, low <= target
        if stop_hit:
            return stop, "stop"
        if target_hit:
            return target, "target"
    return None


def _simulate_slice(
    rows: list[dict[str, Any]], snapshots: list[dict[str, Any]], start: int, end: int,
    *, minimum_net_rr: float, max_setup_age_hours: int,
    fee_bps: Decimal, slippage_bps: Decimal,
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    consumed_setups: set[str] = set()
    for index in range(start, end):
        bar = rows[index]
        prior = rows[index - 1] if index > start else None
        if prior is not None and int(bar["open_time"]) - int(prior["open_time"]) != INTERVAL_MILLISECONDS["1h"]:
            if position:
                close_time = int(prior["close_time"]) + 1
                trades.append(_make_trade(position, _number(prior["close"], "close", positive=True),
                                          close_time, "data_gap_exit", index - 1, fee_bps, slippage_bps))
                position = None
            pending = None
        current_snapshot = snapshots[index]
        if position:
            result = _exit_on_bar(position, bar)
            if result:
                raw_exit, reason = result
                trades.append(_make_trade(position, raw_exit, int(bar["close_time"]) + 1,
                                          reason, index, fee_bps, slippage_bps))
                position = None
            elif not current_snapshot.get("integrity_ok"):
                trades.append(_make_trade(position, _number(bar["close"], "close", positive=True),
                                          int(bar["close_time"]) + 1, "integrity_review_exit",
                                          index, fee_bps, slippage_bps))
                position = None
        elif pending:
            age_hours = (int(bar["open_time"]) - int(pending["setup_confirm_ms"])) / 3_600_000
            candidate_now = current_snapshot.get("candidate")
            if (age_hours > max_setup_age_hours or not current_snapshot.get("integrity_ok")):
                pending = None
            else:
                op = _number(bar["open"], "open", positive=True)
                high = _number(bar["high"], "high", positive=True)
                low = _number(bar["low"], "low", positive=True)
                zone_low = _number(pending["zone_low"], "zone_low", positive=True)
                zone_high = _number(pending["zone_high"], "zone_high", positive=True)
                stop = _number(pending["stop"], "stop", positive=True)
                target = _number(pending["target"], "target", positive=True)
                side = pending["side"]
                if (side == "long" and op <= stop) or (side == "short" and op >= stop):
                    pending = None
                else:
                    zone_touch = low <= zone_high and high >= zone_low
                    target_before_entry = (high >= target) if side == "long" else (low <= target)
                    if target_before_entry and not zone_touch:
                        pending = None
                    elif target_before_entry and zone_touch:
                        pending = None
                    elif zone_touch:
                        position = {
                            "setup_id": pending["setup_id"],
                            "target_event_id": pending["target_event_id"],
                            "side": side,
                            "signal_time_ms": int(pending["signal_time_ms"]),
                            "entry_time_ms": int(bar["open_time"]),
                            "entry_index": index,
                            "raw_entry": _number(pending["entry_reference"], "entry_reference", positive=True),
                            "stop": stop,
                            "target": target,
                        }
                        consumed_setups.add(pending["setup_id"])
                        pending = None
                        result = _exit_on_bar(position, bar)
                        if result:
                            raw_exit, reason = result
                            trades.append(_make_trade(position, raw_exit, int(bar["close_time"]) + 1,
                                                      reason, index, fee_bps, slippage_bps))
                            position = None
                    elif (not _candidate_allowed(candidate_now, minimum_net_rr, max_setup_age_hours)
                          or candidate_now.get("setup_id") != pending.get("setup_id")):
                        pending = None

        candidate = current_snapshot.get("candidate")
        if position is None and pending is None and current_snapshot.get("integrity_ok"):
            if _candidate_allowed(candidate, minimum_net_rr, max_setup_age_hours):
                setup_id = str(candidate["setup_id"])
                if setup_id not in consumed_setups:
                    pending = dict(candidate)
                    pending["signal_index"] = index
                    consumed_setups.add(setup_id)
    if position:
        last = rows[end - 1]
        trades.append(_make_trade(position, _number(last["close"], "close", positive=True),
                                  int(last["close_time"]) + 1, "split_or_segment_end",
                                  end - 1, fee_bps, slippage_bps))
    return trades


def _split_ranges(rows: list[dict[str, Any]], train_fraction: float, validation_fraction: float) -> dict[str, tuple[int, int]]:
    count = len(rows)
    cut1 = int(count * train_fraction)
    cut2 = int(count * (train_fraction + validation_fraction))
    return {"train": (0, cut1), "validation": (cut1, cut2), "test": (cut2, count)}


def run_configuration(
    rows: list[dict[str, Any]], snapshots: list[dict[str, Any]], *,
    minimum_net_rr: float, max_setup_age_hours: int,
    fee_bps_per_side: float, slippage_bps_per_side: float,
    train_fraction: float = 0.6, validation_fraction: float = 0.2,
    include_test: bool = True,
) -> dict[str, Any]:
    values = (minimum_net_rr, fee_bps_per_side, slippage_bps_per_side, train_fraction, validation_fraction)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("參數必須是有限數值。")
    if minimum_net_rr <= 0 or fee_bps_per_side < 0 or slippage_bps_per_side < 0:
        raise ValueError("最低淨 RR 必須大於 0，成本不可小於 0。")
    if type(max_setup_age_hours) is not int or max_setup_age_hours <= 0:
        raise ValueError("OB 最長有效時間必須是正整數小時。")
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1 or train_fraction + validation_fraction >= 1:
        raise ValueError("train/validation 切分比例無效。")
    bounds = _split_ranges(rows, train_fraction, validation_fraction)
    report: dict[str, Any] = {
        "parameters": {"minimum_net_rr": minimum_net_rr, "max_setup_age_hours": max_setup_age_hours,
                       "fee_bps_per_side": fee_bps_per_side, "slippage_bps_per_side": slippage_bps_per_side},
        "splits": {},
        "trades": [],
    }
    for name, (start, end) in bounds.items():
        if name == "test" and not include_test:
            report["splits"][name] = {
                "start_utc": _utc(int(rows[start]["open_time"])) if start < end else None,
                "end_exclusive_utc": _utc(int(rows[end - 1]["close_time"]) + 1) if start < end else None,
                "metrics": None,
                "status": "not_evaluated_during_parameter_search",
            }
            continue
        trades = _simulate_slice(
            rows, snapshots, start, end, minimum_net_rr=minimum_net_rr,
            max_setup_age_hours=max_setup_age_hours, fee_bps=Decimal(str(fee_bps_per_side)),
            slippage_bps=Decimal(str(slippage_bps_per_side)),
        )
        trades.sort(key=lambda trade: (trade["entry_time_utc"], trade["setup_id"]))
        if start < end:
            start_utc = _utc(int(rows[start]["open_time"]))
            end_utc = _utc(int(rows[end - 1]["close_time"]) + 1)
        else:
            start_utc = end_utc = None
        report["splits"][name] = {
            "start_utc": start_utc, "end_exclusive_utc": end_utc, "metrics": _metrics(trades),
        }
        report["trades"].extend({**trade, "split": name} for trade in trades)
    report["summary"] = _metrics(report["trades"])
    return report


def tune_parameters(
    rows: list[dict[str, Any]], snapshots: list[dict[str, Any]], *,
    rr_grid: list[float], age_grid: list[int], fee_bps_per_side: float,
    slippage_bps_per_side: float, minimum_validation_trades: int = 20,
    train_fraction: float = 0.6, validation_fraction: float = 0.2,
) -> dict[str, Any]:
    if not rr_grid or not age_grid or minimum_validation_trades < 0:
        raise ValueError("參數搜尋範圍不得為空，最低驗證交易數不可小於零。")
    if any(not math.isfinite(value) or value <= 0 for value in rr_grid):
        raise ValueError("RR 搜尋值必須都是大於零的有限數值。")
    if any(type(value) is not int or value <= 0 for value in age_grid):
        raise ValueError("OB 有效時間搜尋值必須是正整數小時。")
    unique_rr = sorted(set(rr_grid))
    unique_age = sorted(set(age_grid))
    candidates = []
    for rr in unique_rr:
        for age in unique_age:
            result = run_configuration(
                rows, snapshots, minimum_net_rr=rr, max_setup_age_hours=age,
                fee_bps_per_side=fee_bps_per_side, slippage_bps_per_side=slippage_bps_per_side,
                train_fraction=train_fraction, validation_fraction=validation_fraction,
                include_test=False,
            )
            validation = result["splits"]["validation"]["metrics"]
            candidates.append({
                "minimum_net_rr": rr, "max_setup_age_hours": age,
                "train": result["splits"]["train"]["metrics"],
                "validation": validation, "test": result["splits"]["test"]["metrics"],
                "eligible_for_selection": validation["trade_count"] >= minimum_validation_trades,
                "test_was_used_for_selection": False,
            })
    eligible = [row for row in candidates if row["eligible_for_selection"]]
    def validation_rank(row: dict[str, Any]) -> tuple[float, float, int]:
        profit_factor = row["validation"]["profit_factor"]
        if profit_factor == "infinite":
            pf_score = math.inf
        elif profit_factor is None:
            pf_score = -1.0
        else:
            pf_score = float(profit_factor)
        return (float(row["validation"]["expectancy_r"]), pf_score, int(row["validation"]["trade_count"]))

    ranked = sorted(eligible, key=validation_rank, reverse=True)
    selected = ranked[0] if ranked else None
    if selected is None:
        default_row = next((row for row in candidates if row["minimum_net_rr"] == 1.5 and row["max_setup_age_hours"] == 168), None)
        selected = default_row or (candidates[0] if candidates else None)
    return {
        "selection_rule": "只依 validation 的 expectancy R、profit factor、交易數排序；test 不參與選參數。若沒有任何設定達到最低驗證交易數，回報 fallback，不能宣稱完成有效調參。",
        "minimum_validation_trades": minimum_validation_trades,
        "selected_parameters": None if selected is None else {
            "minimum_net_rr": selected["minimum_net_rr"],
            "max_setup_age_hours": selected["max_setup_age_hours"],
        },
        "selection_status": ("selected_by_validation" if ranked else "insufficient_validation_trades_fallback"),
        "selected_validation_expectancy_positive": bool(selected and selected["validation"]["expectancy_r"] > 0),
        "candidates": candidates,
    }


def _load_grid(value: str, *, integers: bool = False) -> list[Any]:
    try:
        parts = [part.strip() for part in value.split(",") if part.strip()]
        values = [int(part) if integers else float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"搜尋範圍格式錯誤：{value}") from exc
    if not values:
        raise ValueError("搜尋範圍不可為空。")
    return values


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default) + "\n", encoding="utf-8")
    temp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0：下載 Binance 多週期歷史資料，回測目前正式策略並調整少量參數；研究用途，不下單。")
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download", help="下載或增量更新一個週期的 CSV")
    download.add_argument("symbol", nargs="?", default="BTCUSDT")
    download.add_argument("--interval", choices=INTERVALS, default="1h")
    download.add_argument("--start")
    download.add_argument("--end")
    download.add_argument("--data-dir", type=Path, default=Path("data/historical"))

    download_all = sub.add_parser("download-all", help="下載或增量更新 1d、4h、1h 三個週期")
    download_all.add_argument("symbol", nargs="?", default="BTCUSDT")
    download_all.add_argument("--start")
    download_all.add_argument("--end")
    download_all.add_argument("--data-dir", type=Path, default=Path("data/historical"))

    backtest = sub.add_parser("backtest", help="用單組參數回測正式多週期策略")
    backtest.add_argument("symbol", nargs="?", default="BTCUSDT")
    backtest.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    backtest.add_argument("--fee-bps-per-side", type=float, default=10.0)
    backtest.add_argument("--slippage-bps-per-side", type=float, default=5.0)
    backtest.add_argument("--minimum-net-rr", type=float, default=1.5)
    backtest.add_argument("--max-setup-age-hours", type=int, default=168)
    backtest.add_argument("--train-fraction", type=float, default=0.6)
    backtest.add_argument("--validation-fraction", type=float, default=0.2)
    backtest.add_argument("--output", type=Path)

    tune = sub.add_parser("tune", help="在 validation 區間比較預先定義的少量參數；test 僅最後報告")
    tune.add_argument("symbol", nargs="?", default="BTCUSDT")
    tune.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    tune.add_argument("--fee-bps-per-side", type=float, default=10.0)
    tune.add_argument("--slippage-bps-per-side", type=float, default=5.0)
    tune.add_argument("--rr-grid", default="1.25,1.5,1.75,2.0")
    tune.add_argument("--age-grid", default="72,168,240")
    tune.add_argument("--minimum-validation-trades", type=int, default=20)
    tune.add_argument("--train-fraction", type=float, default=0.6)
    tune.add_argument("--validation-fraction", type=float, default=0.2)
    tune.add_argument("--output", type=Path)

    args = parser.parse_args(argv)
    try:
        symbol = validate_symbol(args.symbol)
        if args.command in {"download", "download-all"}:
            intervals = INTERVALS if args.command == "download-all" else (args.interval,)
            results = []
            for interval in intervals:
                result = download_to_csv(symbol, interval, args.data_dir, start=args.start, end=args.end)
                results.append(result)
                print(f"{interval}: 已下載 {result['downloaded_count']:,} 根；檔案累積 {result['total_count']:,} 根")
                if result["data_integrity"]["gaps"]:
                    print(f"提醒：{interval} 有 {len(result['data_integrity']['gaps'])} 段歷史缺口，會保留並分段處理。")
            print(json.dumps({"symbol": symbol, "downloads": results}, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default))
            return 0

        if args.command == "backtest":
            base_min_rr = args.minimum_net_rr
            base_max_age = args.max_setup_age_hours
            rows, snapshots, data_summary = _prepare_snapshots(
                args.data_dir, symbol, fee_bps_per_side=args.fee_bps_per_side,
                slippage_bps_per_side=args.slippage_bps_per_side,
                minimum_net_rr=base_min_rr, max_setup_age_hours=base_max_age,
            )
            result = run_configuration(
                rows, snapshots, minimum_net_rr=base_min_rr, max_setup_age_hours=base_max_age,
                fee_bps_per_side=args.fee_bps_per_side, slippage_bps_per_side=args.slippage_bps_per_side,
                train_fraction=args.train_fraction, validation_fraction=args.validation_fraction,
            )
            output = args.output or args.data_dir / f"{symbol}_mtf_backtest.json"
            report = {"report_type": "formal_mtf_strategy_backtest", "report_version": 1,
                      "data": data_summary, **result,
                      "limitations": ["回測沿用正式 mtf-bos-ob-retest-v1 多週期結構、OB 回踩、止損與波段目標規則。",
                                      "僅用已收盤 K 線；訊號在收盤後建立，掛單下一根 1H K 線才生效。",
                                      "同根 K 線停損與目標同時觸及時停損優先；費率及滑價為明示假設。",
                                      "歷史 OHLC 無法還原盤中路徑、排隊成交、點差、流動性、資金費率或市場衝擊。"]}
            _atomic_json(output, report)
            print(json.dumps({"output": str(output), "data": data_summary,
                              "splits": {name: item["metrics"] for name, item in result["splits"].items()},
                              "parameters": result["parameters"]}, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default))
            return 0

        rr_grid = _load_grid(args.rr_grid)
        age_grid = _load_grid(args.age_grid, integers=True)
        rows, snapshots, data_summary = _prepare_snapshots(
            args.data_dir, symbol, fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
            minimum_net_rr=min(rr_grid), max_setup_age_hours=max(age_grid),
        )
        tuning = tune_parameters(
            rows, snapshots, rr_grid=rr_grid, age_grid=age_grid,
            fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
            minimum_validation_trades=args.minimum_validation_trades,
            train_fraction=args.train_fraction, validation_fraction=args.validation_fraction,
        )
        selected = tuning["selected_parameters"]
        selected_report = None
        if selected:
            selected_report = run_configuration(
                rows, snapshots, minimum_net_rr=selected["minimum_net_rr"],
                max_setup_age_hours=selected["max_setup_age_hours"],
                fee_bps_per_side=args.fee_bps_per_side,
                slippage_bps_per_side=args.slippage_bps_per_side,
                train_fraction=args.train_fraction, validation_fraction=args.validation_fraction,
            )
        output = args.output or args.data_dir / f"{symbol}_mtf_tuning.json"
        report = {"report_type": "formal_mtf_strategy_parameter_tuning", "report_version": 1,
                  "data": data_summary, "tuning": tuning, "selected_configuration": selected_report,
                  "limitations": ["參數只在預先指定的 RR 與 OB 有效時間範圍內比較；未調整市場結構定義。",
                                  "validation 用於選參數，test 不參與選參數，只在選定後做一次樣本外報告。",
                                  "若驗證交易數不足或 expectancy 不為正，不應宣稱參數已找到優勢。",
                                  "費率和滑價不納入搜尋，必須依預期交易商品與實際交易成本另行設定。"]}
        _atomic_json(output, report)
        print(json.dumps({"output": str(output), "data": data_summary, "tuning": tuning,
                          "selected_test": None if not selected_report else selected_report["splits"]["test"]["metrics"]},
                         ensure_ascii=False, indent=2, allow_nan=False, default=_json_default))
        return 0
    except (BinanceAPIError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
