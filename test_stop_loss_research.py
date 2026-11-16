import unittest
from decimal import Decimal

import historical_backtest as hb
import stop_loss_research as slr
from market_structure import INTERVAL_MILLISECONDS


def candle(index, *, interval="4h", open_=100, high=103, low=98, close=101):
    step = INTERVAL_MILLISECONDS[interval]
    opened = index * step
    return {
        "symbol": "BTCUSDT", "interval": interval,
        "open_time": str(opened), "open_time_utc": hb._utc(opened),
        "open": str(open_), "high": str(high), "low": str(low), "close": str(close),
        "volume": "10", "close_time": str(opened + step - 1),
        "close_time_utc": hb._utc(opened + step - 1),
        "exchange_close_time": str(opened + step - 1),
        "exchange_close_time_utc": hb._utc(opened + step - 1),
        "is_closed": "true", "source": hb.SOURCE,
    }


def sample_candidate(side="long"):
    return {
        "setup_id": "ob-1", "target_event_id": "swing-target", "side": side,
        "signal_time_ms": 100 * INTERVAL_MILLISECONDS["1h"],
        "setup_confirm_ms": 0, "setup_age_hours": 1.0,
        "zone_low": "99", "zone_high": "101", "entry_reference": "100",
        "stop": "90" if side == "long" else "110",
        "target": "125" if side == "long" else "75", "net_rr": "2.0",
    }


class StopLossResearchTests(unittest.TestCase):
    def test_atr_buffer_widens_long_and_short_stops(self):
        context = {"atr": 4.0, "swing_low": 88.0, "swing_high": 112.0}
        long_result, reason = slr.transform_candidate(
            sample_candidate("long"), context, "atr_4h_buffer",
            atr_multiple=0.25, fee_bps=Decimal("10"), slippage_bps=Decimal("5"))
        self.assertIsNone(reason)
        self.assertEqual(long_result["stop"], "89.0")
        short_result, reason = slr.transform_candidate(
            sample_candidate("short"), context, "atr_4h_buffer",
            atr_multiple=0.25, fee_bps=Decimal("10"), slippage_bps=Decimal("5"))
        self.assertIsNone(reason)
        self.assertEqual(short_result["stop"], "111.0")

    def test_structure_stop_is_beyond_confirmed_swing_and_buffer(self):
        context = {"atr": 4.0, "swing_low": 85.0, "swing_high": 115.0}
        long_result, reason = slr.transform_candidate(
            sample_candidate("long"), context, "swing_4h_buffer",
            atr_multiple=0.25, fee_bps=Decimal("10"), slippage_bps=Decimal("5"))
        self.assertIsNone(reason)
        self.assertEqual(long_result["stop"], "84.0")
        short_result, reason = slr.transform_candidate(
            sample_candidate("short"), context, "swing_4h_buffer",
            atr_multiple=0.25, fee_bps=Decimal("10"), slippage_bps=Decimal("5"))
        self.assertIsNone(reason)
        self.assertEqual(short_result["stop"], "116.0")

    def test_missing_atr_or_confirmed_swing_rejects_widened_candidate(self):
        updated, reason = slr.transform_candidate(
            sample_candidate(), {"atr": None, "swing_low": 80.0, "swing_high": 120.0}, "atr_4h_buffer")
        self.assertEqual(reason, "missing_4h_atr")
        self.assertEqual(updated["net_rr"], "-999")
        updated, reason = slr.transform_candidate(
            sample_candidate(), {"atr": 4.0, "swing_low": None, "swing_high": None}, "swing_4h_buffer")
        self.assertEqual(reason, "missing_confirmed_4h_swing")
        self.assertEqual(updated["net_rr"], "-999")

    def test_4h_pivot_is_unavailable_until_confirmation_candle_closes(self):
        highs = [11, 12, 15, 12, 11, 10, 12, 13]
        lows = [8, 9, 10, 9, 8, 7, 8, 9]
        rows = [candle(i, open_=10, high=highs[i], low=lows[i], close=10)
                for i in range(len(highs))]
        close_after, contexts = slr.build_4h_contexts(rows, atr_period=3)
        self.assertIsNone(contexts[3]["swing_high"])
        self.assertEqual(contexts[4]["swing_high"], 15.0)
        self.assertEqual(close_after[4], int(rows[4]["close_time"]) + 1)

    def test_atr_restarts_after_a_4h_data_gap(self):
        rows = [candle(i, high=103, low=98, close=101) for i in range(4)]
        rows.append(candle(5, high=103, low=98, close=101))
        _, contexts = slr.build_4h_contexts(rows, atr_period=3)
        self.assertIsNotNone(contexts[2]["atr"])
        self.assertIsNone(contexts[4]["atr"])

    def test_original_baseline_candidate_is_unchanged(self):
        candidate = sample_candidate()
        result, reason = slr.transform_candidate(candidate, {}, "ob_1h_baseline")
        self.assertIsNone(reason)
        self.assertEqual(result, candidate)


if __name__ == "__main__":
    unittest.main()
