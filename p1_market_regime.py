#!/usr/bin/env python3
"""P1-03: immutable-source descriptive MA200 / ATR regime study.

Independently classifies P1-01 candlestick observations using only complete
1h/4h/1d bars available strictly BEFORE the pattern starts; no new trading
signals, orders, return-optimized thresholds, or changes to formal strategy.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import io
import json
import math
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any
from zipfile import ZipFile

VERSION = 'p1-market-regime-ma200-atr-v1.1-review-propagation'
BASELINE_RUN = 43820018876
P1_RUN = 43857787070
LOCK_SHA256 = '42c0d049a8f277934b2908767869e1c6830009c47f346463140c9abac7549dbf'
P1_MANIFEST_SHA = '5baa4b6e63eb91d4591811954a3980eff343994e5fb86425c042cce5f79f88c1'
P1_EVENTS_SHA = '71b7edf1ca06ba1a4b50cf74c3189b29ccdab344983ee6b2dc37ca5b33937da7'
FRAMES = {'1h': 3_600_000, '4h': 14_400_000, '1d': 86_400_000}
PATTERN_BARS = {'bullish_engulfing': 2, 'bearish_engulfing': 2,
                'morning_star': 3, 'three_white_soldiers': 3,
                'three_black_crows': 3}
PATTERN_SIDES = {'bullish_engulfing':'long','bearish_engulfing':'short',
                 'morning_star':'long','three_white_soldiers':'long',
                 'three_black_crows':'short'}
HORIZONS = (24, 72, 168)
MA_PERIOD = 200
SLOPE_BARS = 20
ATR_PERIOD = 14
ATR_HISTORY = 200
LOW_PERCENTILE = 0.25
HIGH_PERCENTILE = 0.75
TRENDS = ('bullish', 'bearish', 'transition_or_range', 'insufficient_prior_ma')
VOLATILITY = ('low', 'normal', 'high', 'insufficient_prior_atr_distribution')
EVENT_KEY = ('symbol','timeframe','pattern','signal_open_ms')
EVIDENCE_COLUMNS = list(EVENT_KEY) + [
    'direction','split','pattern_start_open_ms','reference_closed_at_utc',
    'trend','ma200_prepattern','ma200_slope_20_atr','prior_close_vs_ma200_atr',
    'volatility','prior_atr14_pct','prior_atr14_percentile','regime_data_end_ms']
OBS_COLUMNS = ['symbol','timeframe','pattern','direction','split','horizon_hours',
               'trend','volatility','events','evaluated','censored','unavailable',
               'directional_favorable_percent','paper_net_positive_percent',
               'mean_paper_net_pct']


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def write_csv(path:Path, fields:list[str], rows:list[dict[str,Any]])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore',lineterminator='\n')
        w.writeheader()
        for row in rows:w.writerow(row)


def write_json(path:Path,obj:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def parse_json(b:bytes)->dict[str,Any]:
    result=json.loads(b)
    if not isinstance(result,dict):raise ValueError('Evidence must be a JSON object')
    return result


class FolderEvidence:
    def __init__(self,path:Path):self.path=path
    def read(self,name:str)->bytes:
        if not name or '/' in name or '\\' in name or name.startswith('.'):
            raise ValueError('Unsafe source filename')
        return (self.path/name).read_bytes()


@contextmanager
def evidence(path:Path):
    if path.is_dir():yield FolderEvidence(path)
    else:
        with ZipFile(path) as z:yield z


def verify_inputs(frozen:Any,p1:Any,lock_raw:bytes)->tuple[dict[str,Any],list[dict[str,str]],dict[str,Any]]:
    if sha(lock_raw)!=LOCK_SHA256:
        raise ValueError('Frozen prospective lock SHA256 changed')
    lock=parse_json(lock_raw)
    if lock.get('protocol_version')!='p0-forward-v1' or len(lock.get('symbols',[]))!=8 or len(lock.get('frozen_data_prefixes',{}))!=24:
        raise ValueError('Wrong frozen study universe')
    manifest_raw=p1.read('p1_01_manifest.json')
    events_raw=p1.read('p1_01_events.csv')
    if sha(manifest_raw)!=P1_MANIFEST_SHA or sha(events_raw)!=P1_EVENTS_SHA:
        raise ValueError('Official verified P1-01 evidence changed')
    m=parse_json(manifest_raw)
    if (m.get('version')!='p1-candlestick-geometry-v1.1' or
        m.get('status')!='exploratory_frozen_history_only' or
        m.get('source_run')!=BASELINE_RUN or m.get('lock_sha256')!=LOCK_SHA256 or
        m.get('unique_pattern_events')!=106433 or
        m.get('observed_pattern_horizon_rows')!=319299 or
        m.get('fixed_horizons_hours')!=list(HORIZONS) or
        set(m.get('patterns',[]))!=set(PATTERN_BARS) or
        set(m.get('timeframes',[]))!=set(FRAMES) or
        set(m.get('input_sha256',{}))!=set(lock['frozen_data_prefixes']) or
        set(lock.get('symbols',[]))!={'BTCUSDT','ETHUSDT','SOLUSDT','ZECUSDT','XRPUSDT','BNBUSDT','NEARUSDT','DOGEUSDT'}):
        raise ValueError('P1-01 metadata does not match prior frozen protocol')
    for name,meta in lock['frozen_data_prefixes'].items():
        raw=frozen.read(name)
        if len(raw)!=meta['bytes'] or sha(raw)!=meta['sha256'] or sha(raw)!=m['input_sha256'][name]:
            raise ValueError(f'Original Binance frozen input SHA256 mismatch: {name}')
    reader=csv.DictReader(io.StringIO(events_raw.decode('utf-8-sig'),newline=''))
    required=set(EVENT_KEY)|{'split','direction','horizon_hours','outcome_status',
                             'signal_close_utc','paper_net_pct','directional_favorable_pct'}
    if not required.issubset(reader.fieldnames or []):raise ValueError('P1 events header changed')
    rows=list(reader)
    if len(rows)!=319299:raise ValueError('Wrong original P1-01 observation count')
    return lock,rows,m


@dataclass(frozen=True)
class Candle:
    t:int
    o:float
    h:float
    l:float
    c:float


def parse_bars(raw:bytes,symbol:str,frame:str,expected:int)->list[Candle]:
    if frame not in FRAMES:raise ValueError('Only fully closed 1h / 4h / 1d are allowed')
    step=FRAMES[frame]
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'),newline=''))
    required={'symbol','interval','open_time','close_time','is_closed','source','open','high','low','close','volume'}
    if not required.issubset(reader.fieldnames or []):raise ValueError('Missing source OHLCV header')
    bars=[];prior=-1
    for r in reader:
        t=int(r['open_time'])
        if (r['symbol']!=symbol or r['interval']!=frame or r['source']!='Binance Spot REST API'
            or r['is_closed']!='true' or t%step or int(r['close_time'])!=t+step-1 or t<=prior):
            raise ValueError('Duplicated, unclosed, misaligned or incorrectly sourced candle')
        o,h,l,c,v=(float(r[k]) for k in ('open','high','low','close','volume'))
        if not all(math.isfinite(x) for x in (o,h,l,c,v)) or not(l>0 and v>=0 and l<=min(o,c)<=max(o,c)<=h):
            raise ValueError('OHLCV validation failed')
        bars.append(Candle(t,o,h,l,c));prior=t
    if len(bars)!=expected:raise ValueError('Original bar count differs from P1 manifest')
    return bars


class RegimeSource:
    """All data are from the signal's own closed timeframe; no bar from signal or later."""
    def __init__(self,bars:list[Candle],frame:str):
        if frame not in FRAMES:raise ValueError('Sub-hour frames forbidden')
        self.frame=frame;self.step=FRAMES[frame];self.bars=bars
        self.times=[x.t for x in bars]
        self.starts=[]
        self.close_prefix=[0.0]
        for i,b in enumerate(bars):
            self.starts.append(self.starts[-1] if i and b.t-bars[i-1].t==self.step else i)
            self.close_prefix.append(self.close_prefix[-1]+b.c)
        self.atr=[None]*len(bars)
        self.atr_pct=[None]*len(bars)
        tr_sum=0.0
        tr_values=[0.0]*len(bars)
        for i,b in enumerate(bars):
            st=self.starts[i]
            tr=max(b.h-b.l,abs(b.h-bars[i-1].c),abs(b.l-bars[i-1].c)) if i>st else b.h-b.l
            tr_values[i]=tr
            if i==st:tr_sum=0.0
            tr_sum+=tr
            if i-st>=ATR_PERIOD:tr_sum-=tr_values[i-ATR_PERIOD]
            if i-st>=ATR_PERIOD:
                v=tr_sum/ATR_PERIOD
                self.atr[i]=v;self.atr_pct[i]=v/b.c
            if i+1==len(bars) or bars[i+1].t-b.t!=self.step:
                tr_sum=0.0

    def ma(self,end:int,period:int=MA_PERIOD)->float:
        if period<=0 or end-period+1<self.starts[end]:raise ValueError('Incomplete MA window')
        return (self.close_prefix[end+1]-self.close_prefix[end+1-period])/period

    def classify(self,signal_open_ms:int,pattern:str)->dict[str,Any]:
        if pattern not in PATTERN_BARS:raise ValueError('Unknown pattern')
        idx=bisect.bisect_left(self.times,signal_open_ms)
        if idx>=len(self.times) or self.times[idx]!=signal_open_ms:
            raise ValueError('P1 signal cannot be mapped onto frozen candle')
        first=idx-PATTERN_BARS[pattern]+1
        if first<0 or first<self.starts[idx] or self.times[first]+(PATTERN_BARS[pattern]-1)*self.step!=signal_open_ms:
            raise ValueError('Original P1 pattern crosses a gap or incomplete history')
        # Last usable bar is the one whose CLOSE is no later than pattern onset.
        p=first-1
        if p<self.starts[first]:
            return dict(pattern_start_open_ms=self.times[first],reference_closed_at_utc=None,
                        trend='insufficient_prior_ma',ma200_prepattern=None,ma200_slope_20_atr=None,
                        prior_close_vs_ma200_atr=None,volatility='insufficient_prior_atr_distribution',
                        prior_atr14_pct=None,prior_atr14_percentile=None,regime_data_end_ms=None)
        if self.times[p]+self.step!=self.times[first]:
            raise ValueError('Prior historical bar missing: no forced regime propagation')
        segment=self.starts[p]
        prev_close=self.bars[p].c
        atr=self.atr[p]
        # Fixed rolling window of ATR14/close, all available strictly before pattern.
        vol='insufficient_prior_atr_distribution';atr_pct=None;percentile=None
        if atr is not None and p-ATR_HISTORY+1>=segment+ATR_PERIOD:
            hist=self.atr_pct[p-ATR_HISTORY+1:p+1]
            if len(hist)!=ATR_HISTORY or any(x is None for x in hist):
                raise ValueError('Incomplete ATR history in supposedly contiguous block')
            cur=self.atr_pct[p]
            rank=(sum(x<cur for x in hist)+0.5*sum(x==cur for x in hist))/len(hist)
            percentile=rank
            vol='low' if rank<=LOW_PERCENTILE else 'high' if rank>=HIGH_PERCENTILE else 'normal'
            atr_pct=100*cur
        trend='insufficient_prior_ma';ma=None;slope=None;dist=None
        if p-SLOPE_BARS-MA_PERIOD+1>=segment and atr is not None and atr>0:
            ma=self.ma(p);prev=self.ma(p-SLOPE_BARS)
            slope=(ma-prev)/atr;dist=(prev_close-ma)/atr
            trend=('bullish' if prev_close>ma and slope>0 else
                   'bearish' if prev_close<ma and slope<0 else 'transition_or_range')
        return dict(pattern_start_open_ms=self.times[first],
                    reference_closed_at_utc=datetime.fromtimestamp(self.times[first]/1000,timezone.utc).isoformat(),
                    trend=trend,ma200_prepattern=ma,ma200_slope_20_atr=slope,
                    prior_close_vs_ma200_atr=dist,volatility=vol,prior_atr14_pct=atr_pct,
                    prior_atr14_percentile=percentile,regime_data_end_ms=self.times[first])


