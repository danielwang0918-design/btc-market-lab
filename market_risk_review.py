#!/usr/bin/env python3
"""Create an auditable, research-only review of a user-specified price scenario."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

INTERVALS = ("1d", "4h", "1h")


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


def _read_csv(archive: zipfile.ZipFile, member: str) -> list[dict[str, str]]:
    with archive.open(member) as source:
        text = io.TextIOWrapper(source, encoding="utf-8-sig", newline="")
        return list(csv.DictReader(text))


def _read_bundle(path: Path) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            required = {"metadata.json", "spot_price.csv", "market_structure.csv"}
            missing = sorted(required - names)
            if missing:
                raise ValueError(f"資料包缺少必要檔案：{', '.join(missing)}。")
            metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
            spot_rows = _read_csv(archive, "spot_price.csv")
            structure_rows = _read_csv(archive, "market_structure.csv")
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"無法讀取資料包：{exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("metadata.json 格式錯誤。")
    return metadata, spot_rows, structure_rows


def _integrity_check(metadata: dict[str, Any]) -> dict[str, Any]:
    integrity = metadata.get("data_integrity")
    by_interval = integrity.get("intervals") if isinstance(integrity, dict) else None
    reports: dict[str, Any] = {}
    all_complete = True
    for interval in INTERVALS:
        report = by_interval.get(interval) if isinstance(by_interval, dict) else None
        if not isinstance(report, dict):
            reports[interval] = {"status": "missing_report", "missing_candle_count": None, "gap_count": None}
            all_complete = False
            continue
        status = str(report.get("status", "unknown"))
        gaps = report.get("gaps") if isinstance(report.get("gaps"), list) else []
        errors = report.get("errors") if isinstance(report.get("errors"), list) else []
        reports[interval] = {
            "status": status,
            "is_contiguous": report.get("is_contiguous"),
            "missing_candle_count": report.get("missing_candle_count"),
            "gap_count": len(gaps),
            "error_count": len(errors),
            "gaps": gaps,
        }
        if status != "complete" or gaps or errors or report.get("is_contiguous") is not True:
            all_complete = False
    return {
        "status": "complete" if all_complete else "incomplete_or_gapped",
        "all_intervals_contiguous": all_complete,
        "intervals": reports,
        "effect": "完整性不完整時，報告仍列出情境計算，但不應把跨週期比較視為完整資料檢核。",
    }


def _quote_check(
    spot_rows: list[dict[str, str]], symbol: str, now: datetime, max_age_seconds: int
) -> tuple[dict[str, Any], Decimal | None]:
    candidates = [row for row in spot_rows if row.get("symbol") == symbol]
    if not candidates:
        return {"status": "unavailable", "detail": "找不到對應交易對的現貨報價。"}, None
    row = candidates[-1]
    try:
        price = _decimal(row.get("price"), "現貨最新報價", positive=True)
    except ValueError as exc:
        return {"status": "invalid_price", "detail": str(exc)}, None
    captured = _iso_datetime(row.get("captured_at_utc"))
    if captured is None:
        return {"status": "invalid_timestamp", "captured_at_utc": row.get("captured_at_utc", "")}, price
    age_seconds = (now - captured).total_seconds()
    status = "fresh" if 0 <= age_seconds <= max_age_seconds else ("future_timestamp" if age_seconds < 0 else "stale")
    return {
        "status": status,
        "price": format(price, "f"),
        "captured_at_utc": captured.isoformat(),
        "age_seconds": round(age_seconds, 3),
        "maximum_age_seconds": max_age_seconds,
        "source": row.get("source", ""),
    }, price


def _latest_structure_by_interval(
    rows: list[dict[str, str]], server_time: int | None
) -> dict[str, dict[str, Any]]:
    parsed_server_time: int | None = server_time if type(server_time) is int else None
    result: dict[str, dict[str, Any]] = {}
    for interval in INTERVALS:
        candidates: list[tuple[int, dict[str, str]]] = []
        for row in rows:
            if row.get("interval") != interval or row.get("event_type") != "structure_break":
                continue
            try:
                confirmed_at = int(row.get("confirmed_at_close_time", ""))
            except (TypeError, ValueError):
                continue
            if parsed_server_time is not None and confirmed_at >= parsed_server_time:
                continue
            if row.get("direction") not in {"bullish", "bearish"}:
                continue
            candidates.append((confirmed_at, row))
        if not candidates:
            result[interval] = {"status": "no_confirmed_structure_break"}
            continue
        confirmed_at, event = max(candidates, key=lambda item: item[0])
        result[interval] = {
            "status": "available",
            "direction": event.get("direction", ""),
            "confirmed_at_close_time": confirmed_at,
            "confirmed_at_utc": event.get("confirmed_at_utc", ""),
            "age_milliseconds_at_capture": parsed_server_time - confirmed_at if parsed_server_time is not None else None,
            "reference_price": event.get("reference_price", ""),
            "source": event.get("source", ""),
            "event_id": event.get("event_id", ""),
        }
    return result


def _structure_comparison(
    structures: dict[str, dict[str, Any]], side: str
) -> dict[str, Any]:
    expected = "bullish" if side == "long" else "bearish"
    directions = [structures[i].get("direction") for i in INTERVALS if structures[i].get("status") == "available"]
    if len(directions) != len(INTERVALS):
        consistency = "insufficient_data"
    elif len(set(directions)) == 1:
        consistency = "aligned"
    else:
        consistency = "mixed"
    comparisons = {
        interval: (
            "matches_user_scenario" if item.get("direction") == expected else "differs_from_user_scenario"
        ) if item.get("status") == "available" else "not_available"
        for interval, item in structures.items()
    }
    return {
        "status": consistency,
        "user_scenario_side": side,
        "expected_structure_label_for_comparison": expected,
        "intervals": structures,
        "comparison_to_user_scenario": comparisons,
        "interpretation": "這只是已確認 structure_break 標記的方向比對，不構成趨勢判斷或交易建議。",
    }


def _latest_swing(
    rows: list[dict[str, str]], interval: str, side: str, server_time: int | None
) -> dict[str, Any] | None:
    wanted = "swing_low" if side == "long" else "swing_high"
    candidates: list[tuple[int, dict[str, str]]] = []
    for row in rows:
        if row.get("interval") != interval or row.get("event_type") != wanted:
            continue
        try:
            confirmed_at = int(row.get("confirmed_at_close_time", ""))
            price = _decimal(row.get("reference_price"), "結構參考價", positive=True)
        except (ValueError, TypeError):
            continue
        if server_time is not None and confirmed_at >= server_time:
            continue
        candidates.append((confirmed_at, row))
    if not candidates:
        return None
    confirmed_at, row = max(candidates, key=lambda item: item[0])
    return {
        "event_type": wanted,
        "interval": interval,
        "reference_price": row.get("reference_price", ""),
        "confirmed_at_close_time": confirmed_at,
        "confirmed_at_utc": row.get("confirmed_at_utc", ""),
        "age_milliseconds_at_capture": server_time - confirmed_at if server_time is not None else None,
        "event_id": row.get("event_id", ""),
        "source": row.get("source", ""),
    }


def build_review(
    archive_path: Path,
    *,
    side: str,
    entry: Any,
    stop: Any,
    target: Any,
    fee_bps_per_side: Any,
    slippage_bps_per_side: Any,
    max_quote_age_seconds: int,
    invalidation_interval: str,
    minimum_net_rr: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if side not in {"long", "short"}:
        raise ValueError("情境方向必須是 long 或 short。")
    if invalidation_interval not in INTERVALS:
        raise ValueError("結構參考週期必須是 1d、4h 或 1h。")
    if type(max_quote_age_seconds) is not int or max_quote_age_seconds < 0:
        raise ValueError("報價最長允許秒數必須是非負整數。")
    entry_price = _decimal(entry, "進場價", positive=True)
    stop_price = _decimal(stop, "止損價", positive=True)
    target_price = _decimal(target, "目標價", positive=True)
    fee_bps = _decimal(fee_bps_per_side, "單邊費率（bps）", nonnegative=True)
    slippage_bps = _decimal(slippage_bps_per_side, "單邊滑價（bps）", nonnegative=True)
    min_rr = _decimal(minimum_net_rr, "最低淨風險報酬比", positive=True) if minimum_net_rr is not None else None

    metadata, spot_rows, structure_rows = _read_bundle(archive_path)
    symbol = str(metadata.get("symbol", ""))
    if not symbol:
        raise ValueError("metadata.json 缺少交易對。")
    structure_rows = [row for row in structure_rows if row.get("symbol") == symbol]
    server_time_value = metadata.get("binance_server_time")
    server_time = server_time_value if type(server_time_value) is int else None
    report_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if side == "long":
        ordered = stop_price < entry_price < target_price
        order_rule = "stop < entry < target"
    else:
        ordered = target_price < entry_price < stop_price
        order_rule = "target < entry < stop"
    checks: dict[str, Any] = {
        "data_integrity": _integrity_check(metadata),
        "quote_freshness": None,
        "scenario_price_order": {
            "status": "valid" if ordered else "invalid",
            "required_order": order_rule,
            "stop_is_on_loss_side_of_entry": stop_price < entry_price if side == "long" else stop_price > entry_price,
            "target_is_on_profit_side_of_entry": target_price > entry_price if side == "long" else target_price < entry_price,
        },
        "stop_vs_latest_confirmed_swing": None,
        "net_risk_reward": None,
    }
    quote_check, current_price = _quote_check(spot_rows, symbol, report_now, max_quote_age_seconds)
    checks["quote_freshness"] = quote_check
    if current_price is not None:
        quote_check["distance_from_entry_percent"] = format((current_price - entry_price) / entry_price * Decimal(100), "f")

    structures = _latest_structure_by_interval(structure_rows, server_time)
    structure_comparison = _structure_comparison(structures, side)
    swing = _latest_swing(structure_rows, invalidation_interval, side, server_time)
    if swing is None:
        checks["stop_vs_latest_confirmed_swing"] = {
            "status": "reference_unavailable",
            "selected_interval": invalidation_interval,
            "detail": "所選週期沒有可用的最新已確認反向波段標記。",
        }
    else:
        swing_price = _decimal(swing["reference_price"], "結構參考價", positive=True)
        stop_beyond = stop_price <= swing_price if side == "long" else stop_price >= swing_price
        checks["stop_vs_latest_confirmed_swing"] = {
            "status": "stop_at_or_beyond_reference" if stop_beyond else "stop_inside_or_before_reference",
            "selected_interval": invalidation_interval,
            "comparison": "long 止損價 <= 最近已確認波段低點" if side == "long" else "short 止損價 >= 最近已確認波段高點",
            "user_stop_price": format(stop_price, "f"),
            "reference": swing,
            "interpretation": "僅回報價位相對位置；波段參考不代表普遍適用的止損規則。",
        }

    if not ordered:
        checks["net_risk_reward"] = {"status": "not_calculated", "detail": "情境價格順序不符合所選方向。"}
    else:
        gross_risk = abs(entry_price - stop_price)
        gross_reward = abs(target_price - entry_price)
        cost_rate = (fee_bps + slippage_bps) / Decimal(10_000)
        entry_cost = entry_price * cost_rate
        stop_exit_cost = stop_price * cost_rate
        target_exit_cost = target_price * cost_rate
        net_risk = gross_risk + entry_cost + stop_exit_cost
        net_reward = gross_reward - entry_cost - target_exit_cost
        ratio = net_reward / net_risk if net_risk > 0 else None
        rr_status = "calculated"
        threshold_result: str | None = None
        if min_rr is not None and ratio is not None:
            threshold_result = "meets_user_threshold" if ratio >= min_rr else "below_user_threshold"
        checks["net_risk_reward"] = {
            "status": rr_status,
            "assumed_quantity": "1 base-asset unit",
            "fee_bps_per_fill": format(fee_bps, "f"),
            "slippage_bps_per_fill": format(slippage_bps, "f"),
            "cost_model": "每個進出場分別按名目價格套用費率與滑價；不含點差、資金費率、借幣成本、稅費及合約乘數。",
            "gross_risk_per_unit": format(gross_risk, "f"),
            "gross_reward_per_unit": format(gross_reward, "f"),
            "entry_cost_per_unit": format(entry_cost, "f"),
            "stop_exit_cost_per_unit": format(stop_exit_cost, "f"),
            "target_exit_cost_per_unit": format(target_exit_cost, "f"),
            "net_risk_per_unit": format(net_risk, "f"),
            "net_reward_per_unit": format(net_reward, "f"),
            "net_reward_to_risk_ratio": format(ratio, ".8f") if ratio is not None else None,
            "minimum_net_rr_user_threshold": format(min_rr, "f") if min_rr is not None else None,
            "threshold_comparison": threshold_result,
        }

    try:
        archive_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"無法計算輸入資料包雜湊：{exc}") from exc
    return {
        "report_type": "research_only_scenario_review",
        "report_version": "1",
        "generated_at_utc": report_now.isoformat(),
        "input_archive": archive_path.name,
        "input_archive_sha256": archive_digest,
        "symbol": symbol,
        "scenario": {
            "side": side,
            "entry_price": format(entry_price, "f"),
            "stop_price": format(stop_price, "f"),
            "target_price": format(target_price, "f"),
            "minimum_net_rr_user_threshold": format(min_rr, "f") if min_rr is not None else None,
            "selected_invalidation_reference_interval": invalidation_interval,
        },
        "checks": checks,
        "market_structure_context": structure_comparison,
        "limitations": [
            "本報告只檢查使用者提供的價格情境與資料狀態，不產生買賣訊號、進出場建議、勝率或績效宣稱。",
            "多週期比較只使用資料包中的最近已確認 structure_break 標記；缺少標記會列為資料不足。",
            "費率與滑價由使用者提供，成本模型為每單位線性近似，未涵蓋點差、資金費率、借幣成本、稅費、流動性、成交機率或合約乘數。",
            "資料完整性與報價新鮮度是獨立檢核；風險報酬計算不會自動替代或豁免這些檢核。",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="對既有 Binance 資料包執行研究用途的情境風險檢核。")
    parser.add_argument("archive", type=Path, help="資料擷取工具產生的 ZIP 檔案")
    parser.add_argument("--side", choices=("long", "short"), required=True, help="使用者提供的情境方向；不會由程式推測")
    parser.add_argument("--entry", required=True, help="使用者提供的情境進場價")
    parser.add_argument("--stop", required=True, help="使用者提供的情境止損價")
    parser.add_argument("--target", required=True, help="使用者提供的情境目標價")
    parser.add_argument("--fee-bps-per-side", required=True, help="單次成交費率假設，單位 bps")
    parser.add_argument("--slippage-bps-per-side", required=True, help="每次成交滑價假設，單位 bps")
    parser.add_argument("--max-quote-age-seconds", type=int, required=True, help="使用者設定的報價最長允許秒數")
    parser.add_argument("--invalidation-interval", choices=INTERVALS, required=True, help="比較止損價所用的已確認波段週期")
    parser.add_argument("--minimum-net-rr", help="選填：使用者自行設定的最低淨風險報酬比門檻")
    parser.add_argument("--output", type=Path, help="JSON 報告路徑（預設為 ZIP 同目錄的 *_risk_review.json）")
    args = parser.parse_args(argv)
    try:
        report = build_review(
            args.archive,
            side=args.side,
            entry=args.entry,
            stop=args.stop,
            target=args.target,
            fee_bps_per_side=args.fee_bps_per_side,
            slippage_bps_per_side=args.slippage_bps_per_side,
            max_quote_age_seconds=args.max_quote_age_seconds,
            invalidation_interval=args.invalidation_interval,
            minimum_net_rr=args.minimum_net_rr,
        )
        output = args.output or args.archive.with_name(f"{args.archive.stem}_risk_review.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    print(f"情境檢核報告已建立：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
