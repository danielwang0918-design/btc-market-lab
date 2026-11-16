#!/usr/bin/env python3
"""P1-05 Stage2: frozen P0 per-symbol *paper* entry/SL/TP cash-risk audit.

No live feeds, no broker, no order capability. Read-only, deterministic replay.
OHLCV cannot establish within-bar order of touch; this is NOT actual fills.
"""
from __future__ import annotations
import argparse,csv,hashlib,io,json,math
from collections import Counter
from datetime import datetime,timezone
from decimal import Decimal, getcontext
from pathlib import Path
from zipfile import ZipFile

from p1_05_stage1_audit import load_frozen, parse_time, START, RISK_FRACTION, MAX_TRADES, SYMBOLS

getcontext().prec=40
VERSION='p1-05-stage2-frozen-price-risk-audit-v1'
FEE=Decimal('0.001')
SLIP=Decimal('0.0005')
ONE=Decimal('1')
STEP_MS=3_600_000
PER_SYMBOL_SHA={
 'BTCUSDT':'522f42b2fad531759876ebb51b7ee1470e0997d3749514997ab7ffe6c9671ff9',
 'ETHUSDT':'0c20281afdf9faa343b0c4bcbf23d34baae67b2ceeac467b2581d46446df7be1',
 'SOLUSDT':'ca3620249fd0a5244ee02c7426a461f3679c00ed1b28a5607e136528f7c7379a',
 'ZECUSDT':'6a69930b31bfba03d6b449e390afb0e7ad04dd8f487b90079cedb2de484fcb95',
 'XRPUSDT':'f0aae21f3715ba6077b7357b01c470a89b0f55759cb138e0428f65d706819baa',
 'BNBUSDT':'40067c4b6153452daffa42b425125c67b0c21ac9fa1a2a858c227dd1aa826003',
 'NEARUSDT':'096b8ded5710cbb86e8d96d6cc674d526883a69168200ae114b5240cb94b3048',
 'DOGEUSDT':'075999683ca6cc99673104d776bcaf3ab665512139ea31a25bd430b828eef68b',
}
FIELDS=('symbol','split','setup_id','direction','signal_time_utc','entry_time_utc','exit_time_utc','exit_reason','bars_held','r_multiple')
DECIMAL_FIELDS=('raw_entry_price','entry_price','exit_price','stop_price','target_price','gross_pnl_per_unit','fees_paid_per_unit','net_pnl_per_unit','r_multiple')
FIRST_SPLIT=__import__('datetime').datetime(2023,1,1,tzinfo=timezone.utc)
SECOND_SPLIT=__import__('datetime').datetime(2025,1,1,tzinfo=timezone.utc)

def digest(data:bytes)->str:return hashlib.sha256(data).hexdigest()

def num(x):
    try:d=Decimal(str(x))
    except Exception as e: raise ValueError('Invalid decimal') from e
    if not d.is_finite():raise ValueError('Nonfinite numerical data')
    return d

def dt_ms(t):return int(parse_time(t).timestamp()*1000)

def key(row):return (row['symbol'],row['split'],row['setup_id'])

def _norm(t):
    return {k:str(t[k]) for k in FIELDS}

def _close(a,b,tol=Decimal('0.00000000000001')):
    return abs(a-b)<=tol*max(ONE,abs(a),abs(b))

def load_price_sources(folder:Path, frozen_rows:list[dict])->dict:
    wanted={key(x):x for x in frozen_rows}
    if len(wanted)!=1340:raise ValueError('P0 duplicate source identity')
    result={}
    for sym in sorted(SYMBOLS):
        path=folder/f'{sym}_fixed_calendar_baseline.json'
        raw=path.read_bytes()
        if digest(raw)!=PER_SYMBOL_SHA[sym]:raise ValueError(f'Per-symbol SHA256 mismatch: {sym}')
        data=json.loads(raw.decode('utf-8-sig'))
        if data.get('symbol')!=sym or data.get('status')!='complete' or data.get('strategy') is None:
            raise ValueError(f'Bad per-symbol metadata {sym}')
        for t in data['trades']:
            if t.get('symbol')!=sym or not set(DECIMAL_FIELDS)<=set(t):raise ValueError('Malformed trade price fields')
            k=key(t)
            if k not in wanted or k in result:raise ValueError('Unexpected or repeated per-symbol setup')
            base=wanted[k]
            for col in FIELDS:
                if col in ('r_multiple',):
                    if num(t[col])!=num(base[col]):raise ValueError('R differs from pooled source')
                elif str(t[col])!=str(base[col]):raise ValueError(f'Per-symbol/pooled mismatch {col}')
            result[k]=t
    if set(result)!=set(wanted):raise ValueError('Missing source priced trades')
    return result

