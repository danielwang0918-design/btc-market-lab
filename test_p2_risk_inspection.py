"""Synthetic fixtures only; never assert market prices or trading wins."""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

import p2_risk_inspection as risk

ASOF = "2026-11-16T11:00:00Z"
STAMP = "2026-11-16T10:45:00Z"


def obs(category, ident=None, **overrides):
    item = {"id": ident or category, "category": category, "metric": "reviewed_measure",
            "value": "1", "unit": "source-defined", "scope": "test synthetic venue",
            "observed_at_utc": STAMP, "source_name": "fixture source",
            "source_url": "https://example.org/evidence", "status": "sourced"}
    item.update(overrides)
    return item


def complete():
    return {"schema_version": 1, "as_of_utc": ASOF,
            "observations": [obs(c) for c in risk.REQUIRED]}


class InspectionTests(unittest.TestCase):
    def test_unknown_is_wait_not_false_all_clear(self):
        out = risk.inspect({"schema_version": 1, "as_of_utc": ASOF, "observations": []})
        self.assertEqual(out["level"], 2)
        self.assertEqual(set(out["missing_categories"]), set(risk.REQUIRED))
        self.assertFalse(out["trade_signal"])

    def test_critical_and_watch_have_distinct_precedence(self):
        data = complete()
        data["risks"] = [{"severity": "critical", "summary": "synthetic shock", "evidence_ids": ["btc"]}]
        self.assertEqual(risk.inspect(data)["level"], 1)
        data["risks"][0]["severity"] = "watch"
        self.assertEqual(risk.inspect(data)["level"], 2)
        data["risks"][0]["severity"] = "critical"
        data["observations"][0]["observed_at_utc"] = "2026-10-15T11:00:00Z"
        self.assertEqual(risk.inspect(data)["level"], 2)
        self.assertFalse(risk.inspect(data)["risks"][0]["active"])

    def test_level_four_requires_complete_coverage(self):
        self.assertEqual(risk.inspect(complete())["level"], 4)
        data = complete()
        data["observations"].pop()
        self.assertEqual(risk.inspect(data)["level"], 2)

    def test_candidate_only_after_all_gates_confirmed(self):
        data = complete()
        technical = next(x for x in data["observations"] if x["category"] == "technical")
        technical.update({"timeframe": "4h", "is_closed": True})
        data["candidate_gates"] = {
            g: {"confirmed": True, "evidence_ids": [risk.GATE_CATEGORIES[g]]}
            for g in risk.GATES
        }
        self.assertEqual(risk.inspect(data)["level"], 3)
        # Overlapping OB/FVG alone never produces a research candidate.
        data["candidate_gates"]["heatmap"]["confirmed"] = False
        self.assertEqual(risk.inspect(data)["level"], 4)
        data["candidate_gates"]["heatmap"]["confirmed"] = True
        technical["is_closed"] = False
        self.assertEqual(risk.inspect(data)["level"], 4)
        technical["is_closed"] = True
        data["observations"] = [x for x in data["observations"] if x["category"] != "oi"]
        self.assertEqual(risk.inspect(data)["level"], 2)

    def test_future_and_missing_timestamp_not_fresh(self):
        data = complete()
        data["observations"][0]["observed_at_utc"] = "2026-11-16T11:10:00Z"
        self.assertFalse(risk.inspect(data)["coverage"][data["observations"][0]["category"]])
        data["observations"][0] = {
            "id": "btc", "category": "btc", "status": "unavailable", "reason": "API failure"
        }
        out = risk.inspect(data)
        self.assertEqual(out["level"], 2)
        self.assertEqual(out["observations"][0]["reason"], "API failure")

    def test_invalid_input_fails_closed(self):
        data = complete()
        data["observations"][0]["value"] = "NaN"
        with self.assertRaises(ValueError):
            risk.inspect(data)
        data = complete()
        data["observations"][0]["source_url"] = "http://example.org/unsafe"
        with self.assertRaises(ValueError):
            risk.inspect(data)
        data = complete()
        data["observations"].append(dict(data["observations"][0]))
        with self.assertRaises(ValueError):
            risk.inspect(data)
        data = complete()
        data["risks"] = [{"severity": "critical", "evidence_ids": ["missing"]}]
        with self.assertRaises(ValueError):
            risk.inspect(data)
        data = complete()
        data["candidate_gates"] = {"unknown_gate": {"confirmed": True}}
        with self.assertRaises(ValueError):
            risk.inspect(data)

    def test_collector_bundle_provenance_missing_stays_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "fixture.zip"
            with ZipFile(p, "w") as z:
                z.writestr("metadata.json", json.dumps({"symbol": "BTCUSDT"}))

                def write(name, rows):
                    buf = io.StringIO()
                    writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
                    z.writestr(name, buf.getvalue())

                write("spot_price.csv", [{"symbol": "BTCUSDT", "price": "60000.01", "captured_at_utc": STAMP}])
                write("spot_24h_stats.csv", [
                    {"symbol": "BTCUSDT", "volume": "500.3", "priceChangePercent": "-2.5",
                     "captured_at_utc": STAMP}
                ])
                write("futures_open_interest.csv", [
                    {"symbol": "BTCUSDT", "open_interest": "99", "exchange_time_utc": ""}
                ])
                write("futures_funding_rate.csv", [
                    {"symbol": "BTCUSDT", "funding_rate": "0.0001", "funding_time_utc": "2026-11-16T08:00:00Z"}
                ])
            result = risk.inspect(
                {"schema_version": 1, "as_of_utc": ASOF, "observations": []}, bundle_paths=(p,)
            )
            self.assertTrue(result["coverage"]["btc"])
            self.assertTrue(result["coverage"]["funding"])
            self.assertFalse(result["coverage"]["oi"])
            self.assertEqual(len(result["bundle_digests"][0]["sha256"]), 64)
            self.assertEqual(result["level"], 2)

    def test_eleven_section_report(self):
        report = risk.render_markdown(risk.inspect(complete()))
        self.assertTrue(all("## " + section in report for section in risk.SECTIONS))
        self.assertIn("https://example.org/evidence", report)
        self.assertIn("不自動下單", report)


if __name__ == "__main__":
    unittest.main()
