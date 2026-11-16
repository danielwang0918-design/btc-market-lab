import copy,unittest
from decimal import Decimal as D
import p1_portfolio as m
import p1_portfolio_study as study
from test_p1_portfolio import FakeAsset

class AccountAuditTests(unittest.TestCase):
    def result(self):return m.run_account('A','fractional_spot',0,4*m.H,{'AAA':FakeAsset()})
    def test_independent_cash_audit_accepts_reconciled_account(self):
        self.assertEqual(study.audit_run(self.result(),'fractional_spot')['trades_checked'],1)
    def test_tampered_quantity_fee_or_equity_fails(self):
        for field in ('quantity','fees_usdt','equity_after'):
            r=self.result();r['trades'][0][field]+=D(1)
            with self.subTest(field=field),self.assertRaises(ValueError):study.audit_run(r,'fractional_spot')
    def test_future_feature_timestamp_fails(self):
        r=self.result();r['trades'][0]['feature_cutoff_ms']+=m.H
        with self.assertRaisesRegex(ValueError,'future'):study.audit_run(r,'fractional_spot')
    def test_overlapping_positions_fail(self):
        r=self.result();r['trades'].append(copy.deepcopy(r['trades'][0]))
        with self.assertRaisesRegex(ValueError,'Overlap'):study.audit_run(r,'fractional_spot')
    def test_PF_uses_USDT_pnl_not_unweighted_R(self):
        rows=[dict(net_pnl_usdt=D(1),r_multiple=D(1),fees_usdt=D('.1'),slippage_usdt=D('.05'),loss_exceeds_budget=False,cash_capped=False),
              dict(net_pnl_usdt=D(-2),r_multiple=D('-.5'),fees_usdt=D('.1'),slippage_usdt=D('.05'),loss_exceeds_budget=False,cash_capped=False)]
        r=study.metrics(rows)
        self.assertEqual(r['profit_factor'],.5)
        self.assertEqual(r['net_expectancy_USDT'],-.5)
        self.assertEqual(r['net_expectancy_R'],.25)
    def test_zero_trades_do_not_claim_zero_percent_win_rate(self):
        r=study.metrics([])
        self.assertIsNone(r['win_rate_pct']);self.assertIsNone(r['average_R'])
        self.assertIsNone(r['profit_factor'])
    def test_wrong_planned_stop_risk_cannot_pass_cash_only_reconciliation(self):
        r=self.result();r['trades'][0]['planned_risk']-=D(1)
        with self.assertRaisesRegex(ValueError,'risk'):study.audit_run(r,'fractional_spot')
    def test_wrong_R_or_slippage_cannot_pass_matching_final_equity(self):
        for field in ('r_multiple','slippage_usdt'):
            r=self.result();r['trades'][0][field]+=D(1)
            with self.subTest(field=field),self.assertRaises(ValueError):study.audit_run(r,'fractional_spot')
if __name__=='__main__':unittest.main()
