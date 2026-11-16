#!/usr/bin/env python3
"""P1-02: independent, descriptive preexisting support/resistance context for P1-01.

Research-only: spot closed 1H/4H/1D; all pivots fully confirmed BEFORE
pattern onset. Does not change formal trading rules, tune on opened Test, or
claim a profit edge from overlapping observational returns.
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
from statistics import mean, median
from typing import Any
from zipfile import ZipFile

VERSION = 'p1-preexisting-zone-context-v1.1-review-propagation'
P1_RUN = 43857787070
BASELINE_RUN = 43820018876
LOCK_SHA = '42c0d049a8f277934b2908767869e1c6830009c47f346463140c9abac7549dbf'
P1_EVENTS_SHA = '71b7edf1ca06ba1a4b50cf74c3189b29ccdab344983ee6b2dc37ca5b33937da7'
P1_MANIFEST_SHA = '5baa4b6e63eb91d4591811954a3980eff343994e5fb86425c042cce5f79f88c1'
FRAMES = {'1h': 3_600_000, '4h': 14_400_000, '1d': 86_400_000}
PATTERN_LENGTH = {'bullish_engulfing':2,'bearish_engulfing':2,'morning_star':3,
                  'three_white_soldiers':3,'three_black_crows':3}
FRAME_REF = {'1h':'4h','4h':'1d','1d':'1d'}
PATTERN_SIDE = {'bullish_engulfing':'long','bearish_engulfing':'short','morning_star':'long',
                'three_white_soldiers':'long','three_black_crows':'short'}
HORIZONS = (24,72,168)
LOOKBACK_DAYS = 180  # fixed before inspection of outcomes, descriptive only
PIVOT_LEFT = 2
PIVOT_RIGHT = 2
CLUSTER_HALF_WIDTH_ATR = 0.5
MAX_PROXIMITY_ATR = 0.75
MIN_CONFIRMED_PIVOTS = 2
LOCATION_CLASSES = ('near_confirmed_zone','near_single_pivot_unconfirmed',
                    'far_from_nearest_pivot','no_preexisting_pivot','insufficient_prior_atr',
                    'reference_data_gap_or_stale')


def sha(b:bytes)->str:
    return hashlib.sha256(b).hexdigest()


def _json(b:bytes)->dict[str,Any]:
    result=json.loads(b)
    if not isinstance(result,dict): raise ValueError('Expected JSON object')
    return result


def _asfinite(text: Any, *, positive:bool=False)->float:
    x=float(text)
    if not math.isfinite(x) or (positive and x<=0): raise ValueError('Invalid numeric data')
    return x


def checked_sources(frozen:ZipFile, artifact:ZipFile, lock:dict[str,Any])->tuple[dict[str,Any],bytes]:
    manifest_bytes=artifact.read('p1_01_manifest.json')
    events_bytes=artifact.read('p1_01_events.csv')
    if sha(manifest_bytes)!=P1_MANIFEST_SHA or sha(events_bytes)!=P1_EVENTS_SHA:
        raise ValueError('P1-01 official GitHub source evidence drift')
    m=_json(manifest_bytes)
    if (m.get('version')!='p1-candlestick-geometry-v1.1' or
        m.get('status')!='exploratory_frozen_history_only' or
        m.get('source_run')!=BASELINE_RUN or
        m.get('lock_sha256')!=LOCK_SHA or
        m.get('observed_pattern_horizon_rows')!=319299 or
        m.get('unique_pattern_events')!=106433 or
        m.get('fixed_horizons_hours')!=list(HORIZONS) or
        set(m.get('input_sha256',{}))!=set(lock['frozen_data_prefixes'])):
        raise ValueError('P1-01 manifest metadata incompatible with study contract')
    if set(m.get('patterns', []))!=set(PATTERN_LENGTH) or set(m.get('timeframes', []))!=set(FRAMES):
        raise ValueError('P1 patterns/timeframes changed')
    for fname,entry in lock['frozen_data_prefixes'].items():
        raw=frozen.read(fname)
        if (sha(raw)!=entry['sha256'] or sha(raw)!=m['input_sha256'].get(fname)
            or len(raw)!=entry['bytes']):
            raise ValueError(f'Frozen historical file drift: {fname}')
    return m,events_bytes



class DirectoryEvidence:
    """Directly read GH-downloaded, named root files without re-zipping."""
    def __init__(self,path:Path):self.path=path
    def read(self,name:str)->bytes:
        if not name or '/' in name or '\\' in name or name.startswith('.'):
            raise ValueError('Unsafe evidence filename')
        return (self.path/name).read_bytes()


@contextmanager
def open_evidence(path:Path):
    if path.is_dir():yield DirectoryEvidence(path)
    else:
        with ZipFile(path) as artifact:yield artifact



@dataclass(frozen=True)
class Bar:
    t:int
    o:float
    h:float
    l:float
    c:float


def bars_from_csv(raw:bytes,symbol:str,frame:str,expected_count:int)->list[Bar]:
    if frame not in FRAMES: raise ValueError('Subhour / unsupported timeframe forbidden')
    step=FRAMES[frame]
    rows=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'),newline=''))
    if not {'symbol','interval','open_time','close_time','is_closed','open','high','low','close','volume','source'}.issubset(rows.fieldnames or []):
        raise ValueError('Invalid historical CSV header')
    result=[];last=-1
    for x in rows:
        t=int(x['open_time'])
        if (x['symbol']!=symbol or x['interval']!=frame or x['is_closed']!='true'
            or x['source']!='Binance Spot REST API' or t%step or t<=last
            or int(x['close_time'])!=t+step-1):
            raise ValueError('Invalid, duplicate or unclosed source candle')
        o,h,l,c=(_asfinite(x[k],positive=True) for k in ('open','high','low','close'))
        _asfinite(x['volume'])
        if l>min(o,c) or h<max(o,c) or l>h or float(x['volume'])<0:
            raise ValueError('Bad OHLCV source')
        result.append(Bar(t,o,h,l,c));last=t
    if len(result)!=expected_count:raise ValueError('Frozen source bar count mismatch')
    return result


def contiguous_starts(bars:list[Bar],step:int)->list[int]:
    out=[]
    for i,b in enumerate(bars):
        out.append(out[-1] if i and b.t-bars[i-1].t==step else i)
    return out


def confirmed_pivots(bars:list[Bar],starts:list[int])->tuple[list[tuple[int,int,float]],list[tuple[int,int,float]]]:
    """(pivot_index, confirmation_index, price), sorted by pivot index.

    p+2 close is the earliest legitimate availability, and P1-02 further
    requires that confirmation CLOSES before the first pattern candle opens.
    """
    if len(starts)!=len(bars):raise ValueError('Start-array mismatch')
    lows=[];highs=[]
    for p in range(PIVOT_LEFT,len(bars)-PIVOT_RIGHT):
        if starts[p+PIVOT_RIGHT] != starts[p-PIVOT_LEFT]:continue
        x=bars[p]
        neighbors=bars[p-2:p]+bars[p+1:p+3]
        if all(x.l<q.l for q in neighbors):lows.append((p,p+2,x.l))
        if all(x.h>q.h for q in neighbors):highs.append((p,p+2,x.h))
    return lows,highs


@dataclass(frozen=True)
class PivotSource:
    rows: list[tuple[int,int,float]]
    pivot_indices: list[int]
    confirmed_exclusive_ms: list[int]
    pivot_open_ms: list[int]

    @classmethod
    def from_rows(cls, rows:list[tuple[int,int,float]], bars:list[Bar],step:int)->'PivotSource':
        return cls(rows, [x[0] for x in rows], [bars[x[1]].t+step for x in rows],
                   [bars[x[0]].t for x in rows])


@dataclass(frozen=True)
class Context:
    frame:str
    bars:list[Bar]
    starts:list[int]
    timestamps:list[int]
    lows:PivotSource
    highs:PivotSource

    @classmethod
    def from_bars(cls,frame:str,bars:list[Bar])->'Context':
        starts=contiguous_starts(bars,FRAMES[frame])
        lo,hi=confirmed_pivots(bars,starts)
        return cls(frame,bars,starts,[b.t for b in bars],
                   PivotSource.from_rows(lo,bars,FRAMES[frame]),
                   PivotSource.from_rows(hi,bars,FRAMES[frame]))


def classify(pattern_ctx:Context, reference_ctx:Context, i:int,pattern:str,
             atr:float|None)->dict[str,Any]:
    """Only fully closed reference-frame pivots available BEFORE pattern begins.

    1H pattern sees previously closed 4H, 4H sees 1D, 1D sees earlier 1D.
    The in-progress reference candle is never consulted.
    """
    bars=pattern_ctx.bars
    starts=pattern_ctx.starts
    if pattern not in PATTERN_LENGTH or not 0<=i<len(bars):raise ValueError('Unknown pattern/index')
    if FRAME_REF.get(pattern_ctx.frame)!=reference_ctx.frame:
        raise ValueError('High-frame reference mapping changed')
    first=i-PATTERN_LENGTH[pattern]+1
    if first<0 or first<starts[i]:raise ValueError('Pattern crosses a historical data gap')
    side=PATTERN_SIDE[pattern]
    if atr is None or not math.isfinite(atr) or atr<=0:
        return dict(location='insufficient_prior_atr',pivots_near=0,
                    nearest_distance_atr=None,zone_reference=None,
                    preexisting_pivots_in_window=0)
    extreme=min(x.l for x in bars[first:i+1]) if side=='long' else max(x.h for x in bars[first:i+1])
    first_ms=bars[first].t
    ref_step=FRAMES[reference_ctx.frame]
    required_closed_end=(first_ms//ref_step)*ref_step
    j=bisect.bisect_right(reference_ctx.timestamps,first_ms-ref_step)-1
    if j<0:
        return dict(location='no_preexisting_pivot',pivots_near=0,
                    nearest_distance_atr=None,zone_reference=None,
                    preexisting_pivots_in_window=0)
    if reference_ctx.bars[j].t+ref_step!=required_closed_end:
        return dict(location='reference_data_gap_or_stale',pivots_near=0,
                    nearest_distance_atr=None,zone_reference=None,
                    preexisting_pivots_in_window=0)
    pivots=reference_ctx.lows if side=='long' else reference_ctx.highs
    earliest_ms=first_ms-LOOKBACK_DAYS*86_400_000
    left=bisect.bisect_left(pivots.pivot_open_ms,earliest_ms)
    # Ref must be in the same contiguous segment as latest confirmed ref candle.
    left=max(left,bisect.bisect_left(pivots.pivot_indices,reference_ctx.starts[j]))
    # As-of = pattern first open: higher timeframe bars ending exactly at this
    # instant are already closed, but never a bar closing AFTER this instant.
    right=bisect.bisect_right(pivots.confirmed_exclusive_ms,first_ms)
    eligible=[(p,val) for p,_,val in pivots.rows[left:right]]
    if not eligible:
        return dict(location='no_preexisting_pivot',pivots_near=0,
                    nearest_distance_atr=None,zone_reference=None,
                    preexisting_pivots_in_window=0)
    anchor_p,anchor=min(eligible,key=lambda pv:abs(extreme-pv[1]))
    near=[price for p,price in eligible if abs(price-anchor)<=CLUSTER_HALF_WIDTH_ATR*atr]
    distance=abs(extreme-anchor)/atr
    if distance<=MAX_PROXIMITY_ATR and len(near)>=MIN_CONFIRMED_PIVOTS:
        location='near_confirmed_zone'
    elif distance<=MAX_PROXIMITY_ATR:
        location='near_single_pivot_unconfirmed'
    else:
        location='far_from_nearest_pivot'
    return dict(location=location,pivots_near=len(near),
                nearest_distance_atr=round(distance,6),zone_reference=round(median(near),10),
                preexisting_pivots_in_window=len(eligible))


def _key(r:dict[str,Any])->tuple[str,str,str,int]:
    return str(r['symbol']),str(r['timeframe']),str(r['pattern']),int(r['signal_open_ms'])


def _summary_accumulate(states:dict, row:dict, location:str)->None:
    k=(row['symbol'],row['timeframe'],row['pattern'],row['split'],int(row['horizon_hours']),location)
    item=states[k]
    item['events']+=1
    if row['outcome_status']=='evaluated':
        v=_asfinite(row['paper_net_pct'])
        item['evaluated']+=1;item['net_sum']+=v;item['net_pos']+=v>0
        favorable=_asfinite(row['directional_favorable_pct'])
        item['directional_pos']+=favorable>0
    elif row['outcome_status']=='right_censored_or_split_boundary':
        item['censored']+=1
    elif row['outcome_status']=='unavailable_fixed_time_fill':
        if row['paper_net_pct'] or row['directional_favorable_pct'] or row.get('censor_reason') not in ('zero_volume_entry','zero_volume_exit'):
            raise ValueError('Unavailable paper fill must have explicit zero-volume cause and no return')
        item['unavailable']+=1
    else:raise ValueError('Unknown P1 observation status')


def build_summary(acc:dict[tuple,dict])->list[dict[str,Any]]:
    ret=[]
    for (sym,frame,pattern,split,h,location),r in sorted(acc.items()):
        n=r['evaluated']
        ret.append(dict(symbol=sym,timeframe=frame,pattern=pattern,split=split,horizon_hours=h,
                        location=location,events=r['events'],evaluated=n,censored=r['censored'],unavailable=r['unavailable'],
                        directional_positive_pct=round(100*r['directional_pos']/n,4) if n else None,
                        paper_net_positive_pct=round(100*r['net_pos']/n,4) if n else None,
                        mean_paper_net_pct=round(r['net_sum']/n,7) if n else None))
    return ret



def compare_strata(summary:list[dict[str,Any]])->list[dict[str,Any]]:
    """Same coin, pattern, timeframe, period and horizon; no best-group selection."""
    paired=defaultdict(dict)
    for r in summary:
        key=tuple(r[k] for k in ('symbol','timeframe','pattern','split','horizon_hours'))
        paired[key][r['location']]=r
    out=[]
    for key,byloc in sorted(paired.items()):
        near=byloc.get('near_confirmed_zone')
        far=byloc.get('far_from_nearest_pivot')
        n=near['evaluated'] if near else 0
        m=far['evaluated'] if far else 0
        out.append(dict(zip(('symbol','timeframe','pattern','split','horizon_hours'),key),
                        near_evaluated=n,far_evaluated=m,
                        near_paper_mean_pct=near['mean_paper_net_pct'] if near else None,
                        far_paper_mean_pct=far['mean_paper_net_pct'] if far else None,
                        mean_diff_near_minus_far_pct=round(near['mean_paper_net_pct']-far['mean_paper_net_pct'],7) if n and m else None,
                        near_net_positive_pct=near['paper_net_positive_pct'] if near else None,
                        far_net_positive_pct=far['paper_net_positive_pct'] if far else None))
    return out


def pool_by_frame(acc:dict[tuple,dict])->list[dict[str,Any]]:
    """Descriptive only: correlated observations and unequal coin weights."""
    group=defaultdict(lambda:dict(events=0,evaluated=0,censored=0,unavailable=0,net_sum=0.,net_pos=0,directional_pos=0))
    for (_sym,frame,_pat,split,h,location),r in acc.items():
        dest=group[(frame,split,h,location)]
        for key in dest:dest[key]+=r[key]
    out=[]
    for (frame,split,h,location),r in sorted(group.items()):
        n=r['evaluated']
        out.append(dict(timeframe=frame,split=split,horizon_hours=h,location=location,
                        events=r['events'],evaluated=n,censored=r['censored'],unavailable=r['unavailable'],
                        paper_net_positive_pct=round(100*r['net_pos']/n,4) if n else None,
                        mean_paper_net_pct=round(r['net_sum']/n,7) if n else None,
                        warning='Descriptive, overlapping multi-coin events; not independent or OOS'))
    return out


def write_csv(path:Path,items:list[dict[str,Any]],columns:list[str])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns,extrasaction='raise');w.writeheader();w.writerows(items)


def analyze(frozen_path:Path, artifact_path:Path,lock_path:Path,out_dir:Path)->dict[str,Any]:
    if sha(lock_path.read_bytes())!=LOCK_SHA:raise ValueError('Pre-registered lock SHA256 mismatch')
    lock=_json(lock_path.read_bytes())
    if lock.get('protocol_version')!='p0-forward-v1' or len(lock.get('symbols',[]))!=8 or len(lock.get('frozen_data_prefixes',{}))!=24:
        raise ValueError('Frozen lock mismatched universe')
    with open_evidence(frozen_path) as frozen, open_evidence(artifact_path) as p1:
        manifest,events_bytes=checked_sources(frozen,p1,lock)
        events=defaultdict(list)
        reader=csv.DictReader(io.StringIO(events_bytes.decode('utf-8-sig'),newline=''))
        needed={'symbol','timeframe','pattern','direction','split','signal_open_ms',
                'atr14_before_signal','horizon_hours','outcome_status','paper_net_pct',
                'directional_favorable_pct'}
        if not needed.issubset(set(reader.fieldnames or [])):raise ValueError('Incomplete P1 result header')
        for row in reader:
            if row['symbol'] not in lock['symbols'] or row['timeframe'] not in FRAMES:
                raise ValueError('Unknown P1 source symbol/timeframe')
            if row['pattern'] not in PATTERN_LENGTH or row['direction']!=PATTERN_SIDE[row['pattern']]:
                raise ValueError('Invalid P1 pattern/direction')
            if int(row['horizon_hours']) not in HORIZONS:raise ValueError('P1 horizon drift')
            events[(row['symbol'],row['timeframe'])].append(row)
        if sum(len(v) for v in events.values())!=manifest['observed_pattern_horizon_rows']:
            raise ValueError('Missing P1 historical event rows')
        unique_out=[];states=defaultdict(lambda:dict(events=0,evaluated=0,censored=0,unavailable=0,net_sum=0.,net_pos=0,directional_pos=0))
        loc_counter=Counter();split_counter=Counter();n_unique=0
        for sym in lock['symbols']:
            contexts={}
            for f in FRAMES:
                name=f'{sym}_{f}.csv'
                bars=bars_from_csv(frozen.read(name),sym,f,lock['frozen_data_prefixes'][name]['frozen_bars'])
                contexts[f]=Context.from_bars(f,bars)
            for frame in FRAMES:
                pattern_context=contexts[frame]
                reference_context=contexts[FRAME_REF[frame]]
                bar_index={bar.t:i for i,bar in enumerate(pattern_context.bars)}
                grouped=defaultdict(list)
                for event in events[(sym,frame)]:grouped[_key(event)].append(event)
                for key,three in sorted(grouped.items()):
                    if len(three)!=3 or sorted(int(x['horizon_hours']) for x in three)!=list(HORIZONS):
                        raise ValueError('Horizon group must contain exactly 24/72/168h')
                    row=three[0];i=bar_index.get(int(row['signal_open_ms']))
                    if i is None:raise ValueError('Signal time missing from frozen source')
                    if any((x['split'],x['direction'],x['atr14_before_signal']) != (row['split'],row['direction'],row['atr14_before_signal']) for x in three):
                        raise ValueError('P1 event metadata inconsistent across horizons')
                    atr=(_asfinite(row['atr14_before_signal'],positive=True) if row['atr14_before_signal'] else None)
                    status=classify(pattern_context,reference_context,i,row['pattern'],atr)
                    unique_out.append(dict(symbol=sym,timeframe=frame,reference_frame=FRAME_REF[frame],pattern=row['pattern'],
                                           direction=row['direction'],split=row['split'],
                                           signal_open_ms=int(row['signal_open_ms']),
                                           prior_atr14=round(atr,8) if atr is not None else None,
                                           **status))
                    n_unique+=1;loc_counter[status['location']]+=1;split_counter[row['split']]+=1
                    for x in three:_summary_accumulate(states,x,status['location'])
        if n_unique!=manifest['unique_pattern_events']:
            raise ValueError('P1 unique event count mismatch; fail closed')
        summary=build_summary(states)
        if any(x['events']!=x['evaluated']+x['censored']+x['unavailable'] for x in summary):
            raise ValueError('Missing evaluated/censored/unavailable evidence bucket')
        if sum(x['unavailable'] for x in summary)!=24:
            raise ValueError('Expected 24 independently audited unavailable fills')
        if sum(x['evaluated'] for x in summary)!=311873:
            raise ValueError('Revised P1 expected evaluated observation count mismatch')
        if sum(x['events'] for x in summary)!=manifest['observed_pattern_horizon_rows']:
            raise ValueError('Summary accounting mismatch')
        out_dir.mkdir(parents=True,exist_ok=True)
        events_path=out_dir/'p1_02_zone_evidence.csv'
        fields=list(unique_out[0])
        write_csv(events_path,unique_out,fields)
        fields=list(summary[0]);write_csv(out_dir/'p1_02_zone_summary.csv',summary,fields)
        comparisons=compare_strata(summary)
        write_csv(out_dir/'p1_02_near_vs_far_by_stratum.csv',comparisons,list(comparisons[0]))
        pools=pool_by_frame(states)
        write_csv(out_dir/'p1_02_descriptive_by_frame.csv',pools,list(pools[0]))
        report=dict(version=VERSION,status='historical_exploratory_only',decision_timeframes=['1h','4h','1d'],
                    p1_run=P1_RUN,p1_events_sha256=P1_EVENTS_SHA,p1_manifest_sha256=P1_MANIFEST_SHA,
                    lock_sha256=LOCK_SHA,source_run=BASELINE_RUN,
                    source_file_count=len(lock['frozen_data_prefixes']),
                    unique_events=n_unique,observations=manifest['observed_pattern_horizon_rows'],
                    verified_strata_comparisons=len(comparisons),
                    unavailable_fixed_time_fills=sum(x['unavailable'] for x in summary),
                    zone_counts=dict(loc_counter),split_counts=dict(split_counter),
                    fixed_rules=dict(reference_frames=FRAME_REF,pivot_left=2,pivot_right=2,lookback_calendar_days=LOOKBACK_DAYS,
                                     preexisting_only_confirmed_before_pattern_starts=True,
                                     cluster_half_width_atr=CLUSTER_HALF_WIDTH_ATR,
                                     max_distance_atr=MAX_PROXIMITY_ATR,min_pivot_count=MIN_CONFIRMED_PIVOTS),
                    notes=['P1-01 audited correction PR #9: 278 recovered pattern events and 24 unavailable zero-volume fill windows; not a new historical holdout.',
                        'Zone is a causal two-confirmed-pivot price cluster, not evidence of live order book support.',
                        'All reference pivots were confirmed before first pattern candle open.',
                        'Pattern outcomes copied from frozen P1-01; no trade execution or strategy optimization.',
                        'Paper net result is fixed horizon next-open-to-close mark-to-market (Spot shorts not executable).',
                        'Correlated and overlapping candles / crypto pairs cannot be treated as independent trades.',
                        'Historical Test opened; do NOT choose best threshold, pattern, or horizon and call it independent OOS.',
                    ])
        (out_dir/'p1_02_manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        lines=['# P1-02｜型態發生前已確認之支撐／壓力區與績效分層','',
               f'本階段比對 **{n_unique:,}** 個既有 P1-01 型態事件；三個固定觀察期限，無新交易策略。','',
               '## 前置位置分類（所有歷史資料，非獨立樣本外）','',
               '| 類別 | 唯一型態事件 |','|---|---:|']
        for k in LOCATION_CLASSES:lines.append(f'| {k} | {loc_counter[k]:,} |')
        lines+=['','## 方法與限制','',
                '- 1H 型態參考已收盤 4H、4H 型態參考已收盤 1D、1D 型態參考更早 1D；從前 180 日同一連續段中取得「**型態開始前已完成兩根右側確認**」的 2L/2R pivot。',
                '- 以事前的 ATR14 正規化，至少兩個已確認 pivot 在同一群距離範圍，且型態最低／最高價靠近才標記關鍵區。',
                '- 方向觀察與模擬報酬保留 P1-01 原始三個期限及成本；**不挑最優值，不宣稱正期望值**。',
                '- 已看過的 Test 資料不具備新的樣本外地位，未來需要事前鎖定後的資料驗證。','',
                '## 選定規則下的分類個案數（沒有事後擇優）','',
                f'- near_confirmed_zone：{loc_counter["near_confirmed_zone"]:,} 個唯一型態事件。',
                f'- far_from_nearest_pivot：{loc_counter["far_from_nearest_pivot"]:,} 個唯一型態事件。',
                '- `p1_02_near_vs_far_by_stratum.csv` 固定保留**全部**幣種×週期×型態×時段×期限的比較，不因正負績效挑欄。',
                '- `p1_02_descriptive_by_frame.csv` 依週期分開顯示，不把 1D 事件和 1H 事件當成統計獨立樣本。','']
        (out_dir/'p1_02_report_zh.md').write_text('\n'.join(lines),encoding='utf-8')
        return report


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen',type=Path,required=True)
    parser.add_argument('--p1-artifact',type=Path,required=True)
    parser.add_argument('--lock',type=Path,default=Path('PROSPECTIVE_LOCK_V1.json'))
    parser.add_argument('--out-dir',type=Path,default=Path('research/p1_02'))
    args=parser.parse_args(argv)
    report=analyze(args.frozen,args.p1_artifact,args.lock,args.out_dir)
    print(json.dumps({'status':report['status'],'unique_events':report['unique_events'],'zones':report['zone_counts']},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())