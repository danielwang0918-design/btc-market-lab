import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import historical_backtest as hb
import market_structure


def candle(index, *, open_, high, low, close, interval="1h", symbol="BTCUSDT"):
    step = market_structure.INTERVAL_MILLISECONDS[interval]
    opened = index * step
    return {
        "symbol": symbol,
        "interval": interval,
        "open_time": str(opened),
        "open_time_utc": hb._utc(opened),
        "open": str(open_),
        "high": str(high),
        "low": str(low),
        "close": str(close),
        "volume": "1",
        "close_time": str(opened + step - 1),
        "close_time_utc": hb._utc(opened + step - 1),
        "exchange_close_time": str(opened + step - 1),
        "exchange_close_time_utc": hb._utc(opened + step - 1),
        "is_closed": "true",
        "source": hb.SOURCE,
    }


def signal(index=0, *, side="long", zone_low="90", zone_high="100", entry="100", stop="89",
           target="120", rr="2", setup_id="ob-1", confirm_ms=0):
    return {
        "setup_id": setup_id,
        "target_event_id": "swing-1",
        "side": side,
        "signal_index": index,
        "signal_time_ms": (index + 1) * 3_600_000,
        "setup_confirm_ms": confirm_ms,
        "setup_age_hours": 1.0,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "entry_reference": entry,
        "stop": stop,
        "target": target,
        "net_rr": rr,
    }


class PaginatedKlineClient:
    def __init__(self, server_time):
        self.server_time = server_time
        self.step = 3_600_000
        self.pages = 0

    def get_json(self, base, path, params=None):
        if path == "/api/v3/time":
            return {"serverTime": self.server_time}
        if path != "/api/v3/klines":
            raise AssertionError(f"Unexpected endpoint: {path}")
        self.pages += 1
        start = int(params["startTime"])
        end = int(params["endTime"])
        limit = int(params["limit"])
        result = []
        for opened in range(start, end + 1, self.step):
            if len(result) >= limit:
                break
            result.append([
                opened, "100", "102", "99", "101", "10", opened + self.step - 1,
            ])
        return result


