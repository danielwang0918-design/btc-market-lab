"""Read-only P1-06A arithmetic and OHLC bounds; never generates trade decisions."""
from __future__ import annotations

import bisect
from decimal import Decimal as D

H = 3_600_000
SOURCE_COMMIT = '09d9e1cd43eb0e666ce3e4500da97dc5589396e9'
SOURCE_ZIP_SHA = '828ec78907a33e30afdb5ae1d397e80b135a38e015fd8c7678a8d155ccc0d596'
PROTOCOL_SHA = '6b62d825f7210d50a3bb6a3e294923d64b038a32126cfe4aacf39fadfcf8a581'
POLICIES = ('A', 'B', 'E_ob', 'E_2b')
MODELS = ('fractional_spot', 'hypothetical_1x_long_short')
PERIODS = ('continuous', 'train', 'validation', 'opened_test')
POST_HOURS = (6, 12, 24, 48)
OPEN_EXITS = ('stop_gap', 'data_gap_first_quote')
INTRABAR_EXITS = ('stop', 'target')
STOPS = ('stop', 'stop_gap')


def decimal(x):
    return x if isinstance(x, D) else D(str(x))


def coin_group(symbol):
    return 'BTC' if symbol == 'BTCUSDT' else 'other_seven'


def validate_manifest(manifest):
    if manifest.get('current_commit') != SOURCE_COMMIT:
        raise ValueError('Pinned P1-05 source commit mismatch')
    if manifest.get('run_count') != 120 or manifest.get('independently_checked_accounts') != 120:
        raise ValueError('Incomplete source accounts')
    if manifest.get('live_execution_implemented') or manifest.get('new_OOS'):
        raise ValueError('Unexpected source scope')


def cost_decomposition(trade):
    q = decimal(trade['quantity'])
    sign = 1 if trade['side'] == 'long' else -1
    gross = q * (decimal(trade['raw_exit']) - decimal(trade['raw_entry'])) * sign
    fees, slip, net = (decimal(trade[k]) for k in ('fees_usdt', 'slippage_usdt', 'net_pnl_usdt'))
    if abs(gross - fees - slip - net) > D('1e-12'):
        raise ValueError('Archived cost identity does not reconcile')
    risk = decimal(trade['planned_risk'])
    if q <= 0 or risk <= 0:
        raise ValueError('Nonpositive archived quantity or risk')
    raw_stop_risk = q * abs(decimal(trade['raw_entry']) - decimal(trade['stop']))
    return dict(raw_gross_USDT=gross, fees_USDT=fees, slippage_USDT=slip,
        cost_drag_USDT=fees+slip, net_USDT=net, raw_gross_R=gross/risk,
        cost_drag_R=(fees+slip)/risk, net_R=net/risk,
        normal_stop_cost_share=(risk-raw_stop_risk)/risk,
        cost_flipped_raw_gain_to_loss=gross > 0 and net < 0,
        raw_direction_loss=gross < 0, raw_flat_net_loss=gross == 0 and net < 0)


def confirmation_geometry(trade, registered, entered):
    """Compare archived plans arithmetically, without a counterfactual replay."""
    e, stop, target = (decimal(trade[k]) for k in ('raw_entry', 'stop', 'target'))
    ref, old_stop, old_target = (decimal(registered[k]) for k in ('entry_reference', 'SL', 'TP1'))
    sign = D(1) if trade['side'] == 'long' else D(-1)
    def exact_rr(price, sl, tp):
        fill_e, fill_sl, fill_tp = price*(1+sign*D('.0005')), sl*(1-sign*D('.0005')), tp*(1-sign*D('.0005'))
        risk = (fill_e-fill_sl)*sign+(fill_e+fill_sl)*D('.001')
        reward = (fill_tp-fill_e)*sign-(fill_e+fill_tp)*D('.001')
        return reward/risk
    registered_time = int(registered['time_ms'])
    if registered_time > int(trade['entry_time_ms']):
        raise ValueError('Future canonical registration')
    actual_approx = ((target-e)*sign-(e+target)*D('.0015'))/((e-stop)*sign+(e+stop)*D('.0015'))
    return dict(registered_entry_reference=ref, registered_OB_stop=old_stop,
        registered_original_TP1=old_target, registered_plan_net_RR=registered['net_RR'],
        entered_signal_logged_net_RR=entered['net_RR'],
        original_reference_exact_net_RR=exact_rr(ref,old_stop,old_target),
        RR_change_from_original_reference=exact_rr(e,stop,target)-exact_rr(ref,old_stop,old_target),
        confirmation_entry_price_drift_pct=(e-ref)*sign/ref*100,
        entry_delay_from_registration_hours=D(int(trade['entry_time_ms'])-registered_time)/H,
        stop_extended_beyond_original_OB_edge=(stop-old_stop)*sign < 0,
        logged_RR_matches_actual_entry_approx=format(actual_approx,'.8f') == entered['net_RR'])


