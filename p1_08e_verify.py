#!/usr/bin/env python3
"""Read-only independent archive/real-drill verification, no collection or writes to GitHub."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path,PurePosixPath
from zipfile import ZipFile

from p1_08d_receiver import Receiver,read,write,c
from p1_08d_retention import verify_retained_bundle
from p1_08e_ops import Supervisor
from p1_08e_drill import validate_death,outage_summary
from p1_08e_monitor import daily_report


def verify_archive(reference,out):
    out=Path(out)
    if out.exists():raise ValueError('Independent verification needs a new directory')
    out.mkdir(parents=True)
    o=Supervisor.restore(out/'supervisor',remote=reference['remote'],branch=reference['branch'],expected_head=reference['head'],mode='real_public',root_pin=reference['root_pin'])
    o.archive.git(['merge-base','--is-ancestor',reference['root_pin'],reference['head']])
    o.archive.git(['fsck','--full','--no-reflogs'])
    cat=o.archive.catalog(reference['head']);v=o.receiver().verify(anchor=cat['receiver_checkpoint'])
    config=read(o.root/'state/config.json')
    if cat['retention_until']!=config['retention_until'] or c.timestamp(cat['retention_until'])<c.add_months(c.timestamp(config['created_at']),24):raise ValueError('Retention policy does not cover 24 months')
    if reference['receiver_checkpoint']!=v['checkpoint']:raise ValueError('Release checkpoint differs from current archive')
    events=[read(p) for p in sorted((o.root/'state/events').glob('*.json'))]
    if [e['generation'] for e in events]!=list(range(1,cat['generation']+1)):raise ValueError('Operational record sequence drift')
    recoveries=[e for e in events if e['kind']=='controlled_recovery']
    reports=[e['evidence'] for e in events if e['kind']=='real_controlled_stop_report']
    if not recoveries or not reports:raise ValueError('No real controlled-stop evidence; synthetic tests cannot satisfy this gate')
    death=recoveries[-1]['death_evidence'];validate_death(death)
    staged=o.archive.catalog(recoveries[-1]['remote_readback']['remote_head'])
    if staged['lease']['pid']!=death['pid'] or staged['lease']['host']!=death['host']:raise ValueError('Observed stopped PID does not match archived lease')
    matching=[p for p in (o.root/'state/incidents').glob('*.zip') if c.sha_file(p)==death['partial_state_sha256']]
    if not matching:raise ValueError('Missing preserved interrupted state')
    genuine_partial_receipt=False
    with ZipFile(matching[0]) as z:
        for name in z.namelist():
            p=PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts:raise ValueError('Unsafe interrupted evidence path')
            if not name.endswith('.receipt.json'):continue
            r=c.load_json(z.read(name))
            if r.get('endpoint')=='klines' and r['http_status']==200 and r['raw_sha256']==death['real_receipt_marker']['raw_sha256']:
                body=z.read(str(p.parent/r['raw_file']))
                if c.sha_bytes(body)!=r['raw_sha256'] or c.timestamp(r['system_received_at'])>c.timestamp(death['stopped_at']):raise ValueError('Interrupted raw receipt/time mismatch')
                if not r['url'].startswith('https://data-api.binance.vision/api/v3/klines?'):raise ValueError('Wrong real source')
                genuine_partial_receipt=True
    if not genuine_partial_receipt:raise ValueError('No actual public kline response preserved before process interruption')
    report=reports[-1]
    if report['mode']!='real_public' or report['seven_day_status']!='not_started' or report['level2_pass']:raise ValueError('Drill misrepresented as pilot completion')
    stop=c.timestamp(report['outage']['stopped_at_UTC']);end=c.timestamp(report['outage']['recovered_at_UTC'])
    if outage_summary(stop,end)!=report['outage']:raise ValueError('Outage calendar/gap counts changed')
    before=o.archive.catalog(report['real_initial_collection']['published_head'])
    for name,digest in before['files_sha256'].items():
        if cat['files_sha256'].get(name)!=digest:raise ValueError('Original hash/time/evidence-ID bytes changed')
    baseline=o.root/'state/frozen-baseline'
    base_check=verify_retained_bundle(baseline/'p1-08d-capture.zip',read(baseline/'p1-08d-metadata.json'),out/'independent-D-baseline',
        expected_commit='c3b27a6aaf33b8291b9b3c7559521433c3f44b99',expected_run_id=44023196507)
    if c.sha_file(baseline/'p1-08d-capture.zip')!='b5178061c7dcdef7261fc31f2b39f33b6640dceb0cb94656df414b022fc8b9c7':raise ValueError('Frozen D baseline differs')
    day=datetime.now(timezone.utc).strftime('%Y-%m-%d');daily=daily_report(o,day);write(out/'daily-report.json',daily)
    if daily['seven_day']['status']!='not_started':raise ValueError('No approved persistent seven-day host is active in this acceptance')
    local_ref='refs/heads/'+reference['branch'];o.archive.git(['update-ref',local_ref,reference['head']])
    o.archive.git(['bundle','create',str((out/'archive-history.bundle').resolve()),local_ref])
    o.archive.git(['bundle','verify',str((out/'archive-history.bundle').resolve())])
    result=dict(version='p1-08e-independent-acceptance-v1',archive_head=reference['head'],root_pin=reference['root_pin'],
        archive_files_verified=len(cat['files_sha256']),original_pre_stop_files_byte_identical=len(before['files_sha256']),
        receiver_checkpoint=v['checkpoint'],real_raw_receipts=v['raw_receipts'],normalized_evidence=v['normalized_evidence'],
        real_process_stop_returncode=death['returncode'],real_outage_ms=report['outage']['actual_unavailable_ms'],
        natural_market_closes_during_outage=report['outage']['market_closes_during_outage'],
        canonical_parent_chain_verified=True,remote_restore_and_partial_evidence_verified=True,
        baseline_D_raw_receipts=base_check['raw_receipts'],baseline_D_normalized_evidence=base_check['normalized_evidence'],
        baseline_D_unchanged=True,portable_git_bundle_verified=True,
        retention_policy_months=27,elapsed_24month_retention_proven=False,
        seven_day_status='not_started',real_seven_days_completed=False,level1_engineering_scope_pass=True,level2_pass=False,
        active_trading_hypotheses=0,external_timestamp_attestation_verified=False)
    write(out/'acceptance.json',result)
    inventory={str(p.relative_to(out)):c.sha_file(p) for p in sorted(out.rglob('*')) if p.is_file() and 'archive-cache.git' not in p.parts}
    write(out/'verification-files.json',inventory);return result


def cli(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--reference',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args(argv);print(json.dumps(verify_archive(read(a.reference),a.out),indent=2));return 0


if __name__=='__main__':raise SystemExit(cli())
