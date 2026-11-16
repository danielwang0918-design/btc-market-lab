"""Stage-1 research risk ledger unit tests. These are NOT simulated exchange orders."""
import tempfile
import unittest
import warnings
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
import p1_05_stage1_audit as p

T=datetime(2024,1,1,tzinfo=timezone.utc)
def fixture(symbol='BTCUSDT',side='long',h=1,r='-1',tag='a',signal=None):
    e=T+timedelta(hours=h)
    return {'symbol':symbol,'split':'validation','setup_id':symbol+':'+tag,'direction':side,'signal_time_utc':(signal or T).isoformat(),
            'entry_time_utc':e.isoformat(),'exit_time_utc':(e+timedelta(hours=1)).isoformat(),
            'exit_reason':'stop' if Decimal(r)<0 else 'target','r_multiple':r,'bars_held':'1'}
def row(**kw):
    return p.validate_trade(fixture(**kw))

class P105Stage1Tests(unittest.TestCase):
    def test_reject_naive_date(self):
        with self.assertRaises(ValueError):p.parse_time('2024-01-01T02:00:00')
    def test_normalize_timezones(self):
        self.assertEqual(p.parse_time('2024-01-01T08:00:00+08:00'),T)
    def test_reject_not_finite_r(self):
        for bad in ('NaN','Infinity','-Infinity'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):p.parse_r(bad)
    def test_reject_over_one_r_loss(self):
        with self.assertRaises(ValueError):p.parse_r('-1.00000001')
    def test_r_zero_is_allowed(self):
        self.assertEqual(p.parse_r('0'),0)
    def test_reject_reverse_signal(self):
        x=fixture();x['signal_time_utc']=(T+timedelta(hours=4)).isoformat()
        with self.assertRaises(ValueError):p.validate_trade(x)
    def test_reject_entry_after_exit(self):
        x=fixture();x['exit_time_utc']=x['entry_time_utc']
        with self.assertRaises(ValueError):p.validate_trade(x)
    def test_reject_unknown_frame_extra_column(self):
        x=fixture();x['foo']='x'
        with self.assertRaises(ValueError):p.validate_trade(x)
    def test_reject_wrong_split(self):
        x=fixture();x['split']='test'
        with self.assertRaises(ValueError):p.validate_trade(x)
    def test_reject_unknown_symbol(self):
        x=fixture();x['symbol']='FAKEUSDT';x['setup_id']='FAKEUSDT:a'
        with self.assertRaises(ValueError):p.validate_trade(x)
    def test_reject_spot_short_legacy_mismatch(self):
        x=fixture();x['direction']='invalid'
        with self.assertRaises(ValueError):p.validate_trade(x)
    def test_reject_bad_identity(self):
        x=fixture();x['setup_id']='XYZ'
        with self.assertRaises(ValueError):p.validate_trade(x)
    def test_one_2pct_stop(self):
        accepted,decisions,s=p.replay([row()],max_trades=1)
        self.assertEqual(accepted[0]['equity_after'],Decimal('980'))
        self.assertEqual(accepted[0]['risk_budget'],Decimal('20'))
        self.assertEqual(s['wins'],0)
    def test_compound_risk_current_balance(self):
        r1=row(h=1,tag='1');r2=row(h=3,tag='2')
        acc,_,s=p.replay([r1,r2],max_trades=2)
        self.assertEqual(acc[1]['risk_budget'],Decimal('19.60'))
        self.assertEqual(acc[1]['equity_after'],Decimal('960.40'))
        self.assertEqual(s['accepted_trades'],2)
    def test_winner_2r_and_pf(self):
        a=row(h=1,r='2',tag='1');b=row(h=3,r='-1',tag='2')
        _,_,s=p.replay([a,b],max_trades=2)
        self.assertEqual(s['win_rate_pct'],50)
        self.assertEqual(s['mean_net_r'],0.5)
        self.assertEqual(s['profit_factor_net_r'],2)
        self.assertAlmostEqual(s['final_equity_usdt'],1019.2)
    def test_conflicts_skipped(self):
        a=row(symbol='BTCUSDT',h=1,tag='1')
        b=row(symbol='ETHUSDT',h=1,tag='2')
        acc,dec,s=p.replay([b,a],max_trades=2)
        self.assertEqual(acc[0]['symbol'],'BTCUSDT')
        self.assertEqual(s['rejection_reasons']['position_open'],1)
    def test_causal_tie_sort_ignores_outcome_and_exit(self):
        a=row(symbol='BTCUSDT',h=1,r='-1',tag='one')
        b=row(symbol='ETHUSDT',h=1,r='3',tag='two')
        aa,_,_=p.replay([a,b],max_trades=1)
        self.assertEqual(aa[0]['symbol'],'BTCUSDT')
        b['_exit']=a['_exit']+timedelta(days=2)
        aa,_,_=p.replay([b,a],max_trades=1)
        self.assertEqual(aa[0]['symbol'],'BTCUSDT')
    def test_entry_at_exact_prior_exit_allowed(self):
        a=row(h=1,tag='one');b=row(h=2,tag='two')
        acc,_,_=p.replay([a,b],max_trades=2)
        self.assertEqual(len(acc),2)
    def test_spot_only_excludes_short_no_claim_live(self):
        a=row(h=1,side='short',tag='s');b=row(h=2,side='long',tag='l')
        acc,ds,s=p.replay([a,b],max_trades=3,spot_long_only=True)
        self.assertEqual([x['direction'] for x in acc],['long'])
        self.assertEqual(s['rejection_reasons']['spot_short_unavailable'],1)
    def test_trade_cap_reason(self):
        a=row(h=1,tag='one');b=row(h=3,tag='two')
        acc,ds,s=p.replay([a,b],max_trades=1)
        self.assertEqual(len(acc),1)
        self.assertEqual(s['rejection_reasons']['trade_cap'],1)
    def test_flat_r_not_a_win(self):
        acc,_,s=p.replay([row(r='0')],max_trades=1)
        self.assertEqual(s['flat'],1)
        self.assertEqual(s['win_rate_pct'],0)
    def test_reject_zero_trades_requested(self):
        with self.assertRaises(ValueError):p.replay([],max_trades=0)
    def test_empty_source_summary_not_infinite_winrate(self):
        acc,ds,s=p.replay([],max_trades=1)
        self.assertIsNone(s['win_rate_pct']);self.assertEqual(s['final_equity_usdt'],1000)
    def test_archive_duplicate_names_refused(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'a.zip'
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning)
                with ZipFile(f,'w',ZIP_DEFLATED) as z:
                    z.writestr(p.TRADES_MEMBER,b'x');z.writestr(p.TRADES_MEMBER,b'y');z.writestr(p.BASELINE_MEMBER,b'{}')
            with self.assertRaisesRegex(ValueError,'Duplicate ZIP'):
                p.load_frozen(f)
    def test_archive_modified_refused(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'a.zip'
            with ZipFile(f,'w',ZIP_DEFLATED) as z:
                z.writestr(p.TRADES_MEMBER,b'evil');z.writestr(p.BASELINE_MEMBER,b'{}')
            with self.assertRaisesRegex(ValueError,'SHA256'):
                p.load_frozen(f)
    def test_summary_rejects_future_lookahead_by_time(self):
        bad=fixture();bad['entry_time_utc']=('2024-01-02T12:00:00+00:00')
        with self.assertRaises(ValueError):p.validate_trade(bad)

if __name__=='__main__':unittest.main()