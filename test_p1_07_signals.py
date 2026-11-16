import unittest
from decimal import Decimal as D
import p1_candlestick_research as c
import p1_07_signals as m
import p1_portfolio as old
from types import SimpleNamespace

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
    def test_next_open_on_level_is_not_confirmed_outside_or_inside(self):
        self.assertFalse(m.entry_location('long',D(120),D(120)))
        self.assertTrue(m.entry_location('short',D(119),D(120)))
    def test_later_reclaim_does_not_cancel_prior_continuation_or_leak_daily_extreme(self):
        t=old.START
        daily=[b(t-(20-i)*DAY) for i in range(20)];daily[15]=b(t-5*DAY,h=120,l=80)
        bars=[b(t-3600000),b(t,h=123,l=99,o=100,close=121),b(t+3600000,h=122,l=118,o=121,close=119),b(t+7200000,h=999,l=1,o=119,close=120),b(t+10800000)]
        a=SimpleNamespace(symbol='BTCUSDT',bars=bars,frames={'1d':daily},reg=SimpleNamespace(atr=[1]*5),by_time={x.open_ms:i for i,x in enumerate(bars)},times=[x.open_ms for x in bars])
        a.stop_context_cache={x.open_ms:dict(feature_cutoff_ms=x.open_ms,atr=1,volatility='normal') for x in bars}
        rows,_=m.generate(a)
        c=next(r for r in rows if r['policy']=='T20_CONT' and r['edge']=='upper')
        f=next(r for r in rows if r['policy']=='T20_FAIL' and r['edge']=='upper')
        self.assertEqual(c['entry_time_ms'],t+3600000)
        self.assertEqual(f['entry_time_ms'],t+7200000)
        self.assertEqual(f['stop'],D('123.1'))
    def test_entry_hour_final_volume_cannot_decide_open_admission(self):
        t=old.START
        daily=[b(t-(20-i)*DAY) for i in range(20)];daily[15]=b(t-5*DAY,h=120,l=80)
        bars=[b(t-3600000),b(t,h=123,l=99,o=100,close=121),b(t+3600000,h=122,l=118,o=121,close=119,v=0)]
        a=SimpleNamespace(symbol='BTCUSDT',bars=bars,frames={'1d':daily},reg=SimpleNamespace(atr=[1]*3),by_time={x.open_ms:i for i,x in enumerate(bars)},times=[x.open_ms for x in bars])
        a.stop_context_cache={x.open_ms:dict(feature_cutoff_ms=x.open_ms,atr=1,volatility='normal') for x in bars}
        rows,_=m.generate(a)
        candidate=next(r for r in rows if r['policy']=='T20_CONT' and r['edge']=='upper')
        self.assertEqual(candidate['status'],'eligible')
        self.assertEqual(candidate['entry_time_ms'],t+3600000)
    def test_atr_cannot_bridge_gap_immediately_before_utc_day(self):
        t=old.START
        daily=[b(t-(20-i)*DAY) for i in range(20)];daily[15]=b(t-5*DAY,h=120,l=80)
        bars=[b(t-7200000),b(t,h=123,l=99,o=100,close=121),b(t+3600000,h=122,l=118,o=121,close=119)]
        a=SimpleNamespace(symbol='BTCUSDT',bars=bars,frames={'1d':daily},reg=SimpleNamespace(atr=[1,None,None]),by_time={x.open_ms:i for i,x in enumerate(bars)},times=[x.open_ms for x in bars])
        a.stop_context_cache={x.open_ms:dict(feature_cutoff_ms=x.open_ms,atr=None,volatility='unknown') for x in bars}
        rows,_=m.generate(a)
        candidate=next(r for r in rows if r['policy']=='T20_CONT' and r['edge']=='upper')
        self.assertEqual(candidate['status'],'preconfirmation_ATR_gap')
if __name__=='__main__':unittest.main()
