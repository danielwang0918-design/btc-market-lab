#!/usr/bin/env python3
"""Honest UTC calendar denominators and operations reporting; no returns."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
from p1_08c_registry import FRAMES,utc,timestamp,system_ms

H=FRAMES['1h'];DAY=24*H


def expected_closes(start,end,frame):
    step=FRAMES[frame]
    return list(range((start//step+1)*step,(end//step)*step+1,step)) if end>start else []


def outage_closes(start,end,frame):return expected_closes(start,end,frame)


def seven_day_status(start,asof):
    start=timestamp(start) if isinstance(start,str) else start
    elapsed=max(0,asof-start) if start is not None else 0
    return dict(status='not_started' if start is None else ('in_progress' if elapsed<7*DAY else 'awaiting_full_evidence_review'),
        elapsed_ms=elapsed,elapsed_168h=start is not None and elapsed>=7*DAY,level2_pass=False,
        reason='Real duration is necessary; seven daily records, backups and gap/recovery review are also required')


def feed_report(rows,symbol,frame,start,end,asof):
    expected=expected_closes(start,min(end,asof),frame);expected_set=set(expected);due={t for t in expected if t+300000<=asof}
    confirmed={};duplicates=revisions=0
    for r in rows:
        if r.get('symbol')!=symbol or r.get('timeframe')!=frame or r.get('close_exclusive_ms') not in expected_set:continue
        received=timestamp(r['wire_received_at'])
        if received>asof:continue
        if r['status']=='new':confirmed.setdefault(r['close_exclusive_ms'],received)
        if r['status']=='duplicate':duplicates+=1
        if r['status']=='source_revision':revisions+=1
    ontime={cl for cl,received in confirmed.items() if cl<=received<=cl+300000}
    late={cl for cl,received in confirmed.items() if received>cl+300000}
    return dict(symbol=symbol,timeframe=frame,expected=len(expected),due=len(due),confirmed=len(confirmed),
        missing_due=len(due-set(confirmed)),pending_grace=len(expected_set-due-set(confirmed)),late=len(late),
        duplicates=duplicates,revisions=revisions,on_time_success_rate=len(ontime&due)/len(due) if due else None,
        receipt_success_rate=len(set(confirmed)&due)/len(due) if due else None,
        missing_close_times_UTC=[utc(t) for t in sorted(due-set(confirmed))])


def daily_report(supervisor,day,*,asof=None):
    now=supervisor.clock() if asof is None else asof
    dt=datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=timezone.utc);begin=int(dt.timestamp()*1000)
    if begin>now:raise ValueError('No invented report for a future UTC day')
    head=supervisor.head();cat=supervisor.archive.catalog(head);config=__import__('p1_08d_receiver').read(supervisor.root/'state/config.json')
    start=timestamp(cat['trial_started_at']) if cat['trial_started_at'] else timestamp(config['created_at'])
    start=max(start,begin);end=min(begin+DAY,now)
    receiver=supervisor.receiver();rows=receiver.processed_rows()
    feeds=[feed_report(rows,s,f,start,end,now) for s in ('BTCUSDT','ETHUSDT') for f in ('1h','4h','1d')]
    alerts=[]
    for f in feeds:
        if f['missing_due']:alerts.append(dict(kind='expected_close_missing',symbol=f['symbol'],timeframe=f['timeframe'],count=f['missing_due']))
        if f['late']:alerts.append(dict(kind='collection_lag',symbol=f['symbol'],timeframe=f['timeframe'],count=f['late']))
        if f['revisions']:alerts.append(dict(kind='source_revision',symbol=f['symbol'],timeframe=f['timeframe'],count=f['revisions']))
    if now-timestamp(cat['last_backup_verified_at'])>7200000:alerts.append(dict(kind='backup_stale'))
    if timestamp(cat['retention_until'])-now<30*DAY:alerts.append(dict(kind='retention_expiry_warning'))
    if cat['lease'] is not None:alerts.append(dict(kind='lease_held',owner=cat['lease']))
    if cat['status'] not in ('ready','lease_held'):alerts.append(dict(kind='operation_blocked',reason=cat['status']))
    if now<timestamp(config['created_at']):alerts.append(dict(kind='clock_rollback'))
    return dict(version='p1-08e-daily-ops-v1',UTC_date=day,asof_UTC=utc(now),mode=config['mode'],
        scope='pilot' if cat['trial_started_at'] else 'engineering_only_not_a_seven_day_trial',
        window_start_UTC=utc(start),window_end_UTC=utc(end),feeds=feeds,alerts=alerts,
        latest_remote_head=head,latest_valid_checkpoint=receiver.checkpoint(),backup_last_verified_UTC=cat['last_backup_verified_at'],
        retention_until=cat['retention_until'],continue_conditions_met=not alerts and cat['status']=='ready',
        seven_day=seven_day_status(cat['trial_started_at'],now),active_trading_hypotheses=0,
        external_timestamp_attestation_verified=False,nested_frames_are_independent_samples=False)


def cli(argv=None):
    from p1_08e_ops import Supervisor
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--day',required=True);p.add_argument('--out',type=Path)
    a=p.parse_args(argv);r=daily_report(Supervisor(a.root),a.day);text=json.dumps(r,indent=2)+'\n'
    if a.out:
        with a.out.open('x') as f:f.write(text)
    else:print(text)
    return 0


if __name__=='__main__':raise SystemExit(cli())
