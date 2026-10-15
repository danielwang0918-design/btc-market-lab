#!/usr/bin/env python3
"""Calendar-backed event risk reminders. No sentiment or price direction inference."""
from __future__ import annotations

import argparse
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_CALENDAR = Path("data/events/calendar.json")
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
FED_FOMC = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_RSS = "https://www.federalreserve.gov/feeds/press_monetary.xml"
SEC_RSS = "https://www.sec.gov/news/pressreleases.rss"
UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")


class EventDataError(ValueError):
    pass


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("時間必須含時區，例如 2026-10-15T12:30:00Z。")
    return parsed.astimezone(UTC)


def validate_event(event: dict[str, Any]) -> dict[str, Any]:
    required = ("event_id", "title", "category", "event_time_utc", "source_name", "source_url", "affected_scope", "severity")
    if any(not str(event.get(key, "")).strip() for key in required):
        raise EventDataError("事件必須包含 ID、標題、分類、時間、來源、影響範圍與嚴重度。")
    if event["severity"] not in {"critical", "high", "medium", "info", "review_required"}:
        raise EventDataError("severity 僅接受 critical、high、medium、info、review_required。")
    event_time = _parse_datetime(str(event["event_time_utc"]))
    normalized = dict(event)
    normalized["event_time_utc"] = _utc_iso(event_time)
    normalized.setdefault("first_seen_at_utc", normalized["event_time_utc"])
    normalized.setdefault("scheduled", True)
    normalized.setdefault("status", "confirmed")
    normalized.setdefault("review_required", normalized["severity"] == "review_required")
    return normalized


