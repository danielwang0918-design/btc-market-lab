#!/usr/bin/env python3
"""Download public Binance spot and USDⓈ-M futures market data."""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"
SOURCE_SPOT = "Binance Spot REST API"
SOURCE_FUTURES = "Binance USDⓈ-M Futures REST API"
INTERVALS = {"1d": "1d", "4h": "4h", "1h": "1h"}
KLINE_COLUMNS = ["symbol", "interval", "open_time", "open_time_utc", "open", "high", "low", "close", "volume", "close_time", "close_time_utc", "is_closed", "source"]


class BinanceAPIError(Exception):
    """An HTTP, API, or response-format error from Binance."""


def utc_iso(ms: int | float) -> str:
    return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BinanceClient:
    def __init__(self, timeout: float = 15.0, retries: int = 3, user_agent: str = "btc-market-lab/1.0"):
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent

    def get_json(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = f"{base}{path}" + (f"?{query}" if query else "")
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and "code" in payload and "msg" in payload:
                    raise BinanceAPIError(f"Binance API error {payload['code']}: {payload['msg']}")
                return payload
            except urllib.error.HTTPError as exc:
                retryable = exc.code in (418, 429) or 500 <= exc.code < 600
                if not retryable or attempt >= self.retries:
                    body = exc.read().decode("utf-8", errors="replace")[:500]
                    raise BinanceAPIError(f"HTTP {exc.code} from {url}: {body}") from exc
                retry_after = exc.headers.get("Retry-After")
                delay = min(float(retry_after), 30.0) if retry_after and retry_after.isdigit() else min(2 ** attempt, 8)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt >= self.retries:
                    raise BinanceAPIError(f"Network or JSON error from {url}: {exc}") from exc
                delay = min(2 ** attempt, 8)
            if delay:
                time.sleep(delay)
        raise BinanceAPIError(f"Request failed after retries: {url}")


def validate_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if not re.fullmatch(r"[A-Z0-9]{5,20}", symbol):
        raise ValueError("交易對格式不正確；請使用例如 BTCUSDT 的英數字代碼。")
    return symbol


def validate_klines(raw: Any, symbol: str, interval: str, server_time: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise BinanceAPIError(f"{interval} K 線回應為空或格式錯誤。")
    rows: list[dict[str, Any]] = []
    previous_open = -1
    for item in raw:
        if not isinstance(item, list) or len(item) < 7:
            raise BinanceAPIError(f"{interval} K 線欄位不足。")
        try:
            open_time, close_time = int(item[0]), int(item[6])
            values = [float(item[i]) for i in (1, 2, 3, 4, 5)]
        except (ValueError, TypeError, IndexError) as exc:
            raise BinanceAPIError(f"{interval} K 線包含非數值欄位。") from exc
        open_price, high, low, close, volume = values
        if not all(math.isfinite(value) for value in values):
            raise BinanceAPIError(f"{interval} K 線包含非有限數值。")
        if open_time <= previous_open or close_time < open_time:
            raise BinanceAPIError(f"{interval} K 線時間順序或時間範圍錯誤。")
        if min(open_price, high, low, close) <= 0 or volume < 0 or low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise BinanceAPIError(f"{interval} K 線價格或成交量完整性檢查失敗。")
        previous_open = open_time
        rows.append({"symbol": symbol, "interval": interval, "open_time": open_time, "open_time_utc": utc_iso(open_time),
                     "open": item[1], "high": item[2], "low": item[3], "close": item[4], "volume": item[5],
                     "close_time": close_time, "close_time_utc": utc_iso(close_time), "is_closed": str(close_time < server_time).lower(), "source": SOURCE_SPOT})
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def fetch_market_data(symbol: str, output_dir: Path, client: BinanceClient | None = None, limit: int = 200) -> Path:
    symbol = validate_symbol(symbol)
    if not 1 <= limit <= 1000:
        raise ValueError("K 線數量必須介於 1 到 1000。")
    client = client or BinanceClient()
    collection_started_at = now_iso()
    server = client.get_json(SPOT_BASE, "/api/v3/time")
    if not isinstance(server, dict) or not isinstance(server.get("serverTime"), int):
        raise BinanceAPIError("Binance server time 回應格式錯誤，無法可靠判斷 K 線是否收盤。")
    server_time = server["serverTime"]
    klines: list[dict[str, Any]] = []
    for interval in INTERVALS:
        raw = client.get_json(SPOT_BASE, "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        klines.extend(validate_klines(raw, symbol, interval, server_time))
    ticker = client.get_json(SPOT_BASE, "/api/v3/ticker/price", {"symbol": symbol})
    ticker_captured_at = now_iso()
    stats = client.get_json(SPOT_BASE, "/api/v3/ticker/24hr", {"symbol": symbol})
    stats_captured_at = now_iso()
    for name, response in (("現貨最新報價", ticker), ("現貨 24 小時統計", stats)):
        if not isinstance(response, dict) or "symbol" not in response:
            raise BinanceAPIError(f"{name}回應格式錯誤。")
    ticker_row = {"symbol": symbol, "price": ticker.get("price"), "captured_at_utc": ticker_captured_at, "source": SOURCE_SPOT}
    stat_row = {"symbol": symbol, **stats, "captured_at_utc": stats_captured_at, "source": SOURCE_SPOT}
    optional: dict[str, list[dict[str, Any]]] = {"futures_open_interest.csv": [], "futures_funding_rate.csv": []}
    errors: dict[str, str] = {}
    optional_status: dict[str, str] = {}
    try:
        oi = client.get_json(FUTURES_BASE, "/fapi/v1/openInterest", {"symbol": symbol})
        if not isinstance(oi, dict) or "openInterest" not in oi:
            raise BinanceAPIError("未平倉量回應格式錯誤。")
        optional["futures_open_interest.csv"].append({"symbol": symbol, "open_interest": oi["openInterest"], "exchange_time": oi.get("time"), "exchange_time_utc": utc_iso(oi["time"]) if oi.get("time") is not None else "", "captured_at_utc": now_iso(), "source": SOURCE_FUTURES})
        optional_status["futures_open_interest"] = "available"
    except (BinanceAPIError, ValueError, TypeError) as exc:
        optional_status["futures_open_interest"] = "error"
        errors["futures_open_interest"] = str(exc)
    try:
        rates = client.get_json(FUTURES_BASE, "/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
        if not isinstance(rates, list):
            raise BinanceAPIError("資金費率回應格式錯誤。")
        if rates:
            optional_status["futures_funding_rate"] = "available"
            rate = rates[-1]
            optional["futures_funding_rate.csv"].append({"symbol": symbol, "funding_rate": rate.get("fundingRate"), "funding_time": rate.get("fundingTime"), "funding_time_utc": utc_iso(rate["fundingTime"]) if rate.get("fundingTime") is not None else "", "captured_at_utc": now_iso(), "source": SOURCE_FUTURES})
        else:
            optional_status["futures_funding_rate"] = "empty"
    except (BinanceAPIError, ValueError, TypeError) as exc:
        optional_status["futures_funding_rate"] = "error"
        errors["futures_funding_rate"] = str(exc)
    captured_at = now_iso()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{symbol}_{stamp}"
    work = output_dir / stem
    work.mkdir()
    write_csv(work / "klines.csv", klines, KLINE_COLUMNS)
    write_csv(work / "spot_price.csv", [ticker_row], ["symbol", "price", "captured_at_utc", "source"])
    write_csv(work / "spot_24h_stats.csv", [stat_row], list(stat_row.keys()))
    write_csv(work / "futures_open_interest.csv", optional["futures_open_interest.csv"], ["symbol", "open_interest", "exchange_time", "exchange_time_utc", "captured_at_utc", "source"])
    write_csv(work / "futures_funding_rate.csv", optional["futures_funding_rate.csv"], ["symbol", "funding_rate", "funding_time", "funding_time_utc", "captured_at_utc", "source"])
    metadata = {"symbol": symbol, "captured_at_utc": captured_at, "binance_server_time": server_time, "binance_server_time_utc": utc_iso(server_time), "kline_limit_per_interval": limit, "intervals": list(INTERVALS), "collection_started_at_utc": collection_started_at, "sources": {"spot": SPOT_BASE, "futures": FUTURES_BASE}, "optional_endpoint_status": optional_status, "optional_endpoint_errors": errors, "notes": ["K 線只在 close_time 早於 Binance server time 時標記 is_closed=true。", "未平倉量端點未提供事件時間時，同時記錄擷取時間。", "未回傳的期貨欄位以空 CSV 表頭保留；端點錯誤及空資料狀態見本檔。"]}
    (work / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = output_dir / f"{stem}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(work.iterdir()):
            zf.write(path, arcname=path.name)
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="擷取 Binance 公開現貨與合約市場資料，輸出 CSV 和 ZIP。")
    parser.add_argument("symbol", help="交易對，例如 BTCUSDT")
    parser.add_argument("--output-dir", type=Path, default=Path("data"), help="輸出目錄（預設：./data）")
    parser.add_argument("--limit", type=int, default=200, help="每個週期的 K 線數量 1–1000（預設：200）")
    args = parser.parse_args(argv)
    try:
        archive = fetch_market_data(args.symbol, args.output_dir, limit=args.limit)
    except (BinanceAPIError, ValueError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    print(f"資料擷取完成：{archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