def load_one_hour_bars(zip_path:Path,lock_path:Path)->dict[str,dict[int,dict]]:
    lock_bytes=lock_path.read_bytes()
    lock=json.loads(lock_bytes)
    expected=lock['frozen_data_prefixes']
    result={}
    with ZipFile(zip_path) as z:
        names=z.namelist()
        if len(names)!=len(set(names)):raise ValueError('Duplicate frozen ZIP filename')
        for member,meta in expected.items():
            raw=z.read(member)
            if digest(raw)!=meta['sha256'] or len(raw)!=meta['bytes']:
                raise ValueError(f'Frozen OHLCV content hash/bytes mismatch {member}')
            # Strictly audit all 24 frozen input CSVs, but parse only the 8 hourly series.
            if not member.endswith('_1h.csv') or meta['symbol'] not in SYMBOLS:continue
            frames={};prior=-1
            for row in csv.DictReader(io.StringIO(raw.decode('utf-8-sig'),newline='')):
                if row['symbol']!=meta['symbol'] or row['interval']!='1h' or row['is_closed'].lower()!='true':raise ValueError('Wrong 1h identity/closed status')
                ts=int(row['open_time']);stop_time=int(row['close_time']);
                if ts<=prior or ts%STEP_MS or stop_time!=ts+STEP_MS-1:raise ValueError('Invalid frozen bar time')
                prior=ts
                o,h,l,c,v=(num(row[x]) for x in ('open','high','low','close','volume'))
                if not (l>0 and l<=o<=h and l<=c<=h and v>=0):raise ValueError('Invalid OHLCV')
                frames[ts]={'o':o,'h':h,'l':l,'c':c,'v':v}
            if len(frames)!=meta['frozen_bars']:raise ValueError('Frozen hourly bar count drift')
            result[meta['symbol']]=frames
    if len(result)!=8:raise ValueError('Expected eight one-hour series')
    return result

def fill_price(side,raw,entry):
    return raw*(ONE+SLIP if ((side=='long')==entry) else ONE-SLIP)

def verified_trade(priced:dict,bars:dict[int,dict])->dict:
    side=priced['direction'];entry_time=dt_ms(priced['entry_time_utc']);exit_time=dt_ms(priced['exit_time_utc'])
    if side not in ('long','short') or not (entry_time<exit_time and entry_time%STEP_MS==0 and exit_time%STEP_MS==0):raise ValueError('Bad fill timing')
    entry_bar=bars.get(entry_time);exit_bar=bars.get(exit_time-STEP_MS)
    if entry_bar is None or exit_bar is None:raise ValueError('Missing exact source 1H entry/exit candle')
    raw,entry,exit_,stop,target,gross,fees,net,reported_r=(num(priced[x]) for x in DECIMAL_FIELDS)
    if min(raw,entry,exit_,stop,target)<=0:raise ValueError('Nonpositive prices')
    if not _close(entry,fill_price(side,raw,True)):
        raise ValueError('Source entry/slippage formula inconsistent')
    if side=='long':
        if not stop<raw<target:raise ValueError('Invalid long stop/target placement')
        actual_gross=exit_-entry
    else:
        if not target<raw<stop:raise ValueError('Invalid short stop/target placement')
        actual_gross=entry-exit_
    stop_fill=fill_price(side,stop,False)
    risk_unit=abs(entry-stop_fill)+(entry+stop_fill)*FEE
    actual_fee=(entry+exit_)*FEE
    if risk_unit<=0 or not _close(gross,actual_gross) or not _close(fees,actual_fee) or not _close(net,actual_gross-actual_fee):
        raise ValueError('P0 trade price / fee / R cashflow invalid')
    reconstructed_r=net/risk_unit
    if abs(reconstructed_r-reported_r)>Decimal('0.00000000500001'):raise ValueError('Original R does not reconcile to price and stop')
    if priced['exit_reason']=='stop' and not _close(exit_,stop_fill):raise ValueError('Stop fill not at stop')
    if priced['exit_reason']=='target' and not _close(exit_,fill_price(side,target,False)):raise ValueError('Target fill not at target')
    if priced['exit_reason'] not in ('stop','target','data_gap_exit'):raise ValueError('Unknown exit reason')
    entry_reachable=entry_bar['v']>0 and entry_bar['l']<=raw<=entry_bar['h']
    raw_exit=exit_/(ONE-SLIP if side=='long' else ONE+SLIP)
    exit_reachable=exit_bar['v']>0 and exit_bar['l']<=raw_exit<=exit_bar['h']
    reasons=[]
    if entry_bar['v']==0:reasons.append('zero_volume_entry_bar')
    if not entry_reachable:reasons.append('entry_reference_not_reached_by_1h_range')
    if exit_bar['v']==0:reasons.append('zero_volume_exit_bar')
    if not exit_reachable:reasons.append('exit_reference_not_reached_by_1h_range')
    same_bar=entry_time==exit_time-STEP_MS
    if same_bar:reasons.append('same_bar_entry_and_exit_order_unknown')
    # We do not use any future price information to decide actual eligibility for
    # the original P0 published model. These flags are *after-trade diagnostics*.
    return {**priced,'_entry':parse_time(priced['entry_time_utc']),'_exit':parse_time(priced['exit_time_utc']),
            '_per_unit_risk':risk_unit,'_per_unit_pnl':net,'_fill_entry':entry,
            '_raw_entry':raw,'_stop':stop,'_target':target,
            '_r':reconstructed_r,'_diagnostic':';'.join(reasons),'_same_bar':same_bar,
            '_entry_reachable':entry_reachable,'_exit_reachable':exit_reachable,
            '_entry_volume':entry_bar['v'],'_exit_volume':exit_bar['v']}