class BarIndex:
    def __init__(self, bars):
        self.bars = bars
        self.times = [b.open_ms for b in bars]
        if any(b <= a for a, b in zip(self.times, self.times[1:])):
            raise ValueError('Unsorted or duplicate diagnostic bars')

    def window(self, start, end):
        return self.bars[bisect.bisect_left(self.times, start):bisect.bisect_left(self.times, end)]

    def at(self, time):
        i = bisect.bisect_left(self.times, time)
        return self.bars[i] if i < len(self.times) and self.times[i] == time else None


def excursions(trade, bars):
    """Conditional bounds, not an invented intrabar path or net exit PnL."""
    index = bars if isinstance(bars, BarIndex) else BarIndex(bars)
    entry, exit_time = int(trade['entry_time_ms']), int(trade['exit_time_ms'])
    if exit_time < entry or entry % H or exit_time % H:
        raise ValueError('Invalid archived interval')
    selected = index.window(entry, exit_time)
    q = decimal(trade['quantity'])
    scale = decimal(trade['planned_risk']) / q
    reference = decimal(trade['raw_entry'])
    sign = 1 if trade['side'] == 'long' else -1
    known = [D(0), (decimal(trade['raw_exit']) - reference)*sign]
    observed = list(known)
    entry_uncertain = trade['policy'] in ('A', 'B')
    exit_uncertain = trade['exit_reason'] in INTRABAR_EXITS
    exit_bar = exit_time-H if trade['exit_reason'] not in OPEN_EXITS else None
    zero = 0
    for b in selected:
        if b.v == 0:
            zero += 1
            continue
        moves = [(b.h-reference)*sign, (b.l-reference)*sign]
        observed.extend(moves)
        if not (b.open_ms == entry and entry_uncertain) and not (b.open_ms == exit_bar and exit_uncertain):
            known.extend(moves)
    missing = (exit_time-entry)//H - len(selected)
    complete = missing == 0 and zero == 0
    lower_f, lower_a = max(known), -min(known)
    envelope_f, envelope_a = max(observed), -min(observed)
    upper_f, upper_a = (envelope_f, envelope_a) if complete else (None, None)
    return dict(mfe_price_lower=lower_f, mae_price_lower=lower_a,
        mfe_price_upper=upper_f, mae_price_upper=upper_a,
        mfe_observed_envelope=envelope_f, mae_observed_envelope=envelope_a,
        mfe_gross_over_net_risk_R_lower=lower_f/scale,
        mae_gross_over_net_risk_R_lower=lower_a/scale,
        mfe_gross_over_net_risk_R_upper=upper_f/scale if upper_f is not None else None,
        mae_gross_over_net_risk_R_upper=upper_a/scale if upper_a is not None else None,
        holding_missing_bars=missing, holding_zero_volume_bars=zero,
        excursion_status='complete_observed_OHLC_bounds' if complete else 'incomplete_no_true_upper_bound',
        entry_bar_order_uncertain=entry_uncertain, exit_bar_order_uncertain=exit_uncertain)


