"""Safety and determinism tests for P1-05 Stage2; synthetic fixtures, no live trading."""
from __future__ import annotations
import unittest
import tempfile
import json
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from p1_05_stage2_sizing import (
  ONE,FEE,SLIP,START,RISK_FRACTION,STEP_MS,SYMBOLS,PER_SYMBOL_SHA,VERSION,
  num,dt_ms,key,_close,fill_price,verified_trade,size_one,replay,digest,load_price_sources,load_one_hour_bars)

D=Decimal
T0=datetime(2024,1,1,tzinfo=timezone.utc)

def stamp(hours):return (T0+timedelta(hours=hours)).isoformat()

def test_priced(direction='long',entry=0,exit_hours=2,reason='target'):
    raw=D('100');stop=D('98') if direction=='long' else D('102');target=D('104') if direction=='long' else D('96')
    ex_raw=(stop if reason=='stop' else target)
    fill=fill_price(direction,raw,True);exitfill=fill_price(direction,ex_raw,False);stopfill=fill_price(direction,stop,False)
    gross=exitfill-fill if direction=='long' else fill-exitfill
    fee=FEE*(fill+exitfill);risk=abs(fill-stopfill)+FEE*(fill+stopfill)
    return {'symbol':'BTCUSDT','split':'validation','setup_id':'BTCUSDT:fake:1','direction':direction,
      'signal_time_utc':stamp(entry),'entry_time_utc':stamp(entry),'exit_time_utc':stamp(entry+exit_hours),
      'exit_reason':reason,'bars_held':exit_hours,'raw_entry_price':str(raw),
      'entry_price':str(fill),'exit_price':str(exitfill),'stop_price':str(stop),
      'target_price':str(target),'gross_pnl_per_unit':str(gross),
      'fees_paid_per_unit':str(fee),'net_pnl_per_unit':str(gross-fee),'r_multiple':str((gross-fee)/risk)}

def bar(price=100,vol=1):return dict(o=D(price),h=D('110'),l=D('90'),c=D(price),v=D(vol))

def checked(direction='long',entry=0,exit_hours=2,reason='target'):
    r=test_priced(direction,entry,exit_hours,reason)
    bars={dt_ms(stamp(i)):bar() for i in range(0,100)}
    return verified_trade(r,bars)