def size_one(equity:Decimal,priced:dict,*,cash_capped:bool)->dict:
    if not equity>0:raise ValueError('No capital to size position')
    risk_budget=equity*RISK_FRACTION
    theoretical_qty=risk_budget/priced['_per_unit_risk']
    cash_qty=equity/(priced['_fill_entry']*(ONE+FEE))
    qty=min(theoretical_qty,cash_qty) if cash_capped else theoretical_qty
    if qty<=0:raise ValueError('Zero qty')
    notional=priced['_fill_entry']*qty
    invested=notional*(ONE+FEE)
    actual_risk=qty*priced['_per_unit_risk']
    net=qty*priced['_per_unit_pnl']
    if cash_capped and invested>equity*(ONE+Decimal('1e-28')):raise ValueError('Overspent quote cash')
    if actual_risk>risk_budget*(ONE+Decimal('1e-28')):raise ValueError('Above model risk budget')
    return {'qty':qty,'notional':notional,'cash_reserved':invested,'risk_budget':risk_budget,
            'actual_risk':actual_risk,'model_pnl':net,'equity_after':equity+net,
            'sizing_status':'cash_capped' if cash_capped and cash_qty<theoretical_qty else 'risk_budget_sized'}

def _pct(n,d):return float(Decimal(n)*100/Decimal(d)) if d else None

