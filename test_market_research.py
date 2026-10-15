import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import market_research as research
from market_structure import INTERVAL_MILLISECONDS, inspect_candle_integrity

NOW = datetime(2025, 1, 10, 12, 0, tzinfo=timezone.utc)
SERVER_TIME = int(NOW.timestamp() * 1000)
SYMBOL = "BTCUSDT"
SOURCE = "Binance Spot REST API"


def config(**overrides):
    result = {
        "config_version": research.CONFIG_VERSION,
        "strategy_version": research.STRATEGY_VERSION,
        "rules": research._rules(),
        "minimum_net_rr": "1",
        "max_quote_age_seconds": 30,
        "fee_bps_per_side": "10",
        "slippage_bps_per_side": "10",
        "max_setup_age_hours": 1000,
    }
    result.update(overrides)
    return result


def candle(symbol, interval, open_time, *, open_, high, low, close):
    step = INTERVAL_MILLISECONDS[interval]
    return {
        "symbol": symbol,
        "interval": interval,
        "open_time": str(open_time),
        "open_time_utc": datetime.fromtimestamp(open_time / 1000, timezone.utc).isoformat(),
        "open": str(open_), "high": str(high), "low": str(low), "close": str(close), "volume": "1",
        "close_time": str(open_time + step - 1),
        "close_time_utc": datetime.fromtimestamp((open_time + step - 1) / 1000, timezone.utc).isoformat(),
        "is_closed": "true", "source": SOURCE,
    }


def evidence_for(row, role="test"):
    return {
        "role": role, "symbol": row["symbol"], "interval": row["interval"],
        "open_time": int(row["open_time"]), "open_time_utc": row["open_time_utc"],
        "close_time": int(row["close_time"]), "close_time_utc": row["close_time_utc"],
        "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"],
        "volume": row["volume"], "is_closed": "true", "source": SOURCE,
    }


def event(row, event_type, direction, *, event_id, low="", high="", reference="", formed_row=None, evidence_rows=None):
    formed_row = formed_row or row
    evidence_rows = evidence_rows or [row]
    return {
        "event_id": event_id,
        "symbol": row["symbol"], "interval": row["interval"], "event_type": event_type,
        "direction": direction, "formed_at_open_time": formed_row["open_time"],
        "formed_at_utc": formed_row["open_time_utc"], "confirmed_at_open_time": row["open_time"],
        "confirmed_at_open_time_utc": row["open_time_utc"], "confirmed_at_close_time": row["close_time"],
        "confirmed_at_utc": row["close_time_utc"], "price_low": low, "price_high": high,
        "reference_price": reference,
        "evidence_json": json.dumps([evidence_for(item) for item in evidence_rows], separators=(",", ":")),
        "detector_version": "fixture-v1", "parameters_json": "{}", "source": SOURCE,
    }


