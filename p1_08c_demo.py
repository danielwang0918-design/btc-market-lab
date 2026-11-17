#!/usr/bin/env python3
"""Offline synthetic acceptance, not strategy performance or forward evidence."""
from __future__ import annotations
import argparse
import csv
from copy import deepcopy
from datetime import datetime,timezone
import io
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile

from p1_08c_registry import Registry,FRAMES,SYMBOLS,CANDIDATE_KEYS,sha_file,sha_bytes,utc,load_json,timestamp

BASE='330c87cc62252f8c1ee52756712954d5e0dfed30'
SOURCE_SHA='75093e26e3dd7737d713037dd145edc5a425fae2b579d9558110b5afcab07305'
H=FRAMES['1h']


def save(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False);f.write('\n')


def verify_baseline(path):
    if sha_file(path)!=SOURCE_SHA:raise ValueError('P1-08B source artifact changed')
    with ZipFile(path) as z:
        m=load_json(z.read('results/p1_08b_manifest.json'))
        if m['current_commit']!=BASE or m['source_accounts']!=160 or m['source_actual_rows']!=32232 or m['raw_candles']!=717638 or len(m['frozen_source_sha256'])!=24:
            raise ValueError('Original P1-08B population/version mismatch')
        for name,digest in m['output_sha256'].items():
            if sha_bytes(z.read('results/'+name))!=digest:raise ValueError('Original output hash drift: '+name)
        for name,digest in m['code_sha256'].items():
            if sha_file(Path(__file__).parent/name)!=digest:raise ValueError('Inherited P1-08B code changed')
        accounts=list(csv.DictReader(io.StringIO(z.read('results/source_accounts.csv').decode('utf-8-sig'))))
        if len(accounts)!=160:raise ValueError('Original account count changed')
    return dict(source_artifact_sha256=SOURCE_SHA,source_commit=BASE,output_hashes_verified=len(m['output_sha256']),
        inherited_code_hashes_verified=len(m['code_sha256']),source_accounts=160,frozen_files=24,frozen_candles=717638,
        original_accounts_modified=False,financial_recomputation=False)


def synthetic_candle(symbol,op,*,zero=False):
    return dict(symbol=symbol,timeframe='1h',open_at=utc(op),close_at=utc(op+H),
        ohlcv=dict(open='100',high='102',low='99',close='101',volume='0' if zero else '10',quote_volume=None),
        source=dict(id='SYNTHETIC_FIXTURE',uri='synthetic://not-real-market-data',market_event_at=None,
                    published_at=None,unknown_reason='Synthetic source has no exchange publication time'))