def replay(priced_rows:list[dict],*,long_only:bool,max_trades:int=MAX_TRADES):
    # Strictly causal tie-break, never sorting on *exit* or trade return.
    rows=sorted(priced_rows,key=lambda r:(r['_entry'],r['symbol'],r['setup_id']))
    equity=START;peak=START;last_exit=None;maxdd=Decimal(0)
    accepted=[];decisions=[]
    for r in rows:
        decision={'symbol':r['symbol'],'split':r['split'],'setup_id':r['setup_id'],
            'entry_time_utc':r['entry_time_utc'],'direction':r['direction'],'status':'rejected','reason':''}
        if long_only and r['direction']=='short':reason='spot_short_unavailable'
        elif len(accepted)>=max_trades:reason='trade_cap'
        elif last_exit is not None and r['_entry']<last_exit:reason='position_open'
        else:
            b=equity;s=size_one(equity,r,cash_capped=long_only)
            equity=s['equity_after']
            if not equity.is_finite() or equity<0:raise ValueError('Insolvent model account')
            peak=max(peak,equity);maxdd=max(maxdd,(peak-equity)/peak)
            info={'trade_number':len(accepted)+1,'symbol':r['symbol'],'split':r['split'],'setup_id':r['setup_id'],
                'direction':r['direction'],'entry_time_utc':r['entry_time_utc'],'exit_time_utc':r['exit_time_utc'],
                'exit_reason':r['exit_reason'],'entry_price':r['entry_price'],'raw_entry_price':r['raw_entry_price'],
                'stop_price':r['stop_price'],'target_price':r['target_price'],'exit_price':r['exit_price'],
                'r_multiple_original':r['r_multiple'],'r_recomputed':r['_r'],
                'model_quantity':s['qty'],'entry_notional_usdt':s['notional'],'entry_cash_required_usdt':s['cash_reserved'],
                'risk_budget_usdt':s['risk_budget'],'actual_model_risk_usdt':s['actual_risk'],
                'sizing_status':s['sizing_status'],'pnl_usdt':s['model_pnl'],
                'equity_before_usdt':b,'equity_after_usdt':equity,
                'price_reachability_entry':r['_entry_reachable'],'price_reachability_exit':r['_exit_reachable'],
                'has_same_bar_sequence_unknown':r['_same_bar'],'after_trade_diagnostics':r['_diagnostic'],
                'exchange_filter_status':'UNVERIFIED_historical_MIN_NOTIONAL_STEP_SIZE_TICK_SIZE',
                'execution_status':'OHLCV_PAPER_NOT_LIVE_FILL'}
            accepted.append(info);last_exit=r['_exit']
            decision['status']='accepted';decision['trade_number']=len(accepted)
            decisions.append(decision);continue
        decision['reason']=reason;decisions.append(decision)
    n=len(accepted);rs=[num(x['r_recomputed']) for x in accepted]
    wins=sum(x>0 for x in rs);negative=[x for x in rs if x<0];positive=[x for x in rs if x>0]
    gain=sum(positive,Decimal(0));loss=-sum(negative,Decimal(0))
    summary={'accepted_trades':n,'wins':wins,'losses':len(negative),'flats':n-wins-len(negative),
      'win_rate_pct':_pct(wins,n),'mean_r':float(sum(rs,Decimal(0))/n) if n else None,
      'profit_factor_r':float(gain/loss) if loss>0 else None,
      'initial_equity_usdt':float(START),'final_equity_usdt':float(equity),
      'return_pct':float((equity/START-ONE)*100),'max_closed_trade_drawdown_pct':float(maxdd*100),
      'spot_long_only':long_only,'candidate_trades':len(rows),
      'acceptance_rationale':'one position; 2% risk target; actual qty cash capped for long-only; hypothetical unconstrained notional for short+long',
      'rejections':dict(Counter(d['reason'] for d in decisions if d['reason'])),
      'cash_capped_trades':sum(x['sizing_status']=='cash_capped' for x in accepted),
      'reachability_unverified_trades':sum(not x['price_reachability_entry'] or not x['price_reachability_exit'] for x in accepted),
      'same_hour_entry_exit_trades':sum(x['has_same_bar_sequence_unknown'] for x in accepted),
      'exchange_filters_unverified_count':len(accepted),
      'model_order_notional_under_5_usdt':sum(num(x['entry_notional_usdt'])<Decimal('5') for x in accepted),
      'model_order_notional_under_10_usdt':sum(num(x['entry_notional_usdt'])<Decimal('10') for x in accepted),
      'actual_risk_as_pct_of_equity_mean':float(sum((num(x['actual_model_risk_usdt'])/num(x['equity_before_usdt']) for x in accepted),Decimal(0))/Decimal(n)*100) if n else None,
      'start_date_utc':accepted[0]['entry_time_utc'] if accepted else None,
      'end_date_utc':accepted[-1]['exit_time_utc'] if accepted else None}
    return accepted,decisions,summary

def write_csv(path,rows):
    if not rows:raise ValueError('No output rows')
    cols=list(rows[0].keys())
    with path.open('w',encoding='utf-8',newline='') as out:
        w=csv.DictWriter(out,fieldnames=cols);w.writeheader()
        for x in rows:w.writerow({k:str(v) if v is not None else '' for k,v in x.items()})