def post_stop_window(trade, bars, hours, cutoff):
    result = dict(status='not_stop', hours=hours, entry_recovered_or_better=None,
        original_target_reached=None, best_price_move_from_entry=None, worst_price_move_from_entry=None,
        endpoint_favorable=None, endpoint_adverse=None, endpoint_flat=None,
        any_favorable_from_stop=None, any_adverse_from_stop=None, both_directions_order_unresolved=None)
    if trade['exit_reason'] not in STOPS:
        return result
    start = int(trade['exit_time_ms'])
    if start+hours*H > cutoff:
        return {**result, 'status': 'split_or_calendar_censored'}
    index = bars if isinstance(bars, BarIndex) else BarIndex(bars)
    window = index.window(start, start+hours*H)
    if len(window) != hours:
        return {**result, 'status': 'data_gap'}
    if any(b.v == 0 for b in window):
        return {**result, 'status': 'zero_volume'}
    reference = decimal(trade['raw_entry'])
    target = decimal(trade['target'])
    sign = 1 if trade['side'] == 'long' else -1
    moves = [(price-reference)*sign for b in window for price in (b.h, b.l)]
    best, worst = max(moves), min(moves)
    stop_quote = decimal(trade['raw_exit'])
    endpoint = (window[-1].c-stop_quote)*sign
    favorable = max((p-stop_quote)*sign for b in window for p in (b.h, b.l)) > 0
    adverse = min((p-stop_quote)*sign for b in window for p in (b.h, b.l)) < 0
    return dict(result, status='complete', entry_recovered_or_better=best >= 0,
        original_target_reached=best >= (target-reference)*sign,
        best_price_move_from_entry=best, worst_price_move_from_entry=worst,
        endpoint_move_from_stop_quote=endpoint, endpoint_favorable=endpoint > 0,
        endpoint_adverse=endpoint < 0, endpoint_flat=endpoint == 0,
        any_favorable_from_stop=favorable, any_adverse_from_stop=adverse,
        both_directions_order_unresolved=favorable and adverse)


