import csv
import tempfile
import unittest
from pathlib import Path

import binance_market_data as base
import derivatives_research as derivatives


class DerivativesResearchTests(unittest.TestCase):
    def test_date_range_uses_utc_and_includes_end_date(self):
        self.assertEqual(derivatives.utc_iso(derivatives.parse_utc_date("2025-01-01")), "2025-01-01T00:00:00+00:00")
        self.assertEqual(derivatives.utc_iso(derivatives.parse_utc_date("2025-01-01", end=True)), "2025-01-01T23:59:59.999000+00:00")

    def test_normalize_open_interest_and_reject_invalid_long_short(self):
        row = derivatives.normalize_row("open_interest", {"timestamp": 123, "sumOpenInterest": "2", "sumOpenInterestValue": "200"}, "BTCUSDT", "1h")
        self.assertEqual(row["timestamp"], 123)
        with self.assertRaises(derivatives.DerivativesError):
            derivatives.normalize_row("global_long_short", {"timestamp": 123, "longShortRatio": "1.2", "longAccount": "0.9", "shortAccount": "0.5"}, "BTCUSDT", "1h")

    def test_history_pagination_advances_without_duplicate_timestamps(self):
        class Client:
            calls = []
            def get_json(self, base_url, path, params):
                self.calls.append((path, params.copy()))
                if len(self.calls) == 1:
                    return [{"timestamp": i, "sumOpenInterest": str(i + 1), "sumOpenInterestValue": str((i + 1) * 100)} for i in range(500)]
                return [{"timestamp": 499, "sumOpenInterest": "500", "sumOpenInterestValue": "50000"},
                        {"timestamp": 500, "sumOpenInterest": "501", "sumOpenInterestValue": "50100"}]
        client = Client()
        result = derivatives.fetch_history(client, "open_interest", "BTCUSDT", "1h", 0, 600)
        self.assertEqual(len(result), 501)
        self.assertEqual(client.calls[1][1]["startTime"], 500)

    def test_funding_extreme_threshold_uses_only_prior_observations(self):
        points = [(i * 1000, float(i)) for i in range(1, 31)]
        threshold = derivatives._past_percentile_threshold(points, 31_000, 100_000)
        self.assertEqual(threshold, 29.0)
        self.assertIsNone(derivatives._past_percentile_threshold(points[:10], 31_000, 100_000))

    def test_price_oi_merge_is_asof_and_keeps_price_and_oi_changes_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            step = base.INTERVAL_MILLISECONDS["1h"]
            candles = []
            for i, close in enumerate((100, 110, 99)):
                opened = i * step
                candles.append({"symbol": "BTCUSDT", "interval": "1h", "open_time": opened,
                                "open_time_utc": derivatives.utc_iso(opened), "open": str(close), "high": str(close + 1),
                                "low": str(close - 1), "close": str(close), "volume": "1",
                                "close_time": opened + step - 1, "close_time_utc": derivatives.utc_iso(opened + step - 1),
                                "quote_volume": "1", "trade_count": "1", "taker_buy_base_volume": "0.5",
                                "taker_buy_quote_volume": "0.5", "is_closed": "true", "source": "test"})
            base.write_csv(root / "BTCUSDT_futures_klines_1h.csv", candles, derivatives.CSV_FIELDS["futures_klines"])
            stats = []
            for i, oi in enumerate((100, 120, 90)):
                ts = (i + 1) * step - 1
                stats.append({"symbol": "BTCUSDT", "period": "1h", "timestamp": ts,
                              "timestamp_utc": derivatives.utc_iso(ts), "sumOpenInterest": str(oi),
                              "sumOpenInterestValue": str(oi * 100), "source": "test"})
            base.write_csv(root / "BTCUSDT_open_interest_1h.csv", stats, derivatives.CSV_FIELDS["open_interest"])
            derivatives.analyze("BTCUSDT", root)
            with (root / "BTCUSDT_derivatives_aligned_1h.csv").open(newline="", encoding="utf-8-sig") as handle:
                result = list(csv.DictReader(handle))
            self.assertEqual(result[1]["price_oi_state"], "price_up_oi_up")
            self.assertAlmostEqual(float(result[1]["oi_change_pct"]), 20.0)
            self.assertAlmostEqual(float(result[1]["price_change_pct"]), 10.0)
            self.assertEqual(result[1]["price_close_time"], str(2 * step - 1))

    def test_liquidation_event_and_heatmap_separate_sides(self):
        raw = {"e": "forceOrder", "E": 1000, "o": {"s": "BTCUSDT", "S": "SELL", "o": "LIMIT", "q": "0.2", "ap": "10050", "X": "FILLED", "z": "0.1", "T": 1000}}
        event = derivatives.normalize_liquidation(raw)
        self.assertEqual(event["liquidated_side"], "long")
        heat = derivatives.liquidation_heatmap([event, {**event, "average_price": "10075", "executed_quantity": "0.2", "event_time": 2000},
                                                {**event, "side": "BUY", "liquidated_side": "short", "event_time": 3000}], 100)
        self.assertEqual(len(heat), 2)
        self.assertEqual(heat[0]["event_count"], 2)
        self.assertEqual({row["liquidated_side"] for row in heat}, {"long", "short"})


if __name__ == "__main__":
    unittest.main()
