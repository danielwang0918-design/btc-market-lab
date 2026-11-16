import unittest
from decimal import Decimal as D
import p1_08b_study as m
from test_p1_08b_paths import trade,bar
import p1_08b_paths as paths


class StudyTests(unittest.TestCase):
    def test_zero_trade_account_profile_does_not_require_a_trade_side(self):
        row=dict(model='fractional_spot',period='train',scenario='source_stop_first',policy='ML_GATE')
        self.assertEqual(m.identity(row,'actual_account'),('actual_account','fractional_spot','train','source_stop_first','ML_GATE'))

    def test_same_price_path_different_equity_is_not_a_new_event(self):
        a=trade();b=dict(a,equity_before='10',quantity='2',planned_risk=str(D(a['planned_risk'])*2))
        self.assertEqual(m.path_key(a),m.path_key(b))

    def test_other_exit_scenario_has_separate_identity(self):
        a=trade();b=dict(a,scenario='feasible_upper',raw_exit='120',exit_reason='target')
        self.assertNotEqual(m.path_key(a),m.path_key(b))

    def test_incomplete_window_is_not_silently_a_zero_MFE(self):
        label=paths.analyze(trade(),[bar(0),bar(2)])
        s=m.summarize([label]);self.assertEqual(s['n_complete'],0)
        self.assertIsNone(s['mean_mfe_R0_lower'])

    def test_no_trades_metrics_remain_undefined(self):
        s=m.summarize([]);self.assertEqual(s['n'],0);self.assertIsNone(s['median_mfe_R0_lower'])

    def test_cost_coverage_needs_finite_upper_bound(self):
        label=paths.analyze(trade(raw_exit='100'),[bar(0,h=100),bar(2,h=100)])
        self.assertEqual(m.summarize([label])['definitely_cost_insufficient'],0)

    def positive_contrasts(self):
        rows=[]
        for period,universe in [('train','all_eight'),('validation','all_eight'),('continuous','all_eight'),('continuous','BTC'),('continuous','other_seven'),('opened_test','all_eight')]:
            for metric in ('mfe_R0','mfe_ATRpre','mae_R0'):
                rows.append(dict(period=period,universe=universe,metric=metric,n_a=40,n_b=40,delta=.2 if metric!='mae_R0' else -.1,low95=.1))
        return rows

    def test_formative_hypothesis_can_pass_without_claiming_profit(self):
        self.assertTrue(m.hypothesis_gate(self.positive_contrasts()))

    def test_higher_MFE_with_higher_MAE_cannot_qualify(self):
        rows=self.positive_contrasts()
        next(r for r in rows if r['period']=='validation' and r['metric']=='mae_R0')['delta']=.1
        self.assertFalse(m.hypothesis_gate(rows))

    def test_tiny_BTC_control_is_insufficient_not_success(self):
        rows=self.positive_contrasts()
        next(r for r in rows if r['universe']=='BTC' and r['metric']=='mfe_R0')['n_b']=2
        self.assertFalse(m.hypothesis_gate(rows))

    def test_opened_test_reversal_is_not_hidden_by_pooled_result(self):
        rows=self.positive_contrasts()
        next(r for r in rows if r['period']=='opened_test' and r['metric']=='mfe_R0')['delta']=-.1
        self.assertFalse(m.hypothesis_gate(rows))

    def test_missing_groups_are_not_recommended(self):
        self.assertFalse(m.hypothesis_gate([]))


if __name__=='__main__':unittest.main()
