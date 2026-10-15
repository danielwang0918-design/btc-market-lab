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

from binance_market_data import BinanceAPIError, BinanceClient, SPOT_BASE, validate_symbol
from market_structure import INTERVAL_MILLISECONDS, inspect_candle_integrity

DEFAULT_START = "2017-08-17"
INTERVALS = ("1d", "4h", "1h")
COLUMNS = ["symbol", "interval", "open_time", "open_time_utc", "open", "high", "low", "close", "volume", "close_time", "close_time_utc", "exchange_close_time", "exchange_close_time_utc", "is_closed", "source"]
SOURCE = "Binance Spot REST API historical klines"


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


def _fee_factor(bps: float, side: str) -> float:
    return bps / 10_000.0


def _closed_rows(path: Path, symbol: str, interval: str) -> list[dict[str, Any]]:
    rows = load_csv(path, symbol, interval)
    rows = [row for row in rows if str(row.get("is_closed", "")).lower() == "true"]
    report = inspect_candle_integrity(rows, interval)
    if report["errors"]:
        raise ValueError(f"資料完整性錯誤：{report['errors'][0]['kind']}。")
    for index, row in enumerate(rows):
        try:
            o, h, low, c, volume = (float(row[field]) for field in ("open", "high", "low", "close", "volume"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"第 {index + 1} 根 K 線 OHLCV 欄位無效。") from exc
        if not all(math.isfinite(x) for x in (o, h, low, c, volume)) or min(o, h, low, c) <= 0 or volume < 0 or low > min(o, c) or h < max(o, c) or low > h:
            raise ValueError(f"第 {index + 1} 根 K 線 OHLCV 完整性檢查失敗。")
    if not rows:
        raise ValueError(f"{path} 沒有已收盤 K 線。")
    return rows


def _simulate_segment(rows: list[dict[str, Any]], events: list[dict[str, Any]], fee_bps: float, slippage_bps: float, target_r: float) -> list[dict[str, Any]]:
    """Breakout-close signal, next-candle-open entry, OB-edge stop and fixed-R target."""
    if len(rows) < 2:
        return []
    by_open = {int(row["open_time"]): i for i, row in enumerate(rows)}
    step = INTERVAL_MILLISECONDS["1h"]
    setups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    ob_by_close: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    breaks: list[dict[str, Any]] = []
    for event in events:
        confirm = int(event["confirmed_at_close_time"])
        if event["event_type"] == "order_block":
            ob_by_close[(confirm, event["direction"])].append(event)
        elif event["event_type"] == "structure_break":
            breaks.append(event)
    for event in breaks:
        confirmation = int(event["confirmed_at_close_time"])
        entry_idx = by_open.get(confirmation + 1)
        obs = ob_by_close.get((confirmation, event["direction"]), [])
        if entry_idx is None or not obs:
            continue
        candle = rows[entry_idx]
        if int(candle["open_time"]) != confirmation + 1:
            continue
        ob = obs[-1]
        setups[entry_idx].append({"direction": event["direction"], "setup": "structure_break_with_order_block",
                                  "signal_time": confirmation, "stop": float(ob["price_low"] if event["direction"] == "bullish" else ob["price_high"]),
                                  "event_id": event["event_id"]})
    trades: list[dict[str, Any]] = []
    index = 0
    while index < len(rows) - 1:
        if not setups.get(index):
            index += 1
            continue
        # If both directions signal on the same close, skip the ambiguous setup.
        candidates = setups[index]
        directions = {c["direction"] for c in candidates}
        if len(directions) != 1:
            index += 1
            continue
        setup = candidates[-1]
        side = "long" if setup["direction"] == "bullish" else "short"
        raw_entry = float(rows[index]["open"])
        entry = raw_entry
        stop = setup["stop"]
        if not (stop < entry if side == "long" else stop > entry):
            index += 1
            continue
        risk = abs(entry - stop)
        target = entry + target_r * risk if side == "long" else entry - target_r * risk
        exit_index = len(rows) - 1
        exit_price = float(rows[exit_index]["close"])
        exit_reason = "segment_end"
        for j in range(index, len(rows)):
            bar = rows[j]
            o, h, lo = float(bar["open"]), float(bar["high"]), float(bar["low"])
            if side == "long":
                if o <= stop:
                    exit_index, exit_price, exit_reason = j, o, "stop_gap"
                    break
                stop_hit, target_hit = lo <= stop, h >= target
                if stop_hit or target_hit:
                    exit_index = j
                    # If both levels appear in one candle, assume stop was first.
                    exit_price = stop if stop_hit else target
                    exit_reason = "stop" if stop_hit else "target"
                    break
            else:
                if o >= stop:
                    exit_index, exit_price, exit_reason = j, o, "stop_gap"
                    break
                stop_hit, target_hit = h >= stop, lo <= target
                if stop_hit or target_hit:
                    exit_index = j
                    exit_price = stop if stop_hit else target
                    exit_reason = "stop" if stop_hit else "target"
                    break
        gross = (exit_price - entry) * (1 if side == "long" else -1)
        costs = (entry + exit_price) * (_fee_factor(fee_bps, side) + slippage_bps / 10_000)
        net = gross - costs
        initial_risk = risk + (entry + stop) * (fee_bps / 10_000 + slippage_bps / 10_000)
        trades.append({"setup": setup["setup"], "direction": side, "signal_time": setup["signal_time"],
                       "entry_time": int(rows[index]["open_time"]), "exit_time": int(rows[exit_index]["close_time"]),
                       "entry": entry, "stop": stop, "target": target, "exit": exit_price,
                       "exit_reason": exit_reason, "gross_pnl_per_unit": gross,
                       "cost_per_unit": costs, "net_pnl_per_unit": net,
                       "r_multiple": net / initial_risk if initial_risk > 0 else 0.0})
        index = exit_index + 1
    return trades


def _p0_events(rows: list[dict[str, Any]], *, left_bars: int = 2, right_bars: int = 2, ob_lookback: int = 10) -> list[dict[str, Any]]:
    """Stream only the BOS/OB evidence needed by P0, without hindsight or quadratic labels."""
    if not rows:
        return []
    events: list[dict[str, Any]] = []
    latest_high: tuple[int, int, float] | None = None
    latest_low: tuple[int, int, float] | None = None
    broken_highs: set[int] = set()
    broken_lows: set[int] = set()
    for index, candle in enumerate(rows):
        pivot = index - right_bars
        if pivot >= left_bars:
            window = rows[pivot - left_bars:index + 1]
            pivot_high = float(rows[pivot]["high"])
            pivot_low = float(rows[pivot]["low"])
            if all(pivot_high > float(item["high"]) for j, item in enumerate(window) if j != left_bars):
                latest_high = (pivot, index, pivot_high)
            if all(pivot_low < float(item["low"]) for j, item in enumerate(window) if j != left_bars):
                latest_low = (pivot, index, pivot_low)
        close = float(candle["close"])
        candidates: list[tuple[str, tuple[int, int, float]]] = []
        if latest_high and latest_high[0] not in broken_highs and index > latest_high[1] and close > latest_high[2]:
            candidates.append(("bullish", latest_high))
        if latest_low and latest_low[0] not in broken_lows and index > latest_low[1] and close < latest_low[2]:
            candidates.append(("bearish", latest_low))
        for direction, (pivot_idx, confirmed_idx, level) in candidates:
            if direction == "bullish":
                broken_highs.add(pivot_idx)
            else:
                broken_lows.add(pivot_idx)
            confirmed_close = int(candle["close_time"])
            event_id = f"{candle['symbol']}:1h:structure_break:{direction}:{candle['open_time']}"
            events.append({"event_type": "structure_break", "direction": direction,
                           "confirmed_at_close_time": confirmed_close, "event_id": event_id})
            start = max(confirmed_idx, index - ob_lookback)
            opposing = next((j for j in range(index - 1, start - 1, -1)
                             if (float(rows[j]["close"]) < float(rows[j]["open"]) if direction == "bullish"
                                 else float(rows[j]["close"]) > float(rows[j]["open"]))), None)
            if opposing is not None:
                ob = rows[opposing]
                events.append({"event_type": "order_block", "direction": direction,
                               "confirmed_at_close_time": confirmed_close,
                               "price_low": str(ob["low"]), "price_high": str(ob["high"]),
                               "event_id": f"{event_id}:ob"})
    return events


def _contiguous_segments(rows: list[dict[str, Any]], interval: str = "1h") -> list[list[dict[str, Any]]]:
    step = INTERVAL_MILLISECONDS[interval]
    result: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous = None
    for row in rows:
        opened = int(row["open_time"])
        if previous is not None and opened - previous != step:
            result.append(current)
            current = []
        current.append(row)
        previous = opened
    if current:
        result.append(current)
    return result


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(trades)
    wins = [t for t in trades if t["net_pnl_per_unit"] > 0]
    losses = [t for t in trades if t["net_pnl_per_unit"] < 0]
    average_win = sum(t["net_pnl_per_unit"] for t in wins) / len(wins) if wins else 0.0
    average_loss = sum(t["net_pnl_per_unit"] for t in losses) / len(losses) if losses else 0.0
    expectancy = sum(t["r_multiple"] for t in trades) / count if count else 0.0
    gross_win = sum(t["net_pnl_per_unit"] for t in wins)
    gross_loss = abs(sum(t["net_pnl_per_unit"] for t in losses))
    equity = peak = 0.0
    max_dd = 0.0
    consecutive = current = 0
    for trade in trades:
        equity += trade["r_multiple"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if trade["r_multiple"] < 0:
            current += 1
            consecutive = max(consecutive, current)
        else:
            current = 0
    return {"trades": count, "win_rate": len(wins) / count if count else 0.0,
            "average_win_per_unit": average_win, "average_loss_per_unit": average_loss,
            "expectancy_r": expectancy, "profit_factor": gross_win / gross_loss if gross_loss else None,
            "max_drawdown_r": max_dd, "max_consecutive_losses": consecutive}


def run_backtest(path: Path, symbol: str, *, fee_bps: float = 10.0, slippage_bps: float = 5.0,
                 target_r: float = 2.0, train_fraction: float = .6, validation_fraction: float = .2) -> dict[str, Any]:
    if not all(math.isfinite(v) for v in (fee_bps, slippage_bps, target_r, train_fraction, validation_fraction)) or fee_bps < 0 or slippage_bps < 0 or target_r <= 0 or not 0 < train_fraction < 1 or not 0 < validation_fraction < 1 or train_fraction + validation_fraction >= 1:
        raise ValueError("成本、目標 R 與資料切分比例設定無效。")
    rows = _closed_rows(path, validate_symbol(symbol), "1h")
    integrity = inspect_candle_integrity(rows, "1h")
    n = len(rows)
    cut1, cut2 = int(n * train_fraction), int(n * (train_fraction + validation_fraction))
    bounds = {"train": (0, cut1), "validation": (cut1, cut2), "test": (cut2, n)}
    results: dict[str, Any] = {}
    all_trades: list[dict[str, Any]] = []
    for name, (start, end) in bounds.items():
        segment = rows[start:end]
        start_ms = int(segment[0]["open_time"]) if segment else None
        end_ms = int(segment[-1]["close_time"]) if segment else None
        trades: list[dict[str, Any]] = []
        for contiguous in _contiguous_segments(segment):
            segment_events = _p0_events(contiguous)
            trades.extend(_simulate_segment(contiguous, segment_events, fee_bps, slippage_bps, target_r))
        results[name] = {"period_start_utc": _utc(start_ms) if start_ms is not None else None,
                         "period_end_utc": _utc(end_ms) if end_ms is not None else None,
                         "metrics": _metrics(trades)}
        all_trades.extend({**trade, "split": name} for trade in trades)
    def breakdown(key: str) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for trade in all_trades:
            groups[str(trade[key])].append(trade)
        return {name: _metrics(group) for name, group in sorted(groups.items())}
    return {"symbol": symbol, "interval": "1h", "strategy": "structure_break_with_order_block_v1",
            "data_summary": {"closed_candles": n, "start_utc": rows[0].get("open_time_utc") or _utc(int(rows[0]["open_time"])),
                             "end_utc": rows[-1].get("close_time_utc") or _utc(int(rows[-1]["close_time"])),
                             "normalized_exchange_close_time_count": sum(
                                 int(row.get("exchange_close_time") or row["close_time"]) != int(row["close_time"]) for row in rows)},
            "strategy_definition": "1H 收盤確認結構突破與同時確認 OB；下一根 1H 開盤進場，OB 外緣停損，固定目標 R。",
            "execution": {"signal": "closed_candle_only", "entry": "next_candle_open", "fee_bps_per_side": fee_bps,
                          "slippage_bps_per_side": slippage_bps, "target_r": target_r,
                          "same_candle_stop_and_target": "stop_first", "split_trade_handling": "每個分段獨立；分段最後仍未出場的持倉以最後收盤價平倉"},
            "split_method": {"type": "chronological", "train_fraction": train_fraction, "validation_fraction": validation_fraction,
                             "test_fraction": 1 - train_fraction - validation_fraction,
                             "boundaries": {"train_end_index": cut1, "validation_end_index": cut2, "candle_count": n}},
            "periods": results, "by_direction": breakdown("direction"), "by_setup": breakdown("setup"),
            "gap_handling": "每個 train/validation/test 段內再按連續 K 線切分；市場結構與持倉不跨越缺口。",
            "trades_detail": all_trades, "data_integrity": integrity}


def _json_default(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    raise TypeError(f"無法序列化 {type(value).__name__}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下載 Binance BTC 現貨歷史 K 線並執行 P0 回測（只讀，不下單）。")
    sub = parser.add_subparsers(dest="command", required=True)
    download = sub.add_parser("download", help="下載或增量更新歷史 K 線 CSV")
    download.add_argument("symbol", nargs="?", default="BTCUSDT")
    download.add_argument("--interval", choices=INTERVALS, default="1h")
    download.add_argument("--start", help="UTC 起始日期/時間；預設首次從 2017-08-17 開始，之後接續既有檔案")
    download.add_argument("--end", help="UTC 結束日期/時間；日期格式 YYYY-MM-DD 會包含整天")
    download.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    backtest = sub.add_parser("backtest", help="使用 1H CSV 跑時間順序 train/validation/test 回測")
    backtest.add_argument("symbol", nargs="?", default="BTCUSDT")
    backtest.add_argument("--data-dir", type=Path, default=Path("data/historical"))
    backtest.add_argument("--fee-bps-per-side", type=float, default=10.0)
    backtest.add_argument("--slippage-bps-per-side", type=float, default=5.0)
    backtest.add_argument("--target-r", type=float, default=2.0)
    backtest.add_argument("--train-fraction", type=float, default=.6)
    backtest.add_argument("--validation-fraction", type=float, default=.2)
    backtest.add_argument("--output", type=Path, help="回測 JSON 輸出路徑（預設 data/historical/<symbol>_backtest.json）")
    args = parser.parse_args(argv)
    try:
        if args.command == "download":
            report = download_to_csv(args.symbol, args.interval, args.data_dir, start=args.start, end=args.end)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            report = run_backtest(args.data_dir / f"{validate_symbol(args.symbol)}_1h.csv", args.symbol,
                                  fee_bps=args.fee_bps_per_side, slippage_bps=args.slippage_bps_per_side,
                                  target_r=args.target_r, train_fraction=args.train_fraction,
                                  validation_fraction=args.validation_fraction)
            output = args.output or args.data_dir / f"{validate_symbol(args.symbol)}_backtest.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default) + "\n", encoding="utf-8")
            compact = {"symbol": report["symbol"], "periods": {name: value["metrics"] for name, value in report["periods"].items()},
                       "by_direction": report["by_direction"], "by_setup": report["by_setup"], "output": str(output)}
            print(json.dumps(compact, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default))
    except (BinanceAPIError, OSError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
