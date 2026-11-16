#!/usr/bin/env python3
"""BTC-first, source-attributed event risk inspection. No network calls or orders."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile, BadZipFile

import event_risk

VERSION = "p2-risk-inspection-v1"
UTC = timezone.utc
# Data freshness limits (hours), not predictive thresholds or optimized strategy parameters.
MAX_AGE_HOURS = {
    "btc": 2, "eth": 2, "volume": 2, "oi": 2, "funding": 12,
    "long_short": 2, "liquidation": 2, "orderbook": 1,
    "etf": 120, "macro": 168, "news": 48, "technical": 8, "heatmap": 2,
}
REQUIRED = tuple(MAX_AGE_HOURS)
GATES = ("4h_structure", "2b", "ob", "fvg", "heatmap", "volume", "oi", "funding", "net_rr")
GATE_CATEGORIES = {
    "4h_structure": "technical", "2b": "technical", "ob": "technical",
    "fvg": "technical", "heatmap": "heatmap", "volume": "volume",
    "oi": "oi", "funding": "funding", "net_rr": "technical",
}
SECTIONS = (
    "1. 巡檢時間與資料截止時間", "2. 最終風險等級", "3. BTC 市場異常檢查",
    "4. ETH 市場輔助檢查", "5. 衍生品與清算分析",
    "6. 宏觀經濟及國際新聞", "7. ETF 與資金流分析",
    "8. 2B、OB、FVG 與清算熱力圖交叉分析",
    "9. 已提供來源的事實、合理推論、無法取得的資料",
    "10. 接下來需要關注的事件及價格結構", "11. 最終風險判斷",
)


def parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("時間必須是含時區 ISO 8601 字串")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("無效 ISO 8601 時間") from exc
    if dt.tzinfo is None:
        raise ValueError("時間必須包含時區")
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def valid_source(url: str) -> bool:
    try:
        p = urlsplit(url)
        return p.scheme == "https" and bool(p.hostname) and not p.username and not p.password
    except ValueError:
        return False


def read_csv_member(z: ZipFile, name: str) -> list[dict[str, str]]:
    if name not in z.namelist():
        return []
    with z.open(name) as raw:
        return list(csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")))


def bundle_observations(path: Path) -> tuple[list[dict], dict]:
    """Read existing Binance collector fields; do not reinterpret venue/funding units."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with ZipFile(path) as z:
        if len(z.namelist()) != len(set(z.namelist())):
            raise ValueError("ZIP 包含重複檔名")
        metadata = json.loads(z.read("metadata.json").decode("utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("metadata.json 非物件")
        symbol = metadata.get("symbol")
        if symbol not in ("BTCUSDT", "ETHUSDT"):
            raise ValueError("僅接受 BTCUSDT 或 ETHUSDT Binance bundle")
        spot = "https://api.binance.com/api/v3/ticker/price?symbol=" + symbol
        stats = "https://api.binance.com/api/v3/ticker/24hr?symbol=" + symbol
        oi_url = "https://fapi.binance.com/fapi/v1/openInterest?symbol=" + symbol
        funding_url = "https://fapi.binance.com/fapi/v1/fundingRate?symbol=" + symbol
        results: list[dict] = []

        def add(category: str, metric: str, value: str, when: str, url: str, unit: str, *, detail: str = "") -> None:
            if not value or not when:
                return
            results.append({
                "id": f"{symbol}:{category}:{metric}", "category": category,
                "metric": metric, "value": value, "unit": unit,
                "observed_at_utc": when, "source_name": "Binance Public API",
                "source_url": url,
                "scope": f"Binance {symbol}; " + ("USDⓈ-M futures" if category in {"oi", "funding"} else "spot"),
                "detail": detail, "status": "sourced",
            })

        for row in read_csv_member(z, "spot_price.csv"):
            if row.get("symbol") == symbol:
                add("btc" if symbol == "BTCUSDT" else "eth", "spot_price_usdt",
                    row.get("price", ""), row.get("captured_at_utc", ""), spot, "USDT")
        for row in read_csv_member(z, "spot_24h_stats.csv"):
            if row.get("symbol") == symbol:
                ts = row.get("captured_at_utc", "")
                add("volume", "rolling_24h_base_volume", row.get("volume", ""), ts, stats,
                    symbol.removesuffix("USDT"), detail="rolling exchange 24h, not fixed UTC day")
                add("btc" if symbol == "BTCUSDT" else "eth", "rolling_24h_price_change_pct",
                    row.get("priceChangePercent", ""), ts, stats, "%")
        for row in read_csv_member(z, "futures_open_interest.csv"):
            if row.get("symbol") == symbol:
                # Missing exchange timestamp must not silently become capture time.
                add("oi", "open_interest_as_reported", row.get("open_interest", ""),
                    row.get("exchange_time_utc", ""), oi_url, "exchange reported units")
        for row in read_csv_member(z, "futures_funding_rate.csv"):
            if row.get("symbol") == symbol:
                add("funding", "last_published_funding_rate", row.get("funding_rate", ""),
                    row.get("funding_time_utc", ""), funding_url, "fraction",
                    detail="historical funding, NOT a forecast")
    return results, {"path": str(path), "sha256": digest, "symbol": symbol}


def _normalize_observation(item: dict, asof: datetime) -> dict:
    if not isinstance(item, dict):
        raise ValueError("觀測紀錄必須是物件")
    category = item.get("category")
    if category not in MAX_AGE_HOURS:
        raise ValueError(f"未知資料分類：{category}")
    if not isinstance(item.get("id"), str) or not item["id"].strip():
        raise ValueError("觀測紀錄需具唯一 id")
    out = dict(item)
    status = item.get("status", "sourced")
    if status not in ("sourced", "unavailable"):
        raise ValueError("status 僅限 sourced / unavailable")
    out["status"] = status
    if status == "unavailable":
        out["fresh"] = False
        out["reason"] = str(item.get("reason") or "未取得資料")
        return out
    for key in ("metric", "scope", "source_name", "source_url", "observed_at_utc", "unit"):
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise ValueError(f"{item['id']} 缺少 {key}")
    if not valid_source(item["source_url"]):
        raise ValueError(f"{item['id']} 來源 URL 必須為有效 HTTPS 網址")
    value = item.get("value")
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)) or str(value).strip() == "":
        raise ValueError(f"{item['id']} 不得以空值代表已取得數值")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError):
        d = None
    if d is not None and not d.is_finite():
        raise ValueError(f"{item['id']} 數值不得為 NaN/Inf")
    observed = parse_time(item["observed_at_utc"])
    age = (asof - observed).total_seconds() / 3600
    out["fresh"] = 0 <= age <= MAX_AGE_HOURS[category]
    out["age_hours"] = round(age, 5)
    out["observed_at_utc"] = iso(observed)
    out["reason"] = "" if out["fresh"] else "資料過期或晚於本次巡檢時點"
    return out


