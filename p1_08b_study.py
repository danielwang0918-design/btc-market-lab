"""Read frozen events, compute bounded price labels and exploratory associations."""
from __future__ import annotations
import argparse,csv,hashlib,io,itertools,json,math,os
from collections import defaultdict,Counter
from decimal import Decimal as D,localcontext
from pathlib import Path
from zipfile import ZipFile
import p1_candlestick_research as core
import p1_loss_study as files
import p1_stop_study as calendars
import p1_portfolio as money
import p1_08a_diagnostics as stats
import p1_08b_paths as paths

BASE='973791ea5be0170a6b41edfeb9f6756426314539'
SOURCE_SHA='6f7120ca7c3866a2126afab4745cb8440f7562a45dc96ffb142a8fdd030e2380'
PROTOCOL_SHA='b84ef2e9efbd868115af895d51560fa864c12c1ed16d052fd7d372a4120bc655'
TRADE_FIELDS=('event_id','policy','symbol','side','registration_ms','entry_time_ms','exit_time_ms','exit_bar_ms','reserved_until_ms',
    'raw_entry','stop','target','raw_exit','exit_reason','market_at_open','scenario','quantity','planned_risk','risk_budget',
    'net_pnl_usdt','r_multiple','fees_usdt','slippage_usdt','equity_before','equity_after','model','period',
    'path_dependent_outcome','gap_or_zero_volume_path_unknown')
CONTEXT_FIELDS=tuple(k+'_'+f for f in core.FRAMES for k in ('reference_close_ms','MA','ER20','ER_state','ATR14','ATR_level','volatility_change','rvol','rvol_bin','turnover_bin'))
ASSOCIATION_BINS={'compression_bin':('<1','>=1','unknown'),'body_bin':('>=.5','<.5','unknown'),
    'extension_bin':('<.5','.5-1','>=1','unknown'),'ER_pre_bin':('range_proxy','mixed','efficient_move_proxy','unknown'),
    'break_rvol_bin':('low','normal','high','insufficient_history','gap_in_lookback','zero_prior_mean','unknown'),
    'range_position_bin':('below_range','inside_range','above_range','unknown'),
    **{'MA_'+f:stats.STATE_CLASSES['MA'] for f in core.FRAMES},'cost_share_bin':('<=.25','>.25','unknown')}


class Table:
    def __init__(self,path):self.path=path;self.file=path.open('w',encoding='utf-8-sig',newline='');self.writer=None;self.count=0
    def write(self,row):
        if self.writer is None:
            self.writer=csv.DictWriter(self.file,fieldnames=list(row));self.writer.writeheader()
        self.writer.writerow(row);self.count+=1
    def close(self):
        if self.writer is None:self.file.write('no_rows\n')
        self.file.close()


def read(z,name,fields=None):
    with z.open(name) as f:
        rows=csv.DictReader(io.TextIOWrapper(f,encoding='utf-8-sig'))
        return [{k:r[k] for k in fields if k in r} if fields else r for r in rows]


def path_key(row):
    identity=[str(row[k]) for k in ('policy','event_id','scenario','entry_time_ms','exit_time_ms','exit_bar_ms','exit_reason','market_at_open')]
    identity += [format(D(row[k]).normalize(),'f') for k in ('raw_entry','stop','target','raw_exit')]
    return hashlib.sha256(json.dumps(identity,separators=(',',':')).encode()).hexdigest()[:32]


def correlation(a,b):
    if len(a)<3:return None
    ma=sum(a)/len(a);mb=sum(b)/len(b)
    va=sum((x-ma)**2 for x in a);vb=sum((x-mb)**2 for x in b)
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/math.sqrt(va*vb) if va and vb else None


