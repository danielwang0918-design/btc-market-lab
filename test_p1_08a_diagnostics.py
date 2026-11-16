import unittest
from decimal import Decimal as D
from types import SimpleNamespace
import p1_candlestick_research as core
import p1_08a_diagnostics as m


def bar(i, close=100, high=None, low=None, volume=1, step=3600000):
    c=D(str(close));return core.Candle(i*step,c,D(str(high)) if high is not None else c+1,
        D(str(low)) if low is not None else c-1,c,D(str(volume)))


class TimingTests(unittest.TestCase):
    def test_raw_feature_book_contains_no_realized_or_account_labels(self):
        book=m.FeatureBook({'1h':[bar(i) for i in range(240)]})
        out=book.at(240*m.H)
        blocked={'status','source_final_status','net_pnl_usdt','exit_reason','equity_before','path_dependent_outcome'}
        self.assertFalse(set(out)&blocked)

    def test_future_entry_bar_prices_and_volume_cannot_change_features(self):
        bars=[bar(i,100+i/100) for i in range(240)]
        a=m.FrameState(bars,'1h').at(240*m.H)
        b=m.FrameState(bars+[bar(240,99999,volume=99999)],'1h').at(240*m.H)
        self.assertEqual(a,b)

    def test_4h_incomplete_bar_is_never_used(self):
        bars=[bar(i,100+i/100,step=4*m.H) for i in range(240)]
        a=m.FrameState(bars,'4h').at(957*m.H)
        self.assertEqual(a['reference_close_ms'],956*m.H)
        self.assertEqual(a,m.FrameState(bars+[bar(240,99999,step=4*m.H)],'4h').at(957*m.H))

    def test_missing_expected_close_does_not_forward_fill(self):
        self.assertEqual(m.FrameState([bar(i) for i in range(40)],'1h').at(41*m.H)['status'],'reference_gap_or_stale')

    def test_gap_resets_efficiency_and_volume_history(self):
        state=m.FrameState([bar(i) for i in list(range(40))+list(range(41,45))],'1h').at(45*m.H)
        self.assertEqual(state['ER_state'],'unknown')
        self.assertEqual(state['rvol_bin'],'gap_in_lookback')
        self.assertEqual(state['volatility_change'],'unknown')

    def test_subhour_frame_is_forbidden(self):
        with self.assertRaises(ValueError):m.FrameState([],'15m')

    def test_duplicate_clock_fails(self):
        with self.assertRaises(ValueError):m.FrameState([bar(0),bar(0)],'1h')

    def test_exact_close_is_usable_and_future_daily_candle_is_not(self):
        state=m.FrameState([bar(i,step=24*m.H) for i in range(240)],'1d').at(240*24*m.H)
        self.assertEqual(state['reference_close_ms'],240*24*m.H)
        self.assertEqual(state['status'],'closed_reference')