def inspect(data: dict, *, bundle_paths: tuple[Path, ...] = (), calendar: dict | None = None) -> dict:
    if data.get("schema_version") != 1:
        raise ValueError("schema_version 必須是 1")
    asof = parse_time(data.get("as_of_utc"))
    if not isinstance(data.get("observations", []), list):
        raise ValueError("observations 必須是陣列")
    inputs = list(data.get("observations", []))
    digests: list[dict] = []
    for path in bundle_paths:
        rows, digest = bundle_observations(path)
        inputs.extend(rows)
        digests.append(digest)
    observations: list[dict] = []
    by_id: dict[str, dict] = {}
    for item in inputs:
        row = _normalize_observation(item, asof)
        if row["id"] in by_id:
            raise ValueError(f"重複觀測 ID：{row['id']}")
        by_id[row["id"]] = row
        observations.append(row)
    coverage = {c: any(r["category"] == c and r["fresh"] for r in observations) for c in REQUIRED}
    findings: list[dict] = []
    risks = data.get("risks", [])
    if not isinstance(risks, list):
        raise ValueError("risks 必須是陣列")
    for risk in risks:
        if not isinstance(risk, dict) or risk.get("severity") not in ("critical", "watch"):
            raise ValueError("risks severity 僅限 critical/watch")
        refs = risk.get("evidence_ids")
        if not isinstance(refs, list) or not refs or not all(isinstance(x, str) and x in by_id for x in refs):
            raise ValueError("風險敘述必須連到存在的 observation IDs")
        usable = all(by_id[x]["fresh"] for x in refs)
        findings.append({"severity": risk["severity"], "summary": str(risk.get("summary", "")),
                         "evidence_ids": refs, "active": usable, "reason": "" if usable else "引用資料缺漏或過期"})
    calendar_result = None
    if calendar is not None:
        calendar_result = event_risk.evaluate_event_risk(calendar, asof, symbol="BTCUSDT")
        # Honor existing P2 blackout; stale/incomplete calendars cannot certify a risk label.
        status = calendar_result.get("status")
        valid = not calendar_result.get("calendar_stale") and not calendar_result.get("incomplete_sources")
        if valid and status in ("active_blackout", "upcoming_high_risk"):
            findings.append({"severity": "critical" if status == "active_blackout" else "watch",
                             "summary": "官方行事曆重大事件禁入時段" if status == "active_blackout" else "24 小時內重大事件",
                             "active": True, "evidence_ids": [], "reason": "",
                             "calendar_events": calendar_result.get("active_events", [])})
    gates_input = data.get("candidate_gates", {})
    if not isinstance(gates_input, dict) or set(gates_input) - set(GATES):
        raise ValueError("candidate_gates 包含未知門檻")
    gate_results: dict[str, dict] = {}
    for name in GATES:
        value = gates_input.get(name)
        refs = value.get("evidence_ids", []) if isinstance(value, dict) else []
        passed = (isinstance(value, dict) and value.get("confirmed") is True
                  and isinstance(refs, list) and bool(refs)
                  and all(isinstance(ref, str) and ref in by_id and by_id[ref]["fresh"]
                          and by_id[ref]["category"] == GATE_CATEGORIES[name] for ref in refs))
        if passed and name in ("4h_structure", "2b", "ob", "fvg"):
            passed = all(by_id[ref].get("timeframe") == "4h" and by_id[ref].get("is_closed") is True for ref in refs)
        gate_results[name] = {"passed": bool(passed), "evidence_ids": refs}
    complete = all(coverage.values())
    critical = any(r["active"] and r["severity"] == "critical" for r in findings)
    watch = any(r["active"] and r["severity"] == "watch" for r in findings)
    if critical:
        level, reason = 1, "存在有來源支持的重大風險或有效行事曆禁入時段"
    elif not complete or watch:
        level, reason = 2, "關鍵資料缺漏/過期，或尚有待確認風險"
    elif all(g["passed"] for g in gate_results.values()):
        level, reason = 3, "必要研究門檻均具有新鮮來源；僅為候選研究，不是下單訊號"
    else:
        level, reason = 4, "必要資料覆蓋齊全，未發現活躍風險或已確認候選結構"
    return {"version": VERSION, "as_of_utc": iso(asof), "level": level, "level_reason": reason,
            "observations": observations, "coverage": coverage,
            "missing_categories": [k for k, v in coverage.items() if not v],
            "risks": findings, "candidate_gates": gate_results, "calendar": calendar_result,
            "bundle_digests": digests, "trade_signal": False,
            "assurance": "source-attributed inputs; external truths not independently reverified by this offline tool"}


