"""Independent P1-02 causality, locked provenance and summarization tests."""
from __future__ import annotations
import csv
import io
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

import p1_zone_context as m

HOUR=3_600_000


def candles(n, step, *, lows=None, highs=None):
    lows=lows or {};highs=highs or {}
    return [m.Bar(i*step,100.,highs.get(i,110.),lows.get(i,100.),105.) for i in range(n)]


def contexts():
    ref=candles(14,4*HOUR,lows={2:90.,5:90.},highs={2:130.,5:130.})
    # high pivots p2 and p5 are strict highs, lows are strict lows.
    pat=candles(47,HOUR,lows={40:90.2,41:91.},highs={40:115.,41:118.})
    return m.Context.from_bars('1h',pat),m.Context.from_bars('4h',ref)


class CausalPivotTests(unittest.TestCase):
    def test_only_1h_4h_1d_allowed(self):
        self.assertEqual(set(m.FRAMES),{'1h','4h','1d'})
        self.assertEqual(m.FRAME_REF,{'1h':'4h','4h':'1d','1d':'1d'})
        self.assertNotIn('15m',m.FRAMES)

    def test_closed_pivot_has_two_right_candles(self):
        _,ref=contexts()
        self.assertIn((2,4,90.),ref.lows.rows)
        self.assertIn((5,7,90.),ref.lows.rows)
        self.assertEqual(ref.lows.confirmed_exclusive_ms[:2],[20*HOUR,32*HOUR])

    def test_two_preexisting_support_pivots_confirmed(self):
        patt,ref=contexts()
        r=m.classify(patt,ref,41,'bullish_engulfing',5)
        self.assertEqual(r['location'],'near_confirmed_zone')
        self.assertGreaterEqual(r['pivots_near'],2)
        self.assertLess(r['nearest_distance_atr'],.1)

    def test_bearish_resistance_uses_highs_not_lows(self):
        patt,ref=contexts()
        p=list(patt.bars);p[40]=m.Bar(40*HOUR,102.,130.2,100.,101.)
        p[41]=m.Bar(41*HOUR,101.,129.,99.,100.)
        context=m.Context.from_bars('1h',p)
        r=m.classify(context,ref,41,'bearish_engulfing',5)
        self.assertEqual(r['location'],'near_confirmed_zone')

    def test_unclosed_4h_bar_does_not_affect_1h_signal(self):
        patt,ref=contexts();before=m.classify(patt,ref,41,'bullish_engulfing',5)
        b=list(ref.bars);b[10]=m.Bar(40*HOUR,99.,300.,1.,100.)
        changed=m.Context.from_bars('4h',b)
        after=m.classify(patt,changed,41,'bullish_engulfing',5)
        self.assertEqual(before,after)

    def test_future_1h_pattern_cannot_affect_signal(self):
        patt,ref=contexts();before=m.classify(patt,ref,41,'bullish_engulfing',5)
        b=list(patt.bars);b[42]=m.Bar(42*HOUR,100.,400.,.1,105.)
        after=m.classify(m.Context.from_bars('1h',b),ref,41,'bullish_engulfing',5)
        self.assertEqual(before,after)

    def test_right_confirmation_after_pattern_start_excluded(self):
        patt,_=contexts()
        b=candles(14,4*HOUR,lows={2:90.,8:90.},highs={2:130.})
        ref=m.Context.from_bars('4h',b)
        self.assertIn((8,10,90.),ref.lows.rows)
        result=m.classify(patt,ref,41,'bullish_engulfing',5)
        self.assertEqual(result['location'],'near_single_pivot_unconfirmed')

    def test_missing_recent_closed_4h_classified_stale(self):
        patt,ref=contexts()
        b=[x for x in ref.bars if x.t!=36*HOUR]
        after=m.classify(patt,m.Context.from_bars('4h',b),41,'bullish_engulfing',5)
        self.assertEqual(after['location'],'reference_data_gap_or_stale')

    def test_future_only_pivots_not_counted(self):
        patt,ref=contexts()
        x=m.classify(patt,ref,19,'bullish_engulfing',5)
        self.assertIn(x['location'],('no_preexisting_pivot','near_single_pivot_unconfirmed'))
        self.assertNotEqual(x['location'],'near_confirmed_zone')

    def test_unknown_atr_requires_no_price_claim(self):
        patt,ref=contexts()
        x=m.classify(patt,ref,41,'bullish_engulfing',None)
        self.assertEqual(x['location'],'insufficient_prior_atr')
        self.assertIsNone(x['zone_reference'])

    def test_invalid_or_unsupported_reference_rejected(self):
        patt,ref=contexts()
        with self.assertRaises(ValueError):m.classify(patt,patt,41,'bullish_engulfing',5)
        with self.assertRaises(ValueError):m.classify(patt,ref,41,'unknown',5)

    def test_pattern_crosses_gap_rejected(self):
        patt,ref=contexts()
        bars=[x for x in patt.bars if x.t!=40*HOUR]
        c=m.Context.from_bars('1h',bars)
        with self.assertRaises(ValueError):m.classify(c,ref,40,'bullish_engulfing',5)

    def test_tied_lows_not_confirmed(self):
        b=candles(14,4*HOUR,lows={2:90.,3:90.,5:90.})
        x=m.Context.from_bars('4h',b)
        self.assertNotIn((2,4,90.),x.lows.rows)
        self.assertNotIn((3,5,90.),x.lows.rows)

    def test_daily_reference_never_uses_current_daily_pattern_candle(self):
        b=candles(15,24*HOUR,lows={2:90.,5:90.,9:90.2,10:91.})
        ctx=m.Context.from_bars('1d',b)
        x=m.classify(ctx,ctx,10,'bullish_engulfing',5)
        self.assertEqual(x['location'],'near_confirmed_zone')
        self.assertEqual(x['pivots_near'],2)


