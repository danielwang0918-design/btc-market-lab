import unittest
from decimal import Decimal as D
import p1_candlestick_research as core
import p1_portfolio as money
import p1_08b_paths as m

T=money.START;H=money.H;DAY=24*H
def bar(i,o=100,h=105,l=95,c=100,v=1):
    return core.Candle(T+i*H,*[D(str(x)) for x in (o,h,l,c,v)])
def trade(**changes):
    r=dict(policy='T20_CONT',event_id='x',symbol='BTCUSDT',side='long',registration_ms=T,
        entry_time_ms=T,exit_time_ms=T+3*H,exit_bar_ms=T+2*H,raw_entry='100',stop='90',target='120',
        raw_exit='105',exit_reason='max_hold_72h',market_at_open='True',scenario='source_stop_first',quantity='1')
    r.update(changes);risk=-money.net_per_unit(D(r['raw_entry']),D(r['stop']),r['side'],D(15) if r['scenario']=='source_slip15' else D(5))
    r['planned_risk']=str(risk)
    return r


class PathTests(unittest.TestCase):
    def test_full_closed_holding_extremes_are_exact(self):
        r=m.analyze(trade(),[bar(0,h=112,l=99),bar(1,h=108,l=94),bar(2,h=110,l=98,c=105)])
        self.assertEqual(r['mfe_R0_lower'],D('1.2'));self.assertEqual(r['mfe_R0_upper'],D('1.2'))
        self.assertEqual(r['mae_R0_upper'],D('.6'))

    def test_short_direction_is_mirrored(self):
        r=m.analyze(trade(side='short',stop='110',target='80',raw_exit='95'),[bar(i,h=104,l=88,c=95) for i in range(3)])
        self.assertEqual(r['mfe_R0_lower'],D('1.2'));self.assertEqual(r['mae_R0_lower'],D('.4'))

    def test_entry_limit_bar_high_is_not_guaranteed_after_fill_in_S_ATR1(self):
        r=m.analyze(trade(policy='S_ATR1',market_at_open='False'),[bar(0,h=119,l=91),bar(1,h=106,l=99),bar(2,h=106,l=99,c=105)])
        self.assertLess(r['mfe_R0_lower'],r['mfe_R0_upper'])
        self.assertTrue(r['entry_bar_uncertain'])

    def test_intrabar_exit_does_not_claim_later_bar_high_as_guaranteed(self):
        r=m.analyze(trade(exit_reason='stop',raw_exit='90',exit_time_ms=T+H,exit_bar_ms=T),[bar(0,h=150,l=80,c=120)])
        self.assertEqual(r['mfe_R0_lower'],0)
        self.assertEqual(r['mfe_R0_upper'],5)
        self.assertEqual(r['mae_R0_lower'],1)

    def test_future_high_does_not_enter_held_prefix_but_tape_is_flagged(self):
        rows=[bar(i,h=105 if i<3 else 200) for i in range(6)]
        held=m.analyze(trade(),rows,6)
        tape=m.analyze(trade(),rows,6,tape=True)
        self.assertEqual(held['mfe_R0_upper'],D('.5'))
        self.assertEqual(tape['mfe_R0_upper'],10)
        self.assertTrue(tape['contains_post_exit_prices']);self.assertFalse(held['contains_post_exit_prices'])

    def test_gap_removes_true_upper_bound(self):
        r=m.analyze(trade(),[bar(0),bar(2)])
        self.assertIsNone(r['mfe_R0_upper']);self.assertEqual(r['missing_bars'],1)

    def test_zero_volume_removes_true_upper_bound(self):
        r=m.analyze(trade(),[bar(0),bar(1,v=0),bar(2)])
        self.assertIsNone(r['mae_R0_upper']);self.assertEqual(r['zero_volume_bars'],1)

    def test_split_censors_fixed_tape_even_if_later_data_exist(self):
        r=m.analyze(trade(),[bar(i) for i in range(6)],6,tape=True,cutoff=T+4*H)
        self.assertTrue(r['right_censored']);self.assertIsNone(r['mfe_R0_upper'])

    def test_early_exit_prefix_does_not_need_future_split_data(self):
        r=m.analyze(trade(),[bar(i) for i in range(3)],72,cutoff=T+4*H)
        self.assertFalse(r['right_censored']);self.assertTrue(r['early_exit_capped'])

    def test_open_exit_does_not_include_the_exit_hours_later_extreme(self):
        r=m.analyze(trade(exit_reason='stop_gap',raw_exit='85',exit_time_ms=T+H,exit_bar_ms=T+H),[bar(0),bar(1,o=85,h=999,l=80,c=100)])
        self.assertEqual(r['mfe_R0_upper'],D('.5'));self.assertEqual(r['mae_R0_lower'],D('1.5'))

    def test_same_bar_TP_SL_order_remains_unknown(self):
        r=m.analyze(trade(),[bar(0,h=125,l=85)]+[bar(i) for i in range(1,6)],6,tape=True)
        self.assertEqual(r['TP_SL_order'],'same_bar_unknown')

    def test_distinct_bar_TP_SL_order_is_known_only_for_tape(self):
        r=m.analyze(trade(),[bar(0,h=105,l=85),bar(1,h=125,l=95)]+[bar(i) for i in range(2,6)],6,tape=True)
        self.assertEqual(r['TP_SL_order'],'SL_first_distinct_bars')

    def test_same_bar_extrema_have_unknown_peak_order(self):
        r=m.analyze(trade(),[bar(0,h=118,l=92),bar(1),bar(2)])
        self.assertEqual(r['peak_order'],'same_bar_unknown')

    def test_arrival_time_is_an_hour_interval(self):
        r=m.analyze(trade(),[bar(0,h=104,l=99),bar(1,h=111,l=99),bar(2)])
        hit=next(x for x in r['arrivals'] if x['level']=='favorable_1R0')
        self.assertEqual(hit['possible_from_hours'],D(1));self.assertEqual(hit['confirmed_by_hours'],D(2))

    def test_giveback_uses_gross_endpoint_and_cost_is_separate(self):
        r=m.analyze(trade(),[bar(0,h=115),bar(1),bar(2,c=105)])
        self.assertEqual(r['giveback_R0_lower'],1)
        self.assertEqual(r['endpoint_gross_R0'],D('.5'))

    def test_two_R_definitions_are_not_silently_equated(self):
        r=m.analyze(trade(),[bar(i) for i in range(3)])
        self.assertNotEqual(r['R0_price'],r['R_model_per_unit'])

    def test_favorable_but_too_small_to_cover_cost(self):
        r=m.analyze(trade(raw_exit='100.01'),[bar(i,h='100.02',l='99.99',c='100.01') for i in range(3)])
        self.assertGreater(r['mfe_R0_lower'],0)
        self.assertLess(r['net_quote_potential_R_model_lower'],0)
        self.assertGreater(r['cost_over_mfe_lower'],1)

    def test_zero_MFE_cost_ratio_is_undefined(self):
        r=m.analyze(trade(raw_exit='99'),[bar(i,h=100,l=98,c=99) for i in range(3)])
        self.assertIsNone(r['cost_over_mfe_lower'])

    def test_cost_stress_does_not_change_price_R0(self):
        rows=[bar(i) for i in range(3)]
        a=m.analyze(trade(),rows);b=m.analyze(trade(scenario='source_slip15'),rows)
        self.assertEqual(a['mfe_R0_lower'],b['mfe_R0_lower'])
        self.assertLess(b['net_quote_potential_R_model_lower'],a['net_quote_potential_R_model_lower'])

    def test_nonpositive_risk_and_duplicate_bars_fail_closed(self):
        with self.assertRaises(ValueError):m.analyze(trade(stop='100'),[bar(i) for i in range(3)])
        with self.assertRaises(ValueError):m.analyze(trade(),[bar(0),bar(0),bar(2)])

    def test_unsupported_horizon_is_not_a_parameter_grid(self):
        with self.assertRaises(ValueError):m.analyze(trade(),[bar(0)],7)

    def test_unknown_entry_type_is_not_assumed_market(self):
        with self.assertRaises(ValueError):m.analyze(trade(market_at_open=''),[bar(i) for i in range(3)])


