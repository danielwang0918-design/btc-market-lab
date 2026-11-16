import unittest
from decimal import Decimal as D
import p1_candlestick_research as core
import p1_stop_candidates as m

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

if __name__=='__main__':unittest.main()
