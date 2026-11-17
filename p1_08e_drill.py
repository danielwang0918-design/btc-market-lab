#!/usr/bin/env python3
"""Actual public-data reception and controlled SIGKILL; not a seven-day trial.

Only explicitly initialized isolated P1-08E state is touched. Real clocks and
unmodified D receiver, no mocked prices or injected transport in this script.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from zipfile import ZipFile,ZIP_DEFLATED

from p1_08d_receiver import Receiver,read,write,c
from p1_08e_ops import Supervisor
from p1_08e_monitor import expected_closes,daily_report


def outage_summary(start,end):
    if end<start:raise ValueError('Outage clock reversed')
    return dict(stopped_at_UTC=c.utc(start),recovered_at_UTC=c.utc(end),actual_unavailable_ms=end-start,
        market_closes_during_outage={f:[c.utc(t) for t in expected_closes(start,end,f)] for f in ('1h','4h','1d')},
        missed_scheduled_poll_nodes=[c.utc(t+120000) for t in expected_closes(start-120000,end-120000,'1h')],
        no_hourly_gap_is_fabricated=True)


def validate_death(proof):
    if proof.get('process_dead') is not True or proof.get('returncode')!=-signal.SIGKILL or type(proof.get('pid')) is not int or proof['pid']<=0:
        raise ValueError('Real OS SIGKILL exit observation required')


def run_drill(root,*,remote,branch,baseline_files=None):
    root=Path(root)
    if root.exists():raise ValueError('Use a new explicit isolated engineering directory')
    root.mkdir(parents=True);started=c.system_ms()
    if baseline_files and {'p1-08d-capture.zip','p1-08d-metadata.json'}<=set(baseline_files):
        from p1_08d_retention import verify_retained_bundle
        verify_retained_bundle(baseline_files['p1-08d-capture.zip'],read(baseline_files['p1-08d-metadata.json']),root/'frozen-D-baseline-check',
            expected_commit='c3b27a6aaf33b8291b9b3c7559521433c3f44b99',expected_run_id=44023196507)
    seed=Receiver.create(root/'new-engineering-source',mode='real_public')
    seed.snapshot(root/'new-engineering-seed.zip')
    live=Supervisor.create(root/'before-stop',remote=remote,branch=branch,snapshot=root/'new-engineering-seed.zip',
        snapshot_sha256=c.sha_file(root/'new-engineering-seed.zip'),checkpoint=seed.checkpoint(),mode='real_public',baseline_files=baseline_files)
    first=live.cycle(expected_head=live.head())
    if first['status']!='ok':raise ValueError('First actual data receipt did not pass; do not manufacture a drill success')
    before_head=live.head();before_cat=live.archive.catalog(before_head);before_files=dict(before_cat['files_sha256'])
    log=root/'actual-child.log'
    with log.open('wb') as output:
        child=subprocess.Popen([sys.executable,str(Path(__file__).with_name('p1_08e_ops.py')),'cycle','--root',str(live.root),'--expected-head',before_head],
            stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        deadline=time.monotonic()+90;marker=None
        while time.monotonic()<deadline and child.poll() is None:
            for f in (live.root/'state/receiver').glob('.pending-*/*.receipt.json'):
                try:
                    meta=read(f)
                    if meta['endpoint']=='klines' and meta['http_status']==200 and c.sha_file(f.parent/meta['raw_file'])==meta['raw_sha256']:
                        marker=dict(file=str(f),raw_sha256=meta['raw_sha256'],wire_received_at=meta['system_received_at']);break
                except (ValueError,OSError,KeyError):pass
            if marker:break
            time.sleep(0.02)
        if marker is None:
            if child.poll() is None:os.killpg(child.pid,signal.SIGKILL)
            child.wait(timeout=10);raise ValueError('No completed genuine kline receipt was observed before stop; drill incomplete')
        stop=c.system_ms();os.killpg(child.pid,signal.SIGKILL);returncode=child.wait(timeout=10);exited=c.system_ms()
    current=live.archive.head();cat=live.archive.catalog(current)
    if cat['lease'] is None or cat['lease']['pid']!=child.pid:raise ValueError('Child had no live collection lease at interruption')
    partial=root/'interrupted-receiver-state.zip'
    with ZipFile(partial,'x',compression=ZIP_DEFLATED) as z:
        for p in sorted((live.root/'state/receiver').rglob('*')):
            if p.is_file() and not p.is_symlink():z.write(p,'receiver/'+str(p.relative_to(live.root/'state/receiver')))
        for p in sorted((live.root/'diagnostics').glob('*.json')):z.write(p,'ops_diagnostics/'+p.name)
    death=dict(process_dead=child.poll() is not None,pid=child.pid,host=socket.gethostname(),returncode=returncode,
        stopped_at=c.utc(stop),exit_observed_at=c.utc(exited),reason='Controlled real SIGKILL after a real public kline response',
        partial_state_zip=str(partial),partial_state_sha256=c.sha_file(partial),real_receipt_marker=marker)
    validate_death(death);write(root/'process-death-evidence.json',death)
    recovered=Supervisor.restore(root/'restored-from-remote',remote=remote,branch=branch,expected_head=current,mode='real_public',root_pin=live.local['root_pin'])
    recovered.recover_stopped(expected_head=current,death_evidence=death)
    resumed=recovered.cycle(expected_head=recovered.head());recovery_end=c.system_ms()
    if resumed['status']!='ok':raise ValueError('Post-interruption live data collection did not pass')
    final_head=recovered.head();final_cat=recovered.archive.catalog(final_head)
    for name,digest in before_files.items():
        if final_cat['files_sha256'].get(name)!=digest:raise ValueError('Original raw/time/evidence IDs changed during recovery')
    status=recovered.status()
    if status['seven_day_status']!='not_started':raise ValueError('Engineering drill must not activate the seven-day trial')
    day=__import__('datetime').datetime.fromtimestamp(recovery_end/1000,__import__('datetime').timezone.utc).strftime('%Y-%m-%d')
    daily=daily_report(recovered,day);write(root/'daily-engineering-report.json',daily)
    report=dict(version='p1-08e-real-controlled-stop-v1',mode='real_public',started_at_UTC=c.utc(started),completed_at_UTC=c.utc(recovery_end),
        real_initial_collection=first,real_resumed_collection=resumed,process_death=death,outage=outage_summary(stop,recovery_end),
        original_files_byte_identical=len(before_files),source_and_checkpoint_verified=True,remote_data_head=final_head,
        seven_day_status='not_started',level2_pass=False,active_trading_hypotheses=0,external_timestamp_attestation_verified=False)
    note_head=recovered.record_note(expected_head=final_head,kind='real_controlled_stop_report',evidence=report)
    daily_head,stored_daily=recovered.record_daily_report(expected_head=note_head,day=day)
    report['evidence_record_head']=note_head;report['daily_record_head']=daily_head
    write(root/'real-drill-report.json',report)
    reference=dict(remote=remote,branch=branch,head=daily_head,root_pin=live.local['root_pin'],mode='real_public',
        receiver_checkpoint=recovered.receiver().checkpoint(),plan_sha256=c.sha_file(Path(__file__).with_name('P1_08E_PLAN.json')),
        trial_status='not_started',external_timestamp_attestation_verified=False)
    write(root/'archive-reference.json',reference);return report,reference


def cli(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--remote',required=True);p.add_argument('--branch',required=True)
    p.add_argument('--baseline-capture',type=Path);p.add_argument('--baseline-metadata',type=Path)
    a=p.parse_args(argv);files={}
    if a.baseline_capture:files['p1-08d-capture.zip']=a.baseline_capture
    if a.baseline_metadata:files['p1-08d-metadata.json']=a.baseline_metadata
    report,reference=run_drill(a.root,remote=a.remote,branch=a.branch,baseline_files=files)
    print(json.dumps(dict(archive_reference=reference,seven_day_status=report['seven_day_status'],outage=report['outage']),indent=2));return 0


if __name__=='__main__':raise SystemExit(cli())
