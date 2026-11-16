import copy
import unittest
from decimal import Decimal as D
import p1_candlestick_research as core
import p1_loss_diagnostics as m

H = 3600000


def trade(**changes):
    row = dict(side='long', raw_entry='100', raw_exit='90', entry_fill='100.05',
        exit_fill='89.955', stop='90', target='120', quantity='2', planned_risk='20.59',
        risk_budget='20.59', equity_before='1029.5', fees_usdt='.38001',
        slippage_usdt='.19', net_pnl_usdt='-20.57001', entry_time_ms='0',
        exit_time_ms=str(3*H), exit_reason='stop', policy='A')
    row.update(changes)
    return row


def bar(t, o=100, h=110, l=95, c=100, v=1):
    return core.Candle(t, o, h, l, c, v)


class DecompositionTests(unittest.TestCase):
    def test_long_cost_identity_uses_raw_quotes_and_does_not_reapply_costs(self):
        x=m.cost_decomposition(trade())
        self.assertEqual(x['raw_gross_USDT'], D(-20))
        self.assertEqual(x['cost_drag_USDT'], D('.57001'))
        self.assertEqual(x['net_USDT'], D('-20.57001'))

    def test_short_sign_is_mirrored(self):
        x=m.cost_decomposition(trade(side='short', net_pnl_usdt='19.42999'))
        self.assertEqual(x['raw_gross_USDT'], D(20))
        self.assertEqual(x['net_USDT'], D('19.42999'))

    def test_inconsistent_source_costs_fail_instead_of_silent_repair(self):
        with self.assertRaisesRegex(ValueError, 'cost identity'):
            m.cost_decomposition(trade(net_pnl_usdt='-99'))

    def test_cost_flip_is_distinct_from_already_wrong_price_direction(self):
        x=m.cost_decomposition(trade(raw_exit='100.1', net_pnl_usdt='-.37001'))
        self.assertTrue(x['cost_flipped_raw_gain_to_loss'])
        self.assertFalse(x['raw_direction_loss'])


class ExcursionTests(unittest.TestCase):
    def setUp(self):
        self.bars=[bar(0,h=150,l=80),bar(H,h=105,l=98),bar(2*H,h=130,l=85)]

    def test_intrabar_entry_and_stop_bar_extremes_are_not_guaranteed(self):
        x=m.excursions(trade(),self.bars)
        self.assertEqual(x['mfe_price_lower'],D(5))
        self.assertEqual(x['mfe_price_upper'],D(50))
        self.assertEqual(x['mae_price_lower'],D(10))
        self.assertEqual(x['mae_price_upper'],D(20))
        self.assertTrue(x['entry_bar_order_uncertain'])
        self.assertTrue(x['exit_bar_order_uncertain'])

    def test_next_open_entry_allows_entry_bar_extremes_if_exit_is_later(self):
        x=m.excursions(trade(policy='E_ob'),self.bars)
        self.assertEqual(x['mfe_price_lower'],D(50))
        self.assertFalse(x['entry_bar_order_uncertain'])

    def test_same_entry_exit_bar_has_no_unearned_extreme(self):
        x=m.excursions(trade(exit_time_ms=str(H)),self.bars)
        self.assertEqual(x['mfe_price_lower'],0)
        self.assertEqual(x['mfe_price_upper'],50)

    def test_gap_exit_ignores_prices_later_in_its_quote_bar(self):
        bars=[bar(0,h=101),bar(H,h=102),bar(2*H,h=999,l=1)]
        x=m.excursions(trade(exit_time_ms=str(2*H),exit_reason='stop_gap'),bars)
        self.assertEqual(x['mfe_price_upper'],2)
        self.assertFalse(x['exit_bar_order_uncertain'])

    def test_close_exit_extremes_are_known_except_intrabar_entry(self):
        x=m.excursions(trade(policy='E_2b',exit_reason='max_hold_72h'),self.bars)
        self.assertEqual(x['mfe_price_lower'],50)
        self.assertEqual(x['mae_price_lower'],20)

    def test_missing_bar_removes_true_upper_bound(self):
        x=m.excursions(trade(),[self.bars[0],self.bars[2]])
        self.assertEqual(x['holding_missing_bars'],1)
        self.assertIsNone(x['mfe_price_upper'])
        self.assertEqual(x['mfe_observed_envelope'],50)

    def test_zero_volume_is_marked_incomplete(self):
        x=m.excursions(trade(),[self.bars[0],bar(H,v=0),self.bars[2]])
        self.assertEqual(x['holding_zero_volume_bars'],1)
        self.assertIsNone(x['mae_price_upper'])

    def test_short_excursions_reverse_price_direction(self):
        x=m.excursions(trade(side='short',raw_exit='110'),self.bars)
        self.assertEqual(x['mfe_price_lower'],2)
        self.assertEqual(x['mae_price_lower'],10)
        self.assertEqual(x['mfe_price_upper'],20)

    def test_future_after_exit_cannot_change_holding_excursion(self):
        a=m.excursions(trade(),self.bars)
        b=m.excursions(trade(),self.bars+[bar(3*H,h=9999,l=1)])
        self.assertEqual(a,b)