def summarize(labels):
    valid=[r for r in labels if r['complete']];n=len(valid)
    result=dict(n=len(labels),n_complete=n,n_incomplete=len(labels)-n,small_sample=n<30,
        n_early_exit=sum(r['early_exit_capped'] for r in labels),n_post_exit_tape=sum(r['contains_post_exit_prices'] for r in labels),
        n_entry_clock_proxy=sum(r['entry_bar_uncertain'] for r in labels),n_exit_bar_unknown=sum(r['exit_bar_uncertain'] for r in labels),
        n_right_censored=sum(r['right_censored'] for r in labels),
        definitely_cost_insufficient=sum(r['net_quote_potential_R_model_upper']<=0 for r in valid),
        definitely_cost_covered=sum(r['net_quote_potential_R_model_lower']>0 for r in valid),
        favorable_but_cost_insufficient=sum(r['mfe_R0_lower']>0 and r['net_quote_potential_R_model_upper']<=0 for r in valid))
    for key in ('mfe_R0_lower','mfe_R0_upper','mae_R0_lower','mae_R0_upper','mfe_R_model_lower','mfe_R_model_upper',
                'giveback_R0_lower','giveback_R0_upper','close_efficiency_proxy','observed_closes_near_entry_fraction','observed_hours',
                'cost_over_mfe_lower','cost_over_mfe_upper'):
        values=[float(r[key]) for r in valid if r.get(key) is not None]
        result['mean_'+key]=sum(values)/len(values) if values else None
        result['median_'+key]=stats.percentile(values,.5)
        if key in ('mfe_R0_lower','mae_R0_lower'):
            for pct in (25,75,90):result[f'p{pct}_'+key]=stats.percentile(values,pct/100)
    held=[r for r in valid if r['path_scope']=='full_original_hold' and 'source_net_R' in r]
    result['held_mfe_model_vs_archived_netR_correlation']=correlation([float(r['mfe_R_model_lower']) for r in held],[float(r['source_net_R']) for r in held])
    return result


def hypothesis_gate(rows):
    def find(period,universe,metric):return next((r for r in rows if (r['period'],r['universe'],r['metric'])==(period,universe,metric)),None)
    for period in ('train','validation'):
        for metric in ('mfe_R0','mfe_ATRpre','mae_R0'):
            r=find(period,'all_eight',metric)
            if not r or min(r['n_a'],r['n_b'])<30 or r['delta'] is None:return False
            if metric=='mae_R0' and r['delta']>0 or metric!='mae_R0' and r['delta']<=0:return False
    r=find('continuous','all_eight','mfe_R0')
    if not r or r['low95'] is None or r['low95']<=0:return False
    for period,universe in [('continuous','BTC'),('continuous','other_seven'),('opened_test','all_eight')]:
        r=find(period,universe,'mfe_R0')
        if not r or min(r['n_a'],r['n_b'])<10 or r['delta'] is None or r['delta']<=0:return False
    return True


def verify(z):
    manifest=json.loads(z.read('results/p1_08a_manifest.json'))
    if manifest['current_commit']!=BASE or manifest['strategy_replays'] or manifest['new_OOS'] or manifest['live_execution']:raise ValueError('Wrong fixed P1-08A source')
    for name,digest in manifest['output_sha256'].items():
        if files.sha(z.read('results/'+name))!=digest:raise ValueError('Source output hash changed: '+name)
    for name,digest in manifest['code_sha256'].items():
        if files.sha(Path(__file__).with_name(name).read_bytes())!=digest:raise ValueError('Inherited source code changed')
    audits=read(z,'results/account_audit.csv')
    if len(audits)!=160 or any(r['verified']!='True' for r in audits):raise ValueError('Archived accounts not validated')
    return manifest


def frozen_and_events(z):
    with ZipFile(io.BytesIO(z.read('inputs/p1-07-source.zip'))) as p7:
        events=read(p7,'results/new_signal_events.csv')
        with ZipFile(io.BytesIO(p7.read('inputs/p1-06b-source.zip'))) as p6:
            with ZipFile(io.BytesIO(p6.read('inputs/p1-06a-source.zip'))) as a:
                with ZipFile(io.BytesIO(a.read('inputs/p1-05-source.zip'))) as p5:
                    frozen=p5.read('inputs/frozen-source.zip')
    return frozen,{(r['policy'],r['event_id']):r for r in events}


def catalog():
    return [dict(feature=k,role='known_at_or_before_entry',availability='source closed frame reference<=decision; geometry at original breach close',
                 note='No exit/PnL/path labels') for k in (*CONTEXT_FIELDS,'body_share','upper_wick_share','lower_wick_share','compression_ratio','ATR_pre','ER_pre','break_rvol','close_extension_ATR','breakclose_location_20D',
                    'prebreak_ATR_pct','flat_cost5_over_ATR_pre','flat_cost15_over_ATR_pre')]+[
        dict(feature='entry_bar_final_body_and_RVOL',role='deferred_not_populated',availability='entry_time+1H',note='Cannot authorize that same open'),
        dict(feature='future_pivot',role='deferred_not_populated',availability='right confirmation candles closed',note='No hindsight pivot'),
        dict(feature='MFE_MAE_arrivals_giveback_exit_PnL',role='posthoc_label_only',availability='after relevant future window or original exit',note='Separate files; never feature')]


