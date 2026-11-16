import unittest
from decimal import Decimal as D
import p1_candlestick_research as core
import p1_stop_candidates as m
import p1_stop_study as study

H=3600000
def bar(t=0,o=105,h=110,l=85,c=105,v=1):
    return core.Candle(t,o,h,l,c,v)

class ReachabilityTests(unittest.TestCase):
    def test_must_cross_reference_before_lower_stop_when_open_above_buy(self):
        self.assertEqual(m.feasible_bar(bar(),D(100),D(90),D(120),'long',False),{'stop'})
    def test_low_may_precede_reference_when_open_below_buy(self):
        self.assertEqual(m.feasible_bar(bar(o=95),D(100),D(90),D(120),'long',False),{'stop','hold'})
    def test_close_below_stop_forces_stop_after_reference_even_if_low_earlier(self):
        self.assertEqual(m.feasible_bar(bar(o=95,c=88),D(100),D(90),D(120),'long',False),{'stop'})
    def test_both_barriers_from_open_position_are_both_feasible(self):
        self.assertEqual(m.feasible_bar(bar(h=125),D(100),D(90),D(120),'long',True),{'stop','target'})
    def test_no_reference_touch_is_not_a_fill(self):
        self.assertEqual(m.feasible_bar(bar(o=105,h=110,l=102,c=104),D(100),D(90),D(120),'long',False),{'unfilled'})
    def test_short_mirror(self):
        self.assertEqual(m.feasible_bar(bar(o=105,h=115,l=95,c=105),D(100),D(110),D(80),'short',False),{'stop','hold'})
    def test_impossible_OHLC_is_rejected(self):
        with self.assertRaises(ValueError):m.feasible_bar(bar(o=999),D(100),D(90),D(120),'long',False)

class MoneyTests(unittest.TestCase):
    def test_stop_widening_resizes_quantity(self):
        a=m.size_cash(D(1000),D(100),D(99),'long')
        b=m.size_cash(D(1000),D(100),D(90),'long')
        self.assertGreater(a['quantity'],b['quantity'])
        self.assertLessEqual(b['planned_risk'],D(20))
    def test_costs_and_cash_are_both_in_risk(self):
        a=m.size_cash(D(1000),D(100),D('99.99'),'long')
        self.assertLessEqual(a['cash_required'],1000)
        self.assertLess(a['planned_risk'],20)
    def test_slip15_budgets_at15_not5(self):
        a=m.size_cash(D(1000),D(100),D(90),'long',D(15))
        self.assertLessEqual(a['planned_risk'],20)
        self.assertEqual(m.net_unit(D(100),D(90),'long',D(15))*a['quantity'],-a['planned_risk'])
    def test_independent_target_is_exactly_2_net_R(self):
        for side,stop in [('long',D(95)),('short',D(105))]:
            target=m.target_for_R(D(100),stop,side,D(2))
            self.assertAlmostEqual(float(m.net_rr(D(100),stop,target,side)),2,12)
    def test_minimum_RR_gate_is_not_bypassed_after_wider_stop(self):
        self.assertLess(m.net_rr(D(100),D(90),D(110),'long'),D('1.5'))

class CausalityTests(unittest.TestCase):
    def test_pivot_right_confirmation_must_be_closed_before_entry(self):
        rows=[(0,2,99.0),(1,3,98.0)]
        times=[i*H for i in range(5)]
        self.assertEqual(m.latest_pivot(rows,times,3*H),D(99))
    def test_sweep_uses_previous20_not_current_extreme(self):
        bars=[bar(i*H,o=100,h=105,l=95,c=101) for i in range(20)]
        bars.append(bar(20*H,o=96,h=102,l=94,c=100))
        self.assertEqual(m.sweep_side(bars,20),'long')
    def test_sweep_excludes_gap_and_both_sides(self):
        bars=[bar(i*H,o=100,h=105,l=95,c=101) for i in range(20)]
        self.assertIsNone(m.sweep_side(bars+[bar(21*H,o=96,h=102,l=94,c=100)],20))
        self.assertIsNone(m.sweep_side(bars+[bar(20*H,h=106,l=94)],20))
    def test_future_candles_cannot_change_sweep_signal(self):
        bars=[bar(i*H,o=100,h=105,l=95,c=101) for i in range(20)]+[bar(20*H,o=96,h=102,l=94,c=100)]
        self.assertEqual(m.sweep_side(bars,20),m.sweep_side(bars+[bar(21*H,h=9999)],20))

