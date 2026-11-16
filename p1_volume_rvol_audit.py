#!/usr/bin/env python3
"""Independently audit P1-04 RVOL arithmetic, immutable joins and every CSV cell.

Uses direct Fraction windows instead of the study's rolling Decimal sum, and
independent evidence counters instead of the study's Stats class.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import re
from collections import Counter, defaultdict
from decimal import Decimal,localcontext
from fractions import Fraction
from pathlib import Path
from statistics import median
from zipfile import ZipFile

import p1_candlestick_research as core
import p1_volume_rvol as study


def check_legacy_row(original,enriched):
    if any(original[k]!=enriched.get(k) for k in core.OUTPUT_COLUMNS):
        raise ValueError('P1-01 original field changed in volume observations')


def check_feature(row,bars,i,frame,precision):
    step=core.FRAMES[frame];current=bars[i]
    block=bars[max(0,i-20):i+1]
    breaks=[j for j in range(1,len(block)) if block[j].open_ms-block[j-1].open_ms!=step]
    n=len(block)-1-(breaks[-1] if breaks else 0)
    expected=dict(current_volume=current.v,prior_mean_volume=None,rvol=None,
        rvol_bin=None,rvol_status=None,extreme_rvol=False,prior_bars_used=n,
        prior_window_start_ms=block[0].open_ms if n==20 else None,
        prior_window_end_exclusive_ms=current.open_ms if n else None,
        available_at_ms=current.open_ms+step)
    if n<20:
        status='gap_in_lookback' if breaks else 'insufficient_history'
        expected.update(rvol_bin=status,rvol_status=status)
    else:
        total=sum((Fraction(b.v) for b in block[:-1]),Fraction(0))
        with localcontext() as ctx:
            ctx.prec=precision
            mean=total/20
            expected['prior_mean_volume']=Decimal(mean.numerator)/Decimal(mean.denominator)
            if not total:
                expected.update(rvol_bin='zero_prior_mean',rvol_status='zero_prior_mean')
            else:
                ratio=20*Fraction(current.v)/total
                expected.update(rvol=Decimal(ratio.numerator)/Decimal(ratio.denominator),
                    rvol_bin='low' if ratio<Fraction(4,5) else 'normal' if ratio<Fraction(6,5) else 'high',
                    rvol_status='available',extreme_rvol=ratio>=10)
    numeric=('current_volume','prior_mean_volume','rvol','prior_bars_used',
             'prior_window_start_ms','prior_window_end_exclusive_ms','available_at_ms')
    for key,value in expected.items():
        cell=row[key]
        if key in numeric:
            good=cell in ('',None) if value is None else cell not in ('',None) and Decimal(str(cell))==value
        else:
            good=str(cell)==str(value)
        if not good:raise ValueError(f'Independent RVOL feature differs: {key}')


class EvidenceStats:
    """Independent raw-outcome accounting; no volume-study Stats calls."""
    def __init__(self):
        self.count=0;self.censor=0;self.no_fill=0;self.direction_count=0;self.values=[]

    def add(self,row):
        self.count+=1
        if row['outcome_status']=='evaluated':
            net=Decimal(row['paper_net_pct']);direction=Decimal(row['directional_favorable_pct'])
            if not net.is_finite() or not direction.is_finite():raise ValueError('Invalid evidence return')
            self.values.append(net);self.direction_count+=direction>0
        else:
            if row['paper_net_pct'] or row['directional_favorable_pct']:
                raise ValueError('Non-evaluated evidence has a return')
            if row['outcome_status']=='right_censored_or_split_boundary':self.censor+=1
            elif row['outcome_status']=='unavailable_fixed_time_fill':self.no_fill+=1
            else:raise ValueError('Unknown evidence status')

    def result(self):
        n=len(self.values);wins=[v for v in self.values if v>0];losses=[-v for v in self.values if v<0]
        pf=round(float(sum(wins)/sum(losses)),8) if losses else None
        pf_status=('defined' if losses else 'no_evaluated_outcomes' if not n
                   else 'no_negative_returns' if wins else 'all_zero_returns')
        return dict(observed_events=self.count,evaluated=n,right_censored=self.censor,
            paper_fill_unavailable=self.no_fill,
            unevaluated_pct=round(100*(self.count-n)/self.count,6) if self.count else None,
            directional_positive_pct=round(100*self.direction_count/n,6) if n else None,
            paper_net_positive_pct=round(100*len(wins)/n,6) if n else None,
            mean_paper_net_pct=round(float(sum(self.values)/n),8) if n else None,
            median_paper_net_pct=round(float(median(self.values)),8) if n else None,
            paper_event_profit_factor=pf,paper_event_pf_status=pf_status)


def check_summary(path,grid,acc):
    fields=tuple(grid);empty=EvidenceStats().result()
    totals=Counter();rows=0
    with path.open(encoding='utf-8-sig',newline='') as f:
        for actual,key in itertools.zip_longest(csv.DictReader(f),itertools.product(*grid.values())):
            if actual is None or key is None or tuple(str(actual[k]) for k in fields)!=tuple(map(str,key)):
                raise ValueError('Missing, repeated or incorrectly ordered full-grid group')
            expected=acc[key].result() if key in acc else empty
            for name,value in expected.items():
                cell=actual[name]
                good=(cell=='' if value is None else cell==value if isinstance(value,str)
                      else cell!='' and Decimal(cell)==Decimal(str(value)))
                if not good:raise ValueError(f'Cross-tab metric differs from raw observations: {name}')
            totals.update({k:int(actual[k]) for k in ('observed_events','evaluated','right_censored','paper_fill_unavailable')})
            rows+=1
    return dict(rows=rows,totals=dict(totals))


def indexed(path):
    result={}
    with path.open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            key=study.event_key(row)
            if key in result:raise ValueError('Duplicate feature/context event')
            result[key]=row
    return result


def audit(frozen_path,p1_dir,context_dir,lock_path,out_dir,tests_log=None):
    lock=core.read_lock(lock_path)
    study.check_known_files(p1_dir,study.P1_PINS);study.check_known_files(context_dir,study.CONTEXT_PINS)
    manifest=json.loads((out_dir/'p1_04_manifest.json').read_text())
    if manifest['protocol_sha256']!=study.PROTOCOL_SHA256:
        raise ValueError('Changed pre-result volume protocol')
    for name,expected in manifest['output_sha256'].items():
        if study.digest(out_dir/name)!=expected:raise ValueError('Study output SHA256 mismatch')
    for name,expected in manifest['code_sha256'].items():
        if study.digest(Path(__file__).with_name(name))!=expected:raise ValueError('Study source changed after run')
    features=indexed(out_dir/'p1_04_features.csv')
    zmap=indexed(context_dir/'zone/p1_02_zone_evidence.csv');gmap=indexed(context_dir/'regime/p1_03_regime_evidence.csv')
    if set(features)!=set(zmap) or set(features)!=set(gmap) or len(features)!=106433:
        raise ValueError('Missing/extra joined feature event')
    grouped=defaultdict(list)
    for key in features:grouped[key[:2]].append(key)
    integrity=json.loads((out_dir/'p1_04_source_integrity.json').read_text())
    # Independent direct-window arithmetic and complete source-data diagnostics.
    with ZipFile(frozen_path) as z:
        if len(z.namelist())!=len(set(z.namelist())):raise ValueError('Duplicate frozen source file')
        for name,meta in lock['frozen_data_prefixes'].items():
            raw=z.read(name)
            if core.sha(raw)!=meta['sha256'] or len(raw)!=meta['bytes']:raise ValueError('Original source bytes changed')
            sym=meta['symbol'];frame=meta['interval'];step=core.FRAMES[frame]
            bars=core.parse_bars(raw,sym,frame,meta['frozen_bars'],closed_through_ms=meta['frozen_last_close_exclusive_ms'])
            ix={b.open_ms:i for i,b in enumerate(bars)}
            zeros=[b.open_ms for b in bars if b.v==0]
            gaps=[dict(after_open_ms=a.open_ms,next_open_ms=b.open_ms,missing_bars=(b.open_ms-a.open_ms)//step-1)
                  for a,b in zip(bars,bars[1:]) if b.open_ms-a.open_ms>step]
            d=integrity[name]
            if (d['bars']!=len(bars) or d['zero_volume_open_ms']!=zeros or d['gaps']!=gaps
                    or d['last_close_exclusive_ms']!=meta['frozen_last_close_exclusive_ms']
                    or d['sha256']!=meta['sha256'] or d['bytes']!=len(raw) or d['duplicate_bars']!=0
                    or d['first_open_ms']!=bars[0].open_ms or d['zero_volume_bars']!=len(zeros)
                    or d['gap_segments']!=len(gaps) or d['missing_bars']!=sum(x['missing_bars'] for x in gaps)):
                raise ValueError('Source integrity diagnostics differ from original bars')
            for key in grouped[(sym,frame)]:
                check_feature(features[key],bars,ix[key[3]],frame,d['ratio_precision_digits'])
                for field in study.CONTEXT_FIELDS:
                    expected=zmap[key].get(field,gmap[key].get(field,''))
                    if features[key][field]!=expected:raise ValueError('P1-02/03 classification field changed')
                if any(features[key][f]!=zmap[key][f] or features[key][f]!=gmap[key][f]
                       for f in ('direction','split')):
                    raise ValueError('Context metadata changed during join')
    acc={view:defaultdict(EvidenceStats) for view in study.VIEWS}
    pooled=defaultdict(EvidenceStats);all_rows=EvidenceStats();counts=Counter();seen=set()
    with (p1_dir/'p1_01_events.csv').open(encoding='utf-8-sig',newline='') as source, \
            (out_dir/'p1_04_observations.csv').open(encoding='utf-8-sig',newline='') as enriched:
        for original,row in itertools.zip_longest(csv.DictReader(source),csv.DictReader(enriched)):
            if original is None or row is None:raise ValueError('Observation rows added or missing')
            check_legacy_row(original,row)
            key=study.event_key(original);h=int(original['horizon_hours']);full_key=(*key,h)
            if full_key in seen:raise ValueError('Duplicate event/horizon in enriched results')
            seen.add(full_key)
            for f in (*study.FEATURE_FIELDS,*study.CONTEXT_FIELDS):
                if row[f]!=features[key][f]:raise ValueError('RVOL/context changed between horizons')
            base=tuple(h if f=='horizon_hours' else original[f] for f in study.BASE_DIMS)
            for view,dims in study.VIEWS.items():
                acc[view][base+tuple(row[d] for d in dims)].add(original)
            for split in (original['split'],'all'):pooled[(split,h,row['rvol_bin'])].add(original)
            all_rows.add(original);counts[original['outcome_status']]+=1
    if (all_rows.count,len(all_rows.values),all_rows.censor,all_rows.no_fill)!=(319299,311873,7402,24):
        raise ValueError('Inherited event/outcome accounting changed')
    bin_counts=Counter(r['rvol_bin'] for r in features.values())
    if manifest['unique_event_rvol_bin_counts']!={b:bin_counts[b] for b in study.RVOL_BINS}:
        raise ValueError('Manifest must count every RVOL category, including empty categories')
    checked={}
    expected_totals=dict(observed_events=319299,evaluated=311873,right_censored=7402,paper_fill_unavailable=24)
    for view,dims in study.VIEWS.items():
        checked[view]=check_summary(out_dir/f'p1_04_{view}.csv',study.grid_for(lock['symbols'],dims),acc[view])
        if checked[view]['totals']!=expected_totals:raise ValueError('Cross-tab totals differ from baseline')
    pool_grid=dict(split=(*study.SPLITS,'all'),horizon_hours=core.HORIZONS_HOURS,rvol_bin=study.RVOL_BINS)
    check_summary(out_dir/'p1_04_rvol_by_split_horizon.csv',pool_grid,pooled)
    # Check matched comparisons and coin-stability counts independently as well.
    baseline=defaultdict(EvidenceStats)
    for key,stats in acc['pattern_rvol'].items():
        b=baseline[key[:-1]];b.count+=stats.count;b.censor+=stats.censor;b.no_fill+=stats.no_fill
        b.direction_count+=stats.direction_count;b.values.extend(stats.values)
    with (out_dir/'p1_04_matched_rvol_comparison.csv').open(encoding='utf-8-sig',newline='') as f:
        expected_keys=itertools.product(*study.grid_for(lock['symbols'],('rvol_bin',)).values())
        for r,key in itertools.zip_longest(csv.DictReader(f),expected_keys):
            if r is None or key is None:raise ValueError('Incomplete matched comparison')
            if tuple(r[k] for k in (*study.BASE_DIMS,'rvol_bin'))!=tuple(map(str,key)):
                raise ValueError('Matched comparison group identity changed')
            base=baseline[key[:-1]].result();normal=acc['pattern_rvol'][(*key[:-1],'normal')].result()
            current=acc['pattern_rvol'][key].result()
            for prefix,metrics in (('baseline_',base),('normal_',normal),('',current)):
                for field in ('evaluated','mean_paper_net_pct'):
                    value=metrics[field];cell=r[prefix+field]
                    if (cell!='' if value is None else cell=='' or Decimal(cell)!=Decimal(str(value))):
                        raise ValueError('Matched comparison differs from source outcomes')
            for column,other in (('mean_difference_vs_all_rvol_pp',base),('mean_difference_vs_normal_pp',normal)):
                value=round(current['mean_paper_net_pct']-other['mean_paper_net_pct'],8) \
                    if current['evaluated'] and other['evaluated'] else None
                if (r[column]!='' if value is None else r[column]=='' or Decimal(r[column])!=Decimal(str(value))):
                    raise ValueError('Matched mean difference changed')
    stable=defaultdict(list)
    for key,stats in acc['pattern_rvol'].items():
        if stats.values:stable[key[1:]].append(sum(stats.values)/len(stats.values))
    with (out_dir/'p1_04_symbol_stability.csv').open(encoding='utf-8-sig',newline='') as f:
        sg={k:v for k,v in study.grid_for(lock['symbols'],('rvol_bin',)).items() if k!='symbol'}
        for r,key in itertools.zip_longest(csv.DictReader(f),itertools.product(*sg.values())):
            if r is None or key is None:raise ValueError('Incomplete cross-symbol stability')
            if tuple(r[k] for k in sg)!=tuple(map(str,key)):raise ValueError('Symbol stability group changed')
            means=stable.get(key,[])
            expected=(len(means),sum(x>0 for x in means),sum(x<0 for x in means))
            if tuple(int(r[k]) for k in ('symbols_with_evaluated_events','symbols_positive_mean','symbols_negative_mean'))!=expected:
                raise ValueError('Symbol stability counts differ from unrounded event means')
            for label,fn in (('median',median),('minimum',min),('maximum',max)):
                value=round(float(fn(means)),8) if means else None
                cell=r[label+'_symbol_mean_paper_net_pct']
                if (cell!='' if value is None else cell=='' or Decimal(cell)!=Decimal(str(value))):
                    raise ValueError('Symbol stability mean changed')
    tests=None
    if tests_log:
        log=tests_log.read_text();matches=re.findall(r'Ran (\d+) tests',log)
        if not matches or '\nOK\n' not in log:raise ValueError('No successful full Python test evidence')
        tests=int(matches[-1])
    result=dict(status='p1_04_independent_acceptance_passed',version=study.VERSION,
        audit_code_sha256=study.digest(Path(__file__)),study_manifest_sha256=study.digest(out_dir/'p1_04_manifest.json'),
        base_commit=study.BASE_COMMIT,protocol_sha256=study.PROTOCOL_SHA256,
        feature_events_checked=len(features),original_p1_fields_checked=len(seen)*len(core.OUTPUT_COLUMNS),
        observation_rows_checked=len(seen),original_p1_field_changes=0,context_field_changes=0,
        direct_fraction_rvol_reference=True,complete_cross_tabs=checked,
        source_files_checked=len(integrity),source_bars=sum(x['bars'] for x in integrity.values()),
        zero_volume_bars=sum(x['zero_volume_bars'] for x in integrity.values()),
        gap_segments=sum(x['gap_segments'] for x in integrity.values()),
        missing_source_bars=sum(x['missing_bars'] for x in integrity.values()),
        inherited_outcome_counts=dict(counts),python_tests_passed=tests,
        github_commit=os.getenv('GITHUB_SHA'),github_run=os.getenv('GITHUB_RUN_ID'))
    study.json_write(out_dir/'p1_04_acceptance.json',result)
    report_zh(out_dir,manifest,result,pooled)
    print(json.dumps(result,ensure_ascii=False))
    return result


def report_zh(out_dir,manifest,acceptance,pooled):
    def fmt(value,digits=6):
        return '—' if value is None else f'{value:.{digits}f}'
    lines=['# P1-04｜成交量／RVOL 與型態交叉研究驗收結果','',
        '**研究程式與完整性驗收通過；歷史描述結果，不代表正期望值策略或新 OOS。**','',
        f"版本 `{study.VERSION}`；基準 commit `{study.BASE_COMMIT}`；完整 Python 測試 {acceptance['python_tests_passed']} 項通過。",
        f"規格 SHA256：`{study.PROTOCOL_SHA256}`。首次量能績效執行前固定，沒有回看調整門檻。",'',
        '## 固定條件','',
        '- RVOL＝型態完成棒已收盤成交量／前 20 根均量，分母排除當期。低量 <0.8、正常 [0.8,1.2)、高量 ≥1.2；極端 ≥10 保留，不截尾。',
        '- 歷史不足、缺口暖機、零前期均量各列一組；三個期限使用同一事件特徵。',
        '- 原始 24／72／168 小時結果與每邊 10 bps 費用、5 bps 滑價不改，讀取 PR #11 已驗證的位置與 MA200／ATR 分類。','',
        '## 基準對帳與資料品質','',
        '| 指標 | 實測 |','|---|---:|',
        '| 型態事件／特徵列 | 106,433 |','| 固定期限觀察列 | 319,299 |',
        '| 可評估 | 311,873 |','| 缺口／邊界／追蹤不足 | 7,402 |','| 成交不足 | 24 |',
        '| P1-01 原始欄位變動 | 0 |','| P1-02／03 分類變動 | 0 |',
        f"| 來源檔／已收盤棒 | {acceptance['source_files_checked']}／{acceptance['source_bars']:,} |",
        f"| 零量來源棒 | {acceptance['zero_volume_bars']} |",
        f"| 缺口段／缺失來源棒 | {acceptance['gap_segments']}／{acceptance['missing_source_bars']} |",'',
        '上述缺口為跨檔案計數，不是獨立缺失小時；沒有補棒。24 列成交不足保持不可評估，沒有轉成輸、贏或零報酬。','',
        '## 唯一事件 RVOL 分組','', '| 分組 | 事件數 |','|---|---:|']
    for bucket in study.RVOL_BINS:lines.append(f"| {bucket} | {manifest['unique_event_rvol_bin_counts'].get(bucket,0):,} |")
    lines.extend(['',f"RVOL≥10 的事件共 {manifest['extreme_rvol_event_count']:,} 件，全部保留在 high。",'',
        '## 全時段、全幣種、全型態的描述結果','',
        '此處為依事件等權、跨幣／週期混合的描述值；不表示可同時投入等額資金。分組本身不是交易規則，也不是獨立交易。',
        '| 小時 | RVOL | 事件數 | 可評估 | 不可評估% | 方向有利% | 淨正報酬% | 平均淨報酬% | 中位數% | 事件 PF |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|'])
    for h in core.HORIZONS_HOURS:
        for bucket in study.RVOL_BINS:
            r=pooled[('all',h,bucket)].result()
            lines.append(f"| {h} | {bucket} | {r['observed_events']:,} | {r['evaluated']:,} | {fmt(r['unevaluated_pct'])} | {fmt(r['directional_positive_pct'])} | {fmt(r['paper_net_positive_pct'])} | {fmt(r['mean_paper_net_pct'])} | {fmt(r['median_paper_net_pct'])} | {fmt(r['paper_event_profit_factor'])} |")
    lines.extend(['','## 固定時段的 RVOL 結果','',
        '| 時段 | 小時 | RVOL | 可評估 | 平均淨報酬% | 事件 PF |','|---|---:|---|---:|---:|---:|'])
    for split in study.SPLITS:
        for h in core.HORIZONS_HOURS:
            for bucket in study.RVOL_BINS:
                r=pooled[(split,h,bucket)].result()
                lines.append(f"| {split} | {h} | {bucket} | {r['evaluated']:,} | {fmt(r['mean_paper_net_pct'])} | {fmt(r['paper_event_profit_factor'])} |")
    lines.extend(['','## 全部交叉報告與雜湊','',
        '| 視圖 | 完整列數 |','|---|---:|'])
    for view,checked in acceptance['complete_cross_tabs'].items():
        lines.append(f"| `p1_04_{view}.csv` | {checked['rows']:,} |")
    lines.extend(['','四個視圖共 693,360 組，包含零樣本，每個視圖各自對回 319,299 列；不能跨視圖相加當作更多事件。',
        '- `p1_04_matched_rvol_comparison.csv`：全部 6,480 組與同層全 RVOL／normal 基準的差異；不排名、不篩選。',
        '- `p1_04_symbol_stability.csv`：全部 810 組的八幣種樣本覆蓋與未四捨五入平均值的正負計數；原逐幣結果完整保留。',
        '- `p1_04_features.csv`、`p1_04_observations.csv`：完整特徵與保留原欄位的逐列觀察。',
        '- `p1_04_source_integrity.json`：逐檔 hash、截止、零量時間戳與全部缺口。',
        '- `p1_04_manifest.json`：全部輸入／程式／輸出 SHA256、版本、固定規格；`p1_04_acceptance.json`：獨立核對紀錄。','',
        '## 限制與驗收結論','',
        '平均淨紙上報酬只代表等權事件樣本均值。事件 PF 是正淨報酬%加總／負淨報酬%絕對加總，沒有資金約束、重疊部位或可執行做空模型，不能認作交易系統 PF；沒有負報酬時 PF 為 null 並附狀態。',
        '實際交易筆數、投資組合最大回撤及新 OOS 均未定義；沒有以任意排序或資金曲線冒充。需要另案事前定義部位、執行、風控與鎖定未來資料。',
        '非零成交量也不保證次根開盤與期末收盤可成交；Spot 空頭是假設，沒有借幣、資金費率或容量。相同事件三期限、跨幣／跨週期與相鄰窗口大量相關，不能當獨立交易計算顯著性。',
        '2025+ Test 已開啟，2026 選樣亦有回看偏差。不得用此結果反覆調整 RVOL、挑選最好組合或宣稱新策略。只有 OHLCV，無法精確重建 CVD、主動成交方向、訂單吸收或棒內路徑。',
        '**建議通過 P1-04 特徵、完整分層與可重現性研究驗收；不建議據此通過策略獲利／實盤驗收。**',''])
    (out_dir/'P1_04_RESULT_ZH.md').write_text('\n'.join(lines),encoding='utf-8')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('frozen-archive','p1-dir','context-dir','lock','out-dir'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--tests-log',type=Path)
    a=p.parse_args(argv)
    audit(a.frozen_archive,a.p1_dir,a.context_dir,a.lock,a.out_dir,a.tests_log)
    return 0


if __name__=='__main__':raise SystemExit(main())
