"""Tests for causal pattern joins and full-candle MFE overstatement audit."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

import pattern_trade_study as study
from test_pattern_research import candles
import pattern_research


class PatternTradeStudyTests(unittest.TestCase):
    def test_interior_mfe_excludes_both_boundary_bars(self):
        rows = candles([100, 200, 138.2, 170, 121.4])
        i = 5
        # long favorable high on entry candle and exit candle must NOT count.
        rows[i]["high"] = "900"
        rows[i + 2]["high"] = "900"
        rows[i + 1]["high"] = "110"
        trade = {"entry_time_utc": "1970-01-01T05:00:00+00:00",
                 "exit_time_utc": "1970-01-01T07:00:00+00:00",
                 "raw_entry_price": 100, "stop_price": 90, "direction": "long", "mfe_r": 80}
        m = study._mfe_interior(trade, rows, [int(x["open_time"]) for x in rows])
        self.assertEqual(m["mfe_r_interior_lower_bound"], 1.0)
        self.assertEqual(m["interior_candle_count"], 1)
        self.assertTrue(m["mfe_1r_boundary_ambiguous"] is False)
        trade["mfe_r"] = 80
        rows[i + 1]["high"] = "104"
        m = study._mfe_interior(trade, rows, [int(x["open_time"]) for x in rows])
        self.assertEqual(m["mfe_r_interior_lower_bound"], .4)
        self.assertTrue(m["mfe_1r_boundary_ambiguous"])

    def test_no_interior_on_same_bar_stop(self):
        rows = candles([100, 200, 138.2, 170, 121.4])
        trade = {"entry_time_utc": "1970-01-01T05:00:00+00:00",
                 "exit_time_utc": "1970-01-01T05:00:00+00:00",
                 "raw_entry_price": 100, "stop_price": 90, "direction": "short", "mfe_r": 2}
        self.assertEqual(study._mfe_interior(trade, rows, [int(x["open_time"]) for x in rows])["mfe_r_interior_lower_bound"], 0)

    def test_no_future_pattern_at_decision_time(self):
        events = pattern_research.analyze_patterns(candles([100, 200, 138.2, 170, 121.4]))
        candidate = next(e for e in events if e["pattern"] == "gartley")
        t = int(candidate["usable_from_open_time"])
        self.assertIsNone(study._latest_patterns([candidate], t - 1, 168*3600000)["1h_harmonic"])
        self.assertEqual(study._latest_patterns([candidate], t, 168*3600000)["1h_harmonic"]["pattern"], "gartley")
        self.assertFalse(study._latest_patterns([candidate], t + 169*3600000, 168*3600000)["1h_harmonic"]["recent"])

    def test_net_expectancy_pf_and_closed_trade_drawdown(self):
        r = [{"exit_time_utc": f"2026-01-0{i+1}T00:00:00+00:00",
              "setup_id": f"id{i}", "r_multiple": v}
             for i, v in enumerate([-1, 2, -1, -1])]
        perf = study._performance(r)
        self.assertEqual(perf["trades"], 4)
        self.assertEqual(perf["wins"], 1)
        self.assertEqual(perf["expectancy_r"], -.25)
        self.assertAlmostEqual(perf["profit_factor"], 2/3, places=5)
        self.assertEqual(perf["maximum_closed_trade_drawdown_r"], 2)

    def test_signal_time_is_timezone_aware(self):
        with self.assertRaises(ValueError):
            study._ms("2026-01-01T00:00:00")

    def test_end_to_end_source_fingerprints_and_stratification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); records = candles([100, 200, 138.2, 170, 121.4])
            for interval in ("1h", "4h", "1d"):
                iv_rows = candles([100, 200, 138.2, 170, 121.4], interval=interval)
                with (root / f"BTCUSDT_{interval}.csv").open("w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=iv_rows[0].keys());w.writeheader();w.writerows(iv_rows)
            trade = {"setup_id": "test", "direction": "long", "signal_time_utc": "1970-01-01T15:00:00+00:00",
                     "entry_time_utc": "1970-01-01T16:00:00+00:00", "exit_time_utc": "1970-01-01T16:00:00+00:00",
                     "raw_entry_price": "100", "stop_price": "90", "r_multiple": "-1", "mfe_r": "1",
                     "exit_reason": "stop", "bars_held": 1, "split": "test"}
            (root / "BTCUSDT_trade_diagnostics.json").write_text(json.dumps({
                "strategy": "mtf-bos-ob-retest-v1", "report_version": "mock",
                "variants": {"ob_1h_baseline": {"trades": [trade]}}}))
            output = root / "out"
            result = study.build_study(root, root / "BTCUSDT_trade_diagnostics.json", output)
            self.assertEqual(result["variants"]["ob_1h_baseline"]["test"]["performance"]["trades"], 1)
            self.assertEqual(result["variants"]["ob_1h_baseline"]["test"]["mfe_boundary_audit"]["boundary_ambiguous_losers_ge_1r"], 1)
            self.assertEqual(len(result["provenance_sha256"]), 4)
            with (output / "BTCUSDT_pattern_trade_features.csv").open() as f:
                row=next(csv.DictReader(f));self.assertEqual(row["split"], "test");self.assertEqual(row["mfe_r_interior_lower_bound"], "0.0")


if __name__ == "__main__":
    unittest.main()