class Fake:
    def __init__(self):
        self.symbol='AAA';self.times=[i*H for i in range(10)];self.by_time={t:i for i,t in enumerate(self.times)}
        self.bars=[bar(t,o=100,h=104,l=98,c=101) for t in self.times]
        self.snaps=[{'integrity_ok':True} for _ in self.times]
        self.pivot_lows=[(0,2,98.0)];self.pivot_highs=[(0,2,102.0)]
        self.stop_context_cache={t:dict(feature_cutoff_ms=t,atr=1,volatility='normal',MA_1h='bullish',MA_4h='bullish',MA_1d='bullish') for t in self.times}

def event(**kw):
    x=dict(symbol='AAA',setup_id='fixed',side='long',registration_ms=3*H,anchor_ms=3*H,event_id='AAA|fixed|3',
        anchor_status='entered',setup_confirm_ms=2*H,signal_time_ms=3*H,entry_reference='100',stop='95',target='130',zone_low='95',zone_high='100')
    x.update(kw);return x

class CandidateTests(unittest.TestCase):
    def test_close_confirmation_enters_next_open_not_confirmation_close(self):
        a=Fake();p=m.controlled_plan(event(),'E_CLOSE',a)
        self.assertEqual(p['entry_time_ms'],4*H);self.assertEqual(p['raw_entry'],100)
    def test_third_confirmation_candle_remains_eligible(self):
        a=Fake();a.bars[3]=bar(3*H,o=100,h=104,l=98,c=99);a.bars[4]=bar(4*H,o=100,h=104,l=98,c=99)
        p=m.controlled_plan(event(),'E_CLOSE',a)
        self.assertEqual(p['entry_time_ms'],6*H)
    def test_entry_bar_future_close_does_not_gate_signal(self):
        a=Fake();p=m.controlled_plan(event(),'E_CLOSE',a);a.bars[4]=bar(4*H,o=100,h=999,l=98,c=900)
        self.assertEqual(m.controlled_plan(event(),'E_CLOSE',a),p)
    def test_waiting_stop_invalidates_instead_of_selecting_later_recovery(self):
        a=Fake();a.bars[3]=bar(3*H,o=100,h=104,l=94,c=103)
        self.assertEqual(m.controlled_plan(event(),'E_CLOSE',a)['status'],'original_SL_TP_invalidated_while_waiting')
    def test_zero_volume_confirmation_is_unavailable(self):
        a=Fake();a.bars[3]=bar(3*H,o=100,h=104,l=98,c=103,v=0)
        self.assertEqual(m.controlled_plan(event(),'E_CLOSE',a)['status'],'confirmation_zero_volume')
    def test_break_and_retest_requires_two_distinct_closed_bars(self):
        a=Fake();a.bars[3]=bar(3*H,o=100,h=105,l=98,c=103);a.bars[4]=bar(4*H,o=102,h=104,l=101,c=103)
        self.assertEqual(m.controlled_plan(event(),'E_BREAK_RETEST',a)['entry_time_ms'],5*H)
    def test_ATR_stop_does_not_tighten_or_shorten_target(self):
        a=Fake();a.stop_context_cache[3*H]['atr']=10
        p=m.controlled_plan(event(),'S_ATR1',a)
        self.assertEqual(p['stop'],90);self.assertEqual(p['target'],130)
    def test_swing_stop_uses_confirmed_swing_and_buffer(self):
        a=Fake();a.pivot_lows=[(0,2,90.0)]
        self.assertEqual(m.controlled_plan(event(),'S_SWING',a)['stop'],D('89.9'))
    def test_environment_unknown_is_watch(self):
        a=Fake();a.stop_context_cache[3*H]['MA_4h']='insufficient_prior_ma'
        self.assertEqual(m.controlled_plan(event(),'F_4H',a)['status'],'4H_MA_filter_rejected')
    def test_environment_only_does_not_add_a_second_RR_gate_to_A(self):
        a=Fake();e=event(target='107')
        self.assertEqual(m.controlled_plan(e,'A',a)['status'],m.controlled_plan(e,'F_4H',a)['status'])

