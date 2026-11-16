"""Regression tests for 1H OB retest execution audit (NOT trading logic)."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

import entry_execution_audit as audit

STEP = 3_600_000


def candles():
    out=[]
    for i, (o,h,l,c) in enumerate([(105,108,94,97), (100,125,90,111), (105,111,97,110), (105,300,99,109)]):
        x=i*STEP
        out.append({'open_time':str(x),'close_time':str(x+STEP-1),
                    'open':str(o),'high':str(h),'low':str(l),'close':str(c)})
    return out


def trade(*, entry=0, exit_=STEP, bars=1, direction='long'):
    def utc(ms):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
    return {'setup_id':'test-setup', 'split':'test','direction':direction,
            'signal_time_utc':utc(0), 'entry_time_utc':utc(entry),
            'exit_time_utc':utc(exit_), 'raw_entry_price':'100',
            'stop_price':'95' if direction=='long' else '110',
            'target_price':'115' if direction=='long' else '90',
            'r_multiple':'-1','exit_reason':'stop','bars_held':bars}


class ExecutionAuditTests(unittest.TestCase):
    def test_entry_candle_stop_is_not_excluded_and_no_true_fill_timestamp(self):
        t=trade()
        b=candles()
        r=audit.analyze_trade(t,b,[int(x['open_time']) for x in b])
        self.assertEqual(r['signal_to_entry_bar_hours'],0)
        self.assertTrue(r['entry_bar_stop'])
        self.assertTrue(r['one_bar_stop_open_on_approach_side'])
        self.assertFalse(r['intrabar_fill_sequence_observed'])
        self.assertAlmostEqual(r['open_to_reference_abs_bps'], 500.)

    def test_exit_boundary_close_plus_one_and_held_bars(self):
        b=candles(); times=[int(x['open_time']) for x in b]
        t=trade(exit_=2*STEP,bars=2)
        r=audit.analyze_trade(t,b,times)
        self.assertEqual(r['bars_held'],2)
        self.assertFalse(r['one_bar_stop_open_on_approach_side'])
        t['bars_held']=3
        with self.assertRaises(ValueError): audit.analyze_trade(t,b,times)

    def test_delayed_retest_is_not_next_hour_open(self):
        b=candles();t=trade(entry=2*STEP,exit_=3*STEP)
        # The entry reference 100 lies inside 97..111 of 02:00 bar.
        r=audit.analyze_trade(t,b,[int(x['open_time']) for x in b])
        self.assertEqual(r['signal_to_entry_bar_hours'],2)
        self.assertEqual(r['signal_to_entry_bucket'],'2-3h')
        self.assertTrue(r['entry_reference_within_entry_bar'])

    def test_bad_timestamps_or_missing_candles_fail_closed(self):
        b=candles();times=[int(x['open_time']) for x in b]
        t=trade(entry=0,exit_=3*STEP,bars=3)
        with self.assertRaises(ValueError): audit.analyze_trade(t,b[:1]+b[2:],times[:1]+times[2:])
        t=trade();t['signal_time_utc']='1970-01-01T00:00:00'
        with self.assertRaises(ValueError): audit.analyze_trade(t,b,times)

    def test_group_metrics_not_reported_as_new_strategy(self):
        b=candles()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with (root/'BTCUSDT_1h.csv').open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(b[0]));w.writeheader();w.writerows(b)
            trade_data={'strategy':'mtf-bos-ob-retest-v1','variants':{
                'ob_1h_baseline':{'trades':[trade()]}}}
            (root/'BTCUSDT_trade_diagnostics.json').write_text(json.dumps(trade_data))
            result=audit.build_audit(root,root/'out')
            row=result['variants']['ob_1h_baseline']['test']['all']
            self.assertEqual((row['trades'],row['one_bar_stops'],row['wins']),(1,1,0))
            self.assertIn('BTCUSDT_1h.csv',result['provenance_sha256'])
            self.assertTrue((root/'out'/'BTCUSDT_entry_execution_audit.csv').is_file())


if __name__=='__main__': unittest.main()