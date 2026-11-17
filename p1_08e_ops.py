#!/usr/bin/env python3
"""Supervised data operations with one remote Git authority; never orders.

Ordinary fast-forward pushes, no permission changes or force push. A lease is
kept until a separately fetched copy verifies. Git is not WORM/notarization.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import socket
import subprocess
import tempfile
import uuid

from p1_08d_receiver import Receiver,read,write,c

PLAN=Path(__file__).with_name('P1_08E_PLAN.json')
VERSION='p1-08e-ops-v1'


class GitArchive:
    def __init__(self,cache,remote,branch):
        if not re.fullmatch(r'research/p1-08e-[a-z0-9-]+',branch):raise ValueError('Only isolated P1-08E data branches; never main')
        self.cache=Path(cache);self.remote=str(remote);self.branch=branch;self.ref='refs/heads/'+branch
        if not self.cache.exists():
            self.cache.mkdir(parents=True);subprocess.run(['git','init','--bare','--quiet',str(self.cache)],check=True)
            self.git(['config','user.name','BTC Market Lab supervised data archive'])
            self.git(['config','user.email','btc-market-lab-research@users.noreply.github.com'])

    def git(self,args,*,data=None,env=None):
        p=subprocess.run(['git','--git-dir='+str(self.cache)]+args,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        if p.returncode:raise ValueError('Git '+args[0]+' failed; stop and retain local diagnostics')
        return p.stdout

    def head(self):
        result=self.git(['ls-remote','--refs',self.remote,self.ref]).decode().strip()
        if not result:return None
        expected=result.split()[0]
        self.git(['fetch','--quiet','--no-tags',self.remote,self.ref])
        actual=self.git(['rev-parse','FETCH_HEAD']).decode().strip()
        if actual!=expected:raise ValueError('Archive moved during fetch; no collection allowed')
        return actual

    def catalog(self,head):return c.load_json(self.git(['show',head+':catalog.json']))

    def files(self,head):
        names=self.git(['ls-tree','-r','--name-only',head]).decode().splitlines()
        return {name:self.git(['show',head+':'+name]) for name in names}

    def publish(self,parent,catalog,*,state=None,message='P1-08E supervised data checkpoint'):
        if self.head()!=parent:raise ValueError('Stale remote parent: no fork or overwrite')
        files=self.files(parent) if parent else {}
        if state is not None:
            for p in sorted(Path(state).rglob('*')):
                if p.is_symlink():raise ValueError('No symlink in archived state')
                if not p.is_file():continue
                name='state/'+str(p.relative_to(state));raw=p.read_bytes()
                if name in files and files[name]!=raw:raise ValueError('Immutable raw/operation file changed')
                files[name]=raw
        inv={name:c.sha_bytes(raw) for name,raw in files.items() if name.startswith('state/')}
        catalog=dict(catalog,files_sha256=inv);files['catalog.json']=c.canonical(catalog)+b'\n'
        with tempfile.TemporaryDirectory(prefix='p108e-index-',dir=self.cache.parent) as d:
            env=dict(os.environ,GIT_INDEX_FILE=str(Path(d)/'index'))
            self.git(['read-tree','--empty'],env=env)
            entries=[]
            for name,raw in sorted(files.items()):
                path=PurePosixPath(name)
                if path.is_absolute() or '..' in path.parts or '\\' in name or '\n' in name:raise ValueError('Unsafe Git archive path')
                blob=self.git(['hash-object','-w','--stdin'],data=raw).decode().strip()
                entries.append('100644 '+blob+'\t'+name+'\n')
            self.git(['update-index','--index-info'],data=''.join(entries).encode(),env=env)
            tree=self.git(['write-tree'],env=env).decode().strip()
        args=['commit-tree',tree]+(['-p',parent] if parent else [])
        commit=self.git(args,data=(message+'\n').encode()).decode().strip()
        # The server rejects concurrent non-fast-forward publications. Never use --force.
        self.git(['push','--quiet',self.remote,commit+':'+self.ref])
        if self.head()!=commit:raise ValueError('Remote publication not the latest acknowledged head')
        return commit

    def export(self,head,dest):
        dest=Path(dest)
        if dest.exists():raise ValueError('Restore must use a new directory')
        files=self.files(head);catalog=c.load_json(files['catalog.json'])
        if {k for k in files if k.startswith('state/')}!=set(catalog['files_sha256']):raise ValueError('Remote file inventory mismatch')
        dest.mkdir(parents=True)
        for name,digest in catalog['files_sha256'].items():
            path=PurePosixPath(name)
            if not name.startswith('state/') or path.is_absolute() or '..' in path.parts or '\\' in name:raise ValueError('Unsafe restore path')
            raw=files[name]
            if c.sha_bytes(raw)!=digest:raise ValueError('Remote raw hash mismatch')
            p=dest/Path(*path.parts[1:]);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw);p.chmod(0o444)
        # Git does not store empty directories; these are required by the unchanged C schema.
        for kind in c.KINDS:(dest/'receiver/registry'/kind).mkdir(parents=True,exist_ok=True)
        (dest/'receiver/batches').mkdir(parents=True,exist_ok=True)
        (dest/'events').mkdir(exist_ok=True)
        Receiver(dest/'receiver').verify(anchor=catalog['receiver_checkpoint'])
        return catalog

    def independent_readback(self,head):
        with tempfile.TemporaryDirectory(prefix='p108e-independent-') as d:
            other=GitArchive(Path(d)/'objects.git',self.remote,self.branch)
            if other.head()!=head:raise ValueError('Remote head changed during independent readback')
            cat=other.export(head,Path(d)/'state')
            return dict(remote_head=head,verified_files=len(cat['files_sha256']),receiver_checkpoint=cat['receiver_checkpoint'],
                verification='fresh Git object store fetched from remote; not timestamp notarization',external_timestamp_attestation_verified=False)


class Supervisor:
    def __init__(self,root,*,clock=None,opener=None):
        self.root=Path(root);self.local=read(self.root/'local.json');self.branch=self.local['branch']
        if self.local['mode']!='synthetic' and (clock is not None or opener is not None):raise ValueError('No injected clock/transport in real operation')
        self.clock=clock or c.system_ms;self.opener=opener
        self.archive=GitArchive(self.root/'archive-cache.git',self.local['remote'],self.branch)

    @staticmethod
    def _check_remote(remote,mode):
        if mode not in ('synthetic','real_public'):raise ValueError('Explicit real/synthetic mode')
        if mode=='real_public' and remote!='https://github.com/danielwang0918-design/btc-market-lab.git':raise ValueError('Use the existing authorized repo only; no new service or credentials')

    @classmethod
    def create(cls,root,*,remote,branch,snapshot,snapshot_sha256,checkpoint,mode,clock=None,opener=None):
        cls._check_remote(remote,mode);root=Path(root)
        if root.exists():raise ValueError('New durable state root required')
        if mode!='synthetic' and (clock is not None or opener is not None):raise ValueError('No fake real I/O')
        root.mkdir(parents=True);(root/'state/events').mkdir(parents=True);(root/'diagnostics').mkdir()
        archive=GitArchive(root/'archive-cache.git',remote,branch)
        if archive.head() is not None:raise ValueError('Archive already exists; restore actual latest head instead')
        receiver=Receiver.restore(snapshot,root/'state/receiver',expected_sha256=snapshot_sha256,anchor=checkpoint,
            clock=clock,opener=opener,expected_mode=mode)
        now=(clock or c.system_ms)()
        config=dict(version=VERSION,mode=mode,created_at=c.utc(now),base_commit='c3b27a6aaf33b8291b9b3c7559521433c3f44b99',
            plan_sha256=c.sha_file(PLAN),retention_until=c.utc(c.add_months(now,27)),external_timestamp_attestation_verified=False)
        write(root/'state/config.json',config)
        catalog=dict(version=VERSION,mode=mode,generation=0,lease=None,status='ready',trial_started_at=None,
            receiver_checkpoint=receiver.checkpoint(),retention_until=config['retention_until'],last_backup_verified_at=c.utc(now),
            external_timestamp_attestation_verified=False)
        head=archive.publish(None,catalog,state=root/'state',message='Initialize isolated P1-08E data archive, no trading hypothesis')
        archive.independent_readback(head)
        local=dict(remote=remote,branch=branch,mode=mode,head=head,root_pin=head)
        (root/'local.json').write_bytes(c.canonical(local)+b'\n')
        return cls(root,clock=clock,opener=opener)

    @classmethod
    def restore(cls,root,*,remote,branch,expected_head,mode,clock=None,opener=None,root_pin=None):
        cls._check_remote(remote,mode);root=Path(root)
        if root.exists():raise ValueError('Never overwrite a local state during recovery')
        root.mkdir(parents=True);(root/'diagnostics').mkdir();archive=GitArchive(root/'archive-cache.git',remote,branch)
        if archive.head()!=expected_head:raise ValueError('Provided parent is not actual latest remote head')
        catalog=archive.export(expected_head,root/'state')
        if catalog['mode']!=mode:raise ValueError('No real/synthetic mixing')
        pin=root_pin or archive.git(['rev-list','--max-parents=0',expected_head]).decode().strip()
        local=dict(remote=remote,branch=branch,mode=mode,head=expected_head,root_pin=pin)
        (root/'local.json').write_bytes(c.canonical(local)+b'\n')
        return cls(root,clock=clock,opener=opener)

    def receiver(self):return Receiver(self.root/'state/receiver',clock=self.clock if self.local['mode']=='synthetic' else None,opener=self.opener,expected_mode=self.local['mode'])
    def head(self):return self.archive.head()

    def _cache(self,head):
        self.local['head']=head;tmp=self.root/'local.json.tmp';tmp.write_bytes(c.canonical(self.local)+b'\n');tmp.replace(self.root/'local.json')

    def diagnostic(self,kind,reason):
        event=dict(event_id=str(uuid.uuid4()),observed_at=c.utc(self.clock()),kind=kind,reason=str(reason),
            mode=self.local['mode'],recovery_action='Stop; preserve local state and verify latest remote before explicit recovery')
        write(self.root/'diagnostics'/(event['event_id']+'.json'),event);return event

    @contextmanager
    def _lock(self):
        with (self.root/'operation.lock').open('a+b') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as exc:raise ValueError('Another local supervisor owns the operation lock') from exc
            try:yield
            finally:fcntl.flock(lock,fcntl.LOCK_UN)

    def _guard(self,expected_head,*,allow_lease=False):
        actual=self.head()
        if actual!=expected_head or actual!=self.local['head']:raise ValueError('Stale parent/local state; stop before public data I/O')
        self.archive.git(['merge-base','--is-ancestor',self.local['root_pin'],actual])
        cat=self.archive.catalog(actual)
        if cat['lease'] is not None and not allow_lease:raise ValueError('Global collection lease already held; no second collector')
        if cat['status']!='ready' and not allow_lease:raise ValueError('Unresolved operational condition; operator review required')
        config=read(self.root/'state/config.json')
        if config['plan_sha256']!=c.sha_file(PLAN):raise ValueError('Frozen operational plan drift')
        verified=self.receiver().verify(anchor=cat['receiver_checkpoint'])
        if verified['checkpoint']!=cat['receiver_checkpoint']:raise ValueError('Unpublished local receiver advance; recover explicitly, never adopt a hidden fork')
        if self.clock()<verified['last_good_wall_ms']:raise ValueError('Clock rollback before collection')
        local_files={'state/'+str(p.relative_to(self.root/'state')) for p in (self.root/'state').rglob('*') if p.is_file()}
        if local_files!=set(cat['files_sha256']):raise ValueError('Unpublished/unknown local files; stop and audit')
        for name,digest in cat['files_sha256'].items():
            path=self.root/name
            if not path.is_file() or path.is_symlink() or c.sha_file(path)!=digest:raise ValueError('Local raw/checkpoint inventory drift')
        if shutil.disk_usage(self.root).free<268435456:raise ValueError('Low disk: stop before receiving more data')
        return cat

    def reserve(self,*,expected_head):
        cat=self._guard(expected_head);owner=dict(id=str(uuid.uuid4()),pid=os.getpid(),host=socket.gethostname(),acquired_at=c.utc(self.clock()),phase='collecting')
        cat.update(lease=owner,status='lease_held')
        head=self.archive.publish(expected_head,cat,message='Reserve one supervised collection lease')
        self._cache(head);return head

    def _finish(self,parent,cat,*,event,status):
        if sum(p.stat().st_size for p in (self.root/'state').rglob('*') if p.is_file())>536870912:raise ValueError('Archive payload capacity exceeded; no unapproved storage expansion')
        cat.update(status='backup_verification_pending');cat['receiver_checkpoint']=self.receiver().checkpoint()
        staged=self.archive.publish(parent,cat,state=self.root/'state',message='Stage received raw data under lease for remote readback')
        self._cache(staged)
        proof=self.archive.independent_readback(staged)
        generation=cat['generation']+1;event=dict(event,generation=generation,observed_at=c.utc(self.clock()),remote_readback=proof,
            ops_code_sha256=c.sha_file(Path(__file__)),mode=self.local['mode'],external_timestamp_attestation_verified=False)
        write(self.root/'state/events'/f'{generation:010d}.json',event)
        cat.update(generation=generation,lease=None,status=status,last_backup_verified_at=c.utc(self.clock()))
        final=self.archive.publish(staged,cat,state=self.root/'state',message='Complete verified supervised data operation')
        self._cache(final);return final,proof

    def cycle(self,*,expected_head):
        with self._lock():
            try:
                lease=self.reserve(expected_head=expected_head);cat=self.archive.catalog(lease)
                summary=self.receiver().collect()
                status='ready' if summary['status']=='ok' else 'blocked_quality'
                final,proof=self._finish(lease,cat,event=dict(kind='collection',parent_head=expected_head,summary=summary),status=status)
                return dict(status='ok' if status=='ready' else status,parent_head=expected_head,published_head=final,
                    remote_readback_verified=True,receiver_checkpoint=proof['receiver_checkpoint'],summary=summary,
                    external_timestamp_attestation_verified=False)
            except Exception as exc:
                self.diagnostic('operation_failed',type(exc).__name__+': '+str(exc));raise

    def recover_stopped(self,*,expected_head,death_evidence):
        with self._lock():
            cat=self._guard(expected_head,allow_lease=True)
            if cat['lease'] is None or death_evidence.get('process_dead') is not True or not isinstance(death_evidence.get('returncode'),int) or death_evidence['returncode']>=0:
                raise ValueError('Cannot clear a lease without explicit stopped-process evidence')
            if self.local['mode']=='real_public' and death_evidence.get('pid')!=cat['lease']['pid']:raise ValueError('Stopped PID is not the lease owner')
            if self.local['mode']=='real_public' and cat['lease']['host']==socket.gethostname():
                try:os.kill(cat['lease']['pid'],0)
                except ProcessLookupError:pass
                else:raise ValueError('Lease-owner PID is still alive; do not steal lease')
            final,_=self._finish(expected_head,cat,event=dict(kind='controlled_recovery',death_evidence=death_evidence),status='ready')
            return final

    def start_trial(self,*,expected_head,operator_id,durable_host,persistence_confirmed):
        if persistence_confirmed is not True or not operator_id.strip() or not durable_host.strip():raise ValueError('Explicit operator and durable host attestation required')
        with self._lock():
            cat=self._guard(expected_head)
            if cat['trial_started_at'] is not None:raise ValueError('Trial already started; never backdate or restart its clock')
            lease=self.reserve(expected_head=expected_head);cat=self.archive.catalog(lease)
            cat['trial_started_at']=c.utc(self.clock())
            final,_=self._finish(lease,cat,event=dict(kind='pilot_activation',operator_id=operator_id,durable_host=durable_host,
                persistence_is_operator_attested_not_platform_certified=True,trial_started_at=cat['trial_started_at']),status='ready')
            return final

    def record_daily_report(self,*,expected_head,day):
        from p1_08e_monitor import daily_report
        with self._lock():
            self._guard(expected_head);report=daily_report(self,day)
            lease=self.reserve(expected_head=expected_head);cat=self.archive.catalog(lease)
            final,_=self._finish(lease,cat,event=dict(kind='daily_report',report=report),status='ready')
            return final,report

    def status(self):
        from p1_08e_monitor import seven_day_status
        head=self.head();cat=self.archive.catalog(head)
        s=seven_day_status(cat['trial_started_at'],self.clock())
        return dict(remote_head=head,operation_status=cat['status'],seven_day_status=s['status'],level2_pass=False,
            external_timestamp_attestation_verified=False,receiver_checkpoint=cat['receiver_checkpoint'])


def cli(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['cycle','status','restore'])
    p.add_argument('--root',type=Path,required=True);p.add_argument('--expected-head');p.add_argument('--remote');p.add_argument('--branch')
    a=p.parse_args(argv)
    if a.command=='restore':o=Supervisor.restore(a.root,remote=a.remote,branch=a.branch,expected_head=a.expected_head,mode='real_public');result=o.status()
    else:
        o=Supervisor(a.root);result=o.cycle(expected_head=a.expected_head) if a.command=='cycle' else o.status()
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':raise SystemExit(cli())