def run(out_dir,*,baseline_zip=None):
    out=Path(out_dir)
    if out.exists():raise ValueError('Use a new output directory; never overwrite evidence')
    baseline=verify_baseline(baseline_zip) if baseline_zip else None
    out.mkdir(parents=True);root=Path(__file__).parent
    p=load_json((root/'P1_08C_SYNTHETIC_PROTOCOL.json').read_bytes());start=timestamp(p['study_start_at']);clock=[start-1]
    r=Registry.create(out/'synthetic_registry',p,mode='synthetic',clock=lambda:clock[0])
    registrations=[];checkpoints=[];latest={};inputs={}
    clock[0]=start+H+1000
    for symbol in SYMBOLS:
        evidence=r.ingest(synthetic_candle(symbol,start));eid=evidence['record_sha256'];inputs[symbol]=eid
        pending=symbol=='BTCUSDT'
        c=dict(market_event_key='synthetic-fixture-'+symbol,symbol=symbol,timeframe='1h',formed_at=utc(start+H),
            confirmed_at=None if pending else utc(start+H),state='pending' if pending else 'confirmed',
            stance='watch' if pending else 'long',arm='all',observation_start_at=utc(start+2*H),evidence_ids=[eid],
            features=dict(last_close=dict(value=101,known_at=utc(clock[0]),evidence_ids=[eid],unknown_reason=None),
                rvol20=dict(value=None,known_at=None,evidence_ids=[],unknown_reason='Synthetic fixture has insufficient 21-bar history')),
            quality=dict(data_gaps=[],delays=[],unknown_fields=['rvol20']),previous_candidate_sha256=None)
        record=r.register(c);registrations.append(record);checkpoints.append(r.checkpoint());latest[symbol]=record
    clock[0]=start+2*H+1000;e2=r.ingest(synthetic_candle('BTCUSDT',start+H))
    c={k:v for k,v in latest['BTCUSDT']['payload'].items() if k in CANDIDATE_KEYS}
    c=deepcopy(c);c.update(confirmed_at=utc(start+2*H),state='confirmed',stance='long',observation_start_at=utc(start+3*H),
        previous_candidate_sha256=latest['BTCUSDT']['record_sha256'],evidence_ids=[inputs['BTCUSDT'],e2['record_sha256']])
    c['features']['last_close']=dict(value=101,known_at=utc(clock[0]),evidence_ids=[e2['record_sha256']],unknown_reason=None)
    record=r.register(c);registrations.append(record);checkpoints.append(r.checkpoint());latest['BTCUSDT']=record
    observation_ids={s:[] for s in SYMBOLS}
    for hour in range(2,9):
        clock[0]=start+(hour+1)*H+1
        for symbol in SYMBOLS:
            first=3 if symbol=='BTCUSDT' else 2
            if not first<=hour<first+6 or (symbol=='ZECUSDT' and hour==4):continue
            e=r.ingest(synthetic_candle(symbol,start+hour*H,zero=(symbol=='DOGEUSDT' and hour==4)))
            observation_ids[symbol].append(e['record_sha256'])
    labels=[]
    for symbol,candidate in latest.items():
        incomplete=symbol in ('ZECUSDT','DOGEUSDT')
        labels.append(r.label(dict(candidate_sha256=candidate['record_sha256'],horizon_hours=6,
            values=dict(diagnostic_end_close=None if incomplete else 101,diagnostic_max_high=None if incomplete else 102),
            status='incomplete' if incomplete else 'complete',evidence_ids=observation_ids[symbol],intrabar_order='unknown',
            limitations=['synthetic missing/zero observation'] if incomplete else ['Synthetic labels; no transaction, PnL or execution inference'])))
    for cp in checkpoints:r.verify(anchor=cp)
    save(out/'registration_checkpoints.json',checkpoints);save(out/'final_checkpoint.json',r.checkpoint())
    save(out/'registry_verification.json',r.verify(anchor=checkpoints[0]));save(out/'sample_structure_audit.json',r.audit())
    with (out/'candidate_versions.csv').open('x',newline='',encoding='utf-8') as f:
        fields=['event_id','revision','symbol','state','stance','formed_at','confirmed_at','data_available_at','registered_at','observation_start_at','mode','research_only']
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for record in registrations:w.writerow({k:record['payload'][k] for k in fields})
    with (out/'posthoc_labels.csv').open('x',newline='',encoding='utf-8') as f:
        fields=['candidate_sha256','horizon_hours','observation_start_at','observation_end_at','label_completed_at','status','intrabar_order','calculation_verified']
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for record in labels:w.writerow({k:record['payload'][k] for k in fields})
    audit=r.audit()
    manifest=dict(version='p1-08c-synthetic-acceptance-v1',base_commit=BASE,
        current_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        synthetic_only=True,real_forward_candidates=0,qualified_strategy_count=0,active_hypotheses=0,
        unique_events=audit['unique_events'],candidate_versions=len(registrations),complete_labels=6,incomplete_labels=2,
        new_backtests=0,financial_changes=0,external_timestamp_attestation_verified=False,
        source_baseline=baseline,protocol_sha256=sha_file(root/'P1_08C_SYNTHETIC_PROTOCOL.json'),
        plan_sha256=sha_file(root/'P1_08C_PLAN.json'),
        code_sha256={name:sha_file(root/name) for name in ('p1_08c_registry.py','p1_08c_demo.py','p1_08c_public_probe.py')},
        output_sha256={str(f.relative_to(out)):sha_file(f) for f in sorted(out.rglob('*')) if f.is_file()})
    save(out/'p1_08c_manifest.json',manifest);return manifest


def cli(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out-dir',type=Path,required=True);p.add_argument('--baseline-zip',type=Path)
    a=p.parse_args(argv);m=run(a.out_dir,baseline_zip=a.baseline_zip)
    print(json.dumps({k:v for k,v in m.items() if k not in ('output_sha256','code_sha256')},indent=2));return 0


if __name__=='__main__':raise SystemExit(cli())