class AccountTests(unittest.TestCase):
    def plans(self):
        plans=[];paths={}
        for symbol in ['BBB','AAA']:
            p=dict(event(symbol=symbol,event_id=symbol),policy='A',status='eligible',registration_ms=0,entry_time_ms=0,
                entry_time_utc=core.utc(0),raw_entry=D(100),stop=D(90),target=D(120),feature_cutoff_ms=0)
            plans.append(p);x=dict(exit_time_ms=H,exit_time_utc=core.utc(H),exit_bar_ms=0,reserved_until_ms=H,raw_exit=D(90),exit_reason='stop')
            paths[symbol+'|A']=dict(source_stop_first=x,path_dependent_outcome=False,gap_or_zero_volume_path_unknown=False)
        return plans,paths
    def test_account_tie_does_not_use_future_pnl_or_exit(self):
        ps,x=self.plans();a=Fake();r=m.ledger(ps,x,'A','fractional_spot','source_stop_first',0,2*H,{'AAA':a,'BBB':a})
        self.assertEqual(r['trades'][0]['symbol'],'AAA');self.assertEqual(len(r['trades']),1)
        self.assertEqual(r['decisions'][1]['status'],'global_position_busy')
        self.assertTrue(study.audit_account(r)['cash_risk_costs_single_position_and_MTM_verified'])
    def test_spot_short_is_rejected_not_silently_counted(self):
        ps,x=self.plans()
        for p in ps:p['side']='short'
        r=m.ledger(ps,x,'A','fractional_spot','source_stop_first',0,2*H,{'AAA':Fake(),'BBB':Fake()})
        self.assertEqual(r['trades'],[]);self.assertEqual(r['final_equity'],1000)
    def test_worse_exit_risk_is_reported_as_a_gap_not_clamped(self):
        ps,x=self.plans()
        for path in x.values():path['source_stop_first']['raw_exit']=D(80);path['source_stop_first']['exit_reason']='stop_gap'
        r=m.ledger(ps,x,'A','fractional_spot','source_stop_first',0,2*H,{'AAA':Fake(),'BBB':Fake()})
        self.assertTrue(r['trades'][0]['loss_exceeds_budget'])
    def test_hypothetical_short_reserves_collateral_in_available_cash(self):
        ps,x=self.plans()
        for p in ps:p['side']='short';p['stop']=D(110);p['target']=D(80)
        for path in x.values():
            path['source_stop_first'].update(raw_exit=D(110),exit_time_ms=2*H,exit_bar_ms=H,reserved_until_ms=2*H)
        r=m.ledger(ps,x,'A','hypothetical_1x_long_short','source_stop_first',0,3*H,{'AAA':Fake(),'BBB':Fake()})
        self.assertEqual(r['curve'][1]['cash_usdt'],D(1000)-r['trades'][0]['cash_required'])
    def test_stress_RR_is_recomputed_at_actual_cost_without_changing_signal(self):
        ps,x=self.plans()
        for path in x.values():path['source_slip15']=path['source_stop_first']
        r=m.ledger(ps,x,'A','fractional_spot','source_slip15',0,2*H,{'AAA':Fake(),'BBB':Fake()})
        expected=m.net_unit(D(100),D(120),'long',D(15))/-m.net_unit(D(100),D(90),'long',D(15))
        self.assertEqual(r['trades'][0]['cost_scenario_target_RR'],expected)

if __name__=='__main__':unittest.main()
