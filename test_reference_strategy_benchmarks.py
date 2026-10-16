import unittest
from decimal import Decimal

import reference_strategy_benchmarks as rb


def candles(count, *, gap_at=None):
    rows = []
    base = 1_700_000_000_000
    step = rb.INTERVAL_MILLISECONDS["1h"]
    for index in range(count):
        opened = base + index * step
        if gap_at is not None and index >= gap_at:
            opened += step
        close = 100.0
        rows.append({
            "symbol": "BTCUSDT",
            "interval": "1h",
            "open_time": opened,
            "open_time_utc": "test",
            "open": str(close),
            "high": str(close + 0.5),
            "low": str(close - 0.5),
            "close": str(close),
            "volume": "10",
            "close_time": opened + step - 1,
            "close_time_utc": "test",
            "is_closed": "true",
            "source": "test",
        })
    if gap_at is not None:
        for index, row in enumerate(rows):
            expected = base + index * step
            if index >= gap_at:
                row["open_time"] = expected + step
                row["close_time"] = row["open_time"] + step - 1
    return rows


class PublicStrategyBenchmarkTests(unittest.TestCase):
    def test_macd_event_only_uses_closed_bar_and_enters_next_open(self):
        rows = candles(6)
        events = [0, 1, 0, -1, 0, 0]
        trades = rb.run_macd_slice(
            rows, events, 0, len(rows), long_short=True,
            fee_bps=Decimal("10"), slip_bps=Decimal("5"),
        )
        self.assertGreaterEqual(len(trades), 2)
        self.assertEqual(trades[0]["entry_time_utc"], rb.hb._utc(rows[2]["open_time"]))
        self.assertEqual(trades[0]["signal_time_utc"], rb.hb._utc(rows[1]["close_time"] + 1))
        self.assertEqual(trades[0]["exit_reason"], "opposite_macd_cross")

    def test_supertrend_stop_wins_when_stop_and_target_touch_same_bar(self):
        rows = candles(4)
        rows[1]["high"] = "103"
        rows[1]["low"] = "97"
        events = [1, 0, 0, 0]
        atr = [1.0, 1.0, 1.0, 1.0]
        trades = rb.run_supertrend_slice(
            rows, events, atr, 0, len(rows), long_only=False,
            fee_bps=Decimal("10"), slip_bps=Decimal("5"),
        )
        self.assertGreaterEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "stop")
        self.assertAlmostEqual(float(trades[0]["raw_exit_price"] if "raw_exit_price" in trades[0] else trades[0]["exit_price"]), 98.5, places=5)

    def test_supertrend_position_closes_at_gap_instead_of_crossing_it(self):
        rows = candles(5)
        rows[1]["high"] = "100.5"
        rows[1]["low"] = "99.5"
        step = rb.INTERVAL_MILLISECONDS["1h"]
        rows[2]["open_time"] += step
        rows[2]["close_time"] += step
        events = [1, 0, 0, 0, 0]
        trades = rb.run_supertrend_slice(
            rows, events, [1.0] * len(rows), 0, len(rows), long_only=False,
            fee_bps=Decimal("10"), slip_bps=Decimal("5"),
        )
        self.assertTrue(trades)
        self.assertEqual(trades[0]["exit_reason"], "data_gap_exit")

    def test_macd_indicator_resets_at_gap(self):
        rows = candles(60)
        step = rb.INTERVAL_MILLISECONDS["1h"]
        for index in range(30, len(rows)):
            rows[index]["open_time"] += step
            rows[index]["close_time"] += step
        events = rb.build_macd_events(rows)
        self.assertEqual(len(events), len(rows))
        self.assertEqual(events[30], 0)

    def test_trade_metrics_returns_zeroes_for_empty_set(self):
        result = rb._trade_metrics([])
        self.assertEqual(result["trade_count"], 0)
        self.assertEqual(result["win_rate_percent"], 0.0)
        self.assertEqual(result["compounded_return_percent"], 0.0)


if __name__ == "__main__":
    unittest.main()
