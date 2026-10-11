"""Retire only this successful backup's six owned working files.

No directory scans across jobs, recursive removal, backup retention, retries or
failure-scene recovery. The caller keeps maintenance lock, ERP stopped, the
authenticated copy and receipt pinned, after both transfer workers exited.
"""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import re

from . import mac_smb_stream_runtime_contract as cfg, mac_smb_stream_contract as archive
from .mac_service_restore import Lease, _read_complete

KEY = 'backup_work_retained'
PENDING = 'work-retirement.pending.json'
COMPLETED = 'work-retirement.verified.json'
IDENTITY = ('dev','ino','uid','gid','mode','nlink','size','mtime_ns','ctime_ns')
DIRECTORY = ('dev','ino','uid','gid','mode')
ERROR = '已验证备份的工作材料仍需核查；保留剩余文件，不自动重试'


def require(ok):
    if not ok: raise ValueError(ERROR)


def fingerprint(info, keys=IDENTITY): return {key:getattr(info,'st_'+key) for key in keys}


def digest(held):
    held.check(); offset=0; total=os.fstat(held.fd).st_size; value=hashlib.sha256()
    while offset<total:
        raw=os.pread(held.fd,min(1024*1024,total-offset),offset)
        require(bool(raw));value.update(raw);offset+=len(raw)
    held.check()
    return value.hexdigest()


def names(filename):
    archive.filename(filename)
    return ('recovery.zip','check.zip','readback-authenticated.zip',
            'credentials.tmencrypted',filename,'download.tmbackup')


def capture(folder, payload):
    expected={name:payload['raw_sha256'] for name in names(payload['encrypted'].name)[:3]}
    expected.update({'credentials.tmencrypted':payload['manifest']['files']['credentials.tmencrypted'],
        payload['encrypted'].name:payload['sha256'],'download.tmbackup':payload['sha256']})
    folder.check();require(set(os.listdir(folder.fd))==set(expected))
    records={}
    for name,wanted in expected.items():
        require(cfg.sha(wanted))
        with Lease(folder.path/name,uid=folder.uid,gid=folder.gid,mode=0o600,parent=folder) as held:
            require(digest(held)==wanted)
            records[name]=dict(sha256=wanted,identity=fingerprint(os.fstat(held.fd)))
    folder.check()
    return dict(directory=fingerprint(os.fstat(folder.fd),DIRECTORY),files=records)


def retained(state, root):
    values=state.get(KEY,[])
    require(isinstance(values,list) and len(values)<=128
        and all(isinstance(path,str) and Path(path).parent==root/'staging'
                and re.fullmatch('smb-stream-[0-9a-f]{32}',Path(path).name) for path in values)
        and len(set(values))==len(values))
    return list(values)


def mark(state, root, job):
    values=retained(state,root)
    require(job not in values and len(values)<128)
    proposed={**state,KEY:[*values,job]};retained(proposed,root)
    return proposed


def clear(state, root, job):
    values=retained(state,root);require(job in values)
    result=dict(state);values.remove(job)
    if values:result[KEY]=values
    else:result.pop(KEY,None)
    return result


def _record(folder, name, value):
    raw=cfg.canonical(value)
    fd=os.open(name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=folder.fd)
    with Lease(folder.path/name,uid=folder.uid,gid=folder.gid,mode=0o600,parent=folder,
               fd=fd,mutable=True) as held:
        remaining=memoryview(raw)
        while remaining:
            count=os.write(fd,remaining);require(count>0);remaining=remaining[count:]
        os.fsync(fd)
        require(_read_complete(held,65536)==raw)
    os.fsync(folder.fd)


def retire(root, context, result, copy, receipt_raw, protect):
    """The caller's protect() checks the committed state, receipt and approval."""
    job=Path(result['job']);work=result['work'];expected=names(result['filename'])
    require(job.parent==root.path/'staging' and re.fullmatch('smb-stream-[0-9a-f]{32}',job.name)
        and isinstance(work,dict) and set(work)=={'directory','files'}
        and set(work['directory'])==set(DIRECTORY) and set(work['files'])==set(expected))
    protect();require(digest(copy)==result['sha256'])
    with ExitStack() as stack:
        stage=stack.enter_context(Lease(root.path/'staging',uid=context['uid'],gid=context['gid'],
            mode=0o700,parent=root,directory=True,mutable=True))
        folder=stack.enter_context(Lease(job,uid=context['uid'],gid=context['gid'],
            mode=0o700,parent=stage,directory=True,mutable=True))
        require(fingerprint(os.fstat(folder.fd),DIRECTORY)==work['directory']
            and set(os.listdir(folder.fd))==set(expected))
        held={};removed=[]
        try:
            for name in expected:
                record=work['files'][name]
                require(isinstance(record,dict) and set(record)=={'sha256','identity'} and cfg.sha(record['sha256']))
                item=Lease(job/name,uid=context['uid'],gid=context['gid'],mode=0o600,parent=folder)
                held[name]=item
                require(fingerprint(os.fstat(item.fd))==record['identity'] and digest(item)==record['sha256'])
            protect()
            journal=dict(type='tianming.mac-smb-stream-work-retirement.v1',job=job.name,
                backup_sha256=result['sha256'],receipt_sha256=cfg.digest(receipt_raw),work=work)
            _record(folder,PENDING,journal)
            pending=stack.enter_context(Lease(job/PENDING,uid=context['uid'],gid=context['gid'],
                mode=0o600,parent=folder))
            require(_read_complete(pending,65536)==cfg.canonical(journal))
            for name in expected:
                protect();folder.check();copy.check();pending.check()
                require(set(os.listdir(folder.fd))==(set(expected)-set(removed))|{PENDING})
                for other,item in held.items():
                    if other not in removed:item.check()
                os.unlink(name,dir_fd=folder.fd)
                require(os.fstat(held[name].fd).st_nlink==0)
                removed.append(name);os.fsync(folder.fd)
            protect();folder.check();pending.check()
            require(set(os.listdir(folder.fd))=={PENDING})
            completion={**journal,'removed':removed,
                'bytes_removed':sum(work['files'][name]['identity']['size'] for name in removed)}
            _record(folder,COMPLETED,completion)
            completed=stack.enter_context(Lease(job/COMPLETED,uid=context['uid'],gid=context['gid'],
                mode=0o600,parent=folder))
            require(_read_complete(completed,65536)==cfg.canonical(completion))
            protect()
            require(set(os.listdir(folder.fd))=={PENDING,COMPLETED})
        finally:
            # Deleted names cannot pass Lease.__exit__'s named-object check.
            # Keep their FDs until every unlink/confirmation finishes.
            for item in held.values():os.close(item.fd)