def run(baseline_zip:Path,sources:Path,frozen_ohlcv:Path,lock:Path,out:Path)->dict:
    original,base=load_frozen(baseline_zip)
    priced=load_price_sources(sources,original)
    bars=load_one_hour_bars(frozen_ohlcv,lock)
    analyzed=[verified_trade(priced[key(r)],bars[r['symbol']]) for r in original]
    if len(analyzed)!=1340:raise ValueError('Audited trade count drift')
    out.mkdir(parents=True,exist_ok=True)
    # Full diagnostic exports, not win/loss selection.
    diag=[]
    for r in analyzed:
        diag.append({k:r.get(k,'') for k in FIELDS}|{
          'raw_entry_price':r['raw_entry_price'],'entry_price':r['entry_price'],'exit_price':r['exit_price'],
          'stop_price':r['stop_price'],'target_price':r['target_price'],
          'risk_per_unit':r['_per_unit_risk'],'net_pnl_per_unit':r['_per_unit_pnl'],
          'r_recomputed':r['_r'],'entry_reachable':r['_entry_reachable'],
          'exit_reachable':r['_exit_reachable'],'entry_volume':r['_entry_volume'],
          'exit_volume':r['_exit_volume'],'same_bar':r['_same_bar'],'diagnostic_flags':r['_diagnostic']})
    write_csv(out/'all_1340_source_price_audit.csv',diag)
    reports={};
    for name,long_only in [('hypothetical_both_sides',False),('spot_long_only_cash_capped',True)]:
        acc,dec,summary=replay(analyzed,long_only=long_only)
        reports[name]=summary
        write_csv(out/f'{name}_ledger.csv',acc)
        write_csv(out/f'{name}_decisions.csv',dec)
        for split in ('train','validation','test'):
            a,_,s=replay([x for x in analyzed if x['split']==split],long_only=long_only)
            reports[name+'_'+split]=s
    common_end=reports['hypothetical_both_sides']['end_date_utc']
    for name in ('hypothetical_both_sides','spot_long_only_cash_capped'):
        with (out/f'{name}_ledger.csv').open(encoding='utf-8',newline='') as stream:
            prior=[r for r in csv.DictReader(stream) if parse_time(r['exit_time_utc'])<=parse_time(common_end)]
        reports[name]['common_calendar_cutoff_utc']=common_end
        reports[name]['closed_trades_by_common_cutoff']=len(prior)
        reports[name]['equity_by_common_cutoff_usdt']=float(num(prior[-1]['equity_after_usdt'])) if prior else float(START)
    result={'status':'PAPER_PRICES_AND_CASH_RISK_VERIFIED_NOT_REAL_FILLS',
      'version':VERSION,'source_github_run':43820018876,
      'source_baseline_sha256':'7546fc9692be973784ccd27c35b79b745407b8ba37bf0e30d1672d4aa1544e2c',
      'source_8_symbol_json_sha256':PER_SYMBOL_SHA,
      'price_formula':'R = [(exit_fill-entry_fill)*side - 0.001*(entry_fill+exit_fill)] / (abs(entry_fill-normal_stop_fill)+0.001*(entry_fill+normal_stop_fill))',
      'initial_equity':'1000','risk_budget_fraction':'0.02',
      'all_source_trades':len(analyzed),'source_1h_bars_checked':{k:len(v) for k,v in bars.items()},
      'source_order_quantity_not_present':True,
      'all_1340_unreachable_source_price':sum(not r['_entry_reachable'] or not r['_exit_reachable'] for r in analyzed),
      'all_1340_same_bar_unknown':sum(r['_same_bar'] for r in analyzed),
      'results':reports,
      'limitations':['Original P0 limit entries are recorded at the hour open even if the price is touched later in that same hour; intrabar execution order is unknowable',
        'OHLCV ranges indicate potential price reachability, not demonstrated filled orders',
        'Historical exchange LOT_SIZE/MIN_NOTIONAL/tickSize/market impact and latency are absent: never claim exchange-executable order sizes',
        'No actual Spot shorts, borrowing, perpetual margin or liquidation model',
        'All-direction scenario cannot be executed as a one-thousand-USDT Spot account; unconstrained short/long notional is only R-model comparability',
        'Spot-long scenario caps cash to equity including entry fee, sizes fractional quantity without unverifiable exchange lot rounding',
        'Model stop -1R assumes stop fills exactly despite gaps; actual losses can exceed 2%',
        'Closed-trade drawdown excludes intra-position mark-to-market',
        'Data are frozen and the historical 2025+ Test is already opened; no new untouched OOS or new strategy edge']}
    (out/'p1_05_stage2_manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    return result

def main(argv=None):
    p=argparse.ArgumentParser(description='P1-05 Stage2, read-only frozen paper price/stop/TP and sizing audit')
    p.add_argument('--baseline-zip',type=Path,required=True)
    p.add_argument('--sources',type=Path,required=True)
    p.add_argument('--frozen-ohlcv',type=Path,required=True)
    p.add_argument('--lock',type=Path,required=True)
    p.add_argument('--out-dir',type=Path,required=True)
    a=p.parse_args(argv)
    z=run(a.baseline_zip,a.sources,a.frozen_ohlcv,a.lock,a.out_dir)
    print(json.dumps({'status':z['status'],'source_trades':z['all_source_trades'],'results':{k:v for k,v in z['results'].items() if k in ('hypothetical_both_sides','spot_long_only_cash_capped')}},ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())