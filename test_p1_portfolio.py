import unittest
from decimal import Decimal as D
import p1_portfolio as m
import p1_candlestick_research as core
import p1_market_regime as regime
import p1_zone_context as zone
import p1_volume_rvol as vol
import math

class FakeAsset:
    def __init__(self,times=None,volumes=None):
        times=times or [0,m.H,2*m.H,3*m.H]
        volumes=volumes or [1]*len(times)
        self.times=times;self.by_time={t:i for i,t in enumerate(times)}
        self.bars=[core.Candle(t,101,102,99,101,v) for t,v in zip(times,volumes)]
        candidate=dict(setup_id='fixed',side='long',signal_time_ms=m.H,setup_confirm_ms=m.H-1,
            stop='95',target='110',zone_low='95',zone_high='100',entry_reference='100',net_rr='2')
        self.snaps=[{'integrity_ok':True,'candidate':candidate if i==0 else None} for i in range(len(times))]
    def features(self,t,price,side):
        return dict(feature_cutoff_ms=t,location='near_confirmed_zone',trend='bullish',volatility='normal',pattern_ids='bullish_engulfing',rvol=D('1.2'),rvol_bin='high',atr=1)
    def two_b_pivot(self,*args):return D(99)


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

class PortfolioExecutionTests(unittest.TestCase):
    def test_two_coins_tie_allows_only_alphabetical_first(self):
        r=m.run_account('A','fractional_spot',0,4*m.H,{'BBB':FakeAsset(),'AAA':FakeAsset()})
        self.assertEqual(len(r['trades']),1)
        self.assertEqual(r['trades'][0]['symbol'],'AAA')
        self.assertEqual(r['decision_counts']['global_position_busy'],1)
        self.assertEqual(r['curve'][-1]['time_ms'],4*m.H)

    def test_strict_account_does_not_assume_missing_exchange_rules(self):
        r=m.run_account('A','strict_spot',0,4*m.H,{'AAA':FakeAsset()})
        self.assertEqual(r['trades'],[])
        self.assertEqual(r['final_equity'],1000)
        self.assertEqual(r['decision_counts']['historical_filters_unavailable'],1)

    def test_insufficient_execution_evidence_explicitly_says_watch_with_plan(self):
        r=m.run_account('A','strict_spot',0,4*m.H,{'AAA':FakeAsset()})
        signal=next(x for x in r['signals'] if x['event']=='historical_filters_unavailable')
        self.assertEqual(signal['action'],'觀望')
        self.assertEqual(signal['SL'],'95');self.assertEqual(signal['TP1'],'110')
        self.assertEqual(signal['TP2'],'')
        self.assertTrue(signal['confirmation_required'])

    def test_cash_equation_fee_pnl_and_actual_risk_reconcile(self):
        r=m.run_account('A','fractional_spot',0,4*m.H,{'AAA':FakeAsset()})
        p=r['trades'][0]
        self.assertAlmostEqual(float(p['equity_after']-1000),float(p['net_pnl_usdt']),places=10)
        self.assertAlmostEqual(float(p['net_pnl_usdt']),float(p['quantity']*m.net_per_unit(p['raw_entry'],p['raw_exit'],'long')),places=10)
        self.assertLessEqual(p['planned_risk'],p['risk_budget'])
        self.assertLessEqual(p['cash_required'],p['equity_before'])

    def test_preentry_features_cannot_use_current_bar_close(self):
        class CheckAsset(FakeAsset):
            def features(self,t,price,side):
                self.last_requested=t
                return super().features(t,price,side)
        a=CheckAsset();r=m.run_account('C','fractional_spot',0,4*m.H,{'AAA':a})
        self.assertEqual(r['trades'][0]['feature_cutoff_ms'],m.H)

    def test_pending_order_cannot_survive_unobserved_price_gap(self):
        a=FakeAsset(times=[0,2*m.H,3*m.H])
        r=m.run_account('A','fractional_spot',0,4*m.H,{'AAA':a})
        self.assertEqual(r['trades'],[])
        self.assertEqual(r['decision_counts']['pending_data_gap_cancel'],1)

    def test_held_gap_exits_first_available_quote_not_backdated_close(self):
        a=FakeAsset(times=[0,m.H,3*m.H])
        a.bars[-1]=core.Candle(3*m.H,90,92,89,91,1)
        r=m.run_account('A','fractional_spot',0,4*m.H,{'AAA':a})
        self.assertEqual(r['trades'][0]['exit_time_ms'],3*m.H)
        self.assertEqual(r['trades'][0]['raw_exit'],90)
        self.assertGreater(r['unpriced_exposure_hours'],0)

    def test_zero_volume_entry_is_not_a_trade(self):
        r=m.run_account('A','fractional_spot',0,4*m.H,{'AAA':FakeAsset(volumes=[1,0,1,1])})
        self.assertEqual(r['trades'],[])
        self.assertEqual(r['decision_counts']['zero_volume_entry_unavailable'],1)

    def test_slippage_stress_keeps_nominal_five_bps_risk_budget(self):
        a=FakeAsset();a.bars[2]=core.Candle(2*m.H,94,96,90,94,1)
        r=m.run_account('A','fractional_spot_slip15',0,4*m.H,{'AAA':a})
        self.assertLess(r['trades'][0]['r_multiple'],-1)

    def test_spot_shorts_never_enter_primary_cash_account(self):
        a=FakeAsset();a.snaps[0]['candidate'].update(side='short',stop='105',target='90',zone_low='100',zone_high='105')
        r=m.run_account('A','fractional_spot',0,4*m.H,{'AAA':a})
        self.assertEqual(r['trades'],[])
        self.assertEqual(r['decision_counts']['spot_short_unavailable'],1)

    def test_E_third_confirmation_bar_can_enter_at_next_open(self):
        a=FakeAsset(times=[i*m.H for i in range(6)])
        a.snaps[0]['candidate']['target']='120'
        for i in (1,2):a.bars[i]=core.Candle(i*m.H,101,102,99,100,1)
        a.bars[3]=core.Candle(3*m.H,99,104,98,103,1)
        a.bars[4]=core.Candle(4*m.H,103,104,102,103,1)
        r=m.run_account('E_ob','fractional_spot',0,6*m.H,{'AAA':a})
        self.assertEqual(len(r['trades']),1)
        self.assertEqual(r['trades'][0]['entry_time_ms'],4*m.H)

    def test_E_confirmation_cannot_be_fabricated_from_zero_volume(self):
        a=FakeAsset();a.snaps[0]['candidate']['target']='120'
        a.bars[1]=core.Candle(m.H,99,104,98,103,0)
        r=m.run_account('E_ob','fractional_spot',0,4*m.H,{'AAA':a})
        self.assertEqual(r['trades'],[])

    def test_E_2b_stop_is_beyond_confirmed_wick_and_original_OB(self):
        a=FakeAsset();a.snaps[0]['candidate']['target']='120'
        a.bars[1]=core.Candle(m.H,99,102,98,101,1)
        r=m.run_account('E_2b','fractional_spot',0,4*m.H,{'AAA':a})
        self.assertEqual(len(r['trades']),1)
        self.assertEqual(r['trades'][0]['entry_time_ms'],2*m.H)
        self.assertEqual(r['trades'][0]['stop'],D('94.9'))

    def test_E_same_bar_target_stop_sensitivity_keeps_rules_fixed(self):
        def asset():
            a=FakeAsset();a.snaps[0]['candidate']['target']='120'
            a.bars[1]=core.Candle(m.H,99,103,98,102,1)
            a.bars[2]=core.Candle(2*m.H,102,125,90,103,1)
            return a
        low=m.run_account('E_ob','fractional_spot',0,4*m.H,{'AAA':asset()})
        high=m.run_account('E_ob','fractional_spot_target_first',0,4*m.H,{'AAA':asset()})
        self.assertEqual(low['trades'][0]['exit_reason'],'stop')
        self.assertEqual(high['trades'][0]['exit_reason'],'target')

    def test_zero_volume_terminal_bar_is_marked_not_falsely_sold(self):
        a=FakeAsset(volumes=[1,1,1,0])
        r=m.run_account('A','fractional_spot',0,4*m.H,{'AAA':a})
        self.assertTrue(r['terminal_position_unresolved'])
        self.assertEqual(r['trades'],[])
        self.assertEqual(r['unpriced_exposure_hours'],1)

    def test_E_maximum_holding_time_is_fixed_seventy_two_hours(self):
        a=FakeAsset(times=[i*m.H for i in range(76)]);a.snaps[0]['candidate']['target']='120'
        a.bars[1]=core.Candle(m.H,99,103,98,102,1)
        r=m.run_account('E_ob','fractional_spot',0,76*m.H,{'AAA':a})
        self.assertEqual(r['trades'][0]['exit_reason'],'max_hold_72h')
        self.assertEqual(r['trades'][0]['exit_time_ms']-r['trades'][0]['entry_time_ms'],72*m.H)

