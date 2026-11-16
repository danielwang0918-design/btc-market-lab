import unittest
from decimal import Decimal as D
import p1_portfolio as money
import p1_08a_study as m


def account():
    start=money.START;end=start+money.H;e=D(100);x=D(101);s=D(95);q=D(1)
    ef=money.fill(e,'long');xf=money.fill(x,'long',False);risk=-money.net_per_unit(e,s,'long')
    fee=(ef+xf)*D('.001');net=money.net_per_unit(e,x,'long')
    t=dict(event_id='a',symbol='BTCUSDT',side='long',entry_time_ms=str(start),exit_time_ms=str(end),reserved_until_ms=str(end),
        feature_cutoff_ms=str(start),signal_time_ms=str(start),raw_entry=str(e),raw_exit=str(x),stop=str(s),target='110',
        quantity='1',entry_fill=str(ef),exit_fill=str(xf),fees_usdt=str(fee),slippage_usdt=str(x-e-fee-net),net_pnl_usdt=str(net),
        planned_risk=str(risk),risk_budget='20',cash_required=str(ef*D('1.001')),equity_before='1000',equity_after=str(1000+net),
        r_multiple=str(net/risk),exit_reason='max_hold_72h',scenario='source_stop_first')
    curve=[dict(time_ms=str(start),equity_usdt='1000',cash_usdt='1000'),dict(time_ms=str(end),equity_usdt=str(1000+net),cash_usdt=str(1000+net))]
    summary=dict(model='fractional_spot',trade_count='1',wins='1',losses='0',final_equity_USDT=str(1000+net),maximum_drawdown_pct='0',
        calendar_start_utc=money.utc(start),calendar_end_utc=money.utc(end))
    return summary,[t],curve,[dict(event_id='a',status='entered')]


class ArchiveTests(unittest.TestCase):
    def test_account_is_audited_without_any_strategy_replay(self):
        self.assertTrue(m.audit_account(*account())['verified'])

    def test_tampered_final_equity_fails(self):
        args=account();args[0]['final_equity_USDT']='999'
        with self.assertRaises(ValueError):m.audit_account(*args)

    def test_duplicate_entered_decision_is_rejected(self):
        args=account();args[3].append(dict(args[3][0]))
        with self.assertRaises(ValueError):m.audit_account(*args)

    def test_excess_cash_is_rejected(self):
        args=account();args[1][0]['cash_required']='1001'
        with self.assertRaises(ValueError):m.audit_account(*args)

    def test_excess_risk_is_rejected(self):
        args=account();args[1][0]['planned_risk']='21'
        with self.assertRaises(ValueError):m.audit_account(*args)

    def test_future_feature_cutoff_is_rejected(self):
        args=account();args[1][0]['feature_cutoff_ms']=str(money.START+1)
        with self.assertRaises(ValueError):m.audit_account(*args)

    def test_coordinated_understated_risk_and_R_cannot_pass_budget_audit(self):
        args=account();t=args[1][0]
        t['planned_risk']=str(D(t['planned_risk'])/2)
        t['r_multiple']=str(D(t['net_pnl_usdt'])/D(t['planned_risk']))
        with self.assertRaises(ValueError):m.audit_account(*args)

    def test_understated_reservation_cannot_pass_cash_audit(self):
        args=account();args[1][0]['cash_required']='1'
        with self.assertRaises(ValueError):m.audit_account(*args)

    def test_spot_short_is_never_silently_merged(self):
        args=account();args[1][0]['side']='short'
        with self.assertRaises(ValueError):m.audit_account(*args)

    def test_missing_opportunity_feature_is_not_forward_filled(self):
        with self.assertRaises(KeyError):m.annotate(dict(symbol='BTCUSDT',entry_time_ms=str(money.START)),{})


if __name__=='__main__':unittest.main()
