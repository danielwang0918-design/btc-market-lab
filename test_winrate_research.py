import copy
import unittest
from decimal import Decimal

import historical_backtest as hb
import winrate_research as wr
from market_structure import INTERVAL_MILLISECONDS


def candle(index, *, interval="1h", open_=100, high=103, low=98, close=101, volume=10):
    step = INTERVAL_MILLISECONDS[interval]
    opened = index * step
    return {
        "symbol": "BTCUSDT",
        "interval": interval,
        "open_time": str(opened),
        "open_time_utc": hb._utc(opened),
        "open": str(open_),
        "high": str(high),
        "low": str(low),
        "close": str(close),
        "volume": str(volume),
        "close_time": str(opened + step - 1),
        "close_time_utc": hb._utc(opened + step - 1),
        "exchange_close_time": str(opened + step - 1),
        "exchange_close_time_utc": hb._utc(opened + step - 1),
        "is_closed": "true",
        "source": hb.SOURCE,
    }


def candidate(index=0, *, side="long", target="120", rr="2", setup_id="ob-1"):
    step = INTERVAL_MILLISECONDS["1h"]
    return {
        "setup_id": setup_id,
        "target_event_id": "swing-1",
        "side": side,
        "signal_index": index,
        "signal_time_ms": (index + 1) * step,
        "setup_confirm_ms": 0,
        "setup_age_hours": 1.0,
        "zone_low": "99",
        "zone_high": "100",
        "entry_reference": "100",
        "stop": "89" if side == "long" else "111",
        "target": target,
        "net_rr": rr,
    }


class WinRateResearchTests(unittest.TestCase):
    def test_no_filter_simulator_matches_formal_simulator(self):
        rows = [
            candle(0, open_=105, high=110, low=104, close=108),
            candle(1, open_=107, high=109, low=99, close=101),
            candle(2, open_=101, high=121, low=95, close=115),
        ]
        snapshots = [
            {"candidate": candidate(0), "integrity_ok": True},
            {"candidate": candidate(1, target="120"), "integrity_ok": True},
            {"candidate": None, "integrity_ok": True},
        ]
        features = [{} for _ in rows]
        fee, slip = Decimal("10"), Decimal("5")
        formal = hb._simulate_slice(
            rows, copy.deepcopy(snapshots), 0, len(rows),
            minimum_net_rr=1.5, max_setup_age_hours=168,
            fee_bps=fee, slippage_bps=slip,
        )
        researched = wr.simulate_filtered_slice(
            rows, copy.deepcopy(snapshots), features, 0, len(rows),
            filter_name="none", minimum_net_rr=1.5, max_setup_age_hours=168,
            fee_bps=fee, slippage_bps=slip,
        )
        self.assertEqual(researched, formal)

    def test_fixed_r_target_moves_target_in_trade_direction_and_keeps_original_rr(self):
        original = candidate(side="long", target="130", rr="2.5")
        snaps = [{"candidate": original, "integrity_ok": True}]
        converted = wr.make_target_snapshots(
            snaps, "0.5R", fee_bps=Decimal("10"), slippage_bps=Decimal("5")
        )[0]["candidate"]
        self.assertEqual(converted["_original_net_rr"], "2.5")
        self.assertEqual(converted["_original_target"], "130")
        self.assertAlmostEqual(float(converted["target"]), 105.5)
        self.assertNotEqual(converted["net_rr"], "2.5")

    def test_fixed_r_target_moves_down_for_short(self):
        original = candidate(side="short", target="70", rr="2.5")
        converted = wr.make_target_snapshots(
            [{"candidate": original, "integrity_ok": True}], "1.0R",
            fee_bps=Decimal("10"), slippage_bps=Decimal("5")
        )[0]["candidate"]
        self.assertAlmostEqual(float(converted["target"]), 89.0)
        self.assertEqual(converted["_original_target"], "70")

    def test_feature_at_signal_time_does_not_change_when_only_future_hourly_bars_change(self):
        hourly = [
            candle(i, open_=100 + i * 0.1, high=102 + i * 0.1,
                   low=99 + i * 0.1, close=101 + i * 0.1, volume=10 + i)
            for i in range(60)
        ]
        four_hour = [
            candle(i, interval="4h", open_=100 + i, high=103 + i,
                   low=98 + i, close=101 + i, volume=100 + i)
            for i in range(20)
        ]
        daily = [
            candle(i, interval="1d", open_=100 + i, high=103 + i,
                   low=98 + i, close=101 + i, volume=1000 + i)
            for i in range(5)
        ]
        original = wr.build_feature_rows({"1h": hourly, "4h": four_hour, "1d": daily}, hourly)
        changed = copy.deepcopy(hourly)
        for i in range(35, len(changed)):
            changed[i]["close"] = str(float(changed[i]["close"]) * 3)
            changed[i]["high"] = str(float(changed[i]["high"]) * 3)
            changed[i]["low"] = str(float(changed[i]["low"]) * 3)
        altered = wr.build_feature_rows({"1h": changed, "4h": four_hour, "1d": daily}, changed)
        self.assertEqual(original[25], altered[25])

    def test_directional_rsi_filter_is_symmetric_for_long_and_short(self):
        feature = {"rsi14": 57}
        self.assertTrue(wr.entry_filter_passes("rsi55", candidate(side="long"), feature))
        self.assertFalse(wr.entry_filter_passes("rsi55", candidate(side="short"), feature))
        feature["rsi14"] = 43
        self.assertFalse(wr.entry_filter_passes("rsi55", candidate(side="long"), feature))
        self.assertTrue(wr.entry_filter_passes("rsi55", candidate(side="short"), feature))

    def test_candidate_selection_reads_validation_not_test(self):
        def row(name, win_rate, expectancy, pf, count, test_win):
            return {
                "target_mode": "swing", "filter": name,
                "train": {"trade_count": 30, "win_rate_percent": 30, "expectancy_r": 0.1, "profit_factor": 1.1},
                "validation": {
                    "trade_count": count, "win_rate_percent": win_rate,
                    "expectancy_r": expectancy, "profit_factor": pf,
                },
                "test": {"win_rate_percent": test_win},
            }
        candidates = [
            row("low-val-high-test", 50, 0.1, 1.1, 12, 99),
            row("high-val-low-test", 60, 0.2, 1.2, 12, 0),
        ]
        chosen, _ = wr._select_candidate(candidates, 8)
        self.assertEqual(chosen["filter"], "high-val-low-test")

    def test_profitable_gate_requires_minimum_validation_sample(self):
        candidate_row = {
            "target_mode": "swing", "filter": "rsi55",
            "train": {"trade_count": 40, "win_rate_percent": 70, "expectancy_r": 0.5, "profit_factor": 2},
            "validation": {
                "trade_count": 8, "win_rate_percent": 75,
                "expectancy_r": 0.4, "profit_factor": 1.8,
            },
        }
        selected, status = wr._select_candidate([candidate_row], 8)
        self.assertEqual(status, "no_validation_candidate_passed_profitability_gate_exploratory_only")
        self.assertEqual(selected["filter"], "rsi55")

    def test_filter_specs_are_unique_and_cover_the_no_filter_baseline(self):
        names = [name for name, _ in wr.FILTERS]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("none", names)
        self.assertIn("ema4h1d_rsi55_age72", names)
        self.assertIn("swing", wr.TARGETS)


if __name__ == "__main__":
    unittest.main()