class PostStopTests(unittest.TestCase):
    def test_excludes_stop_bar_and_right_boundary(self):
        t=trade(exit_time_ms=str(H))
        bars=[bar(0,h=999),bar(H,o=92,h=95,l=85,c=93),bar(2*H,h=500)]
        x=m.post_stop_window(t,bars,1,3*H)
        self.assertEqual(x['status'],'complete')
        self.assertFalse(x['entry_recovered_or_better'])
        self.assertFalse(x['original_target_reached'])

    def test_split_boundary_censors_even_if_future_data_exist(self):
        x=m.post_stop_window(trade(exit_time_ms=str(H)),[bar(H),bar(2*H)],2,2*H)
        self.assertEqual(x['status'],'split_or_calendar_censored')
        self.assertIsNone(x['entry_recovered_or_better'])

    def test_missing_and_zero_volume_are_not_successful_observations(self):
        t=trade(exit_time_ms=str(H))
        self.assertEqual(m.post_stop_window(t,[],1,3*H)['status'],'data_gap')
        self.assertEqual(m.post_stop_window(t,[bar(H,v=0)],1,3*H)['status'],'zero_volume')

    def test_non_stop_exits_have_no_post_stop_label(self):
        x=m.post_stop_window(trade(exit_reason='target'),[],24,999*H)
        self.assertEqual(x['status'],'not_stop')

    def test_endpoint_direction_is_disjoint_while_extremes_can_overlap(self):
        t=trade(exit_time_ms=str(H))
        x=m.post_stop_window(t,[bar(H,o=90,h=105,l=80,c=88)],1,3*H)
        self.assertFalse(x['endpoint_favorable'])
        self.assertTrue(x['endpoint_adverse'])
        self.assertTrue(x['any_favorable_from_stop'])
        self.assertTrue(x['any_adverse_from_stop'])
        self.assertTrue(x['both_directions_order_unresolved'])

    def test_short_post_stop_endpoint_and_target_are_mirrored(self):
        t=trade(side='short',raw_exit='110',target='90',exit_time_ms=str(H))
        x=m.post_stop_window(t,[bar(H,o=110,h=115,l=85,c=100)],1,3*H)
        self.assertTrue(x['endpoint_favorable'])
        self.assertTrue(x['original_target_reached'])

    def test_every_user_fixed_window_uses_its_own_endpoint(self):
        t=trade(exit_time_ms=str(H))
        bars=[bar(k*H,o=90,h=100,l=80,c=91 if k<24 else 89) for k in range(1,49)]
        for hours in (6,12,24,48):
            with self.subTest(hours=hours):
                x=m.post_stop_window(t,bars,hours,49*H)
                self.assertEqual(x['status'],'complete')
                self.assertEqual(x['endpoint_favorable'],hours<24)


