"""Authenticated stream backup publication and its retained recovery copy.

No mounted-filesystem claims, NAS fallback, credential installation or cleanup.
The final encrypted local copy is recovery evidence tied to the NAS readback,
not a replacement for NAS verification. The caller must hold maintenance lock
and keep ERP stopped; routing and provisioning are separate deployment gates.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import stat

from . import mac_smb_stream_runtime as runtime, mac_smb_stream_runtime_contract as cfg
from . import mac_smb_stream_backup as builder, mac_smb_stream_contract as archive
from . import mac_backup_storage as local, mac_service_manage as manage
from . import mac_smb_stream_work as work
from .mac_service_restore import Lease
from .storage import database_info, sha

TYPE='tianming.mac-smb-stream-backup-receipt.v1'
INDEX='last_backup_stream'
FIELDS={'type','context_sha256','control_sha256','contract_sha256','package_sha256',
        'filename','sha256','size','raw_sha256','database','verified_at','transfer','local_copy'}
IDENTITY=('dev','ino','uid','gid','mode','nlink','size','mtime_ns','ctime_ns')
ERROR='SMB完整备份成功记录未确认；保留副本和原状态，不覆盖重试'


def require(ok):
    if not ok: raise ValueError(ERROR)


def fingerprint(info): return {key:getattr(info,'st_'+key) for key in IDENTITY}


def receipt_name(filename):
    return archive.filename(filename)[:-len('.tmbackup')]+'.smb-stream.json'


def reauthenticated_receipt_name(value):
    """Current control-bound alias; preserve the original immutable receipt."""
    require(cfg.sha(value.get('control_sha256')))
    return 'reauth-'+value['control_sha256']+'-'+receipt_name(value['filename'])


def matches_receipt_name(value, name):
    return name in (receipt_name(value['filename']),reauthenticated_receipt_name(value))


def approval():
    """Historical display: small root records only, no package hash or network."""
    context=runtime.keychain.load_context();runtime.actual_identity(context)
    records={p:runtime.publication.read_record(p) for p in
        (cfg.CONTROL,cfg.CONTRACT,runtime.keychain.CONTEXT,runtime.published.REGISTRY,runtime.published.PUBLIC)}
    require(runtime.boot.decode(records[runtime.keychain.CONTEXT])==context)
    control,value=(runtime.boot.decode(records[p]) for p in (cfg.CONTROL,cfg.CONTRACT))
    require(cfg.canonical(control)==records[cfg.CONTROL] and cfg.canonical(value)==records[cfg.CONTRACT])
    cfg.validate(control,value,cfg.digest(records[runtime.keychain.CONTEXT]),context['uid'])
    registry=runtime.boot.decode(records[runtime.published.REGISTRY])
    require(isinstance(registry,dict) and set(registry)=={'type','context_sha256','publisher_sha256','packages'}
        and registry['type']==runtime.publication.REGISTRY_TYPE
        and registry['context_sha256']==control['context_sha256']
        and registry['publisher_sha256']==control['publisher_sha256']==cfg.digest(records[runtime.published.PUBLIC])
        and isinstance(registry['packages'],list) and 1<=len(registry['packages'])<=32
        and all(cfg.sha(p) for p in registry['packages']) and control['package_sha256'] in registry['packages'])
    return dict(context=context,control=control,value=value,control_sha=cfg.digest(records[cfg.CONTROL]))


@contextmanager
def root_lease(context):
    path=cfg.BASE/'instance'
    require(path==path.resolve())
    with Lease(path,uid=context['uid'],gid=context['gid'],mode=0o700,directory=True,mutable=True) as root:
        yield root


@contextmanager
def receipts(root,context,*,create=False):
    with Lease(root.path/'control',uid=context['uid'],gid=context['gid'],mode=0o700,
               parent=root,directory=True,mutable=True) as control:
        with local._record_directory(control.fd,'backup-receipts',context['uid'],
                os.fstat(control.fd).st_dev,create=create) as fd:
            require(os.fstat(fd).st_gid==context['gid'])
            yield fd


@contextmanager
def copy_lease(root,context,filename):
    with Lease(root.path/'backups',uid=context['uid'],gid=context['gid'],mode=0o700,
               parent=root,directory=True,mutable=True) as backups:
        with Lease(backups.path/archive.filename(filename),uid=context['uid'],gid=context['gid'],
                   mode=0o600,parent=backups) as held:
            yield held


def validate(value,bound,*,now=None):
    control,context=bound['control'],bound['context']
    require(isinstance(value,dict) and set(value)==FIELDS and value['type']==TYPE
        and value['context_sha256']==control['context_sha256']
        and value['control_sha256']==bound['control_sha']
        and value['contract_sha256']==control['contract_sha256']
        and value['package_sha256']==control['package_sha256'] and cfg.sha(value['raw_sha256'])
        and isinstance(value['database'],dict) and isinstance(value['verified_at'],str))
    archive.filename(value['filename'])
    payload=dict(encrypted=Path(value['filename']),size=value['size'],sha256=value['sha256'])
    runtime.files._spec(value['size'],value['sha256'])
    builder._proofs(bound['value'],payload,value['transfer'])
    when=datetime.fromisoformat(value['verified_at'])
    require(when.utcoffset() is not None and when<=(now or datetime.now(timezone.utc))+timedelta(minutes=5))
    copy=value['local_copy']
    require(isinstance(copy,dict) and set(copy)=={'path','sha256','size','identity'}
        and copy['path']==str(cfg.BASE/'instance/backups'/value['filename'])
        and copy['sha256']==value['sha256'] and type(copy['size']) is int and copy['size']==value['size'])
    identity=copy['identity']
    require(isinstance(identity,dict) and set(identity)==set(IDENTITY)
        and all(type(v) is int and v>=0 for v in identity.values()) and identity['ino']>0
        and identity['uid']==context['uid'] and identity['gid']==context['gid']
        and identity['mode']==stat.S_IFREG|0o600 and identity['nlink']==1 and identity['size']==value['size'])
    return value


def _copy_authenticated(root,context,result):
    job=Path(result['job'])
    require(job.parent==root.path/'staging' and re.fullmatch('smb-stream-[0-9a-f]{32}',job.name))
    with ExitStack() as stack:
        stage=stack.enter_context(Lease(root.path/'staging',uid=context['uid'],gid=context['gid'],
            mode=0o700,parent=root,directory=True,mutable=True))
        folder=stack.enter_context(Lease(job,uid=context['uid'],gid=context['gid'],mode=0o700,
            parent=stage,directory=True,mutable=True))
        source=stack.enter_context(Lease(job/'download.tmbackup',uid=context['uid'],gid=context['gid'],
            mode=0o600,parent=folder))
        require(os.fstat(source.fd).st_size==result['size'])
        backups=stack.enter_context(Lease(root.path/'backups',uid=context['uid'],gid=context['gid'],
            mode=0o700,parent=root,directory=True,mutable=True))
        filename=archive.filename(result['filename'])
        fd=os.open(filename,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=backups.fd)
        output=stack.enter_context(Lease(backups.path/filename,uid=context['uid'],gid=context['gid'],
            mode=0o600,parent=backups,fd=fd,mutable=True))
        offset=0;digest=hashlib.sha256()
        while offset<result['size']:
            chunk=os.pread(source.fd,min(1024*1024,result['size']-offset),offset)
            require(bool(chunk));digest.update(chunk)
            written=0
            while written<len(chunk):
                count=os.write(fd,chunk[written:]);require(count>0);written+=count
            offset+=len(chunk)
        os.fsync(fd);os.fsync(backups.fd)
        source.check();output.check()
        require(digest.hexdigest()==result['sha256'] and os.fstat(fd).st_size==result['size']
                and sha(output.path)==result['sha256'])
        output.check()
        return dict(path=str(output.path),sha256=result['sha256'],size=result['size'],identity=fingerprint(os.fstat(fd)))


def archive_and_publish_stopped(manager,password):
    """Internal candidate: full backup first; exclusive receipt then CAS state."""
    return _archive_and_publish(manager,password)


def archive_and_publish_captured(manager,password,resume):
    """Publish the authenticated captured point after Manager resumes ERP."""
    require(callable(resume))
    return _archive_and_publish(manager,password,resume)


def _archive_and_publish(manager,password,resume=None):
    binding=runtime.load_binding()
    require(manager.root==cfg.BASE/'instance' and manager._process() is None
            and Path(__file__).resolve()==binding.tree/'desktop_assistant/mac_smb_stream_receipt.py')
    context=binding.context
    bound=dict(context=context,control=binding.control,value=binding.value,control_sha=binding.control_sha)
    try:
        with root_lease(context) as root:
            before,state=manage.record(root,'state.json',context)
            binding.check()
            resumed=False
            def resume_captured():
                nonlocal resumed
                binding.check();root.check()
                require(manage.record(root,'state.json',context)[0]==before)
                resumed=resume()
                require(type(resumed) is bool)
                return resumed
            if resume is None:
                result=builder.build_and_verify_stopped(manager,password,binding.value,runtime.transfer)
            else:
                result=builder.build_and_verify_captured(manager,password,binding.value,runtime.transfer,resume_captured)
            require(result['type']=='tianming.mac-smb-stream-candidate-verification.v1'
                    and result['backup_authenticated'] is True and result['state_published'] is False)
            local_copy=_copy_authenticated(root,context,result)
            receipt=dict(type=TYPE,context_sha256=binding.control['context_sha256'],
                control_sha256=binding.control_sha,contract_sha256=binding.control['contract_sha256'],
                package_sha256=binding.control['package_sha256'],filename=result['filename'],sha256=result['sha256'],
                size=result['size'],raw_sha256=result['raw_sha256'],database=result['database'],
                verified_at=datetime.now(timezone.utc).isoformat(),transfer=result['transfer'],local_copy=local_copy)
            validate(receipt,bound)
            raw=cfg.canonical(receipt);name=receipt_name(result['filename'])
            def recheck():
                binding.check();root.check()
                if not resumed:
                    require(manager._process() is None)
            recheck()
            require(manage.record(root,'state.json',context)[0]==before
                    and manager.state==state)
            if not resumed:
                require(database_info(root.path/'shared/data/carton_erp.sqlite3')==result['database'])
            with copy_lease(root,context,result['filename']) as copy:
                require(fingerprint(os.fstat(copy.fd))==local_copy['identity'])
                with receipts(root,context,create=True) as directory:
                    local._exclusive(directory,name,raw,context['uid']);os.fsync(directory)
                    state.update(last_backup=local_copy['path'],last_backup_at=receipt['verified_at'],backup_error=None)
                    state[INDEX]=dict(path=local_copy['path'],receipt=name,receipt_sha256=cfg.digest(raw))
                    state=work.mark(state,root.path,result['job'])
                    def final_check():
                        recheck();copy.check()
                        require(fingerprint(os.fstat(copy.fd))==local_copy['identity']
                            and local._local_object(directory,name,context['uid'])[0]==raw)
                    final_check()
                    manage.replace_record(root,'state.json',state,before,context,final_check)
                    published=cfg.canonical(state)
                    def protected_success():
                        final_check()
                        require(manage.record(root,'state.json',context)[0]==published)
                    try:
                        work.retire(root,context,result,copy,raw,protected_success)
                        retired=work.clear(state,root.path,result['job'])
                        # The CAS check belongs to replace_record: its final
                        # recheck runs after the old state has been replaced.
                        manage.replace_record(root,'state.json',retired,published,context,final_check)
                    except Exception:
                        # Backup is authenticated and retained. Keep the pending
                        # state reference visible; do not retry this job later.
                        pass
            return Path(local_copy['path'])
    except Exception:
        raise ValueError(ERROR) from None


def verified_at(root,state,*,now=None):
    """Historical success, not present NAS availability or durability proof."""
    try:
        require(Path(root)==cfg.BASE/'instance')
        bound=approval();context=bound['context'];index=state[INDEX]
        require(isinstance(index,dict) and set(index)=={'path','receipt','receipt_sha256'}
                and index['path']==state['last_backup'] and cfg.sha(index['receipt_sha256']))
        filename=Path(index['path']).name;archive.filename(filename)
        require(index['receipt'] in (receipt_name(filename),'reauth-'+bound['control_sha']+'-'+receipt_name(filename)))
        with root_lease(context) as held:
            with receipts(held,context) as directory:
                raw,value=local._local_object(directory,index['receipt'],context['uid'])
            require(raw==cfg.canonical(value) and cfg.digest(raw)==index['receipt_sha256'])
            validate(value,bound,now=now)
            require(matches_receipt_name(value,index['receipt']))
            require(value['local_copy']['path']==index['path'] and value['filename']==filename
                    and value['verified_at']==state['last_backup_at'])
            with copy_lease(held,context,filename) as copy:
                require(fingerprint(os.fstat(copy.fd))==value['local_copy']['identity'])
        require(approval()==bound)
        return value['verified_at']
    except Exception:
        return None