class RegimeIndex:
    """P1-03 MA200 / ATR formulas at an entry cutoff, on each closed timeframe."""
    def __init__(self, bars, frame, source=None):
        import p1_market_regime as regime
        self.source = source or regime.RegimeSource([regime.Candle(b.open_ms, float(b.o), float(b.h), float(b.l), float(b.c)) for b in bars], frame)

    def at(self, t):
        source = self.source
        p = bisect.bisect_right(source.times, t-source.step)-1
        result = dict(direction='reference_data_gap_or_stale', reference_close_ms=None,
            volatility='insufficient_prior_atr_distribution', atr14=None)
        if p < 0 or source.times[p]+source.step != (t//source.step)*source.step:
            return result
        st, atr = source.starts[p], source.atr[p]
        result.update(direction='insufficient_prior_ma', reference_close_ms=source.times[p]+source.step, atr14=atr)
        if p-20-200+1 >= st and atr is not None and atr > 0:
            ma, prev, close = source.ma(p), source.ma(p-20), source.bars[p].c
            result['direction'] = 'bullish' if close > ma and ma > prev else 'bearish' if close < ma and ma < prev else 'transition_or_range'
        if atr is not None and p-200+1 >= st+14:
            history = source.atr_pct[p-199:p+1]
            current = history[-1]
            rank = (sum(x < current for x in history)+.5*sum(x == current for x in history))/200
            result['volatility'] = 'low' if rank <= .25 else 'high' if rank >= .75 else 'normal'
        return result


class StructureIndex:
    """Read-only original 2L2R BOS context inside its canonical 200-bar window."""
    def __init__(self, bars, frame, events):
        self.index = BarIndex(bars)
        self.step = {'1h': H, '4h': 4*H, '1d': 24*H}[frame]
        self.events = sorted(events, key=lambda e: int(e['confirmed_at_close_time']))
        self.confirms = [int(e['confirmed_at_close_time']) for e in self.events]

    def at(self, t):
        p = bisect.bisect_right(self.index.times, t-self.step)-1
        result = dict(direction='no_confirmed_BOS_in_window', phase='insufficient_two_breaks',
            reference_confirm_ms=None, age_hours=None)
        if p < 0 or self.index.times[p]+self.step != (t//self.step)*self.step:
            return dict(result, direction='reference_data_gap_or_stale')
        window = self.index.times[max(0, p-199):p+1]
        if any(b-a != self.step for a, b in zip(window, window[1:])):
            return dict(result, direction='reference_gap_in_last200')
        opens = set(window)
        end = bisect.bisect_left(self.confirms, t)
        eligible = []
        for index in range(end-1, -1, -1):
            e = self.events[index]
            if int(e['confirmed_at_close_time']) < window[0]:
                break
            if e['evidence_opens'].issubset(opens):
                eligible.append(e)
                if len(eligible) == 2:
                    break
        if not eligible:
            return result
        latest = eligible[0]
        phase = 'insufficient_two_breaks'
        if len(eligible) > 1:
            phase = 'successive_same_direction_breaks' if latest['direction'] == eligible[1]['direction'] else 'latest_break_changed_direction'
        return dict(direction=latest['direction'], phase=phase,
            reference_confirm_ms=int(latest['confirmed_at_close_time']),
            age_hours=D(t-int(latest['confirmed_at_close_time'])-1)/H)


def mtf_alignment(directions, side):
    if any(d not in ('bullish', 'bearish', 'transition_or_range') for d in directions):
        return 'unknown'
    expected = 'bullish' if side == 'long' else 'bearish'
    return 'all_aligned' if all(d == expected for d in directions) else 'mixed_or_opposed'


def condition_state(features, side, policy):
    order = ('pattern', 'rvol') if policy == 'C' else ('zone', 'ma', 'pattern', 'rvol', 'atr')
    checks = dict(zone=features['location'] == 'near_confirmed_zone',
        ma=features['trend'] == ('bullish' if side == 'long' else 'bearish'),
        pattern=bool(features['pattern_ids']),
        rvol=features['rvol'] is not None and decimal(features['rvol']) >= D('1.2'),
        atr=features['volatility'] == 'normal')
    failures = [key for key in order if not checks[key]]
    return dict(**{key+'_ok': value for key, value in checks.items()},
        pass_policy=not failures, failed_conditions='|'.join(failures) or 'none',
        first_failed_condition=failures[0] if failures else 'none',
        rvol_missing=features['rvol'] is None)


def holding_bucket(hours):
    hours = decimal(hours)
    for lo, hi in ((0, 1), (1, 6), (6, 24), (24, 72)):
        if hours <= hi:
            return f'[{lo},{hi}]' if lo == 0 else f'({lo},{hi}]'
    return '(72,+inf)'


def bucket(value, edges):
    lo = 0
    for hi in edges:
        if value < decimal(hi):
            return f'[{lo},{hi})'
        lo = hi
    return f'[{lo},+inf)'


def mean(values):
    return sum(values)/len(values) if values else None


def aggregate(trades):
    n = len(trades)
    net = [decimal(t['net_USDT']) for t in trades]
    rs = [decimal(t['net_R']) for t in trades]
    winners = [r for r, p in zip(rs, net) if p > 0]
    losers = [r for r, p in zip(rs, net) if p < 0]
    positive = sum(p for p in net if p > 0)
    negative = -sum(p for p in net if p < 0)
    avg_win, avg_loss = mean(winners), mean([-r for r in losers])
    gross = [decimal(t['raw_gross_USDT']) for t in trades]
    gross_loss = -sum(p for p in gross if p < 0)
    fields = dict(trades=n, wins=len(winners), losses=len(losers),
        win_rate_pct=D(len(winners))*100/n if n else None,
        mean_net_R=mean(rs), mean_winning_R=avg_win, mean_losing_R_abs=avg_loss,
        empirical_binary_break_even_win_pct=avg_loss/(avg_win+avg_loss)*100 if avg_win is not None and avg_loss is not None else None,
        raw_gross_USDT=sum(gross), net_USDT=sum(net),
        fees_USDT=sum(decimal(t['fees_USDT']) for t in trades),
        slippage_USDT=sum(decimal(t['slippage_USDT']) for t in trades),
        mean_cost_drag_R=mean([decimal(t['cost_drag_R']) for t in trades]),
        net_PF=positive/negative if negative else None,
        fixed_trade_raw_PF=sum(p for p in gross if p > 0)/gross_loss if gross_loss else None,
        net_losses_already_raw_price_loss=sum(t['raw_direction_loss'] for t in trades),
        net_losses_cost_flipped_raw_gain=sum(t['cost_flipped_raw_gain_to_loss'] for t in trades),
        net_losses_raw_flat=sum(t['raw_flat_net_loss'] for t in trades))
    stops = [t for t in trades if t.get('exit_reason') in STOPS]
    fields.update(stop_exits=len(stops), stop_exit_rate_pct=D(len(stops))*100/n if n else None,
        actual_loss_rate_pct=D(len(losers))*100/n if n else None,
        normal_stop_exits=sum(t.get('exit_reason') == 'stop' for t in trades),
        gap_stop_exits=sum(t.get('exit_reason') == 'stop_gap' for t in trades),
        non_stop_losses=sum(decimal(t['net_USDT']) < 0 and t.get('exit_reason') not in STOPS for t in trades))
    if trades and 'holding_hours' in trades[0]:
        fields.update(mean_holding_hours=mean([t['holding_hours'] for t in trades]),
            cash_capped_trades=sum(t['cash_capped'] in (True, 'True') for t in trades),
            normal_stop_cost_share_ge_half=sum(t['normal_stop_cost_share'] >= D('.5') for t in trades),
            same_entry_bar_stop=sum(t['same_entry_bar_stop'] for t in trades),
            same_entry_bar_stop_order_uncertain=sum(t['same_entry_bar_stop_order_uncertain'] for t in trades),
            dual_SL_TP_exit_bar=sum(t['dual_SL_TP_exit_bar'] for t in trades),
            gap_or_zero_volume_exposure=sum(t['holding_missing_bars'] > 0 or t['holding_zero_volume_bars'] > 0 for t in trades),
            stops=sum(t['exit_reason'] in STOPS for t in trades),
            mean_stop_holding_hours=mean([t['holding_hours'] for t in stops]),
            mean_confirmation_entry_price_drift_pct=mean([t['confirmation_entry_price_drift_pct'] for t in trades]),
            mean_entry_delay_from_registration_hours=mean([t['entry_delay_from_registration_hours'] for t in trades]),
            mean_RR_change_from_original_reference=mean([t['RR_change_from_original_reference'] for t in trades]),
            confirmation_entry_moved_further_in_side=sum(t['confirmation_entry_price_drift_pct'] > 0 for t in trades),
            logged_RR_not_actual_entry=sum(not t['logged_RR_matches_actual_entry_approx'] for t in trades),
            preentry_original_setup_absent=sum(not t['preentry_same_canonical_setup'] for t in trades),
            complete_excursion_stop_bounds=sum(t['excursion_status'] == 'complete_observed_OHLC_bounds' for t in stops),
            mean_stop_distance_ATR=mean([t['stop_distance_over_ATR14_1h'] for t in trades if t['stop_distance_over_ATR14_1h'] is not None]),
            mean_planned_net_target_RR=mean([t['planned_net_target_RR'] for t in trades]),
            gap_extra_loss_vs_normal_stop_USDT=sum(max(D(0), -t['net_USDT']-decimal(t['planned_risk'])) for t in stops if t['exit_reason'] == 'stop_gap'),
            stop_with_favorable_price_certain=sum(t['mfe_price_lower'] > 0 for t in stops),
            stop_with_favorable_price_only_possible=sum(t['mfe_price_lower'] == 0 and t['mfe_price_upper'] is not None and t['mfe_price_upper'] > 0 for t in stops),
            mean_stop_MFE_gross_R_lower=mean([t['mfe_gross_over_net_risk_R_lower'] for t in stops]),
            mean_stop_MFE_gross_R_upper=mean([t['mfe_gross_over_net_risk_R_upper'] for t in stops if t['mfe_gross_over_net_risk_R_upper'] is not None]),
            mean_stop_MAE_gross_R_lower=mean([t['mae_gross_over_net_risk_R_lower'] for t in stops]),
            mean_stop_MAE_gross_R_upper=mean([t['mae_gross_over_net_risk_R_upper'] for t in stops if t['mae_gross_over_net_risk_R_upper'] is not None]),
            stop_after_gross_1risk_certain=sum(t['exit_reason'] in STOPS and t['mfe_gross_over_net_risk_R_lower'] >= 1 for t in trades),
            stop_after_gross_1risk_only_possible=sum(t['exit_reason'] in STOPS and t['mfe_gross_over_net_risk_R_lower'] < 1 and t['mfe_gross_over_net_risk_R_upper'] is not None and t['mfe_gross_over_net_risk_R_upper'] >= 1 for t in trades))
    return fields
