"""Deterministic regression checks for P1-01 closed-bar pattern research."""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

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
        s=m.summarize(rows)
        self.assertEqual(s[0]['observed_signals'],1)
        self.assertEqual(s[0]['net_positive_percent_paper'],0)
        self.assertEqual(s[0]['directional_positive_percent'],100)
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

if __name__ == '__main__':unittest.main()