class P0HistoryTests(unittest.TestCase):
    def test_strategy_events_match_canonical_closed_candle_labels(self):
        closes = [10, 12, 9, 11, 13, 10, 14, 8, 9, 15, 13, 16, 11, 17, 12,
                  18, 14, 16, 10, 19, 13, 15, 8, 14, 20, 12, 17, 11, 21, 16,
                  22, 14, 18, 9, 23, 13, 19, 10, 24, 15]
        rows = []
        for i, close in enumerate(closes):
            open_ = close - (0.3 if i % 2 == 0 else -0.3)
            rows.append(candle(i, open_=open_, high=max(open_, close) + 0.7,
                               low=min(open_, close) - 0.7, close=close))
        fast = hb._strategy_events(rows, "1h")
        canonical = market_structure.analyze_candles(rows)
        wanted = {"swing_high", "swing_low", "structure_break", "order_block"}
        canonical = [event for event in canonical if event["event_type"] in wanted]
        key = lambda event: (
            event["event_type"], event["direction"], int(event["formed_at_open_time"]),
            int(event["confirmed_at_close_time"]), str(event["price_low"]),
            str(event["price_high"]), str(event["reference_price"]),
        )
        self.assertEqual(sorted(map(key, fast)), sorted(map(key, canonical)))

    def test_strategy_event_evidence_is_confirmed_only(self):
        rows = [
            candle(i, open_=value - 0.2, high=value + 1, low=value - 1, close=value)
            for i, value in enumerate([10, 12, 9, 11, 13, 10, 14, 8, 9, 15, 13, 16])
        ]
        events = hb._strategy_events(rows, "1h")
        for event in events:
            self.assertLessEqual(int(event["confirmed_at_close_time"]), int(rows[-1]["close_time"]))
            evidence = json.loads(event["evidence_json"])
            self.assertTrue(evidence)
            self.assertTrue(all(item["is_closed"] == "true" for item in evidence))
            self.assertTrue(all(int(item["close_time"]) <= int(event["confirmed_at_close_time"]) for item in evidence))

    def test_historical_download_paginates_and_keeps_only_closed_bars(self):
        step = market_structure.INTERVAL_MILLISECONDS["1h"]
        client = PaginatedKlineClient(server_time=6 * step)
        rows = hb.fetch_historical_candles(
            "BTCUSDT", "1h", 0, 5 * step, client=client, page_limit=2,
        )
        self.assertEqual(len(rows), 5)
        self.assertEqual(client.pages, 3)
        self.assertEqual([int(row["open_time"]) for row in rows], [i * step for i in range(5)])
        self.assertTrue(all(row["is_closed"] == "true" for row in rows))
        self.assertTrue(all(row["source"] == "Binance Spot REST API" for row in rows))

    def test_csv_rows_keep_timestamp_strings_for_evidence_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "BTCUSDT_1h.csv"
            hb.save_csv(path, [candle(0, open_=100, high=101, low=99, close=100)])
            rows = hb._closed_rows(path, "BTCUSDT", "1h")
            self.assertIsInstance(rows[0]["open_time"], str)
            self.assertIsInstance(rows[0]["close_time"], str)
            self.assertEqual(rows[0]["source"], "Binance Spot REST API")

    def test_retest_order_waits_until_next_bar_then_fills(self):
        rows = [
            candle(0, open_=105, high=110, low=104, close=108),
            candle(1, open_=107, high=109, low=99, close=101),
            candle(2, open_=101, high=121, low=95, close=115),
        ]
        snapshots = [
            {"candidate": signal(0), "integrity_ok": True},
            {"candidate": signal(1), "integrity_ok": True},
            {"candidate": None, "integrity_ok": True},
        ]
        trades = hb._simulate_slice(
            rows, snapshots, 0, len(rows), minimum_net_rr=1.5, max_setup_age_hours=168,
            fee_bps=hb.Decimal("10"), slippage_bps=hb.Decimal("5"),
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "target")
        self.assertEqual(trades[0]["entry_time_utc"], hb._utc(int(rows[1]["open_time"])))
        self.assertGreater(float(trades[0]["fees_paid_per_unit"]), 0)
        self.assertGreater(float(trades[0]["r_multiple"]), 0)

    def test_open_position_exits_when_integrity_window_fails(self):
        rows = [
            candle(0, open_=105, high=110, low=104, close=108),
            candle(1, open_=107, high=109, low=99, close=101),
            candle(2, open_=101, high=109, low=95, close=102),
            candle(3, open_=102, high=110, low=96, close=103),
        ]
        snapshots = [
            {"candidate": signal(0), "integrity_ok": True},
            {"candidate": None, "integrity_ok": True},
            {"candidate": None, "integrity_ok": False},
            {"candidate": None, "integrity_ok": True},
        ]
        trades = hb._simulate_slice(
            rows, snapshots, 0, len(rows), minimum_net_rr=1.5, max_setup_age_hours=168,
            fee_bps=hb.Decimal("10"), slippage_bps=hb.Decimal("5"),
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "integrity_review_exit")
        self.assertEqual(trades[0]["exit_time_utc"], hb._utc(int(rows[2]["close_time"]) + 1))

    def test_same_bar_stop_and_target_resolves_to_stop(self):
        rows = [
            candle(0, open_=105, high=110, low=104, close=108),
            candle(1, open_=107, high=109, low=99, close=101),
            candle(2, open_=101, high=121, low=88, close=95),
        ]
        snapshots = [
            {"candidate": signal(0), "integrity_ok": True},
            {"candidate": signal(1), "integrity_ok": True},
            {"candidate": None, "integrity_ok": True},
        ]
        trades = hb._simulate_slice(
            rows, snapshots, 0, len(rows), minimum_net_rr=1.5, max_setup_age_hours=168,
            fee_bps=hb.Decimal("10"), slippage_bps=hb.Decimal("5"),
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "stop")
        self.assertLess(float(trades[0]["r_multiple"]), 0)

    def test_orders_are_not_carried_across_train_test_boundary(self):
        rows = [
            candle(0, open_=105, high=110, low=104, close=108),
            candle(1, open_=107, high=109, low=101, close=102),
            candle(2, open_=101, high=109, low=99, close=100),
            candle(3, open_=100, high=110, low=95, close=105),
        ]
        snapshots = [
            {"candidate": signal(0), "integrity_ok": True},
            {"candidate": signal(1), "integrity_ok": True},
            {"candidate": signal(2), "integrity_ok": True},
            {"candidate": None, "integrity_ok": True},
        ]
        train = hb._simulate_slice(
            rows, snapshots, 0, 2, minimum_net_rr=1.5, max_setup_age_hours=168,
            fee_bps=hb.Decimal("10"), slippage_bps=hb.Decimal("5"),
        )
        validation = hb._simulate_slice(
            rows, snapshots, 2, 4, minimum_net_rr=1.5, max_setup_age_hours=168,
            fee_bps=hb.Decimal("10"), slippage_bps=hb.Decimal("5"),
        )
        self.assertEqual(train, [])
        self.assertEqual(len(validation), 1)
        self.assertEqual(validation[0]["setup_id"], "ob-1")

    def test_tuning_uses_only_validation_and_falls_back_if_sample_is_too_small(self):
        rows = [candle(i, open_=100, high=101, low=99, close=100) for i in range(100)]
        snapshots = [{"candidate": None, "integrity_ok": True} for _ in rows]
        report = hb.tune_parameters(
            rows, snapshots, rr_grid=[1.25, 1.5], age_grid=[72, 168],
            fee_bps_per_side=10, slippage_bps_per_side=5, minimum_validation_trades=1,
        )
        self.assertEqual(report["selection_status"], "insufficient_validation_trades_fallback")
        self.assertEqual(report["selected_parameters"], {"minimum_net_rr": 1.5, "max_setup_age_hours": 168})
        self.assertTrue(all(not row["test_was_used_for_selection"] for row in report["candidates"]))
        self.assertTrue(all("test" not in row for row in report["candidates"]))

    def test_end_to_end_snapshot_engine_uses_three_closed_timeframes(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            for interval, count in (("1d", 20), ("4h", 120), ("1h", 480)):
                rows = []
                for i in range(count):
                    close = 100 + ((i % 12) - 6) * 0.7
                    open_ = close + (0.25 if i % 2 else -0.25)
                    rows.append(candle(
                        i, open_=open_, high=max(open_, close) + 0.8,
                        low=min(open_, close) - 0.8, close=close, interval=interval,
                    ))
                hb.save_csv(data_dir / f"BTCUSDT_{interval}.csv", rows)
            rows_1h, snapshots, summary = hb._prepare_snapshots(
                data_dir, "BTCUSDT", fee_bps_per_side=10, slippage_bps_per_side=5,
                minimum_net_rr=1.25, max_setup_age_hours=240, progress_every=0,
            )
            self.assertEqual(len(rows_1h), 480)
            self.assertEqual(len(snapshots), 480)
            self.assertEqual(summary["strategy"], "mtf-bos-ob-retest-v1")
            self.assertTrue(snapshots[-1]["integrity_ok"])
            self.assertTrue(all("candidate" in item for item in snapshots))

    def test_historical_integrity_keeps_gaps_explicit(self):
        rows = [
            candle(0, open_=100, high=101, low=99, close=100),
            candle(1, open_=100, high=101, low=99, close=100),
            candle(3, open_=100, high=101, low=99, close=100),
        ]
        report = market_structure.inspect_candle_integrity(rows, "1h")
        self.assertEqual(report["missing_candle_count"], 1)
        self.assertEqual(len(hb._contiguous_segments(rows, "1h")), 2)


if __name__ == "__main__":
    unittest.main()
