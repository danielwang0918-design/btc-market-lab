import unittest
from decimal import Decimal as D
import p1_portfolio as m


class MoneyTests(unittest.TestCase):
    def test_sizing_uses_stop_distance_and_both_sides_costs(self):
        a=m.size_order(D(1000),D(100),D(90),'long')
        b=m.size_order(D(1000),D(100),D(99),'long')
        self.assertEqual(a['risk_budget'],D(20))
        self.assertGreater(b['quantity'],a['quantity'])
        self.assertLessEqual(a['planned_risk'],D(20))
        self.assertLessEqual(b['cash_required'],D(1000))

    def test_cash_cap_cannot_borrow_to_reach_two_percent(self):
        s=m.size_order(D(1000),D(100),D('99.99'),'long')
        self.assertLessEqual(s['cash_required'],D(1000))
        self.assertLess(s['planned_risk'],D(20))

    def test_lot_rounding_and_minimum_notional(self):
        rules={'step':'1','min_qty':'1','min_notional':'500'}
        a=m.size_order(D(1000),D(100),D(90),'long',rules=rules)
        self.assertEqual(a['status'],'below_exchange_minimum')

    def test_missing_historical_rules_reject_strict_order(self):
        self.assertEqual(m.size_order(D(1000),D(100),D(90),'long',strict=True)['status'],'historical_filters_unavailable')

    def test_normal_stop_is_minus_one_R_and_gap_can_exceed_it(self):
        r=m.unit_risk(D(100),D(90),'long')
        self.assertEqual(m.net_per_unit(D(100),D(90),'long'),-r)
        self.assertLess(m.net_per_unit(D(100),D(85),'long')/r,D(-1))

    def test_long_and_short_cashflows_include_fees_once(self):
        self.assertLess(m.net_per_unit(D(100),D(100),'long'),0)
        self.assertLess(m.net_per_unit(D(100),D(100),'short'),0)
        self.assertGreater(m.net_per_unit(D(100),D(110),'long'),0)
        self.assertGreater(m.net_per_unit(D(100),D(90),'short'),0)

    def test_same_bar_sensitivity_and_gap_priority(self):
        self.assertEqual(m.exit_price('long',D(100),D(115),D(85),D(90),D(110),'stop_first'),(D(90),'stop'))
        self.assertEqual(m.exit_price('long',D(100),D(115),D(85),D(90),D(110),'target_first'),(D(110),'target'))
        self.assertEqual(m.exit_price('long',D(80),D(115),D(75),D(90),D(110),'target_first'),(D(80),'stop_gap'))

    def test_causal_ties_never_sort_by_exit(self):
        a={'entry_time_utc':'2020-01-01T00:00:00+00:00','symbol':'AAA','setup_id':'x','exit_time_utc':'2020-01-10T00:00:00+00:00','r_multiple':'-1'}
        b={**a,'symbol':'BBB','exit_time_utc':'2020-01-02T00:00:00+00:00','r_multiple':'2'}
        r=m.legacy_replay([b,a])
        self.assertEqual(r['trades'][0]['symbol'],'AAA')
        self.assertEqual(r['final_equity'],D(980))

    def test_legacy_R_is_already_net_no_double_cost(self):
        a={'entry_time_utc':'2020-01-01T00:00:00+00:00','symbol':'AAA','setup_id':'x','exit_time_utc':'2020-01-02T00:00:00+00:00','r_multiple':'-1'}
        self.assertEqual(m.legacy_replay([a])['final_equity'],D(980))

if __name__=='__main__':unittest.main()
