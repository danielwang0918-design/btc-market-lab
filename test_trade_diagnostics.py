import unittest
import trade_diagnostics as td


class TradeDiagnosticsTests(unittest.TestCase):
    def test_long_and_short_excursions_are_directional(self):
        hourly=[{"open_time":"1000","open_time_utc":"x","high":"106","low":"97"},
                {"open_time":"4600","open_time_utc":"y","high":"108","low":"95"},
                {"open_time":"8200","open_time_utc":"z","high":"104","low":"99"}]
        times=[1000,4600,8200]
        trade={"entry_time_utc":"1970-01-01T00:00:01+00:00",
               "exit_time_utc":"1970-01-01T00:00:09+00:00","direction":"long",
               "raw_entry_price":"100","stop_price":"90","setup_id":"long"}
        result=td.excursion_metrics(trade,hourly,times)
        self.assertEqual(result["mfe_price"],8.0); self.assertEqual(result["mae_price"],5.0)
        self.assertEqual(result["mfe_r"],.8)
        short=dict(trade,direction="short",stop_price="110",setup_id="short")
        result=td.excursion_metrics(short,hourly,times)
        self.assertEqual(result["mfe_price"],5.0); self.assertEqual(result["mae_price"],8.0)

    def test_ema_resets_after_gap(self):
        rows=[{"open_time":str(i*100),"close":str(i+1)} for i in range(21)]
        rows.append({"open_time":str(22*100),"close":"22"})
        ema=td._ema20_by_segment(rows,100)
        self.assertIsNotNone(ema[19]); self.assertIsNone(ema[21])

    def test_trend_alignment_requires_position_and_slope(self):
        self.assertEqual(td._trend_alignment("long",{"position_pct":1,"slope_pct_3bars":1}),"aligned")
        self.assertEqual(td._trend_alignment("short",{"position_pct":1,"slope_pct_3bars":1}),"opposed")
        self.assertEqual(td._trend_alignment("long",{"position_pct":1,"slope_pct_3bars":-1}),"mixed")


if __name__=="__main__": unittest.main()
