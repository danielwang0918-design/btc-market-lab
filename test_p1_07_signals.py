import unittest
from decimal import Decimal as D
import p1_candlestick_research as c
import p1_07_signals as m

DAY=86400000
def b(t,h=110,l=90,o=100,close=100,v=1):return c.Candle(t,o,h,l,close,v)
class ReferenceTests(unittest.TestCase):
    def daily(self):
        x=[b(i*DAY) for i in range(20)];x[15]=b(15*DAY,h=120,l=80);return x
    def test_exact_previous20_days_exclude_current_day(self):
        ds=self.daily()+[b(20*DAY,h=999,l=1)]
        r=m.daily_reference(ds,20*DAY+3600000)
        self.assertEqual(r['high'],120);self.assertEqual(r['low'],80);self.assertEqual(r['low_age'],5)
    def test_most_recent_tied_extreme_is_used(self):
        ds=self.daily();ds[19]=b(19*DAY,h=120,l=80)
        self.assertEqual(m.daily_reference(ds,20*DAY)['low_age'],1)
    def test_missing_or_stale_daily_reference_is_unknown(self):
        ds=self.daily();self.assertIsNone(m.daily_reference(ds[:10]+ds[11:],20*DAY))
        self.assertIsNone(m.daily_reference(ds,21*DAY))
    def test_20_hours_cannot_substitute_for_20_days(self):
        self.assertIsNone(m.daily_reference([b(i*3600000) for i in range(20)],DAY))
    def test_future_daily_prices_cannot_change_reference(self):
        ds=self.daily();a=m.daily_reference(ds,20*DAY)
        self.assertEqual(a,m.daily_reference(ds+[b(20*DAY,h=999,l=1)],20*DAY))
    def test_running_intraday_extreme_does_not_use_later_hours(self):
        bars=[b(i*3600000,h=101+i,l=99-i) for i in range(6)]
        p=m.intraday_prefix(bars,2)
        self.assertEqual(min(x.l for x in p),97);self.assertEqual(max(x.h for x in p),103)
    def test_intraday_gap_and_zero_volume_are_unknown(self):
        self.assertIsNone(m.intraday_prefix([b(0),b(7200000)],1))
        self.assertIsNone(m.intraday_prefix([b(0),b(3600000,v=0)],1))
    def test_failed_and_continuation_have_opposite_known_sides(self):
        self.assertEqual(m.arm_side('T20_FAIL','upper'),'short')
        self.assertEqual(m.arm_side('T20_CONT','upper'),'long')
        self.assertEqual(m.arm_side('T20_FAIL','lower'),'long')
    def test_source_strategy_integrity_exit_is_not_new_independent_exit(self):
        class A:pass
        a=A();a.bars=[b(0)];a.times=[0];a.by_time={0:0};a.snaps=[{'integrity_ok':False}]
        x=m.execution_view(a)
        self.assertTrue(x.snaps[0]['integrity_ok']);self.assertFalse(a.snaps[0]['integrity_ok'])
if __name__=='__main__':unittest.main()
