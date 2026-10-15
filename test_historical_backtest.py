import tempfile
import unittest
from pathlib import Path

import historical_backtest as backtest
from market_structure import INTERVAL_MILLISECONDS


class PagedClient:
    def __init__(self, start, bars):
        self.start = start
        self.bars = bars
        self.calls = []

    def get_json(self, base, path, params=None):
        if path == "/api/v3/time":
            return {"serverTime": self.start + len(self.bars) * INTERVAL_MILLISECONDS["1h"] + 1000}
        if path == "/api/v3/klines":
            self.calls.append(params)
            after = [row for row in self.bars if row[0] >= params["startTime"]]
            return after[:params["limit"]]
        raise AssertionError(path)


def raw_bar(open_time, *, o="100", h="102", low="99", c="101", volume="1"):
    step = INTERVAL_MILLISECONDS["1h"]
    return [open_time, o, h, low, c, volume, open_time + step - 1]


def candle(open_time, o, h, low, c):
    step = INTERVAL_MILLISECONDS["1h"]
    return {"symbol": "BTCUSDT", "interval": "1h", "open_time": open_time,
            "open_time_utc": backtest._utc(open_time), "open": str(o), "high": str(h),
            "low": str(low), "close": str(c), "volume": "1", "close_time": open_time + step - 1,
            "close_time_utc": backtest._utc(open_time + step - 1), "is_closed": "true", "source": backtest.SOURCE}


class HistoricalBacktestTests(unittest.TestCase):
    def test_download_paginates_with_maximum_and_returns_sorted_closed_rows(self):
        step = INTERVAL_MILLISECONDS["1h"]
        start = 1_700_000_000_000 // step * step
        raw = [raw_bar(start + i * step) for i in range(5)]
        client = PagedClient(start, raw)
        rows = backtest.fetch_historical_candles("btcusdt", "1h", start, start + 5 * step, client, page_limit=2)
        self.assertEqual([row["open_time"] for row in rows], [start + i * step for i in range(5)])
        self.assertEqual([call["limit"] for call in client.calls], [2, 2, 2])
        self.assertTrue(all(row["is_closed"] == "true" for row in rows))

    def test_download_rejects_invalid_ohlcv_and_nonadvancing_time(self):
        step = INTERVAL_MILLISECONDS["1h"]
        start = 1_700_000_000_000 // step * step
        bad = PagedClient(start, [raw_bar(start, low="103")])
        with self.assertRaisesRegex(backtest.BinanceAPIError, "OHLCV"):
            backtest.fetch_historical_candles("BTCUSDT", "1h", start, start + step, bad)

    def test_csv_download_is_persisted_and_incremental_update_deduplicates(self):
        step = INTERVAL_MILLISECONDS["1h"]
        start = 1_700_000_000_000 // step * step
        with tempfile.TemporaryDirectory() as temp:
            client = PagedClient(start, [raw_bar(start), raw_bar(start + step)])
            report = backtest.download_to_csv("BTCUSDT", "1h", Path(temp), start=backtest._utc(start),
                                               end=backtest._utc(start + 2 * step - 1), client=client)
            self.assertEqual(report["total_count"], 2)
            self.assertTrue(Path(report["path"]).exists())

    def test_same_candle_stop_and_target_uses_stop_first_and_costs_reduce_result(self):
        step = INTERVAL_MILLISECONDS["1h"]
        start = 1_700_000_000_000 // step * step
        rows = [candle(start + i * step, 100, 102, 99, 101) for i in range(5)]
        rows[2] = candle(start + 2 * step, 100, 112, 94, 108)
        confirm = start + step - 1
        events = [
            {"event_type": "structure_break", "direction": "bullish", "confirmed_at_close_time": confirm,
             "event_id": "break"},
            {"event_type": "order_block", "direction": "bullish", "confirmed_at_close_time": confirm,
             "price_low": "95", "price_high": "100"},
        ]
        trades = backtest._simulate_segment(rows, events, fee_bps=10, slippage_bps=5, target_r=2)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "stop")
        self.assertLess(trades[0]["r_multiple"], 0)

    def test_metrics_cover_expectancy_drawdown_and_loss_streak(self):
        trades = [
            {"net_pnl_per_unit": 2.0, "r_multiple": 1.0},
            {"net_pnl_per_unit": -1.0, "r_multiple": -1.0},
            {"net_pnl_per_unit": -2.0, "r_multiple": -2.0},
        ]
        stats = backtest._metrics(trades)
        self.assertEqual(stats["trades"], 3)
        self.assertAlmostEqual(stats["win_rate"], 1 / 3)
        self.assertAlmostEqual(stats["expectancy_r"], -2 / 3)
        self.assertAlmostEqual(stats["profit_factor"], 2 / 3)
        self.assertEqual(stats["max_consecutive_losses"], 2)
        self.assertAlmostEqual(stats["max_drawdown_r"], 3)


if __name__ == "__main__":
    unittest.main()
