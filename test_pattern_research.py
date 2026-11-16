"""Deterministic regression cases for the research-only causal pattern detector."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

import pattern_research as patterns


def candles(prices, *, interval="1h", closed_last=True, start_index=0):
    """Create smooth alternating 3-bar-apart turns with two confirmation bars."""
    direction = 1 if prices[1] > prices[0] else -1
    anchor_points = [(2 + 3 * i, float(p)) for i, p in enumerate(prices)]
    first, last = anchor_points[0], anchor_points[-1]
    anchors = [(0, first[1] + 14 * direction), *anchor_points, (last[0] + 2, last[1] + 14 * (1 if len(prices) % 2 else -1) * direction)]
    # For increasing first leg, first point is a LOW; prior value must be higher.
    anchors[0] = (0, first[1] + 14 * direction)
    # For last point, extrapolate opposite to final motion.
    last_motion = 1 if prices[-1] > prices[-2] else -1
    anchors[-1] = (last[0] + 2, last[1] - 14 * last_motion)
    step = patterns.INTERVAL_MS[interval]
    out = []
    for i in range(last[0] + 3):
        left, right = next((a, b) for a, b in zip(anchors, anchors[1:]) if a[0] <= i <= b[0])
        base = left[1] + (right[1] - left[1]) * (i - left[0]) / (right[0] - left[0])
        pivot_slot = (i - 2) % 3 == 0 and i >= 2 and i <= last[0]
        if pivot_slot:
            index = (i - 2) // 3
            is_low = (index % 2 == 0) if direction == 1 else (index % 2 != 0)
            high, low = (base + 1.0, base) if is_low else (base, base - 1.0)
        else:
            high, low = base + 0.25, base - 0.25
        close = (high + low) / 2
        open_ms = (start_index + i) * step
        out.append({
            "symbol": "BTCUSDT", "interval": interval,
            "open_time": str(open_ms), "close_time": str(open_ms + step - 1),
            "open": str(close), "high": str(high), "low": str(low), "close": str(close),
            "volume": "100", "is_closed": "true", "source": "test-synthetic",
        })
    if not closed_last:
        out[-1]["is_closed"] = "false"
    return out


class PatternResearchTests(unittest.TestCase):
    def test_four_bullish_harmonics_are_detected(self):
        fixtures = {
            "gartley": [100, 200, 138.2, 170, 121.4],
            "bat": [100, 200, 155, 185, 111.4],
            "butterfly": [100, 200, 121.4, 170, 73],
            "crab": [100, 200, 140, 190, 38.2],
        }
        for name, ps in fixtures.items():
            with self.subTest(name=name):
                rows = candles(ps)
                result = patterns.analyze_patterns(rows)
                matched = [e for e in result if e["pattern"] == name]
                self.assertEqual(len(matched), 1, result)
                item = matched[0]
                self.assertEqual(item["direction"], "bullish")
                self.assertEqual(item["family"], "harmonic")
                self.assertEqual(item["formed_at_open_time"], int(rows[14]["open_time"]))
                self.assertEqual(item["confirmed_at_close_time"], int(rows[16]["close_time"]))
                self.assertEqual(item["usable_from_open_time"], int(rows[16]["close_time"]) + 1)
                self.assertEqual(len(json.loads(item["evidence_json"])), 5)

    def test_bearish_harmonic_mirrors_bullish(self):
        inverse = [500 - v for v in [100, 200, 138.2, 170, 121.4]]
        result = patterns.analyze_patterns(candles(inverse))
        self.assertTrue(any(x["pattern"] == "gartley" and x["direction"] == "bearish" for x in result), result)

    def test_impulse_five_waves_candidate_and_invalid_rules(self):
        result = patterns.analyze_patterns(candles([100, 160, 130, 210, 172, 260]))
        impulse = [x for x in result if x["pattern"] == "impulse_5_candidate"]
        self.assertEqual(len(impulse), 1, result)
        self.assertEqual(impulse[0]["direction"], "bullish")
        for ps in (
            [100, 160, 130, 170, 165, 230],  # wave 3 shortest
            [100, 160, 130, 210, 150, 250],  # wave 4 overlaps wave 1
            [100, 160, 90, 210, 170, 260],   # wave 2 past origin
        ):
            with self.subTest(ps=ps):
                out = patterns.analyze_patterns(candles(ps))
                self.assertFalse(any(x["pattern"] == "impulse_5_candidate" for x in out), out)

    def test_not_confirmed_until_right_side_closes(self):
        rows = candles([100, 200, 138.2, 170, 121.4])
        for cutoff in (14, 15, 16):
            outcome = patterns.analyze_patterns(rows[:cutoff])
            self.assertFalse(any(x["pattern"] == "gartley" for x in outcome))
        self.assertTrue(any(x["pattern"] == "gartley" for x in patterns.analyze_patterns(rows)))
        self.assertFalse(any(x["pattern"] == "gartley" for x in patterns.analyze_patterns(candles([100, 200, 138.2, 170, 121.4], closed_last=False))))

    def test_prefix_invariance_without_lookahead(self):
        rows = candles([100, 200, 138.2, 170, 121.4, 190, 125, 200])
        full = patterns.analyze_patterns(rows)
        for n in range(1, len(rows) + 1):
            part = patterns.analyze_patterns(rows[:n])
            visible_full = [x for x in full if x["confirmed_at_close_time"] <= int(rows[n-1]["close_time"])]
            self.assertEqual([x["pattern_id"] for x in part], [x["pattern_id"] for x in visible_full], n)

    def test_gap_breaks_pattern(self):
        rows = candles([100, 200, 138.2, 170, 121.4])
        del rows[9]  # One missing candle inside XABCD sequence.
        self.assertFalse(patterns.analyze_patterns(rows))

    def test_only_1h_4h_1d_and_closed_candles(self):
        for interval in ("1h", "4h", "1d"):
            rows = candles([100, 200, 138.2, 170, 121.4], interval=interval)
            self.assertTrue(patterns.analyze_patterns(rows))
        rows = candles([100, 200, 138.2, 170, 121.4])
        rows[0]["interval"] = "15m"
        with self.assertRaises(ValueError):
            patterns.analyze_patterns(rows)
        rows = candles([100, 200, 138.2, 170, 121.4])
        rows[6]["is_closed"] = "false"
        with self.assertRaises(ValueError):
            patterns.analyze_patterns(rows)

    def test_ratio_and_integrity_fail_closed(self):
        rows = candles([100, 200, 138.2, 170, 95])
        self.assertFalse(any(x["family"] == "harmonic" for x in patterns.analyze_patterns(rows)))
        for value in (-0.01, 0.11, float("nan")):
            with self.assertRaises(ValueError):
                patterns.analyze_patterns(rows, ratio_tolerance=value)
        for field, value in (("high", "nan"), ("close_time", "123"), ("volume", "-1")):
            bad = [dict(r) for r in rows]
            bad[1][field] = value
            with self.assertRaises(ValueError):
                patterns.analyze_patterns(bad)

    def test_asof_filter_and_csv_export(self):
        rows = candles([100, 200, 138.2, 170, 121.4])
        results = patterns.analyze_patterns(rows)
        event = next(x for x in results if x["pattern"] == "gartley")
        self.assertEqual(patterns.patterns_available_at([event], event["usable_from_open_time"] - 1), [])
        self.assertEqual(patterns.patterns_available_at([event], event["usable_from_open_time"]), [event])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            raw, annotated = path / "bars.csv", path / "patterns.csv"
            with raw.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            self.assertEqual(patterns.main([str(raw), str(annotated)]), 0)
            with annotated.open("r", newline="", encoding="utf-8") as f:
                self.assertEqual(list(csv.DictReader(f))[0]["pattern"], "gartley")


if __name__ == "__main__":
    unittest.main()
