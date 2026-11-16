#!/usr/bin/env python3
"""Reproduce the P1-01 review: compare every row, all groups and exact v1 geometry.

No parameter selection, inference of independent samples, or portfolio simulation.
The baseline module must be extracted from PR #7's pinned original commit.
"""
from __future__ import annotations

import argparse
import ast
import csv
import heapq
import importlib.util
import inspect
import io
import json
import statistics
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import p1_candlestick_research as current

BASELINE_COMMIT = '54c4cd08b8bfb911a65e12f195102f34caf52623'
BASELINE_CODE_SHA256 = '6165372d07d59927a32c2c1b23bd5201baf9ca70101254b5f4c562749ce1916c'
KEY = ('symbol','timeframe','pattern','signal_open_ms','horizon_hours')
GROUP = ('symbol','timeframe','pattern','split','horizon_hours')
METRICS = ('observed_signals','evaluated','right_censored','paper_fill_unavailable',
           'directional_positive_percent','net_positive_percent_paper',
           'mean_paper_net_pct','median_paper_net_pct')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_csv(path, rows, fields):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields)
        w.writeheader();w.writerows(rows)


def identity(row, fields=KEY):
    return tuple(str(row[k]) for k in fields)


def comparison(before, after, fields):
    left={identity(r,fields):r for r in before}
    right={identity(r,fields):r for r in after}
    result=[]
    for key in sorted(left.keys() | right.keys()):
        row=dict(zip(fields,key))
        for metric in METRICS:
            default=0 if metric in METRICS[:4] else None
            old=left.get(key,{}).get(metric,default)
            new=right.get(key,{}).get(metric,default)
            row.update({metric+'_before':old,metric+'_after':new,
                        metric+'_delta':round(new-old,8) if old is not None and new is not None else None})
        result.append(row)
    return result


def metric_rows(acc):
    out=[]
    for (scope,value,h),items in sorted(acc.items()):
        net=items['net'];n=len(net)
        out.append({'scope':scope,'value':value,'horizon_hours':h,
            'observed_signals':items['observed'],'evaluated':n,
            'right_censored':items['observed']-n-items['fill_unavailable'],
            'paper_fill_unavailable':items['fill_unavailable'],
            'directional_positive_percent':100*items['favorable']/n if n else None,
            'net_positive_percent_paper':100*sum(x>0 for x in net)/n if n else None,
            'mean_paper_net_pct':statistics.mean(net) if n else None,
            'median_paper_net_pct':statistics.median(net) if n else None})
    return out