def load_calendar(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": 1, "events": [], "last_sync_at_utc": None, "sources": [], "status": "missing"}
    except (OSError, json.JSONDecodeError) as exc:
        raise EventDataError(f"無法讀取事件行事曆：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise EventDataError("事件行事曆格式錯誤；需要包含 events 陣列的 JSON。")
    data["events"] = [validate_event(item) for item in data["events"] if isinstance(item, dict)]
    data.setdefault("last_sync_at_utc", None)
    data.setdefault("sources", [])
    data["status"] = "available"
    return data


def evaluate_event_risk(calendar: dict[str, Any], now: datetime, *, symbol: str = "BTCUSDT") -> dict[str, Any]:
    now = now.astimezone(UTC)
    if calendar.get("status") == "missing":
        return {"status": "unavailable", "level": "unknown", "candidate_action": "manual_review", "active_events": [],
                "message": "找不到事件行事曆；宏觀/政策事件風險未檢查，請先同步資料或人工確認。"}
    last_sync = calendar.get("last_sync_at_utc")
    stale = True
    if last_sync:
        try:
            stale = (now - _parse_datetime(last_sync)) > timedelta(hours=48)
        except (ValueError, TypeError):
            stale = True
    freshness_message = "行事曆超過 48 小時未同步；需人工確認官方時間是否有更新。" if stale else ""
    upcoming: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    for event in calendar.get("events", []):
        if event.get("status") in {"cancelled", "superseded"} or event.get("review_required"):
            continue
        if symbol not in event.get("affected_scope", []) and "global_markets" not in event.get("affected_scope", []):
            continue
        event_time = _parse_datetime(event["event_time_utc"])
        minutes = (event_time - now).total_seconds() / 60
        if event["severity"] in {"critical", "high"} and -30 <= minutes <= 60:
            active.append({**event, "minutes_until_event": round(minutes, 1), "window": "T-60m 至 T+30m"})
        elif 0 < minutes <= 24 * 60 and event["severity"] in {"critical", "high"}:
            upcoming.append({**event, "minutes_until_event": round(minutes, 1)})
    if active:
        return {"status": "active_blackout", "level": "critical", "candidate_action": "no_trade", "active_events": active,
                "message": "重大已排程事件位於風險窗口內；候選情境暫停，待事件發布及市場重新評估後再分析。" + (f" {freshness_message}" if stale else ""),
                "calendar_stale": stale}
    if upcoming:
        return {"status": "upcoming_high_risk", "level": "high", "candidate_action": "reassess", "active_events": upcoming,
                "message": "24 小時內有重大事件；降低對候選情境的信心並在事件前重新評估風險。" + (f" {freshness_message}" if stale else ""),
                "calendar_stale": stale}
    return {"status": "stale_calendar" if stale else "clear", "level": "unknown" if stale else "normal",
            "candidate_action": "manual_review" if stale else "none", "active_events": [],
            "message": "行事曆沒有找到 24 小時內的已確認高風險事件；這不代表沒有突發新聞風險。" + (f" {freshness_message}" if stale else ""),
            "calendar_stale": stale}


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "BTC-Market-Lab/1.0 (public event calendar research)"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise EventDataError(f"來源回應 HTTP {response.status}: {url}")
            return response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise EventDataError(f"無法讀取來源 {url}: {exc}") from exc


def _ics_unfold(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _ics_datetime(property_line: str) -> datetime:
    key, value = property_line.split(":", 1)
    tz_match = re.search(r"TZID=([^;:]+)", key)
    if re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").replace(hour=8, minute=30, tzinfo=EASTERN).astimezone(UTC)
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    local = datetime.strptime(value, "%Y%m%dT%H%M%S")
    return local.replace(tzinfo=ZoneInfo(tz_match.group(1)) if tz_match else EASTERN).astimezone(UTC)


def parse_bls_ics(raw: bytes) -> list[dict[str, Any]]:
    lines = _ics_unfold(raw.decode("utf-8-sig"))
    rows: list[dict[str, Any]] = []
    current: dict[str, str] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            title = html.unescape(current.get("SUMMARY", "")).replace("\\,", ",").replace("\\;", ";")
            start = current.get("DTSTART")
            if start:
                category = "cpi" if "consumer price index" in title.lower() else "employment" if "employment situation" in title.lower() else "macro_release"
                severity = "critical" if category in {"cpi", "employment"} else "medium"
                event_time = _ics_datetime(start)
                rows.append(validate_event({
                    "event_id": f"bls-{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')}-{event_time.strftime('%Y%m%dT%H%MZ')}",
                    "title": title, "category": category, "event_time_utc": _utc_iso(event_time),
                    "source_name": "U.S. Bureau of Labor Statistics", "source_url": BLS_ICS,
                    "affected_scope": ["BTCUSDT", "global_markets"], "severity": severity,
                    "scheduled": True, "status": "confirmed", "review_required": False,
                    "description": "BLS 官方發布行事曆；時間依其行事曆標示。",
                }))
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            base_key = key.split(";", 1)[0]
            if base_key in {"DTSTART", "SUMMARY", "UID", "URL"}:
                current[base_key] = line if base_key == "DTSTART" else value
    return rows


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def parse_fomc_calendar(raw: bytes) -> list[dict[str, Any]]:
    parser = _TextParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    text = re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts)))
    years = {int(value) for value in re.findall(r"(20\d{2}) FOMC Meetings", text)}
    rows: list[dict[str, Any]] = []
    month_names = "January|February|March|April|May|June|July|August|September|October|November|December"
    for year in years:
        start = text.find(f"{year} FOMC Meetings")
        end_candidates = [text.find(f"{year + 1} FOMC Meetings", start + 1), text.find("Future Year:", start + 1)]
        end = min((item for item in end_candidates if item >= 0), default=len(text))
        section = text[start:end]
        for match in re.finditer(rf"({month_names})\s+(\d{{1,2}})\s*[-–]\s*(\d{{1,2}})", section):
            month, _first_day, last_day = match.groups()
            local = datetime.strptime(f"{year} {month} {int(last_day):02d} 14:00", "%Y %B %d %H:%M").replace(tzinfo=EASTERN)
            event_time = local.astimezone(UTC)
            rows.append(validate_event({
                "event_id": f"fomc-{year}-{local.strftime('%m%d')}",
                "title": f"FOMC 利率決議（會議第二日；14:00 ET 預定發布）", "category": "fomc_rate_decision",
                "event_time_utc": _utc_iso(event_time), "source_name": "Federal Reserve Board",
                "source_url": FED_FOMC, "affected_scope": ["BTCUSDT", "global_markets"],
                "severity": "critical", "scheduled": True, "status": "confirmed", "review_required": False,
                "description": "官方行事曆列出會議日期；14:00 ET 發布時段按聯準會月曆中決議/記者會排程標準化。請以臨近日期的官方日曆再次確認。",
            }))
    return rows


