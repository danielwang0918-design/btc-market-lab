#!/usr/bin/env python3
"""Verify the Codex P1-01 correction propagates without changing old P1-02/03 classifications.

This is an immutable-historical-evidence audit. It does not calculate trading signals.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

VERSION = 'p1-review-propagation-v1'
OLD_ZONE_SHA256 = '90d080897f437d66d9125f1b75adc43644355e4f7fef0b0d045ba7b8e5d2e29a'
OLD_REGIME_SHA256 = '6669dfb793f77f57c39051ebcb53549d20ecb1e1d170d31dc0bdd4c91fbd1161'
NEW_P1_EVENTS_SHA256 = '71b7edf1ca06ba1a4b50cf74c3189b29ccdab344983ee6b2dc37ca5b33937da7'
NEW_P1_MANIFEST_SHA256 = '5baa4b6e63eb91d4591811954a3980eff343994e5fb86425c042cce5f79f88c1'
K = ('symbol','timeframe','pattern','signal_open_ms')
ZONE_FIELDS = ('location','pivots_near','nearest_distance_atr','zone_reference','preexisting_pivots_in_window')
REGIME_FIELDS = ('trend','volatility','ma200_prepattern','ma200_slope_20_atr',
                 'prior_close_vs_ma200_atr','prior_atr14_pct','prior_atr14_percentile','regime_data_end_ms')


def digest(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def indexed(path:Path)->dict[tuple[str,...],dict[str,str]]:
    out={}
    with path.open(encoding='utf-8-sig',newline='') as f:
        r=csv.DictReader(f)
        if not set(K).issubset(r.fieldnames or []):raise ValueError('Missing identity fields')
        for row in r:
            key=tuple(row[x] for x in K)
            if not all(key) or key in out:raise ValueError('Missing or duplicated event key')
            out[key]=row
    return out


def compare(old:Path,new:Path,*,expected_hash:str,fields:tuple[str,...]) -> tuple[dict[str,int],list[dict[str,str]]]:
    if digest(old)!=expected_hash:raise ValueError('Original audited classification CSV has changed')
    before,after=indexed(old),indexed(new)
    if not all(set(fields).issubset(r) for r in (next(iter(before.values())),next(iter(after.values())))):
        raise ValueError('Classification schema changed')
    if set(before)-set(after):raise ValueError('Original pattern events disappeared')
    if any(any(before[k][col]!=after[k][col] for col in fields) for k in before):
        raise ValueError('Previous descriptive classifications drifted after P1 correction')
    added=[after[k] for k in sorted(set(after)-set(before))]
    return {'original_events':len(before),'revised_events':len(after),'added_events':len(added),
            'changed_old_classifications':0},added


def audit(old_zone:Path,new_zone:Path,old_regime:Path,new_regime:Path,
          p1_events:Path,p1_manifest:Path,outdir:Path)->dict:
    if digest(p1_events)!=NEW_P1_EVENTS_SHA256 or digest(p1_manifest)!=NEW_P1_MANIFEST_SHA256:
        raise ValueError('Codex-reviewed P1-01 source bytes were replaced')
    with p1_manifest.open(encoding='utf-8') as f:m=json.load(f)
    if (m.get('unique_pattern_events')!=106433 or m.get('observed_pattern_horizon_rows')!=319299 or
            m.get('version')!='p1-candlestick-geometry-v1.1'):
        raise ValueError('P1-01 reviewed manifest status/count inconsistent')
    zone,zadds=compare(old_zone,new_zone,expected_hash=OLD_ZONE_SHA256,fields=ZONE_FIELDS)
    regime,radds=compare(old_regime,new_regime,expected_hash=OLD_REGIME_SHA256,fields=REGIME_FIELDS)
    keys1={tuple(x[k] for k in K) for x in zadds}
    keys2={tuple(x[k] for k in K) for x in radds}
    if (keys1!=keys2 or len(keys1)!=278 or
            zone['original_events']!=106155 or regime['original_events']!=106155 or
            zone['revised_events']!=106433 or regime['revised_events']!=106433):
        raise ValueError('Event addition/revision mismatch across P1-02 and P1-03')
    def check_manifest(dir:Path, name:str):
        payload=json.loads((dir/name).read_text(encoding='utf-8'))
        if (payload.get('p1_events_sha256')!=NEW_P1_EVENTS_SHA256 or
            payload.get('p1_manifest_sha256')!=NEW_P1_MANIFEST_SHA256 or
            payload.get('unique_events')!=106433 or payload.get('observations')!=319299 or
            payload.get('unavailable_fixed_time_fills')!=24):
            raise ValueError('Downstream manifest source or unavailable-fill accounting mismatch')
    check_manifest(new_zone.parent,'p1_02_manifest.json')
    check_manifest(new_regime.parent,'p1_03_manifest.json')
    # Check all report strata account for 319299 observations, not just row-count.
    for name,src in [('p1_02_zone_summary.csv',new_zone.parent),('p1_03_regime_summary.csv',new_regime.parent)]:
        with (src/name).open(encoding='utf-8-sig',newline='') as f:rr=list(csv.DictReader(f))
        totals=Counter({k:sum(int(r[k]) for r in rr) for k in ('events','evaluated','censored','unavailable')})
        if (totals['events']!=319299 or totals['evaluated']!=311873 or
                totals['censored']!=7402 or totals['unavailable']!=24 or
                totals['events']!=totals['evaluated']+totals['censored']+totals['unavailable']):
            raise ValueError(name+' counts do not match reviewed fill evidence')
    added_keys_by_sym=Counter(row['symbol'] for row in zadds)
    zclasses=Counter(row['location'] for row in zadds)
    trend=Counter(row['trend'] for row in radds)
    vol=Counter(row['volatility'] for row in radds)
    result={'version':VERSION,'status':'review_propagation_verified','source_review_run':43857787070,
            'predecessor_p1_02_run':43856047519,'predecessor_p1_03_run':43859350879,
            'decision_timeframes':['1h','4h','1d'],'original_events':106155,
            'reviewed_events':106433,'reviewed_observation_rows':319299,
            'reviewed_evaluated_rows':311873,'reviewed_censored_rows':7402,
            'reviewed_unavailable_fixed_time_fill_rows':24,
            'old_zone_classification_drift':0,'old_regime_classification_drift':0,
            'new_events_by_symbol':dict(sorted(added_keys_by_sym.items())),
            'new_zone_classes':dict(sorted(zclasses.items())),
            'new_trend_classes':dict(sorted(trend.items())),
            'new_atr_classes':dict(sorted(vol.items())),
            'input_sha256':{'reviewed_events':NEW_P1_EVENTS_SHA256,'reviewed_manifest':NEW_P1_MANIFEST_SHA256,
                'old_zone':OLD_ZONE_SHA256,'old_regime':OLD_REGIME_SHA256,
                'reviewed_zone':digest(new_zone),'reviewed_regime':digest(new_regime)},
            'conclusion':'Descriptive categorization unchanged for 106155 old signals; 278 formerly missed events added without retuning. No profit or OOS claim.'}
    outdir.mkdir(parents=True,exist_ok=True)
    (outdir/'P1_REVIEW_PROPAGATION_RESULT.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    for name,rows in [('new_zone_cases.csv',zadds),('new_regime_cases.csv',radds)]:
        with (outdir/name).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]),extrasaction='raise');w.writeheader();w.writerows(rows)
    lines=['# P1-01 Codex 修正對 P1-02 / P1-03 的完整傳播驗收','',
           '**結果：106,155 筆既有事件的位置與 MA/ATR 分類全數保持不變，新增 278 筆。**','',
           '| 指標 | 修正後 |','|---|---:|',
           '| 型態事件 | 106,433 |','| 24/72/168h 觀察列 | 319,299 |',
           '| 可評估紙上結果 | 311,873 |','| 缺口／邊界／追蹤期不足 | 7,402 |',
           '| 零量成交不足，不計入輸贏 | 24 |',
           '| P1-02 原有分類變化 | 0 |','| P1-03 原有分類變化 | 0 |','','## 新增型態的分類','',
           f'- 已確認支撐壓力區新增 {zclasses["near_confirmed_zone"]} 筆。',
           f'- MA200 多頭新增 {trend["bullish"]} 筆，空頭新增 {trend["bearish"]} 筆。',
           '- 不代表任何具備正期望值或新增獨立 Test，僅是既有資料經品質修正後的分類。',
           '- P1-02/03 原有 v1 研究檔案保留，不得默默覆蓋；後續需要重新註冊假設並使用獨立未來資料。','']
    (outdir/'P1_REVIEW_PROPAGATION_RESULT.md').write_text('\n'.join(lines),encoding='utf-8')
    return result


def main()->int:
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('old-zone','new-zone','old-regime','new-regime','p1-events','p1-manifest','outdir'):
        p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args()
    result=audit(a.old_zone,a.new_zone,a.old_regime,a.new_regime,a.p1_events,a.p1_manifest,a.outdir)
    print(json.dumps({'status':result['status'],'old':result['original_events'],'new':result['reviewed_events'],'old_classes_changed':0,'unavailable':24},ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())