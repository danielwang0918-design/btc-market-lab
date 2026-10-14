import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import market_risk_review as review


NOW = datetime(2025, 1, 2, 12, 0, tzinfo=timezone.utc)
SERVER_TIME = int(NOW.timestamp() * 1000)
SYMBOL = "BTCUSDT"


def structure_event(interval, event_type, direction, confirmed_at, reference_price, event_id):
    return {
        "symbol": SYMBOL,
        "interval": interval,
        "event_type": event_type,
        "direction": direction,
        "confirmed_at_close_time": str(confirmed_at),
        "confirmed_at_utc": datetime.fromtimestamp(confirmed_at / 1000, timezone.utc).isoformat(),
        "reference_price": str(reference_price),
        "source": "Binance Spot REST API",
        "event_id": event_id,
    }


def make_bundle(path, *, quote_age_seconds=10, gaps=None, events=None, include_files=None):
    gaps = gaps or {}
    events = events or []
    integrity_intervals = {}
    for interval in review.INTERVALS:
        interval_gaps = gaps.get(interval, [])
        has_gaps = bool(interval_gaps)
        integrity_intervals[interval] = {
            "status": "gaps_detected" if has_gaps else "complete",
            "is_contiguous": not has_gaps,
            "missing_candle_count": sum(item["missing_candle_count"] for item in interval_gaps),
            "gaps": interval_gaps,
            "errors": [],
        }
    metadata = {
        "symbol": SYMBOL,
        "binance_server_time": SERVER_TIME,
        "data_integrity": {"intervals": integrity_intervals},
    }
    captured_at = (NOW - timedelta(seconds=quote_age_seconds)).isoformat()
    spot_buffer = io.StringIO(newline="")
    spot_writer = csv.DictWriter(spot_buffer, fieldnames=["symbol", "price", "captured_at_utc", "source"])
    spot_writer.writeheader()
    spot_writer.writerow({"symbol": SYMBOL, "price": "100", "captured_at_utc": captured_at, "source": "Binance Spot REST API"})
    structure_fields = [
        "symbol", "interval", "event_type", "direction", "confirmed_at_close_time",
        "confirmed_at_utc", "reference_price", "source", "event_id",
    ]
    structure_buffer = io.StringIO(newline="")
    structure_writer = csv.DictWriter(structure_buffer, fieldnames=structure_fields)
    structure_writer.writeheader()
    structure_writer.writerows(events)
    members = {
        "metadata.json": json.dumps(metadata).encode("utf-8"),
        "spot_price.csv": spot_buffer.getvalue().encode("utf-8-sig"),
        "market_structure.csv": structure_buffer.getvalue().encode("utf-8-sig"),
    }
    if include_files is not None:
        members = {name: content for name, content in members.items() if name in include_files}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def default_arguments(path, **overrides):
    result = {
        "archive_path": path,
        "side": "long",
        "entry": "100",
        "stop": "90",
        "target": "120",
        "fee_bps_per_side": "10",
        "slippage_bps_per_side": "5",
        "max_quote_age_seconds": 30,
        "invalidation_interval": "4h",
        "now": NOW,
    }
    result.update(overrides)
    return result