def parse_rss_review_items(raw: bytes, *, source_name: str, source_url: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise EventDataError(f"RSS/XML 格式錯誤：{exc}") from exc
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    rows: list[dict[str, Any]] = []
    now = _utc_iso(datetime.now(UTC))
    for item in items:
        title = item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title")
        link_node = item.find("link")
        if link_node is None:
            link_node = item.find("{http://www.w3.org/2005/Atom}link")
        link = (link_node.get("href") if link_node is not None else None) or (link_node.text if link_node is not None else None) or source_url
        if not title:
            continue
        published_text = item.findtext("pubDate") or item.findtext("{http://www.w3.org/2005/Atom}published") or item.findtext("{http://www.w3.org/2005/Atom}updated")
        published_at = None
        if published_text:
            try:
                parsed_published = parsedate_to_datetime(published_text)
                if parsed_published.tzinfo is not None:
                    published_at = _utc_iso(parsed_published)
            except (TypeError, ValueError, OverflowError):
                published_at = None
        item_key = hashlib.sha256(f"{source_name}|{title}|{link}".encode("utf-8")).hexdigest()[:20]
        rows.append({"event_id": f"review-{item_key}", "title": title.strip(),
                     "category": "policy_news_review", "event_time_utc": published_at or now, "first_seen_at_utc": now,
                     "source_published_at_utc": published_at, "event_time_estimated": published_at is None,
                     "source_name": source_name, "source_url": link.strip() if isinstance(link, str) else source_url,
                     "feed_url": source_url, "affected_scope": ["unclassified"], "severity": "review_required",
                     "scheduled": False, "status": "needs_human_review", "review_required": True,
                     "description": "官方 RSS 標題僅供人工審查；未推斷 BTC 影響、方向或嚴重程度。"})
    return rows


def sync_calendar(path: Path) -> dict[str, Any]:
    bls = parse_bls_ics(_fetch(BLS_ICS))
    fed = parse_fomc_calendar(_fetch(FED_FOMC))
    feeds = [("Federal Reserve Board", FED_RSS), ("U.S. Securities and Exchange Commission", SEC_RSS)]
    review: list[dict[str, Any]] = []
    feed_errors: list[str] = []
    for source_name, url in feeds:
        try:
            review.extend(parse_rss_review_items(_fetch(url), source_name=source_name, source_url=url))
        except EventDataError as exc:
            feed_errors.append(str(exc))
    existing = load_calendar(path)
    # A sync replaces official scheduled events, while preserving manually classified events.
    manual = [event for event in existing.get("events", []) if event.get("origin") == "manual"]
    unique = {event["event_id"]: event for event in [*bls, *fed, *review, *manual]}
    data = {"schema_version": 1, "last_sync_at_utc": _utc_iso(datetime.now(UTC)), "events": sorted(unique.values(), key=lambda x: x["event_time_utc"]),
            "sources": [BLS_ICS, FED_FOMC, FED_RSS, SEC_RSS], "sync_errors": feed_errors, "status": "available"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def add_manual_event(path: Path, *, title: str, category: str, event_time: str, source_name: str, source_url: str,
                     affected_scope: list[str], severity: str, notes: str = "") -> dict[str, Any]:
    calendar = load_calendar(path)
    parsed = _parse_datetime(event_time)
    item_key = hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
    event = validate_event({"event_id": f"manual-{parsed.strftime('%Y%m%dT%H%M%SZ')}-{item_key}", "title": title,
                            "category": category, "event_time_utc": _utc_iso(parsed), "source_name": source_name,
                            "source_url": source_url, "affected_scope": affected_scope, "severity": severity,
                            "description": notes, "origin": "manual", "review_required": False,
                            "first_seen_at_utc": _utc_iso(datetime.now(UTC)), "scheduled": True, "status": "confirmed"})
    calendar["events"] = [item for item in calendar["events"] if item["event_id"] != event["event_id"]] + [event]
    calendar["events"].sort(key=lambda item: item["event_time_utc"])
    calendar["status"] = "available"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(calendar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步官方宏觀日曆與政策新聞人工審查清單；不預測新聞方向。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("sync", "show"):
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR)
    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR)
    add_parser.add_argument("--title", required=True)
    add_parser.add_argument("--category", required=True)
    add_parser.add_argument("--time", required=True, help="含時區 ISO 時間，例如 2026-10-15T12:30:00Z")
    add_parser.add_argument("--source-name", required=True)
    add_parser.add_argument("--source-url", required=True)
    add_parser.add_argument("--scope", nargs="+", default=["BTCUSDT", "global_markets"])
    add_parser.add_argument("--severity", required=True, choices=("critical", "high", "medium", "info"))
    add_parser.add_argument("--notes", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "sync":
            data = sync_calendar(args.calendar)
        elif args.command == "add":
            add_manual_event(args.calendar, title=args.title, category=args.category, event_time=args.time,
                             source_name=args.source_name, source_url=args.source_url,
                             affected_scope=args.scope, severity=args.severity, notes=args.notes)
            data = load_calendar(args.calendar)
        else:
            data = load_calendar(args.calendar)
    except (EventDataError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({"calendar": str(args.calendar), "event_count": len(data["events"]), "last_sync_at_utc": data.get("last_sync_at_utc"),
                      "sync_errors": data.get("sync_errors", []), "sample": data["events"][:5]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
