"""Deterministic regression checks for P1-01 closed-bar pattern research."""
import csv
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import p1_candlestick_research as m
HOUR=m.FRAMES['1h']

def b(i,o,c,hi=None,lo=None,step=HOUR):
    return m.Candle(i*step,float(o),float(max(o,c) if hi is None else hi),float(min(o,c) if lo is None else lo),float(c),1.)

class StudyTests(unittest.TestCase):
    def test_only_three_allowed_timeframes(self):
        self.assertEqual(set(m.FRAMES),{'1h','4h','1d'})
        with self.assertRaises(ValueError):m.study_rows([b(0,1,2)],'BTCUSDT','15m')
    def test_bullish_engulfing(self):
        arr=[b(0,105,100),b(1,99,107)]
        self.assertIn(('bullish_engulfing','long'),m.confirmed_at(arr,1,0))
    def test_bearish_engulfing(self):
        arr=[b(0,100,105),b(1,106,97)]
        self.assertIn(('bearish_engulfing','short'),m.confirmed_at(arr,1,0))
    def test_equal_size_is_not_strict_engulf(self):
        arr=[b(0,105,100),b(1,100,105)]
        self.assertNotIn(('bullish_engulfing','long'),m.confirmed_at(arr,1,0))
    def test_morning_star_crypto_no_gap(self):
        arr=[b(0,110,100),b(1,100,101,hi=101,lo=99),b(2,101,108)]
        self.assertIn(('morning_star','long'),m.confirmed_at(arr,2,0))
    def test_not_morning_star_if_third_not_midpoint(self):
        arr=[b(0,110,100),b(1,100,101,hi=101,lo=99),b(2,101,104)]
        self.assertNotIn(('morning_star','long'),m.confirmed_at(arr,2,0))
    def test_three_white_soldiers(self):
        arr=[b(0,100,105,hi=106),b(1,105,111,hi=112),b(2,111,119,hi=120)]
        self.assertIn(('three_white_soldiers','long'),m.confirmed_at(arr,2,0))
    def test_three_black_crows(self):
        arr=[b(0,120,115,lo=114),b(1,115,109,lo=108),b(2,109,102,lo=101)]
        self.assertIn(('three_black_crows','short'),m.confirmed_at(arr,2,0))
    def test_future_bar_cannot_change_pattern_at_i(self):
        arr=[b(0,105,100),b(1,99,107)]
        a=m.confirmed_at(arr,1,0)
        arr+=[b(2,107,70),b(3,70,150)]
        self.assertEqual(a,m.confirmed_at(arr,1,0))
    def test_segment_boundary_prevents_cross_gap_pattern(self):
        arr=[b(0,105,100),b(2,99,107)]
        starts=m.contiguous_segment_start(arr,HOUR)
        self.assertEqual(starts,[0,1])
        self.assertEqual(m.confirmed_at(arr,1,starts[1]),[])
    def test_24h_next_open_and_exit_close(self):
        arr=[b(i,100+i*.01,101+i*.01) for i in range(30)]
        arr[0]=b(0,105,100)
        arr[1]=b(1,99,107)
        rows=m.study_rows(arr,'BTCUSDT','1h',horizons=(24,))
        signal=[r for r in rows if r['pattern']=='bullish_engulfing' and r['signal_open_ms']==HOUR][0]
        self.assertEqual(signal['entry_next_open_utc'],m.utc(2*HOUR))
        self.assertEqual(signal['exit_close_utc'],m.utc(26*HOUR))
        self.assertEqual(signal['outcome_status'],'evaluated')
    def test_censor_on_incomplete_24h_followup(self):
        arr=[b(0,105,100),b(1,99,107)]+[b(i,110,112) for i in range(2,18)]
        rows=m.study_rows(arr,'BTCUSDT','1h',(24,))
        s=[r for r in rows if r['pattern']=='bullish_engulfing'][0]
        self.assertEqual(s['outcome_status'],'right_censored_or_split_boundary')
        self.assertIsNone(s['paper_net_pct'])
    def test_4h_24_hour_is_six_bars(self):
        step=m.FRAMES['4h']
        arr=[b(0,105,100,step=step),b(1,99,107,step=step)]+[b(i,110,112,step=step) for i in range(2,13)]
        rows=m.study_rows(arr,'ETHUSDT','4h',(24,))
        s=[r for r in rows if r['pattern']=='bullish_engulfing'][0]
        self.assertEqual(s['exit_close_utc'],m.utc((1+6+1)*step))
    def test_net_includes_roundtrip_costs(self):
        _,net=m.paper_return(100,100,'long')
        self.assertLess(net,-.29)
        self.assertGreater(net,-.32)
    def test_both_directions_are_hypothetical(self):
        up,net=m.paper_return(100,110,'long')
        down,snet=m.paper_return(100,90,'short')
        self.assertGreater(net,0);self.assertGreater(snet,0)
        self.assertAlmostEqual(up,10);self.assertAlmostEqual(down,10)
    def test_bad_candle_schema_and_unclosed_rejected(self):
        header='symbol,interval,open_time,close_time,open,high,low,close,volume,is_closed,source\n'
        row='BTCUSDT,1h,0,3599999,99,101,98,100,1,false,Binance Spot REST API\n'
        with self.assertRaises(ValueError):m.parse_bars((header+row).encode(),'BTCUSDT','1h',1)
    def test_bad_source_sha_checked_before_patterns(self):
        with tempfile.TemporaryDirectory() as d:
            a=Path(d)/'lock.json';a.write_text('{}',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'DIGEST'):m.read_lock(a)
    def test_summary_does_not_select_best(self):
        rows=[{'symbol':'BTCUSDT','timeframe':'1h','pattern':'morning_star','split':'train',
               'horizon_hours':24,'outcome_status':'evaluated','paper_net_pct':-2,
               'directional_favorable_pct':1}]
        s=next(x for x in m.summarize(rows) if x['pattern']=='morning_star')
        self.assertEqual(s['observed_signals'],1)
        self.assertEqual(s['net_positive_percent_paper'],0)
        self.assertEqual(s['directional_positive_percent'],100)
    def test_trend_context_does_not_use_pattern_candles(self):
        arr=[b(0,5,4),b(1,4,3),b(2,3,2),b(3,2,3),b(4,3,4)]
        self.assertEqual(m.pretrend(arr,4,3,0),'down')
    def test_atr_must_not_peek_before_gap(self):
        arr=[b(i,100,101) for i in range(18)]
        self.assertIsNone(m.atr14_before(arr,15,1))
        self.assertIsNotNone(m.atr14_before(arr,16,1))
    def test_no_intra_phase_lookahead(self):
        self.assertEqual(m.split_for(m.START_VALIDATION),'validation')
        self.assertEqual(m.split_for(m.START_EXPLORATORY),'exploratory_test_opened')
        self.assertEqual(m.split_end('train',0),m.START_VALIDATION)

    # Actual frozen-data examples: exact inclusive thresholds were lost to float
    # subtraction. These use the original v1 definitions, without any tolerance.
    def test_exact_bullish_engulfing_40_percent_boundary(self):
        arr=[b(0,308.95,308.62,310.51,307.98),
             b(1,308.62,309.40,309.72,307.77)]
        self.assertIn(('bullish_engulfing','long'),m.confirmed_at(arr,1,0))

    def test_exact_bearish_engulfing_40_percent_boundary(self):
        arr=[b(0,213.60,213.67,214.38,213.14),
             b(1,213.67,212.93,213.85,212.00)]
        self.assertIn(('bearish_engulfing','short'),m.confirmed_at(arr,1,0))

    def test_exact_morning_star_50_percent_boundary(self):
        arr=[b(0,137.92,136.53,137.98,135.20),
             b(1,136.53,136.62,138.15,135.80),
             b(2,136.63,137.40,137.88,136.61)]
        self.assertIn(('morning_star','long'),m.confirmed_at(arr,2,0))

    def test_exact_soldiers_upper_wick_half_body_boundary(self):
        arr=[b(0,32.09,32.25,32.26,32.06),
             b(1,32.25,32.73,32.97,32.23),
             b(2,32.73,32.86,32.89,32.68)]
        self.assertIn(('three_white_soldiers','long'),m.confirmed_at(arr,2,0))

    def test_exact_crows_lower_wick_half_body_boundary(self):
        arr=[b(0,13.63,13.55,13.63,13.53),
             b(1,13.56,13.52,13.57,13.50),
             b(2,13.52,13.46,13.52,13.43)]
        self.assertIn(('three_black_crows','short'),m.confirmed_at(arr,2,0))

    def test_below_exact_threshold_is_rejected_without_epsilon(self):
        arr=[b(0,308.95,308.62,310.51,307.98),
             b(1,308.62,309.39999999,309.72,307.77)]
        self.assertNotIn(('bullish_engulfing','long'),m.confirmed_at(arr,1,0))

    @staticmethod
    def raw_bars(frame='1h', opens=(0,), closed='true'):
        stream=io.StringIO(newline='')
        writer=csv.writer(stream)
        writer.writerow(['symbol','interval','open_time','close_time','open','high',
                         'low','close','volume','is_closed','source'])
        for t in opens:
            writer.writerow(['BTCUSDT',frame,t,t+m.FRAMES[frame]-1,
                             '100.123456789123456789','102','99','101','1',closed,
                             'Binance Spot REST API'])
        return stream.getvalue().encode()

    def test_parser_preserves_original_decimal_price(self):
        arr=m.parse_bars(self.raw_bars(),'BTCUSDT','1h',1)
        self.assertEqual(arr[0].o,Decimal('100.123456789123456789'))

    def test_closed_flag_cannot_override_locked_close_cutoff(self):
        with self.assertRaisesRegex(ValueError,'cutoff'):
            m.parse_bars(self.raw_bars(),'BTCUSDT','1h',1,closed_through_ms=HOUR-1)

    def test_all_archive_hashes_checked_before_any_pattern_detection(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); archive=root/'input.zip'
            meta={}
            with ZipFile(archive,'w') as z:
                for frame in m.FRAMES:
                    raw=self.raw_bars(frame)
                    name=f'BTCUSDT_{frame}.csv'
                    meta[name]={'sha256':m.sha(raw),'bytes':len(raw),'frozen_bars':1,
                                'frozen_last_close_exclusive_ms':m.FRAMES[frame]}
                    z.writestr(name,raw if frame!='1d' else raw+b'CORRUPT')
            with patch.object(m,'read_lock',return_value={'symbols':['BTCUSDT'],
                    'frozen_data_prefixes':meta}), patch.object(m,'study_rows') as detect:
                with self.assertRaisesRegex(ValueError,'SHA256'):
                    m.main(['--frozen-archive',str(archive),'--lock',str(root/'lock'),
                            '--out-dir',str(root/'out')])
                detect.assert_not_called()
            self.assertFalse((root/'out').exists())

    def test_duplicate_zip_members_fail_before_hash_or_detection(self):
        import warnings
        with tempfile.TemporaryDirectory() as d:
            archive=Path(d)/'input.zip'
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning)
                with ZipFile(archive,'w') as z:
                    z.writestr('BTCUSDT_1h.csv',b'bad')
                    z.writestr('BTCUSDT_1h.csv',b'other')
            with patch.object(m,'read_lock',return_value={'symbols':['BTCUSDT'],
                    'frozen_data_prefixes':{'BTCUSDT_1h.csv':{'sha256':'bad','bytes':3}}}):
                with self.assertRaisesRegex(ValueError,'Duplicate archive'):
                    m.main(['--frozen-archive',str(archive),'--lock','unused'])

    def test_duplicate_horizons_cannot_duplicate_event_rows(self):
        with self.assertRaises(ValueError):
            m.study_rows([b(0,105,100),b(1,99,107)],'BTCUSDT','1h',(24,24))

    def test_nonfinite_paper_fills_rejected(self):
        for entry,exit_ in ((float('nan'),100),(100,float('inf'))):
            with self.subTest(entry=entry,exit=exit_), self.assertRaises(ValueError):
                m.paper_return(entry,exit_,'long')

    def test_zero_event_patterns_still_reported(self):
        rows=[{'symbol':'BTCUSDT','timeframe':'1h','pattern':'morning_star',
               'split':'train','horizon_hours':24,'outcome_status':'evaluated',
               'paper_net_pct':-2,'directional_favorable_pct':1}]
        summary={r['pattern']:r for r in m.summarize(rows)}
        self.assertEqual(set(summary),set(m.PATTERNS))
        zero=summary['three_black_crows']
        self.assertEqual(zero['observed_signals'],0)
        self.assertIsNone(zero['net_positive_percent_paper'])

    def test_censor_reasons_separate_gap_boundary_and_tail(self):
        base=[b(0,105,100),b(1,99,107)]+[b(i,110,112) for i in range(2,35)]
        for reason,arr in (
            ('data_gap',base[:10]+base[11:]),
            ('incomplete_followup',base[:20]),
            ('split_boundary',[m.Candle(x.open_ms+m.START_VALIDATION-10*HOUR,
                x.o,x.h,x.l,x.c,x.v) for x in base])):
            with self.subTest(reason=reason):
                r=next(r for r in m.study_rows(arr,'BTCUSDT','1h',(24,))
                       if r['pattern']=='bullish_engulfing')
                self.assertEqual(r['censor_reason'],reason)
                self.assertIsNone(r['paper_net_pct'])

    def test_one_day_holds_exactly_one_closed_day(self):
        step=m.FRAMES['1d']
        arr=[b(0,105,100,step=step),b(1,99,107,step=step),
             b(2,110,112,step=step)]
        r=next(r for r in m.study_rows(arr,'BTCUSDT','1d',(24,))
               if r['pattern']=='bullish_engulfing')
        self.assertEqual(r['entry_next_open_utc'],m.utc(2*step))
        self.assertEqual(r['exit_close_utc'],m.utc(3*step))

    def test_gap_at_next_open_does_not_delay_hypothetical_entry(self):
        arr=[b(0,105,100),b(1,99,107)]+[b(i,110,112) for i in range(3,35)]
        r=next(r for r in m.study_rows(arr,'BTCUSDT','1h',(24,))
               if r['pattern']=='bullish_engulfing')
        self.assertEqual(r['outcome_status'],'right_censored_or_split_boundary')
        self.assertIsNone(r['entry_next_open_utc'])

    def test_exit_exactly_at_split_boundary_is_allowed(self):
        origin=m.START_VALIDATION-26*HOUR
        arr=[b(0,105,100),b(1,99,107)]+[b(i,110,112) for i in range(2,30)]
        arr=[m.Candle(x.open_ms+origin,x.o,x.h,x.l,x.c,x.v) for x in arr]
        r=next(r for r in m.study_rows(arr,'BTCUSDT','1h',(24,))
               if r['pattern']=='bullish_engulfing')
        self.assertEqual(r['split'],'train')
        self.assertEqual(r['outcome_status'],'evaluated')
        self.assertEqual(r['exit_close_utc'],m.utc(m.START_VALIDATION))

    def test_fee_and_slippage_cashflows_in_both_directions(self):
        for side,entry_fill,exit_fill in (
                ('long',Decimal('100.05'),Decimal('109.945')),
                ('short',Decimal('99.95'),Decimal('90.045'))):
            exit_price=110 if side=='long' else 90
            gross=(exit_fill-entry_fill) if side=='long' else (entry_fill-exit_fill)
            expected=100*(gross-Decimal('.001')*(entry_fill+exit_fill))/entry_fill
            _,net=m.paper_return(100,exit_price,side)
            self.assertAlmostEqual(net,float(expected),places=10)

    def test_favorable_direction_is_not_positive_net(self):
        directional,net=m.paper_return(100,100.1,'long')
        self.assertGreater(directional,0)
        self.assertLess(net,0)

    def test_extreme_wicks_do_not_invent_stop_or_target_exits(self):
        base=[b(0,105,100),b(1,99,107)]+[b(i,110,112) for i in range(2,30)]
        changed=base.copy();changed[5]=b(5,110,112,1000,1)
        def outcome(arr):
            return next(r['paper_net_pct'] for r in m.study_rows(arr,'BTCUSDT','1h',(24,))
                        if r['pattern']=='bullish_engulfing' and r['signal_open_ms']==HOUR)
        self.assertEqual(outcome(base),outcome(changed))

    def test_all_subhour_frames_rejected_at_data_and_study_entrypoints(self):
        for frame in ('1m','5m','15m','30m'):
            with self.subTest(frame=frame):
                with self.assertRaises(ValueError):m.parse_bars(b'', 'BTCUSDT',frame,1)
                with self.assertRaises(ValueError):m.study_rows([], 'BTCUSDT',frame)

    def test_parser_rejects_integrity_and_provenance_errors(self):
        raw=self.raw_bars().decode()
        for old,new in (('BTCUSDT','ETHUSDT'),('Binance Spot REST API','Other'),
                        (',1h,',',4h,'),(',0,3599999,',',1,3600000,'),
                        (',102,99,101,',',98,99,101,'),(',102,99,101,',',NaN,99,101,'),
                        (',102,99,101,',',Infinity,99,101,')):
            with self.subTest(new=new),self.assertRaises(ValueError):
                m.parse_bars(raw.replace(old,new).encode(),'BTCUSDT','1h',1)
        for opens in ((0,0),(HOUR,0)):
            with self.subTest(opens=opens),self.assertRaises(ValueError):
                m.parse_bars(self.raw_bars(opens=opens),'BTCUSDT','1h',2)

    def test_full_universe_grid_includes_absent_splits_and_patterns(self):
        rows=m.summarize([],symbols=['BTCUSDT'])
        self.assertEqual(len(rows),3*5*3*3)
        self.assertTrue(all(r['observed_signals']==0 and r['mean_paper_net_pct'] is None
                            for r in rows))

    def test_appending_future_candles_does_not_change_signal_features(self):
        arr=[b(i,100+i,101+i,102+i,99+i) for i in range(50)]
        arr[19]=b(19,130,120);arr[20]=b(20,119,132)
        def features(bars):
            return {tuple(r[k] for k in ('pattern','signal_open_ms','prior_trend_3',
                     'atr14_before_signal','split'))
                    for r in m.study_rows(bars,'BTCUSDT','1h',(24,))
                    if r['signal_open_ms']<=21*HOUR}
        self.assertEqual(features(arr[:22]),features(arr))

    def test_zero_volume_next_open_cannot_be_a_paper_fill(self):
        arr=[b(0,105,100),b(1,99,107)]+[b(i,110,112) for i in range(2,30)]
        before=m.study_rows(arr,'BTCUSDT','1h',(24,))
        arr[2]=m.Candle(2*HOUR,110,110,110,110,0)
        after=m.study_rows(arr,'BTCUSDT','1h',(24,))
        self.assertEqual({r['signal_open_ms'] for r in before if r['pattern']=='bullish_engulfing'},
                         {r['signal_open_ms'] for r in after if r['pattern']=='bullish_engulfing'})
        r=next(r for r in after if r['pattern']=='bullish_engulfing')
        self.assertEqual(r['outcome_status'],'unavailable_fixed_time_fill')
        self.assertEqual(r['censor_reason'],'zero_volume_entry')
        self.assertIsNone(r['paper_net_pct'])

    def test_zero_volume_exit_is_not_classified_as_win_or_loss(self):
        arr=[b(0,105,100),b(1,99,107)]+[b(i,110,112) for i in range(2,30)]
        arr[25]=m.Candle(25*HOUR,110,110,110,110,0)
        rows=m.study_rows(arr,'BTCUSDT','1h',(24,))
        r=next(r for r in rows if r['pattern']=='bullish_engulfing')
        self.assertEqual(r['outcome_status'],'unavailable_fixed_time_fill')
        self.assertEqual(r['censor_reason'],'zero_volume_exit')
        s=next(r for r in m.summarize(rows) if r['pattern']=='bullish_engulfing')
        self.assertEqual(s['evaluated'],0)
        self.assertEqual(s['right_censored'],0)
        self.assertEqual(s['paper_fill_unavailable'],1)
        self.assertIsNone(s['net_positive_percent_paper'])

if __name__ == '__main__':unittest.main()
