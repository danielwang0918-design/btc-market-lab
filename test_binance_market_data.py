import csv
import json
import tempfile
import urllib.error
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import binance_market_data as market


class FakeClient:
    def __init__(self):
        self.calls = []

    def get_json(self, base, path, params=None):
        self.calls.append((base, path, params))
        if path == "/api/v3/time":
            return {"serverTime": 1_700_000_000_000}
        if path == "/api/v3/klines":
            interval = params["interval"]
            step = market.INTERVAL_MILLISECONDS[interval]
            t = (1_600_000_000_000 // step) * step
            return [[t, "100", "110", "90", "105", "12", t + step - 1]]
        if path == "/api/v3/ticker/price":
            return {"symbol": "BTCUSDT", "price": "123"}
        if path == "/api/v3/ticker/24hr":
            return {"symbol": "BTCUSDT", "priceChange": "1"}
        if path == "/fapi/v1/openInterest":
            return {"openInterest": "10", "time": 1_700_000_000_000}
        if path == "/fapi/v1/fundingRate":
            return [{"fundingRate": "0.0001", "fundingTime": 1_700_000_000_000}]
        raise AssertionError(path)


class GapClient(FakeClient):
    def get_json(self, base, path, params=None):
        if path == "/api/v3/klines" and params["interval"] == "1h":
            self.calls.append((base, path, params))
            step = market.INTERVAL_MILLISECONDS["1h"]
            first = (1_600_000_000_000 // step) * step
            bars = [
                [first, "9", "10", "8", "9", "1", first + step - 1],
                [first + step, "9", "10.5", "8.5", "10", "1", first + 2 * step - 1],
                [first + 3 * step, "11", "12", "11", "11.5", "1", first + 4 * step - 1],
            ]
            return bars
        return super().get_json(base, path, params)


class MarketDataTests(unittest.TestCase):
    def test_closed_flag_and_kline_integrity(self):
        step = market.INTERVAL_MILLISECONDS["1h"]
        base = (1_700_000_000_000 // step) * step
        raw = [[base, "100", "110", "90", "105", "2", base + step - 1], [base + step, "105", "112", "100", "110", "3", base + 2 * step - 1]]
        rows = market.validate_klines(raw, "BTCUSDT", "1h", base + step + 100)
        self.assertEqual([r["is_closed"] for r in rows], ["true", "false"])
        self.assertEqual(rows[0]["source"], market.SOURCE_SPOT)
        report = market.inspect_candle_integrity(rows, "1h", base + step + 100)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["closed_candle_count"], 1)

    def test_rejects_bad_price_and_duplicate_time(self):
        step = market.INTERVAL_MILLISECONDS["1h"]
        base = (1_700_000_000_000 // step) * step
        with self.assertRaises(market.BinanceAPIError):
            market.validate_klines([[base, "100", "90", "95", "101", "1", base + step - 1]], "BTCUSDT", "1h", base + step)
        with self.assertRaises(market.BinanceAPIError):
            market.validate_klines([[base, "100", "110", "90", "105", "1", base + step - 1], [base, "100", "110", "90", "105", "1", base + step - 1]], "BTCUSDT", "1h", base + step)

    def test_rejects_out_of_order_unaligned_bad_close_and_future_timestamps(self):
        step = market.INTERVAL_MILLISECONDS["1h"]
        base = (1_700_000_000_000 // step) * step
        first = [base, "100", "110", "90", "105", "1", base + step - 1]
        second = [base + step, "100", "110", "90", "105", "1", base + 2 * step - 1]
        with self.assertRaisesRegex(market.BinanceAPIError, "out_of_order_open_time"):
            market.validate_klines([second, first], "BTCUSDT", "1h", base + 3 * step)
        with self.assertRaisesRegex(market.BinanceAPIError, "unaligned_open_time"):
            market.validate_klines([[base + 1, "100", "110", "90", "105", "1", base + step]], "BTCUSDT", "1h", base + 3 * step)
        with self.assertRaisesRegex(market.BinanceAPIError, "invalid_close_time"):
            market.validate_klines([[base, "100", "110", "90", "105", "1", base + step - 2]], "BTCUSDT", "1h", base + 3 * step)
        with self.assertRaisesRegex(market.BinanceAPIError, "future_open_time"):
            market.validate_klines([first], "BTCUSDT", "1h", base - 1)

    def test_integrity_report_identifies_each_missing_open_time(self):
        step = market.INTERVAL_MILLISECONDS["1h"]
        base = (1_700_000_000_000 // step) * step
        raw = [
            [base, "100", "110", "90", "105", "1", base + step - 1],
            [base + 3 * step, "100", "110", "90", "105", "1", base + 4 * step - 1],
        ]
        rows = market.validate_klines(raw, "BTCUSDT", "1h", base + 5 * step)
        report = market.inspect_candle_integrity(rows, "1h", base + 5 * step)
        self.assertEqual(report["status"], "gaps_detected")
        self.assertEqual(report["analysis_suitability"], "partial_gap_segmented")
        self.assertEqual(report["missing_candle_count"], 2)
        gap = report["gaps"][0]
        self.assertEqual(gap["first_missing_open_time"], base + step)
        self.assertEqual(gap["last_missing_open_time"], base + 2 * step)
        self.assertEqual(gap["next_open_time"], base + 3 * step)

    def test_retries_rate_limit_then_succeeds(self):
        response = type("Response", (), {"read": lambda self: b'{"ok": true}', "__enter__": lambda self: self, "__exit__": lambda self, *args: None})()
        headers = {"Retry-After": "1"}
        rate_limit = urllib.error.HTTPError("https://example.test", 429, "limited", headers, None)
        with patch.object(market.urllib.request, "urlopen", side_effect=[rate_limit, response]) as urlopen, patch.object(market.time, "sleep") as sleep:
            payload = market.BinanceClient(retries=1).get_json("https://example.test", "/data")
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_symbol_validation(self):
        self.assertEqual(market.validate_symbol("btcusdt"), "BTCUSDT")
        with self.assertRaises(ValueError):
            market.validate_symbol("BTC/USDT")

    def test_writes_csv_and_zip_with_optional_data(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(market, "now_iso", return_value="2025-01-01T00:00:00+00:00"):
            archive = market.fetch_market_data("BTCUSDT", Path(temp), FakeClient(), limit=1)
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as zf:
                self.assertEqual(set(zf.namelist()), {"klines.csv", "spot_price.csv", "spot_24h_stats.csv", "futures_open_interest.csv", "futures_funding_rate.csv", "market_structure.csv", "metadata.json"})
                rows = list(csv.DictReader(zf.open("klines.csv").read().decode("utf-8-sig").splitlines()))
                self.assertEqual(len(rows), 3)
                self.assertTrue(all(row["source"] == market.SOURCE_SPOT for row in rows))
                metadata = json.loads(zf.read("metadata.json"))
                self.assertEqual(metadata["optional_endpoint_errors"], {})
                self.assertEqual(metadata["optional_endpoint_status"], {"futures_open_interest": "available", "futures_funding_rate": "available"})
                self.assertEqual(metadata["data_integrity"]["status"], "complete")
                self.assertTrue(metadata["data_integrity"]["is_contiguous"])
                self.assertEqual(metadata["data_integrity"]["intervals"]["1h"]["received_candle_count"], 1)
                self.assertEqual(metadata["market_structure"]["event_count"], 0)
                self.assertEqual(metadata["market_structure"]["methodology"]["default_parameters"], {"left_bars": 2, "right_bars": 2, "order_block_lookback_bars": 10, "two_b_return_bars": 2})

    def test_zip_records_gap_location_and_does_not_label_across_it(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = market.fetch_market_data("BTCUSDT", Path(temp), GapClient(), limit=3)
            with zipfile.ZipFile(archive) as zf:
                metadata = json.loads(zf.read("metadata.json"))
                report = metadata["data_integrity"]
                self.assertEqual(report["status"], "gaps_detected")
                self.assertFalse(report["is_contiguous"])
                self.assertEqual(report["analysis_suitability"], "partial_gap_segmented")
                gap = report["intervals"]["1h"]["gaps"][0]
                step = market.INTERVAL_MILLISECONDS["1h"]
                self.assertEqual(gap["first_missing_open_time"], gap["previous_open_time"] + step)
                self.assertEqual(gap["next_open_time"], gap["previous_open_time"] + 2 * step)
                events = list(csv.DictReader(zf.open("market_structure.csv").read().decode("utf-8-sig").splitlines()))
                self.assertFalse(any(row["interval"] == "1h" and row["event_type"] == "fair_value_gap" for row in events))

    def test_optional_futures_failure_is_recorded(self):
        client = FakeClient()
        original = client.get_json
        def failing(base, path, params=None):
            if path == "/fapi/v1/openInterest":
                raise market.BinanceAPIError("unsupported symbol")
            return original(base, path, params)
        client.get_json = failing
        with tempfile.TemporaryDirectory() as temp:
            archive = market.fetch_market_data("ABCUSDT", Path(temp), client, limit=1)
            with zipfile.ZipFile(archive) as zf:
                meta = json.loads(zf.read("metadata.json"))
                self.assertIn("unsupported symbol", meta["optional_endpoint_errors"]["futures_open_interest"])
                self.assertEqual(meta["optional_endpoint_status"]["futures_open_interest"], "error")


if __name__ == "__main__":
    unittest.main()
