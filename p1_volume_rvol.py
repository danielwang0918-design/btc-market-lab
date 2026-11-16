#!/usr/bin/env python3
"""P1-04: fixed RVOL features and complete descriptive cross-tabs, never orders.

Only immutable, closed 1H/4H/1D Binance Spot evidence is accepted. No strategy
changes, opened-Test optimization, portfolio simulation, CVD or absorption claims.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from statistics import median
from zipfile import ZipFile

import p1_candlestick_research as core
import p1_zone_context as zone
import p1_market_regime as regime

VERSION='p1-volume-rvol-v1'
BASE_COMMIT='78d141a4d5fcb03eb2c56a4da369d1b3194aea63'
PROTOCOL_SHA256='1b30c5ac93d8c87258e884287ec44f5850bcf35b8f99970ca649723ea19ba68a'
FROZEN_RUN=43820018876
P1_RUN=43857787070
CONTEXT_RUN=43862040301
WINDOW=20
RVOL_BINS=('low','normal','high','insufficient_history','gap_in_lookback','zero_prior_mean')
SPLITS=('train','validation','exploratory_test_opened')
EVENT_KEY=('symbol','timeframe','pattern','signal_open_ms')
BASE_DIMS=('symbol','timeframe','pattern','split','horizon_hours')
VIEWS={
    'pattern_rvol':('rvol_bin',),
    'pattern_location_rvol':('location','rvol_bin'),
    'pattern_ma200_rvol':('trend','rvol_bin'),
    'pattern_location_ma200_atr_rvol':('location','trend','volatility','rvol_bin'),
}
CONTEXT_PINS={
    'zone/p1_02_zone_evidence.csv':'58ba1109420a887075f654ccfa131e8033bef3afc9972095ee1535eeb7dda4e9',
    'zone/p1_02_manifest.json':'0e0ce7350b9940f6d97ea97f48e4b74be1ffb73e9ac1778cc71a44a78aecdaa9',
    'regime/p1_03_regime_evidence.csv':'b26379ed8f7abc00afd8d2708fa73794b6afd5961dc9167ade6bb1d30446be9c',
    'regime/p1_03_manifest.json':'84c56184723871e436e3aada0b1fa001bf3475e1a3944b62835dd3363795bb28',
    'audit/P1_REVIEW_PROPAGATION_RESULT.json':'635da2285d73e8564284785228268773f793d6f1efbfe9ec736ce78d5df90a9d',
}
P1_PINS={'p1_01_events.csv':zone.P1_EVENTS_SHA,'p1_01_manifest.json':zone.P1_MANIFEST_SHA}
FEATURE_FIELDS=('current_volume','prior_mean_volume','rvol','rvol_bin','rvol_status',
    'extreme_rvol','prior_bars_used','prior_window_start_ms','prior_window_end_exclusive_ms','available_at_ms')
CONTEXT_FIELDS=('reference_frame','location','pivots_near','nearest_distance_atr','zone_reference',
    'preexisting_pivots_in_window','pattern_start_open_ms','trend','volatility','ma200_prepattern',
    'ma200_slope_20_atr','prior_close_vs_ma200_atr','prior_atr14_pct','prior_atr14_percentile','regime_data_end_ms')
FEATURE_COLUMNS=(*EVENT_KEY,'direction','split',*FEATURE_FIELDS,*CONTEXT_FIELDS)
OBS_COLUMNS=(*core.OUTPUT_COLUMNS,*FEATURE_FIELDS,*CONTEXT_FIELDS)


def digest(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):h.update(block)
    return h.hexdigest()


def check_known_files(root:Path,pins:dict[str,str])->dict[str,str]:
    for name,expected in pins.items():
        if digest(root/name)!=expected:raise ValueError(f'Pinned evidence SHA256 mismatch: {name}')
    return dict(pins)


def event_key(r):
    return r['symbol'],r['timeframe'],r['pattern'],int(r['signal_open_ms'])


def read_source(raw:bytes,symbol:str,frame:str,meta:dict)->list[core.Candle]:
    if core.sha(raw)!=meta['sha256'] or len(raw)!=meta['bytes']:
        raise ValueError(f'Frozen source SHA256/bytes mismatch: {symbol}_{frame}')
    bars=core.parse_bars(raw,symbol,frame,meta['frozen_bars'],
                         closed_through_ms=meta['frozen_last_close_exclusive_ms'])
    if bars[-1].open_ms+core.FRAMES[frame]!=meta['frozen_last_close_exclusive_ms']:
        raise ValueError('Frozen last closed candle differs from lock')
    return bars


class VolumeSource:
    """Current closed-bar volume / twenty contiguous, strictly earlier bars."""
    def __init__(self,bars,frame):
        if frame not in core.FRAMES:raise ValueError('Only closed 1h/4h/1d frames are allowed')
        self.bars=bars;self.step=core.FRAMES[frame]
        for i,b in enumerate(bars):
            if (b.open_ms%self.step or (i and b.open_ms<=bars[i-1].open_ms)
                    or not b.v.is_finite() or b.v<0):
                raise ValueError('Invalid volume, duplicate, unsorted or misaligned candle')
        self.starts=core.contiguous_segment_start(bars,self.step)
        # Adaptive precision retains small volumes when an extreme old bar rolls
        # out; fifty digits minimum also makes exported ratios reproducible.
        nonzero=[b.v for b in bars if b.v]
        self.precision=max(50,max((v.adjusted() for v in nonzero),default=0)
                            -min((v.as_tuple().exponent for v in nonzero),default=0)+5)
        self.prior_sums=[None]*len(bars)
        with localcontext() as ctx:
            ctx.prec=self.precision
            total=Decimal(0)
            for i,b in enumerate(bars):
                start=self.starts[i]
                if i==start:total=Decimal(0)
                else:total+=bars[i-1].v
                if i-start>WINDOW:total-=bars[i-WINDOW-1].v
                if i-start>=WINDOW:self.prior_sums[i]=total

    def feature_at(self,i):
        if not 0<=i<len(self.bars):raise ValueError('Volume feature index out of range')
        b=self.bars[i];start=self.starts[i];n=min(i-start,WINDOW)
        out=dict(current_volume=b.v,prior_mean_volume=None,rvol=None,rvol_bin=None,
            rvol_status=None,extreme_rvol=False,prior_bars_used=n,
            prior_window_start_ms=self.bars[i-WINDOW].open_ms if n==WINDOW else None,
            prior_window_end_exclusive_ms=b.open_ms if n else None,
            available_at_ms=b.open_ms+self.step)
        if n<WINDOW:
            status='gap_in_lookback' if start else 'insufficient_history'
            out.update(rvol_bin=status,rvol_status=status)
            return out
        total=self.prior_sums[i]
        with localcontext() as ctx:
            ctx.prec=self.precision
            out['prior_mean_volume']=total/WINDOW
            if not total:
                out.update(rvol_bin='zero_prior_mean',rvol_status='zero_prior_mean')
            else:
                numerator=WINDOW*b.v
                bucket='low' if 5*numerator<4*total else 'normal' if 5*numerator<6*total else 'high'
                out.update(rvol=numerator/total,rvol_bin=bucket,rvol_status='available',
                           extreme_rvol=numerator>=10*total)
        return out


def load_context(path):
    indexed={}
    with path.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if not set(EVENT_KEY).issubset(reader.fieldnames or []):raise ValueError('Missing context event key')
        for r in reader:
            key=event_key(r)
            if key in indexed:raise ValueError('Duplicate context event')
            indexed[key]=r
    return indexed


def attach_context(row,z,g):
    key=event_key(row)
    if key!=event_key(z) or key!=event_key(g):raise ValueError('Context event identity mismatch')
    if any(x.get(f)!=row.get(f) for x in (z,g) for f in ('direction','split')):
        raise ValueError('Context event metadata mismatch')
    frame=row['timeframe'];pattern=row['pattern'];step=core.FRAMES[frame]
    first=int(row['signal_open_ms'])-(zone.PATTERN_LENGTH[pattern]-1)*step
    if (z.get('reference_frame')!=zone.FRAME_REF[frame] or z.get('location') not in zone.LOCATION_CLASSES
            or g.get('trend') not in regime.TRENDS or g.get('volatility') not in regime.VOLATILITY
            or int(g.get('pattern_start_open_ms',-1))!=first):
        raise ValueError('Unknown classification or changed prepattern reference')
    if g.get('regime_data_end_ms') and int(g['regime_data_end_ms'])>first:
        raise ValueError('Future regime data in context')
    return {k:(z[k] if k in z and k not in g else g.get(k,'')) for k in CONTEXT_FIELDS}


def iter_pattern_groups(rows):
    seen=set()
    common=(*EVENT_KEY,'direction','split','signal_close_utc','prior_trend_3','atr14_before_signal')
    for key,items in itertools.groupby(rows,key=event_key):
        group=list(items)
        if key in seen or len(group)!=3 or sorted(int(r['horizon_hours']) for r in group)!=list(core.HORIZONS_HOURS):
            raise ValueError('Missing or duplicated pattern/horizon')
        seen.add(key)
        first=group[0]
        if any(tuple(r[k] for k in common)!=tuple(first[k] for k in common) for r in group):
            raise ValueError('Inconsistent event metadata across horizons')
        frame=first['timeframe'];pattern=first['pattern']
        if (frame not in core.FRAMES or pattern not in zone.PATTERN_SIDE
                or first['direction']!=zone.PATTERN_SIDE[pattern]):
            raise ValueError('Invalid pattern/frame/direction')
        close=int(first['signal_open_ms'])+core.FRAMES[frame]
        if first['split']!=core.split_for(close) or first['signal_close_utc']!=core.utc(close):
            raise ValueError('Signal confirmation time or split is inconsistent')
        yield group


def finite_decimal(value):
    try:out=Decimal(str(value))
    except (InvalidOperation,ValueError) as exc:raise ValueError('Invalid outcome') from exc
    if not out.is_finite():raise ValueError('Nonfinite outcome')
    return out


@dataclass
class Stats:
    n:int=0
    evaluated:int=0
    censored:int=0
    unavailable:int=0
    favorable:int=0
    nets:list=field(default_factory=list)

    def add(self,r):
        status=r['outcome_status']
        if status=='evaluated':
            net=finite_decimal(r['paper_net_pct']);direction=finite_decimal(r['directional_favorable_pct'])
            self.evaluated+=1;self.nets.append(net);self.favorable+=direction>0
        elif status in ('right_censored_or_split_boundary','unavailable_fixed_time_fill'):
            if r['paper_net_pct'] not in ('',None) or r['directional_favorable_pct'] not in ('',None):
                raise ValueError('Non-evaluated observation carries a return')
            if status=='unavailable_fixed_time_fill':
                if r.get('censor_reason') not in ('zero_volume_entry','zero_volume_exit','zero_volume_entry_and_exit'):
                    raise ValueError('Unexplained unavailable fixed-time fill')
                self.unavailable+=1
            else:
                if r.get('censor_reason') not in ('data_gap','split_boundary','incomplete_followup'):
                    raise ValueError('Unexplained censored observation')
                self.censored+=1
        else:raise ValueError('Unknown observation status')
        self.n+=1

    def result(self):
        n=self.evaluated;positive=sum((x for x in self.nets if x>0),Decimal(0))
        loss=-sum((x for x in self.nets if x<0),Decimal(0))
        pf=round(float(positive/loss),8) if loss else None
        status='defined' if loss else 'no_evaluated_outcomes' if not n else 'no_negative_returns' if positive else 'all_zero_returns'
        return dict(observed_events=self.n,evaluated=n,right_censored=self.censored,
            paper_fill_unavailable=self.unavailable,
            unevaluated_pct=round(100*(self.censored+self.unavailable)/self.n,6) if self.n else None,
            directional_positive_pct=round(100*self.favorable/n,6) if n else None,
            paper_net_positive_pct=round(100*sum(x>0 for x in self.nets)/n,6) if n else None,
            mean_paper_net_pct=round(float(sum(self.nets,Decimal(0))/n),8) if n else None,
            median_paper_net_pct=round(float(median(self.nets)),8) if n else None,
            paper_event_profit_factor=pf,paper_event_pf_status=status)


METRIC_FIELDS=tuple(Stats().result())


def summary_rows(acc,grid):
    empty=Stats().result();dims=tuple(grid)
    for key in itertools.product(*grid.values()):
        stats=acc.get(key)
        yield {**dict(zip(dims,key)),**(stats.result() if stats else empty)}


def stability_metrics(stats):
    means=[sum(s.nets,Decimal(0))/s.evaluated for s in stats if s.evaluated]
    return dict(symbols_with_evaluated_events=len(means),
        symbols_positive_mean=sum(x>0 for x in means),symbols_negative_mean=sum(x<0 for x in means),
        median_symbol_mean_paper_net_pct=round(float(median(means)),8) if means else None,
        minimum_symbol_mean_paper_net_pct=round(float(min(means)),8) if means else None,
        maximum_symbol_mean_paper_net_pct=round(float(max(means)),8) if means else None)


def write_csv(path,fields,rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='raise')
        w.writeheader();w.writerows(rows)


def json_write(path,obj):
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def grid_for(symbols,extra):
    values=dict(symbol=tuple(symbols),timeframe=tuple(core.FRAMES),pattern=core.PATTERNS,
                split=SPLITS,horizon_hours=core.HORIZONS_HOURS,
                location=zone.LOCATION_CLASSES,trend=regime.TRENDS,
                volatility=regime.VOLATILITY,rvol_bin=RVOL_BINS)
    return {name:values[name] for name in (*BASE_DIMS,*extra)}


def run_study(frozen_path,p1_dir,context_dir,lock_path,out_dir):
    protocol=Path(__file__).with_name('P1_04_VOLUME_RVOL_PROTOCOL.json')
    if digest(protocol)!=PROTOCOL_SHA256:raise ValueError('Pre-result P1-04 protocol SHA256 changed')
    lock=core.read_lock(lock_path)
    check_known_files(p1_dir,P1_PINS);check_known_files(context_dir,CONTEXT_PINS)
    p1_manifest=json.loads((p1_dir/'p1_01_manifest.json').read_text())
    if (p1_manifest['unique_pattern_events']!=106433 or p1_manifest['observed_pattern_horizon_rows']!=319299
            or p1_manifest['outcome_rows_evaluated']!=311873):
        raise ValueError('Reviewed baseline counts changed')
    zmap=load_context(context_dir/'zone/p1_02_zone_evidence.csv')
    gmap=load_context(context_dir/'regime/p1_03_regime_evidence.csv')
    if len(zmap)!=106433 or set(zmap)!=set(gmap):raise ValueError('Context events missing or duplicated')
    requested=defaultdict(list)
    for key in zmap:requested[key[:2]].append(key)
    features={};source_audit={};zero_times={}
    with ZipFile(frozen_path) as z:
        names=z.namelist()
        if len(names)!=len(set(names)):raise ValueError('Duplicate frozen ZIP member')
        # Verify all bytes before computing any feature, including the last file.
        for fname,meta in lock['frozen_data_prefixes'].items():
            raw=z.read(fname)
            if core.sha(raw)!=meta['sha256'] or len(raw)!=meta['bytes']:
                raise ValueError(f'Frozen source SHA256/bytes mismatch: {fname}')
            if p1_manifest['input_sha256'][fname]!=meta['sha256']:
                raise ValueError('Reviewed events and frozen source differ')
        for fname,meta in lock['frozen_data_prefixes'].items():
            sym=meta['symbol'];frame=meta['interval'];step=core.FRAMES[frame]
            bars=read_source(z.read(fname),sym,frame,meta);source=VolumeSource(bars,frame)
            index={b.open_ms:i for i,b in enumerate(bars)}
            gaps=[dict(after_open_ms=a.open_ms,next_open_ms=b.open_ms,missing_bars=(b.open_ms-a.open_ms)//step-1)
                  for a,b in zip(bars,bars[1:]) if b.open_ms-a.open_ms>step]
            zero_times[(sym,frame)]={b.open_ms for b in bars if b.v==0}
            source_audit[fname]=dict(sha256=meta['sha256'],bytes=meta['bytes'],bars=len(bars),
                duplicate_bars=0,first_open_ms=bars[0].open_ms,last_close_exclusive_ms=bars[-1].open_ms+step,
                gap_segments=len(gaps),missing_bars=sum(x['missing_bars'] for x in gaps),gaps=gaps,
                zero_volume_bars=len(zero_times[(sym,frame)]),zero_volume_open_ms=sorted(zero_times[(sym,frame)]),
                ratio_precision_digits=source.precision)
            for key in requested[(sym,frame)]:
                if key[3] not in index:raise ValueError('Pattern missing from original frozen volume source')
                features[key]=source.feature_at(index[key[3]])
    out_dir.mkdir(parents=True,exist_ok=True)
    views={name:defaultdict(Stats) for name in VIEWS};pooled=defaultdict(Stats)
    baseline=defaultdict(Stats);bin_counts=Counter();extreme_count=0;seen=set();totals=Stats()
    status_counts=Counter()
    with (p1_dir/'p1_01_events.csv').open(encoding='utf-8-sig',newline='') as src, \
            (out_dir/'p1_04_features.csv').open('w',encoding='utf-8-sig',newline='') as ff, \
            (out_dir/'p1_04_observations.csv').open('w',encoding='utf-8-sig',newline='') as of:
        fw=csv.DictWriter(ff,fieldnames=FEATURE_COLUMNS);fw.writeheader()
        ow=csv.DictWriter(of,fieldnames=OBS_COLUMNS);ow.writeheader()
        reader=csv.DictReader(src)
        if tuple(reader.fieldnames)!=tuple(core.OUTPUT_COLUMNS):raise ValueError('Reviewed observation schema drift')
        for group in iter_pattern_groups(reader):
            r=group[0];key=event_key(r)
            if key not in zmap or key not in gmap or key not in features:
                raise ValueError('Incomplete feature/context join')
            context=attach_context(r,zmap[key],gmap[key]);volume=features[key]
            joined={**{k:r[k] for k in (*EVENT_KEY,'direction','split')},**volume,**context}
            fw.writerow(joined);seen.add(key);bin_counts[volume['rvol_bin']]+=1;extreme_count+=volume['extreme_rvol']
            for row in group:
                # Validate the inherited no-fill rule directly against raw volumes.
                t=key[3];step=core.FRAMES[row['timeframe']];h=int(row['horizon_hours'])
                zeros=zero_times[key[:2]]
                if row['outcome_status']=='evaluated' and (t+step in zeros or t+h*3_600_000 in zeros):
                    raise ValueError('Reviewed outcome incorrectly fills a zero-volume bar')
                if row['outcome_status']=='unavailable_fixed_time_fill':
                    reason=row['censor_reason']
                    if ((reason=='zero_volume_entry' and t+step not in zeros) or
                            (reason=='zero_volume_exit' and t+h*3_600_000 not in zeros)):
                        raise ValueError('Reviewed unavailable fill does not match raw volume')
                ow.writerow({**row,**volume,**context});totals.add(row);status_counts[row['outcome_status']]+=1
                base_key=tuple(h if f=='horizon_hours' else row[f] for f in BASE_DIMS)
                baseline[base_key].add(row)
                for view,dims in VIEWS.items():
                    views[view][base_key+tuple(joined[d] for d in dims)].add(row)
                for split in (row['split'],'all'):
                    pooled[(split,h,volume['rvol_bin'])].add(row)
    if seen!=set(zmap) or len(seen)!=106433 or totals.n!=319299 or \
            (totals.evaluated,totals.censored,totals.unavailable)!=(311873,7402,24):
        raise ValueError('P1-04 event/outcome accounting differs from reviewed P1-01')
    row_counts={}
    for view,dims in VIEWS.items():
        grid=grid_for(lock['symbols'],dims)
        path=out_dir/f'p1_04_{view}.csv'
        write_csv(path,(*grid,*METRIC_FIELDS),summary_rows(views[view],grid))
        row_counts[view]=math.prod(len(v) for v in grid.values())
        counts=tuple(sum(getattr(s,a) for s in views[view].values()) for a in ('n','evaluated','censored','unavailable'))
        if counts!=(319299,311873,7402,24):raise ValueError('Cross-tab accounting mismatch')
    pool_grid=dict(split=(*SPLITS,'all'),horizon_hours=core.HORIZONS_HOURS,rvol_bin=RVOL_BINS)
    write_csv(out_dir/'p1_04_rvol_by_split_horizon.csv',(*pool_grid,*METRIC_FIELDS),summary_rows(pooled,pool_grid))
    # Matched comparisons retain every fixed stratum, never rank by performance.
    comparisons=[];stability=defaultdict(list)
    for row in summary_rows(views['pattern_rvol'],grid_for(lock['symbols'],('rvol_bin',))):
        key=tuple(row[f] for f in BASE_DIMS);parent=baseline.get(key,Stats()).result()
        normal=views['pattern_rvol'].get((*key,'normal'),Stats()).result()
        delta=lambda other:round(row['mean_paper_net_pct']-other['mean_paper_net_pct'],8) \
            if row['evaluated'] and other['evaluated'] else None
        comparisons.append({**{f:row[f] for f in (*BASE_DIMS,'rvol_bin')},
            'evaluated':row['evaluated'],'mean_paper_net_pct':row['mean_paper_net_pct'],
            'baseline_evaluated':parent['evaluated'],'baseline_mean_paper_net_pct':parent['mean_paper_net_pct'],
            'mean_difference_vs_all_rvol_pp':delta(parent),'normal_evaluated':normal['evaluated'],
            'normal_mean_paper_net_pct':normal['mean_paper_net_pct'],'mean_difference_vs_normal_pp':delta(normal)})
        sk=tuple(row[f] for f in ('timeframe','pattern','split','horizon_hours','rvol_bin'))
        if row['evaluated']:stability[sk].append(views['pattern_rvol'][(*key,row['rvol_bin'])])
    write_csv(out_dir/'p1_04_matched_rvol_comparison.csv',tuple(comparisons[0]),comparisons)
    stable_rows=[]
    sg={k:v for k,v in grid_for(lock['symbols'],('rvol_bin',)).items() if k!='symbol'}
    for key in itertools.product(*sg.values()):
        stable_rows.append({**dict(zip(sg,key)),**stability_metrics(stability.get(key,[]))})
    write_csv(out_dir/'p1_04_symbol_stability.csv',tuple(stable_rows[0]),stable_rows)
    json_write(out_dir/'p1_04_source_integrity.json',source_audit)
    outputs={p.name:digest(p) for p in sorted(out_dir.glob('p1_04_*.csv'))}
    outputs['p1_04_source_integrity.json']=digest(out_dir/'p1_04_source_integrity.json')
    manifest=dict(version=VERSION,status='historical_descriptive_only',base_commit=BASE_COMMIT,
        protocol_sha256=PROTOCOL_SHA256,protocol=json.loads(protocol.read_text()),
        source_runs=dict(frozen=FROZEN_RUN,p1=P1_RUN,context=CONTEXT_RUN),
        code_sha256={p:digest(Path(__file__).with_name(p)) for p in
                     ('p1_volume_rvol.py','p1_candlestick_research.py','p1_zone_context.py','p1_market_regime.py')},
        lock_sha256=core.LOCK_SHA256,frozen_archive_sha256=digest(frozen_path),
        frozen_input_sha256={name:meta['sha256'] for name,meta in lock['frozen_data_prefixes'].items()},
        p1_input_sha256=P1_PINS,context_input_sha256=CONTEXT_PINS,
        unique_pattern_events=len(seen),observation_rows=totals.n,outcome_counts=dict(status_counts),
        unique_event_rvol_bin_counts={b:bin_counts[b] for b in RVOL_BINS},extreme_rvol_event_count=extreme_count,
        complete_cross_tab_rows=row_counts,output_sha256=outputs,
        p1_01_outcome_changes=0,p1_02_classification_changes=0,p1_03_classification_changes=0,
        portfolio_max_drawdown_pct=None,portfolio_max_drawdown_status='not_defined_without_portfolio_execution_and_capital_model',
        actual_trade_count=None,new_out_of_sample=False,
        limitations=['Overlapping events, repeated horizons, timeframe and coin correlations are not independent trades.',
            'Mean and event PF are equal-entry-notional descriptive outcomes, not executable strategy expectancy/PF.',
            'Opened 2025+ Test and hindsight universe selection are not new out-of-sample validation.',
            'Fixed adverse slippage and fees do not guarantee next-open or horizon-close execution.',
            'Spot shorts are hypothetical; no borrowing, funding, capacity, margin or liquidation model.',
            'OHLCV cannot identify aggressor-side CVD, exact intrabar execution, order-book absorption or stop/target order.'])
    json_write(out_dir/'p1_04_manifest.json',manifest)
    return manifest


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--frozen-archive',type=Path,required=True)
    p.add_argument('--p1-dir',type=Path,required=True)
    p.add_argument('--context-dir',type=Path,required=True)
    p.add_argument('--lock',type=Path,default=Path('PROSPECTIVE_LOCK_V1.json'))
    p.add_argument('--out-dir',type=Path,default=Path('research/p1_04'))
    a=p.parse_args(argv)
    r=run_study(a.frozen_archive,a.p1_dir,a.context_dir,a.lock,a.out_dir)
    print(json.dumps({k:r[k] for k in ('version','unique_pattern_events','observation_rows',
        'outcome_counts','unique_event_rvol_bin_counts','complete_cross_tab_rows')},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