class EvidenceTests(unittest.TestCase):
    def test_invalid_locked_source_rejected_before_processing(self):
        with tempfile.TemporaryDirectory() as d:
            a=Path(d)/'lock.json';a.write_text('{}',encoding='utf8')
            with self.assertRaisesRegex(ValueError,'lock SHA256'):
                m.analyze(Path('missing.zip'),Path('missing_p1.zip'),a,Path(d)/'out')

    def test_missing_event_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            z1=Path(d)/'a.zip';z2=Path(d)/'b.zip'
            with ZipFile(z1,'w') as z:z.writestr('p1_01_events.csv','tampered')
            with ZipFile(z2,'w') as z:z.writestr('mock.txt','')
            with ZipFile(z1) as p1,ZipFile(z2) as src:
                with self.assertRaises((ValueError,KeyError)):
                    m.checked_sources(src,p1,{'frozen_data_prefixes':{}})

    def test_numeric_summary_and_censored_count(self):
        acc={}
        r={'symbol':'BTCUSDT','timeframe':'1h','pattern':'bullish_engulfing',
           'split':'train','horizon_hours':'24','outcome_status':'evaluated',
           'paper_net_pct':'0.56','directional_favorable_pct':'0.9'}
        loc='near_confirmed_zone'
        from collections import defaultdict
        acc=defaultdict(lambda:dict(events=0,evaluated=0,censored=0,net_sum=0.,net_pos=0,directional_pos=0))
        m._summary_accumulate(acc,r,loc)
        c={**r,'outcome_status':'right_censored_or_split_boundary','paper_net_pct':''}
        m._summary_accumulate(acc,c,loc)
        y=m.build_summary(acc)[0]
        self.assertEqual((y['events'],y['evaluated'],y['censored']), (2,1,1))
        self.assertEqual(y['mean_paper_net_pct'],.56)
        self.assertEqual(y['directional_positive_pct'],100.)

    def test_near_vs_far_compares_only_matched_strata(self):
        k=dict(symbol='BTCUSDT',timeframe='4h',pattern='morning_star',split='train',horizon_hours=24)
        near=dict(**k,location='near_confirmed_zone',evaluated=10,mean_paper_net_pct=.2,paper_net_positive_pct=60.)
        far=dict(**k,location='far_from_nearest_pivot',evaluated=5,mean_paper_net_pct=-.1,paper_net_positive_pct=40.)
        data=m.compare_strata([near,far])
        self.assertEqual(len(data),1)
        self.assertAlmostEqual(data[0]['mean_diff_near_minus_far_pct'],.3)
        self.assertEqual(data[0]['near_evaluated'],10)

    def test_unmatched_strata_yields_null_delta(self):
        x=dict(symbol='BTCUSDT',timeframe='4h',pattern='morning_star',split='train',horizon_hours=24,
               location='near_confirmed_zone',evaluated=9,mean_paper_net_pct=.2,paper_net_positive_pct=55.)
        z=m.compare_strata([x])[0]
        self.assertIsNone(z['mean_diff_near_minus_far_pct'])
        self.assertEqual(z['far_evaluated'],0)

    def test_timeframe_pool_keeps_frames_separate(self):
        from collections import defaultdict
        a=defaultdict(lambda:dict(events=0,evaluated=0,censored=0,net_sum=0.,net_pos=0,directional_pos=0))
        for frame in ('1h','1d'):
            x=dict(symbol='BTCUSDT',timeframe=frame,pattern='morning_star',split='train',horizon_hours=24,
                   outcome_status='evaluated',paper_net_pct='.2',directional_favorable_pct='.6')
            m._summary_accumulate(a,x,'near_confirmed_zone')
        r=m.pool_by_frame(a)
        self.assertEqual(len(r),2)
        self.assertEqual({z['timeframe'] for z in r},{'1h','1d'})


if __name__=='__main__':unittest.main()