class StateTests(unittest.TestCase):
    def test_periodic_equal_true_ranges_are_unchanged_not_float_noise(self):
        # Two windows have exactly14 TRs of0.2, at different price scales.
        # The intervening jump has already left the last14 bars.
        bars=[bar(i,close=100 if i<100 else 300,
            high=D(100 if i<100 else 300)+D('.1'),
            low=D(100 if i<100 else 300)-D('.1')) for i in range(120)]
        self.assertEqual(m.FrameState(bars,'1h').at(120*m.H)['volatility_change'],'unchanged')

    def test_flat_price_is_range_proxy_without_division_by_zero(self):
        r=m.FrameState([bar(i) for i in range(240)],'1h').at(240*m.H)
        self.assertEqual(r['ER20'],0)
        self.assertEqual(r['ER_state'],'range_proxy')

    def test_monotonic_move_is_efficiency_one(self):
        r=m.FrameState([bar(i,100+i) for i in range(240)],'1h').at(240*m.H)
        self.assertAlmostEqual(r['ER20'],1)
        self.assertEqual(r['ER_state'],'efficient_move_proxy')

    def test_rvol_excludes_current_from_denominator(self):
        r=m.FrameState([bar(i,volume=2 if i<30 else 4) for i in range(31)],'1h').at(31*m.H)
        self.assertEqual(r['rvol'],D(2))

    def test_zero_turnover_has_unknown_illiquidity(self):
        r=m.FrameState([bar(i,volume=0) for i in range(240)],'1h').at(240*m.H)
        self.assertIsNone(r['illiquidity_proxy'])
        self.assertEqual(r['rvol_bin'],'zero_prior_mean')
        self.assertGreater(r['known_zero_volume_count'],0)

    def test_turnover_rank_uses_only_previous_200(self):
        r=m.FrameState([bar(i,volume=1 if i<200 else 2) for i in range(201)],'1h').at(201*m.H)
        self.assertEqual(r['turnover_percentile'],1)

    def test_break_reference_excludes_current_high(self):
        r=m.FrameState([bar(i) for i in range(20)]+[bar(20,110,high=112,low=99)],'1h').at(21*m.H)
        self.assertEqual(r['break20'],'up_break')

    def test_wick_reclaim_is_not_future_breakout_failure(self):
        r=m.FrameState([bar(i) for i in range(20)]+[bar(20,100,high=112,low=99)],'1h').at(21*m.H)
        self.assertEqual(r['break20'],'upper_sweep_reclaim')

    def test_two_sided_bar_has_no_assumed_intrabar_order(self):
        r=m.FrameState([bar(i) for i in range(20)]+[bar(20,100,high=112,low=88)],'1h').at(21*m.H)
        self.assertEqual(r['break20'],'both_sides_unknown')

    def test_volatility_expansion_is_different_from_percentile_level(self):
        bars=[bar(i) for i in range(240)]+[bar(i,high=120,low=80) for i in range(240,255)]
        r=m.FrameState(bars,'1h').at(255*m.H)
        self.assertEqual(r['volatility_change'],'expansion')
        self.assertGreater(r['ATR_change_ratio'],1)

    def test_insufficient_ma_is_not_called_range(self):
        r=m.FrameState([bar(i) for i in range(21)],'1h').at(21*m.H)
        self.assertEqual(r['MA'],'insufficient_prior_ma')


def trade(net='-1', gross_exit='101', reason='max_hold_72h'):
    # One unit, entry100, exit101, fees1, slip1 -> net-1.
    return dict(symbol='BTCUSDT',event_id='a',side='long',raw_entry='100',raw_exit=gross_exit,
        stop='95',target='115',quantity='1',fees_usdt='1',slippage_usdt='1',net_pnl_usdt=net,
        planned_risk='10',r_multiple=str(D(net)/10),entry_time_ms=m.START,exit_time_ms=m.START+m.H,
        reserved_until_ms=m.START+m.H,exit_reason=reason,scenario='source_stop_first')


class MoneyTests(unittest.TestCase):
    def test_positive_raw_gain_can_be_net_loss_without_stop(self):
        r=m.enrich_money(trade());s=m.summarize([r])
        self.assertEqual(s['stops'],0);self.assertEqual(s['losses'],1)
        self.assertEqual(s['cost_flipped_trades'],1)
        self.assertAlmostEqual(s['mean_raw_R']-s['mean_cost_R'],s['mean_net_R'])

    def test_invalid_archived_cost_identity_fails_closed(self):
        with self.assertRaises(ValueError):m.enrich_money(trade(net='-2'))

    def test_nonpositive_archived_risk_fails(self):
        t=trade();t['planned_risk']='0'
        with self.assertRaises(ValueError):m.enrich_money(t)

    def test_expected_cost_share_does_not_use_realized_exit(self):
        a=m.enrich_money(trade())['expected_stop_cost_share']
        t=trade(net='8',gross_exit='110')
        self.assertEqual(a,m.enrich_money(t)['expected_stop_cost_share'])

    def test_empty_group_preserves_undefined_metrics(self):
        r=m.summarize([])
        self.assertEqual(r['n'],0);self.assertIsNone(r['win_rate_pct']);self.assertIsNone(r['mean_net_R'])

    def test_15bps_expected_economics_is_not_5bps(self):
        a=m.enrich_money(trade());t=trade();t['scenario']='source_slip15'
        self.assertGreater(m.enrich_money(t)['expected_stop_cost_share'],a['expected_stop_cost_share'])


