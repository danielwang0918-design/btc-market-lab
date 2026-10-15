#!/usr/bin/env python3
"""Download, align, and inspect public Binance USDⓈ-M derivatives data.

This module is a research collector only. It does not infer trader direction from
open interest or funding, and its liquidation heatmap aggregates observed stream
snapshots rather than estimating untriggered liquidation prices.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import os
import random
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import binance_market_data as base

VERSION = "binance-usdm-derivatives-research-v1"
FUTURES = base.FUTURES_BASE
PERIOD_MS = {
    "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
    "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "12h": 43_200_000, "1d": 86_400_000,
}
SERIES = {
    "open_interest": ("/futures/data/openInterestHist", 500, "30_days"),
    "funding": ("/fapi/v1/fundingRate", 1000, "exchange_history"),
    "global_long_short": ("/futures/data/globalLongShortAccountRatio", 500, "30_days"),
    "taker_buy_sell": ("/futures/data/takerBuySellVol", 500, "30_days"),
    "taker_sentiment": ("/futures/data/takerlongshortRatio", 500, "30_days"),
}
CSV_FIELDS = {
    "open_interest": ["symbol", "period", "timestamp", "timestamp_utc", "sumOpenInterest", "sumOpenInterestValue", "source"],
    "funding": ["symbol", "fundingTime", "timestamp_utc", "fundingRate", "markPrice", "source"],
    "global_long_short": ["symbol", "period", "timestamp", "timestamp_utc", "longShortRatio", "longAccount", "shortAccount", "source"],
    "taker_buy_sell": ["symbol", "period", "timestamp", "timestamp_utc", "buySellRatio", "buyVol", "sellVol", "source"],
    "taker_sentiment": ["symbol", "period", "timestamp", "timestamp_utc", "buySellRatio", "buyVol", "sellVol", "source"],
    "futures_klines": ["symbol", "interval", "open_time", "open_time_utc", "open", "high", "low", "close", "volume", "close_time", "close_time_utc", "quote_volume", "trade_count", "taker_buy_base_volume", "taker_buy_quote_volume", "is_closed", "source"],
    "liquidations": ["event_time", "event_time_utc", "symbol", "side", "liquidated_side", "order_type", "order_status", "original_quantity", "average_price", "executed_quantity", "executed_notional", "source", "raw_json"],
}
DATA_API_LIMIT_NOTE = "OI/多空比/taker 歷史統計的 Binance 公開資料端點目前僅能查最近 30 天；單次請求上限 500 筆。"


class DerivativesError(Exception):
    """Invalid or unavailable derivatives market data."""


def utc_iso(ms: int | float | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).isoformat()


def parse_utc_date(value: str, *, end: bool = False) -> int:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"日期請使用 YYYY-MM-DD：{value}") from exc
    dt = datetime.combine(parsed + (timedelta(days=1) if end else timedelta()), day_time.min, timezone.utc)
    return int(dt.timestamp() * 1000) - (1 if end else 0)


def _num(value: Any, label: str, *, allow_zero: bool = True) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DerivativesError(f"{label} 不是有效數值。") from exc
    if not math.isfinite(result) or (not allow_zero and result <= 0) or (allow_zero and result < 0):
        raise DerivativesError(f"{label} 數值超出有效範圍。")
    return result


def normalize_row(feed: str, item: Any, symbol: str, period: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise DerivativesError(f"{feed} 回應不是物件。")
    if feed == "funding":
        timestamp = item.get("fundingTime")
        rate = _num(item.get("fundingRate"), "fundingRate")
        if type(timestamp) is not int or timestamp < 0:
            raise DerivativesError("fundingTime 必須是非負整數毫秒。")
        return {"symbol": symbol, "fundingTime": timestamp, "timestamp_utc": utc_iso(timestamp),
                "fundingRate": item.get("fundingRate"), "markPrice": item.get("markPrice", ""), "source": base.SOURCE_FUTURES}
    timestamp = item.get("timestamp")
    if type(timestamp) is not int or timestamp < 0:
        raise DerivativesError(f"{feed} timestamp 必須是非負整數毫秒。")
    common = {"symbol": symbol, "period": period, "timestamp": timestamp,
              "timestamp_utc": utc_iso(timestamp), "source": base.SOURCE_FUTURES}
    if feed == "open_interest":
        oi = item.get("sumOpenInterest")
        oi_value = item.get("sumOpenInterestValue")
        _num(oi, "sumOpenInterest")
        _num(oi_value, "sumOpenInterestValue")
        return {**common, "sumOpenInterest": oi, "sumOpenInterestValue": oi_value}
    if feed == "global_long_short":
        ratio = _num(item.get("longShortRatio"), "longShortRatio", allow_zero=False)
        long_account = _num(item.get("longAccount"), "longAccount")
        short_account = _num(item.get("shortAccount"), "shortAccount")
        if abs(long_account + short_account - 1.0) > 0.02:
            raise DerivativesError("longAccount + shortAccount 應接近 1。")
        return {**common, "longShortRatio": str(ratio), "longAccount": item.get("longAccount"), "shortAccount": item.get("shortAccount")}
    if feed in {"taker_buy_sell", "taker_sentiment"}:
        buy = _num(item.get("buyVol"), "buyVol")
        sell = _num(item.get("sellVol"), "sellVol")
        ratio = item.get("buySellRatio")
        if ratio in (None, ""):
            ratio = str(buy / sell) if sell else ""
        else:
            _num(ratio, "buySellRatio")
        return {**common, "buySellRatio": ratio, "buyVol": item.get("buyVol"), "sellVol": item.get("sellVol")}
    raise DerivativesError(f"未知資料序列：{feed}")


def validate_futures_kline(item: Any, symbol: str, interval: str, server_ms: int) -> dict[str, Any]:
    if not isinstance(item, list) or len(item) < 11:
        raise DerivativesError("合約 K 線回應欄位不足。")
    try:
        opened, closed = int(item[0]), int(item[6])
        prices = [_num(item[i], f"K 線欄位 {i}", allow_zero=False) for i in (1, 2, 3, 4)]
        volume, quote_volume = _num(item[5], "volume"), _num(item[7], "quote_volume")
        trades = int(item[8])
        taker_base, taker_quote = _num(item[9], "taker_buy_base_volume"), _num(item[10], "taker_buy_quote_volume")
    except (ValueError, TypeError, IndexError) as exc:
        raise DerivativesError("合約 K 線含無效欄位。") from exc
    step = base.INTERVAL_MILLISECONDS.get(interval)
    if not step or opened % step or closed < opened or opened > server_ms:
        raise DerivativesError("合約 K 線時間對齊或收盤時間無效。")
    op, high, low, close = prices
    if high < max(op, close) or low > min(op, close) or low > high:
        raise DerivativesError("合約 K 線 OHLC 關係錯誤。")
    return {"symbol": symbol, "interval": interval, "open_time": opened, "open_time_utc": utc_iso(opened),
            "open": item[1], "high": item[2], "low": item[3], "close": item[4], "volume": item[5],
            "close_time": closed, "close_time_utc": utc_iso(closed), "quote_volume": item[7],
            "trade_count": trades, "taker_buy_base_volume": item[9], "taker_buy_quote_volume": item[10],
            "is_closed": str(closed < server_ms).lower(), "source": base.SOURCE_FUTURES}


def merge_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str], key: str) -> int:
    existing: dict[str, dict[str, Any]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                existing[str(row[key])] = row
    for row in rows:
        existing[str(row[key])] = row
    ordered = sorted(existing.values(), key=lambda row: int(row[key]))
    base.write_csv(path, ordered, fields)
    return len(ordered)


def fetch_history(client: base.BinanceClient, feed: str, symbol: str, period: str,
                  start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    path, max_limit, _ = SERIES[feed]
    limit = max_limit
    if feed == "funding":
        cursor = start_ms
        params_base: dict[str, Any] = {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": limit}
        time_field = "fundingTime"
    else:
        if period not in PERIOD_MS:
            raise ValueError(f"不支援資料週期：{period}")
        params_base = {"symbol": symbol, "period": period, "startTime": start_ms, "endTime": end_ms, "limit": limit}
        time_field = "timestamp"
        cursor = start_ms
    collected: dict[int, dict[str, Any]] = {}
    last_cursor = -1
    while cursor <= end_ms:
        params = {**params_base, "startTime": cursor}
        raw = client.get_json(FUTURES, path, params)
        if not isinstance(raw, list):
            raise DerivativesError(f"{feed} API 回應不是陣列。")
        if not raw:
            break
        page: list[dict[str, Any]] = []
        for item in raw:
            row = normalize_row(feed, item, symbol, period)
            timestamp = int(row[time_field])
            if start_ms <= timestamp <= end_ms:
                page.append(row)
                collected[timestamp] = row
        max_timestamp = max(int(row[time_field]) for row in page) if page else max(int(item[time_field]) for item in raw)
        next_cursor = max_timestamp + 1
        if next_cursor <= cursor or next_cursor <= last_cursor:
            raise DerivativesError(f"{feed} API 分頁時間未前進，已停止以避免重複請求。")
        last_cursor = cursor
        cursor = next_cursor
        if len(raw) < limit:
            break
    return [collected[key] for key in sorted(collected)]


def fetch_futures_klines(client: base.BinanceClient, symbol: str, interval: str,
                         start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    if interval not in base.INTERVAL_MILLISECONDS:
        raise ValueError(f"不支援合約 K 線週期：{interval}")
    step = base.INTERVAL_MILLISECONDS[interval]
    cursor = start_ms - (start_ms % step)
    server = client.get_json(FUTURES, "/fapi/v1/time")
    if not isinstance(server, dict) or type(server.get("serverTime")) is not int:
        raise DerivativesError("Binance Futures server time 回應格式錯誤。")
    rows: dict[int, dict[str, Any]] = {}
    last = -1
    while cursor <= end_ms:
        raw = client.get_json(FUTURES, "/fapi/v1/klines", {"symbol": symbol, "interval": interval,
                          "startTime": cursor, "endTime": end_ms, "limit": 1500})
        if not isinstance(raw, list):
            raise DerivativesError("合約 K 線 API 回應不是陣列。")
        if not raw:
            break
        for item in raw:
            row = validate_futures_kline(item, symbol, interval, server["serverTime"])
            if start_ms <= row["open_time"] <= end_ms:
                rows[row["open_time"]] = row
        newest = max(int(item[0]) for item in raw)
        if newest <= cursor or newest <= last:
            raise DerivativesError("合約 K 線分頁時間未前進。")
        last, cursor = cursor, newest + step
        if len(raw) < 1500:
            break
    return [rows[key] for key in sorted(rows)]


def download(symbol: str, start: str, end: str | None, period: str, interval: str,
             output_dir: Path, *, client: base.BinanceClient | None = None) -> dict[str, Any]:
    symbol = base.validate_symbol(symbol)
    if period not in PERIOD_MS:
        raise ValueError("--period 必須是 Binance 支援的週期。")
    start_ms = parse_utc_date(start)
    end_ms = parse_utc_date(end, end=True) if end else int(time.time() * 1000)
    if start_ms > end_ms:
        raise ValueError("開始日期必須早於或等於結束日期。")
    client = client or base.BinanceClient()
    output_dir.mkdir(parents=True, exist_ok=True)
    endpoints: dict[str, Any] = {}
    coverage_floor = int(time.time() * 1000) - 30 * 86_400_000
    for feed in SERIES:
        fetch_start = max(start_ms, coverage_floor) if SERIES[feed][2] == "30_days" else start_ms
        status: dict[str, Any] = {"requested_start_utc": utc_iso(start_ms), "effective_start_utc": utc_iso(fetch_start),
                                  "requested_end_utc": utc_iso(end_ms), "history_limit": SERIES[feed][2]}
        if fetch_start > end_ms:
            rows = []
            status.update({"status": "outside_retention_window", "row_count_downloaded": 0})
        else:
            try:
                rows = fetch_history(client, feed, symbol, period, fetch_start, end_ms)
                status.update({"status": "available" if rows else "empty", "row_count_downloaded": len(rows),
                               "first_timestamp_utc": utc_iso(rows[0].get("timestamp", rows[0].get("fundingTime"))) if rows else None,
                               "last_timestamp_utc": utc_iso(rows[-1].get("timestamp", rows[-1].get("fundingTime"))) if rows else None})
            except (base.BinanceAPIError, DerivativesError, ValueError, TypeError) as exc:
                rows = []
                status.update({"status": "error", "error": str(exc), "row_count_downloaded": 0})
        key = "fundingTime" if feed == "funding" else "timestamp"
        stored = merge_csv(output_dir / f"{symbol}_{feed}_{period}.csv", rows, CSV_FIELDS[feed], key)
        status["row_count_persisted"] = stored
        endpoints[feed] = status
    try:
        candles = fetch_futures_klines(client, symbol, interval, start_ms, end_ms)
        candle_status = {"status": "available" if candles else "empty", "row_count_downloaded": len(candles),
                         "first_open_utc": candles[0]["open_time_utc"] if candles else None,
                         "last_close_utc": candles[-1]["close_time_utc"] if candles else None}
    except (base.BinanceAPIError, DerivativesError, ValueError, TypeError) as exc:
        candles, candle_status = [], {"status": "error", "error": str(exc), "row_count_downloaded": 0}
    candle_count = merge_csv(output_dir / f"{symbol}_futures_klines_{interval}.csv", candles, CSV_FIELDS["futures_klines"], "open_time")
    metadata = {"version": VERSION, "symbol": symbol, "contract_market": "Binance USDⓈ-M perpetual market data",
                "period": period, "price_interval": interval, "requested_start_utc": utc_iso(start_ms),
                "requested_end_utc": utc_iso(end_ms), "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                "sources": {"rest": FUTURES, "documentation": "https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data"},
                "series": endpoints, "futures_klines": {**candle_status, "row_count_persisted": candle_count},
                "limits": {"30_day_series": DATA_API_LIMIT_NOTE,
                           "liquidation_history": "Binance public REST has no historical all-user liquidation endpoint; use the live public forceOrder stream collector. No historical heatmap is fabricated.",
                           "interpretation": "OI, funding, ratios, and taker flow describe different measurements; none alone identifies market-wide long or short positioning."}}
    (output_dir / f"{symbol}_derivatives_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _percent_change(now: str | float | None, before: str | float | None) -> float | None:
    if now in (None, "") or before in (None, ""):
        return None
    previous = float(before)
    return (float(now) / previous - 1) * 100 if previous else None


def _past_percentile_threshold(values: list[tuple[int, float]], timestamp: int, window_ms: int,
                               minimum: int = 30, percentile: float = .95) -> float | None:
    history = sorted(abs(value) for t, value in values if timestamp - window_ms <= t < timestamp)
    if len(history) < minimum:
        return None
    # Nearest-rank percentile, stable and dependency-free.
    index = max(0, math.ceil(percentile * len(history)) - 1)
    return history[index]


def _asof_price(candles: list[dict[str, str]], timestamp: int, interval_ms: int) -> dict[str, str] | None:
    # A candle is usable only after its close. Never attach a price from a future bar.
    candidates = [row for row in candles if row.get("is_closed") == "true" and int(row["close_time"]) <= timestamp]
    if not candidates:
        return None
    row = candidates[-1]
    if timestamp - int(row["close_time"]) > interval_ms:
        return None
    return row


def analyze(symbol: str, data_dir: Path, interval: str = "1h", period: str = "1h") -> dict[str, Any]:
    symbol = base.validate_symbol(symbol)
    candles = _read_csv(data_dir / f"{symbol}_futures_klines_{interval}.csv")
    oi_rows = _read_csv(data_dir / f"{symbol}_open_interest_{period}.csv")
    funding_rows = _read_csv(data_dir / f"{symbol}_funding_{period}.csv")
    global_rows = _read_csv(data_dir / f"{symbol}_global_long_short_{period}.csv")
    taker_rows = _read_csv(data_dir / f"{symbol}_taker_buy_sell_{period}.csv")
    sentiment_rows = _read_csv(data_dir / f"{symbol}_taker_sentiment_{period}.csv")
    interval_ms = base.INTERVAL_MILLISECONDS[interval]
    def time_key(row: dict[str, str]) -> int:
        return int(row.get("timestamp") or row.get("fundingTime") or 0)
    candles.sort(key=lambda row: int(row["open_time"]))
    for rows in (oi_rows, funding_rows, global_rows, taker_rows, sentiment_rows):
        rows.sort(key=time_key)
    by_time = [{time_key(row): row for row in rows} for rows in (global_rows, taker_rows, sentiment_rows)]
    aligned: list[dict[str, Any]] = []
    previous_oi: dict[str, str] | None = None
    previous_price: dict[str, str] | None = None
    for row in oi_rows:
        ts = time_key(row)
        candle = _asof_price(candles, ts, interval_ms)
        if candle is None:
            continue
        current_price = candle["close"]
        extra = [mapping.get(ts, {}) for mapping in by_time]
        oi_delta = _percent_change(row.get("sumOpenInterestValue"), previous_oi.get("sumOpenInterestValue") if previous_oi else None)
        price_delta = _percent_change(current_price, previous_price.get("close") if previous_price else None)
        if oi_delta is None or price_delta is None:
            quadrant = "insufficient_previous_observation"
        elif oi_delta > 0 and price_delta > 0:
            quadrant = "price_up_oi_up"
        elif oi_delta > 0 and price_delta < 0:
            quadrant = "price_down_oi_up"
        elif oi_delta < 0 and price_delta > 0:
            quadrant = "price_up_oi_down"
        elif oi_delta < 0 and price_delta < 0:
            quadrant = "price_down_oi_down"
        else:
            quadrant = "mixed_or_flat"
        aligned.append({"symbol": symbol, "timestamp": ts, "timestamp_utc": utc_iso(ts),
                        "price_close_time": candle["close_time"], "price_close_time_utc": candle["close_time_utc"],
                        "price_close": current_price, "oi_contracts": row.get("sumOpenInterest"),
                        "oi_value_usdt": row.get("sumOpenInterestValue"), "oi_change_pct": oi_delta,
                        "price_change_pct": price_delta, "price_oi_state": quadrant,
                        "global_long_short_ratio": extra[0].get("longShortRatio"),
                        "global_long_account": extra[0].get("longAccount"), "global_short_account": extra[0].get("shortAccount"),
                        "taker_buy_volume": extra[1].get("buyVol"), "taker_sell_volume": extra[1].get("sellVol"),
                        "taker_buy_sell_ratio": extra[1].get("buySellRatio"),
                        "taker_buy_sell_position_ratio": extra[2].get("buySellRatio"),
                        "interpretation_note": "描述同步變化，不由單一欄位推斷全市場多空部位。"})
        previous_oi, previous_price = row, candle
    funding_values = [(time_key(row), float(row["fundingRate"])) for row in funding_rows if row.get("fundingRate") not in (None, "")]
    funding_analysis: list[dict[str, Any]] = []
    previous_funding_price: dict[str, str] | None = None
    for row in funding_rows:
        ts = time_key(row)
        funding = float(row["fundingRate"])
        threshold = _past_percentile_threshold(funding_values, ts, 30 * 86_400_000)
        candle = _asof_price(candles, ts, interval_ms)
        mark = float(row["markPrice"]) if row.get("markPrice") else None
        funding_price_change = _percent_change(candle["close"], previous_funding_price.get("close") if candle and previous_funding_price else None)
        if threshold is None:
            extreme = "insufficient_history"
        elif abs(funding) >= threshold:
            extreme = "extreme_positive" if funding > 0 else "extreme_negative" if funding < 0 else "extreme_zero"
        else:
            extreme = "within_past_30d_range"
        funding_analysis.append({"symbol": symbol, "funding_time": ts, "funding_time_utc": utc_iso(ts),
                                "funding_rate": funding, "funding_abs_past_30d_p95": threshold,
                                "funding_extreme_status": extreme, "mark_price": mark,
                                "price_close": candle["close"] if candle else None,
                                "price_change_since_prior_funding_pct": funding_price_change,
                                "method": "絕對資金費率與此前 30 日資料的第 95 百分位比較；最少 30 個較早樣本，不使用未來值。",
                                "interpretation_note": "資金費率方向反映資金費率計算方向，不等同市場淨多/淨空持倉。"})
        if candle:
            previous_funding_price = candle
    out_rows = [{k: ("" if value is None else value) for k, value in row.items()} for row in aligned]
    base.write_csv(data_dir / f"{symbol}_derivatives_aligned_{period}.csv", out_rows, list(aligned[0].keys()) if aligned else ["symbol", "timestamp", "timestamp_utc"])
    fund_rows = [{k: ("" if value is None else value) for k, value in row.items()} for row in funding_analysis]
    base.write_csv(data_dir / f"{symbol}_funding_analysis.csv", fund_rows, list(funding_analysis[0].keys()) if funding_analysis else ["symbol", "funding_time", "funding_rate", "funding_extreme_status"])
    summary = {"version": VERSION, "symbol": symbol, "period": period,
               "aligned_observations": len(aligned), "funding_observations": len(funding_analysis),
               "price_oi_state_counts": {state: sum(row["price_oi_state"] == state for row in aligned)
                                         for state in sorted({row["price_oi_state"] for row in aligned})},
               "funding_extreme_counts": {state: sum(row["funding_extreme_status"] == state for row in funding_analysis)
                                          for state in sorted({row["funding_extreme_status"] for row in funding_analysis})},
               "rules": {"price_alignment": "最近一根 close_time 不晚於衍生品時間戳的已收盤合約 K 線；最多容許一個價格週期延遲。",
                         "oi_change": "相鄰已回傳 OI 統計點的百分比變化；缺口不插值。OI 使用 USDT 名目價值欄位做跨時比較。",
                         "funding_extreme": "過去 30 日絕對資金費率第 95 百分位，至少 30 個先前樣本。",
                         "no_direction_inference": "分類只描述價格、OI 和 taker flow 的同期變化；不代表新開多單/空單，因 OI 每一份合約同時有多方與空方。"},
               "data_limitations": [DATA_API_LIMIT_NOTE,
                                    "多空比按帳戶比例，不是資金比例；taker buy/sell 是主動成交方向，不是持倉方向。",
                                    "交易所公開強平 WebSocket 每個 symbol 每秒最多推送一筆快照，不是全部強平訂單。",
                                    "清算熱區只可由本機持續記錄的實際強平事件聚合；不估算尚未觸發的倉位清算價。"]}
    (data_dir / f"{symbol}_derivatives_analysis.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def normalize_liquidation(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("data", payload)
    order = event.get("o") if isinstance(event, dict) else None
    if not isinstance(order, dict):
        raise DerivativesError("強平事件格式缺少 order 物件。")
    symbol, side = str(order.get("s", "")).upper(), str(order.get("S", "")).upper()
    timestamp = order.get("T", event.get("E"))
    if not symbol or side not in {"BUY", "SELL"} or type(timestamp) is not int:
        raise DerivativesError("強平事件的交易對、方向或時間戳無效。")
    qty = _num(order.get("z", order.get("q")), "強平已成交數量")
    price = _num(order.get("ap", order.get("p")), "強平成交均價", allow_zero=False)
    return {"event_time": timestamp, "event_time_utc": utc_iso(timestamp), "symbol": symbol,
            "side": side, "liquidated_side": "long" if side == "SELL" else "short",
            "order_type": order.get("o", ""), "order_status": order.get("X", ""),
            "original_quantity": order.get("q", ""), "average_price": str(price), "executed_quantity": str(qty),
            "executed_notional": str(price * qty), "source": "Binance USDⓈ-M forceOrder WebSocket snapshot",
            "raw_json": json.dumps(payload, separators=(",", ":"), sort_keys=True)}


def liquidation_heatmap(rows: list[dict[str, Any]], price_bin: float, time_bucket_hours: int = 24) -> list[dict[str, Any]]:
    if not math.isfinite(price_bin) or price_bin <= 0:
        raise ValueError("價格分箱寬度必須是正數。")
    if type(time_bucket_hours) is not int or time_bucket_hours <= 0:
        raise ValueError("時間分箱小時數必須是正整數。")
    bucket_ms = time_bucket_hours * 3_600_000
    bins: dict[tuple[str, int, int], dict[str, Any]] = {}
    for row in rows:
        price = _num(row.get("average_price"), "強平成交均價", allow_zero=False)
        qty = _num(row.get("executed_quantity"), "強平數量")
        timestamp = int(row["event_time"])
        time_bucket = timestamp - (timestamp % bucket_ms)
        key = (str(row["liquidated_side"]), math.floor(price / price_bin), time_bucket)
        item = bins.setdefault(key, {"liquidated_side": key[0], "bin_index": key[1],
                                     "price_bin_low": key[1] * price_bin,
                                     "price_bin_high": (key[1] + 1) * price_bin,
                                     "time_bucket_start": key[2], "time_bucket_start_utc": utc_iso(key[2]),
                                     "time_bucket_hours": time_bucket_hours,
                                     "event_count": 0, "executed_quantity": 0.0,
                                     "executed_notional": 0.0, "first_event_time": timestamp,
                                     "last_event_time": timestamp})
        item["event_count"] += 1
        item["executed_quantity"] += qty
        item["executed_notional"] += price * qty
        item["first_event_time"] = min(item["first_event_time"], timestamp)
        item["last_event_time"] = max(item["last_event_time"], timestamp)
    return sorted(bins.values(), key=lambda row: (row["liquidated_side"], row["time_bucket_start"], row["price_bin_low"]))


def _recv_exact(sock: ssl.SSLSocket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        data = sock.recv(length - len(chunks))
        if not data:
            raise ConnectionError("WebSocket peer closed connection.")
        chunks.extend(data)
    return bytes(chunks)


def _send_ws_frame(sock: ssl.SSLSocket, opcode: int, payload: bytes = b"") -> None:
    mask = os.urandom(4)
    length = len(payload)
    header = bytes([0x80 | opcode, 0x80 | min(length, 125)]) if length <= 125 else (
        bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", length) if length < 65536 else
        bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", length))
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    sock.sendall(header + mask + masked)


def _websocket_message(sock: ssl.SSLSocket) -> tuple[int, bytes]:
    first, second = _recv_exact(sock, 2)
    opcode, length = first & 0x0F, second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > 8_000_000:
        raise DerivativesError("WebSocket 訊息超過安全大小限制。")
    mask = _recv_exact(sock, 4) if second & 0x80 else b""
    payload = _recv_exact(sock, length)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, payload


def _connect_liquidation_stream(symbol: str, timeout: float = 60) -> ssl.SSLSocket:
    host = "fstream.binance.com"
    path = f"/market/ws/{symbol.lower()}@forceOrder"
    raw = socket.create_connection((host, 443), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: btc-market-lab/1.0\r\n\r\n")
    sock.sendall(request.encode("ascii"))
    response = bytearray()
    while not response.endswith(b"\r\n\r\n"):
        part = sock.recv(1)
        if not part or len(response) > 16_384:
            sock.close()
            raise ConnectionError("WebSocket handshake response incomplete.")
        response.extend(part)
    head = bytes(response).split(b"\r\n\r\n", 1)[0].decode("latin1")
    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
    expected_accept = f"sec-websocket-accept: {accept}".lower()
    if not head.startswith("HTTP/1.1 101") or expected_accept not in head.lower():
        sock.close()
        raise ConnectionError(f"WebSocket handshake failed: {head.splitlines()[0] if head else 'no response'}")
    return sock


def record_liquidations(symbol: str, output: Path, duration_seconds: float | None = None,
                        connector: Any = _connect_liquidation_stream) -> int:
    """Record observed liquidation snapshots to JSONL and normalized CSV."""
    symbol = base.validate_symbol(symbol)
    output.mkdir(parents=True, exist_ok=True)
    jsonl = output / f"{symbol}_liquidations.jsonl"
    csv_path = output / f"{symbol}_liquidations.csv"
    seen = set()
    for old in _read_csv(csv_path):
        seen.add((old.get("event_time"), old.get("symbol"), old.get("side"), old.get("average_price"), old.get("executed_quantity"), old.get("order_type")))
    started = time.monotonic()
    count = 0
    delay = 1.0
    while duration_seconds is None or time.monotonic() - started < duration_seconds:
        sock: ssl.SSLSocket | None = None
        try:
            sock = connector(symbol)
            sock.settimeout(60)
            delay = 1.0
            while duration_seconds is None or time.monotonic() - started < duration_seconds:
                try:
                    opcode, payload = _websocket_message(sock)
                except socket.timeout:
                    continue
                if opcode == 8:
                    break
                if opcode == 9:
                    _send_ws_frame(sock, 10, payload)
                    continue
                if opcode != 1:
                    continue
                parsed = normalize_liquidation(json.loads(payload.decode("utf-8")))
                if parsed["symbol"] != symbol:
                    continue
                identity = (str(parsed["event_time"]), parsed["symbol"], parsed["side"], parsed["average_price"], parsed["executed_quantity"], parsed["order_type"])
                if identity in seen:
                    continue
                seen.add(identity)
                with jsonl.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(parsed, ensure_ascii=False) + "\n")
                write_header = not csv_path.exists() or csv_path.stat().st_size == 0
                with csv_path.open("a", newline="", encoding="utf-8-sig" if write_header else "utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS["liquidations"], extrasaction="ignore")
                    if write_header:
                        writer.writeheader()
                    writer.writerow(parsed)
                count += 1
            if sock:
                sock.close()
        except KeyboardInterrupt:
            if sock:
                sock.close()
            break
        except (OSError, ssl.SSLError, ConnectionError, ValueError, json.JSONDecodeError, DerivativesError) as exc:
            if sock:
                sock.close()
            if duration_seconds is not None and time.monotonic() - started >= duration_seconds:
                break
            print(f"強平資料串流中斷：{exc}；{delay:.0f} 秒後重新連線。", file=sys.stderr)
            time.sleep(delay + random.random() * 0.2)
            delay = min(delay * 2, 30)
    return count


def analyze_liquidations(symbol: str, data_dir: Path, price_bin: float = 100.0) -> dict[str, Any]:
    symbol = base.validate_symbol(symbol)
    path = data_dir / f"{symbol}_liquidations.csv"
    rows = [row for row in _read_csv(path) if row.get("symbol", "").upper() == symbol]
    heat = liquidation_heatmap(rows, price_bin)
    for row in heat:
        row["first_event_time_utc"] = utc_iso(row["first_event_time"])
        row["last_event_time_utc"] = utc_iso(row["last_event_time"])
        row["price_bin_usd"] = price_bin
        row["meaning"] = "已觀測強平事件聚集；不是尚未觸發清算價或未來必達區域。"
    columns = list(heat[0].keys()) if heat else ["liquidated_side", "price_bin_low", "price_bin_high", "event_count", "executed_quantity", "executed_notional"]
    base.write_csv(data_dir / f"{symbol}_liquidation_heatmap_observed.csv", heat, columns)
    result = {"symbol": symbol, "source_rows": len(rows), "price_bin_usd": price_bin,
              "heatmap_bins": len(heat), "liquidated_side_event_count": {
                  side: sum(int(row["event_count"]) for row in heat if row["liquidated_side"] == side) for side in ("long", "short")},
              "definition": "依 forceOrder 快照的已成交均價分箱，分開統計被清算多方/空方數量與名目值。",
              "limitations": ["Binance 每個交易對每秒最多推一筆強平快照，資料可能低估事件數量與成交額。",
                              "熱區僅聚合本機開始記錄後觀測到的事件；沒有歷史完整資料，也不預測未觸發清算水位。"]}
    (data_dir / f"{symbol}_liquidation_heatmap_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="擷取和分析 Binance USDⓈ-M BTC 衍生品市場資料（唯讀研究工具）。")
    sub = parser.add_subparsers(dest="command", required=True)
    down = sub.add_parser("download", help="下載歷史衍生品指標與合約 K 線")
    down.add_argument("symbol")
    down.add_argument("--start", required=True, help="UTC 起始日 YYYY-MM-DD")
    down.add_argument("--end", help="UTC 結束日 YYYY-MM-DD（包含整日；預設現在）")
    down.add_argument("--period", choices=sorted(PERIOD_MS), default="1h")
    down.add_argument("--interval", choices=sorted(base.INTERVAL_MILLISECONDS), default="1h")
    down.add_argument("--output-dir", type=Path, default=Path("data/derivatives"))
    study = sub.add_parser("analyze", help="比較 OI、價格、funding、多空比和 taker 流")
    study.add_argument("symbol")
    study.add_argument("--period", choices=sorted(PERIOD_MS), default="1h")
    study.add_argument("--interval", choices=sorted(base.INTERVAL_MILLISECONDS), default="1h")
    study.add_argument("--data-dir", type=Path, default=Path("data/derivatives"))
    record = sub.add_parser("record-liquidations", help="記錄即時公開強平事件；Ctrl-C 停止")
    record.add_argument("symbol")
    record.add_argument("--duration-seconds", type=float, help="選填；未指定時持續記錄到 Ctrl-C")
    record.add_argument("--output-dir", type=Path, default=Path("data/derivatives"))
    heat = sub.add_parser("liquidation-heatmap", help="以本機已記錄的實際事件輸出價格分箱 CSV")
    heat.add_argument("symbol")
    heat.add_argument("--price-bin-usd", type=float, default=100.0)
    heat.add_argument("--data-dir", type=Path, default=Path("data/derivatives"))
    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            result = download(args.symbol, args.start, args.end, args.period, args.interval, args.output_dir)
        elif args.command == "analyze":
            result = analyze(args.symbol, args.data_dir, args.interval, args.period)
        elif args.command == "record-liquidations":
            if args.duration_seconds is not None and args.duration_seconds <= 0:
                raise ValueError("記錄秒數必須大於 0。")
            count = record_liquidations(args.symbol, args.output_dir, args.duration_seconds)
            result = {"symbol": args.symbol.upper(), "new_snapshots": count, "output_dir": str(args.output_dir)}
        else:
            result = analyze_liquidations(args.symbol, args.data_dir, args.price_bin_usd)
    except (base.BinanceAPIError, DerivativesError, ValueError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