def audit_observations(rows:list[dict[str,str]])->tuple[dict[tuple[str,str],list[dict[str,str]]],int]:
    grouped=defaultdict(list)
    seen=set();last_key=None
    for r in rows:
        sym,frame,pat=(r[k] for k in ('symbol','timeframe','pattern'))
        if frame not in FRAMES or pat not in PATTERN_BARS:raise ValueError('Unsupported P1-01 pattern/timeframe')
        if r['direction']!=PATTERN_SIDES[pat]:raise ValueError('P1 event side incorrect')
        k=tuple(r[f] for f in EVENT_KEY)
        horizon=int(r['horizon_hours'])
        if horizon not in HORIZONS:raise ValueError('Altered P1 event horizon')
        unique=k+(str(horizon),)
        if unique in seen:raise ValueError('Duplicate event/horizon')
        seen.add(unique)
        grouped[(sym,frame)].append(r)
    grouped_keys=Counter(tuple(r[k] for k in EVENT_KEY) for r in rows)
    if any(v!=len(HORIZONS) for v in grouped_keys.values()):
        raise ValueError('Missing one of 24/72/168 hours for a signal')
    return grouped,len(grouped_keys)


def update_outcome(s:dict[str,Any],r:dict[str,str])->None:
    s['events']+=1
    status=r['outcome_status']
    if status=='evaluated':
        try:
            paper=float(r['paper_net_pct']);directional=float(r['directional_favorable_pct'])
        except (TypeError,ValueError) as exc:
            raise ValueError('Evaluated P1 row has no numerical outcome') from exc
        if not math.isfinite(paper) or not math.isfinite(directional):
            raise ValueError('Nonfinite P1 outcome')
        s['evaluated']+=1
        s['directional_favorable']+=directional>0
        s['paper_net_positive']+=paper>0
        s['paper_nets'].append(paper)
    elif status=='right_censored_or_split_boundary':
        if r['paper_net_pct'] or r['directional_favorable_pct']:
            raise ValueError('Censored event must not carry future outcome')
        s['censored']+=1
    elif status=='unavailable_fixed_time_fill':
        if r['paper_net_pct'] or r['directional_favorable_pct'] or r.get('censor_reason') not in ('zero_volume_entry','zero_volume_exit'):
            raise ValueError('Unavailable paper fill has unexplained status or nonempty returns')
        s['unavailable']+=1
    else:
        raise ValueError('Unexpected revised P1 outcome classification')