class MarketRiskReviewTests(unittest.TestCase):
    def make_review(self, path, **overrides):
        return review.build_review(**default_arguments(path, **overrides))

    def test_long_scenario_order_and_net_risk_reward_include_two_fills(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = make_bundle(Path(temp) / "market.zip")
            report = self.make_review(archive, minimum_net_rr="1.9")

        checks = report["checks"]
        self.assertEqual(checks["scenario_price_order"]["status"], "valid")
        calculation = checks["net_risk_reward"]
        self.assertEqual(calculation["gross_risk_per_unit"], "10")
        self.assertEqual(calculation["gross_reward_per_unit"], "20")
        self.assertEqual(calculation["entry_cost_per_unit"], "0.1500")
        self.assertEqual(calculation["stop_exit_cost_per_unit"], "0.1350")
        self.assertEqual(calculation["target_exit_cost_per_unit"], "0.1800")
        self.assertEqual(calculation["net_risk_per_unit"], "10.2850")
        self.assertEqual(calculation["net_reward_per_unit"], "19.6700")
        self.assertEqual(calculation["threshold_comparison"], "meets_user_threshold")

    def test_short_scenario_order_and_costs_are_calculated(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = make_bundle(Path(temp) / "market.zip")
            report = self.make_review(
                archive, side="short", entry="100", stop="110", target="80"
            )

        checks = report["checks"]
        self.assertEqual(checks["scenario_price_order"]["status"], "valid")
        calculation = checks["net_risk_reward"]
        self.assertEqual(calculation["stop_exit_cost_per_unit"], "0.1650")
        self.assertEqual(calculation["target_exit_cost_per_unit"], "0.1200")
        self.assertEqual(calculation["net_risk_per_unit"], "10.3150")
        self.assertEqual(calculation["net_reward_per_unit"], "19.7300")

    def test_invalid_long_and_short_price_orders_are_reported_without_rr(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = make_bundle(Path(temp) / "market.zip")
            long_report = self.make_review(archive, stop="101", target="110")
            short_report = self.make_review(archive, side="short", stop="99", target="90")

        self.assertEqual(long_report["checks"]["scenario_price_order"]["status"], "invalid")
        self.assertEqual(long_report["checks"]["net_risk_reward"]["status"], "not_calculated")
        self.assertEqual(short_report["checks"]["scenario_price_order"]["status"], "invalid")
        self.assertEqual(short_report["checks"]["net_risk_reward"]["status"], "not_calculated")

    def test_quote_freshness_uses_user_supplied_age_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            fresh = make_bundle(Path(temp) / "fresh.zip", quote_age_seconds=30)
            stale = make_bundle(Path(temp) / "stale.zip", quote_age_seconds=31)
            fresh_report = self.make_review(fresh, max_quote_age_seconds=30)
            stale_report = self.make_review(stale, max_quote_age_seconds=30)

        self.assertEqual(fresh_report["checks"]["quote_freshness"]["status"], "fresh")
        self.assertEqual(stale_report["checks"]["quote_freshness"]["status"], "stale")

    def test_kline_gap_is_reported_with_its_location_and_incomplete_gate(self):
        gap = {
            "previous_open_time": 1000,
            "first_missing_open_time": 2000,
            "last_missing_open_time": 2000,
            "next_open_time": 3000,
            "missing_candle_count": 1,
        }
        with tempfile.TemporaryDirectory() as temp:
            archive = make_bundle(Path(temp) / "gap.zip", gaps={"4h": [gap]})
            report = self.make_review(archive)

        integrity = report["checks"]["data_integrity"]
        self.assertEqual(integrity["status"], "incomplete_or_gapped")
        self.assertFalse(integrity["all_intervals_contiguous"])
        self.assertEqual(integrity["intervals"]["4h"]["missing_candle_count"], 1)
        self.assertEqual(integrity["intervals"]["4h"]["gaps"][0]["first_missing_open_time"], 2000)

    def test_multitimeframe_structure_alignment_uses_latest_confirmed_event(self):
        older_time = SERVER_TIME - 20_000
        latest_time = SERVER_TIME - 10_000
        events = [
            structure_event("1d", "structure_break", "bullish", latest_time, 101, "d-bull"),
            structure_event("4h", "structure_break", "bearish", older_time, 99, "4h-old-bear"),
            structure_event("4h", "structure_break", "bullish", latest_time, 102, "4h-new-bull"),
            structure_event("1h", "structure_break", "bullish", latest_time, 103, "1h-bull"),
            structure_event("1h", "structure_break", "bearish", SERVER_TIME, 104, "1h-unclosed"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            archive = make_bundle(Path(temp) / "aligned.zip", events=events)
            report = self.make_review(archive)

        context = report["market_structure_context"]
        self.assertEqual(context["status"], "aligned")
        self.assertEqual(context["intervals"]["4h"]["event_id"], "4h-new-bull")
        self.assertEqual(context["intervals"]["1h"]["event_id"], "1h-bull")
        self.assertEqual(context["comparison_to_user_scenario"]["1d"], "matches_user_scenario")

    def test_multitimeframe_mixed_and_insufficient_data_are_distinguished(self):
        time_ms = SERVER_TIME - 10_000
        mixed_events = [
            structure_event("1d", "structure_break", "bullish", time_ms, 101, "d"),
            structure_event("4h", "structure_break", "bearish", time_ms, 99, "4h"),
            structure_event("1h", "structure_break", "bullish", time_ms, 103, "1h"),
        ]
        missing_interval_events = mixed_events[:2]
        with tempfile.TemporaryDirectory() as temp:
            mixed = make_bundle(Path(temp) / "mixed.zip", events=mixed_events)
            missing = make_bundle(Path(temp) / "missing.zip", events=missing_interval_events)
            mixed_report = self.make_review(mixed)
            missing_report = self.make_review(missing)

        self.assertEqual(mixed_report["market_structure_context"]["status"], "mixed")
        self.assertEqual(missing_report["market_structure_context"]["status"], "insufficient_data")
        self.assertEqual(
            missing_report["market_structure_context"]["intervals"]["1h"]["status"],
            "no_confirmed_structure_break",
        )

    def test_stop_location_compares_against_selected_latest_swing(self):
        time_ms = SERVER_TIME - 10_000
        events = [
            structure_event("4h", "swing_low", "low", time_ms, 95, "long-swing-low"),
            structure_event("4h", "swing_high", "high", time_ms, 105, "short-swing-high"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            archive = make_bundle(Path(temp) / "swings.zip", events=events)
            long_beyond = self.make_review(archive, stop="94")
            long_inside = self.make_review(archive, stop="96")
            short_beyond = self.make_review(archive, side="short", stop="106", target="90")

        self.assertEqual(long_beyond["checks"]["stop_vs_latest_confirmed_swing"]["status"], "stop_at_or_beyond_reference")
        self.assertEqual(long_inside["checks"]["stop_vs_latest_confirmed_swing"]["status"], "stop_inside_or_before_reference")
        self.assertEqual(short_beyond["checks"]["stop_vs_latest_confirmed_swing"]["status"], "stop_at_or_beyond_reference")

    def test_corrupt_and_incomplete_archives_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            corrupt = Path(temp) / "corrupt.zip"
            corrupt.write_bytes(b"not a zip archive")
            incomplete = make_bundle(Path(temp) / "incomplete.zip", include_files={"metadata.json"})

            with self.assertRaisesRegex(ValueError, "無法讀取資料包"):
                self.make_review(corrupt)
            with self.assertRaisesRegex(ValueError, "缺少必要檔案"):
                self.make_review(incomplete)

    def test_nonfinite_and_negative_cost_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = make_bundle(Path(temp) / "market.zip")
            with self.assertRaisesRegex(ValueError, "有限數值"):
                self.make_review(archive, entry="NaN")
            with self.assertRaisesRegex(ValueError, "不可小於 0"):
                self.make_review(archive, fee_bps_per_side="-1")


if __name__ == "__main__":
    unittest.main()
