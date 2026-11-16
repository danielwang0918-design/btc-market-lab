"""Network-free regression tests for frozen 1H+ canceled-OB observations."""
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone

import order_state_post_cancel as m


def hr(i):
    return i*m.HOUR


def candle(i, op=105, hi=108, lo=101, close=106, *, symbol='BTCUSDT'):
    return {'symbol':symbol,'interval':'1h','is_closed':'true','open_time':str(hr(i)),
            'close_time':str(hr(i+1)-1),'open':str(op),'high':str(hi),
            'low':str(lo),'close':str(close)}


class P0FollowupTests(unittest.TestCase):
    def setUp(self):
        self.times=[hr(i) for i in range(20)]
        self.bars=[{'o':105.,'h':108.,'l':101.,'c':106.} for _ in self.times]

    def future(self, **kwargs):
        return m.follow_up(hr(0),0,self.times,self.bars,95,100,'long',[],hr(20),horizons=(3,))['h3']

    def test_only_1h_and_above(self):
        self.assertEqual(m.TIMEFRAMES,('1h','4h','1d'))
        self.assertEqual(m.HORIZONS,(24,72,168))

    def test_closed_hour_validation(self):
        ts,vals=m.clean_closed_hours([candle(0),candle(1)],'BTCUSDT')
        self.assertEqual(ts,[hr(0),hr(1)])
        self.assertEqual(vals[0]['c'],106)

    def test_rejects_15m_or_unclosed_source(self):
        c=candle(1);c['interval']='15m'
        with self.assertRaises(ValueError):m.clean_closed_hours([c],'BTCUSDT')
        c['interval']='1h';c['is_closed']='false'
        with self.assertRaises(ValueError):m.clean_closed_hours([c],'BTCUSDT')

    def test_rejects_duplicate_or_invalid_candle(self):
        with self.assertRaises(ValueError):m.clean_closed_hours([candle(0),candle(0)],'BTCUSDT')
        with self.assertRaises(ValueError):m.clean_closed_hours([candle(0,hi=104)],'BTCUSDT')

    def test_causal_bos_confirmation_not_before_swing_confirmed(self):
        closes=[5,5,5,5,5,5,5,5]
        highs=[10,10,20,11,10,19,21,22]
        bars=[{'o':5,'h':h,'l':1,'c':closes[i]} for i,h in enumerate(highs)]
        bars[6]['c']=21
        t=[hr(i) for i in range(len(bars))]
        events=m.confirmed_one_hour_bos(t,bars)
        bullish=[x for x in events if x['direction']=='bullish']
        self.assertEqual([x['asof_ms'] for x in bullish],[hr(7)])
        self.assertEqual(bullish[0]['broken_swing'],20)

    def test_no_pivot_when_tied_highs(self):
        b=[{'o':5.,'h':10.,'l':1.,'c':5.} for _ in range(8)]
        b[-1]['c']=11
        self.assertEqual(m.confirmed_one_hour_bos([hr(i) for i in range(8)],b),[])

    def test_gap_resets_bos_state(self):
        b=[{'o':5,'h':h,'l':1,'c':5} for h in [10,10,20,11,10,19,21]]
        b[-1]['c']=21
        t=[hr(i) for i in range(5)]+[hr(10),hr(11)]
        self.assertEqual(m.confirmed_one_hour_bos(t,b),[])

    def test_no_future_before_cancel_candle_close(self):
        self.bars[0]['l']=95
        q=self.future()
        self.assertFalse(q['touch_ob'])

    def test_ob_touch_uses_future_hour_wick_only(self):
        self.bars[2]['l']=100
        q=self.future()
        self.assertTrue(q['touch_ob'])
        self.assertFalse(q['close_inside_ob'])
        self.assertEqual(q['first_touch_utc'],m.utc(hr(2)))

    def test_close_inside_ob_is_distinct_from_wick_touch(self):
        self.bars[2].update({'l':96.,'c':98.})
        q=self.future()
        self.assertTrue(q['touch_ob'])
        self.assertTrue(q['close_inside_ob'])

    def test_same_side_1h_bos_only_after_cancel(self):
        bos=[{'asof_ms':hr(1),'direction':'bullish'},
             {'asof_ms':hr(2),'direction':'bearish'},
             {'asof_ms':hr(3),'direction':'bullish'}]
        out=m.follow_up(hr(0),0,self.times,self.bars,95,100,'long',bos,hr(20),horizons=(3,))['h3']
        self.assertTrue(out['same_side_new_1h_bos'])
        self.assertEqual(out['first_same_side_bos_utc'],m.utc(hr(3)))

    def test_opposite_bos_not_counted_as_original_recovery(self):
        bos=[{'asof_ms':hr(2),'direction':'bearish'}]
        out=m.follow_up(hr(0),0,self.times,self.bars,95,100,'long',bos,hr(20),horizons=(3,))['h3']
        self.assertFalse(out['same_side_new_1h_bos'])

    def test_ob_then_same_side_bos_in_later_bar(self):
        self.bars[1]['l']=97
        bos=[{'asof_ms':hr(3),'direction':'bullish'}]
        out=m.follow_up(hr(0),0,self.times,self.bars,95,100,'long',bos,hr(20),horizons=(3,))['h3']
        self.assertEqual(out['sequence'],'ob_bar_closed_before_bos_confirmed')

    def test_ob_and_bos_confirmation_same_hour_unresolved(self):
        self.bars[2]['l']=97
        bos=[{'asof_ms':hr(3),'direction':'bullish'}]
        out=m.follow_up(hr(0),0,self.times,self.bars,95,100,'long',bos,hr(20),horizons=(3,))['h3']
        self.assertEqual(out['sequence'],'same_hour_order_unresolved')

    def test_bos_before_ob(self):
        self.bars[2]['l']=97
        bos=[{'asof_ms':hr(2),'direction':'bullish'}]
        out=m.follow_up(hr(0),0,self.times,self.bars,95,100,'long',bos,hr(20),horizons=(3,))['h3']
        self.assertEqual(out['sequence'],'bos_confirmed_before_ob_bar_open')

    def test_censor_when_horizon_exceeds_frozen_test(self):
        out=m.follow_up(hr(0),0,self.times,self.bars,95,100,'long',[],hr(3),horizons=(3,))['h3']
        self.assertIn('censored',out['status'])
        self.assertIsNone(out['touch_ob'])

    def test_censor_when_gap_inside_window(self):
        self.times[2]=hr(4)
        out=self.future()
        self.assertIn('censored',out['status'])
        self.assertIsNone(out['signed_close_change_pct'])

    def test_reject_unauthorized_side_or_horizon(self):
        with self.assertRaises(ValueError):m.follow_up(hr(0),0,self.times,self.bars,95,100,'both',[],hr(20),horizons=(3,))
        with self.assertRaises(ValueError):m.follow_up(hr(0),0,self.times,self.bars,95,100,'long',[],hr(20),horizons=(0,))

    def test_signed_followup_price_not_a_trade_return(self):
        self.bars[3]['c']=107
        long=m.follow_up(hr(0),0,self.times,self.bars,95,100,'long',[],hr(20),horizons=(3,))['h3']['signed_close_change_pct']
        short=m.follow_up(hr(0),0,self.times,self.bars,95,100,'short',[],hr(20),horizons=(3,))['h3']['signed_close_change_pct']
        self.assertAlmostEqual(long,-short)

    def test_buckets_exclude_right_censored(self):
        complete={'followup':{'h24':{'status':'complete','touch_ob':True,'close_inside_ob':False,
            'same_side_new_1h_bos':True,'sequence':'ob_bar_closed_before_bos_confirmed','signed_close_change_pct':1.0}}}
        cutoff={'followup':{'h24':{'status':'right_censored_missing_contiguous_test_followup',
            'touch_ob':None}}}
        x=m._bucket([complete,cutoff],24)
        self.assertEqual((x['total_cases'],x['complete_followup'],x['right_censored']),(2,1,1))
        self.assertEqual(x['ob_wick_touch_pct_of_complete'],100)

    def test_aggregate_rejects_missing_complete_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):m.aggregate(Path(tmp),Path(tmp))


if __name__=='__main__':unittest.main()