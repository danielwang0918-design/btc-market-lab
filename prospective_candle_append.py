#!/usr/bin/env python3
"""Opt-in, public Binance 1D/4H/1H future candle append, no order APIs.

The original frozen CSV bytes are immutable. This module does NOT run by itself
on any schedule while the development workflow stays unmerged from main.
"""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import prospective_validation as gate


def capture(lock: dict, directory: Path, asof_ms: int, *, client=None,
            fetcher=None, dry_run: bool = False) -> dict[str, int]:
    """Fetch ALL missing *closed* bars first; stage writes only after validation.

    A failure while downloading a particular coin must not make us silently
    exclude it or publish an incomplete prospective cohort.
    """
    if asof_ms % gate.MS_PER['1h']:
        raise ValueError('Capture as-of must be aligned with UTC 1H boundary')
    if not gate.parse_utc(gate.START) <= asof_ms <= gate.parse_utc(gate.END):
        raise ValueError('Capture outside preregistered forward window')
    if fetcher is None:
        from historical_backtest import fetch_historical_candles as fetcher
    current=gate.validate_sources(lock,directory,asof_ms=None)
    all_new={};counts={};temporary=[]
    try:
        for name,meta in lock['frozen_data_prefixes'].items():
            end=asof_ms // gate.MS_PER[meta['interval']] * gate.MS_PER[meta['interval']]
            start=gate.parse_utc(current[name]['last_closed_end_exclusive_utc'])
            if end < start:
                raise ValueError(f'{name}: As-of before already captured candles')
            if start == end:
                all_new[name]=[];counts[name]=0;continue
            bars=fetcher(meta['symbol'],meta['interval'],start,end,client=client)
            expected=list(range(start,end,gate.MS_PER[meta['interval']]))
            observed=[int(x['open_time']) for x in bars]
            if observed != expected:
                raise ValueError(f'{name}: Binance public K-line missing/duplicated bars; reject batch')
            if any(str(b['is_closed']).lower()!='true' or b['source']!=gate.SOURCE for b in bars):
                raise ValueError(f'{name}: Unexpected data source or unclosed candle')
            all_new[name]=bars;counts[name]=len(bars)
        if dry_run:
            return counts
        for name,bars in all_new.items():
            if not bars:continue
            target=directory/name
            fd,tmp=tempfile.mkstemp(prefix='.prospective-append-',suffix='.csv',dir=directory)
            os.close(fd);staged=Path(tmp);temporary.append(staged)
            shutil.copyfile(target,staged)  # preserves exact, independently hashed frozen prefix
            with staged.open('a',newline='',encoding='utf-8') as f:
                from historical_backtest import COLUMNS
                writer=csv.DictWriter(f,fieldnames=COLUMNS,extrasaction='ignore')
                writer.writerows(bars)
        # Validate the ENTIRE appended source while still in temporary files.
        # No staged data may violate frozen prefix, prior rows, timestamps or OHLCV.
        target_pairs=list(zip((directory/n for n,b in all_new.items() if b), temporary))
        for dest,staged in target_pairs:
            meta=lock['frozen_data_prefixes'][dest.name]
            with staged.open('rb') as f:
                prefix=f.read(meta['bytes']);tail=f.read()
            if gate.sha_bytes(prefix)!=meta['sha256']:
                raise ValueError(f'{dest.name}: Staged source prefix mutated')
            gate._verify_ohlcv_tail(dest.name,meta,tail,prefix.splitlines(keepends=True)[0],asof_ms=asof_ms)
        # Replace only after every symbol and timeframe has passed the audit.
        # A disk crash during multi-file replacement still requires a new full
        # manifest verification before observations may be published.
        for dest,staged in target_pairs:
            staged.replace(dest)
        gate.validate_sources(lock,directory,asof_ms=asof_ms,require_current=True)
        return counts
    finally:
        for f in temporary:
            if f.exists():f.unlink()


def main(argv=None)->int:
    p=argparse.ArgumentParser(description='Append PUBLIC Binance 1H+ CLOSED bars, no trades')
    p.add_argument('--lock',type=Path,default=Path('PROSPECTIVE_LOCK_V1.json'))
    p.add_argument('--repo-dir',type=Path,default=Path('.'))
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--asof',type=str)
    p.add_argument('--dry-run',action='store_true')
    a=p.parse_args(argv)
    protocol=gate.lock_check(a.lock,a.repo_dir)
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    at=gate.parse_utc(a.asof) if a.asof else now // gate.MS_PER['1h'] * gate.MS_PER['1h']
    if at>now:raise ValueError('Do not fetch future candles')
    counts=capture(protocol,a.data_dir,at,dry_run=a.dry_run)
    print({'status':'research_only_no_orders','asof':gate.utc(at),'appended_bars_by_file':counts})
    return 0


if __name__=='__main__':
    raise SystemExit(main())