def _pct(n:int,total:int)->float|None:
    return round(100*n/total,4) if total else None


def _mean(x:list[float])->float|None:
    return round(mean(x),8) if x else None


def _fmt(x:Any)->str:
    if x is None:return ''
    return format(x,'.10g') if isinstance(x,float) else str(x)


def run_study(frozen:Any,p1:Any,lock_raw:bytes,out_dir:Path)->dict[str,Any]:
    lock,observations,m=verify_inputs(frozen,p1,lock_raw)
    grouped,count=audit_observations(observations)
    if count!=106433:raise ValueError('Official P1 unique patterns changed')
    raw_for=defaultdict(list)
    for r in observations:raw_for[(r['symbol'],r['timeframe'])].append(r)
    evidences=[];summary=defaultdict(lambda:{'events':0,'evaluated':0,'censored':0,'unavailable':0,'directional_favorable':0,'paper_net_positive':0,'paper_nets':[]})
    by_event={}
    raw_sha={}
    for sym in lock['symbols']:
        for frame in FRAMES:
            fname=f'{sym}_{frame}.csv'
            raw=frozen.read(fname)
            raw_sha[fname]=sha(raw)
            bars=parse_bars(raw,sym,frame,m['bars_by_file'][fname])
            context=RegimeSource(bars,frame)
            rows=raw_for[(sym,frame)]
            # Only 1 unique calculation per event, not one per horizon.
            for r in rows:
                k=tuple(r[f] for f in EVENT_KEY)
                if k not in by_event:
                    reg=context.classify(int(r['signal_open_ms']),r['pattern'])
                    row={field:r.get(field,'') for field in EVENT_KEY+('direction','split')}
                    row.update({x:_fmt(v) for x,v in reg.items()})
                    by_event[k]=row
                    evidences.append(row)
                ctx=by_event[k]
                h=int(r['horizon_hours'])
                group=(sym,frame,r['pattern'],r['direction'],r['split'],h,ctx['trend'],ctx['volatility'])
                update_outcome(summary[group],r)
    if (len(evidences)!=106433 or sum(x['events'] for x in summary.values())!=319299 or
            sum(x['unavailable'] for x in summary.values())!=24 or
            sum(x['evaluated'] for x in summary.values())!=311873 or
            any(x['events']!=x['evaluated']+x['censored']+x['unavailable'] for x in summary.values())):
        raise ValueError('Event/horizon totals changed')
    evidences.sort(key=lambda r:(r['symbol'],r['timeframe'],int(r['signal_open_ms']),r['pattern']))
    groups=[]
    for (sym,frame,pat,direction,split,h,trend,vol),v in sorted(summary.items()):
        groups.append(dict(symbol=sym,timeframe=frame,pattern=pat,direction=direction,split=split,horizon_hours=h,
                           trend=trend,volatility=vol,events=v['events'],evaluated=v['evaluated'],censored=v['censored'],unavailable=v['unavailable'],
                           directional_favorable_percent=_pct(v['directional_favorable'],v['evaluated']),
                           paper_net_positive_percent=_pct(v['paper_net_positive'],v['evaluated']),
                           mean_paper_net_pct=_mean(v['paper_nets'])))
    # Aggregate counts of unique events, not three repeated observation rows.
    trend_counts=dict(sorted(Counter(r['trend'] for r in evidences).items()))
    vol_counts=dict(sorted(Counter(r['volatility'] for r in evidences).items()))
    counts_by_frame=Counter((r['timeframe'],r['trend'],r['volatility']) for r in evidences)
    per_frame=[{'timeframe':frame,'trend':trend,'volatility':vol,'events':n}
               for (frame,trend,vol),n in sorted(counts_by_frame.items())]
    out_dir.mkdir(parents=True,exist_ok=True)
    write_csv(out_dir/'p1_03_regime_evidence.csv',EVIDENCE_COLUMNS,evidences)
    write_csv(out_dir/'p1_03_regime_summary.csv',OBS_COLUMNS,groups)
    write_csv(out_dir/'p1_03_regime_counts.csv',['timeframe','trend','volatility','events'],per_frame)
    result={'version':VERSION,'status':'historical_exploratory_only',
            'decision_timeframes':list(FRAMES),'baseline_run':BASELINE_RUN,'p1_run':P1_RUN,
            'source_sha256':raw_sha,'p1_events_sha256':P1_EVENTS_SHA,
            'p1_manifest_sha256':P1_MANIFEST_SHA,'lock_sha256':LOCK_SHA256,
            'unique_events':len(evidences),'observations':sum(x['events'] for x in summary.values()),
            'group_rows':len(groups),'unavailable_fixed_time_fills':24,'trend_counts':trend_counts,'volatility_counts':vol_counts,
            'parameters':{'ma_period':MA_PERIOD,'slope_comparison_bars':SLOPE_BARS,
                          'atr_period':ATR_PERIOD,'atr_percentile_history':ATR_HISTORY,
                          'atr_percentile_low':LOW_PERCENTILE,'atr_percentile_high':HIGH_PERCENTILE,
                          'classification_basis':'strictly before FIRST pattern candle opens',
                          'continuous_ohlcv_only':True,'future_data_used':False,
                          'no_split_based_parameter_selection':True},
            'limitations':['Revised P1-01 PR #9 changes classification of 24 zero-volume paper fills; these are never wins/losses.', 'Descriptive pattern strata only: NOT independent trades or actual fills.',
                           'MA200/ATR thresholds fixed for version 1; not optimized on opened Test.',
                           'Overlapping observations, coin correlations and repeated horizons are dependent.',
                           'Hypothetical Spot shorts are not executable Spot orders.',
                           'Censored rows are never counted as zero return.',
                           'No formal strategy modifications or production orders.']}
    write_json(out_dir/'p1_03_manifest.json',result)
    return result


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description='P1-03 closed 1H/4H/1D MA200/ATR regime context, descriptive only')
    p.add_argument('--frozen',type=Path,required=True)
    p.add_argument('--p1-artifact',type=Path,required=True)
    p.add_argument('--lock',type=Path,default=Path('PROSPECTIVE_LOCK_V1.json'))
    p.add_argument('--out-dir',type=Path,default=Path('research/p1_03'))
    args=p.parse_args(argv)
    with evidence(args.frozen) as frozen,evidence(args.p1_artifact) as p1:
        result=run_study(frozen,p1,args.lock.read_bytes(),args.out_dir)
    print(json.dumps({'status':result['status'],'unique_events':result['unique_events'],
                      'observations':result['observations'],'trends':result['trend_counts'],
                      'volatility':result['volatility_counts']},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())