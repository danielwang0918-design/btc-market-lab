#!/usr/bin/env python3
"""Independent download verification, not WORM or third-party timestamping.

The caller fetches GitHub metadata over its independent HTTPS connection.
Metadata supplied by an adversary is not itself a trusted attestation.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path,PurePosixPath
from zipfile import ZipFile

from p1_08d_receiver import Receiver,read,write,c

REPO_ID=1419214883
FILES={'receiver_snapshot.zip','receiver_checkpoint.json','receiver_verification.json','projections.json','capture_context.json'}


def publish_bundle(receiver,out,*,context):
    out=Path(out)
    if out.exists():raise ValueError('Never overwrite a capture bundle')
    out.mkdir(parents=True)
    receiver.snapshot(out/'receiver_snapshot.zip')
    write(out/'receiver_checkpoint.json',receiver.checkpoint())
    write(out/'receiver_verification.json',receiver.verify())
    write(out/'projections.json',receiver.projections())
    write(out/'capture_context.json',dict(context,source_mode=receiver.contract['mode'],exchange='Binance',market_type='Spot',
        collector_code_sha256=c.sha_file(Path(__file__).with_name('p1_08d_receiver.py')),
        candidate_versions=0,active_trading_hypotheses=0,external_timestamp_attestation_verified=False))
    manifest=dict(version='p1-08d-capture-bundle-v1',files_sha256={name:c.sha_file(out/name) for name in sorted(FILES)},
        no_strategy_or_execution=True,external_timestamp_attestation_verified=False)
    write(out/'bundle_manifest.json',manifest);return manifest


def verify_retained_bundle(artifact,metadata,out,*,expected_commit,expected_run_id):
    artifact,out=Path(artifact),Path(out)
    if out.exists():raise ValueError('Use a new independent verification directory')
    run=metadata.get('workflow_run',{})
    if (metadata.get('expired') is not False or metadata.get('size_in_bytes')!=artifact.stat().st_size
            or metadata.get('digest')!='sha256:'+c.sha_file(artifact)
            or run.get('id')!=expected_run_id or run.get('head_sha')!=expected_commit
            or run.get('repository_id')!=REPO_ID or run.get('head_repository_id')!=REPO_ID):
        raise ValueError('GitHub artifact digest/provenance/expiry mismatch')
    for key in ('created_at','updated_at'):c.timestamp(metadata[key])
    out.mkdir(parents=True);payload=out/'downloaded_capture';payload.mkdir()
    with ZipFile(artifact) as z:
        names=z.namelist()
        if len(names)!=len(set(names)) or set(names)!=FILES|{'bundle_manifest.json'}:raise ValueError('Unexpected remote capture inventory')
        m=c.load_json(z.read('bundle_manifest.json'))
        if set(m['files_sha256'])!=FILES:raise ValueError('Capture manifest inventory changed')
        for name in names:
            info=z.getinfo(name)
            if info.is_dir() or ((info.external_attr>>16)&0o170000)==0o120000:raise ValueError('Remote capture cannot contain links/directories')
            raw=z.read(name)
            if name in FILES and c.sha_bytes(raw)!=m['files_sha256'][name]:raise ValueError('Capture payload hash changed')
            with (payload/name).open('xb') as f:f.write(raw)
    checkpoint=read(payload/'receiver_checkpoint.json');context=read(payload/'capture_context.json')
    if context['commit']!=expected_commit or str(context['run_id'])!=str(expected_run_id):raise ValueError('Inner and platform capture version disagree')
    receiver=Receiver.restore(payload/'receiver_snapshot.zip',out/'verified_receiver',expected_sha256=m['files_sha256']['receiver_snapshot.zip'],
        anchor=checkpoint,expected_mode=context['source_mode'])
    verification=receiver.verify(anchor=checkpoint)
    if receiver.contract['receiver_code_sha256']!=context['collector_code_sha256'] or context['collector_code_sha256']!=c.sha_file(Path(__file__).with_name('p1_08d_receiver.py')):
        raise ValueError('Capture collector version not the independently checked code')
    result=dict(version='p1-08d-independent-github-receipt-v1',mode=context['source_mode'],
        artifact_id=metadata['id'],workflow_run_id=expected_run_id,workflow_commit=expected_commit,
        github_artifact_created_at=metadata['created_at'],github_artifact_updated_at=metadata['updated_at'],
        independent_verifier_observed_at=c.utc(c.system_ms()),platform_artifact_digest=metadata['digest'],
        platform_artifact_digest_verified=True,receiver_checkpoint_verified=True,receiver_checkpoint=checkpoint,
        raw_receipts=verification['raw_receipts'],normalized_evidence=verification['normalized_evidence'],candidate_versions=0,
        artifact_metadata_source='GitHub API supplied by independent verification job; metadata is not cryptographically signed',
        retention_limits='Admin-deletable and expires; same repository authority can change future code. Not WORM or legal timestamping.',
        source_authentication_limits='Pinned receiver uses public HTTPS; no exchange-signed payload or independent market-event timestamp',
        external_timestamp_attestation_verified=False,independent_forward_strategy_evidence=0)
    write(out/'external_retention_receipt.json',result);write(out/'github_artifact_metadata.json',metadata)
    return result


def cli(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('bundle');b.add_argument('--root',type=Path,required=True);b.add_argument('--out',type=Path,required=True)
    v=sub.add_parser('verify');v.add_argument('--artifact',type=Path,required=True);v.add_argument('--metadata',type=Path,required=True)
    v.add_argument('--out',type=Path,required=True);v.add_argument('--commit',required=True);v.add_argument('--run-id',type=int,required=True)
    a=p.parse_args(argv)
    if a.command=='bundle':r=publish_bundle(Receiver(a.root),a.out,context={k:os.environ.get(v,'local-not-platform-attested') for k,v in [('commit','GITHUB_SHA'),('run_id','GITHUB_RUN_ID'),('run_attempt','GITHUB_RUN_ATTEMPT')]})
    else:r=verify_retained_bundle(a.artifact,read(a.metadata),a.out,expected_commit=a.commit,expected_run_id=a.run_id)
    print(json.dumps(r,indent=2));return 0


if __name__=='__main__':raise SystemExit(cli())