class UncertaintyTests(unittest.TestCase):
    def rows(self):
        return [dict(entry_time_ms=m.START+i*28*24*m.H,net_R=D(1 if i%2 else -1),reserved_until_ms=m.START+i*28*24*m.H+m.H,event_id=str(i)) for i in range(6)]

    def test_shared_calendar_block_keeps_cross_coin_events_together(self):
        rows=self.rows();rows=[dict(rows[0],net_R=D(1)),dict(rows[0],net_R=D(-1),event_id='other')]
        r=m.block_difference(rows,[],m.START,m.START+6*m.BLOCK,reps=100)
        self.assertEqual(r['group_a_nonempty_blocks'],1)
        self.assertEqual(r['group_a_mean_R'],0)
        self.assertIsNone(r['difference_low95'])

    def test_paired_identical_groups_have_zero_interval(self):
        r=m.block_difference(self.rows(),self.rows(),m.START,m.START+6*m.BLOCK,reps=100)
        self.assertEqual(r['difference_low95'],0);self.assertEqual(r['difference_high95'],0)

    def test_bootstrap_is_seed_reproducible(self):
        a=self.rows();b=[dict(r,net_R=r['net_R']+1) for r in a]
        self.assertEqual(m.block_difference(a,b,m.START,m.START+6*m.BLOCK,reps=100),m.block_difference(a,b,m.START,m.START+6*m.BLOCK,reps=100))

    def test_empty_calendar_blocks_are_retained(self):
        r=m.block_difference(self.rows()[:1],self.rows()[-1:],m.START,m.START+6*m.BLOCK,reps=100)
        self.assertEqual(r['calendar_blocks'],6)
        self.assertGreater(r['undefined_resamples'],0)

    def test_boundary_interval_is_half_open(self):
        rows=[dict(event_id='a',entry_time_ms=0,reserved_until_ms=10),dict(event_id='b',entry_time_ms=10,reserved_until_ms=20)]
        self.assertEqual(m.overlap_summary(rows)['pair_overlaps'],0)

    def test_future_entry_is_not_silently_clamped_into_bootstrap(self):
        with self.assertRaises(ValueError):m.block_difference([dict(self.rows()[0],entry_time_ms=m.START-1)],[],m.START,m.START+m.BLOCK,reps=10)

    def test_missing_samples_cannot_qualify_hypothesis(self):
        self.assertFalse(m.hypothesis_eligible([]))

    def qualified_rows(self):
        return [dict(period=period,universe=universe,scenario='source_stop_first',group_a_n=40,group_b_n=40,
            difference_R=.2,difference_low95=.1) for period,universe in
            [('train','all_eight'),('validation','all_eight'),('continuous','all_eight'),('continuous','BTC'),('continuous','other_seven'),('opened_test','all_eight')]]

    def test_consistent_adequate_mechanism_can_qualify_but_is_not_a_strategy(self):
        self.assertTrue(m.hypothesis_eligible(self.qualified_rows()))

    def test_validation_reversal_cannot_be_hidden_by_positive_pooled_result(self):
        rows=self.qualified_rows();rows[1]['difference_R']=-.1
        self.assertFalse(m.hypothesis_eligible(rows))

    def test_insufficient_opened_test_is_inconclusive_not_automatic_pass(self):
        rows=self.qualified_rows();rows[-1]['group_b_n']=2
        self.assertFalse(m.hypothesis_eligible(rows))


if __name__=='__main__':unittest.main()
