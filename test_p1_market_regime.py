"""No-network regression coverage for P1-03 causal market-regime observations."""
import csv
import io
import math
import unittest
from dataclasses import replace
from pathlib import Path

import p1_market_regime as m


def make_bars(n=300,step=m.FRAMES['1h'],start=0,base=100.,delta=.1,vol=1.5):
    result=[]
    for i in range(n):
        c=base+delta*i
        result.append(m.Candle(start+i*step,c-.01,c+vol,c-vol,c))
    return result


def obs(signal=10,pat='bullish_engulfing',hour=24,outcome='evaluated'):
    return {'symbol':'BTCUSDT','timeframe':'1h','pattern':pat,
            'signal_open_ms':str(signal*m.FRAMES['1h']),
            'direction':m.PATTERN_SIDES[pat],'split':'train',
            'horizon_hours':str(hour),'outcome_status':outcome,
            'paper_net_pct':'1.5' if outcome=='evaluated' else '',
            'directional_favorable_pct':'2.1' if outcome=='evaluated' else ''}


class MarketRegimeTests(unittest.TestCase):
    def test_only_closed_one_hour_and_above(self):
        self.assertEqual(set(m.FRAMES),{'1h','4h','1d'})
        self.assertEqual(m.HORIZONS,(24,72,168))
        with self.assertRaises(ValueError):m.RegimeSource([], '15m')

    def test_fixed_preregistration_hashes_present(self):
        self.assertEqual(len(m.LOCK_SHA256),64)
        self.assertEqual(len(m.P1_EVENTS_SHA),64)
        self.assertEqual(m.MA_PERIOD,200)
        self.assertEqual(m.SLOPE_BARS,20)
        self.assertEqual(m.ATR_HISTORY,200)

    def test_bullish_regime_uses_only_prepattern(self):
        b=make_bars(delta=.1)
        r=m.RegimeSource(b,'1h').classify(b[299].t,'bullish_engulfing')
        self.assertEqual(r['trend'],'bullish')
        self.assertEqual(r['volatility'],'low')
        self.assertLessEqual(r['regime_data_end_ms'],r['pattern_start_open_ms'])

    def test_bearish_regime(self):
        b=make_bars(base=300,delta=-.1)
        r=m.RegimeSource(b,'1h').classify(b[299].t,'bullish_engulfing')
        self.assertEqual(r['trend'],'bearish')

    def test_transition_or_range(self):
        b=make_bars(delta=0)
        r=m.RegimeSource(b,'1h').classify(b[299].t,'bullish_engulfing')
        self.assertEqual(r['trend'],'transition_or_range')

    def test_future_mutation_never_changes_earlier_signal(self):
        bars=make_bars(340)
        a=m.RegimeSource(bars,'1h').classify(bars[280].t,'three_black_crows')
        changed=bars.copy()
        for i in range(278,len(changed)):
            b=changed[i]
            changed[i]=m.Candle(b.t,b.o+7000,b.h+7000,b.l+7000,b.c+7000)
        b=m.RegimeSource(changed,'1h').classify(bars[280].t,'three_black_crows')
        self.assertEqual(a,b)

    def test_candle_periods_1d_supported(self):
        b=make_bars(step=m.FRAMES['1d'])
        r=m.RegimeSource(b,'1d').classify(b[280].t,'three_white_soldiers')
        self.assertEqual(r['trend'],'bullish')

    def test_candle_periods_4h_supported(self):
        b=make_bars(step=m.FRAMES['4h'])
        r=m.RegimeSource(b,'4h').classify(b[280].t,'morning_star')
        self.assertEqual(r['trend'],'bullish')

    def test_strictly_pre_pattern_start_ma(self):
        b=make_bars()
        ix=285
        r=m.RegimeSource(b,'1h').classify(b[ix].t,'three_white_soldiers')
        p=ix-3
        self.assertAlmostEqual(float(r['ma200_prepattern']),sum(z.c for z in b[p-199:p+1])/200,8)
        self.assertEqual(r['regime_data_end_ms'],b[ix-2].t)

    def test_exact_first_ma_warmup(self):
        b=make_bars()
        # for 2-bar, p=idx-2, require p>=219; idx=221
        a=m.RegimeSource(b,'1h')
        self.assertEqual(a.classify(b[220].t,'bullish_engulfing')['trend'],'insufficient_prior_ma')
        self.assertEqual(a.classify(b[221].t,'bullish_engulfing')['trend'],'bullish')

    def test_short_history_does_not_guess(self):
        b=make_bars(6)
        r=m.RegimeSource(b,'1h').classify(b[-1].t,'bullish_engulfing')
        self.assertEqual(r['trend'],'insufficient_prior_ma')
        self.assertEqual(r['volatility'],'insufficient_prior_atr_distribution')

    def test_missing_lookback_candle_resets_history(self):
        h=m.FRAMES['1h']
        first=make_bars(250)
        extra=make_bars(250,start=first[-1].t+2*h,base=140)
        src=m.RegimeSource(first+extra,'1h')
        r=src.classify(extra[9].t,'bullish_engulfing')
        self.assertEqual(r['trend'],'insufficient_prior_ma')
        self.assertEqual(r['volatility'],'insufficient_prior_atr_distribution')
        self.assertEqual(src.atr[250],None)
        self.assertEqual(src.atr[263],None)
        self.assertIsNotNone(src.atr[264])
        r2=src.classify(extra[-1].t,'bullish_engulfing')
        self.assertEqual(r2['trend'],'bullish')

    def test_pattern_crossing_missing_bar_refused(self):
        h=m.FRAMES['1h']
        a=make_bars(20)
        b=make_bars(20,start=a[-1].t+2*h)
        source=m.RegimeSource(a+b,'1h')
        with self.assertRaisesRegex(ValueError,'crosses a gap'):
            source.classify(b[0].t,'bullish_engulfing')

    def test_first_eligible_bar_after_gap_is_unknown_not_future(self):
        h=m.FRAMES['1h'];a=make_bars(30);b=make_bars(30,start=a[-1].t+2*h)
        source=m.RegimeSource(a+b,'1h')
        r=source.classify(b[1].t,'bullish_engulfing')
        self.assertEqual(r['trend'],'insufficient_prior_ma')

    def test_unknown_pattern_and_missing_timestamp(self):
        b=make_bars(310);source=m.RegimeSource(b,'1h')
        with self.assertRaises(ValueError):source.classify(b[-1].t,'fake_pattern')
        with self.assertRaises(ValueError):source.classify(b[-1].t+1,'bullish_engulfing')

    def test_rising_volatility_high(self):
        bars=make_bars(350,vol=1.)
        for i in range(315,350):
            v=1.+(i-314)*.5
            b=bars[i]
            bars[i]=m.Candle(b.t,b.o,b.c+v,b.c-v,b.c)
        r=m.RegimeSource(bars,'1h').classify(bars[-1].t,'bullish_engulfing')
        self.assertEqual(r['volatility'],'high')

    def test_dropping_volatility_low(self):
        bars=make_bars(350,vol=18.)
        for i in range(300,350):
            v=max(.4,18.-(i-299)*.5)
            b=bars[i]
            bars[i]=m.Candle(b.t,b.o,b.c+v,b.c-v,b.c)
        r=m.RegimeSource(bars,'1h').classify(bars[-1].t,'bullish_engulfing')
        self.assertEqual(r['volatility'],'low')

    def test_flat_atr_percentile_tie_midrank(self):
        bars=make_bars(350,base=100,delta=0,vol=2)
        a=m.RegimeSource(bars,'1h').classify(bars[-1].t,'bullish_engulfing')
        self.assertEqual(a['volatility'],'normal')
        self.assertEqual(a['prior_atr14_percentile'],.5)

    def test_causal_source_csv_dedup_validation(self):
        h=m.FRAMES['1h']
        header='symbol,interval,open_time,close_time,is_closed,source,open,high,low,close,volume\n'
        row=f'BTCUSDT,1h,0,{h-1},true,Binance Spot REST API,99,102,98,100,1\n'
        ok=m.parse_bars((header+row).encode(),'BTCUSDT','1h',1)
        self.assertEqual(len(ok),1)
        with self.assertRaises(ValueError):m.parse_bars((header+row+row).encode(),'BTCUSDT','1h',2)
        with self.assertRaises(ValueError):m.parse_bars((header+row.replace('true','false')).encode(),'BTCUSDT','1h',1)
        with self.assertRaises(ValueError):m.parse_bars((header+row).encode(),'BTCUSDT','15m',1)

    def test_duplicate_or_missing_horizon_disallowed(self):
        rows=[obs(hour=h) for h in m.HORIZONS]
        grouped,count=m.audit_observations(rows)
        self.assertEqual(count,1)
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            m.audit_observations(rows+rows[:1])
        with self.assertRaisesRegex(ValueError,'Missing'):
            m.audit_observations(rows[:-1])

    def test_side_mismatch_disallowed(self):
        r=obs();r['direction']='short'
        with self.assertRaises(ValueError):m.audit_observations([r])

    def test_censored_not_zero_profit(self):
        s={'events':0,'evaluated':0,'censored':0,'directional_favorable':0,
           'paper_net_positive':0,'paper_nets':[]}
        m.update_outcome(s,obs(outcome='right_censored_or_split_boundary'))
        self.assertEqual(s['censored'],1)
        self.assertEqual(s['evaluated'],0)
        self.assertEqual(m._mean(s['paper_nets']),None)
        m.update_outcome(s,obs())
        self.assertEqual(s['paper_nets'],[1.5])

    def test_censor_with_nonempty_profit_rejected(self):
        s={'events':0,'evaluated':0,'censored':0,'directional_favorable':0,
           'paper_net_positive':0,'paper_nets':[]}
        r=obs(outcome='right_censored_or_split_boundary');r['paper_net_pct']='999'
        with self.assertRaises(ValueError):m.update_outcome(s,r)

    def test_unknown_status_rejected(self):
        s={'events':0,'evaluated':0,'censored':0,'directional_favorable':0,
           'paper_net_positive':0,'paper_nets':[]}
        with self.assertRaises(ValueError):m.update_outcome(s,obs(outcome='probably_a_win'))

    def test_lock_checksum_rejection_before_source_loading(self):
        with self.assertRaisesRegex(ValueError,'SHA256'):
            m.verify_inputs(None,None,b'bad')

    def test_reference_precedes_signal_and_no_future(self):
        bars=make_bars(350)
        ix=333
        r=m.RegimeSource(bars,'1h').classify(bars[ix].t,'morning_star')
        self.assertEqual(r['reference_closed_at_utc'],m.datetime.fromtimestamp(bars[ix-2].t/1000,m.timezone.utc).isoformat())
        self.assertLess(r['regime_data_end_ms'],bars[ix].t)

    def test_safe_ratio_and_summary(self):
        self.assertIsNone(m._pct(1,0))
        self.assertEqual(m._pct(2,4),50.0)
        self.assertEqual(m._mean([1.,-1.]),0)


if __name__=='__main__':unittest.main()