import json
import unittest

import market_structure as structure


def candle(index, *, open_, high, low, close, closed=True):
    open_time = index * 3_600_000
    return {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "open_time": open_time,
        "open_time_utc": f"2025-01-01T{index:02d}:00:00+00:00",
        "open": str(open_),
        "high": str(high),
        "low": str(low),
        "close": str(close),
        "volume": "1.00000000",
        "close_time": open_time + 3_599_999,
        "close_time_utc": f"2025-01-01T{index:02d}:59:59.999000+00:00",
        "is_closed": str(closed).lower(),
        "source": "Binance Spot REST API",
    }


def patterns(events, name):
    return [event for event in events if event["event_type"] == name]


class MarketStructureTests(unittest.TestCase):
    def test_swing_waits_for_right_side_closed_candle(self):
        rows = [
            candle(0, open_=9, high=10, low=8, close=9),
            candle(1, open_=10, high=12, low=7, close=11),
            candle(2, open_=11, high=11, low=8, close=10),
        ]
        events = structure.analyze_candles(rows, left_bars=1, right_bars=1)
        swing = patterns(events, "swing_high")[0]
        self.assertEqual(swing["formed_at_open_time"], rows[1]["open_time"])
        self.assertEqual(swing["confirmed_at_open_time"], rows[2]["open_time"])
        evidence = json.loads(swing["evidence_json"])
        self.assertEqual([item["role"] for item in evidence], ["swing_point", "right_side_confirmation"])
        self.assertTrue(all(item["is_closed"] == "true" for item in evidence))
        self.assertEqual(swing["source"], "Binance Spot REST API")

    def test_unclosed_kline_is_ignored(self):
        rows = [
            candle(0, open_=9, high=10, low=8, close=9),
            candle(1, open_=10, high=12, low=9, close=11),
            candle(2, open_=11, high=11, low=9, close=10, closed=False),
        ]
        events = structure.analyze_candles(rows, left_bars=1, right_bars=1)
        self.assertEqual(patterns(events, "swing_high"), [])
        self.assertTrue(all(item["is_closed"] == "true" for event in events for item in json.loads(event["evidence_json"])))

    def test_three_candle_fvg_has_exact_zone_and_evidence(self):
        rows = [
            candle(0, open_=9, high="10.00000000000000001", low=8, close=9),
            candle(1, open_=10, high=12, low=9, close=11),
            candle(2, open_=11, high=12, low="10.50000000000000002", close=11.5),
        ]
        events = structure.analyze_candles(rows, left_bars=1, right_bars=1)
        fvg = patterns(events, "fair_value_gap")[0]
        self.assertEqual(fvg["direction"], "bullish")
        self.assertEqual(fvg["price_low"], "10.00000000000000001")
        self.assertEqual(fvg["price_high"], "10.50000000000000002")
        self.assertEqual([item["role"] for item in json.loads(fvg["evidence_json"])], ["first_candle", "middle_candle", "third_candle"])

    def test_structure_break_and_order_block_link_to_source_candles(self):
        rows = [
            candle(0, open_=9, high=10, low=8, close=9),
            candle(1, open_=10, high=12, low=9, close=11),
            candle(2, open_=11, high=11, low=8, close=10),
            candle(3, open_=12, high=14, low=11, close=13),
        ]
        events = structure.analyze_candles(rows, left_bars=1, right_bars=1)
        bos = patterns(events, "structure_break")
        ob = patterns(events, "order_block")
        self.assertEqual(len(bos), 1)
        self.assertEqual(bos[0]["direction"], "bullish")
        self.assertEqual(len(ob), 1)
        self.assertEqual(ob[0]["direction"], "bullish")
        self.assertEqual((ob[0]["price_low"], ob[0]["price_high"]), ("8", "11"))
        evidence = json.loads(ob[0]["evidence_json"])
        self.assertEqual([item["role"] for item in evidence], ["last_opposing_candle", "broken_swing", "swing_confirmation", "structure_break"])
        self.assertEqual(evidence[0]["open_time"], rows[2]["open_time"])

    def test_two_b_top_and_bottom_confirm_on_return_inside(self):
        top_rows = [
            candle(0, open_=9, high=10, low=8, close=9),
            candle(1, open_=10, high=12, low=9, close=11),
            candle(2, open_=11, high=11, low=8, close=10),
            candle(3, open_=10, high=13, low=9, close=11),
        ]
        bottom_rows = [
            candle(0, open_=9, high=10, low=8, close=9),
            candle(1, open_=9, high=10, low=7, close=8),
            candle(2, open_=8, high=11, low=8, close=9),
            candle(3, open_=8, high=10, low=6, close=8),
        ]
        top = patterns(structure.analyze_candles(top_rows, left_bars=1, right_bars=1), "two_b_fakeout")
        bottom = patterns(structure.analyze_candles(bottom_rows, left_bars=1, right_bars=1), "two_b_fakeout")
        self.assertEqual(top[0]["direction"], "bearish")
        self.assertEqual(top[0]["reference_price"], "12")
        self.assertEqual(bottom[0]["direction"], "bullish")
        self.assertEqual(bottom[0]["reference_price"], "7")

    def test_duplicate_times_and_bad_parameters_are_rejected(self):
        row = candle(0, open_=9, high=10, low=8, close=9)
        with self.assertRaises(ValueError):
            structure.analyze_candles([row, dict(row)])
        with self.assertRaises(ValueError):
            structure.analyze_candles([row], left_bars=0)

    def test_gap_splits_candles_so_fvg_cannot_bridge_missing_bar(self):
        rows = [
            candle(0, open_=9, high=10, low=8, close=9),
            candle(1, open_=9, high=10.5, low=8.5, close=10),
            candle(3, open_=11, high=12, low=11, close=11.5),
        ]
        report = structure.inspect_candle_integrity(rows, "1h")
        self.assertEqual(report["missing_candle_count"], 1)
        self.assertEqual(report["gaps"][0]["first_missing_open_time"], 2 * 3_600_000)
        events = structure.analyze_candles(rows, left_bars=1, right_bars=1)
        self.assertEqual(patterns(events, "fair_value_gap"), [])

    def test_out_of_order_candles_are_rejected_not_silently_sorted(self):
        rows = [
            candle(2, open_=9, high=10, low=8, close=9),
            candle(1, open_=9, high=10, low=8, close=9),
        ]
        with self.assertRaisesRegex(ValueError, "out_of_order_open_time"):
            structure.analyze_candles(rows)

    def test_appending_future_candles_does_not_rewrite_confirmed_events(self):
        rows = [
            candle(0, open_=9, high=10, low=8, close=9),
            candle(1, open_=10, high=12, low=9, close=11),
            candle(2, open_=11, high=11, low=8, close=10),
            candle(3, open_=9, high=10, low=7, close=8),
            candle(4, open_=8, high=9, low=6, close=7),
            candle(5, open_=8, high=14, low=8, close=13),
            candle(6, open_=13, high=13, low=10, close=12),
            candle(7, open_=12, high=13, low=9, close=10),
            candle(8, open_=10, high=15, low=9, close=14),
            candle(9, open_=14, high=14, low=11, close=12),
        ]
        prefix = rows[:6]
        prefix_events = structure.analyze_candles(prefix, left_bars=1, right_bars=1)
        full_events = structure.analyze_candles(rows, left_bars=1, right_bars=1)
        cutoff = prefix[-1]["close_time"]
        already_confirmed = [event for event in full_events if int(event["confirmed_at_close_time"]) <= cutoff]
        self.assertEqual(prefix_events, already_confirmed)


if __name__ == "__main__":
    unittest.main()