class AsOfContextTests(unittest.TestCase):
    def test_4H_context_excludes_incomplete_4H_bar(self):
        a=m.RegimeIndex([bar(0),bar(4*H,h=999)],'4h')
        x=a.at(6*H)
        self.assertEqual(x['reference_close_ms'],4*H)
        self.assertEqual(x['direction'],'insufficient_prior_ma')

    def test_future_prices_do_not_change_asof_MA_direction(self):
        bars=[bar(i*H,o=100+i,h=102+i,l=99+i,c=101+i) for i in range(240)]
        a=m.RegimeIndex(bars,'1h').at(225*H)
        bars[225]=bar(225*H,h=99999,l=1)
        b=m.RegimeIndex(bars,'1h').at(225*H)
        self.assertEqual(a,b)
        self.assertEqual(a['direction'],'bullish')

    def test_stale_and_gapped_regimes_do_not_force_trend_label(self):
        a=m.RegimeIndex([bar(0),bar(2*H)],'1h')
        self.assertEqual(a.at(2*H)['direction'],'reference_data_gap_or_stale')
        self.assertEqual(a.at(3*H)['direction'],'insufficient_prior_ma')

    def test_BOS_confirmation_time_and_full_evidence_gate_future_event(self):
        bars=[bar(i*H) for i in range(6)]
        events=[dict(confirmed_at_close_time=2*H-1,direction='bullish',evidence_opens={0,H}),
                dict(confirmed_at_close_time=4*H-1,direction='bearish',evidence_opens={H,3*H})]
        a=m.StructureIndex(bars,'1h',events)
        self.assertEqual(a.at(3*H)['direction'],'bullish')
        self.assertEqual(a.at(4*H)['phase'],'latest_break_changed_direction')
        self.assertEqual(a.at(4*H)['reference_confirm_ms'],4*H-1)

    def test_BOS_evidence_outside_canonical_200bar_window_is_unavailable(self):
        bars=[bar(i*H) for i in range(210)]
        events=[dict(confirmed_at_close_time=209*H-1,direction='bullish',evidence_opens={0,208*H})]
        self.assertEqual(m.StructureIndex(bars,'1h',events).at(210*H)['direction'],'no_confirmed_BOS_in_window')

    def test_unknown_MTF_context_is_not_called_range_or_reversal(self):
        self.assertEqual(m.mtf_alignment(['bullish','insufficient_prior_ma','bullish'],'long'),'unknown')
        self.assertEqual(m.mtf_alignment(['bullish']*3,'long'),'all_aligned')
        self.assertEqual(m.mtf_alignment(['bullish','bearish','bullish'],'long'),'mixed_or_opposed')


class FilterAndSummaryTests(unittest.TestCase):
    def test_joint_failures_are_preserved_without_relaxing_RVOL(self):
        f=dict(location='far_from_nearest_pivot',trend='bearish',pattern_ids='',rvol=D('1.199999'),volatility='high')
        x=m.condition_state(f,'long','D')
        self.assertFalse(x['pass_policy'])
        self.assertEqual(x['failed_conditions'],'zone|ma|pattern|rvol|atr')
        self.assertEqual(x['first_failed_condition'],'zone')

    def test_C_is_independent_of_zone_MA_ATR_and_includes_RVOL_boundary(self):
        f=dict(location='unknown',trend='unknown',pattern_ids='bullish_engulfing',rvol=D('1.2'),volatility='unknown')
        self.assertTrue(m.condition_state(f,'long','C')['pass_policy'])

    def test_missing_RVOL_fails_with_missingness_flag(self):
        f=dict(location='near_confirmed_zone',trend='bullish',pattern_ids='x',rvol=None,volatility='normal')
        x=m.condition_state(f,'long','C')
        self.assertFalse(x['pass_policy']);self.assertTrue(x['rvol_missing'])

    def test_empty_group_has_undefined_win_rate_and_expectancy(self):
        x=m.aggregate([])
        self.assertEqual(x['trades'],0)
        self.assertIsNone(x['win_rate_pct']);self.assertIsNone(x['mean_net_R'])

    def test_stop_exit_rate_is_not_loss_rate(self):
        a=dict(m.cost_decomposition(trade()),exit_reason='stop')
        b=dict(m.cost_decomposition(trade(raw_exit='100.1',net_pnl_usdt='-.37001')),exit_reason='max_hold_72h')
        x=m.aggregate([a,b])
        self.assertEqual(x['stop_exit_rate_pct'],50)
        self.assertEqual(x['actual_loss_rate_pct'],100)

    def test_diagnostic_bins_have_fixed_boundaries(self):
        self.assertEqual(m.holding_bucket(D(1)),'[0,1]')
        self.assertEqual(m.holding_bucket(D('1.01')),'(1,6]')
        self.assertEqual(m.holding_bucket(D(100)),'(72,+inf)')

    def test_BTC_is_separated_from_other_seven(self):
        self.assertEqual(m.coin_group('BTCUSDT'),'BTC')
        self.assertEqual(m.coin_group('ETHUSDT'),'other_seven')

    def test_source_manifest_wrong_commit_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'source commit'):
            m.validate_manifest({'current_commit':'future'})


if __name__=='__main__':
    unittest.main()