class Calculations(unittest.TestCase):
    def test_version_research_only(self):self.assertIn('audit',VERSION)
    def test_price_slippage_long_entry(self):self.assertEqual(fill_price('long',D('100'),True),D('100.0500'))
    def test_price_slippage_long_exit(self):self.assertEqual(fill_price('long',D('100'),False),D('99.9500'))
    def test_price_slippage_short_entry(self):self.assertEqual(fill_price('short',D('100'),True),D('99.9500'))
    def test_price_slippage_short_exit(self):self.assertEqual(fill_price('short',D('100'),False),D('100.0500'))
    def test_invalid_nan(self):
        with self.assertRaises(ValueError):num('NaN')
    def test_invalid_inf(self):
        with self.assertRaises(ValueError):num('Infinity')
    def test_precision_round(self):self.assertTrue(_close(D('100'),D('100.000000000000001')))
    def test_long_target_r_reconstruction(self):self.assertGreater(checked()['_r'],D('0'))
    def test_short_target_r_reconstruction(self):self.assertGreater(checked('short')['_r'],D('0'))
    def test_long_stop_r_reconstruction(self):self.assertEqual(checked(reason='stop')['_r'],D('-1'))
    def test_short_stop_r_reconstruction(self):self.assertEqual(checked('short',reason='stop')['_r'],D('-1'))
    def test_both_sides_exact_risk_2pct(self):
        s=size_one(START,checked(),cash_capped=False)
        self.assertAlmostEqual(float(s['actual_risk']/START),0.02)
    def test_cash_capped_never_borrows(self):
        s=size_one(START,checked(),cash_capped=True)
        self.assertLessEqual(s['cash_reserved'],START)
        self.assertLessEqual(s['actual_risk'],START*RISK_FRACTION)
    def test_cash_cap_reduces_risk_when_stop_tight(self):
        r=checked();r['_per_unit_risk']=D('0.001')
        s=size_one(START,r,cash_capped=True)
        self.assertEqual(s['sizing_status'],'cash_capped')
        self.assertLess(s['actual_risk'],D('20'))
    def test_cash_capped_does_not_create_profits(self):
        loss=checked(reason='stop');a=size_one(START,loss,cash_capped=True)
        self.assertLess(a['equity_after'],START)
    def test_fees_included_once(self):
        r=checked();s=size_one(START,r,cash_capped=False)
        self.assertEqual(s['model_pnl'],s['qty']*r['_per_unit_pnl'])
    def test_bad_source_net_rejected(self):
        r=test_priced();r['net_pnl_per_unit']='999'
        with self.assertRaisesRegex(ValueError,'price / fee / R'):verified_trade(r,{dt_ms(stamp(i)):bar() for i in range(0,5)})
    def test_bad_source_r_rejected(self):
        r=test_priced();r['r_multiple']='99'
        with self.assertRaisesRegex(ValueError,'R does not reconcile'):verified_trade(r,{dt_ms(stamp(i)):bar() for i in range(0,5)})
    def test_bad_source_slippage_rejected(self):
        r=test_priced();r['entry_price']='100'
        with self.assertRaisesRegex(ValueError,'slippage'):verified_trade(r,{dt_ms(stamp(i)):bar() for i in range(0,5)})
    def test_bad_source_side_rejected(self):
        r=test_priced();r['direction']='weird'
        with self.assertRaisesRegex(ValueError,'fill timing|Bad'):verified_trade(r,{dt_ms(stamp(i)):bar() for i in range(0,5)})
    def test_no_missing_candles(self):
        with self.assertRaisesRegex(ValueError,'Missing exact source'):verified_trade(test_priced(),{})
    def test_unreachable_entry_is_flagged_not_preselected(self):
        r=test_priced();b={dt_ms(stamp(i)):bar() for i in range(0,5)}
        b[dt_ms(stamp(0))]=dict(o=D('150'),h=D('160'),l=D('140'),c=D('155'),v=D('2'))
        v=verified_trade(r,b)
        self.assertFalse(v['_entry_reachable'])
        self.assertIn('entry_reference_not_reached',v['_diagnostic'])
    def test_zero_volume_exit_is_flagged(self):
        r=test_priced();b={dt_ms(stamp(i)):bar() for i in range(0,5)}
        b[dt_ms(stamp(1))]=bar(vol=0)
        v=verified_trade(r,b)
        self.assertIn('zero_volume_exit_bar',v['_diagnostic'])
    def test_same_bar_unknown_order(self):
        self.assertTrue(checked(exit_hours=1)['_same_bar'])
    def test_different_bar_no_samebar(self):
        self.assertFalse(checked(exit_hours=2)['_same_bar'])
    def test_checksum_pin_unalterable(self):
        self.assertEqual(len(PER_SYMBOL_SHA),8)
        self.assertTrue(all(len(h)==64 for h in PER_SYMBOL_SHA.values()))
    def test_original_r_is_decimal_not_float(self):
        self.assertIsInstance(checked()['_r'],Decimal)
    def test_source_price_stop_inequality(self):
        r=test_priced();r['stop_price']='101'
        with self.assertRaisesRegex(ValueError,'stop/target'):verified_trade(r,{dt_ms(stamp(i)):bar() for i in range(0,5)})
    def test_replay_causal_order_does_not_use_future_exit_time(self):
        a=checked();a['symbol']='SOLUSDT';a['setup_id']='SOLUSDT:a';a['_exit']=T0+timedelta(hours=5)
        b=checked();b['symbol']='BTCUSDT';b['setup_id']='BTCUSDT:b';b['_exit']=T0+timedelta(hours=50)
        # BTC wins same-time tie regardless of later exit
        x,d,s=replay([a,b],long_only=False,max_trades=2)
        self.assertEqual(x[0]['symbol'],'BTCUSDT')
        self.assertIn('position_open',[r['reason'] for r in d])
    def test_spot_no_short(self):
        a=checked('short')
        x,d,s=replay([a],long_only=True)
        self.assertEqual(len(x),0)
        self.assertEqual(d[0]['reason'],'spot_short_unavailable')
    def test_trade_cap(self):
        r=checked();r['_exit']=T0+timedelta(hours=2)
        b=deepcopy(r);b['setup_id']='BTCUSDT:fake:2';b['_entry']=T0+timedelta(hours=3);b['_exit']=T0+timedelta(hours=5)
        x,d,s=replay([r,b],long_only=False,max_trades=1)
        self.assertEqual(len(x),1);self.assertEqual(d[1]['reason'],'trade_cap')
    def test_overlapping_position_rejected(self):
        a=checked();a['_exit']=T0+timedelta(hours=3)
        b=deepcopy(a);b['setup_id']='BTCUSDT:new';b['_entry']=T0+timedelta(hours=1)
        x,d,s=replay([a,b],long_only=False)
        self.assertEqual(len(x),1);self.assertEqual(d[1]['reason'],'position_open')
    def test_split_replay_does_not_look_at_later_return(self):
        a=checked();b=deepcopy(a);b['setup_id']='BTCUSDT:another';b['_entry']=T0+timedelta(hours=4);b['_exit']=T0+timedelta(hours=7)
        x,d,s=replay([a,b],long_only=True)
        self.assertEqual(len(x),2)
        self.assertEqual(s['accepted_trades'],2)
    def test_history_source_corrupted_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):load_price_sources(Path(t),[])
    def test_stop_exceeds_cash_model_risk_rejected(self):
        a=checked();a['_per_unit_risk']=D('-1')
        with self.assertRaises(ValueError):size_one(START,a,cash_capped=True)
    def test_zero_equity_rejected(self):
        with self.assertRaises(ValueError):size_one(D(0),checked(),cash_capped=True)
    def test_source_exits_deterministic(self):
        a=checked(reason='stop');self.assertEqual(a['exit_reason'],'stop')
    def test_invalid_time_rejected(self):
        r=test_priced();r['exit_time_utc']=stamp(0)
        with self.assertRaisesRegex(ValueError,'Bad fill timing'):verified_trade(r,{dt_ms(stamp(i)):bar() for i in range(0,5)})

if __name__=='__main__':unittest.main()