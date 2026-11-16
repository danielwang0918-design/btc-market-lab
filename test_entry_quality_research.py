"""Causal ENTRY-QUALITY-01 unit tests using deliberately controlled mock K lines."""
import unittest
from datetime import datetime, timezone

import entry_quality_research as eq

S = 3_600_000


def bars(prices):
    result=[]
    for i,(o,h,l,c) in enumerate(prices):
        ms=i*S
        result.append({'open_time':str(ms),'close_time':str(ms+S-1),
                       'open':str(o),'high':str(h),'low':str(l),'close':str(c),
                       'volume':'10','source':'unit-test','is_closed':'true'})
    return result


def ts(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).isoformat()


def trade(entry=S, raw=100., stop=95., target=115., direction='long'):
    return {'setup_id':f'BTCUSDT:1h:order_block:bullish:0:{S-1}:100',
            'direction':direction, 'split':'test', 'signal_time_utc':ts(S),
            'entry_time_utc':ts(entry),'exit_time_utc':ts(4*S),
            'raw_entry_price':str(raw),'stop_price':str(stop),
            'target_price':str(target),'r_multiple':'-1','bars_held':2,
            'exit_reason':'stop'}


class EntryQualityTests(unittest.TestCase):
    def test_confirmation_waits_until_next_hour_open_and_fees(self):
        # Entry touch at i=1, closes bullish > reference, no stop touch.
        b=bars([(98,104,97,99),(99,103,97,102),(102,112,101,110),
                (110,118,107,114),(114,116,111,112)])
        times=[int(z['open_time']) for z in b]
        result=eq._evaluate_confirmation(trade(),b,times,5*S,'rejection_close')
        self.assertEqual(result['outcome'],'simulated_target')
        self.assertEqual(result['exit_time_ms'],4*S)
        self.assertGreater(result['r'],0)
        # The original entry time is 1h; candidate first tradable open is 2h.
        self.assertEqual(result['bars_held'],2)

    def test_do_not_enter_on_touch_if_stop_breached(self):
        b=bars([(98,104,97,99),(99,103,94,102),(102,120,101,115)])
        result=eq._evaluate_confirmation(trade(),b,[int(z['open_time']) for z in b],3*S,'rejection_close')
        self.assertEqual(result['outcome'],'rejected_stop_breached_on_touch_candle')

    def test_reject_without_favorable_close(self):
        b=bars([(98,104,97,99),(102,103,97,99),(99,110,97,100)])
        result=eq._evaluate_confirmation(trade(),b,[int(z['open_time']) for z in b],3*S,'rejection_close')
        self.assertEqual(result['outcome'],'rejected_no_confirmation')

    def test_close_above_prior_extreme_is_stronger(self):
        b=bars([(98,106,97,99),(99,103,97,102),(102,112,101,110),(110,118,107,114)])
        times=[int(x['open_time']) for x in b]
        self.assertEqual(eq._evaluate_confirmation(trade(),b,times,4*S,'rejection_above_prior_extreme')['outcome'], 'rejected_no_confirmation')
        self.assertEqual(eq._evaluate_confirmation(trade(),b,times,4*S,'rejection_close')['outcome'],'simulated_target')

    def test_stop_first_when_both_hit_after_confirmed_entry(self):
        b=bars([(98,104,97,99),(99,104,97,102),(102,120,90,100)])
        result=eq._evaluate_confirmation(trade(),b,[int(x['open_time']) for x in b],3*S,'rejection_close')
        self.assertEqual(result['outcome'],'simulated_stop')
        self.assertAlmostEqual(result['r'],-1.0)

    def test_causality_of_ob_features_and_touch_episodes(self):
        b=bars([(100,110,90,99)]*16 + [(100,105,95,101),(100,110,90,100)])
        times=[int(x['open_time']) for x in b]
        t=trade(entry=17*S)
        t['signal_time_utc']=ts(17*S)
        t['setup_id']=f'BTCUSDT:1h:order_block:bullish:0:{S-1}:100'
        orig=eq._info_setup(t,dict(zip(times,b)),b,times,eq._atr14(b))
        self.assertEqual(orig['ob_confirmed_age_at_signal_hours'],16)
        self.assertTrue(orig['signal_was_closed'])
        b[-1]['high']='999999'  # future observation must not affect signal as-of features
        again=eq._info_setup(t,dict(zip(times,b)),b,times,eq._atr14(b))
        self.assertEqual(orig,again)

    def test_gap_resets_atr_and_cancels_next_open(self):
        b=bars([(99,104,97,100)]*20)
        b[2]['open_time']=str(3*S)
        # synthetic gap not globally ordered; test the direct ATR reset on a sorted missing-interval list
        b=bars([(99,104,97,100)]*14)
        b += bars([(99,104,97,100)]*4)
        for j in range(14,18):
            b[j]['open_time']=str((j+1)*S)
        atr=eq._atr14(b)
        self.assertIsNotNone(atr[13]);self.assertIsNone(atr[14]);self.assertIsNone(atr[-1])
        b=bars([(98,104,97,99),(99,103,97,102),(102,112,101,110)])
        b[2]['open_time']=str(3*S)
        self.assertEqual(eq._evaluate_confirmation(trade(),b,[int(x['open_time']) for x in b],4*S,'rejection_close')['outcome'],'no_next_contiguous_bar')

    def test_lower_rr_next_open_is_rejected(self):
        b=bars([(98,104,97,99),(99,103,97,102),(114,116,110,114)])
        result=eq._evaluate_confirmation(trade(),b,[int(x['open_time']) for x in b],3*S,'rejection_close')
        self.assertEqual(result['outcome'],'rejected_net_rr_under_1_5')

    def test_costed_r_stop_minus_one(self):
        self.assertAlmostEqual(eq._costed_r('long',100,95,95),-1)
        self.assertAlmostEqual(eq._costed_r('short',100,105,105),-1)
        self.assertGreater(eq._costed_r('long',100,115,95),1.5)

    def test_end_of_split_marks_trade_at_known_close(self):
        b=bars([(98,104,97,99),(99,103,97,102),(102,103,100,101),(101,104,99,102),
                (102,106,101,104)])
        result=eq._evaluate_confirmation(trade(),b,[int(x['open_time']) for x in b],4*S,'rejection_close')
        self.assertEqual(result['outcome'],'simulated_split_end_exit')
        self.assertEqual(result['exit_time_ms'],4*S)

if __name__=='__main__':
    unittest.main()