def scan(path, *, baseline_rows=None, legacy_fields=None, delta_writer=None):
    saved={} if baseline_rows is None else None
    seen=set();events=set();windows=defaultdict(list);censors=Counter()
    acc=defaultdict(lambda:{'observed':0,'favorable':0,'net':[],'fill_unavailable':0})
    combinations=defaultdict(lambda:{'observed':0,'favorable':0,'net':[],'fill_unavailable':0})
    common=0;added=[];reclassified=[];unavailable_fills={}
    with path.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        legacy_fields=legacy_fields or tuple(reader.fieldnames)
        for r in reader:
            key=identity(r)
            if key in seen:
                raise ValueError(f'Duplicate pattern/horizon row: {key}')
            seen.add(key);events.add(key[:4])
            if saved is not None:
                saved[key]=tuple(r[k] for k in legacy_fields)
            else:
                previous=baseline_rows.pop(key,None)
                if previous is not None:
                    if previous != tuple(r[k] for k in legacy_fields):
                        old=dict(zip(legacy_fields,previous))
                        fill_fields=('entry_next_open_utc','exit_close_utc',
                                     'directional_favorable_pct','paper_net_pct')
                        if (old['outcome_status']!='evaluated' or
                                r['outcome_status']!='unavailable_fixed_time_fill' or
                                any(r[k] for k in fill_fields) or
                                any(r[k]!=old[k] for k in legacy_fields
                                    if k not in (*fill_fields,'outcome_status'))):
                            raise ValueError(f'Unexpected existing outcome or feature change: {key}')
                        changed={**{k:r[k] for k in KEY},'reason':r['censor_reason'],
                                 **{k+'_before':old[k] for k in fill_fields},
                                 'outcome_status_after':r['outcome_status']}
                        reclassified.append(changed)
                        delta_writer.writerow({'change':'zero_volume_fill_unavailable',
                                               **{k:r[k] for k in legacy_fields}})
                    else:
                        common+=1
                else:
                    added.append(key)
                    delta_writer.writerow({'change':'added',**{k:r[k] for k in legacy_fields}})
            evaluated=r['outcome_status']=='evaluated'
            fill_unavailable=r['outcome_status']=='unavailable_fixed_time_fill'
            if fill_unavailable:
                unavailable_fills[key]=r['censor_reason']
            h=int(r['horizon_hours'])
            group=combinations[identity(r,GROUP)];group['observed']+=1
            group['fill_unavailable']+=fill_unavailable
            if evaluated:
                group['net'].append(float(r['paper_net_pct']))
                group['favorable']+=float(r['directional_favorable_pct'])>0
            for scope,value in (('all','all'),('split',r['split']),('pattern',r['pattern']),
                                ('symbol',r['symbol']),('timeframe',r['timeframe'])):
                a=acc[(scope,value,h)];a['observed']+=1
                a['fill_unavailable']+=fill_unavailable
                if evaluated:
                    a['net'].append(float(r['paper_net_pct']))
                    a['favorable']+=float(r['directional_favorable_pct'])>0
            if evaluated:
                entry=int(r['signal_open_ms'])+current.FRAMES[r['timeframe']]
                windows[(r['symbol'],r['timeframe'],h)].append((entry,entry+h*3_600_000))
            else:
                censors[r.get('censor_reason') or 'legacy_unspecified']+=1
    candle_counts=Counter((s,f,t) for s,f,pattern,t in events)
    overlapping=[]
    for (s,frame,h),intervals in sorted(windows.items()):
        intervals.sort();active=[];pairs=0;max_active=0;with_overlap=0
        for i,(start,end) in enumerate(intervals):
            while active and active[0]<=start:
                heapq.heappop(active)
            pairs+=len(active)
            # All windows in this group have the same clock duration.
            with_overlap+=bool(active or (i+1<len(intervals) and intervals[i+1][0]<end))
            heapq.heappush(active,end);max_active=max(max_active,len(active))
        overlapping.append({'symbol':s,'timeframe':frame,'horizon_hours':h,
            'evaluated_windows':len(intervals),'windows_overlapping_another':with_overlap,
            'overlap_pair_count':pairs,'maximum_concurrent_windows':max_active})
    return {'row_count':len(seen),'unique_pattern_events':len(events),
        'signal_candles_with_multiple_patterns':sum(n>1 for n in candle_counts.values()),
        'duplicate_pattern_horizon_rows':0,'common_rows_unchanged':common,
        'common_rows_reclassified_zero_volume_fill':len(reclassified),
        'outcome_unavailable_primary_reason_counts':dict(censors),'overlap_by_symbol_frame_horizon':overlapping,
        'aggregate_metrics':metric_rows(acc)}, saved, legacy_fields, events, added, combinations, reclassified, unavailable_fills


def validate_summary(summary, combinations):
    seen=set()
    for row in summary:
        key=identity(row,GROUP)
        if key in seen:
            raise ValueError(f'Duplicate summary group: {key}')
        seen.add(key)
        data=combinations.get(key,{'observed':0,'favorable':0,'net':[],'fill_unavailable':0})
        net=data['net'];n=len(net)
        expected={'observed_signals':data['observed'],'evaluated':n,
            'right_censored':data['observed']-n-data['fill_unavailable'],
            'paper_fill_unavailable':data['fill_unavailable'],
            'directional_positive_percent':round(100*data['favorable']/n,4) if n else None,
            'net_positive_percent_paper':round(100*sum(x>0 for x in net)/n,4) if n else None,
            'mean_paper_net_pct':round(statistics.mean(net),6) if n else None,
            'median_paper_net_pct':round(statistics.median(net),6) if n else None}
        if any(row.get(k,0 if k=='paper_fill_unavailable' else None)!=v for k,v in expected.items()):
            raise ValueError(f'Summary differs from independently counted CSV rows: {key}')
    if combinations.keys()-seen:
        raise ValueError('Observed combinations omitted from summary')


