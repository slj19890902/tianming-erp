"""Stdlib administrator publication; holds the original lock across worker exit.

Root reads public metadata and one maintenance-control byte only. Application
and secret checks execute as the dedicated UID inside the approved runtime.
"""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import stat
import subprocess
import uuid

from desktop_assistant import mac_keychain_install as boot, mac_system_service as system
from desktop_assistant import mac_release_publish as publication
from desktop_assistant import factory_activation_contract as contract, factory_target_contract as target
from desktop_assistant.home_process import command

DATA=Path('/System/Volumes/Data')


def _owned(info,uid,gid):
    return info.st_uid==uid and (gid is None or info.st_gid==gid)


def _fingerprint(info):
    return tuple(getattr(info,k) for k in ('st_dev','st_ino','st_uid','st_gid','st_mode',
                                           'st_nlink','st_size','st_mtime_ns','st_ctime_ns'))


@contextmanager
def _inputs(meta, request_id, attempt_id):
    """Hold root/control/input names without following links or opening a DB."""
    context=meta['context'];held=[]
    with ExitStack() as stack:
        def open_path(path,uid,gid,mode,*,parent=None,directory=False):
            flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|(os.O_DIRECTORY if directory else 0)
            fd=os.open(path.name if parent else path,flags,dir_fd=parent[1] if parent else None)
            stack.callback(os.close,fd)
            item=(path,fd,uid,gid,mode,parent,directory,_fingerprint(os.fstat(fd)))
            held.append(item);check(item)
            return item
        def check(item):
            path,fd,uid,gid,mode,parent,directory,initial=item
            if parent:check(parent)
            info=os.fstat(fd);named=os.stat(path.name if parent else path,dir_fd=parent[1] if parent else None,follow_symlinks=False)
            if (not (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode) or not _owned(info,uid,gid)
                    or stat.S_IMODE(info.st_mode)!=mode or info.st_dev!=DATA.stat().st_dev
                    or (not directory and (info.st_nlink!=1 or _fingerprint(info)!=initial))
                    or _fingerprint(info)!=_fingerprint(named)):
                raise ValueError('激活公开输入或原维护锁保护/身份变化')
            boot.no_acl(fd)
        def read(item):
            check(item);os.lseek(item[1],0,os.SEEK_SET)
            size=os.fstat(item[1]).st_size
            if not 0<size<=boot.LIMIT:raise ValueError('激活公开输入长度无效')
            value=b''
            while len(value)<=size:
                block=os.read(item[1],size+1-len(value))
                if not block:break
                value+=block
            check(item)
            if len(value)!=size:raise ValueError('激活公开输入读取不完整')
            return value
        base=open_path(system.BASE,0,None,0o755,directory=True)
        root=open_path(system.ROOT,context['uid'],context['gid'],0o700,parent=base,directory=True)
        control=open_path(system.ROOT/'control',context['uid'],context['gid'],0o700,parent=root,directory=True)
        lock=open_path(control[0]/'operation.lock',context['uid'],context['gid'],0o600,parent=control)
        if read(lock)!=b'0':raise ValueError('激活必须使用原完整维护锁')
        for name in ('keychain-context.json','published.json','publisher-public.pem'):
            open_path(system.BASE/name,0,None,0o644,parent=base)
        request=read(open_path(system.BASE/('factory-target-'+request_id+'.json'),0,None,0o644,parent=base))
        network=read(open_path(system.BASE/('factory-network-'+request_id+'.json'),0,None,0o644,parent=base))
        inputs=open_path(system.BASE/'restore-inputs',0,context['gid'],0o750,parent=base,directory=True)
        attempt=open_path(inputs[0]/attempt_id,0,context['gid'],0o750,parent=inputs,directory=True)
        authorization=read(open_path(attempt[0]/'authorization.json',0,context['gid'],0o440,parent=attempt))
        def recheck():
            for item in held:check(item)
        import fcntl
        try:fcntl.flock(lock[1],fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('维护仍在进行，未启动激活核验') from None
        try:
            recheck()
            yield lock[1],request,network,authorization,recheck
            recheck()
        finally:fcntl.flock(lock[1],fcntl.LOCK_UN)


def _absent():
    if (os.path.lexists(system.BASE/contract.NAME)
            or any(system.BASE.glob('factory-activation-*.pending.json'))):
        raise ValueError('已有激活或未决发布记录，不能覆盖、重放或自动重试')


def activate(package,request_id,input_attempt,authorization_sha256,*,cutover_authorized=False,source_isolated=False):
    identity,_=boot.modules();identity._administrator();boot.verify_code()
    if cutover_authorized is not True or source_isolated is not True:
        raise ValueError('需要当次正式切换授权和旧端隔离确认')
    target._hash(package);target._hash(authorization_sha256);target._id(request_id);target._id(input_attempt)
    with publication.lock():
        _absent()
        meta=system.metadata(package);contract.require_protocol(system._program_manifest(meta))
        with _inputs(meta,request_id,input_attempt) as (fd,request_raw,network_raw,authorization,recheck):
            saved=boot.decode(request_raw);proposed=boot.decode(network_raw)
            target.validate_request(saved);contract.network.validate(proposed)
            expected_target={'host_sha256':target.host_hash(),'installation_id':meta['context']['installation_id'],
                'service_uid':meta['context']['uid'],'service_gid':meta['context']['gid'],
                'service_account':target.SERVICE_ACCOUNT,'root':str(system.ROOT)}
            if (saved['request_id']!=request_id or saved['package_sha256']!=package
                    or saved['target']!=expected_target or saved['publisher_sha256']!=meta['publisher_sha256']
                    or target.digest(proposed)!=saved['network_sha256']
                    or boot.digest(authorization)!=authorization_sha256):raise ValueError('激活输入绑定不符')
            body=contract.authorization_body(authorization,saved)  # Signature verified in worker, not here.
            def window():
                if not target._time(body['issued_at'])<=target._now()<target._time(body['install_before']):
                    raise ValueError('激活发布已超出源端授权窗口')
            window()
            value={'package_sha256':package,'request_id':request_id,'input_attempt':input_attempt,
                'authorization_sha256':authorization_sha256,'challenge':uuid.uuid4().hex,'lock_fd':fd,
                **{k:meta[k] for k in ('context_sha256','registry_sha256')}}
            environment={'HOME':str(system.BASE/'home'),'PATH':'/usr/bin:/bin:/usr/sbin:/sbin',
                         'LANG':'en_US.UTF-8','ERP_HOME_REHEARSAL':'1'}
            argv=command([str(meta['tree']/'runtime/bin/python3.12'),'-I','-B',str(meta['tree']/contract.ENTRY)],environment)
            process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                user=meta['context']['uid'],group=meta['context']['gid'],extra_groups=[],close_fds=True,
                pass_fds=(fd,),umask=0o077,cwd='/',env=environment)
            interrupted=False;first=True
            while True:
                try:output,_=process.communicate(boot.canonical(value) if first else None);break
                except KeyboardInterrupt:interrupted=True;first=False
            if interrupted or process.returncode!=0:
                raise ValueError('激活核验未确认完成，保持停止和全部现场；不自动重试')
            result=contract.reply(boot.decode(output),value,meta['context']);record=result['record']
            if (boot.canonical(record['request'])!=boot.canonical(saved)
                    or boot.canonical(record['network'])!=boot.canonical(proposed)
                    or record['authorization'].encode()!=authorization):raise ValueError('激活核验原输入变化')
            latest=system.metadata(package)
            if any(latest[k]!=meta[k] for k in ('context_sha256','publisher_sha256','registry_sha256')):
                raise ValueError('激活核验后root批准身份变化')
            recheck();window();_absent()
            record['activated_at']=target._stamp(target._now());contract.validate_record(record)
            raw=boot.canonical(record)
            pending=system.BASE/('factory-activation-'+request_id+'.pending.json')
            boot._write(pending,raw,mode=0o644);publication.sync_directory(system.BASE)
            if publication.read_record(pending)!=raw:raise ValueError('激活待发布记录回读失败，保留现场')
            recheck();window()
            # The original lock remains owned here, even though the worker has
            # exited. No cooperating resume/backup/update can alter the facts.
            boot._write(system.BASE/contract.NAME,raw,mode=0o644);publication.sync_directory(system.BASE)
            if publication.read_record(system.BASE/contract.NAME)!=raw:
                raise ValueError('激活记录可能已发布但回读未确认，保持停止并保留现场')
            recheck()
            return {'type':'tianming.factory-activation-result.v1','activation_sha256':boot.digest(raw),
                'request_id':request_id,'factory_activated':True,'started':False,'manual_stop':True,
                'source_isolation_attested':True,'source_physical_isolation_verified':False,
                'network_reachability_verified':False,'logout_reboot_verified':False}
