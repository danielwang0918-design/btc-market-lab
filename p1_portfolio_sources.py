"""Read-only canonical P0 tape generation. No P0 source or strategy mutations."""
import argparse,csv,gzip,hashlib,json
from bisect import bisect_left
from decimal import Decimal
from pathlib import Path
import historical_backtest as engine
import p1_candlestick_research as core

PROTOCOL_SHA='56265d7e7aa8c4fb5aeeb2b49442db96df0291b8df8bf667c37bda7c6c0b0690'
TRADE_SHA='cc9ae34de758569306e24d7e8cdec79006ef502effeeadc85425e09547a0d0c0'

def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def generate(symbol,data,out):
    lock=core.read_lock(Path(__file__).with_name('PROSPECTIVE_LOCK_V1.json'))
    if digest(Path(__file__).with_name('P1_05_PROTOCOL.json'))!=PROTOCOL_SHA:raise ValueError('Protocol changed')
    for frame in core.FRAMES:
        name=f'{symbol}_{frame}.csv';raw=(data/name).read_bytes();meta=lock['frozen_data_prefixes'][name]
        if core.sha(raw)!=meta['sha256'] or len(raw)!=meta['bytes']:raise ValueError('Frozen SHA changed')
        core.parse_bars(raw,symbol,frame,meta['frozen_bars'],closed_through_ms=meta['frozen_last_close_exclusive_ms'])
    rows,snaps,summary=engine._prepare_snapshots(data,symbol,fee_bps_per_side=10,
        slippage_bps_per_side=5,minimum_net_rr=1.5,max_setup_age_hours=168,progress_every=10000)
    out.mkdir(parents=True,exist_ok=True)
    with gzip.open(out/f'{symbol}.json.gz','wt',encoding='utf-8') as f:
        json.dump({'symbol':symbol,'input_sha256':{f'{symbol}_{iv}.csv':lock['frozen_data_prefixes'][f'{symbol}_{iv}.csv']['sha256'] for iv in core.FRAMES},
            'canonical_code_sha256':digest(Path(engine.__file__)),
            'open_times':[int(r['open_time']) for r in rows],'snapshots':snaps},f,separators=(',',':'))
    print(symbol,'snapshot count',len(snaps),flush=True)

def reference(data,tapes,source_csv,out):
    if digest(source_csv)!=TRADE_SHA:raise ValueError('Original trade CSV changed')
    expected=list(csv.DictReader(source_csv.open(encoding='utf-8-sig')))
    lock=core.read_lock(Path(__file__).with_name('PROSPECTIVE_LOCK_V1.json'))
    boundaries=[('train',0,1672531200000),('validation',1672531200000,1735689600000),('test',1735689600000,1794787200000)]
    found=[]
    for sym in lock['symbols']:
        rows=engine._closed_rows(data/f'{sym}_1h.csv',sym,'1h')
        with gzip.open(tapes/f'{sym}.json.gz','rt') as f:tape=json.load(f)
        times=tape['open_times']
        for split,start,end in boundaries:
            trades=engine._simulate_slice(rows,tape['snapshots'],bisect_left(times,start),bisect_left(times,end),
                minimum_net_rr=1.5,max_setup_age_hours=168,fee_bps=Decimal(10),slippage_bps=Decimal(5))
            found.extend({'symbol':sym,'split':split,**r} for r in trades)
    fields=tuple(expected[0]);identity=lambda r:tuple(str(r[k]) for k in fields)
    if sorted(map(identity,found))!=sorted(map(identity,expected)):raise ValueError('Unchanged canonical replay differs from frozen original trades')
    out.mkdir(parents=True,exist_ok=True)
    (out/'canonical_reference.json').write_text(json.dumps({'unchanged_P0_replay_matches_archive':True,'original_trades':len(found),
        'trade_csv_sha256':TRADE_SHA,'canonical_source_sha256':digest(Path(engine.__file__))},indent=2)+'\n')
    with (out/'canonical_full_trades.json').open('w') as f:json.dump(found,f)
    print('Canonical original trade replay verified:',len(found))

def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['tape','reference']);p.add_argument('--symbol');p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--tapes',type=Path,required=True);p.add_argument('--trade-csv',type=Path);p.add_argument('--out-dir',type=Path)
    a=p.parse_args()
    if a.mode=='tape':generate(a.symbol,a.data_dir,a.tapes)
    else:reference(a.data_dir,a.tapes,a.trade_csv,a.out_dir)
if __name__=='__main__':main()