class ExactConstants(ast.NodeTransformer):
    def visit_Constant(self,node):
        if isinstance(node.value,float):
            return ast.copy_location(ast.Call(func=ast.Name(id='Decimal',ctx=ast.Load()),
                args=[ast.Constant(str(node.value))],keywords=[]),node)
        return node


def compare_timeframe(hourly,higher,frame):
    step=current.FRAMES[frame];hour=current.FRAMES['1h'];groups=defaultdict(list)
    for b in hourly:
        groups[b.open_ms//step*step].append(b)
    checked=0;skipped=0;mismatches=[]
    def aggregate(bars):
        return {'open':bars[0].o,'high':max(b.h for b in bars),'low':min(b.l for b in bars),
                'close':bars[-1].c,'volume':sum(b.v for b in bars)}
    for b in higher:
        bars=groups.get(b.open_ms,[])
        if (len(bars)!=step//hour or bars[0].open_ms!=b.open_ms or
                bars[-1].open_ms!=b.open_ms+step-hour):
            skipped+=1;continue
        checked+=1;expected=aggregate(bars)
        actual={'open':b.o,'high':b.h,'low':b.l,'close':b.c,'volume':b.v}
        differences={k:{'native':str(actual[k]),'aggregated_1h':str(v)}
                     for k,v in expected.items() if actual[k]!=v}
        if differences:
            traded=[x for x in bars if x.v>0]
            matches_traded=bool(traded and aggregate(traded)==actual)
            mismatches.append({'open_ms':b.open_ms,'open_utc':current.utc(b.open_ms),
                'differences':differences,'zero_volume_hours':sum(x.v==0 for x in bars),
                'matches_nonzero_volume_hours':matches_traded})
    return {'checked_complete_windows':checked,'skipped_incomplete_hourly_windows':skipped,
        'mismatch_windows':len(mismatches),
        'unexplained_mismatch_windows':sum(not x['matches_nonzero_volume_hours'] for x in mismatches),
        'mismatches':mismatches}


def exact_reference(archive, lock, baseline_path, before_events, after_events, unavailable_fills):
    # Execute the ORIGINAL v1 Boolean predicates with Decimal CSV prices and
    # decimal constants. This changes only arithmetic, not one pattern rule.
    spec=importlib.util.spec_from_file_location('_p1_original_review_reference',baseline_path)
    baseline=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=baseline;spec.loader.exec_module(baseline)
    tree=ast.fix_missing_locations(ExactConstants().visit(ast.parse(inspect.getsource(baseline.confirmed_at))))
    ns=dict(baseline.__dict__,Decimal=Decimal)
    exec(compile(tree,'<original-v1-exact-decimal>','exec'),ns)
    exact=ns['confirmed_at']
    oracle_before=set();oracle_after=set();counts=Counter();trace=[];files={};expected_unavailable={}
    cross_frames={}
    with ZipFile(archive) as z:
        coverage_raw=z.read('multi_asset_coverage.json')
        if current.sha(coverage_raw)!=lock['frozen_coverage_sha256']:
            raise ValueError('Locked historical coverage evidence changed')
        coverage=json.loads(coverage_raw)
        for fname,meta in lock['frozen_data_prefixes'].items():
            raw=z.read(fname)
            if current.sha(raw)!=meta['sha256'] or len(raw)!=meta['bytes']:
                raise ValueError(f'Input bytes changed: {fname}')
            symbol=meta['symbol'];frame=meta['interval'];step=current.FRAMES[frame]
            old=baseline.parse_bars(raw,symbol,frame,meta['frozen_bars'])
            records=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
            dec=[baseline.Candle(int(r['open_time']),*(Decimal(r[k]) for k in
                 ('open','high','low','close','volume'))) for r in records]
            starts=current.contiguous_segment_start(old,step)
            gaps=[{'after_open_ms':a.open_ms,'next_open_ms':b.open_ms,
                   'missing_bars':(b.open_ms-a.open_ms)//step-1}
                  for a,b in zip(old,old[1:]) if b.open_ms-a.open_ms>step]
            original=coverage['symbols'][symbol]['intervals'][frame]
            if (len(gaps)!=original['gap_segments'] or
                    sum(g['missing_bars'] for g in gaps)!=original['missing_bars_in_observed_range']):
                raise ValueError(f'Gap audit differs from frozen source: {fname}')
            if old[-1].open_ms+step!=meta['frozen_last_close_exclusive_ms']:
                raise ValueError(f'Wrong final closed candle: {fname}')
            for i in range(len(old)):
                left=set(baseline.confirmed_at(old,i,starts[i]));right=set(exact(dec,i,starts[i]))
                oracle_before.update((symbol,frame,p,str(old[i].open_ms)) for p,side in left)
                oracle_after.update((symbol,frame,p,str(old[i].open_ms)) for p,side in right)
                for h in current.HORIZONS_HOURS:
                    k=i+h*3_600_000//step
                    end=old[-1].open_ms+step
                    boundary=baseline.split_end(baseline.split_for(old[i].open_ms+step),end)
                    if k<len(old) and starts[k]==starts[i] and old[k].open_ms+step<=boundary:
                        entry_zero=dec[i+1].v==0;exit_zero=dec[k].v==0
                        if entry_zero or exit_zero:
                            reason=('zero_volume_entry_and_exit' if entry_zero and exit_zero
                                    else 'zero_volume_entry' if entry_zero else 'zero_volume_exit')
                            expected_unavailable.update({(symbol,frame,p,str(old[i].open_ms),str(h)):reason
                                                         for p,side in right})
                for action,patterns in (('added',right-left),('removed',left-right)):
                    for pattern,side in sorted(patterns):
                        counts[action+':'+pattern]+=1
                        trace.append({'change':action,'symbol':symbol,'timeframe':frame,
                            'pattern':pattern,'signal_open_ms':old[i].open_ms,
                            'original_csv_candles':[{k:r[k] for k in
                                ('open_time','open','high','low','close')}
                                for r in records[max(0,i-2):i+1]]})
            files[fname]={'sha256':current.sha(raw),'bars':len(old),'gap_segments':len(gaps),
                'missing_bars':sum(g['missing_bars'] for g in gaps),'gaps':gaps,
                'zero_volume_bars':sum(b.v==0 for b in dec)}
        for symbol in lock['symbols']:
            def read(frame):
                name=f'{symbol}_{frame}.csv';meta=lock['frozen_data_prefixes'][name]
                return current.parse_bars(z.read(name),symbol,frame,meta['frozen_bars'])
            hourly=read('1h')
            for frame in ('4h','1d'):
                cross_frames[f'{symbol}_{frame}']=compare_timeframe(hourly,read(frame),frame)
    if oracle_before!=before_events or oracle_after!=after_events:
        raise ValueError('CSV events differ from the original predicates / exact-decimal reference')
    if expected_unavailable!=unavailable_fills:
        raise ValueError('Zero-volume unavailable fills differ from the independent raw-data audit')
    return {'exact_reference_matches_all_events':True,'numeric_event_changes':dict(counts),
            'changed_events_with_source_prices':trace,'source_files':files,
            'all_unavailable_fills_match_zero_volume_source_bars':True,
            'cross_timeframe_aggregation':cross_frames,'coverage_sha256':current.sha(coverage_raw)}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--before',type=Path,required=True)
    p.add_argument('--after',type=Path,required=True)
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--baseline-code',type=Path,required=True)
    p.add_argument('--lock',type=Path,required=True)
    p.add_argument('--out-dir',type=Path,required=True)
    args=p.parse_args(argv);args.out_dir.mkdir(parents=True,exist_ok=True)
    if current.sha(args.baseline_code.read_bytes())!=BASELINE_CODE_SHA256:
        raise ValueError('Baseline code is not the pinned PR #7 revision')
    lock=current.read_lock(args.lock)
    bm=read_json(args.before/'p1_01_manifest.json');am=read_json(args.after/'p1_01_manifest.json')
    if bm['input_sha256']!=am['input_sha256'] or bm['bars_by_file']!=am['bars_by_file']:
        raise ValueError('The before/after source datasets differ')
    if len(am['input_sha256'])!=24 or am['lock_sha256']!=current.LOCK_SHA256:
        raise ValueError('Wrong review universe or lock')
    if am['research_code_sha256']!=current.sha(Path(current.__file__).read_bytes()):
        raise ValueError('After results were produced by a different research source')
    before,saved,fields,b_events,_,b_combinations,_,_=scan(args.before/'p1_01_events.csv')
    delta_fields=['change',*fields]
    with (args.out_dir/'p1_01_event_delta.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=delta_fields);writer.writeheader()
        after,_,_,a_events,added,a_combinations,reclassified,unavailable_fills=scan(args.after/'p1_01_events.csv',baseline_rows=saved,
                                     legacy_fields=fields,delta_writer=writer)
        removed=len(saved)
        for row in saved.values():
            writer.writerow({'change':'removed',**dict(zip(fields,row))})
    b_summary=read_json(args.before/'p1_01_summary.json')
    a_summary=read_json(args.after/'p1_01_summary.json')
    validate_summary(b_summary,b_combinations);validate_summary(a_summary,a_combinations)
    grouped=comparison(b_summary,a_summary,GROUP)
    aggregate=comparison(before['aggregate_metrics'],after['aggregate_metrics'],
                         ('scope','value','horizon_hours'))
    metric_fields=[x+suffix for x in METRICS for suffix in ('_before','_after','_delta')]
    write_csv(args.out_dir/'p1_01_full_comparison.csv',grouped,[*GROUP,*metric_fields])
    write_csv(args.out_dir/'p1_01_aggregate_comparison.csv',aggregate,
              ['scope','value','horizon_hours',*metric_fields])
    fill_fields=[*KEY,'reason','entry_next_open_utc_before','exit_close_utc_before',
                 'directional_favorable_pct_before','paper_net_pct_before','outcome_status_after']
    write_csv(args.out_dir/'p1_01_reclassified_fills.csv',reclassified,fill_fields)
    if len(grouped)!=1080 or len(a_summary)!=1080:
        raise ValueError('Expected all 8 x 3 x 5 x 3 splits x 3 horizons, including zeros')
    reference=exact_reference(args.archive,lock,args.baseline_code,b_events,a_events,unavailable_fills)
    report={'baseline_commit':BASELINE_COMMIT,
        'baseline_code_sha256':current.sha(args.baseline_code.read_bytes()),
        'review_code_sha256':current.sha(Path(__file__).read_bytes()),
        'research_code_sha256':current.sha(Path(current.__file__).read_bytes()),
        'archive_sha256':current.sha(args.archive.read_bytes()),'input_files_identical':True,
        'all_summary_metrics_match_independent_csv_counts':True,
        'common_legacy_rows_byte_for_field_identical':after['common_rows_unchanged'],
        'common_rows_reclassified_zero_volume_fill':len(reclassified),
        'added_horizon_rows':len(added),'removed_horizon_rows':removed,
        'before':before,'after':after,'reference':reference,
        'interpretation':'Pooled overlapping event outcomes; no portfolio, statistical independence, selection or tradable win-rate claim.'}
    (args.out_dir/'p1_01_review_audit.json').write_text(
        json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({'before_events':len(b_events),'after_events':len(a_events),
        'added_horizon_rows':len(added),'removed_horizon_rows':removed,
        'common_rows_unchanged':after['common_rows_unchanged'],
        'reclassified_zero_volume_fill_rows':len(reclassified),
        'complete_summary_groups':len(grouped),'exact_reference_matches':True}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
