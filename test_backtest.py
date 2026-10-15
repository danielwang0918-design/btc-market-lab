import unittest
from decimal import Decimal
from pathlib import Path
import tempfile
import csv
import io
import json
import zipfile
from datetime import datetime, timezone

import binance_market_data
import backtest
import market_research
from market_structure import INTERVAL_MILLISECONDS, EVENT_COLUMNS, inspect_candle_integrity


def bar(index, *, open_, high, low, close):
    return {
        "open_time": index * 3_600_000,
        "close_time": index * 3_600_000 + 3_599_999,
        "open": str(open_), "high": str(high), "low": str(low), "close": str(close),
    }


SIGNAL = {
    "generated_at_utc": "2025-01-01T00:59:59.999000+00:00",
    "scenario": {"entry_event_id": "ob-1", "target_event_id": "swing-1"},
}


def make_market_archive(path):
    symbol = "BTCUSDT"
    candles = []
    by_interval = {"1d": [], "4h": [], "1h": []}
    for interval, start, count in (("1d", 0, 1), ("4h", 0, 7), ("1h", 86_400_000, 3)):
        step = INTERVAL_MILLISECONDS[interval]
        for index in range(count):
            opened = start + index * step
            row = {
                "symbol": symbol, "interval": interval, "open_time": str(opened),
                "open_time_utc": datetime.fromtimestamp(opened / 1000, timezone.utc).isoformat(),
                "open": "100", "high": "101", "low": "99", "close": "100", "volume": "1",
                "close_time": str(opened + step - 1),
                "close_time_utc": datetime.fromtimestamp((opened + step - 1) / 1000, timezone.utc).isoformat(),
                "is_closed": "true", "source": binance_market_data.SOURCE_SPOT,
            }
            candles.append(row)
            by_interval[interval].append(row)
    server_time = int(candles[-1]["close_time"]) + 10_000
    metadata = {
        "symbol": symbol, "binance_server_time": server_time,
        "sources": {"spot": binance_market_data.SPOT_BASE},
        "data_integrity": {"intervals": {
            interval: inspect_candle_integrity(rows, interval, server_time)
            for interval, rows in by_interval.items()
        }},
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.json", json.dumps(metadata))
        members = (
            ("klines.csv", candles, binance_market_data.KLINE_COLUMNS),
            ("market_structure.csv", [], EVENT_COLUMNS),
            ("spot_price.csv", [{"symbol": symbol, "price": "100", "captured_at_utc": "2025-01-02T00:00:00+00:00", "source": binance_market_data.SOURCE_SPOT}], ["symbol", "price", "captured_at_utc", "source"]),
        )
        for name, rows, fields in members:
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(name, "\ufeff" + output.getvalue())
    return path


class BacktestFillTests(unittest.TestCase):
    def simulate(self, bars, *, entry_index=0, side="long", fee="0", slip="0"):
        return backtest._simulate_trade(
            side=side, signal=SIGNAL, zone_low=Decimal("99"), zone_high=Decimal("100"),
            stop=Decimal("95"), target=Decimal("110"), candles=bars,
            entry_index=entry_index, fee_bps=Decimal(fee), slippage_bps=Decimal(slip),
        )

    def test_order_cannot_fill_when_next_bar_misses_zone(self):
        self.assertIsNone(self.simulate([bar(0, open_=101, high=103, low=101, close=102)]))

    def test_stop_wins_when_stop_and_target_hit_in_same_candle(self):
        trade = self.simulate([bar(0, open_=100, high=111, low=94, close=105)])
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertEqual(trade["exit_price"], "95")

    def test_stop_gap_uses_worse_open_price(self):
        trade = self.simulate([bar(0, open_=93, high=100, low=92, close=95)])
        self.assertIsNone(trade)  # gap through invalidation means no entry
        trade = self.simulate([
            bar(0, open_=100, high=100, low=99, close=100),
            bar(1, open_=100, high=101, low=99, close=100),
            bar(2, open_=94, high=96, low=93, close=95),
        ], entry_index=1)
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertEqual(trade["exit_price"], "94")

    def test_fee_and_slippage_are_applied_to_both_fills(self):
        trade = self.simulate(
            [bar(0, open_=100, high=111, low=99, close=110)], fee="10", slip="20"
        )
        self.assertEqual(trade["entry_price"], "100.200")
        self.assertEqual(trade["exit_price"], "109.780")
        self.assertGreater(Decimal(trade["fees_paid_per_unit"]), Decimal("0"))

    def test_summary_calculates_drawdown_and_consecutive_losses(self):
        trades = [{"r_multiple": value} for value in ("1", "-1", "-0.5", "2", "-1")]
        summary = backtest.summarize(trades)
        self.assertEqual(summary["trade_count"], 5)
        self.assertEqual(summary["win_rate_percent"], 40.0)
        self.assertEqual(summary["average_win_r"], 1.5)
        self.assertEqual(summary["average_loss_r"], -0.83333333)
        self.assertEqual(summary["max_consecutive_losses"], 2)
        self.assertEqual(summary["max_drawdown_r"], 1.5)

    def test_empty_and_no_loss_summaries_are_well_defined(self):
        self.assertEqual(backtest.summarize([])["trade_count"], 0)
        self.assertEqual(backtest.summarize([{"r_multiple": "1"}])["profit_factor"], "infinite")

    def test_asof_package_excludes_future_candles_and_structure_events(self):
        candles = [
            {"symbol": "BTCUSDT", "interval": "1h", "open_time": "0", "close_time": "999", "open": "100", "high": "101", "low": "99", "close": "100", "volume": "1", "is_closed": "true", "source": "Binance Spot REST API"},
            {"symbol": "BTCUSDT", "interval": "1h", "open_time": "1000", "close_time": "1999", "open": "100", "high": "101", "low": "99", "close": "100", "volume": "1", "is_closed": "true", "source": "Binance Spot REST API"},
        ]
        events = [
            {"symbol": "BTCUSDT", "event_type": "swing_high", "confirmed_at_close_time": "999", "event_id": "past"},
            {"symbol": "BTCUSDT", "event_type": "swing_high", "confirmed_at_close_time": "1000", "event_id": "future"},
        ]
        metadata = {"symbol": "BTCUSDT", "sources": {"spot": "https://api.binance.com"}}
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "asof.zip"
            backtest._asof_archive(archive_path, metadata, candles, events, "BTCUSDT", 1000, "100")
            _, seen_candles, _, seen_events = market_research._load_archive(archive_path)

        self.assertEqual([row["open_time"] for row in seen_candles], ["0"])
        self.assertEqual([row["event_id"] for row in seen_events], ["past"])

    def test_full_backtest_preserves_the_source_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            source = make_market_archive(Path(directory) / "market.zip")
            original = source.read_bytes()
            backtest.run_backtest(
                source, fee_bps_per_side="10", slippage_bps_per_side="5",
                minimum_net_rr="1.5", max_setup_age_hours=168,
            )
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
