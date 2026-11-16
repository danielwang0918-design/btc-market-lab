#!/usr/bin/env python3
"""P1-QUALITY-01: deterministic as-of/overlap evidence gate (never trading).

Only reads existing Codex-reviewed P1-01 and independently classified P1-02/P1-03
historical evidence. No new signals, no parameter selection, no new price data.
The observational means below are not independent trades or portfolio returns.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable
from zipfile import ZipFile

VERSION = 'p1-observation-evidence-gate-v1'
P1_RUN = 43857787070
INTEGRATION_RUN = 43862040301
EXPECTED_SHA = {
    'p1_event': '71b7edf1ca06ba1a4b50cf74c3189b29ccdab344983ee6b2dc37ca5b33937da7',
    'p1_manifest': '5baa4b6e63eb91d4591811954a3980eff343994e5fb86425c042cce5f79f88c1',
    'zone': '58ba1109420a887075f654ccfa131e8033bef3afc9972095ee1535eeb7dda4e9',
    'regime': 'b26379ed8f7abc00afd8d2708fa73794b6afd5961dc9167ade6bb1d30446be9c',
    'propagation': '635da2285d73e8564284785228268773f793d6f1efbfe9ec736ce78d5df90a9d',
}
P1_PATH = {'p1_event': 'after/p1_01_events.csv', 'p1_manifest': 'after/p1_01_manifest.json'}
INT_PATH = {'zone': 'zone/p1_02_zone_evidence.csv', 'regime': 'regime/p1_03_regime_evidence.csv',
            'propagation': 'audit/P1_REVIEW_PROPAGATION_RESULT.json'}
HORIZONS = (24, 72, 168)
ALLOWED_FRAMES = {'1h': 3600, '4h': 14400, '1d': 86400}
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'ZECUSDT', 'XRPUSDT', 'BNBUSDT', 'NEARUSDT', 'DOGEUSDT')
PATTERNS = ('bullish_engulfing','bearish_engulfing','morning_star','three_white_soldiers','three_black_crows')
SPLITS = ('train', 'validation', 'exploratory_test_opened')
KNOWN_STATUS = {'evaluated', 'right_censored_or_split_boundary', 'unavailable_fixed_time_fill'}
KEY_FIELDS = ('symbol','timeframe','pattern','signal_open_ms')


def fail(msg: str):
    raise ValueError(msg)


def sha(data: bytes)->str:
    return hashlib.sha256(data).hexdigest()


def checked_members(archive: ZipFile, paths: dict[str,str]) -> dict[str, bytes]:
    names = [i.filename for i in archive.infolist()]
    if len(names) != len(set(names)):
        fail('Duplicate ZIP members - evidence provenance ambiguous')
    if any(x.startswith('/') or '..' in x.split('/') or '\\' in x for x in names):
        fail('Unsafe ZIP member path')
    result: dict[str,bytes] = {}
    for kind, name in paths.items():
        if name not in names:
            fail(f'Missing required evidence {name}')
        content=archive.read(name)
        if sha(content)!=EXPECTED_SHA[kind]:
            fail(f'Frozen {kind} SHA256 drift; no study allowed')
        result[kind]=content
    return result


def csv_rows(raw: bytes) -> list[dict[str,str]]:
    rdr=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''))
    if not rdr.fieldnames or len(rdr.fieldnames)!=len(set(rdr.fieldnames)):
        fail('Duplicated or missing CSV columns')
    result=[]
    for row in rdr:
        if None in row or any(v is None for v in row.values()):
            fail('Broken or ragged evidence row')
        result.append(row)
    return result


def _int(r:dict, key:str)->int:
    s=r.get(key)
    if s is None or not str(s).isdigit(): fail(f'Invalid integer {key}')
    return int(s)


def _key(r:dict) -> tuple[str,str,str,int]:
    key=(r.get('symbol'),r.get('timeframe'),r.get('pattern'),_int(r,'signal_open_ms'))
    if key[0] not in SYMBOLS or key[1] not in ALLOWED_FRAMES or key[2] not in PATTERNS:
        fail(f'Unexpected symbol/timeframe/pattern {key}')
    if key[3]% (ALLOWED_FRAMES[key[1]]*1000):
        fail(f'Unaligned signal timestamp {key}')
    return key


def _millis(s: str)->int:
    if not s: fail('Missing UTC timestamp for completed observation')
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    if d.tzinfo is None or d.utcoffset().total_seconds()!=0:
        fail('Timestamps must be UTC with explicit timezone')
    return int(d.timestamp()*1000)


def _float(s:str)->float:
    try: x=float(s)
    except (TypeError,ValueError): fail('Invalid numeric observation')
    if not math.isfinite(x): fail('Non-finite observation')
    return x


def _status(r:dict)->str:
    s=r.get('outcome_status')
    if s not in KNOWN_STATUS: fail('Unknown observational status')
    if s=='evaluated':
        if r.get('censor_reason'): fail('Evaluated row has censor reason')
        _float(r['paper_net_pct']);_float(r['directional_favorable_pct'])
    else:
        if r.get('paper_net_pct') or r.get('directional_favorable_pct'):
            fail('Censored or unfillable paper observation has a fake return')
        if not r.get('censor_reason'):fail('Missing unavailable/censor reason')
        if s=='unavailable_fixed_time_fill' and r['censor_reason'] not in ('zero_volume_entry','zero_volume_exit'):
            fail('Unexpected simulated-fill failure')
    return s


def index_and_check(events:list[dict[str,str]], zones:list[dict[str,str]], regimes:list[dict[str,str]]):
    """Fail closed on orphan, duplicate and causally impossible joined event evidence."""
    event_idx: dict[tuple,dict[str,str]] = {}
    statuses=Counter()
    pairs=defaultdict(dict)
    evaluated=[]
    for r in events:
        key=_key(r)
        h=_int(r,'horizon_hours')
        if h not in HORIZONS or h in pairs[key]:
            fail('Duplicate/unrecognized horizon for one event')
        if r.get('split') not in SPLITS or r.get('direction') not in ('long','short'):
            fail('Unexpected split or direction')
        if key in event_idx:
            prev=event_idx[key]
            for fld in ('split','direction','signal_close_utc'):
                if prev[fld]!=r[fld]:fail('Per-event identity changed between horizon rows')
        else:event_idx[key]=r
        pairs[key][h]=r
        st=_status(r)
        statuses[st]+=1
        if st=='evaluated':
            entry,exit_=_millis(r['entry_next_open_utc']),_millis(r['exit_close_utc'])
            signal=_millis(r['signal_close_utc'])
            if not signal==entry or exit_-entry!=h*3600*1000:
                fail('Outcome uses future fill, wrong horizon, or impossible paper timing')
            if entry < key[3] + ALLOWED_FRAMES[key[1]]*1000:
                fail('Paper entry precedes complete signal candle')
            evaluated.append((key,h,entry,exit_,_float(r['paper_net_pct']),_float(r['directional_favorable_pct']),r['split']))
    if len(event_idx)!=106433 or len(events)!=319299 or len(evaluated)!=311873:
        fail('Review sample count drift')
    if statuses!=Counter({'evaluated':311873, 'right_censored_or_split_boundary':7402, 'unavailable_fixed_time_fill':24}):
        fail(f'Reviewed observation status drift: {statuses}')
    for k,items in pairs.items():
        if set(items)!=set(HORIZONS):fail('Incomplete 24/72/168 hour outcome coverage')
    zidx={};ridx={}
    for r in zones:
        k=_key(r)
        if k in zidx:fail('Duplicate zone event identity')
        if k not in event_idx:fail('Zone event has no reviewed pattern event')
        if r.get('direction')!=event_idx[k]['direction'] or r.get('split')!=event_idx[k]['split']:
            fail('Zone identity differs from pattern evidence')
        if r.get('reference_frame') != {'1h':'4h','4h':'1d','1d':'1d'}[k[1]]:
            fail('Unexpected price-zone reference timeframe')
        zidx[k]=r
    for r in regimes:
        k=_key(r)
        if k in ridx:fail('Duplicate market-state event identity')
        if k not in event_idx:fail('Market state has no reviewed pattern event')
        if r.get('direction')!=event_idx[k]['direction'] or r.get('split')!=event_idx[k]['split']:
            fail('Market state identity differs from pattern evidence')
        onset=_int(r,'pattern_start_open_ms')
        if onset>k[3]:
            fail('Pattern onset occurs after signal')
        if not r.get('regime_data_end_ms'):
            if (r.get('trend')!='insufficient_prior_ma' or
                    r.get('volatility')!='insufficient_prior_atr_distribution' or
                    r.get('reference_closed_at_utc')):
                fail('Missing market-state timestamp without explicit no-history state')
        else:
            context_end=_int(r,'regime_data_end_ms')
            if context_end>onset or _millis(r['reference_closed_at_utc'])>onset:
                fail('Market-state context uses knowledge at/after pattern onset')
        ridx[k]=r
    if set(event_idx)!=set(zidx) or set(event_idx)!=set(ridx):
        fail('Unjoined pattern events / orphaned contextual classifications')
    return event_idx,zidx,ridx,evaluated,statuses


def overlap_flags(intervals: list[tuple[int,int]])->tuple[list[bool],int]:
    """Closed [start,end) windows; return each window overlapping ANY other.

    A possible overlapping window can start before this window (prefix max end)
    or after this window (immediately next start in sorted order).
    """
    if any(e<=s for s,e in intervals):fail('Invalid interval ordering')
    order=sorted(range(len(intervals)),key=lambda i:(intervals[i][0], intervals[i][1]))
    flags=[False]*len(intervals)
    max_prev=-1
    for pos,i in enumerate(order):
        start,end=intervals[i]
        prior=start<max_prev
        later=pos+1<len(order) and intervals[order[pos+1]][0]<end
        flags[i]=prior or later
        max_prev=max(max_prev,end)
    return flags, sum(flags)


def peak_concurrent(intervals:list[tuple[int,int]])->int:
    """Simultaneous hypothetical hold windows, NOT capital-allocated positions."""
    points=[]
    for s,e in intervals:
        if e<=s:fail('Invalid interval')
        points.append((s,1));points.append((e,-1))
    points.sort(key=lambda x:(x[0],x[1])) # exits before new entries at identical time
    active=best=0
    for _,d in points:
        active+=d
        if active<0:fail('Broken interval event sweep')
        best=max(best,active)
    if active!=0:fail('Orphaned interval')
    return best


def _iso_week(ms:int)->str:
    d=datetime.fromtimestamp(ms/1000,tz=timezone.utc)
    iso=d.isocalendar()
    return f'{iso.year}-W{iso.week:02d}'


def _csv(path:Path,fields:list[str],rows:Iterable[dict[str,Any]])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields)
        w.writeheader()
        for r in rows:w.writerow(r)


def report(rows:list[tuple],event_idx:dict,zidx:dict,ridx:dict,statuses:Counter):
    by_group=defaultdict(list)
    horizon_all=defaultdict(list)
    metrics=defaultdict(lambda:{'returns':[],'favorable':0,'positive':0,'weeks':defaultdict(list)})
    per_year=defaultdict(list)
    state=defaultdict(lambda:[0,0.0])
    for key,h,start,end,ret,directional,split in rows:
        sym,frame,pattern,_=key
        by_group[(sym,frame,h)].append((start,end))
        horizon_all[h].append((start,end))
        m=metrics[(split,h)]
        m['returns'].append(ret)
        m['positive']+=int(ret>0)
        m['favorable']+=int(directional>0)
        m['weeks'][_iso_week(start)].append(ret)
        year=datetime.fromtimestamp(start/1000,timezone.utc).year
        per_year[(split,year,h)].append(ret)
        for feature,value in (('zone',zidx[key]['location']),('ma200',ridx[key]['trend']),('atr',ridx[key]['volatility'])):
            st=state[(feature,value,split,h)]
            st[0]+=1;st[1]+=ret
    overlap=[];total_overlap=0
    for (symbol,frame,h),v in sorted(by_group.items()):
        _,n=overlap_flags(v)
        total_overlap+=n
        overlap.append({'symbol':symbol,'timeframe':frame,'horizon_hours':h,'evaluated':len(v),
                        'overlapping_evaluated':n,'overlap_percent':round(n/len(v)*100,5) if v else None,
                        'peak_hypothetical_concurrent_windows':peak_concurrent(v)})
    cohorts=[]
    for (split,h),v in sorted(metrics.items()):
        count=len(v['returns'])
        wmeans=[mean(x) for x in v['weeks'].values()]
        cohorts.append({'split':split,'horizon_hours':h,'evaluated':count,
                        'favorable_direction_percent':round(100*v['favorable']/count,6),
                        'net_positive_paper_percent':round(100*v['positive']/count,6),
                        'mean_net_paper_pct':round(mean(v['returns']),8),
                        'observed_utc_iso_weeks':len(wmeans),
                        'positive_mean_weeks':sum(x>0 for x in wmeans),
                        'positive_mean_weeks_percent':round(100*sum(x>0 for x in wmeans)/len(wmeans),4),
                        'unit_of_analysis':'overlapping_paper_windows; never independent trades'})
    yearly=[{'split':split,'entry_year_utc':year,'horizon_hours':h,'evaluated':len(vals),
             'mean_net_paper_pct':round(mean(vals),8),
             'positive_paper_percent':round(100*sum(x>0 for x in vals)/len(vals),6)}
            for (split,year,h),vals in sorted(per_year.items())]
    slices=[{'feature':feature,'value':value,'split':split,'horizon_hours':h,
             'evaluated':n,'mean_net_paper_pct':round(s/n,8)}
            for (feature,value,split,h),(n,s) in sorted(state.items())]
    if total_overlap>len(rows):fail('Overlap flags out of bounds')
    total_24=sum(r['evaluated'] for r in overlap if r['horizon_hours']==24)
    total_72=sum(r['evaluated'] for r in overlap if r['horizon_hours']==72)
    total_168=sum(r['evaluated'] for r in overlap if r['horizon_hours']==168)
    if total_24+total_72+total_168!=len(rows):fail('Overlap cohorts fail reconciliation')
    aggregate={
        'version':VERSION,'status':'strict_evidence_join_and_exposure_audit_passed',
        'source_review_run':P1_RUN,'source_propagation_run':INTEGRATION_RUN,
        'source_sha256':dict(EXPECTED_SHA),
        'allowed_decision_frames':['1h','4h','1d'],
        'unique_events':len(event_idx),'observation_rows':319299,
        'evaluated':len(rows), 'right_censored':statuses['right_censored_or_split_boundary'],
        'unavailable_fixed_time_fill':statuses['unavailable_fixed_time_fill'],
        'matched_zone_classifications':len(zidx),'matched_ma_atr_classifications':len(ridx),
        'same_symbol_frame_horizon_any_overlap':total_overlap,
        'same_symbol_frame_horizon_overlap_pct':round(total_overlap/len(rows)*100,5),
        'total_evaluated_by_horizon':{'24':total_24,'72':total_72,'168':total_168},
        'peak_global_hypothetical_windows_by_horizon':{str(h):peak_concurrent(v) for h,v in sorted(horizon_all.items())},
        'exposure_warning':'Multiple paper windows frequently overlap; peak concurrency is NOT an executable portfolio size or capital usage.',
        'inference_warning':'Opened exploratory Test, post-review evidence. Weekly sign stability is descriptive, not p-value, CI, OOS, or validated trading edge.',
        'positive_edge_claim':False,'real_trades':0,'strategy_changed':False,
    }
    return aggregate,overlap,cohorts,yearly,slices


def run(p1_path:Path,integration_path:Path,out_dir:Path)->dict[str,Any]:
    with ZipFile(p1_path) as a,ZipFile(integration_path) as b:
        p1=checked_members(a,P1_PATH)
        mods=checked_members(b,INT_PATH)
    manifest=json.loads(p1['p1_manifest'])
    propagation=json.loads(mods['propagation'])
    if manifest.get('unique_pattern_events')!=106433 or manifest.get('observed_pattern_horizon_rows')!=319299:
        fail('Incorrect Codex-reviewed P1-01 manifest sample')
    if manifest.get('status')!='exploratory_frozen_history_only' or manifest.get('lock_sha256')!='42c0d049a8f277934b2908767869e1c6830009c47f346463140c9abac7549dbf':
        fail('Incorrect frozen-history source or research status')
    if manifest.get('fixed_horizons_hours')!=list(HORIZONS):fail('Changed horizon assumptions')
    if propagation.get('status')!='review_propagation_verified' or propagation.get('original_events')!=106155 or propagation.get('reviewed_events')!=106433 or propagation.get('old_zone_classification_drift')!=0 or propagation.get('old_regime_classification_drift')!=0:
        fail('Source integration verification incomplete')
    if propagation['input_sha256']['reviewed_events']!=EXPECTED_SHA['p1_event'] or propagation['input_sha256']['reviewed_manifest']!=EXPECTED_SHA['p1_manifest'] or propagation['input_sha256']['reviewed_zone']!=EXPECTED_SHA['zone'] or propagation['input_sha256']['reviewed_regime']!=EXPECTED_SHA['regime']:
        fail('Original source integrity claims differ')
    event_idx,zidx,ridx,evaluated,statuses=index_and_check(csv_rows(p1['p1_event']),csv_rows(mods['zone']),csv_rows(mods['regime']))
    agg,overlap,cohorts,yearly,slices=report(evaluated,event_idx,zidx,ridx,statuses)
    out_dir.mkdir(parents=True,exist_ok=True)
    (out_dir/'p1_quality_manifest.json').write_text(json.dumps(agg,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    _csv(out_dir/'p1_quality_overlap_by_group.csv',list(overlap[0]),overlap)
    _csv(out_dir/'p1_quality_by_split_horizon.csv',list(cohorts[0]),cohorts)
    _csv(out_dir/'p1_quality_by_year_horizon.csv',list(yearly[0]),yearly)
    _csv(out_dir/'p1_quality_context_slices.csv',list(slices[0]),slices)
    rd=['# P1-QUALITY-01｜研究證據與重疊曝險稽核','',
        '**完成的是資料與統計口徑驗收，不是提高勝率的策略。**','',
        f'- 可核對型態：{len(event_idx):,} 個；固定期限觀察：319,299 列',
        f'- 可評估紙上窗口：{len(evaluated):,} 列；截尾：{statuses["right_censored_or_split_boundary"]:,}；零量模擬成交不可評估：{statuses["unavailable_fixed_time_fill"]} 列',
        f'- 相同幣種 × 週期 × 觀察期限內，與其他紙上持有窗口重疊：{agg["same_symbol_frame_horizon_any_overlap"]:,}／{len(evaluated):,}（{agg["same_symbol_frame_horizon_overlap_pct"]:.3f}%）',
        '- 任意兩個重疊窗口**不可視為獨立交易樣本**；跨幣種與跨週期亦可能相關。','',
        '## 固定 24／72／168 小時、分時段的原始描述（不是新 OOS）','',
        '| 資料時段 | 期限 | 可評估窗口 | 淨正紙上報酬比 | 平均淨紙上報酬 | 有資料週數 | 週均報酬為正的週數 |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for x in cohorts:
        rd.append(f'| {x["split"]} | {x["horizon_hours"]}h | {x["evaluated"]:,} | {x["net_positive_paper_percent"]:.3f}% | {x["mean_net_paper_pct"]:.5f}% | {x["observed_utc_iso_weeks"]} | {x["positive_mean_weeks"]} |')
    rd+=['','## 方法限制與使用方式','','- 所有價格型態來自原已鎖定的已收盤 1H／4H／1D；本模組不建立或調整交易訊號。',
         '- 支撐壓力與 MA/ATR 是接入原先已驗證的分類；此稽核檢查身份、一致性和 MA/ATR 的 as-of，但不重新運算 pivot 或均線。',
         '- 使用 UTC 日曆週計算週平均的符號，僅為描述市場週期差異；週與週可能仍相關，不能視為獨立顯著性檢定。',
         '- 觀察資料中的 Spot 空頭為理論空頭，下一根開盤成交為紙上假設，無借幣／資金費率模型。',
         '- 開啟過的 2025+ Test 不能事後再當新留出樣本。不要拿任何分類中較好的歷史數字修改正式策略。',
         '- 後續 P1-04 只可將這套不可改寫的品質關卡延伸到 RVOL，不應隱性優化群組切分。','']
    (out_dir/'P1_QUALITY_01_REPORT_ZH.md').write_text('\n'.join(rd),encoding='utf-8')
    print(json.dumps({'status':agg['status'],'events':agg['unique_events'],'evaluated':agg['evaluated'],'overlapping':agg['same_symbol_frame_horizon_any_overlap'],'overlap_pct':agg['same_symbol_frame_horizon_overlap_pct']},ensure_ascii=False))
    return agg


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(description='P1-QUALITY-01 strict research-only join / paper overlap audit')
    p.add_argument('--p1-reviewed',type=Path,required=True)
    p.add_argument('--p1-integrated',type=Path,required=True)
    p.add_argument('--out-dir',type=Path,required=True)
    args=p.parse_args(argv)
    run(args.p1_reviewed,args.p1_integrated,args.out_dir)
    return 0


if __name__=='__main__':
    raise SystemExit(main())