def render_markdown(result: dict) -> str:
    by_category: dict[str, list[dict]] = {c: [] for c in REQUIRED}
    for obs in result["observations"]:
        by_category[obs["category"]].append(obs)

    def section_obs(categories: tuple[str, ...]) -> str:
        lines = []
        for cat in categories:
            rows = by_category[cat]
            if not rows:
                lines.append(f"- {cat}：未提供資料")
            for r in rows:
                if not r["fresh"]:
                    lines.append(f"- {cat} / {r.get('metric', 'unknown')}：{r['reason']}")
                else:
                    lines.append(f"- {cat} / {r['metric']} = {r['value']} {r['unit']}；"
                                 f"時間 {r['observed_at_utc']}；{r['source_name']}；"
                                 f"來源 {r['source_url']}；口徑 {r['scope']}")
        return "\n".join(lines) or "- 未提供資料"

    sections = [
        f"巡檢 as-of（UTC）：{result['as_of_utc']}；來源資料各有獨立時間戳，禁止當作同時行情。",
        f"**第 {result['level']} 級**。{result['level_reason']}。這不是交易建議。",
        section_obs(("btc", "volume")),
        section_obs(("eth",)),
        section_obs(("oi", "funding", "long_short", "liquidation", "orderbook")),
        section_obs(("macro", "news")) + "\n- 行事曆：" + (str(result["calendar"]) if result["calendar"] else "未提供"),
        section_obs(("etf",)),
        section_obs(("technical", "heatmap")) + "\n- 候選確認門檻：" +
        ", ".join(f"{k}={'通過' if v['passed'] else '未確認'}" for k, v in result["candidate_gates"].items()),
        "- 已附來源：" + str(sum(r["fresh"] for r in result["observations"])) +
        " 筆觀測；僅為提供來源，尚非外部獨立核實。\n" +
        "- 推論：所有風險等級與候選條件是條件式判讀，不代表價格方向。\n" +
        "- 缺漏/過期：" + (", ".join(result["missing_categories"]) or "無核心分類缺漏") + "。",
        "- 追蹤來源尚未覆蓋的項目，更新後按相同 as-of 規則重新計算。\n"
        "- 特別確認 4H 已收盤結構、未來重大宏觀事件與衍生品資料口徑。",
        f"**第 {result['level']} 級**：{result['level_reason']}。不自動下單；不將宏觀利空直接視為放空機會。",
    ]
    return "# BTC/ETH 事件風險巡檢｜研究紀錄\n\n" + "\n\n".join(
        f"## {title}\n\n{body}" for title, body in zip(SECTIONS, sections)
    ) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BTC 事件風險巡檢：只讀、可重播、缺資料不補值")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, action="append", default=[])
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw = args.evidence.read_bytes()
        data = json.loads(raw)
        cal = event_risk.load_calendar(args.calendar) if args.calendar else None
        result = inspect(data, bundle_paths=tuple(args.bundle), calendar=cal)
        result["evidence_sha256"] = hashlib.sha256(raw).hexdigest()
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "inspection.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.out_dir / "inspection.md").write_text(render_markdown(result), encoding="utf-8")
    except (OSError, ValueError, KeyError, BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"level": result["level"], "missing": result["missing_categories"], "out_dir": str(args.out_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