class FeatureTests(unittest.TestCase):
    def fixture(self,midnight=False):
        j=23 if midnight else 0
        bars=[bar(i,h=101,l=99,v=2) for i in range(-80,j)]
        bars.append(bar(j,o=100,h=124,l=99,c=121,v=4));bars.append(bar(j+1))
        daily=[core.Candle(T-(20-i)*DAY,D(100),D(110),D(90),D(100),D(1)) for i in range(20)]
        daily[15]=core.Candle(T-5*DAY,D(100),D(120),D(80),D(100),D(1))
        event=trade(registration_ms=T+(j+1)*H,entry_time_ms=T+(j+1)*H,edge='upper',level='120')
        return event,{'1h':bars,'1d':daily}

    def test_breach_volume_is_closed_and_denominator_excludes_it(self):
        event,frames=self.fixture();r=m.BreakFeatures(frames).at(event)
        self.assertEqual(r['break_rvol'],2)
        self.assertEqual(r['break_available_ms'],event['registration_ms'])

    def test_future_entry_bar_prices_volume_and_PnL_cannot_change_features(self):
        event,frames=self.fixture();a=m.BreakFeatures(frames).at(event)
        frames['1h'][-1]=bar(1,h=9999,l=1,c=999,v=999)
        event.update(net_pnl_usdt='999',exit_time_ms=money.END,path_dependent_outcome='True')
        self.assertEqual(a,m.BreakFeatures(frames).at(event))

    def test_prebreak_ATR_does_not_include_break_expansion(self):
        event,frames=self.fixture();r=m.BreakFeatures(frames).at(event)
        self.assertEqual(r['ATR_pre'],2)
        self.assertEqual(r['compression_ratio'],1)
        self.assertEqual(r['prebreak_available_ms'],T)

    def test_compression_denominator_excludes_current_prebreak_ATR(self):
        event,frames=self.fixture();frames['1h'][-3]=bar(-1,h=109,l=91,v=2)
        r=m.BreakFeatures(frames).at(event)
        self.assertAlmostEqual(float(r['compression_ratio']),11/7)

    def test_midnight_registration_keeps_breach_days_frozen_reference(self):
        event,frames=self.fixture(True)
        frames['1d'].append(core.Candle(T,D(100),D(999),D(1),D(100),D(1)))
        r=m.BreakFeatures(frames).at(event)
        self.assertEqual(r['reference_high'],120)
        self.assertEqual(r['reference_closed_ms'],T)

    def test_non20D_family_is_unavailable_not_faked_using_last_bar(self):
        event,frames=self.fixture();event['policy']='A'
        r=m.BreakFeatures(frames).at(event)
        self.assertEqual(r['feature_status'],'not_defined_for_signal_family')
        self.assertIsNone(r['body_share'])

    def test_features_do_not_export_posthoc_labels(self):
        event,frames=self.fixture();r=m.BreakFeatures(frames).at(event)
        self.assertFalse(set(r)&{'net_pnl_usdt','exit_time_ms','MFE','MAE','status','source_final_status'})

    def test_reference_mismatch_fails_instead_of_silent_join(self):
        event,frames=self.fixture();event['level']='999'
        with self.assertRaises(ValueError):m.BreakFeatures(frames).at(event)


if __name__=='__main__':unittest.main()
