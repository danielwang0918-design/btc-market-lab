#!/usr/bin/env python3
"""Small local research journal. No network, strategy, account access or orders.

Hash integrity is not external timestamp attestation. See P1_08C_METHODS.md.
Only the synthetic mode accepts an injected clock. All values remain research
inputs/labels, never a validated profitable signal or verified live fill.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import uuid

from prospective_validation import sha_bytes, sha_file, utc

VERSION = 'p1-08c-journal-v1'
FRAMES = {'1h':3_600_000, '4h':14_400_000, '1d':86_400_000}
SYMBOLS = ('BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','ZECUSDT','NEARUSDT','DOGEUSDT')
KINDS = ('protocols','evidence','candidates','labels')
MODES = ('synthetic','prospective','historical_import')
PROTOCOL_KEYS = {'study_key','hypothesis','rule','study_start_at','review_dates','arms','features',
    'label_names','label_calculator_sha256','horizons_hours','observation_anchor','costs'}
CANDIDATE_KEYS = {'market_event_key','symbol','timeframe','formed_at','confirmed_at','state','stance','arm',
    'observation_start_at','evidence_ids','features','quality','previous_candidate_sha256'}
LABEL_KEYS = {'candidate_sha256','horizon_hours','values','status','evidence_ids','intrabar_order','limitations'}


def system_ms():
    return int(datetime.now(timezone.utc).timestamp()*1000)


def timestamp(value):
    if not isinstance(value,str):raise ValueError('Timestamp must be explicit UTC ISO8601')
    try:dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError as exc:raise ValueError('Invalid UTC timestamp') from exc
    if dt.tzinfo is None or dt.utcoffset().total_seconds()!=0 or dt.microsecond%1000:
        raise ValueError('UTC timezone and millisecond-or-coarser precision required')
    return int(dt.timestamp()*1000)


def canonical(obj):
    try:return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (TypeError,ValueError) as exc:raise ValueError('Finite JSON values required') from exc


def load_json(value):
    def unique(pairs):
        result={}
        for key,val in pairs:
            if key in result:raise ValueError('Duplicate JSON key')
            result[key]=val
        return result
    def constant(_):raise ValueError('Nonfinite JSON constant')
    try:return json.loads(value,object_pairs_hook=unique,parse_constant=constant)
    except (UnicodeError,json.JSONDecodeError) as exc:raise ValueError('Invalid JSON') from exc


def exact(obj,keys):
    if not isinstance(obj,dict) or set(obj)!=set(keys):raise ValueError('Missing/unknown fields: expected '+str(sorted(keys)))


def text(value):
    if not isinstance(value,str) or not value.strip() or len(value)>4000:raise ValueError('Nonempty bounded text required')
    return value


def digest(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise ValueError('Expected SHA256')
    return value


def strings(value):
    if not isinstance(value,list) or any(not isinstance(x,str) or not x for x in value) or len(value)!=len(set(value)):
        raise ValueError('Unique string list required')
    return value


def number(value):
    if isinstance(value,bool) or not isinstance(value,(str,int,float)):raise ValueError('Finite numeric value required')
    try:d=Decimal(str(value))
    except InvalidOperation as exc:raise ValueError('Invalid number') from exc
    if not d.is_finite():raise ValueError('Finite number required')
    return d


def add_months(ms,months):
    dt=datetime.fromtimestamp(ms/1000,timezone.utc)
    y,m=divmod(dt.year*12+dt.month-1+months,12)
    # The study schedule uses a first-of-month start to make month review exact.
    return int(dt.replace(year=y,month=m+1).timestamp()*1000)


def validate_protocol(p,now,mode):
    exact(p,PROTOCOL_KEYS);text(p['study_key'])
    exact(p['hypothesis'],{'id','statement','falsification'})
    for val in p['hypothesis'].values():text(val)
    exact(p['rule'],{'id','version','code_sha256'});text(p['rule']['id']);text(p['rule']['version']);digest(p['rule']['code_sha256'])
    start=timestamp(p['study_start_at'])
    if mode!='historical_import' and start<=now:raise ValueError('Study start must follow protocol freeze')
    dt=datetime.fromtimestamp(start/1000,timezone.utc)
    if (dt.day,dt.hour,dt.minute,dt.second,dt.microsecond)!=(1,0,0,0,0):raise ValueError('Study starts at a UTC month boundary')
    reviews=[timestamp(x) for x in p['review_dates']]
    if not reviews or reviews!=sorted(set(reviews)) or reviews[0]<=start or reviews[-1]<add_months(start,24):
        raise ValueError('Fixed review dates must cover at least 24 calendar months')
    strings(p['arms'])
    if not p['arms']:raise ValueError('Research arms required')
    if not isinstance(p['features'],dict):raise ValueError('Feature whitelist required')
    for name,spec in p['features'].items():
        if not re.fullmatch('[a-z][a-z0-9_]{0,79}',name) or re.search('mfe|mae|pnl|outcome|exit|final_status|future',name):
            raise ValueError('Post-hoc/reserved feature name')
        exact(spec,{'definition','code_sha256','dtype','lookback_bars'})
        text(spec['definition']);digest(spec['code_sha256'])
        if spec['dtype'] not in ('number','category','boolean'):raise ValueError('Feature dtype')
        if not isinstance(spec['lookback_bars'],dict) or not spec['lookback_bars']:raise ValueError('Explicit feature input frames required')
        for frame,n in spec['lookback_bars'].items():
            if frame not in FRAMES or type(n) is not int or not 1<=n<=10000:raise ValueError('Closed 1H+ lookback required')
    strings(p['label_names'])
    if not p['label_names'] or set(p['features'])&set(p['label_names']):raise ValueError('Separate feature/label names required')
    for name in p['label_names']:text(name)
    digest(p['label_calculator_sha256'])
    horizons=p['horizons_hours']
    if not isinstance(horizons,list) or not horizons or any(type(h) is not int or not 1<=h<=168 for h in horizons) or horizons!=sorted(set(horizons)):
        raise ValueError('Frozen unique 1..168 hour windows required')
    if p['observation_anchor']!='next_1h_boundary_after_registration':raise ValueError('Observation anchor is fixed')
    if p['costs']!=dict(fee_bps_per_side=10,slippage_bps_per_side=[5,15],paper_initial_USDT=1000,risk_fraction=0.02):
        raise ValueError('Cost/budget planning baseline changed')


def evidence_payload(c,now,records):
    exact(c,{'symbol','timeframe','open_at','close_at','ohlcv','source'})
    symbol,frame=c['symbol'],c['timeframe']
    if symbol not in SYMBOLS or frame not in FRAMES:raise ValueError('Wrong symbol or decision timeframe')
    op,cl=timestamp(c['open_at']),timestamp(c['close_at'])
    if op%FRAMES[frame] or cl!=op+FRAMES[frame] or cl>now:raise ValueError('Unclosed, unaligned or invalid candle')
    exact(c['ohlcv'],{'open','high','low','close','volume','quote_volume'})
    o,h,l,z,v=[number(c['ohlcv'][key]) for key in ('open','high','low','close','volume')]
    if not 0<l<=min(o,z)<=max(o,z)<=h or v<0:raise ValueError('Invalid OHLCV geometry')
    q=c['ohlcv']['quote_volume']
    if q is not None and number(q)<0:raise ValueError('Negative quote volume')
    source=c['source'];exact(source,{'id','uri','market_event_at','published_at','unknown_reason'})
    text(source['id']);text(source['uri'])
    if source['published_at'] is None:text(source['unknown_reason'])
    elif not cl<=timestamp(source['published_at'])<=now:raise ValueError('Invalid final candle publication clock')
    if source['market_event_at'] is not None and timestamp(source['market_event_at'])>now:raise ValueError('Future market event')
    key=(symbol,frame,op,source['id'])
    if any(r['kind']=='evidence' and (r['payload']['symbol'],r['payload']['timeframe'],timestamp(r['payload']['open_at']),r['payload']['source']['id'])==key for r in records):
        raise ValueError('Duplicate candle evidence; corrections require a separately identified source revision')
    return dict(**deepcopy(c),received_at=utc(now),normalized_input_sha256=sha_bytes(canonical(c)),
        quality_flags=(['zero_volume'] if v==0 else [])
            +(['source_publication_time_unknown'] if source['published_at'] is None else [])
            +(['source_market_event_time_unknown'] if source['market_event_at'] is None else [])
            +(['quote_volume_unknown'] if q is None else []))


def references(ids,index,*,kind='evidence',symbol=None):
    strings(ids);rows=[]
    for rid in ids:
        digest(rid);record=index.get(rid)
        if record is None or record['kind']!=kind:raise ValueError('Missing, future or wrong-kind reference')
        p=record['payload']
        if symbol is not None and p['symbol']!=symbol:raise ValueError('Cross-symbol evidence')
        rows.append(p)
    return rows


def consecutive(rows,frame,n):
    local=sorted((r for r in rows if r['timeframe']==frame),key=lambda r:timestamp(r['open_at']))
    return (len(local)>=n and len({r['open_at'] for r in local})==len(local)
        and all(timestamp(b['open_at'])==timestamp(a['close_at']) for a,b in zip(local,local[1:]))
        and all('zero_volume' not in r['quality_flags'] for r in local))


def candidate_payload(c,now,records,p,mode):
    exact(c,CANDIDATE_KEYS);canonical(c)
    symbol,frame=c['symbol'],c['timeframe']
    if symbol not in SYMBOLS or frame not in FRAMES or c['arm'] not in p['arms']:raise ValueError('Unknown candidate universe/arm')
    formed=timestamp(c['formed_at']);start=timestamp(p['study_start_at'])
    if formed<start or formed>now:raise ValueError('Formation must be in study and known at registration')
    if timestamp(c['observation_start_at'])!=(now//FRAMES['1h']+1)*FRAMES['1h']:
        raise ValueError('Candidate must freeze before fixed future observation window')
    state,stance=c['state'],c['stance']
    if state not in ('pending','confirmed','invalid') or stance not in ('long','short','watch'):raise ValueError('Unknown state/stance')
    if state!='confirmed' and (stance!='watch' or c['confirmed_at'] is not None):raise ValueError('Unconfirmed/invalid stays watch, never backfill confirmation')
    if state=='confirmed' and c['confirmed_at'] is None:raise ValueError('Confirmed candle required')
    index={r['record_sha256']:r for r in records}
    inputs=references(c['evidence_ids'],index,symbol=symbol)
    if not inputs or not any(r['timeframe']==frame and timestamp(r['close_at'])==formed for r in inputs):
        raise ValueError('Formation must reference a received closed decision-frame candle')
    if state=='confirmed':
        confirmed=timestamp(c['confirmed_at'])
        if not formed<=confirmed<=now or not any(r['timeframe']==frame and timestamp(r['close_at'])==confirmed for r in inputs):
            raise ValueError('Confirmation must already be closed and received')
    if not isinstance(c['features'],dict) or set(c['features'])!=set(p['features']):raise ValueError('Frozen feature whitelist only')
    known=[timestamp(r['received_at']) for r in inputs]
    for name,feature in c['features'].items():
        exact(feature,{'value','known_at','evidence_ids','unknown_reason'})
        if not set(strings(feature['evidence_ids'])).issubset(c['evidence_ids']):raise ValueError('Feature inputs must be part of candidate evidence')
        values=references(feature['evidence_ids'],index,symbol=symbol)
        if feature['value'] is None:
            text(feature['unknown_reason'])
            if feature['known_at'] is not None and timestamp(feature['known_at'])>now:raise ValueError('Future unknown feature clock')
            continue
        if not values:raise ValueError('Known feature needs traceable inputs')
        spec=p['features'][name];dtype=spec['dtype'];value=feature['value']
        if dtype=='number' and (type(value) not in (int,float) or not math.isfinite(value)):raise ValueError('Numeric feature dtype')
        if dtype=='category':text(value)
        if dtype=='boolean' and type(value) is not bool:raise ValueError('Boolean feature dtype')
        available=timestamp(feature['known_at'])
        if not max(timestamp(r['received_at']) for r in values)<=available<=now:raise ValueError('Feature used before actual receipt or after registration')
        if set(r['timeframe'] for r in values)!=set(spec['lookback_bars']) or not all(consecutive(values,f,n) for f,n in spec['lookback_bars'].items()):
            raise ValueError('Insufficient/gapped/zero-volume feature inputs; record unknown instead')
        known.append(available)
    exact(c['quality'],{'data_gaps','delays','unknown_fields'})
    for value in c['quality'].values():strings(value)
    text(c['market_event_key'])
    identity=dict(study_key=p['study_key'],rule=p['rule'],symbol=symbol,timeframe=frame,formed_at=utc(formed),market_event_key=c['market_event_key'])
    event_id=sha_bytes(canonical(identity))
    versions=[r for r in records if r['kind']=='candidates' and r['payload']['event_id']==event_id]
    expected=versions[-1]['record_sha256'] if versions else None
    if c['previous_candidate_sha256']!=expected:raise ValueError('Candidate duplicate or stale revision')
    q=deepcopy(c['quality'])
    if any(r['source']['published_at'] is None for r in inputs) and 'source_publication_time_unknown' not in q['unknown_fields']:
        q['unknown_fields'].append('source_publication_time_unknown')
    for f in sorted({r['timeframe'] for r in inputs}):
        local=[r for r in inputs if r['timeframe']==f]
        if len(local)>1 and not consecutive(local,f,1) and f not in q['data_gaps']:q['data_gaps'].append(f)
    body=deepcopy(c);body['quality']=q
    return dict(**body,event_id=event_id,revision=len(versions)+1,registered_at=utc(now),
        data_available_at=utc(max(known)),registration_delay_ms=now-formed,
        source_rule=p['rule'],label_names=p['label_names'],horizons_hours=p['horizons_hours'],
        feature_calculation_verified=False,research_only=True,mode=mode,
        forward_status='local_unattested_candidate' if mode=='prospective' else 'not_forward_evidence')


def label_payload(x,now,records,p):
    exact(x,LABEL_KEYS);canonical(x)
    index={r['record_sha256']:r for r in records};cid=digest(x['candidate_sha256'])
    candidates=references([cid],index,kind='candidates');c=candidates[0]
    h=x['horizon_hours']
    if type(h) is not int or h not in p['horizons_hours']:raise ValueError('Unregistered horizon')
    start=timestamp(c['observation_start_at']);end=start+h*FRAMES['1h']
    if now<end:raise ValueError('Label window not complete yet')
    if any(r['kind']=='labels' and (r['payload']['candidate_sha256'],r['payload']['horizon_hours'])==(cid,h) for r in records):
        raise ValueError('Label already sealed; no overwrite')
    if not isinstance(x['values'],dict) or set(x['values'])!=set(p['label_names']):raise ValueError('Frozen label keys only')
    if x['status'] not in ('complete','incomplete') or x['intrabar_order'] not in ('unknown','not_assessed'):raise ValueError('No fabricated OHLC intrabar order')
    strings(x['limitations']);observations=references(x['evidence_ids'],index,symbol=c['symbol'])
    if any(r['timeframe']!='1h' or timestamp(r['open_at'])<start or timestamp(r['close_at'])>end for r in observations):
        raise ValueError('Wrong observation tape/window')
    complete=(len(observations)==h and consecutive(observations,'1h',h)
        and min(timestamp(r['open_at']) for r in observations)==start
        and max(timestamp(r['close_at']) for r in observations)==end)
    if x['status']=='complete':
        if not complete or any(v is None for v in x['values'].values()):raise ValueError('Incomplete/zero-volume observations cannot have complete labels')
        for v in x['values'].values():number(v)
    elif any(v is not None for v in x['values'].values()) or not x['limitations']:raise ValueError('Incomplete labels must remain unknown with reasons')
    return dict(**deepcopy(x),observation_start_at=utc(start),observation_end_at=utc(end),
        label_completed_at=utc(now),calculation_code_sha256=p['label_calculator_sha256'],
        calculation_verified=False,ohlc_intrabar_path_unknown=True,research_only=True)


class Registry:
    def __init__(self,root,*,clock=None):
        self.root=Path(root);self.clock=clock
        if self.root.is_symlink():raise ValueError('Symlink registry forbidden')
        if self.root.exists():
            first=self.root/'protocols'/'0000000001.json'
            if first.exists():
                mode=load_json(first.read_bytes()).get('payload',{}).get('mode')
                if clock is not None and mode!='synthetic':raise ValueError('Injected clocks only for synthetic fixtures')

    @classmethod
    def create(cls,root,protocol,*,mode,clock=None):
        if mode not in MODES or (clock is not None and mode!='synthetic'):raise ValueError('Explicit valid mode; synthetic clocks only')
        r=cls(root,clock=clock);now=r._now();validate_protocol(protocol,now,mode)
        if r.root.exists() and any(r.root.iterdir()):raise ValueError('Cannot initialize existing registry')
        r.root.mkdir(parents=True,exist_ok=True)
        for kind in KINDS:(r.root/kind).mkdir()
        payload=dict(protocol=deepcopy(protocol),mode=mode,registry_id=str(uuid.uuid4()),
            frozen_at=utc(now),writer_code_sha256=sha_file(Path(__file__)),
            allowed_symbols=list(SYMBOLS),allowed_timeframes=list(FRAMES),independent_forward_evidence_claim=False)
        r._commit('protocols',payload,now,[])
        return r

    def _now(self):
        now=self.clock() if self.clock is not None else system_ms()
        if type(now) is not int:raise ValueError('Integer UTC millisecond clock required')
        return now

    def _records(self):
        if not self.root.is_dir() or {p.name for p in self.root.iterdir()}-set(KINDS)-{'.write-lock'}:raise ValueError('Unexpected registry root entries')
        records=[]
        for kind in KINDS:
            folder=self.root/kind
            if not folder.is_dir() or folder.is_symlink():raise ValueError('Missing/symlink record area')
            for file in folder.iterdir():
                if file.is_symlink() or not file.is_file() or not re.fullmatch(r'\d{10}\.json',file.name):raise ValueError('Unexpected record file')
                raw=file.read_bytes();r=load_json(raw)
                exact(r,{'version','registry_id','sequence','kind','recorded_at','previous_sha256','payload','record_sha256'})
                if r['kind']!=kind or type(r['sequence']) is not int or file.name!=f"{r['sequence']:010d}.json":raise ValueError('Record location/sequence mismatch')
                body={k:v for k,v in r.items() if k!='record_sha256'}
                if r['record_sha256']!=sha_bytes(canonical(body)) or raw!=canonical(r)+b'\n':raise ValueError('Record hash/canonical bytes changed')
                records.append(r)
        records.sort(key=lambda r:r['sequence'])
        if not records or records[0]['kind']!='protocols' or len([r for r in records if r['kind']=='protocols'])!=1:raise ValueError('Exactly one first frozen protocol required')
        initial=records[0]['payload'];mode=initial['mode']
        exact(initial,{'protocol','mode','registry_id','frozen_at','writer_code_sha256','allowed_symbols','allowed_timeframes','independent_forward_evidence_claim'})
        if (initial['allowed_symbols']!=list(SYMBOLS) or initial['allowed_timeframes']!=list(FRAMES)
                or initial['frozen_at']!=records[0]['recorded_at'] or initial['independent_forward_evidence_claim'] is not False):
            raise ValueError('Frozen first-record contract changed')
        digest(initial['writer_code_sha256']);text(initial['registry_id'])
        if mode not in MODES:raise ValueError('Unknown registry mode')
        if self.clock is not None and mode!='synthetic':raise ValueError('Injected clock on non-synthetic registry')
        protocol=initial['protocol'];validate_protocol(protocol,timestamp(records[0]['recorded_at']),mode)
        for i,r in enumerate(records):
            if r['version']!=VERSION or r['registry_id']!=initial['registry_id'] or r['sequence']!=i+1 or r['previous_sha256']!=(records[i-1]['record_sha256'] if i else None):raise ValueError('Broken sequence or SHA256 chain')
            now=timestamp(r['recorded_at'])
            if i and now<timestamp(records[i-1]['recorded_at']):raise ValueError('Nonmonotonic writer clock')
            if i:
                payload=r['payload']
                if r['kind']=='evidence':
                    input_value={k:payload[k] for k in ('symbol','timeframe','open_at','close_at','ohlcv','source')}
                    expected=evidence_payload(input_value,now,records[:i])
                elif r['kind']=='candidates':
                    expected=candidate_payload({k:payload[k] for k in CANDIDATE_KEYS},now,records[:i],protocol,mode)
                else:expected=label_payload({k:payload[k] for k in LABEL_KEYS},now,records[:i],protocol)
                if payload!=expected:raise ValueError('Record semantics/time fields changed')
        return records

    @contextmanager
    def _writer(self):
        lock=self.root/'.write-lock'
        try:lock.mkdir()
        except FileExistsError as exc:raise ValueError('Writer active or stale lock: fail closed, audit before recovery') from exc
        try:yield
        finally:lock.rmdir()

    def _commit(self,kind,payload,now,records):
        if records and now<timestamp(records[-1]['recorded_at']):raise ValueError('Writer clock rollback')
        r=dict(version=VERSION,registry_id=(records[0]['registry_id'] if records else payload['registry_id']),
            sequence=len(records)+1,kind=kind,recorded_at=utc(now),
            previous_sha256=(records[-1]['record_sha256'] if records else None),payload=payload)
        r['record_sha256']=sha_bytes(canonical(r));folder=self.root/kind
        file=folder/f"{r['sequence']:010d}.json";temp=folder/('.pending-'+uuid.uuid4().hex)
        try:
            with temp.open('xb') as stream:
                stream.write(canonical(r)+b'\n');stream.flush();os.fsync(stream.fileno())
            os.chmod(temp,0o444)
            if kind=='candidates' and self._now()>=timestamp(payload['observation_start_at']):
                raise ValueError('Observation started before local sealing; do not commit')
            os.link(temp,file)  # Atomic, no replacement even if a destination appeared.
            fd=os.open(folder,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        except FileExistsError as exc:raise ValueError('Refusing to overwrite an existing record') from exc
        finally:temp.unlink(missing_ok=True)
        return deepcopy(r)

    def _append(self,kind,value):
        with self._writer():
            records=self._records();now=self._now();initial=records[0]['payload'];p=initial['protocol']
            if now<timestamp(records[-1]['recorded_at']):raise ValueError('Writer clock rollback')
            if kind=='evidence':payload=evidence_payload(value,now,records)
            elif kind=='candidates':payload=candidate_payload(value,now,records,p,initial['mode'])
            else:payload=label_payload(value,now,records,p)
            return self._commit(kind,payload,now,records)

    def ingest(self,value):return self._append('evidence',value)
    def register(self,value):return self._append('candidates',value)
    def label(self,value):return self._append('labels',value)

    def get(self,sha256):
        for r in self._records():
            if r['record_sha256']==sha256:return deepcopy(r)
        raise ValueError('Record not found')

    def checkpoint(self):
        r=self._records()[-1]
        return dict(version=VERSION,registry_id=r['registry_id'],sequence=r['sequence'],
                    record_sha256=r['record_sha256'],recorded_at=r['recorded_at'])

    def verify(self,*,anchor=None):
        records=self._records();initial=records[0]['payload']
        if anchor is not None:
            exact(anchor,{'version','registry_id','sequence','record_sha256','recorded_at'})
            n=anchor['sequence']
            if type(n) is not int or not 1<=n<=len(records):raise ValueError('Pinned checkpoint was truncated')
            r=records[n-1]
            if anchor!=dict(version=VERSION,registry_id=r['registry_id'],sequence=n,record_sha256=r['record_sha256'],recorded_at=r['recorded_at']):raise ValueError('Pinned checkpoint differs')
        return dict(mode=initial['mode'],records=len(records),by_kind=dict(Counter(r['kind'] for r in records)),
            anchor_consistent=anchor is not None,external_timestamp_attestation_verified=False,
            independent_forward_evidence_claim=False,qualified_strategy_count=0)

    def audit(self):
        records=self._records();p=records[0]['payload']['protocol'];first={}
        for r in records:
            if r['kind']=='candidates':first.setdefault(r['payload']['event_id'],r['payload'])
        candidates=list(first.values());pairs=cross=0
        for i,a in enumerate(candidates):
            for b in candidates[i+1:]:
                if max(timestamp(a['observation_start_at']),timestamp(b['observation_start_at']))<min(
                    timestamp(a['observation_start_at'])+max(p['horizons_hours'])*FRAMES['1h'],
                    timestamp(b['observation_start_at'])+max(p['horizons_hours'])*FRAMES['1h']):
                    pairs+=1;cross+=a['symbol']!=b['symbol']
        return dict(unique_events=len(first),candidate_versions=sum(r['kind']=='candidates' for r in records),
            btc_events=sum(c['symbol']=='BTCUSDT' for c in candidates),other_events=sum(c['symbol']!='BTCUSDT' for c in candidates),
            observation_overlap_pairs=pairs,cross_symbol_overlap_pairs=cross,
            nonempty_common_28day_blocks=len({(timestamp(c['observation_start_at'])-timestamp(p['study_start_at']))//(28*24*FRAMES['1h']) for c in candidates}),
            proven_independent_sample_count=None,operational_floor_met=False,
            reason='Planning only; no verified economic labels, no independent-sample or power claim')


def cli(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['init','ingest','register','label','verify','checkpoint','audit'])
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--input',type=Path)
    parser.add_argument('--mode',choices=MODES);parser.add_argument('--anchor',type=Path)
    args=parser.parse_args(argv);value=load_json(args.input.read_bytes()) if args.input else None
    if args.command=='init':r=Registry.create(args.root,value,mode=args.mode);result=r.checkpoint()
    else:
        r=Registry(args.root)
        if args.command=='verify':result=r.verify(anchor=load_json(args.anchor.read_bytes()) if args.anchor else None)
        elif args.command in ('checkpoint','audit'):result=getattr(r,args.command)()
        else:result=getattr(r,args.command)(value)
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False));return 0


if __name__=='__main__':raise SystemExit(cli())