class AsOfFeatureTests(unittest.TestCase):
    def source(self,future_multiplier=1):
        a=m.Asset.__new__(m.Asset)
        a.bars=[]
        for i in range(400):
            c=D(str(100+5*math.sin(i/7)+i*.01))
            if i>=300:c*=D(future_multiplier)
            a.bars.append(core.Candle(i*m.H,c,c+2,c-2,c,D(10+i%11)))
        a.times=[b.open_ms for b in a.bars];a.by_time={t:i for i,t in enumerate(a.times)}
        a.starts=core.contiguous_segment_start(a.bars,m.H)
        a.volume=vol.VolumeSource(a.bars,'1h')
        a.reg=regime.RegimeSource([regime.Candle(b.open_ms,float(b.o),float(b.h),float(b.l),float(b.c)) for b in a.bars],'1h')
        refs=[]
        for i in range(0,400,4):
            b=a.bars[i:i+4];refs.append(zone.Bar(b[0].open_ms,float(b[0].o),float(max(q.h for q in b)),float(min(q.l for q in b)),float(b[-1].c)))
        a.refs=zone.Context.from_bars('4h',refs);a.cache={}
        return a

    def test_adapter_preserves_P1_03_fixed_formulas_at_same_cutoff(self):
        a=self.source();old=a.reg.classify(301*m.H,'bullish_engulfing')
        new=a.features(300*m.H,D(100),'long')
        self.assertEqual(new['trend'],old['trend'])
        self.assertEqual(new['volatility'],old['volatility'])

    def test_future_bar_changes_cannot_change_entry_features(self):
        before=self.source().features(300*m.H,D(100),'long')
        after=self.source(1000).features(300*m.H,D(100),'long')
        self.assertEqual(before,after)

    def test_price_zone_adapter_matches_existing_fixed_cluster_definition(self):
        a=self.source();idx=301;first=300
        pb=[zone.Bar(b.open_ms,float(b.o),float(b.h),float(b.l),float(b.c)) for b in a.bars]
        ctx=zone.Context.from_bars('1h',pb)
        anchor=min(a.bars[first].l,a.bars[idx].l)
        old=zone.classify(ctx,a.refs,idx,'bullish_engulfing',a.reg.atr[first-1])
        self.assertEqual(a.features(first*m.H,anchor,'long')['location'],old['location'])

if __name__=='__main__':unittest.main()
