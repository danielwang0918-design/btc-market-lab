"""Pinned P1-05 loss diagnosis. Reads archives; does not run or alter a strategy."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
import os
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal as D, localcontext
from pathlib import Path
from zipfile import ZipFile

import p1_candlestick_research as core
import p1_portfolio as portfolio
import p1_loss_diagnostics as diag
import historical_backtest as original


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row)) or ['no_rows']
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')


def read_csv(z, name, hashes):
    raw = z.read(name)
    hashes[name] = sha(raw)
    return list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))


def universes(symbols):
    return dict(all_eight=set(symbols), BTC={'BTCUSDT'}, other_seven=set(symbols)-{'BTCUSDT'},
                **{symbol: {symbol} for symbol in symbols})


def matches_features(row, features):
    mismatches = []
    for key in ('location', 'trend', 'volatility', 'pattern_ids', 'rvol_bin'):
        if row[key] != str(features[key]):
            mismatches.append(key)
    for key in ('rvol', 'atr'):
        a, b = row.get(key), features.get(key)
        if (a in ('', None)) != (b is None):
            mismatches.append(key)
        elif b is not None and abs(D(a)-D(str(b))) > D('1e-20')*max(D(1), abs(D(a))):
            mismatches.append(key)
    return mismatches


def audit_account(summary, trades, curve, signals):
    """Reconcile archived files without accepting their headline metrics on trust."""
    cash = D(1000)
    last_exit = -1
    enriched = []
    for row in trades:
        if int(row['entry_time_ms']) < last_exit:
            raise ValueError('Archived overlapping positions')
        if int(row['signal_time_ms']) > int(row['entry_time_ms']) or int(row['feature_cutoff_ms']) != int(row['entry_time_ms']):
            raise ValueError('Archived future signal or feature cutoff')
        if abs(D(row['equity_before'])-cash) > D('1e-12'):
            raise ValueError('Archived equity-before mismatch')
        if D(row['cash_required']) > cash+D('1e-12') or D(row['planned_risk']) > cash*D('.02')+D('1e-12'):
            raise ValueError('Archived unfunded size or excess modeled risk')
        if summary['model'] != 'hypothetical_1x_long_short' and row['side'] != 'long':
            raise ValueError('Unlabelled Spot short')
        c = diag.cost_decomposition(row)
        if abs(c['net_R']-D(row['r_multiple'])) > D('1e-12'):
            raise ValueError('Archived realized R mismatch')
        cash += c['net_USDT']
        if abs(cash-D(row['equity_after'])) > D('1e-12'):
            raise ValueError('Archived equity-after mismatch')
        last_exit = int(row['exit_time_ms'])
        enriched.append(c)
    peak = D(1000)
    dd = D(0)
    for point in curve:
        value = D(point['equity_usdt'])
        peak = max(peak, value)
        dd = max(dd, (peak-value)/peak*100)
    stats = diag.aggregate(enriched)
    if len(trades) != int(summary['trade_count']) or stats['wins'] != int(summary['wins']) or stats['losses'] != int(summary['losses']):
        raise ValueError('Archived outcome counts mismatch')
    if abs(dd-D(summary['maximum_drawdown_pct'])) > D('1e-12'):
        raise ValueError('Archived drawdown mismatch')
    if int(curve[0]['time_ms']) != portfolio.ms(summary['calendar_start_utc']) or int(curve[-1]['time_ms']) != portfolio.ms(summary['calendar_end_utc']):
        raise ValueError('Archived calendar mismatch')
    if summary['terminal_position_unresolved'] != 'False' or abs(cash-D(summary['final_equity_USDT'])) > D('1e-12') or abs(D(curve[-1]['equity_usdt'])-cash) > D('1e-12'):
        raise ValueError('Archived terminal valuation unresolved')
    events = Counter(r['event'] for r in signals)
    if events['entered'] != len(trades) or events['position_closed'] != len(trades):
        raise ValueError('Archived signal/trade count mismatch')
    return dict(trades=len(trades), signal_rows=len(signals), curve_rows=len(curve),
        all_cost_cash_R_counts_and_endpoints_match=True, maximum_drawdown_pct=dd)


def equity_diagnosis(curve):
    peak_value, peak_time = D(curve[0]['equity_usdt']), int(curve[0]['time_ms'])
    worst = dict(drawdown_pct=D(0), peak_time_ms=peak_time, trough_time_ms=peak_time)
    for p in curve:
        value, t = D(p['equity_usdt']), int(p['time_ms'])
        if value >= peak_value:
            peak_value, peak_time = value, t
        drawdown = (peak_value-value)/peak_value*100
        if drawdown > worst['drawdown_pct']:
            worst = dict(drawdown_pct=drawdown, peak_time_ms=peak_time, trough_time_ms=t, peak_USDT=peak_value, trough_USDT=value)
    recovery = next((int(p['time_ms']) for p in curve if int(p['time_ms']) > worst['trough_time_ms'] and D(p['equity_usdt']) >= worst.get('peak_USDT', peak_value)), None)
    return dict(**worst, peak_time_utc=core.utc(worst['peak_time_ms']),
        trough_time_utc=core.utc(worst['trough_time_ms']),
        recovery_time_utc=core.utc(recovery) if recovery is not None else '',
        unrecovered_by_calendar_end=recovery is None and worst['drawdown_pct'] > 0,
        peak_to_trough_hours=D(worst['trough_time_ms']-worst['peak_time_ms'])/diag.H)


def enrich_trade(row, model, policy, period, asset, signals, post_rows, time_rows):
    row = dict(row, policy=policy, period=period, model=model, coin_group=diag.coin_group(row['symbol']))
    row['trade_key'] = '|'.join((model, period, policy, row['symbol'], row['setup_id'], row['entry_time_ms']))
    entry, exit_time = int(row['entry_time_ms']), int(row['exit_time_ms'])
    features = asset.features(entry, D(row['raw_entry']), row['side'])
    mismatch = matches_features(row, features)
    confirmations = [r for r in signals if r['setup_id'] == row['setup_id'] and r['event'] == 'E_closed_confirmation' and int(r['time_ms']) == entry]
    confirmed = not policy.startswith('E') or any(int(r['confirmation_open_ms'])+diag.H <= entry for r in confirmations)
    if mismatch or not confirmed:
        raise ValueError('Archived as-of feature or E confirmation mismatch: '+str(mismatch))
    registrations = [r for r in signals if r['setup_id'] == row['setup_id'] and r['event'] == 'canonical_signal_registered' and int(r['time_ms']) <= entry]
    entered = next(r for r in signals if r['setup_id'] == row['setup_id'] and r['event'] == 'entered' and int(r['time_ms']) == entry)
    registered = max(registrations, key=lambda r: int(r['time_ms']))
    row.update(diag.confirmation_geometry(row, registered, entered))
    audit = dict(trade_key=row['trade_key'], entry_time_ms=entry, signal_time_ms=row['signal_time_ms'],
        last_feature_bar_close_ms=asset.times[asset.by_time[entry]-1]+diag.H,
        preentry_categories_recomputed_match=True, E_confirmation_closed_before_entry=confirmed)
    for frame in core.FRAMES:
        ma = asset.regimes[frame].at(entry)
        bos = asset.structures[frame].at(entry)
        row['MA_direction_'+frame] = ma['direction']
        row['BOS_direction_'+frame] = bos['direction']
        row['BOS_phase_'+frame] = bos['phase']
        row['BOS_age_hours_'+frame] = bos['age_hours']
        audit['MA_reference_close_ms_'+frame] = ma['reference_close_ms']
        audit['BOS_reference_confirm_ms_'+frame] = bos['reference_confirm_ms']
        if ma['reference_close_ms'] is not None and ma['reference_close_ms'] > entry or bos['reference_confirm_ms'] is not None and bos['reference_confirm_ms'] >= entry:
            raise ValueError('Future MTF context')
    if row['MA_direction_1h'] != row['trend']:
        raise ValueError('P1-03 1H regime adapter mismatch')
    time_rows.append(audit)
    row['MTF_MA_alignment'] = diag.mtf_alignment([row['MA_direction_'+f] for f in core.FRAMES], row['side'])
    row['MTF_BOS_alignment'] = diag.mtf_alignment([row['BOS_direction_'+f] for f in core.FRAMES], row['side'])
    snap = asset.snaps[asset.by_time[entry]-1]
    candidate = snap.get('candidate')
    row['preentry_canonical_integrity_ok'] = snap['integrity_ok']
    row['preentry_same_canonical_setup'] = bool(candidate and candidate['setup_id'] == row['setup_id'])
    row.update(diag.cost_decomposition(row))
    row['holding_hours'] = D(exit_time-entry)/diag.H
    row['holding_bucket'] = diag.holding_bucket(row['holding_hours'])
    row['signal_age_hours'] = D(entry-int(row['signal_time_ms']))/diag.H
    row['entry_UTC_session'] = ('00-08', '08-16', '16-24')[(entry//diag.H % 24)//8]
    row['entry_year'] = core.utc(entry)[:4]
    stop_pct = abs(D(row['raw_entry'])-D(row['stop']))/D(row['raw_entry'])*100
    row['stop_distance_pct'] = stop_pct
    row['stop_distance_bucket'] = diag.bucket(stop_pct, ('.5', '1', '2'))
    atr = features['atr']
    row['stop_distance_over_ATR14_1h'] = abs(D(row['raw_entry'])-D(row['stop']))/D(str(atr)) if atr is not None and atr > 0 else None
    row['stop_ATR_bucket'] = diag.bucket(row['stop_distance_over_ATR14_1h'], ('.5', '1', '2')) if row['stop_distance_over_ATR14_1h'] is not None else 'missing_or_zero_ATR'
    e, target, sign = D(row['raw_entry']), D(row['target']), D(1) if row['side'] == 'long' else D(-1)
    e_fill, t_fill = e*(1+sign*D('.0005')), target*(1-sign*D('.0005'))
    row['planned_net_target_RR'] = ((t_fill-e_fill)*sign-(t_fill+e_fill)*D('.001'))/(D(row['planned_risk'])/D(row['quantity']))
    row['target_RR_bucket'] = diag.bucket(row['planned_net_target_RR'], ('1.5', '2', '3'))
    row['cost_share_bucket'] = diag.bucket(row['normal_stop_cost_share'], ('.25', '.5'))
    row['risk_budget_deployed_fraction'] = D(row['planned_risk'])/D(row['risk_budget'])
    row['same_entry_bar_stop'] = row['exit_reason'] == 'stop' and exit_time == entry+diag.H
    row['same_entry_bar_stop_order_uncertain'] = row['same_entry_bar_stop'] and policy in ('A', 'B')
    exit_bar = asset.bar_index.at(exit_time-diag.H) if row['exit_reason'] not in diag.OPEN_EXITS else None
    row['dual_SL_TP_exit_bar'] = bool(exit_bar and (exit_bar.l <= D(row['stop']) and exit_bar.h >= target if row['side'] == 'long' else exit_bar.h >= D(row['stop']) and exit_bar.l <= target))
    row.update(diag.excursions(row, asset.bar_index))
    split_end = next(cut for cut in portfolio.CUTS if entry < cut)
    for hours in diag.POST_HOURS:
        p = diag.post_stop_window(row, asset.bar_index, hours, split_end)
        post_rows.append(dict(trade_key=row['trade_key'], model=model, policy=policy, period=period,
            symbol=row['symbol'], coin_group=row['coin_group'], entry_year=row['entry_year'], exit_reason=row['exit_reason'],
            recorded_stop_exit_utc=row['exit_time_utc'], observation_end_utc=core.utc(exit_time+hours*diag.H),
            censor_cutoff_utc=core.utc(split_end), **p))
    return row


def group_tables(trades, symbols):
    dimensions = dict(all=['all'], exit_reason=sorted({r['exit_reason'] for r in trades}),
        holding_bucket=['[0,1]', '(1,6]', '(6,24]', '(24,72]', '(72,+inf)'],
        trend=['bullish', 'bearish', 'transition_or_range', 'insufficient_prior_ma'],
        volatility=['low', 'normal', 'high', 'insufficient_prior_atr_distribution'],
        location=sorted({r['location'] for r in trades}),
        rvol_bin=['low', 'normal', 'high', 'insufficient_history', 'gap_warmup', 'zero_mean'],
        stop_distance_bucket=['[0,.5)', '[.5,1)', '[1,2)', '[2,+inf)'],
        stop_ATR_bucket=['[0,.5)', '[.5,1)', '[1,2)', '[2,+inf)', 'missing_or_zero_ATR'],
        target_RR_bucket=['[0,1.5)', '[1.5,2)', '[2,3)', '[3,+inf)'],
        cost_share_bucket=['[0,.25)', '[.25,.5)', '[.5,+inf)'],
        entry_UTC_session=['00-08', '08-16', '16-24'], entry_year=[str(y) for y in range(2020, 2027)],
        side=['long', 'short'],
        MTF_MA_alignment=['all_aligned', 'mixed_or_opposed', 'unknown'],
        MTF_BOS_alignment=['all_aligned', 'mixed_or_opposed', 'unknown'],
        preentry_canonical_integrity_ok=[True, False], preentry_same_canonical_setup=[True, False])
    for frame in core.FRAMES:
        dimensions['MA_direction_'+frame] = ['bullish', 'bearish', 'transition_or_range', 'insufficient_prior_ma', 'reference_data_gap_or_stale']
        dimensions['BOS_direction_'+frame] = ['bullish', 'bearish', 'no_confirmed_BOS_in_window', 'reference_data_gap_or_stale', 'reference_gap_in_last200']
        dimensions['BOS_phase_'+frame] = ['successive_same_direction_breaks', 'latest_break_changed_direction', 'insufficient_two_breaks']
    # Include every observed missingness category, never lose rows through a fixed vocabulary.
    for dim in dimensions:
        if dim != 'all':
            dimensions[dim] = list(dict.fromkeys([*dimensions[dim], *(r[dim] for r in trades)]))
    rows = []
    for model, period, policy in itertools.product(diag.MODELS, diag.PERIODS, diag.POLICIES):
        account = [t for t in trades if t['model'] == model and t['period'] == period and t['policy'] == policy]
        for universe, members in universes(symbols).items():
            subset = [t for t in account if t['symbol'] in members]
            for dim, labels in dimensions.items():
                for label in labels:
                    selected = subset if dim == 'all' else [t for t in subset if t[dim] == label]
                    statistics = diag.aggregate(selected)
                    all_stops = sum(t['exit_reason'] in diag.STOPS for t in subset)
                    all_other = len(subset)-all_stops
                    rows.append(dict(model=model, period=period, policy=policy, universe=universe,
                        dimension=dim, category=label, **statistics,
                        share_of_all_stop_exits_pct=D(statistics['stop_exits'])*100/all_stops if all_stops else None,
                        share_of_all_non_stop_exits_pct=D(statistics['trades']-statistics['stop_exits'])*100/all_other if all_other else None))
    return rows


OFFER_EVENTS = {'research_filter_rejected', 'entered', 'global_position_busy', 'spot_short_unavailable',
    'first_1000_cap', 'zero_volume_entry_unavailable', 'historical_filters_unavailable',
    'quantity_zero', 'below_exchange_minimum', 'invalid_prices_or_cash', 'invalid_risk'}


def filter_rows(signals, policy, period, assets):
    rows = []
    for row in signals:
        if row['event'] not in OFFER_EVENTS:
            continue
        t = int(row['time_ms'])
        f = assets[row['symbol']].features(t, D(row['entry_reference']), row['direction'])
        if row['event'] == 'research_filter_rejected' and matches_features(row, f):
            raise ValueError('Rejected signal as-of feature mismatch')
        state = diag.condition_state(f, row['direction'], policy)
        if state['pass_policy'] != (row['event'] != 'research_filter_rejected'):
            raise ValueError('Reconstructed C/D filter does not match source')
        rows.append(dict(period=period, policy=policy, symbol=row['symbol'],
            coin_group=diag.coin_group(row['symbol']), setup_id=row['setup_id'],
            offer_time_utc=row['time_utc'], offer_time_ms=t, side=row['direction'],
            source_event=row['event'], **f, **state))
    return rows


def filter_tables(rows, symbols):
    funnels, failures, masks = [], [], []
    for period, policy in itertools.product(diag.PERIODS, ('C', 'D')):
        order = ('pattern', 'rvol') if policy == 'C' else ('zone', 'ma', 'pattern', 'rvol', 'atr')
        for universe, members in universes(symbols).items():
            for side in ('all', 'long', 'short'):
                selected = [r for r in rows if r['period'] == period and r['policy'] == policy and r['symbol'] in members and (side == 'all' or r['side'] == side)]
                key = dict(period=period, policy=policy, universe=universe, side=side)
                survivors = selected
                funnels.append(dict(**key, stage='feature_stage_offers', signal_events=len(survivors)))
                for condition in order:
                    failed = [r for r in selected if not r[condition+'_ok']]
                    failures.append(dict(**key, condition=condition, evaluated_signal_events=len(selected),
                        failed_events=len(failed), first_failed_events=sum(r['first_failed_condition'] == condition for r in selected),
                        rvol_missing_events=sum(r['rvol_missing'] for r in selected) if condition == 'rvol' else ''))
                    survivors = [r for r in survivors if r[condition+'_ok']]
                    funnels.append(dict(**key, stage='after_'+condition, signal_events=len(survivors)))
                for event in ('entered', 'spot_short_unavailable', 'global_position_busy', 'historical_filters_unavailable'):
                    funnels.append(dict(**key, stage='source_event:'+event,
                        signal_events=sum(r['source_event'] == event for r in survivors)))
                for flags in itertools.product((False, True), repeat=len(order)):
                    failure_set = '|'.join(k for k, fail in zip(order, flags) if fail) or 'none'
                    masks.append(dict(**key, failed_conditions=failure_set,
                        signal_events=sum(r['failed_conditions'] == failure_set for r in selected)))
    return funnels, failures, masks


def post_tables(rows, symbols):
    results = []
    statuses = ('complete', 'split_or_calendar_censored', 'data_gap', 'zero_volume', 'not_stop')
    grouped = defaultdict(list)
    for r in rows:
        for universe in ('all_eight', r['coin_group'], r['symbol']):
            for year in ('all', r['entry_year']):
                grouped[(r['model'], r['period'], r['policy'], r['hours'], universe, year)].append(r)
    for model, period, policy, hours in itertools.product(diag.MODELS, diag.PERIODS, diag.POLICIES, diag.POST_HOURS):
        for universe, year in itertools.product(universes(symbols), ('all', *map(str,range(2020,2027)))):
            selected = grouped[(model,period,policy,hours,universe,year)]
            complete = [r for r in selected if r['status'] == 'complete']
            results.append(dict(model=model, period=period, policy=policy, hours=hours, universe=universe, entry_year=year,
                **{status+'_trades': sum(r['status'] == status for r in selected) for status in statuses},
                entry_recovered_or_better=sum(r['entry_recovered_or_better'] is True for r in complete),
                original_target_reached=sum(r['original_target_reached'] is True for r in complete),
                endpoint_favorable=sum(r['endpoint_favorable'] is True for r in complete),
                endpoint_adverse=sum(r['endpoint_adverse'] is True for r in complete),
                endpoint_flat=sum(r['endpoint_flat'] is True for r in complete),
                any_favorable_from_stop=sum(r['any_favorable_from_stop'] is True for r in complete),
                any_adverse_from_stop=sum(r['any_adverse_from_stop'] is True for r in complete),
                both_directions_order_unresolved=sum(r['both_directions_order_unresolved'] is True for r in complete)))
    return results


def run(source_zip, out):
    with localcontext() as context:
        context.prec = 50
        return _run(source_zip, out)


def _run(source_zip, out):
    out.mkdir(parents=True, exist_ok=True)
    code_root = Path(__file__).parent
    if sha(source_zip.read_bytes()) != diag.SOURCE_ZIP_SHA:
        raise ValueError('Pinned P1-05 archive SHA256 mismatch')
    if sha((code_root/'P1_06A_PROTOCOL.json').read_bytes()) != diag.PROTOCOL_SHA:
        raise ValueError('Diagnostic protocol changed after declaration')
    source_hashes, primary, filters, account_audits, signal_stats, equity_stats = {}, [], [], [], [], []
    post_rows, temporal_rows = [], []
    with ZipFile(source_zip) as z, tempfile.TemporaryDirectory(prefix='p1-06a-') as directory:
        root = Path(directory)
        data, tapes = root/'data', root/'tapes'
        data.mkdir(); tapes.mkdir()
        manifest = json.loads(z.read('results/p1_05_manifest.json'))
        diag.validate_manifest(manifest)
        for name, digest in manifest['code_sha256'].items():
            if sha((code_root/name).read_bytes()) != digest:
                raise ValueError('P1-05 or formal source file changed: '+name)
        lock = core.read_lock(code_root/'PROSPECTIVE_LOCK_V1.json')
        symbols = lock['symbols']
        with ZipFile(io.BytesIO(z.read('inputs/frozen-source.zip'))) as frozen:
            for name in lock['frozen_data_prefixes']:
                raw = frozen.read(name)
                if sha(raw) != lock['frozen_data_prefixes'][name]['sha256']:
                    raise ValueError('Frozen source hash mismatch')
                (data/name).write_bytes(raw)
        for symbol in symbols:
            raw = z.read(f'inputs/tapes/{symbol}.json.gz')
            if sha(raw) != manifest['source_sha256']['canonical_tapes'][symbol+'.json.gz']:
                raise ValueError('Canonical tape hash mismatch')
            (tapes/(symbol+'.json.gz')).write_bytes(raw)
        assets = {sym: portfolio.Asset(sym, data, tapes) for sym in symbols}
        quality = []
        for asset in assets.values():
            print('CONTEXT', asset.symbol, flush=True)
            asset.bar_index = diag.BarIndex(asset.bars)
            asset.regimes = {frame: diag.RegimeIndex(bars, frame, source=asset.reg if frame == '1h' else None) for frame, bars in asset.frames.items()}
            asset.structures = {}
            for frame, bars in asset.frames.items():
                step = core.FRAMES[frame]
                gaps = [b.open_ms-a.open_ms for a,b in zip(bars,bars[1:]) if b.open_ms-a.open_ms != step]
                quality.append(dict(symbol=asset.symbol, timeframe=frame, bars=len(bars),
                    zero_volume_bars=sum(b.v == 0 for b in bars), gap_segments=len(gaps),
                    missing_bars=sum(gap//step-1 for gap in gaps), duplicates_or_unclosed=0,
                    first_open_utc=core.utc(bars[0].open_ms), last_close_exclusive_utc=core.utc(bars[-1].open_ms+step),
                    sha256=manifest['source_sha256']['frozen_files'][asset.symbol+'_'+frame+'.csv']))
                source_rows = original._closed_rows(data/f'{asset.symbol}_{frame}.csv', asset.symbol, frame)
                events = []
                for segment in original._contiguous_segments(source_rows, frame):
                    for event in original._strategy_events(segment, frame):
                        if event['event_type'] == 'structure_break':
                            events.append(dict(confirmed_at_close_time=int(event['confirmed_at_close_time']),
                                direction=event['direction'], evidence_opens={int(e['open_time']) for e in json.loads(event['evidence_json'])}))
                asset.structures[frame] = diag.StructureIndex(bars, frame, events)
        summaries = read_csv(z, 'results/strategy_comparison.csv', source_hashes)
        expected = set(itertools.product(portfolio.MODELS, diag.PERIODS, portfolio.POLICIES))
        if len(summaries) != 120 or {(r['model'], r['period'], r['policy']) for r in summaries} != expected:
            raise ValueError('Source account matrix changed')
        for summary in summaries:
            model, period, policy = (summary[k] for k in ('model', 'period', 'policy'))
            prefix = f'results/runs/{model}__{period}__{policy}'
            trades = read_csv(z, prefix+'__trades.csv', source_hashes)
            curve = read_csv(z, prefix+'__equity.csv', source_hashes)
            signals = read_csv(z, prefix+'__signals.csv', source_hashes)
            audit = audit_account(summary, trades, curve, signals)
            account_audits.append(dict(model=model, period=period, policy=policy, **audit))
            equity_stats.append(dict(model=model, period=period, policy=policy, **equity_diagnosis(curve)))
            counts, unique = Counter(), defaultdict(set)
            for r in signals:
                for universe in ('all_eight', diag.coin_group(r['symbol']), r['symbol']):
                    key = (universe, r['direction'], r['event'])
                    counts[key] += 1
                    unique[key].add(r['setup_id'])
            signal_stats.extend(dict(model=model, period=period, policy=policy,
                universe=universe, side=side, event=event, event_rows=n,
                unique_setup_ids=len(unique[(universe, side, event)]))
                for (universe, side, event), n in sorted(counts.items()))
            if model in diag.MODELS and policy in diag.POLICIES:
                print('DIAGNOSE', model, period, policy, len(trades), flush=True)
                primary.extend(enrich_trade(t, model, policy, period, assets[t['symbol']], signals, post_rows, temporal_rows) for t in trades)
            if model == 'fractional_spot' and policy in ('C', 'D'):
                filters.extend(filter_rows(signals, policy, period, assets))
        source_hashes['results/p1_05_manifest.json'] = sha(z.read('results/p1_05_manifest.json'))
        groups = group_tables(primary, symbols)
        funnel, failures, masks = filter_tables(filters, symbols)
        post_summary = post_tables(post_rows, symbols)
        files = {'trade_diagnostics.csv': primary, 'group_diagnostics.csv': groups,
            'frozen_data_quality.csv': quality,
            'source_account_audit.csv': account_audits, 'rejection_event_counts.csv': signal_stats,
            'equity_drawdown_diagnostics.csv': equity_stats, 'temporal_feature_audit.csv': temporal_rows,
            'C_D_feature_stage_events.csv': filters, 'C_D_filter_funnel.csv': funnel,
            'C_D_condition_failures.csv': failures, 'C_D_joint_failure_masks.csv': masks,
            'post_stop_observations.csv': post_rows, 'post_stop_summary.csv': post_summary}
        contrasts = []
        for model, period, policy in itertools.product(diag.MODELS, diag.PERIODS, diag.POLICIES):
            for universe, members in universes(symbols).items():
                for stopped in (True, False):
                    subset = [t for t in primary if t['model'] == model and t['period'] == period and t['policy'] == policy and t['symbol'] in members and (t['exit_reason'] in diag.STOPS) == stopped]
                    contrasts.append(dict(model=model, period=period, policy=policy, universe=universe,
                        exit_group='stop_exit' if stopped else 'other_exit', **diag.aggregate(subset)))
        files['stop_vs_non_stop_context.csv'] = contrasts
        overlap = []
        for model, a, b in itertools.product(diag.MODELS, diag.POLICIES, diag.POLICIES):
            pa = [t for t in primary if t['model'] == model and t['period'] == 'continuous' and t['policy'] == a]
            pb = [t for t in primary if t['model'] == model and t['period'] == 'continuous' and t['policy'] == b]
            ka = {(t['symbol'], t['setup_id']) for t in pa}
            kb = {(t['symbol'], t['setup_id']) for t in pb}
            ea = {(t['symbol'], t['setup_id'], t['entry_time_ms']) for t in pa}
            eb = {(t['symbol'], t['setup_id'], t['entry_time_ms']) for t in pb}
            overlap.append(dict(model=model, policy_a=a, policy_b=b, shared_setup_ids=len(ka & kb), identical_entries=len(ea & eb)))
        files['policy_sample_overlap.csv'] = overlap
        for name, rows in files.items():
            write_csv(out/name, rows)
        records = evidence_registry(groups, post_summary, funnel)
        write_csv(out/'evidence_registry.csv', records)
        write_json(out/'source_manifest_copy.json', manifest)
        plots(out, groups)
        chinese_report(out, summaries, groups, post_summary, funnel, failures, signal_stats, records, quality)
        chinese_appendix(out, groups, symbols)
        output_hashes = {p.name: sha(p.read_bytes()) for p in sorted(out.iterdir()) if p.is_file() and p.name != 'p1_06a_manifest.json'}
        result = dict(version='p1-06a-loss-diagnostics-v2-user-stop-priority', current_commit=os.getenv('GITHUB_SHA'),
            source_commit=diag.SOURCE_COMMIT, source_run=43879737194, source_artifact=14232136572,
            source_archive_sha256=diag.SOURCE_ZIP_SHA, protocol_sha256=diag.PROTOCOL_SHA,
            source_code_unchanged=True, source_accounts_verified=len(account_audits),
            source_csv_and_manifest_sha256=source_hashes,
            frozen_source_sha256=manifest['source_sha256']['frozen_files'],
            frozen_candle_count=sum(len(bars) for a in assets.values() for bars in a.frames.values()),
            frozen_zero_volume_bars=sum(r['zero_volume_bars'] for r in quality),
            frozen_gap_segments=sum(r['gap_segments'] for r in quality),
            frozen_missing_source_bars=sum(r['missing_bars'] for r in quality),
            primary_trade_rows_including_repeated_period_accounts=len(primary),
            continuous_policy_trade_rows=sum(t['period'] == 'continuous' for t in primary),
            temporal_feature_rows_verified=len(temporal_rows), feature_mismatches=0,
            feature_stage_events=len(filters), new_OOS=False, new_strategy_implemented=False,
            backtest_rerun_or_parameters_changed=False, live_execution=False,
            code_sha256={name: sha((code_root/name).read_bytes()) for name in ('p1_loss_diagnostics.py', 'p1_loss_study.py')},
            output_sha256=output_hashes,
            inference='descriptive_associations_only_not_causal_effects_independent_samples_or_new_OOS')
        write_json(out/'p1_06a_manifest.json', result)
        return result


def group_row(groups, model, policy, period='continuous', universe='all_eight', dimension='all', category='all'):
    return next(r for r in groups if (r['model'], r['policy'], r['period'], r['universe'], r['dimension'], r['category']) == (model, policy, period, universe, dimension, category))


def evidence_registry(groups, post, funnel):
    records = []
    definitions = [
        ('raw_direction_loss', 'net_losses_already_raw_price_loss', '已確認算式', '原始出入場報價方向已造成虧損；不是單純費用翻負'),
        ('cost_flipped_gain', 'net_losses_cost_flipped_raw_gain', '已確認算式', '固定既有交易與數量時，原始價差獲利被費用及滑價翻為淨虧損'),
        ('narrow_stop_cost_share', 'normal_stop_cost_share_ge_half', '已確認模型特性', '正常停損模型風險至少一半來自成本；不表示加寬停損有效'),
        ('same_bar_stop', 'same_entry_bar_stop_order_uncertain', '模型不確定性', 'A/B 限價代理入場與停損同棒，OHLC不能證明先入場後停損'),
        ('dual_touch', 'dual_SL_TP_exit_bar', '模型不確定性', '退出棒SL/TP同觸，主模型stop-first不能還原實際順序'),
        ('pre_stop_favorable', 'stop_with_favorable_price_certain', '事後描述', '止損前確定持倉區間有利波動下界>0；不是曾能可靠獲利成交'),
        ('pre_stop_1risk', 'stop_after_gross_1risk_certain', '事後描述', '止損前有利價格變化下界至少一個正常含成本風險單位；不是淨獲利1R')]
    definitions.append(('logged_RR_context', 'logged_RR_not_actual_entry', '已確認介面語意限制',
        '來源訊號net_RR仍為候選參考，與實際入場及SL重算的同版近似RR不同；帳務風險仍以實際價位計算'))
    for model, policy in itertools.product(diag.MODELS, diag.POLICIES):
        r = group_row(groups, model, policy)
        for cause, field, status, meaning in definitions:
            records.append(dict(model=model, policy=policy, period='continuous', universe='all_eight',
                evidence_id=cause, evidence_type=status, affected_trades=r.get(field, 0),
                denominator_trades=r['trades'], denominator_stop_exits=r['stop_exits'],
                meaning=meaning, causal_effect_identified=False))
        for hours in diag.POST_HOURS:
            p = next(x for x in post if x['model'] == model and x['policy'] == policy and x['period'] == 'continuous' and x['universe'] == 'all_eight' and x['entry_year'] == 'all' and x['hours'] == hours)
            records.append(dict(model=model, policy=policy, period='continuous', universe='all_eight',
                evidence_id=f'post_stop_{hours}h_TP1', evidence_type='事後描述',
                affected_trades=p['original_target_reached'], denominator_trades=p['complete_trades'],
                meaning='止損後完整固定窗口達到原TP1或更有利價格；不是假設延後止損即可實現的報酬', causal_effect_identified=False))
    for policy in ('C', 'D'):
        def n(stage):
            return next(r['signal_events'] for r in funnel if r['period'] == 'continuous' and r['policy'] == policy and r['universe'] == 'all_eight' and r['side'] == 'all' and r['stage'] == stage)
        records.append(dict(model='fractional_spot', policy=policy, period='continuous', universe='all_eight',
            evidence_id='zero_trade_feature_funnel', evidence_type='已確認訊號流程',
            affected_signal_events=n('feature_stage_offers')-n('after_rvol' if policy == 'C' else 'after_atr'),
            denominator_signal_events=n('feature_stage_offers'),
            meaning='篩選失敗事件數；唯一通過者的現貨空頭限制另列，訊號事件不等於成交或獨立樣本', causal_effect_identified=False))
    for name, meaning in (
        ('independent_structure', '獨立結構訊號是否提供不同於共用OB/目標的資訊，需新前瞻資料驗證'),
        ('harmonics', '諧波需因果pivot確認及固定比例規格；本批沒有標籤，無法估計影響筆數'),
        ('ABCDE', 'ABCDE需先驗證波浪標籤可重現性與確認延遲；本批沒有標籤，無法歸因')):
        records.append(dict(evidence_id=name, evidence_type='待驗證假設', affected_trades='',
            meaning=meaning, causal_effect_identified=False))
    return records


def plots(out, groups):
    os.environ.setdefault('MPLCONFIGDIR', '/tmp/p1-06a-mpl')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    for model in diag.MODELS:
        fig, axes = plt.subplots(1, 2, figsize=(13, 5))
        x = list(range(len(diag.POLICIES)))
        for k, (key, label) in enumerate((('raw_gross_USDT', 'Fixed-trade raw price PnL'), ('fees_USDT', 'Fees'), ('slippage_USDT', 'Slippage'), ('net_USDT', 'Net PnL'))):
            values = [float(group_row(groups, model, policy)[key])*(-1 if key in ('fees_USDT', 'slippage_USDT') else 1) for policy in diag.POLICIES]
            axes[0].bar([i+(k-1.5)*.19 for i in x], values, width=.18, label=label)
        axes[0].set(xticks=x, xticklabels=diag.POLICIES, ylabel='USDT', title='Accounting decomposition, not a costless replay')
        axes[0].legend(fontsize=8)
        for j, universe in enumerate(('BTC', 'other_seven')):
            values = [float(group_row(groups, model, p, universe=universe)['stop_exit_rate_pct'] or 0) for p in diag.POLICIES]
            axes[1].bar([i+(j-.5)*.35 for i in x], values, width=.34, label=universe)
        axes[1].set(xticks=x, xticklabels=diag.POLICIES, ylabel='Stop exit rate (%)', title='Different policy samples; descriptive only')
        axes[1].legend()
        for ax in axes:
            ax.axhline(0, color='gray', linewidth=.6)
            ax.grid(axis='y', alpha=.2)
        fig.suptitle('P1-06A offline failure diagnosis | '+model)
        fig.tight_layout(); fig.savefig(out/f'diagnosis_{model}.png', dpi=140); plt.close(fig)


def chinese_report(out, source, groups, post, funnel, failures, signals, evidence, quality):
    def fmt(value, digits=3):
        return '—' if value is None else f'{float(value):.{digits}f}'
    def fraction(n, denominator):
        return f'{n}/{denominator}（{fmt(D(n)*100/denominator, 2)}%）' if denominator else '0/0（無法判斷）'
    lines = ['# P1-06A｜高止損率與虧損根因診斷', '',
        '**本階段不修改策略或門檻，不計算新的策略績效，不產生實盤委託。**', '',
        '## 方法與適用範圍', '',
        f'固定來源：P1-05 commit `{diag.SOURCE_COMMIT}`，Run 43879737194／Artifact 14232136572；ZIP SHA256 `{diag.SOURCE_ZIP_SHA}`。先提交診斷規格，依使用者補充在完整分析前固定6／12／24／48小時窗口及多週期方向方法。',
        '逐一讀取並核對120組來源的交易、拒絕訊號、資金曲線及分段結果；本報告詳析A/B/E_ob/E_2b。兩種主模型分開：fractional_spot為受現金限制的條件式純多；hypothetical_1x_long_short為含未驗證空頭的假想模型，缺借幣資格與成本，不是現貨空單績效。',
        '所有時段來源同為2020-10-15～2026-11-16 UTC；Train截止2023、Validation截止2025。連續帳戶與分段帳戶會重複觀察同一交易，策略也共享訊號，不可加總成獨立樣本。2025–2026 Test已開啟，沒有新OOS。',
        '止損率＝exit_reason為stop或stop_gap的交易數／全部已出場交易；實際虧損率＝淨PnL<0的交易數／全部交易。時間到期、完整性或日曆退出仍可能虧損，兩者不能混稱。',
        '所有下列差異都是固定樣本的描述／算式核對；不同政策交易集合、曝險、時間與單倉競爭不同，不能識別加入某條件的因果效果。', '']
    lines += [f"八幣24個來源共{sum(q['bars'] for q in quality):,}根；零量{sum(q['zero_volume_bars'] for q in quality)}根、缺口{sum(q['gap_segments'] for q in quality)}段／缺{sum(q['missing_bars'] for q in quality)}個來源棒。各檔SHA256與已收盤時戳均符合P0 lock；缺口不填造，詳見frozen_data_quality.csv。", '']
    for model in diag.MODELS:
        lines += [f'## {model}｜止損與成本總覽', '',
            '| 政策 | 交易數 | 止損數／率 | 淨虧損數／率 | 正常／跳空SL | 非SL虧損 | 平均淨R |',
            '|---|---:|---|---|---|---:|---:|']
        for policy in diag.POLICIES:
            r=group_row(groups,model,policy)
            lines.append(f"| {policy} | {r['trades']} | {fraction(r['stop_exits'],r['trades'])} | {fraction(r['losses'],r['trades'])} | {r['normal_stop_exits']}／{r['gap_stop_exits']} | {r['non_stop_losses']} | {fmt(r['mean_net_R'],6)} |")
        lines += ['', '| 政策 | 固定既有交易原始價差USDT | 費用 | 滑價 | 淨PnL | 原始方向已虧損 | 原始獲利被成本翻負 |', '|---|---:|---:|---:|---:|---:|---:|']
        for policy in diag.POLICIES:
            r=group_row(groups,model,policy)
            lines.append(f"| {policy} | {fmt(r['raw_gross_USDT'])} | {fmt(r['fees_USDT'])} | {fmt(r['slippage_USDT'])} | {fmt(r['net_USDT'])} | {r['net_losses_already_raw_price_loss']} | {r['net_losses_cost_flipped_raw_gain']} |")
        lines += ['', '此成本分解固定原來的數量、交易集合和出入場報價，原始價差不是重新跑過的無成本策略。成本對所有交易有影響，但只有「原始獲利被翻負」的筆數可直接歸為算式上的勝負翻轉；原始方向虧損仍不能據此判定是OB、方向還是進場時機造成。', '',
            '### 高止損率的集中情境（事後描述）', '',
            '| 政策 | SL數 | ≤6h的SL | 入場SL距離<1 ATR的交易／其中SL | 入場同棒SL順序不明 |',
            '|---|---:|---|---|---:|']
        for policy in diag.POLICIES:
            r=group_row(groups,model,policy)
            short=[group_row(groups,model,policy,dimension='holding_bucket',category=c) for c in ('[0,1]','(1,6]')]
            narrow=[group_row(groups,model,policy,dimension='stop_ATR_bucket',category=c) for c in ('[0,.5)','[.5,1)')]
            lines.append(f"| {policy} | {r['stop_exits']} | {fraction(sum(x['stop_exits'] for x in short),r['stop_exits'])} | {sum(x['trades'] for x in narrow)}／{sum(x['stop_exits'] for x in narrow)} | {r.get('same_entry_bar_stop_order_uncertain',0)} |")
        lines += ['', '持倉時數是出場後才能知道的結果；較長持有組本來就排除了早期止損，是存活／條件化偏差。不能把長持有組SL率較低解讀成「強制延長持倉有效」。SL/ATR分組也不是停損距離的控制實驗，不挑選其中報酬較佳的區間。', '',
            '### BTC與其他七幣分開', '',
            '| 政策 | 範圍 | 筆數 | 止損數／率 | 淨虧損率% | 平均淨R |', '|---|---|---:|---|---:|---:|']
        for policy, universe in itertools.product(diag.POLICIES, ('BTC','other_seven')):
            r=group_row(groups,model,policy,universe=universe)
            lines.append(f"| {policy} | {universe} | {r['trades']} | {fraction(r['stop_exits'],r['trades'])} | {fmt(r['actual_loss_rate_pct'])} | {fmt(r['mean_net_R'],6)} |")
        for universe in ('all_eight','BTC','other_seven'):
            lines += ['', f'### 分段穩定性｜{universe}', '', '| 政策 | Train n／SL率／淨R | Validation n／SL率／淨R | 已開啟Test n／SL率／淨R |', '|---|---|---|---|']
            for policy in diag.POLICIES:
                cells=[]
                for period in ('train','validation','opened_test'):
                    r=group_row(groups,model,policy,period,universe)
                    cells.append(f"{r['trades']}／{fmt(r['stop_exit_rate_pct'],2)}%／{fmt(r['mean_net_R'],6)}")
                lines.append('| '+policy+' | '+' | '.join(cells)+' |')
        lines += ['', '### 持倉時間、ATR距離與進場RR', '',
            '| 政策 | 止損平均持有h | 全組平均SL／事前ATR14 | 全組平均目標淨RR | 成本占正常停損風險≥50% | 同入場棒SL且順序不明 | SL/TP退出棒同觸 | 跳空較正常SL額外虧損USDT |',
            '|---|---:|---:|---:|---:|---:|---:|---:|']
        for policy in diag.POLICIES:
            r=group_row(groups,model,policy)
            lines.append(f"| {policy} | {fmt(r.get('mean_stop_holding_hours'))} | {fmt(r.get('mean_stop_distance_ATR'))} | {fmt(r.get('mean_planned_net_target_RR'))} | {r.get('normal_stop_cost_share_ge_half',0)} | {r.get('same_entry_bar_stop_order_uncertain',0)} | {r.get('dual_SL_TP_exit_bar',0)} | {fmt(r.get('gap_extra_loss_vs_normal_stop_USDT'))} |")
        lines += ['', '| 政策 | 登記到入場平均h | 比原候選朝交易方向更遠的入場 | 平均價格位移% | 相對原參考平均淨RR變化 | 訊號RR不是實際入場RR |', '|---|---:|---|---:|---:|---:|']
        for policy in diag.POLICIES:
            r=group_row(groups,model,policy)
            lines.append(f"| {policy} | {fmt(r.get('mean_entry_delay_from_registration_hours'))} | {fraction(r.get('confirmation_entry_moved_further_in_side',0),r['trades'])} | {fmt(r.get('mean_confirmation_entry_price_drift_pct'))} | {fmt(r.get('mean_RR_change_from_original_reference'))} | {r.get('logged_RR_not_actual_entry',0)} |")
        lines += ['', '價格位移是確認等待後的幾何變化，與ledger已扣的5bps成交滑價不同，沒有再扣一遍。來源訊號net_RR沿用候選參考價；本診斷另以實際入場與SL重算，兩者不混用。比較同版近似成本公式後才標示顯示RR不一致，避免把P0費率加總近似與P1-05按滑價成交價計費的微小差異當成時機問題。原實際部位風險及E最小RR檢查沒有被本階段改寫。']
        for dimension, title in (('exit_reason','退出原因：正常SL／開盤跳空SL／缺口首次報價／其他'),('holding_bucket','持倉時數分布'),('stop_ATR_bucket','入場SL／事前ATR14'),('target_RR_bucket','入場目標淨RR'),('volatility','事前ATR波動環境'),('MTF_MA_alignment','入場多週期MA200方向'),('MTF_BOS_alignment','入場多週期原BOS方向'),('BOS_phase_1h','入場1H結構階段代理')):
            labels=list(dict.fromkeys(r['category'] for r in groups if r['dimension']==dimension))
            lines += ['', f'### {title}：止損與非止損比較', '', '| 政策 | 類別 | 總筆數 | SL數／率 | 占全部SL% | 占全部非SL% |', '|---|---|---:|---|---:|---:|']
            for policy,label in itertools.product(diag.POLICIES,labels):
                r=group_row(groups,model,policy,dimension=dimension,category=label)
                lines.append(f"| {policy} | {label} | {r['trades']} | {fraction(r['stop_exits'],r['trades'])} | {fmt(r['share_of_all_stop_exits_pct'])} | {fmt(r['share_of_all_non_stop_exits_pct'])} |")
        lines += ['', '### 止損前MFE／MAE', '',
            '均為原始報價的有利／不利變化，除以該交易正常含成本風險／數量；不是已實現淨獲利R。下界只用模型出入場報價與確定持倉的完整棒；上界包含可能在入場前或出場後出現的極值。缺棒或零量時不提供真正上界。A/B為棒內入場代理，E為下一根開盤；SL/TP棒內次序不假定已知。', '',
            '| 政策 | SL數／完整上下界數 | 有利變化下界>0 | 僅上界顯示可能有利 | 有利下界≥1風險單位 | 平均MFE下／上界 | 平均MAE下／上界 |',
            '|---|---|---|---|---|---|---|']
        for policy in diag.POLICIES:
            r=group_row(groups,model,policy)
            lines.append(f"| {policy} | {r['stop_exits']}／{r.get('complete_excursion_stop_bounds',0)} | {fraction(r.get('stop_with_favorable_price_certain',0),r['stop_exits'])} | {fraction(r.get('stop_with_favorable_price_only_possible',0),r['stop_exits'])} | {fraction(r.get('stop_after_gross_1risk_certain',0),r['stop_exits'])} | {fmt(r.get('mean_stop_MFE_gross_R_lower'))}／{fmt(r.get('mean_stop_MFE_gross_R_upper'))} | {fmt(r.get('mean_stop_MAE_gross_R_lower'))}／{fmt(r.get('mean_stop_MAE_gross_R_upper'))} |")
        lines += ['', '### 止損後固定窗口：回升、續逆向與原TP1', '',
            '分母僅為窗口完整且無零量／缺口的SL交易；跨Train／Validation／Test邊界者剔除並列數，連續帳戶也不跨界取未來。期末朝原方向／期末續逆向／持平是相對模型停損退出原始報價的互斥端點分類，不等同整段只朝單一方向。窗口內有利與不利極值可以同時發生；達到TP1價位不保證能成交，也不證明延後或放寬SL更好。', '',
            '| 政策 | h | 完整／SL數 | 期末朝原方向 | 期末續逆向 | 期末持平 | 曾回原入場或更好 | 曾達原TP1 | 兩向皆有／路徑未定 | 邊界／缺口／零量 |',
            '|---|---:|---|---|---|---|---|---|---|---|']
        for policy,hours in itertools.product(diag.POLICIES,diag.POST_HOURS):
            p=next(r for r in post if r['model']==model and r['policy']==policy and r['period']=='continuous' and r['universe']=='all_eight' and r['entry_year']=='all' and r['hours']==hours)
            n=p['complete_trades']; total=n+p['split_or_calendar_censored_trades']+p['data_gap_trades']+p['zero_volume_trades']
            cells=[fraction(p[k],n) for k in ('endpoint_favorable','endpoint_adverse','endpoint_flat','entry_recovered_or_better','original_target_reached','both_directions_order_unresolved')]
            lines.append(f"| {policy} | {hours} | {n}/{total} | "+' | '.join(cells)+f" | {p['split_or_calendar_censored_trades']}／{p['data_gap_trades']}／{p['zero_volume_trades']} |")
    lines += ['', '## C／D零交易：固定條件的完整漏斗', '',
        '以下為fractional_spot的連續帳戶。分母是進入特徵篩選的訊號機會，不是已成交交易。先後篩選只用事前固定順序作帳務歸類；各條件可能同時失敗，完整交集位元遮罩及每條件失敗數另存，沒有因果優先次序。', '',
        '| 政策 | 方向 | 階段 | 事件數 |', '|---|---|---|---:|']
    for r in funnel:
        if r['period']=='continuous' and r['universe']=='all_eight':
            lines.append(f"| {r['policy']} | {r['side']} | {r['stage']} | {r['signal_events']} |")
    lines += ['', '| 政策 | 條件 | 已評估機會 | 所有失敗數（可重疊） | 固定順序首個失敗數 |', '|---|---|---:|---:|---:|']
    for r in failures:
        if r['period']=='continuous' and r['universe']=='all_eight' and r['side']=='all':
            lines.append(f"| {r['policy']} | {r['condition']} | {r['evaluated_signal_events']} | {r['failed_events']} | {r['first_failed_events']} |")
    lines += ['', 'C/D在純多模型各0筆；假想多空各1筆。不能以放寬門檻補造交易，也不能把唯一空頭當作Spot可執行績效。所有原始候選、未觸及／失效／等待／拒絕事件有完整來源ZIP；rejection_event_counts.csv保留各模型、時段、幣種、方向的事件數與setup數。', '',
        '## 共用機制：能確認與不能確認的事', '',
        '- A/B以原BOS＋OB、OB邊界SL及原swing TP1為共用機制；E使用額外收盤確認，E_2b的SL另有預先固定的0.1 ATR緩衝。這些是原規則，不在本階段改動。止損相對ATR、目標RR、等待時間與市場方向的關聯見完整逐筆及分組CSV。',
        '- MA方向採P1-03固定MA200及20棒斜率，分別在已收盤1H／4H／1D重算。BOS方向重用未改P0的2L2R事件，必須已確認且證據仍在前200棒窗口。MA過渡／盤整與BOS最近方向改變是不同代理，不能宣稱已精確分辨整理、趨勢反轉或吸收。缺資料一律未知。',
        '- preentry_same_canonical_setup／preentry_canonical_integrity_ok逐筆揭露等待確認後原候選是否仍存在。原P0會排除已被回踩的OB，因此E確認後舊候選消失是預期可發生的規則差異，不直接判定為錯誤或過期。MTF BOS是否仍同向另列。某組較差只代表不同樣本的關聯，不能據此認定晚確認、MA200或2B本身導致虧損。',
        '- 正常SL、跳空SL、非SL虧損分開；含成本2%只是模型風險預算。由價格與成本恆等式可識別費用拖累，不能把未實測的加寬止損、縮短目標或改方向稱為有效修正。', '',
        '## 資料洩漏、模型限制與樣本偏差', '',
        '- 逐筆重算原入場特徵，確認時間截止與E已收盤確認；新增MTF參考時間均≤入場。這些檢查沒有發現特徵時間違規，不等於證明完整執行模型毫無偏差。MFE／MAE與止損後窗口明確是未來結果，沒有輸入原策略。',
        '- OHLC代理成交依整棒高低判斷觸及、並拒絕target與zone同棒次序不明者；這是保守執行建模，具有事件選擇限制，不能當成當時已知訊號或可現場重現的委託流程。A/B同棒先觸SL後才進入OB的可能性不能排除。',
        '- 停損正常出場記在棒末，只能知道棒內曾觸及。停損後窗口從記錄時間起算，排除退出棒餘下路徑，不宣稱距實際盤中停損精確6／12／24／48小時。跳空／缺口出場為第一可見開盤；無逐筆或更細資料。',
        '- 歷史LOT_SIZE／MIN_NOTIONAL、點差深度、借幣資格及費用缺失；嚴格Spot來源0筆。所有條件式成交結果、估值回撤與假想空頭都保留此限制。',
        '- 固定現存八幣有存活者／選幣偏差；單倉會因同時訊號與幣種排序改變交易集合。政策共享setup、時段帳戶重複、行情序列與幣種相關，沒有獨立樣本顯著性推論。',
        '- 完整零交易組、缺口／零量、邊界刪失與不可分類值都保留。小樣本只列筆數，不把極端比例視為穩定結論。', '',
        '## 下一階段優先順序：只列待驗證假設，不實作', '',
        '1. 先驗證訊號時間／成交可觀測性及成本可承受性：針對高成本占比、同棒入場SL與原候選已變化的樣本建立前瞻記錄。不得從本次Test挑ATR、SL或RR最佳門檻。',
        '2. 獨立結構進場假設：檢驗確認後資訊是否仍新鮮、原swing TP1是否仍具足夠距離，與OB邊SL是否代表有效結構失效。先登記事件時間、入場／SL／TP／失效與成本規格，再用全新資料；不強制所有指標同時成立。',
        '3. 諧波：專案已有pattern_research.py／PATTERN_RESEARCH.md的已確認XABCD候選標註，不能當成新獨立OOS或策略。下一階段先對帳P1-05的來源與可用時間，再事前鎖定研究規格；禁止用後來形成的D點回畫早期訊號。本次P1-05 ledger未附這類標籤，沒有執行新配對或估計會改善多少筆。',
        '4. ABCDE波浪：既有粗略0-1-2-3-4-5候選不等於ABCDE修正浪。先固定可重現的分段及標籤規則，驗證不同標註者／程式的一致性與當下可知性，再談前瞻訊號。不可事後選漂亮波浪解釋輸贏。',
        '驗收結論：本階段交付失敗診斷、可重現證據與限制。尚未識別任何條件的因果修正效果，也未證明新訊號具正期望；沒有策略參數修改或自動交易功能。', '',
        '## 可重現輸出', '',
        '- trade_diagnostics.csv：兩主模型、四政策、四時段全部逐筆成本、SL/ATR、RR、MFE/MAE界限與原候選狀態。',
        '- group_diagnostics.csv及P1_06A_TABLES_ZH.md：模型、政策、時段、BTC／其餘七幣／各幣、進場UTC年度、方向、三週期MA/BOS、ATR、RR、持有期等固定分類，包含零組。',
        '- stop_vs_non_stop_context.csv、post_stop_observations.csv／summary.csv：止損與其他退出比較及四個完整固定窗口／所有刪失。',
        '- C_D_*：逐機會因果特徵重算、固定順序漏斗、各條件失敗及完整交集。',
        '- rejection_event_counts.csv、equity_drawdown_diagnostics.csv、source_account_audit.csv：120組來源拒絕事件、共同日曆資金曲線診斷與獨立核對。',
        '- temporal_feature_audit.csv、policy_sample_overlap.csv、evidence_registry.csv、p1_06a_manifest.json：時間、重複樣本、事實／假設與全部SHA256。',
        '- Artifact保存原始P1-05 ZIP，含120組完整來源CSV及20張資金曲線PNG；本次不改寫任何來源交易。','']
    (out/'P1_06A_REPORT_ZH.md').write_text('\n'.join(lines), encoding='utf-8')


def chinese_appendix(out, groups, symbols):
    def fmt(value):
        return '—' if value is None else f'{float(value):.4f}'
    lines = ['# P1-06A｜逐幣、年度與1H／4H／1D方向完整表', '',
        '年度依進場UTC年分類，與P1-05依出場年彙總已實現損益的表用途不同。各時段帳戶均獨立起始，不能把連續與分段樣本相加。所有分類保留零交易與未知值；斜線依序為筆數／止損數／止損率%／淨虧損率%／平均淨R。MA與BOS方向定義不同，不能混稱市場真實方向。', '']
    for model, period in itertools.product(diag.MODELS, diag.PERIODS):
        lines += [f'## {model}｜{period}', '', '| 幣種 | A | B | E_ob | E_2b |', '|---|---|---|---|---|']
        def cell(r):
            return f"{r['trades']}／{r['stop_exits']}／{fmt(r['stop_exit_rate_pct'])}／{fmt(r['actual_loss_rate_pct'])}／{fmt(r['mean_net_R'])}"
        for symbol in symbols:
            lines.append('| '+symbol+' | '+' | '.join(cell(group_row(groups,model,p,period,symbol)) for p in diag.POLICIES)+' |')
        for dimension in ('entry_year', *(f'MA_direction_{f}' for f in core.FRAMES), *(f'BOS_direction_{f}' for f in core.FRAMES), *(f'BOS_phase_{f}' for f in core.FRAMES), 'preentry_same_canonical_setup', 'preentry_canonical_integrity_ok'):
            labels=list(dict.fromkeys(r['category'] for r in groups if r['dimension']==dimension))
            lines += ['', f'### 八幣｜{dimension}', '', '| 類別 | A | B | E_ob | E_2b |', '|---|---|---|---|---|']
            for category in labels:
                lines.append('| '+str(category)+' | '+' | '.join(cell(group_row(groups,model,p,period,dimension=dimension,category=category)) for p in diag.POLICIES)+' |')
    (out/'P1_06A_TABLES_ZH.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-zip', required=True, type=Path)
    parser.add_argument('--out-dir', required=True, type=Path)
    args = parser.parse_args()
    run(args.source_zip, args.out_dir)


if __name__ == '__main__':
    main()
