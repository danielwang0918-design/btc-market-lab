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
            t = 1_600_000_000_000 + len([x for x in self.calls if x[1] == path]) * 1000
            return [[t, "100", "110", "90", "105", "12", t + 999]]
        if path == "/api/v3/ticker/price":
            return {"symbol": "BTCUSDT", "price": "123"}
        if path == "/api/v3/ticker/24hr":
            return {"symbol": "BTCUSDT", "priceChange": "1"}
        if path == "/fapi/v1/openInterest":
            return {"openInterest": "10", "time": 1_700_000_000_000}
        if path == "/fapi/v1/fundingRate":
            return [{"fundingRate": "0.0001", "fundingTime": 1_700_000_000_000}]
        raise AssertionError(path)


class MarketDataTests(unittest.TestCase):
    def test_closed_flag_and_kline_integrity(self):
        raw = [[1000, "100", "110", "90", "105", "2", 1999], [2000, "105", "112", "100", "110", "3", 2999]]
        rows = market.validate_klines(raw, "BTCUSDT", "1h", 2500)
        self.assertEqual([r["is_closed"] for r in rows], ["true", "false"])
        self.assertEqual(rows[0]["source"], market.SOURCE_SPOT)

    def test_rejects_bad_price_and_duplicate_time(self):
        with self.assertRaises(market.BinanceAPIError):
            market.validate_klines([[1, "100", "90", "95", "101", "1", 2]], "BTCUSDT", "1h", 10)
        with self.assertRaises(market.BinanceAPIError):
            market.validate_klines([[1, "100", "110", "90", "105", "1", 2], [1, "100", "110", "90", "105", "1", 2]], "BTCUSDT", "1h", 10)

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
                self.assertEqual(set(zf.namelist()), {"klines.csv", "spot_price.csv", "spot_24h_stats.csv", "futures_open_interest.csv", "futures_funding_rate.csv", "metadata.json"})
                rows = list(csv.DictReader(zf.open("klines.csv").read().decode("utf-8-sig").splitlines()))
                self.assertEqual(len(rows), 3)
                self.assertTrue(all(row["source"] == market.SOURCE_SPOT for row in rows))
                metadata = json.loads(zf.read("metadata.json"))
                self.assertEqual(metadata["optional_endpoint_errors"], {})
                self.assertEqual(metadata["optional_endpoint_status"], {"futures_open_interest": "available", "futures_funding_rate": "available"})

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
