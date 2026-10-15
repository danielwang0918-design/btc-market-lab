import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import event_risk

NOW = datetime(2025, 1, 10, 12, 0, tzinfo=timezone.utc)


class EventRiskTests(unittest.TestCase):
    def test_bls_ics_recognizes_cpi_and_employment_schedule(self):
        sample = b"""BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:cpi-1\r\nDTSTART;TZID=America/New_York:20260114T083000\r\nSUMMARY:Consumer Price Index - December 2025\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:nfp-1\r\nDTSTART;TZID=America/New_York:20260109T083000\r\nSUMMARY:Employment Situation for December 2025\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""
        events = event_risk.parse_bls_ics(sample)
        self.assertEqual(len(events), 2)
        self.assertEqual({event["category"] for event in events}, {"cpi", "employment"})
        self.assertTrue(all(event["severity"] == "critical" for event in events))
        self.assertTrue(all(event["event_time_utc"].endswith("Z") for event in events))

    def test_fomc_parser_uses_meeting_second_day_and_eastern_release_time(self):
        sample = b"<h4>2026 FOMC Meetings</h4><p>January</p><p>27-28</p><p>March</p><p>17-18*</p><p>2027 FOMC Meetings</p>"
        events = event_risk.parse_fomc_calendar(sample)
        self.assertEqual(len(events), 2)
        self.assertTrue(events[0]["event_time_utc"].startswith("2026-01-28T19:00:00"))
        self.assertEqual(events[0]["category"], "fomc_rate_decision")

    def test_rss_headline_is_review_only_not_classified(self):
        sample = b"<rss><channel><item><title>Digital asset policy update</title><link>https://example.gov/item</link></item></channel></rss>"
        events = event_risk.parse_rss_review_items(sample, source_name="Agency", source_url="https://example.gov/rss")
        self.assertEqual(events[0]["severity"], "review_required")
        self.assertTrue(events[0]["review_required"])
        self.assertEqual(events[0]["affected_scope"], ["unclassified"])

    def test_active_critical_event_blocks_and_upcoming_event_requires_reassessment(self):
        event = {"event_id": "cpi", "title": "CPI", "category": "cpi", "event_time_utc": "2025-01-10T12:00:00Z",
                 "source_name": "BLS", "source_url": "https://www.bls.gov/", "affected_scope": ["BTCUSDT"], "severity": "critical"}
        calendar = {"status": "available", "last_sync_at_utc": "2025-01-10T11:59:00Z", "events": [event]}
        active = event_risk.evaluate_event_risk(calendar, NOW)
        self.assertEqual(active["candidate_action"], "no_trade")
        self.assertEqual(active["status"], "active_blackout")
        event["event_time_utc"] = "2025-01-10T20:00:00Z"
        upcoming = event_risk.evaluate_event_risk(calendar, NOW)
        self.assertEqual(upcoming["candidate_action"], "reassess")
        self.assertEqual(upcoming["status"], "upcoming_high_risk")

    def test_stale_calendar_requires_manual_review(self):
        result = event_risk.evaluate_event_risk({"status": "available", "events": [], "last_sync_at_utc": "2025-01-01T00:00:00Z"}, NOW)
        self.assertEqual(result["candidate_action"], "manual_review")
        self.assertTrue(result["calendar_stale"])

    def test_incomplete_rss_sync_requires_manual_review(self):
        calendar = {"status": "available", "events": [], "last_sync_at_utc": NOW.isoformat(), "sync_errors": ["SEC RSS unavailable"]}
        result = event_risk.evaluate_event_risk(calendar, NOW)
        self.assertEqual(result["candidate_action"], "manual_review")
        self.assertEqual(result["status"], "incomplete_calendar")
        self.assertTrue(result["incomplete_sources"])

    def test_manual_event_persists_in_calendar(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            event = event_risk.add_manual_event(path, title="重大政策消息", category="policy", event_time="2025-01-10T12:00:00Z",
                                                source_name="Official source", source_url="https://example.gov/",
                                                affected_scope=["BTCUSDT"], severity="high")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["events"][0]["event_id"], event["event_id"])
            self.assertEqual(data["events"][0]["origin"], "manual")


if __name__ == "__main__":
    unittest.main()
