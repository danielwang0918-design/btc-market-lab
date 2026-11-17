#!/usr/bin/env python3
"""Bounded public data-only receiver, raw receipts and P1-08C evidence adapter.

No signals, orders, account endpoints, API keys or future price labels.
GitHub retention is independently checkable but is not timestamp notarization.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path,PurePosixPath
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,build_opener,HTTPRedirectHandler
import uuid
from zipfile import ZipFile,ZIP_DEFLATED

import p1_08c_registry as c
from p1_08c_public_probe import metrics as old_metrics

VERSION='p1-08d-receiver-v1'
BASE='https://data-api.binance.vision'
MAX_BYTES=1_048_576
H=c.FRAMES['1h']
PLAN=Path(__file__).with_name('P1_08D_PLAN.json')


def read(path):return c.load_json(Path(path).read_bytes())


def write(path,value):
    """Exclusive immutable metadata; never replace an existing observation."""
    with Path(path).open('xb') as f:f.write(c.canonical(value)+b'\n');f.flush();os.fsync(f.fileno())
    Path(path).chmod(0o444)


def url(endpoint,symbol=None,frame=None):
    if endpoint=='time':return BASE+'/api/v3/time'
    if symbol not in c.SYMBOLS:raise ValueError('Unknown public Spot symbol')
    if endpoint=='klines':
        if frame not in c.FRAMES:raise ValueError('Only closed 1H/4H/1D sources')
        return BASE+'/api/v3/klines?'+urlencode(dict(symbol=symbol,interval=frame,limit=3,timeZone='0'))
    if endpoint=='bookTicker':return BASE+'/api/v3/ticker/bookTicker?'+urlencode(dict(symbol=symbol))
    if endpoint=='depth':return BASE+'/api/v3/depth?'+urlencode(dict(symbol=symbol,limit=20))
    raise ValueError('No trading, account or arbitrary endpoints')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Public receiver does not follow redirects')


def transport(endpoint,symbol,frame,*,clock,opener,session_id):
    target=url(endpoint,symbol,frame);req=Request(target,method='GET',headers={'Accept':'application/json','User-Agent':VERSION})
    start=clock();tick=time.monotonic_ns();raw=b'';status=None;error=None;headers={}
    try:
        with opener(req,timeout=10) as response:
            status=response.status;headers={k:v for k,v in response.headers.items() if k.lower() in ('date','content-type','retry-after','x-mbx-used-weight-1m')}
            raw=response.read(MAX_BYTES+1)
    except HTTPError as exc:
        status=exc.code;headers={k:v for k,v in (exc.headers or {}).items() if k.lower() in ('date','retry-after','content-type')};raw=exc.read(MAX_BYTES+1)
    except (OSError,TimeoutError,ValueError) as exc:error=type(exc).__name__
    finish=clock();elapsed=(time.monotonic_ns()-tick)/1e6;truncated=len(raw)>MAX_BYTES;raw=raw[:MAX_BYTES]
    clock_ok=finish>=start and abs((finish-start)-elapsed)<=2000 and elapsed<=10000
    return raw,dict(endpoint=endpoint,symbol=symbol,timeframe=frame,url=target,http_method='GET',http_status=status,
        exchange='Binance',market_type='Spot',request_started_at=c.utc(start),system_received_at=c.utc(finish),
        monotonic_elapsed_ms=elapsed,clock_session_id=session_id,clock_locally_consistent=clock_ok,
        transport_error=error,response_truncated=truncated,http_headers=headers,
        raw_sha256=c.sha_bytes(raw),raw_bytes=len(raw),source_published_at=None,
        publication_time_status='unknown_not_imputed_from_HTTP_Date_or_serverTime',api_key_used=False,account_permission_used=False)


def server_reference(meta,body):
    if meta['http_status']!=200 or not meta['clock_locally_consistent'] or meta['response_truncated']:return None
    try:
        s=c.load_json(body)['serverTime']
        if type(s) is not int:return None
        low=s-c.timestamp(meta['system_received_at']);high=s-c.timestamp(meta['request_started_at'])
        if low>2000 or high< -2000:return None
        return s
    except (ValueError,KeyError,TypeError):return None


def normalize(row,symbol,frame,meta,server_ms,index):
    if not isinstance(row,list) or len(row)!=12 or type(row[0]) is not int or type(row[6]) is not int:raise ValueError('Invalid kline schema/unit')
    op=row[0];cl=op+c.FRAMES[frame]
    if op%c.FRAMES[frame] or row[6]!=cl-1:raise ValueError('Invalid UTC alignment or inclusive close/unit')
    source=dict(id='binance-spot-public-rest-v1',uri=meta['url']+'#raw-sha256='+meta['raw_sha256']+'&row='+str(index),
        market_event_at=c.utc(op),published_at=None,unknown_reason='REST final-candle publication time not provided')
    value=dict(symbol=symbol,timeframe=frame,open_at=c.utc(op),close_at=c.utc(cl),
        ohlcv=dict(zip(('open','high','low','close','volume','quote_volume'),[row[1],row[2],row[3],row[4],row[5],row[7]])),source=source)
    # Geometry validation even for open rows. Source event clock is only checked for closed input.
    o,h,l,z,v,q=[c.number(row[i]) for i in (1,2,3,4,5,7)]
    if not 0<l<=min(o,z)<=max(o,z)<=h or min(v,q)<0 or type(row[8]) is not int or row[8]<0:raise ValueError('Invalid OHLCV/trade count')
    closed=cl<=server_ms-1000 and cl<=c.timestamp(meta['request_started_at']) and cl<=c.timestamp(meta['system_received_at'])
    return value,closed,c.sha_bytes(c.canonical(row))


class Receiver:
    def __init__(self,root,*,clock=None,opener=None,expected_mode=None):
        self.root=Path(root)
        if self.root.is_symlink():raise ValueError('Symlink root forbidden')
        self.contract=read(self.root/'contract.json');mode=self.contract['mode']
        if mode not in ('synthetic','real_public') or (expected_mode and mode!=expected_mode):raise ValueError('Do not mix real and synthetic stores')
        if mode!='synthetic' and (clock is not None or opener is not None):raise ValueError('Injected clocks/transports forbidden for real-public reception')
        self.clock=clock or c.system_ms;self.opener=opener or build_opener(NoRedirect()).open;self.session_id=str(uuid.uuid4())
        self.registry=c.Registry(self.root/'registry',clock=clock if mode=='synthetic' else None)

    @classmethod
    def create(cls,root,*,mode,symbols=None,frames=None,clock=None,opener=None):
        root=Path(root);symbols=list(symbols or ['BTCUSDT','ETHUSDT']);frames=list(frames or ['1h','4h','1d'])
        if mode not in ('synthetic','real_public') or (mode!='synthetic' and (clock is not None or opener is not None)):raise ValueError('Explicit mode and real system transport required')
        if not symbols or len(set(symbols))!=len(symbols) or not set(symbols)<=set(c.SYMBOLS) or not frames or len(set(frames))!=len(frames) or not set(frames)<=set(c.FRAMES):raise ValueError('Fixed eligible universe/closed decision frames')
        if root.exists():raise ValueError('Never overwrite an existing receiver')
        now=(clock or c.system_ms)();code=c.sha_file(Path(__file__));start=(now//H+1)*H
        p=dict(study_key='p1-08d-data-only-'+str(uuid.uuid4()),
            hypothesis=dict(id='infrastructure-conformance-only',statement='Closed-data and receipt conformance; no price/trading hypothesis',falsification='Any causal/raw/integrity failure'),
            rule=dict(id='no-signal-data-adapter',version=VERSION,code_sha256=code),study_start_at=c.utc(start),
            review_dates=[c.utc(c.add_months(start,24))],arms=['data_only'],
            features={'last_closed_close':dict(definition='Exact close of a raw verified closed candle, not an entry rule',code_sha256=code,dtype='number',lookback_bars={'1h':1})},
            label_names=['reserved_data_quality_no_price_label'],label_calculator_sha256=code,horizons_hours=[72],
            observation_anchor='next_1h_boundary_after_registration',costs=dict(fee_bps_per_side=10,slippage_bps_per_side=[5,15],paper_initial_USDT=1000,risk_fraction=0.02))
        root.mkdir(parents=True);(root/'batches').mkdir()
        reg=c.Registry.create(root/'registry',p,mode='synthetic' if mode=='synthetic' else 'prospective',clock=clock if mode=='synthetic' else None)
        contract=dict(version=VERSION,mode=mode,exchange='Binance',market_type='Spot',symbols=symbols,frames=frames,
            receiver_started_at=c.utc(now),receiver_code_sha256=code,plan_sha256=c.sha_file(PLAN),registry_genesis=reg.checkpoint(),
            active_trading_hypotheses=0,candidate_registration_enabled=False,external_timestamp_attestation_verified=False)
        contract['contract_sha256']=c.sha_bytes(c.canonical(contract));write(root/'contract.json',contract)
        return cls(root,clock=clock,opener=opener,expected_mode=mode)

    def _batches(self):
        allowed={'contract.json','registry','batches','.receiver-lock'}
        if any(p.name not in allowed for p in self.root.iterdir()):raise ValueError('Pending/interrupted or unexpected receiver state; restore a verified snapshot into a new root')
        result=[]
        for i,folder in enumerate(sorted((self.root/'batches').iterdir()),1):
            if folder.is_symlink() or not folder.is_dir() or folder.name!=f'{i:010d}':raise ValueError('Missing/extra/reordered batch')
            m=read(folder/'manifest.json');body={k:v for k,v in m.items() if k!='batch_sha256'}
            if m['batch_sha256']!=c.sha_bytes(c.canonical(body)):raise ValueError('Batch manifest drift')
            expected=set(m['files_sha256'])|{'manifest.json'}
            if {f.name for f in folder.iterdir()}!=expected:raise ValueError('Batch file inventory drift')
            for name,digest in m['files_sha256'].items():
                f=folder/name
                if '/' in name or f.is_symlink() or c.sha_file(f)!=digest:raise ValueError('Raw/metadata hash changed')
            if m['batch_number']!=i or m['previous_batch_sha256']!=(result[-1][1]['batch_sha256'] if result else None):raise ValueError('Broken reception batch chain')
            result.append((folder,m))
        return result

    def verify(self,*,anchor=None):
        contract=read(self.root/'contract.json');body={k:v for k,v in contract.items() if k!='contract_sha256'}
        if contract!=self.contract or contract['contract_sha256']!=c.sha_bytes(c.canonical(body)) or contract['version']!=VERSION or contract['plan_sha256']!=c.sha_file(PLAN):raise ValueError('Frozen receiver contract drift')
        if contract['candidate_registration_enabled'] or contract['active_trading_hypotheses']!=0:raise ValueError('No trading hypothesis authorized')
        self.registry.verify(anchor=contract['registry_genesis']);records=self.registry._records();index={r['record_sha256']:r for r in records}
        if any(r['kind'] in ('candidates','labels') for r in records):raise ValueError('No candidate/price label generation authorized in data-only receiver')
        batches=self._batches();cp=contract['registry_genesis'];bound=set();receipts=0;processed=[];last_good=c.timestamp(contract['receiver_started_at'])
        for folder,m in batches:
            if m['contract_sha256']!=contract['contract_sha256'] or m['registry_before']!=cp:raise ValueError('Registry and raw batches diverged')
            self.registry.verify(anchor=m['registry_after']);cp=m['registry_after']
            metas={}
            for file in sorted(folder.glob('*.receipt.json')):
                meta=read(file);raw=(folder/meta['raw_file']).read_bytes();receipts+=1
                if meta['raw_sha256']!=c.sha_bytes(raw) or meta['raw_bytes']!=len(raw) or meta['url']!=url(meta['endpoint'],meta['symbol'],meta['timeframe']):raise ValueError('Receipt/raw/source mismatch')
                metas[meta['raw_file']]=meta
            summary=read(folder/'summary.json')
            if summary['clock_context_verified']:
                last_good=max([last_good]+[c.timestamp(x['system_received_at']) for x in metas.values()])
            for item in read(folder/'processed.json'):
                processed.append(item)
                if item['status'] not in ('new','duplicate','source_revision'):continue
                meta=metas[item['raw_file']];row=c.load_json((folder/item['raw_file']).read_bytes())[item['row_index']]
                normalized,closed,row_hash=normalize(row,item['symbol'],item['timeframe'],meta,m['closure_reference_server_ms'],item['row_index'])
                if not closed or row_hash!=item['raw_row_sha256'] or normalized!=item['normalized_input']:raise ValueError('Closed-row semantic normalization drift')
                if item['status']=='source_revision':continue
                record=index.get(item['registry_sha256'])
                if record is None or record['kind']!='evidence':raise ValueError('Missing evidence link')
                existing=record['payload']
                for key in ('symbol','timeframe','open_at','close_at','ohlcv'):
                    if existing[key]!=normalized[key]:raise ValueError('Raw-to-registry values drift')
                if item['status']=='new':
                    if existing['source']!=normalized['source'] or c.timestamp(existing['received_at'])<c.timestamp(item['wire_received_at']):raise ValueError('Raw receipt and ingest chronology mismatch')
                    bound.add(record['record_sha256'])
        if cp!=self.registry.checkpoint() or bound!={r['record_sha256'] for r in records if r['kind']=='evidence'}:raise ValueError('Unbound registry advance or missing transport batch')
        checkpoint=dict(version=VERSION,contract_sha256=contract['contract_sha256'],batch_count=len(batches),
            batch_sha256=batches[-1][1]['batch_sha256'] if batches else None,registry_checkpoint=cp)
        if anchor is not None:
            n=anchor['batch_count']
            if not isinstance(n,int) or n>len(batches) or n<0:raise ValueError('Pinned receiver checkpoint truncated')
            expected=dict(version=VERSION,contract_sha256=contract['contract_sha256'],batch_count=n,
                batch_sha256=batches[n-1][1]['batch_sha256'] if n else None,registry_checkpoint=batches[n-1][1]['registry_after'] if n else contract['registry_genesis'])
            if anchor!=expected:raise ValueError('External receiver checkpoint differs')
        return dict(mode=contract['mode'],batches=len(batches),raw_receipts=receipts,normalized_evidence=len(bound),
            candidates=0,labels=0,last_good_wall_ms=last_good,anchor_consistent=anchor is not None,
            external_timestamp_attestation_verified=False,checkpoint=checkpoint)

    def checkpoint(self):return self.verify()['checkpoint']

    def processed_rows(self):
        self.verify();return [r for folder,_ in self._batches() for r in read(folder/'processed.json')]

    @contextmanager
    def _writer(self):
        try:(self.root/'.receiver-lock').mkdir()
        except FileExistsError as exc:raise ValueError('Receiver active or stale lock; fail closed') from exc
        try:yield
        finally:(self.root/'.receiver-lock').rmdir()

    def collect(self,*,include_liquidity=True):
        with self._writer():
            v=self.verify()
            history=self.processed_rows()
            if self.contract['receiver_code_sha256']!=c.sha_file(Path(__file__)):raise ValueError('Receiver writer version drift; freeze a new receiver version')
            stage=self.root/('.pending-'+uuid.uuid4().hex);stage.mkdir();items=[];metas=[];flags=[]
            summary=dict(status='ok',new_evidence=0,duplicate_rows=0,revision_rows=0,open_rows=0,invalid_rows=0,
                zero_volume_rows=0,gap_intervals=0,failed_requests=0,liquidity_snapshots=0,clock_context_verified=False,
                quality_flags=flags,actual_execution_verified=False,active_trading_hypotheses=0)
            def get(endpoint,symbol=None,frame=None):
                raw,meta=transport(endpoint,symbol,frame,clock=self.clock,opener=self.opener,session_id=self.session_id)
                name=f'wire-{len(metas):03d}';meta['raw_file']=name+'.body'
                with (stage/meta['raw_file']).open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
                (stage/meta['raw_file']).chmod(0o444);write(stage/(name+'.receipt.json'),meta);metas.append(meta)
                if meta['http_status']!=200 or meta['transport_error'] or meta['response_truncated']:summary['failed_requests']+=1
                return raw,meta
            server_ms=None
            if self.clock()<v['last_good_wall_ms']:flags.append('clock_rollback')
            else:
                raw,meta=get('time');server_ms=server_reference(meta,raw)
                if server_ms is None:flags.append('server_clock_unverified')
                else:
                    limited=False
                    for symbol in self.contract['symbols']:
                        for frame in self.contract['frames']:
                            _,cap=get('klines',symbol,frame)
                            if cap['http_status'] in (418,429):limited=True;break
                        if limited:break
                        if include_liquidity:
                            for endpoint in ('bookTicker','depth'):
                                _,cap=get(endpoint,symbol)
                                if cap['http_status'] in (418,429):limited=True;break
                        if limited:break
                    if limited:flags.append('rate_limited')
                    else:
                        end_raw,end_meta=get('time')
                        if server_reference(end_meta,end_raw) is None or not all(m['clock_locally_consistent'] for m in metas):flags.append('server_clock_unverified')
                        else:summary['clock_context_verified']=True
            prior={}
            for x in history:
                if x['status']=='new':prior[(x['symbol'],x['timeframe'],x['open_ms'])]=x
            if summary['clock_context_verified']:
                for meta in metas:
                    if meta['endpoint'] not in ('klines','bookTicker','depth') or meta['http_status']!=200:continue
                    try:data=c.load_json((stage/meta['raw_file']).read_bytes())
                    except ValueError:summary['invalid_rows']+=1;continue
                    if meta['endpoint']!='klines':
                        try:
                            if meta['endpoint']=='bookTicker':
                                if data['symbol']!=meta['symbol']:raise ValueError('Wrong liquidity symbol')
                                copied=dict(data,symbol='BTCUSDT');measure=old_metrics('bookTicker',copied)
                            else:measure=old_metrics('depth',data)
                            items.append(dict(status='liquidity',endpoint=meta['endpoint'],symbol=meta['symbol'],raw_file=meta['raw_file'],
                                wire_received_at=meta['system_received_at'],source_published_at=None,metrics=measure,
                                actual_slippage_bps=None,guaranteed_executable=False));summary['liquidity_snapshots']+=1
                        except (ValueError,KeyError,TypeError):summary['invalid_rows']+=1
                        continue
                    if not isinstance(data,list):summary['invalid_rows']+=1;continue
                    for i,row in enumerate(data):
                        item=dict(status='invalid',symbol=meta['symbol'],timeframe=meta['timeframe'],raw_file=meta['raw_file'],row_index=i,
                            wire_received_at=meta['system_received_at'],source_published_at=None)
                        try:normalized,closed,row_hash=normalize(row,meta['symbol'],meta['timeframe'],meta,server_ms,i)
                        except (ValueError,TypeError,KeyError):summary['invalid_rows']+=1;items.append(item);continue
                        item.update(open_ms=row[0],close_exclusive_ms=row[6]+1,raw_row_sha256=row_hash,normalized_input=normalized)
                        if not closed:item['status']='open_unconfirmed';summary['open_rows']+=1;items.append(item);continue
                        op,cl=row[0],row[6]+1;lag=c.timestamp(meta['system_received_at'])-cl
                        item.update(closure_confirmed_at=meta['system_received_at'],collection_lag_ms=lag,
                            availability_class='bootstrap_backfill' if cl<=c.timestamp(self.contract['receiver_started_at']) else ('late_catchup' if lag>300000 else 'post_start_observation'))
                        key=(meta['symbol'],meta['timeframe'],op);old=prior.get(key)
                        if old:
                            item['registry_sha256']=old['registry_sha256'];item['status']='duplicate' if row_hash==old['raw_row_sha256'] else 'source_revision'
                            summary['duplicate_rows' if item['status']=='duplicate' else 'revision_rows']+=1
                        else:
                            existing=[x for k,x in prior.items() if k[:2]==key[:2]]
                            if existing and op>max(x['close_exclusive_ms'] for x in existing):summary['gap_intervals']+=1
                            record=self.registry.ingest(normalized);item.update(status='new',registry_sha256=record['record_sha256'],registry_ingested_at=record['recorded_at'])
                            summary['new_evidence']+=1;prior[key]=item
                        if c.number(row[5])==0:summary['zero_volume_rows']+=1
                        items.append(item)
            if flags or summary['failed_requests'] or summary['invalid_rows'] or summary['revision_rows'] or summary['gap_intervals']:summary['status']='partial'
            write(stage/'processed.json',items);write(stage/'summary.json',summary)
            number=v['batches']+1;m=dict(version=VERSION,batch_number=number,mode=self.contract['mode'],contract_sha256=self.contract['contract_sha256'],
                previous_batch_sha256=v['checkpoint']['batch_sha256'],registry_before=v['checkpoint']['registry_checkpoint'],registry_after=self.registry.checkpoint(),
                closure_reference_server_ms=server_ms,observed_batch_finish_at=c.utc(self.clock()),candidate_hashes=[],
                files_sha256={p.name:c.sha_file(p) for p in sorted(stage.iterdir())})
            m['batch_sha256']=c.sha_bytes(c.canonical(m));write(stage/'manifest.json',m)
            dest=self.root/'batches'/f'{number:010d}'
            if dest.exists():raise ValueError('Refuse batch overwrite')
            stage.rename(dest);fd=os.open(self.root/'batches',os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
            self.verify();return summary

    def projections(self):
        self.verify();rows=self.processed_rows();out={}
        for symbol in self.contract['symbols']:
            for frame in self.contract['frames']:
                local=[r for r in rows if r.get('symbol')==symbol and r.get('timeframe')==frame and r['status']=='new'];flags=[]
                if not local:out[symbol+'/'+frame]=dict(close=None,data_quality_ready=False,quality_flags=['unknown_no_verified_closed_data'],candidate_registration_enabled=False);continue
                latest=max(local,key=lambda r:r['open_ms']);opens=sorted({r['open_ms'] for r in local})
                if any(b-a!=c.FRAMES[frame] for a,b in zip(opens,opens[1:])):flags.append('missing_candles')
                if any(r.get('symbol')==symbol and r.get('timeframe')==frame and r['status']=='source_revision' for r in rows):flags.append('source_revision')
                if c.number(latest['normalized_input']['ohlcv']['volume'])==0:flags.append('zero_volume')
                if latest['collection_lag_ms']>300000:flags.append('collection_lag_exceeds_5min')
                if latest['availability_class']=='bootstrap_backfill':flags.append('bootstrap_not_contemporaneous')
                record=self.registry.get(latest['registry_sha256'])
                out[symbol+'/'+frame]=dict(close=float(c.number(latest['normalized_input']['ohlcv']['close'])),close_at=latest['normalized_input']['close_at'],
                    evidence_sha256=latest['registry_sha256'],wire_received_at=latest['wire_received_at'],registry_ingested_at=record['recorded_at'],
                    semantic_validation='raw-row close equals normalized evidence; no other feature formula certified',
                    quality_flags=flags,data_quality_ready=not flags,candidate_registration_enabled=False)
        return out

    def snapshot(self,target):
        self.verify()
        with ZipFile(target,'x',compression=ZIP_DEFLATED) as z:
            for p in sorted(self.root.rglob('*')):
                if p.is_file():z.write(p,str(p.relative_to(self.root)))
        return c.sha_file(Path(target))

    @classmethod
    def restore(cls,archive,dest,*,expected_sha256,anchor,clock=None,opener=None,expected_mode=None):
        archive,dest=Path(archive),Path(dest)
        if dest.exists() or c.sha_file(archive)!=expected_sha256:raise ValueError('Restore needs new directory and verified external snapshot SHA')
        with ZipFile(archive) as z:
            names=z.namelist()
            if len(names)!=len(set(names)) or any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts or '\\' in n for n in names):raise ValueError('Unsafe snapshot paths')
            dest.mkdir(parents=True)
            for info in z.infolist():
                if info.is_dir() or ((info.external_attr>>16)&0o170000)==0o120000 or info.file_size>MAX_BYTES*3:raise ValueError('Unexpected snapshot entry')
                p=dest/PurePosixPath(info.filename);p.parent.mkdir(parents=True,exist_ok=True)
                with p.open('xb') as f:f.write(z.read(info))
                p.chmod(0o444)
        r=cls(dest,clock=clock,opener=opener,expected_mode=expected_mode);r.verify(anchor=anchor);return r


def cli(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['init','collect','verify','checkpoint','projections','snapshot'])
    p.add_argument('--root',type=Path,required=True);p.add_argument('--out',type=Path);p.add_argument('--anchor',type=Path)
    a=p.parse_args(argv)
    if a.command=='init':r=Receiver.create(a.root,mode='real_public');result=r.verify()
    else:
        r=Receiver(a.root)
        if a.command=='collect':result=r.collect()
        elif a.command=='verify':result=r.verify(anchor=read(a.anchor) if a.anchor else None)
        elif a.command=='snapshot':result={'snapshot_sha256':r.snapshot(a.out)}
        else:result=getattr(r,a.command)()
    if a.out and a.command!='snapshot':write(a.out,result)
    else:print(json.dumps(result,indent=2,allow_nan=False))
    return 0


if __name__=='__main__':raise SystemExit(cli())
