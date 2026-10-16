import unittest
from decimal import Decimal

import anti_gambling_reference_backtest as arb


def daily_rows(closes):
    step = arb.INTERVAL_MILLISECONDS["1d"]
    base = 1_699_920_000_000
    rows = []
    previous_open = None
    for i, close in enumerate(closes):
        opened = float(close) if previous_open is None else float(close)
        rows.append({
            "symbol": "BTCUSDT",
            "interval": "1d",
            "open_time": base + i * step,
            "open_time_utc": "test",
            "open": str(opened),
            "high": str(max(opened, float(close)) + 0.2),
            "low": str(min(opened, float(close)) - 0.2),
            "close": str(float(close)),
            "volume": "100",
            "close_time": base + i * step + step - 1,
            "close_time_utc": "test",
            "is_closed": "true",
            "source": "https://data-api.binance.vision",
        })
        previous_open = opened
    return rows


class AntiGamblingReferenceBacktestTests(unittest.TestCase):
    def test_sma_features_require_full_slow_window(self):
        rows = daily_rows(list(range(100, 125)))
        features = arb.build_features(rows)
        self.assertIsNone(features[18]["sma_slow"])
        self.assertIsNotNone(features[19]["sma_slow"])
        self.assertIsNotNone(features[19]["sma_fast"])

    def test_buy_signal_fills_at_next_daily_open(self):
        rows = daily_rows([100, 100, 100, 100, 100])
        rows[2]["open"] = "101"
        rows[2]["high"] = "101.2"
        features = [
            {"sma_fast": None, "sma_slow": None, "long_condition": False},
            {"sma_fast": 101.0, "sma_slow": 100.0, "long_condition": True},
            {"sma_fast": 100.0, "sma_slow": 100.0, "long_condition": False},
            {"sma_fast": 99.0, "sma_slow": 100.0, "long_condition": False},
            {"sma_fast": 99.0, "sma_slow": 100.0, "long_condition": False},
        ]
        trades = arb.simulate_slice(
            rows, features, 0, len(rows),
            fee_bps_per_side=10, slippage_bps_per_side=5,
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["signal_time_utc"], arb.hb._utc(rows[1]["close_time"] + 1))
        self.assertEqual(trades[0]["entry_time_utc"], arb.hb._utc(rows[2]["open_time"]))
        self.assertEqual(trades[0]["exit_reason"], "split_end")

    def test_close_confirmed_take_profit_exits_next_daily_open(self):
        rows = daily_rows([100, 100, 100, 116, 117])
        rows[2]["open"] = "100"
        rows[2]["high"] = "102"
        rows[2]["low"] = "99"
        rows[2]["close"] = "101"
        rows[3]["open"] = "101"
        rows[3]["high"] = "117"
        rows[3]["low"] = "100"
        rows[3]["close"] = "116"
        rows[4]["open"] = "116.5"
        rows[4]["high"] = "118"
        rows[4]["low"] = "116"
        rows[4]["close"] = "117"
        features = [
            {"sma_fast": None, "sma_slow": None, "long_condition": False},
            {"sma_fast": 101.0, "sma_slow": 100.0, "long_condition": True},
            {"sma_fast": 100.0, "sma_slow": 100.0, "long_condition": False},
            {"sma_fast": 100.0, "sma_slow": 100.0, "long_condition": False},
            {"sma_fast": 100.0, "sma_slow": 100.0, "long_condition": False},
        ]
        trades = arb.simulate_slice(
            rows, features, 0, len(rows),
            fee_bps_per_side=10, slippage_bps_per_side=5,
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "take_profit_close_signal")
        self.assertEqual(trades[0]["exit_time_utc"], arb.hb._utc(rows[4]["open_time"]))

    def test_no_entry_when_fast_sma_is_not_above_slow_sma(self):
        rows = daily_rows([100] * 25)
        features = arb.build_features(rows)
        trades = arb.simulate_slice(rows, features, 0, len(rows))
        self.assertEqual(trades, [])

    def test_metrics_report_win_rate_and_cost_sensitive_returns(self):
        trades = [
            {"net_return_on_exposed_capital_percent": 5.0, "portfolio_return_percent": 1.0,
             "bars_held": 4, "exit_reason": "take_profit_close_signal"},
            {"net_return_on_exposed_capital_percent": -4.0, "portfolio_return_percent": -0.8,
             "bars_held": 2, "exit_reason": "stop_loss_close_signal"},
        ]
        metrics = arb._metrics(trades)
        self.assertEqual(metrics["win_rate_percent"], 50.0)
        self.assertAlmostEqual(metrics["expectancy_percent_on_exposed_capital"], 0.5)
        self.assertEqual(metrics["trade_count"], 2)


if __name__ == "__main__":
    unittest.main()
