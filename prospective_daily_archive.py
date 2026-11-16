#!/usr/bin/env python3
"""P0-08 daily *data-only* append archive for eight preregistered Spot pairs.

Persists a single immutable frozen seed ZIP plus a dated directory of raw K-line
CSV append bytes and a chained checkpoint. Data-only: NO simulation or orders.
Every daily run reconstructs from the archived seed and every prior daily delta;
all values are verified before any new files are staged for a Git commit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZipFile, ZIP_DEFLATED

import prospective_validation as gate
import prospective_candle_append as capture_mod

ARCHIVE_VERSION = 'p0-daily-data-archive-v1'
SEED_NAME = 'frozen_history_v1.zip'
GENESIS_NAME = 'genesis.json'
LAST_NAME = 'latest.json'
DAY_DIR = 'days'
FIXED_SOURCE_RUN = 43820018876
GENESIS_GITHUB_RUN = 43845519180
GENESIS_RECORD_SHA256 = 'f96c42b9e1d3dc1224cdf01d0f05ec54be90a4e6cc60e9d535770be9321249f0'
VALID_PROTOCOL_SHA256 = gate.LOCK_SHA256


def sha(path: Path) -> str:
    return gate.sha_file(path)


def save(path: Path, obj: dict[str, Any]) -> None:
    gate.dump_json(path, obj)


def sha_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_day(day: str) -> str:
    try:
        dt=datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError('Day must be exact YYYY-MM-DD UTC') from exc
    if dt.strftime('%Y-%m-%d') != day:
        raise ValueError('Noncanonical day label')
    ms=int(dt.timestamp()*1000)
    if not gate.parse_utc(gate.START) <= ms <= gate.parse_utc(gate.END):
        raise ValueError('Day outside preregistered observation window')
    return day


def _seed_map(seed: Path, lock: dict[str, Any]) -> dict[str, bytes]:
    """Require exactly 24 flat CSV entries with frozen byte hashes, no zip-slip."""
    if not seed.is_file():
        raise ValueError('Missing immutable frozen history seed')
    expected=set(lock['frozen_data_prefixes'])
    with ZipFile(seed) as archive:
        names=archive.namelist()
        if len(names)!=len(expected) or set(names)!=expected or len(names)!=len(set(names)):
            raise ValueError('Seed ZIP has missing, surplus, nested, or duplicate CSV entries')
        result={}
        for name in sorted(expected):
            info=archive.getinfo(name)
            if info.is_dir() or info.file_size > 20_000_000:
                raise ValueError('Unexpected seed ZIP entry')
            content=archive.read(name)
            meta=lock['frozen_data_prefixes'][name]
            if len(content)!=meta['bytes'] or sha_bytes(content)!=meta['sha256']:
                raise ValueError(f'Seed immutable frozen bytes mismatch: {name}')
            result[name]=content
    return result


def _archive_header(archive_dir: Path, lock: dict[str, Any]) -> dict[str,Any]:
    p=archive_dir/'archive_contract.json'
    if not p.is_file():
        raise ValueError('Archive missing immutable contract')
    meta=gate.read_json(p)
    if (meta.get('version')!=ARCHIVE_VERSION or meta.get('protocol_sha256')!=VALID_PROTOCOL_SHA256
        or meta.get('baseline_run_id')!=FIXED_SOURCE_RUN
        or meta.get('genesis_run_id')!=GENESIS_GITHUB_RUN
        or meta.get('genesis_record_sha256')!=GENESIS_RECORD_SHA256
        or meta.get('seed_sha256')!=sha(archive_dir/SEED_NAME)):
        raise ValueError('Archive contract or frozen seed has drifted')
    if meta.get('symbols')!=lock['symbols']:
        raise ValueError('Archive cannot change preregistered universe')
    return meta


def bootstrap(archive_dir: Path, seed_source: Path, genesis_path: Path, lock: dict[str,Any]) -> dict[str,Any]:
    """First-time archive seed. Never overwrite any existing archive content."""
    if archive_dir.exists() and any(archive_dir.iterdir()):
        raise ValueError('Refusing to reinitialize an existing prospective archive')
    _seed_map(seed_source, lock)
    genesis=gate.read_json(genesis_path)
    gate.verify_record(genesis,lock)
    if (genesis['record_sha256']!=GENESIS_RECORD_SHA256 or
            genesis['result'].get('status')!='preregistered_not_started' or
            gate.parse_utc(genesis['asof_utc'])>=gate.parse_utc(gate.START)):
        raise ValueError('Genesis is not the original pre-start notarized record')
    for name,meta in lock['frozen_data_prefixes'].items():
        item=genesis['files'][name]
        if item.get('full_bytes')!=meta['bytes'] or item.get('full_sha256')!=meta['sha256']:
            raise ValueError('Genesis frozen file digest mismatch')
    archive_dir.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(seed_source,archive_dir/SEED_NAME)
    save(archive_dir/GENESIS_NAME,genesis)
    save(archive_dir/LAST_NAME,genesis)
    contract={'version':ARCHIVE_VERSION,'protocol_sha256':VALID_PROTOCOL_SHA256,
              'symbols':lock['symbols'],'baseline_run_id':FIXED_SOURCE_RUN,
              'genesis_run_id':GENESIS_GITHUB_RUN,'genesis_record_sha256':GENESIS_RECORD_SHA256,
              'seed_sha256':sha(archive_dir/SEED_NAME),
              'data_timeframes':['1d','4h','1h'],
              'strategy_results_in_archive':False,'real_order_placement':False}
    save(archive_dir/'archive_contract.json',contract)
    (archive_dir/DAY_DIR).mkdir(exist_ok=True)
    return {'status':'bootstrapped','seed_sha256':contract['seed_sha256'],
            'genesis_record_sha256':GENESIS_RECORD_SHA256,'archived_days':0}


def reconstruct(archive_dir: Path, out_dir: Path, lock: dict[str,Any]) -> dict[str,Any]:
    """Verify every archived day against chained checkpoints; rebuild exact CSV files."""
    _archive_header(archive_dir,lock)
    genesis=gate.read_json(archive_dir/GENESIS_NAME)
    gate.verify_record(genesis,lock)
    if genesis['record_sha256']!=GENESIS_RECORD_SHA256:
        raise ValueError('Archive Genesis record replaced')
    contents=_seed_map(archive_dir/SEED_NAME,lock)
    latest=genesis
    dates=[]
    days_path=archive_dir/DAY_DIR
    if not days_path.is_dir():
        raise ValueError('Missing archived days directory')
    entries=sorted(days_path.iterdir())
    for day_path in entries:
        day=_safe_day(day_path.name)
        if not day_path.is_dir() or day in dates:
            raise ValueError('Archive contains unexpected files or duplicate day')
        checkpoint=gate.read_json(day_path/'checkpoint.json')
        gate.verify_record(checkpoint,lock)
        if checkpoint['previous_record_sha256']!=latest['record_sha256']:
            raise ValueError('Broken hash-chain or altered checkpoint chronology')
        if gate.utc(gate.parse_utc(day+'T00:00:00+00:00'))!=checkpoint['asof_utc']:
            raise ValueError('Archive date and checkpoint UTC do not agree')
        if gate.parse_utc(checkpoint['asof_utc'])<=gate.parse_utc(latest['asof_utc']):
            raise ValueError('Checkpoint times must increase')
        if checkpoint.get('result',{}).get('status')!='daily_data_archived_not_scored':
            raise ValueError('Archive cannot misstate unscored bars as scored trades')
        delta_dir=day_path/'delta'
        expected=set(lock['frozen_data_prefixes'])
        if not delta_dir.is_dir() or set(x.name for x in delta_dir.iterdir())!=expected:
            raise ValueError('Archived delta directory must contain exactly 24 entries')
        for name in sorted(expected):
            delta_path=delta_dir/name
            if not delta_path.is_file():
                raise ValueError('Delta is not a regular file')
            contents[name]+=delta_path.read_bytes()
            info=checkpoint['files'][name]
            if info['full_bytes']!=len(contents[name]) or info['full_sha256']!=sha_bytes(contents[name]):
                raise ValueError(f'{day}: forward K-line delta differs from hash-chained checkpoint: {name}')
        latest=checkpoint; dates.append(day)
    expected_latest=gate.read_json(archive_dir/LAST_NAME)
    if expected_latest!=latest or expected_latest['record_sha256']!=latest['record_sha256']:
        raise ValueError('Archive latest pointer disagrees with chronological checkpoints')
    out_dir.mkdir(parents=True,exist_ok=True)
    for name,b in contents.items():
        (out_dir/name).write_bytes(b)
    # Check all original bytes, all daily appended timestamp and OHLC boundaries.
    gate.validate_sources(lock,out_dir,asof_ms=gate.parse_utc(latest['asof_utc']) if dates else None,
                          previous=genesis,require_current=bool(dates))
    return {'status':'reconstructed','last_record':latest,'archived_days':len(dates),
            'dataset_sha256':{name:sha_bytes(data) for name,data in contents.items()}}


def daily(archive_dir: Path, lock: dict[str,Any], asof_ms: int, *, fetcher=None,
          client=None) -> dict[str,Any]:
    """Capture ALL 24 files or publish none; only fully closed timeframes."""
    if asof_ms%gate.MS_PER['1d']:
        raise ValueError('Data archive checkpoints must be UTC day boundaries')
    if not gate.parse_utc(gate.START)<=asof_ms<gate.parse_utc(gate.END):
        raise ValueError('Daily archive cutoff outside registered observation window')
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    if asof_ms>now:
        raise ValueError('Future UTC daily capture cannot run before candle close')
    day=_safe_day(gate.utc(asof_ms)[:10])
    if (archive_dir/DAY_DIR/day).exists():
        # A rerun of the same UTC date is a verification-only no-op.
        with tempfile.TemporaryDirectory(prefix='p0-08-replay-') as td:
            existing=reconstruct(archive_dir,Path(td),lock)
        prior=existing['last_record']
        if prior['asof_utc'] != gate.utc(asof_ms):
            raise ValueError('An archived day is already present but is not the latest checkpoint')
        return {'status':'already_archived_verified','asof_utc':prior['asof_utc'],
                'archive_record_sha256':prior['record_sha256'],
                'new_closed_bars':0,'daily_file_count':len(lock['frozen_data_prefixes'])}
    with tempfile.TemporaryDirectory(prefix='p0-08-capture-') as tmp:
        data=Path(tmp)/'candles'
        result=reconstruct(archive_dir,data,lock)
        previous=result['last_record']
        if asof_ms<=gate.parse_utc(previous['asof_utc']):
            raise ValueError('Non-forward daily checkpoint / backdating')
        prior_bytes={name:(data/name).stat().st_size for name in lock['frozen_data_prefixes']}
        counts=capture_mod.capture(lock,data,asof_ms,fetcher=fetcher,client=client)
        verified=gate.validate_sources(lock,data,asof_ms=asof_ms,previous=previous,require_current=True)
        payload={'status':'daily_data_archived_not_scored',
                 'trades_scored':False,'forward_trade_count':None,
                 'appended_closed_candles_by_file':counts,
                 'warning':'Only price/volume data captured. Formal trade simulation is NOT run here; zero count is not a trade result.'}
        checkpoint=gate.make_record(lock,verified,asof_ms,previous=previous,payload=payload)
        # Stage in temporary directory; GH git commit will make day+latest atomic.
        staged=Path(tmp)/'day'
        delta=staged/'delta';delta.mkdir(parents=True)
        for name in lock['frozen_data_prefixes']:
            raw=(data/name).read_bytes()
            if sha_bytes(raw[:prior_bytes[name]])!=previous['files'][name]['full_sha256']:
                raise ValueError(f'{name}: prior checkpoint input edited during capture')
            (delta/name).write_bytes(raw[prior_bytes[name]:])
        save(staged/'checkpoint.json',checkpoint)
        # Original archive remains untouched until all 24 deltas + record validated.
        destination=archive_dir/DAY_DIR/day
        staged.rename(destination)
        save(archive_dir/LAST_NAME,checkpoint)
    return {'status':'daily_data_archived_not_scored','asof_utc':gate.utc(asof_ms),
            'archive_record_sha256':checkpoint['record_sha256'],
            'new_closed_bars':sum(counts.values()),'daily_file_count':len(counts),
            'next_action':'score separately with original frozen simulator when capacity permits'}


def build_seed(source_dir: Path, target_zip: Path, lock: dict[str,Any]) -> None:
    """Build 24-file baseline seed from real frozen source download."""
    target_zip.parent.mkdir(parents=True,exist_ok=True)
    with ZipFile(target_zip,'w',ZIP_DEFLATED,compresslevel=6) as z:
        for name in sorted(lock['frozen_data_prefixes']):
            path=source_dir/name
            if not path.is_file():
                raise ValueError(f'Original 24-file historical seed incomplete: {name}')
            z.write(path,arcname=name)
    _seed_map(target_zip,lock)


def cli(argv=None)->int:
    p=argparse.ArgumentParser(description='P0-08 immutable 1H+ forward data archive (NO strategy decisions or orders)')
    p.add_argument('mode',choices=['bootstrap','audit','daily'])
    p.add_argument('--lock',type=Path,default=Path('PROSPECTIVE_LOCK_V1.json'))
    p.add_argument('--repo-dir',type=Path,default=Path('.'))
    p.add_argument('--archive-dir',type=Path,required=True)
    p.add_argument('--seed-source',type=Path)
    p.add_argument('--genesis',type=Path)
    p.add_argument('--asof',type=str)
    a=p.parse_args(argv)
    lock=gate.lock_check(a.lock,a.repo_dir)
    if a.mode=='bootstrap':
        if not a.seed_source or not a.genesis:
            p.error('Bootstrap requires --seed-source and --genesis')
        with tempfile.TemporaryDirectory(prefix='p0-08-seed-') as tmp:
            seed=Path(tmp)/SEED_NAME
            build_seed(a.seed_source,seed,lock)
            res=bootstrap(a.archive_dir,seed,a.genesis,lock)
    elif a.mode=='audit':
        with tempfile.TemporaryDirectory(prefix='p0-08-verify-') as tmp:
            out=reconstruct(a.archive_dir,Path(tmp),lock)
            res={'status':'archive_verified','archived_days':out['archived_days'],
                 'last_record_sha256':out['last_record']['record_sha256']}
    else:
        if not a.asof:
            p.error('Daily archival requires explicit --asof UTC midnight')
        res=daily(a.archive_dir,lock,gate.parse_utc(a.asof))
    print(json.dumps(res,ensure_ascii=False,sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(cli())