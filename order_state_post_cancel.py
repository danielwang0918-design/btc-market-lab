#!/usr/bin/env python3
"""P0-ORDER-STATE-06: frozen 1H OB cancellation follow-up (DESCRIPTIVE ONLY).

All 1H events are reconstructed from fully closed Binance Spot 1H candles with
2 left/2 right strict swing confirmation and close-break, reproducing the
original engine's 1H BOS timing. No trading, subhour prices or rule selection.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = 'p0-ob-post-cancel-followup-v1'
SYMBOLS = ('BTCUSDT','ETHUSDT','SOLUSDT')
TIMEFRAMES = ('1h','4h','1d')
HORIZONS = (24,72,168)  # predeclared calendar hours, never tuned on Test
HOUR = 3_600_000
EXPECTED_BOS = dict(zip(SYMBOLS,(23,36,35)))
EXPECTED_OTHER = dict(zip(SYMBOLS,(9,17,16)))
REASON = 'mtf_bos_direction_mismatch'


def sha(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1<<20), b''):
            h.update(block)
    return h.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def rows(path: Path) -> list[dict[str,str]]:
    with path.open(encoding='utf-8-sig',newline='') as f:
        r=csv.DictReader(f)
        if not r.fieldnames:
            raise ValueError('Missing CSV header')
        return list(r)


def save_json(path: Path,obj: Any)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    temp.replace(path)


def utc_ms(ts:str)->int:
    dt=datetime.fromisoformat(ts.replace('Z','+00:00'))
    if dt.tzinfo is None:raise ValueError('Naive timestamp')
    v=int(dt.astimezone(timezone.utc).timestamp()*1000)
    if v%HOUR:raise ValueError('Timestamp not aligned to 1H')
    return v


def utc(t:int)->str:
    return datetime.fromtimestamp(t/1000,timezone.utc).isoformat()


def price(x:Any)->float:
    try:v=float(x)
    except (ValueError,TypeError,OverflowError) as e:raise ValueError('Bad numeric price') from e
    if not math.isfinite(v) or v<=0:raise ValueError('Invalid/nonpositive price')
    return v


def med(v:list[float])->float|None:
    return round(statistics.median(v),6) if v else None


def clean_closed_hours(candles:list[dict[str,str]],symbol:str)->tuple[list[int],list[dict[str,float]]]:
    times=[]; data=[];prior=None
    for row in candles:
        if row.get('symbol')!=symbol or row.get('interval')!='1h' or row.get('is_closed','').lower()!='true':
            raise ValueError('Unexpected symbol/interval/unclosed 1H bar')
        t=int(row['open_time']);close_time=int(row['close_time'])
        if t%HOUR or close_time!=t+HOUR-1 or (prior is not None and t<=prior):
            raise ValueError('Malformed 1H candle timestamp')
        o,h,l,c=(price(row[k]) for k in ('open','high','low','close'))
        if not l<=min(o,c)<=max(o,c)<=h:raise ValueError('Bad 1H OHLC')
        times.append(t);data.append({'o':o,'h':h,'l':l,'c':c});prior=t
    if not times:raise ValueError('No closed hourly bars')
    return times,data


def confirmed_one_hour_bos(times:list[int],bars:list[dict[str,float]])->list[dict[str,Any]]:
    """Mirror formal _strategy_events 2L/2R swing + close BOS; no future signals.

    Swing at pivot i only exists at i+2 close; BOS requires index > confirmation
    index and close strictly crosses that swing. Contiguous segments reset state.
    """
    if len(times)!=len(bars):raise ValueError('Length mismatch')
    result=[];start=0
    for boundary in range(1,len(times)+1):
        if boundary<len(times) and times[boundary]-times[boundary-1]==HOUR:
            continue
        n=boundary-start
        high_confirm:dict[int,tuple[int,float]]={};low_confirm:dict[int,tuple[int,float]]={}
        for p in range(2,n-2):
            candle=bars[start+p]
            others=[bars[start+j] for j in range(p-2,p+3) if j!=p]
            if all(candle['h']>other['h'] for other in others):
                high_confirm[p+2]=(p,candle['h'])
            if all(candle['l']<other['l'] for other in others):
                low_confirm[p+2]=(p,candle['l'])
        last_hi=last_lo=None;broken_hi=set();broken_lo=set()
        for i in range(n):
            if i in high_confirm:
                pivot,level=high_confirm[i];last_hi=(pivot,i,level)
            if i in low_confirm:
                pivot,level=low_confirm[i];last_lo=(pivot,i,level)
            close=bars[start+i]['c']
            if last_hi and last_hi[0] not in broken_hi and i>last_hi[1] and close>last_hi[2]:
                broken_hi.add(last_hi[0]);result.append({'asof_ms':times[start+i]+HOUR,'direction':'bullish','broken_swing':last_hi[2]})
            if last_lo and last_lo[0] not in broken_lo and i>last_lo[1] and close<last_lo[2]:
                broken_lo.add(last_lo[0]);result.append({'asof_ms':times[start+i]+HOUR,'direction':'bearish','broken_swing':last_lo[2]})
        start=boundary
    return sorted(result,key=lambda x:(x['asof_ms'],x['direction']))


def follow_up(canc_open:int,start_index:int,opens:list[int],bars:list[dict[str,float]],
              lo:float,hi:float,side:str,bos:list[dict[str,Any]],
              test_end_ms:int,horizons:tuple[int,...]=HORIZONS)->dict[str,Any]:
    if side not in ('long','short') or lo>hi or start_index<0 or opens[start_index]!=canc_open:
        raise ValueError('Bad cancellation/OB')
    origin=bars[start_index]['c'];end_asof=canc_open+HOUR
    original='bullish' if side=='long' else 'bearish'
    out={}; previous=opens[start_index]
    for horizon in horizons:
        if horizon<=0:raise ValueError('Horizon must be positive')
        # Predeclared h future full 1H closes AFTER canceled candle close.
        # Test cutoff disallows any future candle not in the frozen Test split.
        after=start_index+1;last=after+horizon
        complete=(last<=len(opens) and end_asof+horizon*HOUR<=test_end_ms)
        if complete:
            for k in range(after,last):
                if opens[k]!=canc_open+(k-start_index)*HOUR:
                    complete=False;break
        tag=f'h{horizon}'
        if not complete:
            out[tag]={'status':'right_censored_missing_contiguous_test_followup',
                      'touch_ob':None,'close_inside_ob':None,'same_side_new_1h_bos':None,
                      'first_touch_utc':None,'first_same_side_bos_utc':None,'sequence':None,
                      'signed_close_change_pct':None}
            continue
        seq=bars[after:last]
        touch=[opens[after+j] for j,b in enumerate(seq) if b['l']<=hi and b['h']>=lo]
        close_inside=[opens[after+j] for j,b in enumerate(seq) if lo<=b['c']<=hi]
        boses=[b['asof_ms'] for b in bos if end_asof<b['asof_ms']<=end_asof+horizon*HOUR and b['direction']==original]
        t=touch[0] if touch else None
        be=boses[0] if boses else None
        if t is None or be is None:order='both_not_observed' if t is None and be is None else ('ob_only' if be is None else 'bos_only')
        elif t+HOUR<be:order='ob_bar_closed_before_bos_confirmed'
        elif be<=t:order='bos_confirmed_before_ob_bar_open'
        else:order='same_hour_order_unresolved'
        signed=(seq[-1]['c']/origin-1)*(1 if side=='long' else -1)*100
        out[tag]={'status':'complete','touch_ob':bool(touch),'close_inside_ob':bool(close_inside),
                  'same_side_new_1h_bos':bool(boses),
                  'first_touch_utc':utc(t) if t is not None else None,
                  'first_same_side_bos_utc':utc(be) if be is not None else None,
                  'sequence':order, 'signed_close_change_pct':round(signed,6)}
    return out


def run_symbol(history:Path,stage03:Path,stage04:Path,stage05:Path,symbol:str,out_dir:Path)->dict[str,Any]:
    if symbol not in SYMBOLS:raise ValueError('Not a frozen research symbol')
    summ=load_json(stage05/f'{symbol}_ob_distance_summary.json')
    st4=load_json(stage04/f'{symbol}_bos_timeline.json')
    st3=load_json(stage03/f'{symbol}_cancellation_causes.json')
    agg=load_json(stage05/'ob_distance_05_aggregate.json')
    if not (summ.get('status')=='verified_closed_1h_distance_observation'
         and summ.get('subhour_candles_used') is False and summ.get('model_strategy_unchanged') is True
         and st4.get('status')=='verified_from_closed_formal_bos_events'
         and st4.get('uses_subhour_candles') is False and st4.get('strategy_changed') is False
         and st3.get('status')=='verified_reconstructed_test_cancellation_snapshots'
         and st3.get('uses_subhour_data') is False
         and agg.get('status')=='all_three_symbols_verified' and agg.get('uses_subhour_data') is False):
        raise ValueError('Prior stage evidence was not verified')
    if agg.get('by_symbol', {}).get(symbol)!=summ:
        raise ValueError('Stage05 individual summary and frozen aggregate disagree')
    if summ['bos_cancellations']['n']!=EXPECTED_BOS[symbol] or summ['other_changed_cancellations']['n']!=EXPECTED_OTHER[symbol]:
        raise ValueError('Frozen stage 05 counts do not match')
    for iv in ('1d','4h','1h'):
        f=history/f'{symbol}_{iv}.csv'
        if sha(f)!=summ['source_sha256'][f.name] or sha(f)!=st4['source_sha256'][f.name]:
            raise ValueError('Frozen source K-line SHA256 mismatch')
    manifest=history/'multi_asset_coverage.json'
    if sha(manifest)!=summ['source_sha256']['coverage']:
        raise ValueError('Coverage SHA256 differs from frozen study')
    causes=stage03/f'{symbol}_cancellation_causes.csv'
    timeline=stage04/f'{symbol}_bos_timeline.csv'
    if sha(causes)!=summ['source_sha256'][causes.name] or sha(causes)!=st4['source_sha256']['stage03_causes_csv']:
        raise ValueError('Cancellation ledger SHA256 mismatch')
    if sha(timeline)!=summ['source_sha256']['prior_stage04_csv']:
        raise ValueError('BOS first-divergence SHA256 mismatch')
    crows=rows(causes);stage5=rows(stage05/f'{symbol}_ob_distance_records.csv')
    trows=rows(timeline)
    if len({x['setup_id'] for x in stage5})!=len(stage5) or len(stage5)!=EXPECTED_BOS[symbol]+EXPECTED_OTHER[symbol]:
        raise ValueError('Duplicate/missing stage 05 records')
    if len({x['setup_id'] for x in crows})!=len(crows) or {x['setup_id'] for x in crows}!={x['setup_id'] for x in stage5}:
        raise ValueError('Stage 03 and 05 cancellation sets diverge')
    bosmap={x['setup_id']:x for x in trows}
    if len(bosmap)!=EXPECTED_BOS[symbol]:raise ValueError('Bad stage 04 BOS cohort')
    cmap={x['setup_id']:x for x in crows}
    candle_rows=rows(history/f'{symbol}_1h.csv')
    opens,bars=clean_closed_hours(candle_rows,symbol)
    bos=confirmed_one_hour_bos(opens,bars)
    bos_keys={(x['asof_ms'],x['direction']) for x in bos}
    # Cut at the prior baseline's last fully closed UTC DAY, not the final partial day.
    universe=load_json(manifest)['universe']
    selected=universe['selected_at_server_ms']
    if type(selected)is not int or selected<=0:raise ValueError('Bad frozen selected time')
    test_end=(selected//86_400_000)*86_400_000
    recs=[]
    for r in stage5:
        setup=r['setup_id'];prior=cmap[setup]
        if (r['symbol']!=symbol or prior['original_side']!=r['original_side']
            or prior['accepted_open_utc']!=r['accepted_open_utc']
            or prior['canceled_open_utc']!=r['canceled_open_utc']
            or prior['category']!=r['cancel_category'] or prior['split']!='test'
            or r['price_snapshot_closed_hourly_only']!='True' or r['model_strategy_unchanged']!='True'):
            raise ValueError('Changed-cancellation identity and status mismatch')
        cancel=utc_ms(r['canceled_open_utc'])
        i=bisect.bisect_left(opens,cancel)
        if i>=len(opens) or opens[i]!=cancel or cancel>=test_end:
            raise ValueError('Canceled bar missing or outside frozen Test window')
        formed=utc_ms(r['ob_formed_open_utc'])
        fi=bisect.bisect_left(opens,formed)
        if fi>=len(opens) or opens[fi]!=formed:
            raise ValueError('Original OB formation candle absent')
        lo=price(r['ob_low']);hi=price(r['ob_high'])
        if lo>hi or not math.isclose(lo,bars[fi]['l'],rel_tol=0,abs_tol=1e-7) or not math.isclose(hi,bars[fi]['h'],rel_tol=0,abs_tol=1e-7):
            raise ValueError('Frozen OB zone differs from source candle')
        if not math.isclose(price(r['close_at_cancel']),bars[i]['c'],rel_tol=0,abs_tol=1e-6):
            raise ValueError('Cancel close differs from frozen OHLC')
        category='bos' if r['cancel_category']==REASON else 'other'
        if category=='bos':
            if setup not in bosmap or r['first_deviating_timeframe']!='1h':
                raise ValueError('Stage 04 BOS cohort does not match')
            details=json.loads(bosmap[setup]['first_deviation_detail'])
            if len(details)!=1 or details[0]['interval']!='1h' or details[0]['type']!='new_opposite_confirmed_bos':
                raise ValueError('Unsupported reverse BOS evidence')
            marker=utc_ms(details[0]['new_bos_confirmed_at_utc'])
            reversed_side='bearish' if r['original_side']=='long' else 'bullish'
            original_event_id=details[0]['new_bos_event']
            if f':structure_break:{reversed_side}:' not in original_event_id:
                raise ValueError('Reversal event ID and original direction do not agree')
            reference=price(original_event_id.rsplit(':',1)[1])
            same=[event for event in bos if event['asof_ms']==marker and event['direction']==reversed_side]
            if (marker!=cancel+HOUR or (marker,reversed_side) not in bos_keys
                or not any(math.isclose(event['broken_swing'],reference,rel_tol=0,abs_tol=1e-5) for event in same)):
                raise ValueError('Reverse BOS reconstruction mismatches original frozen evidence')
        elif setup in bosmap:raise ValueError('Non-BOS case included in BOS cohort')
        future=follow_up(cancel,i,opens,bars,lo,hi,r['original_side'],bos,test_end)
        result={'symbol':symbol,'setup_id':setup,'category':category,'side':r['original_side'],
                'accepted_open_utc':r['accepted_open_utc'],'canceled_open_utc':r['canceled_open_utc'],
                'ob_low':lo,'ob_high':hi,'close_at_cancel':bars[i]['c'],
                'waiting_hours':int(r['waiting_hours']), 'distance_at_cancel_atr14':float(r['gap_at_cancel_atr14']),
                'original_opposite_1h_bos_confirmed':category=='bos', 'followup':future}
        recs.append(result)
    if Counter(x['category'] for x in recs)!=Counter({'bos':EXPECTED_BOS[symbol],'other':EXPECTED_OTHER[symbol]}):
        raise ValueError('Cohort totals changed')
    out_dir.mkdir(parents=True,exist_ok=True)
    path=out_dir/f'{symbol}_post_cancel_records.json'
    save_json(path,{'version':VERSION,'symbol':symbol,'source_run':43820018876,'stage05_run':43838687913,
                    'data_timeframes_used':list(TIMEFRAMES),'uses_subhour_candles':False,
                    'strategy_changed':False,'status':'verified_frozen_order_cancellations',
                    'input_sha256':{'stage05_csv':sha(stage05/f'{symbol}_ob_distance_records.csv'),
                                    'stage05_json':sha(stage05/f'{symbol}_ob_distance_summary.json'),
                                    'stage04_csv':sha(timeline),'stage03_csv':sha(causes),
                                    'coverage':sha(manifest),
                                    **{iv:sha(history/f'{symbol}_{iv}.csv') for iv in ('1h','4h','1d')}},
                    'cancellations':recs})
    return {'status':'verified_frozen_order_cancellations','symbol':symbol,'cases':len(recs),
            'bos_cancellations':EXPECTED_BOS[symbol],'other_cancellations':EXPECTED_OTHER[symbol],
            'evidence_file':str(path)}


def _bucket(recs:list[dict[str,Any]],horizon:int)->dict[str,Any]:
    field=f'h{horizon}'
    complete=[r for r in recs if r['followup'][field]['status']=='complete']
    touched=[r for r in complete if r['followup'][field]['touch_ob']]
    recovered=[r for r in complete if r['followup'][field]['same_side_new_1h_bos']]
    return {'total_cases':len(recs),'complete_followup':len(complete),'right_censored':len(recs)-len(complete),
            'ob_wick_touch_n':len(touched),'ob_wick_touch_pct_of_complete':round(100*len(touched)/len(complete),3) if complete else None,
            'close_inside_original_ob_n':sum(bool(r['followup'][field]['close_inside_ob']) for r in complete),
            'new_original_side_1h_bos_n':len(recovered),
            'new_original_side_1h_bos_pct_of_complete':round(100*len(recovered)/len(complete),3) if complete else None,
            'both_touch_and_bos':sum(bool(r['followup'][field]['touch_ob']) and bool(r['followup'][field]['same_side_new_1h_bos']) for r in complete),
            'signed_close_change_pct_median':med([float(r['followup'][field]['signed_close_change_pct']) for r in complete]),
            'order_when_both':dict(sorted(Counter(r['followup'][field]['sequence'] for r in complete if r['followup'][field]['touch_ob'] and r['followup'][field]['same_side_new_1h_bos']).items()))}


def aggregate(input_dir:Path,out_dir:Path)->dict[str,Any]:
    bysymbol={};allrows=[]
    for symbol in SYMBOLS:
        obj=load_json(input_dir/f'{symbol}_post_cancel_records.json')
        if obj.get('version')!=VERSION or obj.get('symbol')!=symbol or obj.get('status')!='verified_frozen_order_cancellations' or obj.get('uses_subhour_candles') is not False or obj.get('strategy_changed') is not False:
            raise ValueError('Bad/missing single-symbol evidence')
        data=obj['cancellations']
        if len(data)!=EXPECTED_BOS[symbol]+EXPECTED_OTHER[symbol] or len({x['setup_id'] for x in data})!=len(data):
            raise ValueError('Bad group count or duplicated setups')
        if Counter(x['category'] for x in data)!=Counter({'bos':EXPECTED_BOS[symbol],'other':EXPECTED_OTHER[symbol]}):
            raise ValueError('Cohort mismatch')
        bysymbol[symbol]={str(h):{'bos':_bucket([r for r in data if r['category']=='bos'],h),'other':_bucket([r for r in data if r['category']=='other'],h)} for h in HORIZONS}
        allrows.extend(data)
    out={'version':VERSION,'report_type':'post_cancel_original_OB_descriptive_observation',
         'status':'all_136_original_cancellations_verified','cohort':'prior 2025-2026 viewed Test; NOT new OOS',
         'decisions_only_from':['1h','4h','1d'],'trading_strategy_changed':False,'orders_placed':False,
         'predeclared_followup_hours':list(HORIZONS),'individual':bysymbol,
         'all':{str(h):{'bos':_bucket([r for r in allrows if r['category']=='bos'],h),
                         'other':_bucket([r for r in allrows if r['category']=='other'],h)} for h in HORIZONS},
         'input_sha256':{f'{s}_post_cancel_records.json':sha(input_dir/f'{s}_post_cancel_records.json') for s in SYMBOLS},
         'limitations':['OB wick overlap after cancellation is NOT a limit fill, and does not imply a profitable trade.',
                        'Same-direction 1H BOS is a confirmed historical structure event, NOT the reactivation of a pending order or whole MTF strategy.',
                        'Only previously observed Test cancellations were selected; both cohorts are non-random, selection-biased, and cross-asset correlated.',
                        'The 24/72/168-hour windows are descriptive, not optimized future filters or investment rules.',
                        'Follow-up that cannot span an entire contiguous Test horizon is censored, NOT counted as negative.',
                        'Only 1H,4H,1D closed Binance Spot candles and prior P0 ledgers; no 1m/5m/15m, no orders.']}
    out_dir.mkdir(parents=True,exist_ok=True)
    save_json(out_dir/'post_cancel_06_summary.json',out)
    lines=['# P0-ORDER-STATE-06｜取消後原 OB 與 1H BOS 後續觀察','','研究時段為**已看過的 Test**，取消後 24/72/168 小時為事先固定觀察期限，不改策略或做單週期。','','| 追蹤期 | 取消類別 | 樣本 | 完整觀測 | 原 OB 影線觸及 | 新確認原方向 1H BOS | 原方向收盤變化中位 |','|---:|---|---:|---:|---:|---:|---:|']
    for h in HORIZONS:
        for group in ('bos','other'):
            x=out['all'][str(h)][group]
            lines.append(f"| {h} 小時 | {'1H 反向 BOS 取消' if group=='bos' else '其他原因取消'} | {x['total_cases']} | {x['complete_followup']} | {x['ob_wick_touch_n']} ({x['ob_wick_touch_pct_of_complete']}%) | {x['new_original_side_1h_bos_n']} ({x['new_original_side_1h_bos_pct_of_complete']}%) | {x['signed_close_change_pct_median']}% |")
    lines+=['','## 方法及解讀','','- 追蹤從**取消 K 線收盤後的下一根完整 1H**開始；不可用取消當根的盤中順序推導成交。','- 1H BOS 使用與既有引擎一致的 2/2 根嚴格 Swing + 收盤突破確認，並以原反向 BOS 94 筆資料做時序核對。','- K 線出現影線觸及 OB 不代表實際成交，更不是取消錯誤或潛在獲利。','- 1H 新同向 BOS 不代表 4H／1D 都重新一致；不能自行恢復原掛單。','- 不足完整窗口、資料缺口一律標示右側截尾，不納入該窗口的比例分母。','- 後見篩選與幣種相關性均存在；不能以這批已觀察 Test 選取策略參數。']
    (out_dir/'post_cancel_06_summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    with (out_dir/'post_cancel_06_cases.csv').open('w',encoding='utf-8-sig',newline='') as f:
        keys=['symbol','setup_id','category','side','accepted_open_utc','canceled_open_utc','ob_low','ob_high','close_at_cancel','waiting_hours','distance_at_cancel_atr14']
        for h in HORIZONS:
            keys.extend([f'h{h}_{k}' for k in ('status','touch_ob','close_inside_ob','same_side_new_1h_bos','first_touch_utc','first_same_side_bos_utc','sequence','signed_close_change_pct')])
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader()
        for r in allrows:
            flat={k:r.get(k) for k in keys if k in r}
            for h in HORIZONS:
                flat.update({f'h{h}_{k}':v for k,v in r['followup'][f'h{h}'].items()})
            w.writerow(flat)
    return out


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description='1H+ canceled OB follow-up (observational; never orders)')
    sub=parser.add_subparsers(dest='mode',required=True)
    a=sub.add_parser('symbol');a.add_argument('symbol',choices=SYMBOLS)
    for name in ('history','stage03','stage04','stage05','out'):
        a.add_argument('--'+name,required=True,type=Path)
    b=sub.add_parser('aggregate');b.add_argument('--input',required=True,type=Path);b.add_argument('--out',required=True,type=Path)
    opt=parser.parse_args(argv)
    if opt.mode=='symbol':
        print(json.dumps(run_symbol(opt.history,opt.stage03,opt.stage04,opt.stage05,opt.symbol,opt.out),ensure_ascii=False))
    else:
        report=aggregate(opt.input,opt.out)
        print(json.dumps({'status':report['status'],'totals':report['all']},ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())