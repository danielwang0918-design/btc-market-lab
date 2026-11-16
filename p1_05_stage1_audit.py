#!/usr/bin/env python3
"""P1-05 Stage 1: strict read-only, paper-only portfolio replay of a frozen P0 R ledger.

No broker connection. Not a fills/position-sizing simulator: source R already
contains model fees/slippage. Does not modify or select strategies.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import io
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from zipfile import ZipFile

getcontext().prec = 40
VERSION = 'p1-05-stage1-causal-ledger-audit-v1'
ORIGIN_RUN = 43820018876
BASELINE_MEMBER = 'research/multi_asset/summary/multi_asset_fixed_calendar_baseline.json'
TRADES_MEMBER = 'research/multi_asset/summary/multi_asset_fixed_calendar_trades.csv'
BASELINE_SHA = '7546fc9692be973784ccd27c35b79b745407b8ba37bf0e30d1672d4aa1544e2c'
TRADES_SHA = 'cc9ae34de758569306e24d7e8cdec79006ef502effeeadc85425e09547a0d0c0'
SYMBOLS = frozenset(('BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','ZECUSDT','NEARUSDT','DOGEUSDT'))
SPLITS = ('train','validation','test')
EXPECTED_SOURCE_SPLITS = {'train':691,'validation':318,'test':331}
START = Decimal('1000')
RISK_FRACTION = Decimal('0.02')
MAX_TRADES = 1000
EXPECTED_KEYS = {'symbol','split','setup_id','direction','signal_time_utc','entry_time_utc','exit_time_utc','exit_reason','r_multiple','bars_held'}
END_TRAIN = datetime(2023,1,1,tzinfo=timezone.utc)
END_VALIDATION = datetime(2025,1,1,tzinfo=timezone.utc)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_time(s: str) -> datetime:
    t = datetime.fromisoformat(s.replace('Z','+00:00'))
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError('Naive datetime')
    return t.astimezone(timezone.utc)


def parse_r(s: str) -> Decimal:
    x = Decimal(s)
    if not x.is_finite() or x < Decimal('-1'):
        raise ValueError('Invalid P0 risk multiple; model -1R stop assumption violated')
    return x


def validate_trade(raw: dict[str,str]) -> dict:
    if set(raw)!=EXPECTED_KEYS:
        raise ValueError('Unexpected source trade schema')
    if raw['symbol'] not in SYMBOLS or raw['split'] not in SPLITS:
        raise ValueError('Unknown symbol/split')
    if raw['direction'] not in ('long','short') or raw['exit_reason'] not in ('stop','target','data_gap_exit'):
        raise ValueError('Unknown side or exit state')
    if not raw['setup_id'].startswith(raw['symbol']+':'):
        raise ValueError('Malformed setup identity')
    sig,ent,ext = (parse_time(raw[key]) for key in ('signal_time_utc','entry_time_utc','exit_time_utc'))
    if sig>ent or ent>=ext or (ent-sig).total_seconds()>7*24*3600:
        raise ValueError('Invalid entry/signal/exit timing')
    expected_split = 'train' if sig<END_TRAIN else ('validation' if sig<END_VALIDATION else 'test')
    if expected_split!=raw['split']:
        raise ValueError('Split mismatch with decision timestamp')
    if not raw['bars_held'].isdigit() or int(raw['bars_held'])<1:
        raise ValueError('Bad bars_held')
    return {**raw,'_signal':sig,'_entry':ent,'_exit':ext,'_r':parse_r(raw['r_multiple'])}


def load_frozen(path:Path)->tuple[list[dict],dict]:
    with ZipFile(path) as z:
        files=z.namelist()
        if len(files)!=len(set(files)):
            raise ValueError('Duplicate ZIP members')
        trades_bytes=z.read(TRADES_MEMBER)
        baseline_bytes=z.read(BASELINE_MEMBER)
    if sha(trades_bytes)!=TRADES_SHA or sha(baseline_bytes)!=BASELINE_SHA:
        raise ValueError('Frozen P0 SHA256 mismatch')
    baseline=json.loads(baseline_bytes)
    if baseline.get('status')!='complete' or set(baseline.get('complete_symbols',[]))!=SYMBOLS:
        raise ValueError('Incomplete eight-symbol baseline')
    reader=csv.DictReader(io.StringIO(trades_bytes.decode('utf-8-sig'),newline=''))
    if set(reader.fieldnames or [])!=EXPECTED_KEYS:
        raise ValueError('Baseline CSV schema changed')
    seen=set();rows=[]
    for raw in reader:
        x=validate_trade(raw)
        key=(x['symbol'],x['split'],x['setup_id'])
        if key in seen:raise ValueError('Duplicated setup id')
        seen.add(key);rows.append(x)
    if len(rows)!=1340 or Counter(x['split'] for x in rows)!=EXPECTED_SOURCE_SPLITS:
        raise ValueError('Source sample count drift')
    for split,n in EXPECTED_SOURCE_SPLITS.items():
        if baseline['pooled_observational'][split]['trade_count']!=n:
            raise ValueError('Frozen baseline report/CSV mismatch')
    return rows,baseline


def mean(xs:list[Decimal])->Decimal|None:
    return sum(xs,Decimal(0))/Decimal(len(xs)) if xs else None


def calc_metrics(accepted:list[dict],equity:Decimal,peak_drawdown:Decimal)->dict:
    rs=[x['_r'] for x in accepted]
    wins=[x for x in rs if x>0]; losses=[x for x in rs if x<0]
    gp=sum(wins,Decimal(0));gl=-sum(losses,Decimal(0))
    return {'accepted_trades':len(accepted),'wins':len(wins),'losses':len(losses),'flat':len(rs)-len(wins)-len(losses),
      'win_rate_pct':float(Decimal(100)*len(wins)/len(rs)) if rs else None,
      'mean_net_r':float(mean(rs)) if rs else None,
      'profit_factor_net_r':float(gp/gl) if gl else (None if not wins else 'infinite'),
      'final_equity_usdt':float(equity),'return_pct':float((equity/START-1)*100),
      'max_closed_trade_drawdown_pct':float(peak_drawdown*100),
      'split_counts':dict(Counter(x['split'] for x in accepted)),
      'symbol_counts':dict(Counter(x['symbol'] for x in accepted)),
      'direction_counts':dict(Counter(x['direction'] for x in accepted)),
      'first_entry':accepted[0]['entry_time_utc'] if accepted else None,
      'last_exit':accepted[-1]['exit_time_utc'] if accepted else None}


def replay(rows:list[dict],*,max_trades:int=MAX_TRADES,spot_long_only:bool=False)->tuple[list[dict],list[dict],dict]:
    if max_trades<1:raise ValueError('max_trades must be positive')
    ordered=sorted(rows,key=lambda x:(x['_entry'],x['symbol'],x['setup_id']))
    capital=START;peak=START;maxdd=Decimal(0)
    last_exit=None;accepted=[];decisions=[]
    for item in ordered:
        decision={'symbol':item['symbol'],'split':item['split'],'setup_id':item['setup_id'],
          'entry_time_utc':item['entry_time_utc'],'exit_time_utc':item['exit_time_utc'],
          'direction':item['direction'],'status':'','reject_reason':''}
        if spot_long_only and item['direction']=='short':
            decision['status']='rejected';decision['reject_reason']='spot_short_unavailable'
        elif len(accepted)>=max_trades:
            decision['status']='rejected';decision['reject_reason']='trade_cap'
        elif last_exit is not None and item['_entry']<last_exit:
            decision['status']='rejected';decision['reject_reason']='position_open'
        else:
            risk_budget=capital*RISK_FRACTION
            pnl=risk_budget*item['_r']
            after=capital+pnl
            if not after.is_finite() or after<0:
                raise ValueError('Model liquidation or nonfinite balance')
            peak=max(peak,after)
            dd=(peak-after)/peak
            maxdd=max(maxdd,dd)
            sequence=len(accepted)+1
            accepted.append({**item,'trade_number':sequence,
              'equity_before':capital,'risk_budget':risk_budget,'pnl':pnl,'equity_after':after,'dd_pct':dd*100})
            capital=after;last_exit=item['_exit']
            decision['status']='accepted';decision['trade_number']=sequence
        decisions.append(decision)
    summary=calc_metrics(accepted,capital,maxdd)
    summary.update({'source_candidates':len(ordered),'rejection_reasons':dict(Counter(x['reject_reason'] for x in decisions if x['reject_reason'])),
       'risk_fraction':'0.02','initial_equity_usdt':'1000',
       'trade_limit':max_trades,'scenario':'spot_long_only_paper' if spot_long_only else 'long_and_hypothetical_short_paper'})
    return accepted,decisions,summary


def ledger_csv(path:Path, accepted:list[dict])->None:
    cols=('trade_number','symbol','split','setup_id','direction','signal_time_utc','entry_time_utc',
          'exit_time_utc','exit_reason','r_multiple','equity_before','risk_budget','pnl','equity_after','dd_pct')
    with path.open('w',encoding='utf-8',newline='') as fp:
        writer=csv.DictWriter(fp,fieldnames=cols);writer.writeheader()
        for r in accepted:
            writer.writerow({k:str(r.get(k,'')) for k in cols})


def decision_csv(path:Path, decisions:list[dict])->None:
    cols=('symbol','split','setup_id','entry_time_utc','exit_time_utc','direction','status','reject_reason','trade_number')
    with path.open('w',encoding='utf-8',newline='') as fp:
        writer=csv.DictWriter(fp,fieldnames=cols);writer.writeheader();writer.writerows([{k:x.get(k,'') for k in cols} for x in decisions])


def run(input_zip:Path,out:Path)->dict:
    rows,baseline=load_frozen(input_zip)
    out.mkdir(parents=True,exist_ok=True)
    reports={}
    for mode in ('all_directions','spot_long_only'):
        accepted,decisions,m=replay(rows,spot_long_only=mode=='spot_long_only')
        reports[mode]=m
        ledger_csv(out/f'{mode}_ledger.csv',accepted)
        decision_csv(out/f'{mode}_decisions.csv',decisions)
    for split in SPLITS:
        for mode in ('all_directions','spot_long_only'):
            _,_,m=replay([r for r in rows if r['split']==split],spot_long_only=mode=='spot_long_only')
            reports[f'{split}_{mode}']=m
    # Historical 1000-trade reference is frozen data, not a new holdout.
    assert reports['all_directions']['accepted_trades']==1000
    assert reports['all_directions']['wins']==210
    manifest={
      'status':'reproducible_R_ledger_only_NOT_executable_portfolio',
      'version':VERSION,'run_id':ORIGIN_RUN,'frozen_csv_sha256':TRADES_SHA,'frozen_baseline_sha256':BASELINE_SHA,
      'baseline_split_counts':EXPECTED_SOURCE_SPLITS,'decision_sort':'entry_time_utc, symbol, setup_id; strictly no exit-time tie-breaking',
      'model':'one nonoverlapping theoretical position, risk 2% of post-exit realized cash balance; immutable net R (fees/slippage ALREADY included)',
      'strategy':baseline.get('version'),
      'report':reports,
      'not_yet_verified':['source export lacks entry/stop/notional, so true position sizing and cash margin are UNVERIFIABLE',
        'short positions are hypothetical; Spot long-only scenario is for research only',
        'exchange lot size/min notional, actual fills, available cash and margin NOT verified',
        'closed-trade drawdown excludes intratrade mark-to-market and gaps',
        'R stop bound [-1,...] applies to historical model, NOT real execution',
        'previously opened 2025+ Test and a 2026-selected historical universe are not new OOS']
    }
    (out/'p1_05_stage1_results.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    manifest['result_sha256']=sha((out/'p1_05_stage1_results.json').read_bytes())
    return manifest


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--baseline-zip',type=Path,required=True);p.add_argument('--out-dir',type=Path,required=True)
    args=p.parse_args(argv)
    report=run(args.baseline_zip,args.out_dir)
    print(json.dumps({'status':report['status'],'all_directions':report['report']['all_directions'],
      'spot_long_only':report['report']['spot_long_only']},ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())