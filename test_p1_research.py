import random
import tempfile
import unittest
from pathlib import Path

import market_structure
import p1_research
from historical_backtest import INTERVAL_MILLISECONDS


def candle(index, *, open_, high, low, close):
    step = INTERVAL_MILLISECONDS["1h"]
    opened = index * step
    return {"symbol": "BTCUSDT", "interval": "1h", "open_time": opened,
            "open_time_utc": p1_research.p0._utc(opened), "open": str(open_), "high": str(high),
            "low": str(low), "close": str(close), "volume": "1",
            "close_time": opened + step - 1, "close_time_utc": p1_research.p0._utc(opened + step - 1),
            "is_closed": "true", "source": p1_research.p0.SOURCE}


class P1ResearchTests(unittest.TestCase):
    def test_streaming_p1_events_match_reference_events_on_varied_candle_series(self):
        rng = random.Random(20261015)
        rows = []
        price = 100.0
        for index in range(400):
            open_price = price
            close_price = max(1.0, open_price + rng.uniform(-3.0, 3.0))
            high = max(open_price, close_price) + rng.uniform(0.0, 2.0)
            low = max(0.01, min(open_price, close_price) - rng.uniform(0.0, 2.0))
            rows.append(candle(index, open_=open_price, high=high, low=low, close=close_price))
            price = close_price
        ours = p1_research.p1_events(rows)
        expected = market_structure.analyze_candles(rows)
        event_types = {"structure_break", "order_block", "fair_value_gap", "two_b_fakeout"}
        def signature(items):
            return sorted((item["event_type"], item["direction"], int(item["confirmed_at_close_time"]))
                          for item in items if item["event_type"] in event_types)
        self.assertEqual(signature(ours), signature(expected))

    def test_p1_events_match_reference_fvg_and_confirmed_two_b_events(self):
        top_rows = [
            candle(0, open_=9, high=10, low=8, close=9),
            candle(1, open_=10, high=12, low=9, close=11),
            candle(2, open_=11, high=11, low=8, close=10),
            candle(3, open_=10, high=13, low=9, close=11),
        ]
        ours = p1_research.p1_events(top_rows, left_bars=1, right_bars=1)
        expected = market_structure.analyze_candles(top_rows, left_bars=1, right_bars=1)
        summary = sorted((event["event_type"], event["direction"], int(event["confirmed_at_close_time"]))
                         for event in ours if event["event_type"] in {"fair_value_gap", "two_b_fakeout"})
        reference = sorted((event["event_type"], event["direction"], int(event["confirmed_at_close_time"]))
                           for event in expected if event["event_type"] in {"fair_value_gap", "two_b_fakeout"})
        self.assertEqual(summary, reference)
        self.assertTrue(any(event["event_type"] == "two_b_fakeout" and event["confirmed_at_close_time"] == top_rows[3]["close_time"] for event in ours))

    def test_fvg_overlap_requires_positive_area_and_opposing_overlap_blocks(self):
        step = INTERVAL_MILLISECONDS["1h"]
        signal_close = 5 * step - 1
        base = [
            {"event_type": "structure_break", "direction": "bullish", "confirmed_at_close_time": signal_close},
            {"event_type": "order_block", "direction": "bullish", "confirmed_at_close_time": signal_close,
             "formed_at_open_time": 2 * step, "price_low": "100", "price_high": "110"},
            {"event_type": "fair_value_gap", "direction": "bullish", "confirmed_at_close_time": 4 * step - 1,
             "price_low": "105", "price_high": "111"},
        ]
        selected, counts = p1_research.filter_setups([], base, "ob_fvg_overlap")
        self.assertEqual(counts["accepted"], 1)
        self.assertEqual(len(selected), 2)
        touching = [*base[:2], {**base[2], "price_low": "110", "price_high": "115"}]
        _, counts = p1_research.filter_setups([], touching, "ob_fvg_overlap")
        self.assertEqual(counts["no_fvg_confluence"], 1)
        conflict = [*base, {**base[2], "direction": "bearish", "price_low": "104", "price_high": "106"}]
        _, counts = p1_research.filter_setups([], conflict, "ob_fvg_overlap")
        self.assertEqual(counts["fvg_conflict"], 1)

    def test_two_b_requires_closed_return_inside_within_previous_two_bars(self):
        step = INTERVAL_MILLISECONDS["1h"]
        signal_close = 10 * step - 1
        base = [
            {"event_type": "structure_break", "direction": "bearish", "confirmed_at_close_time": signal_close},
            {"event_type": "order_block", "direction": "bearish", "confirmed_at_close_time": signal_close,
             "formed_at_open_time": 5 * step, "price_low": "100", "price_high": "110"},
            {"event_type": "two_b_fakeout", "direction": "bearish", "confirmed_at_close_time": 8 * step - 1},
        ]
        selected, counts = p1_research.filter_setups([], base, "ob_2b_confirmed")
        self.assertEqual(counts["accepted"], 1)
        future = [*base[:2], {**base[2], "confirmed_at_close_time": signal_close + 1}]
        _, counts = p1_research.filter_setups([], future, "ob_2b_confirmed")
        self.assertEqual(counts["no_2b_confirmation"], 1)
        conflict = [*base, {**base[2], "direction": "bullish"}]
        _, counts = p1_research.filter_setups([], conflict, "ob_2b_confirmed")
        self.assertEqual(counts["two_b_conflict"], 1)

    def test_study_reports_all_preregistered_variants_and_refuses_bad_split(self):
        rows = [candle(i, open_=100 + i, high=102 + i, low=99 + i, close=101 + i) for i in range(20)]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "BTCUSDT_1h.csv"
            p1_research.p0.save_csv(path, rows)
            result = p1_research.run_study(path, "BTCUSDT")
            with self.assertRaises(ValueError):
                p1_research.run_study(path, "BTCUSDT", train_fraction=.8, validation_fraction=.3)
        self.assertEqual(tuple(result["variants"]), p1_research.VARIANTS)
        self.assertIn("test", result["delta_vs_p0"]["ob_fvg_overlap"])
        self.assertEqual(result["data_summary"]["closed_candles"], 20)


if __name__ == "__main__":
    unittest.main()