def make_bundle(path, *, direction="bullish", quote="110", quote_age=5, gap_interval=None, omit_target=False, retest_setup=False, target_hit=False):
    candles = []
    candles_by_interval = {}
    event_rows = []
    alignment_direction = direction
    for interval in research.INTERVALS:
        step = INTERVAL_MILLISECONDS[interval]
        last_open = ((SERVER_TIME - step - 1) // step) * step
        if interval == "1h":
            opens = [last_open - 5 * step + i * step for i in range(6)]
            if direction == "bullish":
                values = [(100, 106, 94, 101), (98, 100, 95, 96), (96, 110, 96, 104),
                          (104, 112, 101, 110), (110, 120, 105, 115), (115, 118, 108, 110)]
            else:
                values = [(105, 110, 99, 103), (102, 105, 100, 104), (104, 104, 95, 98),
                          (98, 99, 90, 95), (95, 98, 84, 88), (88, 92, 85, 90)]
            if retest_setup:
                values[3] = (104, 112, 100, 110) if direction == "bullish" else (98, 100, 90, 95)
            rows = [candle(SYMBOL, interval, t, open_=v[0], high=v[1], low=v[2], close=v[3]) for t, v in zip(opens, values)]
            if target_hit:
                later = (110, 121, 109, 118) if direction == "bullish" else (90, 92, 80, 82)
                rows.append(candle(SYMBOL, interval, opens[-1] + step, open_=later[0], high=later[1], low=later[2], close=later[3]))
            candles_by_interval[interval] = rows
            if direction == "bullish":
                event_rows.extend([
                    event(rows[2], "structure_break", "bullish", event_id="1h-bos", reference="102"),
                    event(rows[2], "order_block", "bullish", event_id="1h-ob", low="95", high="100", formed_row=rows[1], evidence_rows=[rows[1], rows[2]]),
                ])
                if not omit_target:
                    event_rows.append(event(rows[5], "swing_high", "high", event_id="1h-target", reference="120", formed_row=rows[4], evidence_rows=[rows[4], rows[5]]))
            else:
                event_rows.extend([
                    event(rows[2], "structure_break", "bearish", event_id="1h-bos", reference="108"),
                    event(rows[2], "order_block", "bearish", event_id="1h-ob", low="100", high="105", formed_row=rows[1], evidence_rows=[rows[1], rows[2]]),
                ])
                if not omit_target:
                    event_rows.append(event(rows[5], "swing_low", "low", event_id="1h-target", reference="84", formed_row=rows[4], evidence_rows=[rows[4], rows[5]]))
        else:
            opens = [last_open - 2 * step + i * step for i in range(3)]
            rows = [candle(SYMBOL, interval, t, open_=100+i, high=105+i, low=95+i, close=102+i) for i, t in enumerate(opens)]
            candles_by_interval[interval] = rows
            event_rows.append(event(rows[-1], "structure_break", alignment_direction, event_id=f"{interval}-bos", reference="102"))
        candles.extend(rows)
    if gap_interval:
        candles = [row for row in candles if not (row["interval"] == gap_interval and row["open_time"] == candles_by_interval[gap_interval][1]["open_time"])]
    intervals = {i: inspect_candle_integrity([r for r in candles if r["interval"] == i], i, SERVER_TIME) for i in research.INTERVALS}
    has_gaps = any(report["gaps"] for report in intervals.values())
    metadata = {
        "symbol": SYMBOL, "binance_server_time": SERVER_TIME,
        "sources": {"spot": "https://api.binance.com"},
        "data_integrity": {"intervals": intervals, "status": "gaps_detected" if has_gaps else "complete"},
    }
    captured = (NOW - timedelta(seconds=quote_age)).isoformat()
    members = {
        "metadata.json": json.dumps(metadata).encode("utf-8"),
        "klines.csv": _csv_bytes(candles),
        "spot_price.csv": _csv_bytes([{"symbol": SYMBOL, "price": quote, "captured_at_utc": captured, "source": SOURCE}]),
        "market_structure.csv": _csv_bytes(event_rows),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def _csv_bytes(rows):
    if not rows:
        return "\ufeff".encode("utf-8")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


class MarketResearchTests(unittest.TestCase):
    def test_symbol_alias_and_setup_requires_explicit_personal_settings(self):
        self.assertEqual(research.normalize_symbol("btc"), "BTCUSDT")
        self.assertEqual(research.normalize_symbol("ethusdt"), "ETHUSDT")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            answers = iter(["yes", "2", "60", "5", "3", "48"])
            saved = research.configure(path, input_fn=lambda _: next(answers), output_fn=lambda _: None)
            self.assertEqual(saved["minimum_net_rr"], "2")
            self.assertEqual(saved["fee_bps_per_side"], "5")
            self.assertEqual(research.load_config(path), saved)
        with self.assertRaisesRegex(ValueError, "未建立設定"):
            research.configure(Path("unused.json"), input_fn=lambda _: "no", output_fn=lambda _: None)

    def test_long_candidate_waits_for_zone_and_calculates_cost_adjusted_rr(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = make_bundle(Path(temp) / "market.zip")
            report = research.build_analysis(archive, config(), now=NOW)
        self.assertEqual(report["status"], "候選做多")
        self.assertEqual(report["execution_state"], "等待觸發")
        self.assertEqual(report["scenario"]["entry_zone"], {"low": "95", "high": "100", "condition": "現價回到此 OB 區間"})
        self.assertEqual(report["scenario"]["stop_price"], "95")
        rr = report["checks"]["cost_adjusted_risk_reward"]
        self.assertEqual(rr["net_risk_per_unit"], "5.390")
        self.assertEqual(rr["net_reward_per_unit"], "19.560")
        self.assertEqual(rr["status"], "meets_user_threshold")
        self.assertEqual(len(report["evidence"]["structure_breaks"]["1d"]["evidence"]), 1)
        self.assertEqual(report["evidence"]["setup_order_block"]["event_id"], "1h-ob")

    def test_scheduled_critical_event_suspends_candidate_and_upcoming_event_requires_reassessment(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = make_bundle(Path(temp) / "market.zip")
            calendar_path = Path(temp) / "calendar.json"
            event = {
                "event_id": "fixture-cpi", "title": "CPI release", "category": "cpi",
                "event_time_utc": NOW.isoformat(), "source_name": "BLS", "source_url": "https://www.bls.gov/",
                "affected_scope": ["BTCUSDT"], "severity": "critical",
            }
            calendar_path.write_text(json.dumps({"last_sync_at_utc": NOW.isoformat(), "events": [event]}), encoding="utf-8")
            blocked = research.build_analysis(archive, config(), now=NOW, event_calendar_path=calendar_path)
            event["event_time_utc"] = (NOW + timedelta(hours=4)).isoformat()
            calendar_path.write_text(json.dumps({"last_sync_at_utc": NOW.isoformat(), "events": [event]}), encoding="utf-8")
            reassess = research.build_analysis(archive, config(), now=NOW, event_calendar_path=calendar_path)
        self.assertEqual(blocked["status"], "不適合開單")
        self.assertEqual(blocked["execution_state"], "事件風險暫停")
        self.assertEqual(blocked["checks"]["event_risk"]["candidate_action"], "no_trade")
        self.assertEqual(reassess["status"], "候選做多")
        self.assertEqual(reassess["execution_state"], "事件前重新評估")
        self.assertEqual(reassess["checks"]["event_risk"]["candidate_action"], "reassess")

    def test_zone_touch_marks_trigger_but_touching_stop_invalidates(self):
        with tempfile.TemporaryDirectory() as temp:
            triggered_archive = make_bundle(Path(temp) / "triggered.zip", quote="98")
            stopped_archive = make_bundle(Path(temp) / "stopped.zip", quote="94")
            triggered = research.build_analysis(triggered_archive, config(), now=NOW)
            stopped = research.build_analysis(stopped_archive, config(), now=NOW)
        self.assertEqual(triggered["status"], "候選做多")
        self.assertEqual(triggered["execution_state"], "目前已觸發")
        self.assertEqual(stopped["status"], "不適合開單")

    def test_short_candidate_is_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = make_bundle(Path(temp) / "short.zip", direction="bearish", quote="90")
            report = research.build_analysis(archive, config(), now=NOW)
        self.assertEqual(report["status"], "候選做空")
        self.assertEqual(report["execution_state"], "等待觸發")
        self.assertEqual(report["scenario"]["entry_zone"], {"low": "100", "high": "105", "condition": "現價回到此 OB 區間"})
        self.assertEqual(report["scenario"]["stop_price"], "105")

    def test_inconsistent_structure_missing_target_stale_quote_and_low_rr_do_not_force_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            mismatch_path = make_bundle(Path(temp) / "mismatch.zip")
            with zipfile.ZipFile(mismatch_path) as source:
                members = {name: source.read(name) for name in source.namelist()}
            rows = list(csv.DictReader(io.StringIO(members["market_structure.csv"].decode("utf-8-sig"))))
            for row in rows:
                if row["event_id"] == "4h-bos":
                    row["direction"] = "bearish"
            members["market_structure.csv"] = _csv_bytes(rows)
            with zipfile.ZipFile(mismatch_path, "w") as target:
                for name, data in members.items():
                    target.writestr(name, data)
            mismatch = research.build_analysis(mismatch_path, config(), now=NOW)
            missing_target = research.build_analysis(make_bundle(Path(temp) / "no-target.zip", omit_target=True), config(), now=NOW)
            stale = research.build_analysis(make_bundle(Path(temp) / "stale.zip", quote_age=31), config(), now=NOW)
            low_rr = research.build_analysis(make_bundle(Path(temp) / "low-rr.zip"), config(minimum_net_rr="4"), now=NOW)
            old_setup = research.build_analysis(make_bundle(Path(temp) / "old-setup.zip"), config(max_setup_age_hours=1), now=NOW)
        self.assertEqual(mismatch["status"], "不適合開單")
        self.assertEqual(mismatch["checks"]["multi_timeframe_structure"]["status"], "mixed")
        self.assertEqual(missing_target["status"], "不適合開單")
        self.assertEqual(stale["status"], "不適合開單")
        self.assertEqual(stale["checks"]["quote_freshness"]["status"], "stale")
        self.assertEqual(low_rr["status"], "不適合開單")
        self.assertEqual(low_rr["checks"]["cost_adjusted_risk_reward"]["status"], "below_user_threshold")
        self.assertEqual(old_setup["status"], "不適合開單")
        self.assertEqual(old_setup["checks"]["setup_freshness"]["status"], "stale")

    def test_gap_and_future_or_nonclosed_event_evidence_block_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            gap = research.build_analysis(make_bundle(Path(temp) / "gap.zip", gap_interval="4h"), config(), now=NOW)
            self.assertEqual(gap["status"], "不適合開單")
            self.assertFalse(gap["checks"]["data_integrity"]["4h"]["integrity_ok"])
            self.assertEqual(gap["checks"]["data_integrity"]["4h"]["missing_candle_count"], 1)

            unclosed_path = make_bundle(Path(temp) / "unclosed.zip")
            with zipfile.ZipFile(unclosed_path) as source:
                members = {name: source.read(name) for name in source.namelist()}
            candle_rows = list(csv.DictReader(io.StringIO(members["klines.csv"].decode("utf-8-sig"))))
            # Mark the actual 1h OB evidence candle as unclosed in the source CSV.
            event_rows = list(csv.DictReader(io.StringIO(members["market_structure.csv"].decode("utf-8-sig"))))
            ob = next(row for row in event_rows if row["event_id"] == "1h-ob")
            ob_open_time = str(json.loads(ob["evidence_json"])[0]["open_time"])
            for row in candle_rows:
                if row["interval"] == "1h" and row["open_time"] == ob_open_time:
                    row["is_closed"] = "false"
            members["klines.csv"] = _csv_bytes(candle_rows)
            with zipfile.ZipFile(unclosed_path, "w") as target:
                for name, data in members.items():
                    target.writestr(name, data)
            unclosed = research.build_analysis(unclosed_path, config(), now=NOW)
            self.assertEqual(unclosed["status"], "不適合開單")
            self.assertEqual(unclosed["checks"]["event_evidence"]["rejected_event_count"], 1)

            future_path = make_bundle(Path(temp) / "future.zip")
            with zipfile.ZipFile(future_path) as source:
                members = {name: source.read(name) for name in source.namelist()}
            event_rows = list(csv.DictReader(io.StringIO(members["market_structure.csv"].decode("utf-8-sig"))))
            next(row for row in event_rows if row["event_id"] == "1h-bos")["confirmed_at_close_time"] = str(SERVER_TIME)
            members["market_structure.csv"] = _csv_bytes(event_rows)
            with zipfile.ZipFile(future_path, "w") as target:
                for name, data in members.items():
                    target.writestr(name, data)
            future = research.build_analysis(future_path, config(), now=NOW)
            self.assertEqual(future["status"], "不適合開單")
            self.assertIn("future_or_missing_closed_evidence", {item["reason"] for item in future["checks"]["event_evidence"]["rejected_events"]})

    def test_retested_order_block_or_already_reached_target_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            retested = research.build_analysis(make_bundle(Path(temp) / "retested.zip", retest_setup=True), config(), now=NOW)
            target_hit = research.build_analysis(make_bundle(Path(temp) / "target-hit.zip", target_hit=True), config(), now=NOW)
        self.assertEqual(retested["status"], "不適合開單")
        self.assertIn("已被其後已收盤 K 線回測", retested["reasons"][0])
        self.assertEqual(target_hit["status"], "不適合開單")
        self.assertIn("沒有位於預期方向", target_hit["reasons"][0])

    def test_corrupt_or_incomplete_analysis_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            corrupt = Path(temp) / "corrupt.zip"
            corrupt.write_bytes(b"not a zip")
            incomplete = Path(temp) / "incomplete.zip"
            with zipfile.ZipFile(incomplete, "w") as archive:
                archive.writestr("metadata.json", "{}")
            with self.assertRaisesRegex(ValueError, "無法讀取資料包"):
                research.build_analysis(corrupt, config(), now=NOW)
            with self.assertRaisesRegex(ValueError, "缺少必要檔案"):
                research.build_analysis(incomplete, config(), now=NOW)

    def test_trade_review_links_plan_calculates_actual_r_and_appends_record(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = make_bundle(Path(temp) / "market.zip")
            report = research.build_analysis(archive, config(), now=NOW)
            records_dir = Path(temp) / "records"
            analysis_path = research.save_analysis(report, records_dir)
            loaded = json.loads(analysis_path.read_text(encoding="utf-8"))
            result = research.review_trade(
                loaded, actual_entry="101", actual_exit="111", quantity="2", actual_fees="1",
                opened_at="2025-01-10T13:00:00+00:00", closed_at="2025-01-10T14:00:00+00:00",
                notes="遵守計畫，按記錄平倉。", now=NOW,
            )
            path = research.append_trade_review(result, records_dir)
            stored = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(result["analysis_id"], loaded["analysis_id"])
        self.assertEqual(result["created_at_utc"], NOW.isoformat())
        self.assertEqual(result["result"]["gross_pnl_quote_asset"], "20")
        self.assertEqual(result["result"]["net_pnl_after_entered_fees_quote_asset"], "19")
        self.assertEqual(result["result"]["actual_r_multiple"], "1.58333333")
        self.assertEqual(result["comparison"]["planned_fee_estimate_quote_asset_excluding_slippage"], "0.44")
        self.assertEqual(stored["review_notes"], "遵守計畫，按記錄平倉。")
        self.assertEqual(stored["strategy_update"], "none; 此筆復盤只供日後檢視，不會自動修改規則。")

    def test_trade_review_leaves_r_blank_when_analysis_has_no_plan(self):
        analysis = {"analysis_id": "no-plan", "symbol": SYMBOL, "configuration": {}, "scenario": None}
        result = research.review_trade(
            analysis, actual_entry="100", actual_exit="101", quantity="1", actual_fees="0",
            opened_at="2025-01-10T13:00:00+00:00", closed_at="2025-01-10T14:00:00+00:00", notes="manual entry",
            actual_side="long", now=NOW,
        )
        self.assertIsNone(result["result"]["actual_r_multiple"])
        self.assertEqual(result["result"]["net_pnl_after_entered_fees_quote_asset"], "1")


if __name__ == "__main__":
    unittest.main()

