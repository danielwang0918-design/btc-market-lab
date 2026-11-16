#!/usr/bin/env python3
"""Public Binance Spot USDT universe + historical 1d/4h/1h coverage research.

Research-only: no API keys, no order placement, no change to formal strategy.
The current top-volume universe is subject to survivorship and selection bias.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from binance_market_data import BinanceAPIError, BinanceClient, SPOT_PUBLIC_DATA_BASE

VERSION = "multi-asset-history-v1"
INTERVAL_MS = {"1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000}
INTERVALS = tuple(INTERVAL_MS)
STABLE_BASES = frozenset({
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "PAX", "USDD",
    "USD1", "USDG", "GUSD", "USDE", "USDS", "EURI", "EUR", "TRY",
    "BRL", "ARS", "UAH", "RUB", "GBP", "AUD", "JPY", "ZAR",
})
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "5L", "5S")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,24}$")


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def validate_symbols(raw: str) -> list[str]:
    if not raw or not raw.strip():
        raise ValueError("Specify at least one USDT trading pair")
    symbols = [x.strip().upper() for x in raw.split(",")]
    if any(not SYMBOL_RE.fullmatch(x) or not x.endswith("USDT") for x in symbols):
        raise ValueError("Only Binance USDT spot pairs, e.g. ETHUSDT,SOLUSDT")
    if len(symbols) != len(set(symbols)):
        raise ValueError("Duplicate symbol in request")
    if len(symbols) > 50:
        raise ValueError("Use at most 50 pairs per batch (split into multiple runs)")
    return symbols


def _eligible(info: Any) -> bool:
    if not isinstance(info, dict):
        return False
    symbol = info.get("symbol")
    base = str(info.get("baseAsset", ""))
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        return False
    if info.get("status") != "TRADING" or info.get("quoteAsset") != "USDT":
        return False
    if info.get("isSpotTradingAllowed") is False:
        return False
    if base in STABLE_BASES or not base or any(base.endswith(s) for s in LEVERAGED_SUFFIXES):
        return False
    if symbol != base + "USDT":
        return False
    return True


def select_universe(client: Any, *, explicit: str | None, top: int = 10,
                    minimum_24h_usdt: float = 1_000_000) -> dict[str, Any]:
    """Return frozen *current* spot-universe snapshot; never pass rank into past simulations."""
    if not isinstance(top, int) or isinstance(top, bool) or not 1 <= top <= 50:
        raise ValueError("top must be 1..50")
    if not math.isfinite(minimum_24h_usdt) or minimum_24h_usdt < 0:
        raise ValueError("minimum_24h_usdt must be finite and nonnegative")
    meta = client.get_json(SPOT_PUBLIC_DATA_BASE, "/api/v3/exchangeInfo")
    clock = client.get_json(SPOT_PUBLIC_DATA_BASE, "/api/v3/time")
    if not isinstance(meta, dict) or not isinstance(meta.get("symbols"), list):
        raise ValueError("Invalid Binance exchangeInfo data")
    if not isinstance(clock, dict) or type(clock.get("serverTime")) is not int or clock["serverTime"] <= 0:
        raise ValueError("Invalid Binance server timestamp")
    available = {info["symbol"]: info for info in meta["symbols"] if _eligible(info)}
    if explicit:
        chosen = validate_symbols(explicit)
        unavailable = [name for name in chosen if name not in available]
        if unavailable:
            raise ValueError("Not currently tradable Binance Spot USDT symbols: " + ", ".join(unavailable))
        observed = [{"symbol": name, "base_asset": available[name]["baseAsset"],
                     "quote_asset": "USDT", "quote_volume_24h_usdt": None} for name in chosen]
        selection_mode = "explicit"
    else:
        tickers = client.get_json(SPOT_PUBLIC_DATA_BASE, "/api/v3/ticker/24hr")
        if not isinstance(tickers, list):
            raise ValueError("Invalid Binance 24-hour ticker list")
        observed = []
        for tick in tickers:
            if not isinstance(tick, dict):
                continue
            name = tick.get("symbol")
            if name not in available:
                continue
            try:
                volume = float(tick["quoteVolume"])
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
            if not math.isfinite(volume) or volume < minimum_24h_usdt:
                continue
            observed.append({"symbol": name, "base_asset": available[name]["baseAsset"],
                             "quote_asset": "USDT", "quote_volume_24h_usdt": volume})
        observed.sort(key=lambda x: (-x["quote_volume_24h_usdt"], x["symbol"]))
        observed = observed[:top]
        selection_mode = "top_24h_spot_usdt_volume"
        if not observed:
            raise ValueError("No eligible liquid USDT Spot pairs returned by Binance")
    return {
        "version": VERSION, "exchange": "Binance Spot", "api_base": SPOT_PUBLIC_DATA_BASE,
        "data_source": "Binance public market data (no credentials)",
        "selected_at_server_ms": clock["serverTime"], "selected_at_utc": utc(clock["serverTime"]),
        "selection_mode": selection_mode,
        "minimum_24h_usdt": minimum_24h_usdt if not explicit else None,
        "ranking_metric": "24h quoteVolume in USDT" if not explicit else None,
        "selection_bias_warning": (
            "Current active/liquid pairs are known only as of the selection timestamp. "
            "Using this list for pre-selection historical backtests has survivorship and "
            "hindsight universe bias; it does NOT recreate historical investable universes."
        ),
        "symbols": observed,
    }


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def scan_csv(path: Path, symbol: str, interval: str) -> dict[str, Any]:
    """Stream timestamp/integrity audit, checking gaps without inventing candles."""
    step = INTERVAL_MS[interval]
    count, gaps, missing = 0, [], 0
    first_ms = last_ms = previous_ms = None
    seen_source = set()
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"symbol", "interval", "open_time", "close_time", "is_closed", "source", "open", "high", "low", "close", "volume"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Incomplete historical CSV columns: {path}")
        for row in reader:
            if row["symbol"] != symbol or row["interval"] != interval or row["is_closed"].lower() != "true":
                raise ValueError(f"Wrong symbol, interval or unclosed candle: {path}")
            start, end = int(row["open_time"]), int(row["close_time"])
            if start < 0 or start % step or end != start + step - 1:
                raise ValueError(f"Invalid candle time alignment: {path}")
            try:
                opened, high, low, closed, volume = (
                    Decimal(row[k]) for k in ("open", "high", "low", "close", "volume")
                )
            except (InvalidOperation, KeyError, ValueError, TypeError) as exc:
                raise ValueError(f"Invalid price or volume: {path}") from exc
            if (not all(v.is_finite() for v in (opened, high, low, closed, volume))
                    or min(opened, high, low, closed) <= 0 or volume < 0
                    or low > min(opened, closed) or high < max(opened, closed)):
                raise ValueError(f"Invalid OHLCV or nonfinite number: {path}")
            if previous_ms is not None:
                diff = start - previous_ms
                if diff <= 0 or diff % step:
                    raise ValueError(f"Out-of-order, duplicate or irregular spacing: {path}")
                if diff > step:
                    n = diff // step - 1
                    missing += n
                    gaps.append({"after_open_ms": previous_ms, "next_open_ms": start, "missing_bars": n})
            seen_source.add(row["source"])
            if not row["source"]:
                raise ValueError(f"Empty source provenance: {path}")
            count += 1
            if first_ms is None:
                first_ms = start
            last_ms = end
            previous_ms = start
    return {
        "symbol": symbol, "interval": interval, "bars": count,
        "first_observed_candle_open_utc": utc(first_ms) if first_ms is not None else None,
        "last_closed_candle_end_utc": utc(last_ms) if last_ms is not None else None,
        "gap_segments": len(gaps), "missing_bars_in_observed_range": missing,
        "gaps": gaps[:100], "gaps_truncated": len(gaps) > 100,
        "source": sorted(seen_source), "csv_sha256": _hash(path),
        "first_observed_is_not_proven_listing_time": True,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def download_universe(data_dir: Path, universe: dict[str, Any], *, start: str | None,
                      end: str | None, client: Any) -> tuple[dict[str, Any], int]:
    """Reuse canonical per-symbol downloader; preserve partial-failure provenance."""
    entries = universe.get("symbols")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Universe manifest has no symbols")
    if any(not isinstance(entry, dict) or not isinstance(entry.get("symbol"), str) for entry in entries):
        raise ValueError("Malformed universe selection entries")
    validated_symbols = validate_symbols(",".join(entry["symbol"] for entry in entries))
    if any(entry["symbol"] != name for entry, name in zip(entries, validated_symbols)):
        raise ValueError("Unnormalized universe entries")
    # Lazy import after manifest validation keeps malformed files from reaching file paths.
    from historical_backtest import download_to_csv
    results: dict[str, Any] = {}
    failures = 0
    for index, selected in enumerate(entries, 1):
        symbol = selected["symbol"]
        print(f"[{index}/{len(entries)}] Binance history {symbol}: 1d/4h/1h", flush=True)
        report: dict[str, Any] = {"intervals": {}, "status": "complete"}
        for interval in INTERVALS:
            try:
                outcome = download_to_csv(symbol, interval, data_dir, start=start, end=end, client=client)
                path = data_dir / f"{symbol}_{interval}.csv"
                metrics = scan_csv(path, symbol, interval)
                metrics["newly_downloaded_bars"] = outcome["downloaded_count"]
                report["intervals"][interval] = metrics
                if metrics["bars"] == 0:
                    report["status"] = "insufficient_history"
            except (OSError, BinanceAPIError, ValueError, KeyError, TypeError) as exc:
                report["status"] = "download_or_integrity_failure"
                report["intervals"][interval] = {"error": str(exc)}
                failures += 1
                print(f"  {symbol} {interval}: ERROR {exc}", file=sys.stderr, flush=True)
        one, four, day = (report["intervals"].get(k, {}).get("bars", 0) for k in ("1h", "4h", "1d"))
        if report["status"] == "complete" and min(one / 24, four / 6, day) < 365:
            report["status"] = "limited_history_under_365_days"
        results[symbol] = report
    return {
        "version": VERSION, "universe": universe,
        "intervals": list(INTERVALS), "requested_start": start, "requested_end": end,
        "status": "complete" if failures == 0 else "partial_failure",
        "failed_symbol_intervals": failures,
        "symbols": results,
        "interpretation": (
            "Historical candles are observations, not independent strategy trade samples. "
            "Current-volume selection is NOT a historical point-in-time universe. "
            "Correlated assets cannot be pooled as independent tests. "
            "No strategy performance is claimed in this download coverage report."
        ),
    }, failures


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Binance Spot multi-asset history & provenance audit; research only")
    p.add_argument("--data-dir", type=Path, default=Path("data/multi_asset"))
    group = p.add_mutually_exclusive_group()
    group.add_argument("--symbols", help="Comma separated active Spot USDT pairs: BTCUSDT,ETHUSDT,SOLUSDT,...")
    group.add_argument("--top-volume", type=int, default=10, help="Current largest N USDT quote-volume pairs (1..50)")
    p.add_argument("--min-24h-usdt", type=float, default=1_000_000)
    p.add_argument("--start", default="2017-08-17")
    p.add_argument("--end", help="Inclusive UTC day cutoff for reproducibility")
    p.add_argument("--coverage-only", action="store_true", help="List selected universe only; do not download candles")
    p.add_argument("--universe-file", type=Path, help="Reuse previously frozen selection manifest instead of reselecting")
    args = p.parse_args(argv)
    client = BinanceClient()
    try:
        if args.universe_file:
            universe = json.loads(args.universe_file.read_text(encoding="utf-8"))
            if universe.get("version") != VERSION or not universe.get("symbols"):
                raise ValueError("Unknown or empty universe manifest")
        else:
            universe = select_universe(client, explicit=args.symbols,
                                       top=args.top_volume, minimum_24h_usdt=args.min_24h_usdt)
        args.data_dir.mkdir(parents=True, exist_ok=True)
        write_json(args.data_dir / "universe_selection.json", universe)
        print(json.dumps({"symbols": [s["symbol"] for s in universe["symbols"]],
                          "selection_asof": universe.get("selected_at_utc"),
                          "mode": universe["selection_mode"]}, ensure_ascii=False), flush=True)
        if args.coverage_only:
            return 0
        coverage, failures = download_universe(args.data_dir, universe,
                                               start=args.start, end=args.end, client=client)
        write_json(args.data_dir / "multi_asset_coverage.json", coverage)
        print(json.dumps({"status": coverage["status"], "failures": failures,
                          "statuses": {s: entry["status"] for s, entry in coverage["symbols"].items()},
                          "path": str(args.data_dir / "multi_asset_coverage.json")}, ensure_ascii=False), flush=True)
        return 1 if failures else 0
    except (ValueError, OSError, BinanceAPIError, KeyError, json.JSONDecodeError) as exc:
        print(f"Data collection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())