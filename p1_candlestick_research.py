#!/usr/bin/env python3
"""P1-01: isolated, causal candlestick geometry study on frozen 1H/4H/1D Spot data.

Research-only. Does not modify mtf-bos-ob-retest-v1, place orders, optimize a
strategy on an opened Test window, or claim Bulkowski stock reversal figures
as cryptocurrency trading win rates.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zipfile import ZipFile

VERSION = 'p1-candlestick-geometry-v1'
LOCK_SHA256 = '42c0d049a8f277934b2908767869e1c6830009c47f346463140c9abac7549dbf'
FRAMES = {'1h': 3_600_000, '4h': 14_400_000, '1d': 86_400_000}
PATTERNS = ('bullish_engulfing', 'bearish_engulfing', 'morning_star',
            'three_white_soldiers', 'three_black_crows')
HORIZONS_HOURS = (24, 72, 168)  # fixed, do not choose the best horizon after Test
START_VALIDATION = 1672531200000  # 2023-01-01 00:00 UTC
START_EXPLORATORY = 1735689600000 # 2025-01-01 00:00 UTC
FEE_BPS_SIDE = 10
SLIPPAGE_BPS_SIDE = 5
OUTPUT_COLUMNS = ['symbol','timeframe','pattern','direction','split','signal_close_utc',
                  'signal_open_ms','prior_trend_3','atr14_before_signal','horizon_hours',
                  'outcome_status','entry_next_open_utc','exit_close_utc','directional_favorable_pct',
                  'paper_net_pct']


@dataclass(frozen=True)
class Candle:
    open_ms: int
    o: float
    h: float
    l: float
    c: float
    v: float

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def full_range(self) -> float:
        return self.h - self.l

    @property
    def bullish(self) -> bool:
        return self.c > self.o

    @property
    def bearish(self) -> bool:
        return self.c < self.o

    @property
    def body_ratio(self) -> float:
        return self.body / self.full_range if self.full_range > 0 else 0.


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def read_lock(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if sha(raw) != LOCK_SHA256:
        raise ValueError('PRE-REGISTERED LOCK DIGEST CHANGED; stop research')
    obj = json.loads(raw)
    if (obj['protocol_version'] != 'p0-forward-v1'
            or obj['symbols'] != ['BTCUSDT','ETHUSDT','SOLUSDT','ZECUSDT',
                                  'XRPUSDT','BNBUSDT','NEARUSDT','DOGEUSDT']
            or len(obj['frozen_data_prefixes']) != 24):
        raise ValueError('Research universe drift')
    return obj


def parse_bars(raw: bytes, symbol: str, frame: str, expected: int) -> list[Candle]:
    if frame not in FRAMES:
        raise ValueError('Trading data may be only 1h / 4h / 1d')
    step = FRAMES[frame]
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'),newline=''))
    if not {'symbol','interval','open_time','close_time','open','high','low','close','volume',
            'is_closed','source'}.issubset(reader.fieldnames or []):
        raise ValueError('Missing frozen OHLCV header')
    bars = []
    prior = None
    for r in reader:
        if r['symbol'] != symbol or r['interval'] != frame or r['is_closed'] != 'true' or r['source'] != 'Binance Spot REST API':
            raise ValueError('Wrong source, timeframe, symbol or unclosed candle')
        t = int(r['open_time'])
        if t % step or int(r['close_time']) != t + step - 1 or (prior is not None and t <= prior):
            raise ValueError('Misaligned, duplicate or unsorted OHLCV timestamps')
        o, h, l, c, v = (float(r[x]) for x in ('open','high','low','close','volume'))
        if not all(math.isfinite(x) for x in (o,h,l,c,v)) or not (l > 0 and v >= 0 and l <= min(o,c) <= max(o,c) <= h):
            raise ValueError('Invalid or nonfinite OHLCV')
        bars.append(Candle(t,o,h,l,c,v))
        prior=t
    if len(bars) != expected or not bars:
        raise ValueError('Frozen candle row count mismatch')
    return bars


def contiguous_segment_start(bars: list[Candle], step: int) -> list[int]:
    start = []
    for i, b in enumerate(bars):
        start.append(start[i-1] if i and b.open_ms-bars[i-1].open_ms==step else i)
    return start


def confirmed_at(bars: list[Candle], idx: int, start: int) -> list[tuple[str,str]]:
    """Only candles at or before idx. No unknown future bar or opening quote."""
    if idx < 1 or idx >= len(bars) or start > idx-1:
        return []
    a, b = bars[idx-1], bars[idx]
    out = []
    if a.bearish and b.bullish and b.o <= a.c and b.c >= a.o and b.body > a.body and b.body_ratio >= 0.4:
        out.append(('bullish_engulfing','long'))
    if a.bullish and b.bearish and b.o >= a.c and b.c <= a.o and b.body > a.body and b.body_ratio >= 0.4:
        out.append(('bearish_engulfing','short'))
    if idx >= start+2:
        x, y, z = bars[idx-2],bars[idx-1],bars[idx]
        if (x.bearish and x.body_ratio >= .5 and x.body > 0
                and y.body <= .35*x.body and z.bullish and z.body_ratio >= .5
                and max(y.o,y.c) <= (x.o+x.c)/2
                and z.c >= (x.o+x.c)/2):
            out.append(('morning_star','long'))
        seq = (x,y,z)
        if (all(q.bullish and q.body_ratio>=.55 for q in seq)
                and x.c<y.c<z.c and x.o<=y.o<=z.o
                and y.o<=x.c and z.o<=y.c
                and all((q.h-q.c) <= .5*q.body for q in seq)):
            out.append(('three_white_soldiers','long'))
        if (all(q.bearish and q.body_ratio>=.55 for q in seq)
                and x.c>y.c>z.c and x.o>=y.o>=z.o
                and y.o>=x.c and z.o>=y.c
                and all((q.c-q.l) <= .5*q.body for q in seq)):
            out.append(('three_black_crows','short'))
    return out


def pretrend(bars: list[Candle], idx: int, first: int, segment_start: int) -> str:
    """3 fully closed candles BEFORE the pattern. Context, never a hidden filter."""
    if first - 3 < segment_start:
        return 'unknown'
    a,b,c = (bars[i].c for i in (first-3,first-2,first-1))
    return 'up' if a<b<c else 'down' if a>b>c else 'range'


def atr14_before(bars: list[Candle], first: int, start: int) -> float | None:
    if first-15 < start or first<15:
        return None
    vals = []
    for i in range(first-14,first):
        p=bars[i-1]
        b=bars[i]
        vals.append(max(b.h-b.l, abs(b.h-p.c), abs(b.l-p.c)))
    return sum(vals)/len(vals)


def split_for(signal_close_exclusive: int) -> str:
    if signal_close_exclusive < START_VALIDATION:
        return 'train'
    if signal_close_exclusive < START_EXPLORATORY:
        return 'validation'
    return 'exploratory_test_opened'


def split_end(split: str, dataset_close_exclusive: int) -> int:
    if split == 'train':
        return START_VALIDATION
    if split == 'validation':
        return START_EXPLORATORY
    return dataset_close_exclusive


def paper_return(entry: float, exit_: float, side: str) -> tuple[float,float]:
    """Direction and hypothetical fixed holding net %.  Spot shorts are NOT executable."""
    if entry <= 0 or exit_ <= 0 or side not in ('long','short'):
        raise ValueError('Bad hypothetical fill input')
    slip = SLIPPAGE_BPS_SIDE/10000
    fee = FEE_BPS_SIDE/10000
    directional = (exit_/entry-1) if side=='long' else (1-exit_/entry)
    effective_entry = entry*(1+slip if side=='long' else 1-slip)
    effective_exit = exit_*(1-slip if side=='long' else 1+slip)
    gross = ((effective_exit-effective_entry)/effective_entry if side=='long'
             else (effective_entry-effective_exit)/effective_entry)
    net = gross - fee*(1+effective_exit/effective_entry)
    return directional*100,net*100


def study_rows(bars: list[Candle], symbol: str, frame: str,
               horizons: tuple[int,...] = HORIZONS_HOURS) -> list[dict[str,Any]]:
    if frame not in FRAMES:
        raise ValueError('Only 1h,4h,1d')
    step = FRAMES[frame]
    if any(h <= 0 or (h*3_600_000)%step for h in horizons):
        raise ValueError('All forward durations must be positive multiples of timeframe')
    start_by_i=contiguous_segment_start(bars,step)
    result=[]
    end=bars[-1].open_ms+step if bars else 0
    for i,b in enumerate(bars):
        kinds=confirmed_at(bars,i,start_by_i[i])
        if not kinds:
            continue
        signal_close=b.open_ms+step
        split=split_for(signal_close)
        segment_start=start_by_i[i]
        for kind,side in kinds:
            first=i-2 if kind in ('morning_star','three_white_soldiers','three_black_crows') else i-1
            preceding=pretrend(bars,i,first,segment_start)
            atr=atr14_before(bars,first,segment_start)
            for h in horizons:
                k=i+h*3_600_000//step
                eligible=(k<len(bars) and start_by_i[k]==segment_start
                          and bars[k].open_ms+step<=split_end(split,end))
                entry=bars[i+1].o if eligible else None
                exit_=bars[k].c if eligible else None
                direction,net=paper_return(entry,exit_,side) if eligible else (None,None)
                result.append({
                    'symbol':symbol,'timeframe':frame,'pattern':kind,'direction':side,
                    'split':split,'signal_close_utc':utc(signal_close),
                    'signal_open_ms':b.open_ms,'prior_trend_3':preceding,
                    'atr14_before_signal':round(atr,8) if atr is not None else None,
                    'horizon_hours':h,
                    'outcome_status':'evaluated' if eligible else 'right_censored_or_split_boundary',
                    'entry_next_open_utc':utc(bars[i+1].open_ms) if eligible else None,
                    'exit_close_utc':utc(bars[k].open_ms+step) if eligible else None,
                    'directional_favorable_pct':round(direction,8) if eligible else None,
                    'paper_net_pct':round(net,8) if eligible else None,
                })
    return result


def summarize(rows: list[dict[str,Any]]) -> list[dict[str, Any]]:
    grouped=defaultdict(list)
    for r in rows:
        grouped[(r['symbol'],r['timeframe'],r['pattern'],r['split'],r['horizon_hours'])].append(r)
    out=[]
    for (s,frame,kind,split,h),items in sorted(grouped.items()):
        evaluable=[r for r in items if r['outcome_status']=='evaluated']
        net=[r['paper_net_pct'] for r in evaluable]
        gross=[r['directional_favorable_pct'] for r in evaluable]
        out.append({'symbol':s,'timeframe':frame,'pattern':kind,'split':split,'horizon_hours':h,
                    'observed_signals':len(items),'evaluated':len(evaluable),
                    'right_censored':len(items)-len(evaluable),
                    'directional_positive_percent':round(100*sum(x>0 for x in gross)/len(gross),4) if gross else None,
                    'net_positive_percent_paper':round(100*sum(x>0 for x in net)/len(net),4) if net else None,
                    'mean_paper_net_pct':round(statistics.mean(net),6) if net else None,
                    'median_paper_net_pct':round(statistics.median(net),6) if net else None})
    return out


def main(argv: list[str]|None=None) -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--frozen-archive',type=Path,required=True)
    p.add_argument('--lock',type=Path,required=True)
    p.add_argument('--out-dir',type=Path,default=Path('research/p1_01'))
    a=p.parse_args(argv)
    lock=read_lock(a.lock)
    output=[];input_sha={};bars_by_file={}
    with ZipFile(a.frozen_archive) as z:
        archive_files=set(z.namelist())
        for symbol in lock['symbols']:
            for frame in ('1h','4h','1d'):
                fname=f'{symbol}_{frame}.csv'
                meta=lock['frozen_data_prefixes'][fname]
                if fname not in archive_files:
                    raise ValueError(f'Missing frozen evidence: {fname}')
                raw=z.read(fname)
                if sha(raw)!=meta['sha256'] or len(raw)!=meta['bytes']:
                    raise ValueError(f'FROZEN DATA SHA256 MISMATCH: {fname}')
                candles=parse_bars(raw,symbol,frame,meta['frozen_bars'])
                input_sha[fname]=sha(raw)
                bars_by_file[fname]=len(candles)
                output.extend(study_rows(candles,symbol,frame))
    summary=summarize(output)
    # Output complete unselected results: no best-performing-pattern ranking.
    manifest={'version':VERSION,'status':'exploratory_frozen_history_only',
              'source_run':43820018876,'source_archive_sha256':sha(a.frozen_archive.read_bytes()),
              'lock_sha256':LOCK_SHA256,'input_sha256':input_sha,'bars_by_file':bars_by_file,
              'patterns':PATTERNS,'timeframes':list(FRAMES),'fixed_horizons_hours':list(HORIZONS_HOURS),
              'fee_bps_per_side':FEE_BPS_SIDE,'slippage_bps_per_side':SLIPPAGE_BPS_SIDE,
              'observed_pattern_horizon_rows':len(output),
              'unique_pattern_events':len({(r['symbol'],r['timeframe'],r['pattern'],r['signal_open_ms']) for r in output}),
              'outcome_rows_evaluated':sum(r['outcome_status']=='evaluated' for r in output),
              'warning':['All 2025+ Test periods are already opened; not independent out-of-sample validation.',
                         'No fixed stop or target: paper percentage returns, not strategy R, PF or a tradable backtest.',
                         'Spot market short outcomes are only symmetric paper directional studies, not executable Spot shorts.',
                         'Signals across assets and horizons overlap/correlate; do not select the best combinations.',
                         'Patterns are explicitly fixed crypto/no-gap research definitions, not validated Bulkowski definitions.',
                         'No 1m/15m or tick decision inputs; no live trades.']}
    a.out_dir.mkdir(parents=True,exist_ok=True)
    (a.out_dir/'p1_01_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (a.out_dir/'p1_01_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    with (a.out_dir/'p1_01_events.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=OUTPUT_COLUMNS)
        writer.writeheader();writer.writerows(output)
    with (a.out_dir/'p1_01_summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        keys=['symbol','timeframe','pattern','split','horizon_hours','observed_signals','evaluated','right_censored','directional_positive_percent','net_positive_percent_paper','mean_paper_net_pct','median_paper_net_pct']
        writer=csv.DictWriter(f,fieldnames=keys)
        writer.writeheader();writer.writerows(summary)
    totals=Counter((r['pattern'] for r in output if r['horizon_hours']==24))
    print(json.dumps({'status':manifest['status'],'validated_source_files':len(input_sha),'events':manifest['unique_pattern_events'],
                     'counts_24h_each_pattern':dict(totals),'summary_rows':len(summary)},ensure_ascii=False))
    return 0

if __name__=='__main__':
    raise SystemExit(main())