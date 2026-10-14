#!/usr/bin/env python3
"""Rule-based, auditable market research and manual trade review.

This module reads public Binance data. It never authenticates or places orders.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import binance_market_data
from market_structure import inspect_candle_integrity

INTERVALS = ("1d", "4h", "1h")
STRATEGY_VERSION = "mtf-bos-ob-retest-v1"
CONFIG_VERSION = 1
DEFAULT_CONFIG = Path(".market-lab/strategy.json")
DEFAULT_DATA_DIR = Path("data")
DEFAULT_RECORDS_DIR = Path("research")
ANALYSIS_REQUIRED_MEMBERS = {"metadata.json", "klines.csv", "spot_price.csv", "market_structure.csv"}
KNOWN_QUOTES = ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "BNB", "TRY")


def _rules() -> dict[str, Any]:
    return {
        "alignment_intervals": ["1d", "4h", "1h"],
        "entry_interval": "1h",
        "entry_event_type": "order_block",
        "entry_rule": "等待現價回到與三週期結構同向、且 OB 確認後尚未被已收盤 K 線回測的最新 1H OB 全區間；現價位於區間內標記為目前已觸發。",
        "stop_rule": "多方取 OB price_low；空方取 OB price_high。",
        "target_interval": "1h",
        "target_rule": "取 OB 確認後才被確認、位於進場區外側最近且未被後續收盤 K 線觸及的反向波段。",
        "block_candidates_on_any_gap": True,
        "closed_candles_only": True,
    }


def _decimal(value: Any, label: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必須是有限數值。") from exc
    if not result.is_finite():
        raise ValueError(f"{label} 必須是有限數值。")
    if positive and result <= 0:
        raise ValueError(f"{label} 必須大於 0。")
    if nonnegative and result < 0:
        raise ValueError(f"{label} 不可小於 0。")
    return result


def validate_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict) or config.get("config_version") != CONFIG_VERSION:
        raise ValueError("策略設定格式錯誤或版本不支援；請重新執行 --setup。")
    if config.get("strategy_version") != STRATEGY_VERSION or config.get("rules") != _rules():
        raise ValueError("策略規則與目前程式版本不一致；請重新執行 --setup。")
    _decimal(config.get("minimum_net_rr"), "最低淨風險報酬比", positive=True)
    age = config.get("max_quote_age_seconds")
    if type(age) is not int or age <= 0:
        raise ValueError("報價新鮮度門檻必須是正整數秒。")
    _decimal(config.get("fee_bps_per_side"), "單邊費率（bps）", nonnegative=True)
    _decimal(config.get("slippage_bps_per_side"), "單邊滑價（bps）", nonnegative=True)
    setup_age = config.get("max_setup_age_hours")
    if type(setup_age) is not int or setup_age <= 0:
        raise ValueError("結構情境最長有效時間必須是正整數小時。")
    return config


def _read_number(prompt: str, input_fn: Callable[[str], str], *, integer: bool = False, positive: bool = False, nonnegative: bool = False) -> Any:
    raw = input_fn(prompt).strip()
    if integer:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("此設定必須輸入整數。") from exc
        if positive and value <= 0:
            raise ValueError("此設定必須大於 0。")
        if nonnegative and value < 0:
            raise ValueError("此設定不可小於 0。")
        return value
    value = _decimal(raw, prompt.rstrip(), positive=positive, nonnegative=nonnegative)
    return format(value, "f")


def configure(path: Path, input_fn: Callable[[str], str] = input, output_fn: Callable[[str], None] = print) -> dict[str, Any]:
    output_fn("研究規則（mtf-bos-ob-retest-v1）：")
    output_fn("- 1D、4H、1H 最近已確認結構突破必須同向；缺少任何週期或方向不一致就不建立候選。")
    output_fn("- 以同方向且尚未回測的最新 1H OB 作為回踩區，止損參考 OB 外緣，目標取其後最近且未被觸及的反向已確認 1H 波段。")
    output_fn("- 任一週期資料有缺口、情境超過有效時間、報價過舊或扣除費率/滑價後未達最低淨風險報酬比，都不建立候選。")
    output_fn("- 報價進入 OB 區只代表區間觸發條件成立，不代表成交，也沒有下單功能。")
    accepted = input_fn("接受這套明示規則並設定個人門檻？輸入 yes 繼續：").strip().lower()
    if accepted not in {"yes", "y"}:
        raise ValueError("尚未接受策略規則，未建立設定。可檢視此程式的 _rules() 並重新執行 --setup。")
    config = {
        "config_version": CONFIG_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "rules": _rules(),
        "minimum_net_rr": _read_number("最低淨風險報酬比（必填，例如 2）：", input_fn, positive=True),
        "max_quote_age_seconds": _read_number("報價最長允許秒數（必填整數）：", input_fn, integer=True, positive=True),
        "fee_bps_per_side": _read_number("單邊費率假設 bps（必填；1 bps = 0.01%）：", input_fn, nonnegative=True),
        "slippage_bps_per_side": _read_number("單邊滑價假設 bps（必填）：", input_fn, nonnegative=True),
        "max_setup_age_hours": _read_number("OB 情境最長有效小時數（必填整數）：", input_fn, integer=True, positive=True),
    }
    validate_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_fn(f"策略設定已儲存：{path}")
    return config


def load_config(path: Path) -> dict[str, Any]:
    try:
        return validate_config(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise ValueError(f"找不到策略設定 {path}；第一次請執行 python3 market_research.py --setup。") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"策略設定不是有效 JSON：{path}") from exc


def normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol:
        raise ValueError("請輸入幣種，例如 BTC。")
    if not any(symbol.endswith(quote) and len(symbol) > len(quote) for quote in KNOWN_QUOTES):
        symbol += "USDT"
    return binance_market_data.validate_symbol(symbol)


def _iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def _csv_rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    with archive.open(name) as source:
        return list(csv.DictReader(io.TextIOWrapper(source, encoding="utf-8-sig", newline="")))


def _load_archive(path: Path) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            missing = sorted(ANALYSIS_REQUIRED_MEMBERS - names)
            if missing:
                raise ValueError(f"資料包缺少必要檔案：{', '.join(missing)}。")
            metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
            klines = _csv_rows(archive, "klines.csv")
            spots = _csv_rows(archive, "spot_price.csv")
            events = _csv_rows(archive, "market_structure.csv")
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"無法讀取資料包：{exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("metadata.json 格式錯誤。")
    return metadata, klines, spots, events


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    result = {key: event.get(key, "") for key in (
        "event_id", "interval", "event_type", "direction", "formed_at_open_time",
        "confirmed_at_close_time", "confirmed_at_utc", "price_low", "price_high",
        "reference_price", "detector_version", "parameters_json", "source",
    )}
    try:
        result["evidence"] = json.loads(event.get("evidence_json", "[]"))
    except (json.JSONDecodeError, TypeError):
        result["evidence"] = []
    return result


def _integrity_reports(klines: list[dict[str, str]], metadata: dict[str, Any], symbol: str, server_time: int) -> dict[str, Any]:
    reported = metadata.get("data_integrity", {}).get("intervals", {}) if isinstance(metadata.get("data_integrity"), dict) else {}
    reports: dict[str, Any] = {}
    for interval in INTERVALS:
        rows = [row for row in klines if row.get("symbol") == symbol and row.get("interval") == interval]
        recomputed = inspect_candle_integrity(rows, interval, server_time)
        price_errors: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if row.get("source") != binance_market_data.SOURCE_SPOT:
                price_errors.append({"row_index": index, "detail": "K 線資料來源不是 Binance Spot REST API。"})
            try:
                open_price = _decimal(row.get("open"), "open", positive=True)
                high = _decimal(row.get("high"), "high", positive=True)
                low = _decimal(row.get("low"), "low", positive=True)
                close = _decimal(row.get("close"), "close", positive=True)
                volume = _decimal(row.get("volume"), "volume", nonnegative=True)
                if low > min(open_price, close) or high < max(open_price, close) or low > high or volume < 0:
                    raise ValueError("OHLC 或成交量順序不合理。")
            except ValueError as exc:
                price_errors.append({"row_index": index, "detail": str(exc)})
        stored = reported.get(interval) if isinstance(reported, dict) else None
        if not isinstance(stored, dict):
            reports[interval] = {**recomputed, "price_errors": price_errors, "stored_report_status": "missing_report", "integrity_ok": False}
            continue
        stored_gaps = stored.get("gaps") if isinstance(stored.get("gaps"), list) else []
        stored_errors = stored.get("errors") if isinstance(stored.get("errors"), list) else []
        consistent = (
            stored.get("status") == recomputed.get("status")
            and stored.get("is_contiguous") == recomputed.get("is_contiguous")
            and stored.get("received_candle_count") == recomputed.get("received_candle_count")
            and stored.get("closed_candle_count") == recomputed.get("closed_candle_count")
            and stored.get("unclosed_candle_count") == recomputed.get("unclosed_candle_count")
            and stored.get("missing_candle_count") == recomputed.get("missing_candle_count")
            and stored_gaps == recomputed.get("gaps")
            and len(stored_errors) == len(recomputed.get("errors", []))
        )
        reports[interval] = {
            **recomputed,
            "price_errors": price_errors,
            "stored_report_status": stored.get("status"),
            "stored_gaps": stored_gaps,
            "integrity_ok": consistent and recomputed["status"] == "complete" and not price_errors,
            "stored_report_matches_recomputed_data": consistent,
        }
    return reports


def _validated_events(
    events: list[dict[str, str]], candles: list[dict[str, str]], symbol: str, server_time: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candle_lookup = {
        (row.get("interval"), row.get("open_time")): row
        for row in candles
        if row.get("symbol") == symbol
    }
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in events:
        if row.get("symbol") != symbol:
            continue
        try:
            confirmed = int(row.get("confirmed_at_close_time", ""))
            evidence = json.loads(row.get("evidence_json", ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            rejected.append({"event_id": row.get("event_id", ""), "reason": "invalid_confirmation_or_evidence"})
            continue
        if confirmed >= server_time or not isinstance(evidence, list) or not evidence:
            rejected.append({"event_id": row.get("event_id", ""), "reason": "future_or_missing_closed_evidence"})
            continue
        matched = True
        for item in evidence:
            if (
                not isinstance(item, dict)
                or str(item.get("is_closed", "")).lower() != "true"
                or item.get("symbol") != symbol
                or item.get("interval") != row.get("interval")
            ):
                matched = False
                break
            candle = candle_lookup.get((row.get("interval"), str(item.get("open_time", ""))))
            if candle is None or str(candle.get("is_closed", "")).lower() != "true":
                matched = False
                break
            if any(
                str(candle.get(field, "")) != str(item.get(field, ""))
                for field in ("open_time", "open_time_utc", "close_time", "close_time_utc", "open", "high", "low", "close", "volume", "source", "is_closed")
            ):
                matched = False
                break
            if row.get("source") != item.get("source"):
                matched = False
                break
        if matched:
            try:
                evidence_opens = {int(item["open_time"]) for item in evidence}
                evidence_closes = {int(item["close_time"]) for item in evidence}
                if int(row.get("formed_at_open_time", "")) not in evidence_opens:
                    matched = False
                if int(row.get("confirmed_at_open_time", "")) not in evidence_opens or confirmed not in evidence_closes:
                    matched = False
                if any(int(item["close_time"]) > confirmed for item in evidence):
                    matched = False
            except (KeyError, TypeError, ValueError):
                matched = False
        if matched:
            formed = next((item for item in evidence if str(item.get("open_time")) == str(row.get("formed_at_open_time"))), None)
            confirmed_witness = next((item for item in evidence if int(item.get("open_time", -1)) == int(row.get("confirmed_at_open_time", -2))), None)
            if (
                formed is None
                or confirmed_witness is None
                or row.get("formed_at_utc") != formed.get("open_time_utc")
                or row.get("confirmed_at_open_time_utc") != confirmed_witness.get("open_time_utc")
                or row.get("confirmed_at_utc") != confirmed_witness.get("close_time_utc")
            ):
                matched = False
        if matched and row.get("event_type") == "order_block":
            formed = next((item for item in evidence if str(item.get("open_time")) == str(row.get("formed_at_open_time"))), None)
            try:
                if formed is None or _decimal(row.get("price_low"), "OB 下緣") != _decimal(formed.get("low"), "OB 證據低點") or _decimal(row.get("price_high"), "OB 上緣") != _decimal(formed.get("high"), "OB 證據高點"):
                    matched = False
            except ValueError:
                matched = False
        if matched and row.get("event_type") in {"swing_high", "swing_low"}:
            formed = next((item for item in evidence if str(item.get("open_time")) == str(row.get("formed_at_open_time"))), None)
            field = "high" if row.get("event_type") == "swing_high" else "low"
            try:
                if formed is None or _decimal(row.get("reference_price"), "波段參考價") != _decimal(formed.get(field), "波段 K 線價位"):
                    matched = False
            except ValueError:
                matched = False
        if not matched:
            rejected.append({"event_id": row.get("event_id", ""), "reason": "evidence_not_in_closed_input_candles"})
            continue
        event = dict(row)
        event["confirmed_at_close_time"] = str(confirmed)
        event["evidence"] = evidence
        valid.append(event)
    return valid, rejected


def _zone_was_retested(candles: list[dict[str, str]], setup: dict[str, Any], side: str, zone_low: Decimal, zone_high: Decimal) -> bool:
    confirmed = int(setup["confirmed_at_close_time"])
    for candle in candles:
        if candle.get("symbol") != setup.get("symbol") or candle.get("interval") != setup.get("interval"):
            continue
        try:
            if str(candle.get("is_closed", "")).lower() != "true" or int(candle["close_time"]) <= confirmed:
                continue
            low = _decimal(candle.get("low"), "K 線低點", positive=True)
            high = _decimal(candle.get("high"), "K 線高點", positive=True)
        except (KeyError, ValueError):
            continue
        if (side == "long" and low <= zone_high) or (side == "short" and high >= zone_low):
            return True
    return False


def _target_was_reached(candles: list[dict[str, str]], target_event: dict[str, Any], side: str, target: Decimal) -> bool:
    confirmed = int(target_event["confirmed_at_close_time"])
    for candle in candles:
        if candle.get("symbol") != target_event.get("symbol") or candle.get("interval") != target_event.get("interval"):
            continue
        try:
            if str(candle.get("is_closed", "")).lower() != "true" or int(candle["close_time"]) <= confirmed:
                continue
            price = _decimal(candle.get("high" if side == "long" else "low"), "K 線波段外側價位", positive=True)
        except (KeyError, ValueError):
            continue
        if (side == "long" and price >= target) or (side == "short" and price <= target):
            return True
    return False


def _latest_event(events: list[dict[str, Any]], interval: str, event_type: str) -> dict[str, Any] | None:
    matches = [event for event in events if event.get("interval") == interval and event.get("event_type") == event_type]
    return max(matches, key=lambda event: int(event["confirmed_at_close_time"])) if matches else None


def _quote(spots: list[dict[str, str]], symbol: str, now: datetime, max_age: int) -> tuple[dict[str, Any], Decimal | None]:
    rows = [row for row in spots if row.get("symbol") == symbol]
    if not rows:
        return {"status": "unavailable", "detail": "資料包沒有此交易對的現貨報價。"}, None
    row = rows[-1]
    if row.get("source") != binance_market_data.SOURCE_SPOT:
        return {"status": "invalid_source", "detail": "報價來源不是 Binance Spot REST API。"}, None
    try:
        price = _decimal(row.get("price"), "現貨最新報價", positive=True)
    except ValueError as exc:
        return {"status": "invalid_price", "detail": str(exc)}, None
    captured = _iso_datetime(row.get("captured_at_utc"))
    if captured is None:
        return {"status": "invalid_timestamp", "captured_at_utc": row.get("captured_at_utc", "")}, price
    age = (now - captured).total_seconds()
    status = "fresh" if 0 <= age <= max_age else "future_timestamp" if age < 0 else "stale"
    return {
        "status": status,
        "price": format(price, "f"),
        "captured_at_utc": captured.isoformat(),
        "age_seconds": round(age, 3),
        "maximum_age_seconds": max_age,
        "source": row.get("source", ""),
    }, price


def _net_risk_reward(side: str, entry: Decimal, stop: Decimal, target: Decimal, fee_bps: Decimal, slippage_bps: Decimal) -> dict[str, Any]:
    if not (stop < entry < target if side == "long" else target < entry < stop):
        raise ValueError("候選價位順序不符合多空方向。")
    rate = (fee_bps + slippage_bps) / Decimal(10_000)
    entry_cost = entry * rate
    stop_cost = stop * rate
    target_cost = target * rate
    risk = abs(entry - stop) + entry_cost + stop_cost
    reward = abs(target - entry) - entry_cost - target_cost
    ratio = reward / risk if risk > 0 else Decimal("-Infinity")
    return {
        "assumed_quantity": "1 base-asset unit",
        "fee_bps_per_fill": format(fee_bps, "f"),
        "slippage_bps_per_fill": format(slippage_bps, "f"),
        "cost_model": "進場及預期出場各套用一次單邊費率與滑價；不含點差、資金費率、借幣成本、稅費、流動性及合約乘數。",
        "conservative_entry_reference": format(entry, "f"),
        "stop_price": format(stop, "f"),
        "target_price": format(target, "f"),
        "net_risk_per_unit": format(risk, "f"),
        "net_reward_per_unit": format(reward, "f"),
        "net_reward_to_risk_ratio": format(ratio, ".8f"),
    }


def build_analysis(archive_path: Path, config: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    config = validate_config(config)
    report_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    metadata, candles, spots, raw_events = _load_archive(archive_path)
    symbol = str(metadata.get("symbol", "")).upper()
    if not symbol:
        raise ValueError("metadata.json 缺少交易對。")
    sources = metadata.get("sources")
    if not isinstance(sources, dict) or sources.get("spot") != binance_market_data.SPOT_BASE:
        raise ValueError("metadata.json 未確認 Binance 官方現貨 REST API 資料來源。")
    server_time_value = metadata.get("binance_server_time")
    if type(server_time_value) is not int or server_time_value < 0:
        raise ValueError("metadata.json 缺少有效 Binance server time。")
    server_time = server_time_value
    archive_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    integrity = _integrity_reports(candles, metadata, symbol, server_time)
    latest_candles: dict[str, dict[str, Any]] = {}
    candle_errors: list[str] = []
    for interval in INTERVALS:
        interval_rows = [row for row in candles if row.get("symbol") == symbol and row.get("interval") == interval]
        closed_rows = [row for row in interval_rows if str(row.get("is_closed", "")).lower() == "true"]
        if not closed_rows:
            candle_errors.append(f"{interval} 沒有可用的已收盤 K 線。")
            continue
        try:
            latest = max(closed_rows, key=lambda row: int(row.get("open_time", "")))
            if int(latest["close_time"]) >= server_time:
                candle_errors.append(f"{interval} 最新標記已收盤的 K 線尚未早於 Binance server time。")
                continue
            latest_candles[interval] = {
                key: latest.get(key, "") for key in ("open_time", "open_time_utc", "close_time", "close_time_utc", "open", "high", "low", "close", "volume", "source")
            }
        except (KeyError, TypeError, ValueError):
            candle_errors.append(f"{interval} 最新 K 線時間戳無效。")

    valid_events, rejected_events = _validated_events(raw_events, candles, symbol, server_time)
    quote_check, current_price = _quote(spots, symbol, report_now, config["max_quote_age_seconds"])
    structure_by_interval = {
        interval: _latest_event(valid_events, interval, "structure_break")
        for interval in config["rules"]["alignment_intervals"]
    }
    structure_summary = {
        interval: _event_view(event) if event else {"status": "no_confirmed_structure_break"}
        for interval, event in structure_by_interval.items()
    }
    outcome: dict[str, Any] = {
        "report_type": "rule_based_market_research",
        "report_version": "1",
        "analysis_id": "",
        "generated_at_utc": report_now.isoformat(),
        "symbol": symbol,
        "source": {"name": "Binance public REST API", "archive": archive_path.name, "archive_sha256": archive_hash},
        "strategy_version": config["strategy_version"],
        "configuration": {key: config[key] for key in ("minimum_net_rr", "max_quote_age_seconds", "fee_bps_per_side", "slippage_bps_per_side", "max_setup_age_hours", "rules")},
        "status": "不適合開單",
        "execution_state": "無",
        "reasons": [],
        "checks": {
            "data_integrity": integrity,
            "closed_candles": {"status": "available" if not candle_errors else "unavailable", "latest_by_interval": latest_candles, "errors": candle_errors},
            "quote_freshness": quote_check,
            "multi_timeframe_structure": {"status": "insufficient_data", "latest_confirmed_structure_breaks": structure_summary},
            "event_evidence": {"accepted_event_count": len(valid_events), "rejected_event_count": len(rejected_events), "rejected_events": rejected_events},
        },
        "scenario": None,
        "evidence": {"structure_breaks": structure_summary, "setup_order_block": None, "target_swing": None, "context_events": []},
        "limitations": [
            "這是依設定規則產生的研究情境，不是保證、個人化投資建議或成交確認。",
            "現價進入 OB 區只代表價格條件被觸及；未檢查委託簿、流動性、成交機率或盤中路徑。",
            "固定使用結構突破、OB 回踩及波段目標規則；OB/FVG/2B 定義是程式化慣例，尚未做績效或勝率驗證。",
        ],
    }

    def no_trade(reason: str) -> dict[str, Any]:
        if reason not in outcome["reasons"]:
            outcome["reasons"].append(reason)
        return outcome

    if any(not report.get("integrity_ok") for report in integrity.values()):
        return no_trade("K 線資料有缺口、錯誤、完整性報告不一致或缺少報告；本策略設定要求三個週期資料連續才評估候選。")
    if candle_errors or len(latest_candles) != len(INTERVALS):
        return no_trade("無法確認所有週期的最新已收盤 K 線。")
    if quote_check.get("status") != "fresh" or current_price is None:
        return no_trade(f"現貨報價不可用或不新鮮（狀態：{quote_check.get('status')}）。")

    directions = {event.get("direction") for event in structure_by_interval.values() if event}
    if any(event is None for event in structure_by_interval.values()):
        outcome["checks"]["multi_timeframe_structure"]["status"] = "insufficient_data"
        return no_trade("日線、4H、1H 未全部找到已收盤確認的結構突破。")
    if len(directions) != 1 or not directions.issubset({"bullish", "bearish"}):
        outcome["checks"]["multi_timeframe_structure"]["status"] = "mixed"
        return no_trade("日線、4H、1H 最近已確認的結構突破方向不一致。")
    direction = next(iter(directions))
    side = "long" if direction == "bullish" else "short"
    outcome["checks"]["multi_timeframe_structure"]["status"] = "aligned"
    outcome["checks"]["multi_timeframe_structure"]["direction"] = direction

    latest_1h_break = structure_by_interval["1h"]
    latest_break_time = int(latest_1h_break["confirmed_at_close_time"])
    all_setups = [
        event for event in valid_events
        if event.get("interval") == config["rules"]["entry_interval"]
        and event.get("event_type") == config["rules"]["entry_event_type"]
        and event.get("direction") == direction
        and int(event["confirmed_at_close_time"]) >= latest_break_time
    ]
    if not all_setups:
        return no_trade("最新 1H 同向結構突破後沒有具備有效證據的同向 OB。")
    unmitigated: list[tuple[dict[str, Any], Decimal, Decimal, float]] = []
    too_old = False
    for candidate in sorted(all_setups, key=lambda event: int(event["confirmed_at_close_time"]), reverse=True):
        candidate_time = int(candidate["confirmed_at_close_time"])
        age_hours = max(0, (server_time - (candidate_time + 1)) / 3_600_000)
        if age_hours > config["max_setup_age_hours"]:
            too_old = True
            continue
        try:
            candidate_low = _decimal(candidate.get("price_low"), "OB 區間下緣", positive=True)
            candidate_high = _decimal(candidate.get("price_high"), "OB 區間上緣", positive=True)
        except ValueError:
            continue
        if candidate_low >= candidate_high or _zone_was_retested(candles, candidate, side, candidate_low, candidate_high):
            continue
        unmitigated.append((candidate, candidate_low, candidate_high, age_hours))
    if not unmitigated:
        if too_old:
            outcome["checks"]["setup_freshness"] = {"status": "stale", "maximum_age_hours": config["max_setup_age_hours"]}
            return no_trade("同向 1H OB 已超過設定的最長有效時間。")
        return no_trade("同向 1H OB 不存在、價位無效，或已被其後已收盤 K 線回測。")
    setup, zone_low, zone_high, setup_age_hours = unmitigated[0]
    setup_time = int(setup["confirmed_at_close_time"])
    outcome["evidence"]["setup_order_block"] = _event_view(setup)
    outcome["checks"]["setup_freshness"] = {"status": "fresh", "age_hours": round(setup_age_hours, 3), "maximum_age_hours": config["max_setup_age_hours"]}

    if side == "long":
        stop = zone_low
        entry_reference = zone_high
        target_type = "swing_high"
        beyond = lambda price: price > zone_high
    else:
        stop = zone_high
        entry_reference = zone_low
        target_type = "swing_low"
        beyond = lambda price: price < zone_low
    target_candidates = []
    for event in valid_events:
        if event.get("interval") != config["rules"]["target_interval"] or event.get("event_type") != target_type:
            continue
        if int(event["confirmed_at_close_time"]) <= setup_time:
            continue
        try:
            target_price = _decimal(event.get("reference_price"), "波段目標", positive=True)
        except ValueError:
            continue
        if beyond(target_price):
            if _target_was_reached(candles, event, side, target_price):
                continue
            target_candidates.append((abs(target_price - entry_reference), event, target_price))
    if not target_candidates:
        return no_trade("OB 確認後沒有位於預期方向、且超出進場區的已確認 1H 波段目標。")
    _, target_event, target = min(target_candidates, key=lambda item: (item[0], int(item[1]["confirmed_at_close_time"])))
    outcome["evidence"]["target_swing"] = _event_view(target_event)
    outcome["evidence"]["context_events"] = [
        _event_view(event) for event in valid_events
        if event.get("interval") == "1h" and event.get("event_type") in {"fair_value_gap", "two_b_fakeout"}
        and int(event["confirmed_at_close_time"]) >= latest_break_time
    ]
    if (side == "long" and current_price >= target) or (side == "short" and current_price <= target):
        return no_trade("現價已到達或越過規則設定的目標波段，原候選情境失效。")
    if (side == "long" and current_price <= stop) or (side == "short" and current_price >= stop):
        return no_trade("現價已到達或越過 OB 外緣止損參考，原候選情境失效。")

    costs = _net_risk_reward(
        side, entry_reference, stop, target,
        _decimal(config["fee_bps_per_side"], "單邊費率", nonnegative=True),
        _decimal(config["slippage_bps_per_side"], "單邊滑價", nonnegative=True),
    )
    ratio = Decimal(costs["net_reward_to_risk_ratio"])
    minimum_rr = _decimal(config["minimum_net_rr"], "最低淨風險報酬比", positive=True)
    if Decimal(costs["net_reward_per_unit"]) <= 0 or ratio < minimum_rr:
        outcome["checks"]["cost_adjusted_risk_reward"] = {**costs, "status": "below_user_threshold", "minimum_net_rr": format(minimum_rr, "f")}
        return no_trade("扣除設定費率與滑價後的風險報酬比未達個人最低門檻。")
    outcome["checks"]["cost_adjusted_risk_reward"] = {**costs, "status": "meets_user_threshold", "minimum_net_rr": format(minimum_rr, "f")}

    inside_zone = zone_low <= current_price <= zone_high
    outcome["status"] = "候選做多" if side == "long" else "候選做空"
    outcome["execution_state"] = "目前已觸發" if inside_zone else "等待觸發"
    outcome["scenario"] = {
        "side": side,
        "entry_zone": {"low": format(zone_low, "f"), "high": format(zone_high, "f"), "condition": "現價回到此 OB 區間"},
        "conservative_entry_reference": format(entry_reference, "f"),
        "stop_price": format(stop, "f"),
        "target_price": format(target, "f"),
        "trigger_state_basis": {"spot_price": format(current_price, "f"), "quote_captured_at_utc": quote_check["captured_at_utc"], "quote_age_seconds": quote_check["age_seconds"]},
        "risk_reward": outcome["checks"]["cost_adjusted_risk_reward"],
        "entry_event_id": setup.get("event_id", ""),
        "target_event_id": target_event.get("event_id", ""),
    }
    outcome["reasons"].append("所有已設定檢核均通過；觸發狀態只依新鮮現貨報價是否位於 OB 區間判定。")
    return outcome


def save_analysis(report: dict[str, Any], records_dir: Path) -> Path:
    stamp = datetime.fromisoformat(report["generated_at_utc"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    analysis_id = f"{report['symbol']}-{stamp}-{report['source']['archive_sha256'][:12]}"
    report["analysis_id"] = analysis_id
    directory = records_dir / "analyses"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{analysis_id}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def review_trade(
    analysis: dict[str, Any], *, actual_entry: Any, actual_exit: Any, quantity: Any,
    actual_fees: Any, opened_at: str, closed_at: str, notes: str,
    actual_side: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    entry = _decimal(actual_entry, "實際平均進場價", positive=True)
    exit_price = _decimal(actual_exit, "實際平均平倉價", positive=True)
    qty = _decimal(quantity, "成交數量", positive=True)
    fees = _decimal(actual_fees, "實際費用", nonnegative=True)
    opened = _iso_datetime(opened_at)
    closed = _iso_datetime(closed_at)
    if opened is None or closed is None or closed < opened:
        raise ValueError("開倉與平倉時間必須是含時區的 ISO 時間，且平倉不得早於開倉。")
    plan_side = (analysis.get("scenario") or {}).get("side")
    side = actual_side or plan_side
    if side not in {"long", "short"}:
        raise ValueError("此分析沒有候選方向；請在復盤時明確提供 --side long 或 --side short。")
    gross_pnl: Decimal | None = None
    net_pnl: Decimal | None = None
    actual_r: Decimal | None = None
    plan = analysis.get("scenario") or {}
    if side in {"long", "short"}:
        multiplier = Decimal(1) if side == "long" else Decimal(-1)
        gross_pnl = (exit_price - entry) * qty * multiplier
        net_pnl = gross_pnl - fees
        try:
            planned_stop = _decimal(plan.get("stop_price"), "計畫止損價", positive=True)
            risk_amount = abs(entry - planned_stop) * qty
        except ValueError:
            risk_amount = Decimal(0)
        if risk_amount > 0 and side == plan_side:
            actual_r = net_pnl / risk_amount
    config = analysis.get("configuration", {})
    fee_bps = _decimal(config.get("fee_bps_per_side", 0), "設定單邊費率", nonnegative=True)
    planned_entry = _decimal(plan.get("conservative_entry_reference", 0), "計畫進場參考價", positive=True) if plan.get("conservative_entry_reference") else None
    planned_target = _decimal(plan.get("target_price", 0), "計畫目標價", positive=True) if plan.get("target_price") else None
    planned_fee_estimate = (planned_entry + planned_target) * qty * fee_bps / Decimal(10_000) if planned_entry is not None and planned_target is not None else None
    return {
        "record_type": "manual_trade_review",
        "created_at_utc": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "analysis_id": analysis.get("analysis_id", ""),
        "symbol": analysis.get("symbol", ""),
        "side": side,
        "planned_side": plan_side,
        "plan": {key: plan.get(key) for key in ("entry_zone", "conservative_entry_reference", "stop_price", "target_price")},
        "actual": {
            "average_entry": format(entry, "f"), "average_exit": format(exit_price, "f"),
            "quantity": format(qty, "f"), "fees_paid_quote_asset": format(fees, "f"),
            "opened_at": opened.isoformat(), "closed_at": closed.isoformat(),
        },
        "comparison": {
            "entry_vs_plan_price_difference": format(entry - planned_entry, "f") if planned_entry is not None else None,
            "exit_vs_plan_target_difference": format(exit_price - planned_target, "f") if planned_target is not None else None,
            "actual_fees_quote_asset": format(fees, "f"),
            "planned_fee_estimate_quote_asset_excluding_slippage": format(planned_fee_estimate, "f") if planned_fee_estimate is not None else None,
        },
        "result": {
            "gross_pnl_quote_asset": format(gross_pnl, "f") if gross_pnl is not None else None,
            "net_pnl_after_entered_fees_quote_asset": format(net_pnl, "f") if net_pnl is not None else None,
            "actual_r_multiple": format(actual_r, ".8f") if actual_r is not None else None,
            "r_definition": "扣除使用者填入費用後的實際損益，除以實際平均進場價至原計畫止損距離乘數量；分母為價格風險，不含費用。資料不足時留空。",
        },
        "review_notes": notes,
        "strategy_update": "none; 此筆復盤只供日後檢視，不會自動修改規則。",
    }


def append_trade_review(review: dict[str, Any], records_dir: Path) -> Path:
    directory = records_dir / "trade_reviews"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "reviews.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(review, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def print_summary(report: dict[str, Any], output_fn: Callable[[str], None] = print) -> None:
    output_fn(f"{report['symbol']} 分析結論：{report['status']}｜{report['execution_state']}")
    integrity = report["checks"]["data_integrity"]
    integrity_text = "完整" if all(item.get("integrity_ok") for item in integrity.values()) else "不完整或有缺口"
    quote = report["checks"]["quote_freshness"]
    quote_text = f"{quote.get('price', '無資料')}（{quote.get('status')}，{quote.get('captured_at_utc', '無時間戳')}）"
    output_fn(f"資料完整性：{integrity_text}；現貨報價：{quote_text}")
    for interval, event in report["evidence"]["structure_breaks"].items():
        if event.get("event_id"):
            output_fn(f"{interval} 結構突破：{event.get('direction')}，確認時間 {event.get('confirmed_at_utc')}，事件 {event.get('event_id')}")
        else:
            output_fn(f"{interval} 結構突破：資料不足")
    scenario = report.get("scenario")
    if scenario:
        output_fn(f"進場區：{scenario['entry_zone']['low']}–{scenario['entry_zone']['high']}；觸發條件：{scenario['entry_zone']['condition']}")
        output_fn(f"止損參考：{scenario['stop_price']}；目標：{scenario['target_price']}；扣成本淨 RR：{scenario['risk_reward']['net_reward_to_risk_ratio']}")
        output_fn(f"OB 事件：{scenario['entry_event_id']}；目標波段事件：{scenario['target_event_id']}")
    for reason in report.get("reasons", []):
        output_fn(f"檢核：{reason}")
    output_fn(f"資料包 SHA-256：{report['source']['archive_sha256']}")


def _analyze_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="以設定規則分析 Binance 市場資料；僅研究，不下單。")
    parser.add_argument("symbol", nargs="?", help="幣種或交易對，例如 BTC 或 BTCUSDT")
    parser.add_argument("--setup", action="store_true", help="首次設定策略與個人門檻")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--records-dir", type=Path, default=DEFAULT_RECORDS_DIR)
    args = parser.parse_args(argv)
    if args.setup:
        configure(args.config)
        if not args.symbol:
            return 0
    try:
        config = load_config(args.config)
    except ValueError:
        if args.setup:
            raise
        if not sys.stdin.isatty():
            raise
        configure(args.config)
        config = load_config(args.config)
    if not args.symbol:
        parser.error("設定完成後請輸入幣種，例如 BTC。")
    symbol = normalize_symbol(args.symbol)
    archive = binance_market_data.fetch_market_data(symbol, args.data_dir)
    report = build_analysis(archive, config)
    path = save_analysis(report, args.records_dir)
    print_summary(report)
    print(f"分析紀錄已儲存：{path}")
    return 0


def _journal_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="以人工填寫的實際成交資料建立復盤。")
    parser.add_argument("analysis_id", help="當時分析紀錄 ID")
    parser.add_argument("--records-dir", type=Path, default=DEFAULT_RECORDS_DIR)
    parser.add_argument("--entry", required=True, help="實際平均進場價")
    parser.add_argument("--exit", dest="actual_exit", required=True, help="實際平均平倉價")
    parser.add_argument("--quantity", required=True, help="實際成交數量")
    parser.add_argument("--fees", required=True, help="實際總費用，以該交易對的報價資產計")
    parser.add_argument("--side", choices=("long", "short"), help="原分析沒有候選方向，或實際方向不同時必填")
    parser.add_argument("--opened-at", required=True, help="含時區 ISO 時間")
    parser.add_argument("--closed-at", required=True, help="含時區 ISO 時間")
    parser.add_argument("--notes", required=True, help="人工復盤筆記")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Z0-9]{5,20}-\d{8}T\d{12}Z-[0-9a-f]{12}", args.analysis_id):
        print("錯誤：分析紀錄 ID 格式不正確。", file=sys.stderr)
        return 1
    analysis_path = args.records_dir / "analyses" / f"{args.analysis_id}.json"
    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        if analysis.get("analysis_id") != args.analysis_id:
            raise ValueError("分析紀錄 ID 不符。")
        review = review_trade(
            analysis, actual_entry=args.entry, actual_exit=args.actual_exit,
            quantity=args.quantity, actual_fees=args.fees, opened_at=args.opened_at,
            closed_at=args.closed_at, notes=args.notes, actual_side=args.side,
        )
        path = append_trade_review(review, args.records_dir)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(review, ensure_ascii=False, indent=2))
    print(f"復盤已追加至：{path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "journal":
        return _journal_main(args[1:])
    try:
        return _analyze_main(args)
    except (binance_market_data.BinanceAPIError, OSError, ValueError, EOFError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