def identity(row,population):
    return (population,row.get('model','conditional_paper_'+row.get('side','unknown')),row.get('period','continuous'),row['scenario'],row['policy'])


def split(t):return 'train' if t<money.CUTS[0] else 'validation' if t<money.CUTS[1] else 'opened_test'


def profile_dict(profile):return dict(zip(('population','model','period','scenario','policy'),profile))


def scopes():return [('full',None)]+[(kind,h) for h in paths.HORIZONS for kind in ('held','tape')]


def fetch(cache,pid,kind,h):return cache[pid]['full'] if kind=='full' else cache[pid]['horizons'][(kind,h)]


def run(source_zip,out):
    with localcontext() as ctx:
        ctx.prec=50
        return _run(source_zip,out)


def _run(source_zip,out):
    root=Path(__file__).parent
    if out.exists() and any(out.iterdir()):raise ValueError('Output directory must be empty')
    if files.sha(source_zip.read_bytes())!=SOURCE_SHA or files.sha((root/'P1_08B_PROTOCOL.json').read_bytes())!=PROTOCOL_SHA:raise ValueError('Fixed source or protocol SHA changed')
    out.mkdir(parents=True,exist_ok=True)
    with ZipFile(source_zip) as z:
        source=verify(z);frozen,events=frozen_and_events(z)
        actual=read(z,'results/actual_trade_diagnostics.csv',TRADE_FIELDS);isolated=read(z,'results/isolated_event_diagnostics.csv',TRADE_FIELDS)
        accounts=read(z,'results/archived_accounts.csv');snapshots=read(z,'results/opportunity_snapshots.csv')
        profiles={identity(r,'actual_account'):set() for r in accounts}
        seeds={};mappings=[];seen=set()
        for population,rows in [('actual_account',actual),('isolated',isolated)]:
            for row in rows:
                paths.units(row)
                pid=path_key(row);seeds.setdefault(pid,row)
                key=(population,row.get('model',''),row.get('period',''),row['policy'],row['scenario'],row['event_id'])
                if key in seen:raise ValueError('Duplicate profile/event');
                seen.add(key)
                if population=='actual_account':profiles[identity(row,population)].add(pid)
                else:
                    for period in ('continuous',split(int(row['entry_time_ms']))):
                        profile=('isolated','conditional_paper_'+row['side'],period,row['scenario'],row['policy']);profiles.setdefault(profile,set()).add(pid)
                mappings.append(dict(path_id=pid,population=population,model=row.get('model','conditional_paper_'+row['side']),period=row.get('period','source_event'),
                    policy=row['policy'],scenario=row['scenario'],event_id=row['event_id'],symbol=row['symbol'],side=row['side'],entry_time_ms=row['entry_time_ms'],
                    quantity=row['quantity'],planned_risk=row['planned_risk'],archived_net_R=row['r_multiple'],archived_net_pnl_USDT=row['net_pnl_usdt']))
        files.write_csv(out/'source_accounts.csv',accounts);files.write_csv(out/'profile_path_mapping.csv',mappings)
        files.write_csv(out/'feature_catalog.csv',catalog())
        feature_table=Table(out/'feature_snapshots.csv');hold_table=Table(out/'held_path_labels.csv');horizon_table=Table(out/'fixed_horizon_labels.csv');touch_table=Table(out/'touch_time_labels.csv')
        feature_map={};cache={};quality=[]
        lock=core.read_lock(root/'PROSPECTIVE_LOCK_V1.json')
        with ZipFile(io.BytesIO(frozen)) as raw:
            for symbol in lock['symbols']:
                frames={}
                for frame,step in core.FRAMES.items():
                    name=symbol+'_'+frame+'.csv';meta=lock['frozen_data_prefixes'][name];content=raw.read(name)
                    if files.sha(content)!=meta['sha256'] or len(content)!=meta['bytes']:raise ValueError('Frozen raw hash mismatch')
                    bars=core.parse_bars(content,symbol,frame,meta['frozen_bars'],closed_through_ms=meta['frozen_last_close_exclusive_ms']);frames[frame]=bars
                    quality.append(dict(symbol=symbol,frame=frame,sha256=files.sha(content),bars=len(bars),zero_volume=sum(b.v==0 for b in bars),
                        gaps=sum(b.open_ms-a.open_ms>step for a,b in zip(bars,bars[1:])),missing_bars=sum((b.open_ms-a.open_ms)//step-1 for a,b in zip(bars,bars[1:]))))
                feature_source=paths.BreakFeatures(frames)
                for s in snapshots:
                    if s['symbol']!=symbol:continue
                    row=events.get((s['policy'],s['event_id']),dict(policy=s['policy']))
                    geometry=feature_source.at(row)
                    t=int(s['decision_time_ms'])
                    if geometry['break_available_ms'] is not None and geometry['break_available_ms']>t:raise ValueError('Future geometry')
                    f=dict(event_id=s['event_id'],policy=s['policy'],symbol=symbol,side=s['side'],stage=s['stage'],decision_time_ms=t,
                        **{key:s[key] for key in CONTEXT_FIELDS},**geometry)
                    for frame in core.FRAMES:
                        if f['reference_close_ms_'+frame] and int(f['reference_close_ms_'+frame])>t:raise ValueError('Future MTF context')
                    feature_table.write(f);feature_map[(s['policy'],s['event_id'],s['stage'])]=f
                index=legacy_index(frames['1h'])
                for pid,row in seeds.items():
                    if row['symbol']!=symbol:continue
                    cutoff=next(t for t in money.CUTS if int(row['entry_time_ms'])<t)
                    full=paths.analyze(row,index,cutoff=cutoff);arrivals=full.pop('arrivals')
                    full['source_net_R']=D(row['r_multiple'])
                    full['source_gross_R_model']=(D(row['raw_exit'])-D(row['raw_entry']))*(1 if row['side']=='long' else -1)/full['R_model_per_unit']
                    full['realized_cost_R_model']=full['source_gross_R_model']-full['source_net_R']
                    full['price_giveback_R_model_lower']=full['mfe_R_model_lower']-full['source_gross_R_model']
                    meta=dict(path_id=pid,policy=row['policy'],event_id=row['event_id'],symbol=symbol,side=row['side'],scenario=row['scenario'],entry_time_ms=row['entry_time_ms'],exit_reason=row['exit_reason'])
                    hold_table.write(dict(**meta,**full))
                    for hit in arrivals:touch_table.write(dict(path_id=pid,scope='full_original_hold',**hit))
                    cache[pid]=dict(full=full,horizons={})
                    for h in paths.HORIZONS:
                        for kind,tape in [('held',False),('tape',True)]:
                            label=paths.analyze(row,index,h,tape=tape,cutoff=cutoff);hits=label.pop('arrivals')
                            cache[pid]['horizons'][(kind,h)]=label
                            horizon_table.write(dict(**meta,**label))
                            if tape and h==72:
                                for hit in hits:touch_table.write(dict(path_id=pid,scope='counterfactual_tape72',**hit))
                print('PATHS_AND_CAUSAL_FEATURES',symbol,sum(r['symbol']==symbol for r in seeds.values()),flush=True)
        for table in (feature_table,hold_table,horizon_table,touch_table):table.close()
        files.write_csv(out/'data_quality.csv',quality)
        files.write_csv(out/'opportunity_status_labels.csv',[{k:r[k] for k in ('policy','event_id','symbol','stage','source_final_status')} for r in snapshots])
        timeout=[]
        for row in actual:
            if row['policy']=='T20_CONT' and row['exit_reason']=='max_hold_72h':
                pid=path_key(row);label=cache[pid]['full']
                timeout.append(dict(path_id=pid,model=row['model'],period=row['period'],scenario=row['scenario'],symbol=row['symbol'],event_id=row['event_id'],
                    archived_net_R=row['r_multiple'],archived_net_pnl_USDT=row['net_pnl_usdt'],path_class=paths.timeout_class(label),**label))
        files.write_csv(out/'t20_cont_timeout_diagnosis.csv',timeout)
        timeout_ci=[]
        for account in accounts:
            if account['policy']!='T20_CONT':continue
            for universe,metric in itertools.product(('all_eight','BTC','other_seven'),('mfe_R0_lower','giveback_R0_lower','endpoint_gross_R0')):
                local=[r for r in timeout if all(r[k]==account[k] for k in ('model','period','scenario')) and r['complete'] and (universe=='all_eight' or (r['symbol']=='BTCUSDT' if universe=='BTC' else r['symbol']!='BTCUSDT'))]
                start,end=calendars.PERIODS[account['period']]
                ci=stats.block_difference([dict(r,entry_time_ms=seeds[r['path_id']]['entry_time_ms'],net_R=r[metric]) for r in local],[],start,end)
                timeout_ci.append(dict(**{k:account[k] for k in ('model','period','scenario')},universe=universe,metric=metric,n=len(local),
                    estimate=ci['group_a_mean_R'],low95=ci['group_a_low95'],high95=ci['group_a_high95'],nonempty_blocks=ci['group_a_nonempty_blocks'],
                    calendar_blocks=ci['calendar_blocks'],replications=1000,seed=808,label_only_not_net_profit=True))
        files.write_csv(out/'timeout_block_uncertainty.csv',timeout_ci)
        summaries=Table(out/'path_summary.csv');exits=Table(out/'exit_path_summary.csv');timeout_summary=Table(out/'timeout_horizon_summary.csv')
        universes=('all_eight','BTC','other_seven',*lock['symbols'])
        def member(pid,universe):
            if universe=='all_eight':return True
            if universe=='BTC':return seeds[pid]['symbol']=='BTCUSDT'
            if universe=='other_seven':return seeds[pid]['symbol']!='BTCUSDT'
            return seeds[pid]['symbol']==universe
        for profile,ids in sorted(profiles.items()):
            for universe in universes:
                local=sorted(pid for pid in ids if member(pid,universe))
                for kind,h in scopes():
                    labels=[fetch(cache,pid,kind,h) for pid in local]
                    summaries.write(dict(**profile_dict(profile),universe=universe,path_scope=kind,horizon_hours=h,**summarize(labels)))
                    if profile[4]=='T20_CONT' and profile[0]=='actual_account':
                        subset=[fetch(cache,pid,kind,h) for pid in local if seeds[pid]['exit_reason']=='max_hold_72h']
                        timeout_summary.write(dict(**profile_dict(profile),universe=universe,path_scope=kind,horizon_hours=h,**summarize(subset)))
                if universe in ('all_eight','BTC','other_seven'):
                    for reason in ('stop','stop_gap','target','max_hold_72h','calendar_boundary','data_gap_first_quote','integrity_review_exit'):
                        labels=[cache[pid]['full'] for pid in local if seeds[pid]['exit_reason']==reason]
                        exits.write(dict(**profile_dict(profile),universe=universe,exit_reason=reason,**summarize(labels)))
        for table in (summaries,exits,timeout_summary):table.close()
        assoc,contrasts=associations(out,isolated,seeds,cache,feature_map)
        candidates=[hid for hid in ('H_COMPRESSION','H_BODY') if hypothesis_gate([r for r in contrasts if r['hypothesis']==hid])]
        files.write_json(out/'hypothesis_decisions.json',dict(recommended_for_fresh_mechanism_replication=candidates,
            independent_strategy_evidence=False,positive_expectancy_claim=False,new_OOS=False,maximum_candidates=2,
            rejection_is_not_proof_all_features_useless=True))
        report(out,source,seeds,timeout,contrasts,candidates)
        plot(out,timeout)
        result=dict(version='p1-08b-price-paths-v1',current_commit=os.getenv('GITHUB_SHA'),base_commit=BASE,source_zip_sha256=SOURCE_SHA,protocol_sha256=PROTOCOL_SHA,
            source_output_hashes_verified=len(source['output_sha256']),source_accounts=160,source_actual_rows=len(actual),source_isolated_rows=len(isolated),
            source_opportunity_snapshots=len(snapshots),unique_paths=len(seeds),profile_mappings=len(mappings),profile_count=len(profiles),
            held_label_rows=hold_table.count,fixed_horizon_label_rows=horizon_table.count,touch_label_rows=touch_table.count,
            feature_rows=feature_table.count,summary_rows=summaries.count,association_rows=len(assoc),contrast_rows=len(contrasts),
            raw_candles=sum(r['bars'] for r in quality),frozen_source_sha256={r['symbol']+'_'+r['frame']+'.csv':r['sha256'] for r in quality},
            recommended_mechanism_hypotheses=candidates,new_OOS=False,strategy_replays=0,new_capital_curves=0,parameter_optimization=False,live_execution=False,
            code_sha256={name:files.sha((root/name).read_bytes()) for name in ('p1_08b_paths.py','p1_08b_study.py')},
            output_sha256={p.name:files.sha(p.read_bytes()) for p in sorted(out.iterdir()) if p.is_file()})
        files.write_json(out/'p1_08b_manifest.json',result);return result


def legacy_index(bars):
    import p1_loss_diagnostics
    return p1_loss_diagnostics.BarIndex(bars)


def associations(out,isolated,seeds,cache,features):
    rows=[]
    numeric=('compression_ratio','body_share','upper_wick_share','lower_wick_share','close_extension_ATR','extreme_extension_ATR',
        'ER_pre','break_rvol','preclose_location_20D','breakclose_location_20D','prebreak_ATR_pct','flat_cost5_over_ATR_pre','flat_cost15_over_ATR_pre')
    for source in isolated:
        if source['scenario']!='source_stop_first':continue
        pid=path_key(source);label=cache[pid]['horizons'][('tape',24)]
        f=features[(source['policy'],source['event_id'],'entry')];atr=f['ATR_pre'];price=D(source['raw_entry'])
        e,s,r0,risk,slip=paths.units(source)
        cost=(risk-r0)/risk
        rows.append(dict(event_id=source['event_id'],path_id=pid,policy=source['policy'],symbol=source['symbol'],side=source['side'],
            entry_time_ms=int(source['entry_time_ms']),period=split(int(source['entry_time_ms'])),label_complete=label['complete'],
            contains_post_exit_prices=label['contains_post_exit_prices'],mfe_R0=label['mfe_R0_lower'] if label['complete'] else None,
            mae_R0=label['mae_R0_lower'] if label['complete'] else None,
            mfe_ATRpre=label['mfe_price_lower']/atr if label['complete'] and atr is not None and atr>0 else None,
            mfe_price_pct=100*label['mfe_price_lower']/price if label['complete'] else None,
            **{key:f.get(key,'unknown') for key in ASSOCIATION_BINS if key!='cost_share_bin'},
            **{key:f.get(key) for key in numeric},known_entry_cost_share=cost,
            cost_share_bin='<=.25' if cost<=D('.25') else '>.25'))
    files.write_csv(out/'association_event_table.csv',rows)
    grouped=[];correlations=[]
    for policy,side,(period,(start,end)),universe in itertools.product(('A','S_ATR1','T20_FAIL','T20_CONT'),('long','short'),calendars.PERIODS.items(),('all_eight','BTC','other_seven')):
        local=[r for r in rows if r['policy']==policy and r['side']==side and start<=r['entry_time_ms']<end and (universe=='all_eight' or (r['symbol']=='BTCUSDT' if universe=='BTC' else r['symbol']!='BTCUSDT'))]
        for predictor,metric in itertools.product((*numeric,'known_entry_cost_share'),('mfe_R0','mae_R0','mfe_ATRpre')):
            pairs=[(float(r[predictor]),float(r[metric])) for r in local if r[predictor] is not None and r[metric] is not None]
            correlations.append(dict(policy=policy,side=side,period=period,universe=universe,predictor=predictor,posthoc_metric=metric,
                n_pairs=len(pairs),pearson_r=correlation([a for a,b in pairs],[b for a,b in pairs]),small_sample=len(pairs)<30,
                role='descriptive_only_no_iid_pvalue_or_feature_selection'))
        for field,categories in ASSOCIATION_BINS.items():
            for category in categories:
                subset=[r for r in local if r[field]==category];valid=[r for r in subset if r['label_complete']]
                def avg(key):
                    values=[float(r[key]) for r in valid if r[key] is not None]
                    return sum(values)/len(values) if values else None
                grouped.append(dict(policy=policy,side=side,period=period,universe=universe,feature=field,category=category,
                    n=len(subset),n_complete=len(valid),n_post_exit_tape=sum(r['contains_post_exit_prices'] for r in subset),
                    n_mfe_ATRpre=sum(r['mfe_ATRpre'] is not None for r in valid),
                    mean_mfe_R0=avg('mfe_R0'),mean_mae_R0=avg('mae_R0'),mean_mfe_ATRpre=avg('mfe_ATRpre'),mean_mfe_price_pct=avg('mfe_price_pct'),small_sample=len(valid)<30))
    files.write_csv(out/'feature_path_associations.csv',grouped)
    files.write_csv(out/'continuous_feature_correlations.csv',correlations)
    contrasts=[]
    for hid,field,a,b in [('H_COMPRESSION','compression_bin','<1','>=1'),('H_BODY','body_bin','>=.5','<.5')]:
        for (period,(start,end)),universe,metric in itertools.product(calendars.PERIODS.items(),('all_eight','BTC','other_seven'),('mfe_R0','mfe_ATRpre','mae_R0')):
            local=[r for r in rows if r['policy']=='T20_CONT' and r['side']=='long' and start<=r['entry_time_ms']<end and r[metric] is not None and (universe=='all_eight' or (r['symbol']=='BTCUSDT' if universe=='BTC' else r['symbol']!='BTCUSDT'))]
            ra=[dict(r,net_R=r[metric]) for r in local if r[field]==a];rb=[dict(r,net_R=r[metric]) for r in local if r[field]==b]
            ci=stats.block_difference(ra,rb,start,end)
            contrasts.append(dict(hypothesis=hid,period=period,universe=universe,metric=metric,A=a,B=b,n_a=len(ra),n_b=len(rb),
                estimate_a=ci['group_a_mean_R'],estimate_b=ci['group_b_mean_R'],delta=ci['difference_R'],low95=ci['difference_low95'],high95=ci['difference_high95'],
                undefined_resamples=ci['undefined_resamples'],calendar_blocks=ci['calendar_blocks'],nonempty_blocks_a=ci['group_a_nonempty_blocks'],nonempty_blocks_b=ci['group_b_nonempty_blocks'],
                label_scope='24h_counterfactual_reference_tape',replications=1000,seed=808))
    files.write_csv(out/'fixed_hypothesis_contrasts.csv',contrasts);return grouped,contrasts


def report(out,source,seeds,timeout,contrasts,candidates):
    fmt=lambda v:'—' if v is None else f'{float(v):.6f}'
    lines=['# P1-08B｜價格路徑與有效波動完整實測','',
        '先固定cf3af6f再計算；不重跑策略、不改退出／SL／TP／ATR／RR／AI或產生新本金。完整held、固定前綴及原exit後反事實行情帶分開；MFE不是可實現利潤。','',
        f'讀取P1-08A160帳戶、32,232交易列與13,548孤立情境；重複profile映射為{len(seeds)}個唯一policy/event/scenario/path。這不是獨立樣本數，不跨scenario合併推論。','',
        'R0為純價格停損距離；R_model含原成本，以原q與planned_risk驗證。已實現淨R不二次扣成本，gross回吐與費用分開。','',
        '## T20_CONT已開啟Test主成本純多72h退出','']
    target=[r for r in timeout if (r['model'],r['period'],r['scenario'])==('fractional_spot','opened_test','source_stop_first')]
    counts=Counter(r['path_class'] for r in target)
    lines += [f'原28筆到期退出已對帳，淨虧損{sum(D(r["archived_net_R"])<0 for r in target)}筆；下面只診斷原路徑，不改退出。','',
        '| 類別 | 筆數 |','|---|---:|']+[f'| {key} | {value} |' for key,value in sorted(counts.items())]
    lines+=['','| 範圍 | n | MFE≥0.5R0 | MFE≥1R0 | 淨虧損 | 平均MFE_R0 | 平均價格回吐_R0 |','|---|---:|---:|---:|---:|---:|---:|']
    for universe in ('all_eight','BTC','other_seven'):
        r=[r for r in target if universe=='all_eight' or (r['symbol']=='BTCUSDT' if universe=='BTC' else r['symbol']!='BTCUSDT')]
        avg=lambda key:sum(float(t[key]) for t in r)/len(r) if r else None
        lines.append(f'| {universe} | {len(r)} | {sum(t["mfe_R0_lower"]>=D(".5") for t in r)} | {sum(t["mfe_R0_lower"]>=1 for t in r)} | {sum(D(t["archived_net_R"])<0 for t in r)} | {fmt(avg("mfe_R0_lower"))} | {fmt(avg("giveback_R0_lower"))} |')
    lines+=['','這些peak、giveback與到達時間均是事後標籤；不能把觀察到峰值當成可成交最佳退出。完整6/12/24/48/72h分布在path_summary.csv／timeout_horizon_summary.csv，觸價interval及同棒未知在touch_time_labels.csv。','',
        '## 固定特徵假說：全部對照','',
        '| 假說 | 時段 | 範圍 | 標籤 | nA/nB | 差A−B | 95%共同區塊區間 |','|---|---|---|---|---|---:|---|']
    for r in contrasts:lines.append(f'| {r["hypothesis"]} | {r["period"]} | {r["universe"]} | {r["metric"]} | {r["n_a"]}/{r["n_b"]} | {fmt(r["delta"])} | [{fmt(r["low95"])},{fmt(r["high95"])}] |')
    lines+=['',f'符合先固定假說形成門檻：{", ".join(candidates) or "0個"}。不是正期望驗收；即使候選通過，24h行情帶仍可能含原止損後價格，不可當進場優勢或策略收益。',
        '壓縮與突破實體只在原closed breach/prebreak時可知；三週期context與成本只用原決策時已知快照。特徵、最終狀態、路徑及PnL分檔；deferred下一根幾何／RVOL及未確認pivot不填入feature。',
        '完整候選、所有固定分組、未知／零樣本保留。未eligible沒有等價label；原OHLC eligible本身仍有成交代理選樣，不能說完整真實可成交市場母體。','',
        '## 不確定性、成本與限制','',
        '28日共同時間區塊1000次seed808，跨幣同抽；首尾及空區塊保留，長期依賴仍可能超出28日。所有既有Test均是歷史探索，不是獨立OOS，不用重複帳戶、情境或幣種湊有效樣本。',
        '限價入場棒及棒內退出棒只能給寬鬆OHLC上包絡；可能包含入場前／退出後極值，並非都可在持倉中達到。缺棒／零量／觀察邊界使真上界未知，不將未知MFE當0。同棒TP/SL與極值順序未知，原stop-first只讀不當真實先後。',
        'tape使用原記錄入場小時；legacy限價真實秒級成交時間不可得，entry_clock_proxy明列。持倉prefix提前出場時標early_exit，不能冒稱觀察足6/12/24/48/72h。',
        'cost/MFE於MFE0時未定義；net_quote_potential是按模型在事後極值的算式，不是已實現淨R。不補造spread、depth、CVD或精確成本；現貨最小委託及借幣未取得，hypothetical short另模型。',
        '三種路徑、R單位、成本情境與原始帳戶均可由profile_path_mapping.csv追溯；原策略本金未變。若要獨立驗證，需新協議凍結後的全新資料，同一母體／規則／單位及完整品質標籤；MFE差不正或MAE增加即否證，不改門檻救結果。','']
    (out/'P1_08B_RESULT_ZH.md').write_text('\n'.join(lines),encoding='utf-8')


def plot(out,timeout):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    rows=[r for r in timeout if (r['model'],r['period'],r['scenario'])==('fractional_spot','opened_test','source_stop_first')]
    fig,ax=plt.subplots(figsize=(8,5))
    for symbol in sorted({r['symbol'] for r in rows}):
        selected=[r for r in rows if r['symbol']==symbol]
        ax.scatter([float(r['mfe_R0_lower']) for r in selected],[float(r['endpoint_gross_R0']) for r in selected],label=symbol,s=40)
    ax.axhline(0,color='grey',linewidth=.8);ax.set_xlabel('Posthoc held MFE / original price risk R0');ax.set_ylabel('Original72h endpoint gross / R0')
    ax.set_title('Opened Test: original T20_CONT timeouts; peaks are not executable exits');ax.legend(fontsize=8)
    fig.tight_layout();fig.savefig(out/'t20_cont_timeout_mfe_giveback.png',dpi=160);plt.close(fig)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-zip',required=True,type=Path);parser.add_argument('--out-dir',required=True,type=Path)
    args=parser.parse_args();run(args.source_zip,args.out_dir)
