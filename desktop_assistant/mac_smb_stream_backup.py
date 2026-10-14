"""Complete payload + independent stream readback authentication, without publish.

Only a signed dedicated worker may supply the transfer callback, after validating
the root plan and obtaining the existing maintenance lock with ERP stopped.
This module is not a new CLI, a NAS fallback, or an installation authority.
"""
from contextlib import ExitStack, contextmanager
import os
import re
import shutil
import uuid

from . import mac_smb_stream_contract as contract
from . import mac_smb_stream_files_v2 as files
from .storage import database_info, decrypt_file, sha

ERROR='SMB完整备份认证未完成；保留现场，不发布成功或自动重试'


def require(ok):
    if not ok: raise ValueError(ERROR)


@contextmanager
def _private_creation():
    # The signed caller is an isolated single-purpose worker, not the web server.
    previous=os.umask(0o077)
    try: yield
    finally: os.umask(previous)


def _proofs(value,payload,result):
    require(isinstance(result,dict) and set(result)=={'upload','download'})
    upload,download=result['upload'],result['download']
    fields={'type','contract_sha256','connection_guid','filename','bytes','sha256',
            'directories','file','backup_authenticated'}
    for record,kind in ((upload,files.UPLOAD),(download,files.READBACK)):
        require(isinstance(record,dict) and set(record)==fields and record['type']==kind
                and record['backup_authenticated'] is False
                and record['contract_sha256']==contract.digest(value)
                and record['filename']==payload['encrypted'].name
                and type(record['bytes']) is int and record['bytes']==payload['size']
                and record['sha256']==payload['sha256']
                and isinstance(record['connection_guid'],str)
                and re.fullmatch('[0-9a-f]{32}',record['connection_guid']))
        require(isinstance(record['directories'],list) and len(record['directories'])==3)
        for identity in [*record['directories'],record['file']]:
            require(isinstance(identity,dict) and set(identity)=={'index'}
                    and all(type(v) is int and 0<v<2**64 for v in identity.values()))
    require(upload['connection_guid']!=download['connection_guid']
            and upload['directories']==download['directories'] and upload['file']==download['file'])


def build_and_verify_stopped(manager,password,value,transfer):
    """Return a candidate result only; keep all artifacts and original state.

    transfer(source_fd, destination_fd, filename, size, sha256, attempt_id)
    must wait for both independent approved workers and return their validated
    observations. It must not consume/close these descriptors or publish state.
    No production caller is enabled until the signed worker protocol is ready.
    """
    return _build_and_verify(manager,password,value,transfer)


def build_and_verify_captured(manager,password,value,transfer,resume):
    """Authenticate a stopped recovery point, then resume before network IO.

    Only the Manager-owned callback may resume the service. True means startup
    readiness completed; False preserves the original stopped-service contract.
    The published backup describes the captured point, not later live writes.
    """
    require(callable(resume))
    return _build_and_verify(manager,password,value,transfer,resume)


def _build_and_verify(manager,password,value,transfer,resume=None):
    from .mac_service_restore import Lease
    value=dict(contract.validate(value,value.get('context_sha256'),value.get('uid')))
    require(manager._process() is None)
    root=manager.root
    require(root.is_absolute() and root==root.resolve())
    uid,gid=os.geteuid(),os.getegid()
    try:
        with ExitStack() as held:
            root_lease=held.enter_context(Lease(root,uid=uid,gid=gid,mode=0o700,directory=True,mutable=True))
            staging=root/'staging'
            stage_lease=held.enter_context(Lease(staging,uid=uid,gid=gid,mode=0o700,
                directory=True,mutable=True,parent=root_lease))
            inputs=manager._backup_inputs()
            # Source/check plus encrypted upload/download and decrypted readback.
            require(shutil.disk_usage(staging).free>=inputs['required']*3//2)
            name='smb-stream-'+uuid.uuid4().hex
            os.mkdir(name,mode=0o700,dir_fd=stage_lease.fd)
            job=staging/name
            job_lease=held.enter_context(Lease(job,uid=uid,gid=gid,mode=0o700,
                directory=True,mutable=True,parent=stage_lease))
            job_lease.check(empty=True)
            with _private_creation():
                payload=manager._build_backup_payload(password,job,inputs)
            job_lease.check()
            source=held.enter_context(Lease(payload['encrypted'],uid=uid,gid=gid,mode=0o600,
                parent=job_lease))
            resumed=False
            if resume is not None:
                require(manager._process() is None and manager.state==payload['state']
                        and database_info(root/'shared/data/carton_erp.sqlite3')==payload['database']
                        and os.fstat(source.fd).st_size==payload['size']
                        and sha(payload['encrypted'])==payload['sha256'])
                os.fsync(source.fd);os.fsync(job_lease.fd);os.fsync(stage_lease.fd)
                source.check();job_lease.check()
                resumed=resume()
                require(type(resumed) is bool)
                if not resumed:
                    require(manager._process() is None)
                source.check();job_lease.check()
            destination=job/'download.tmbackup'
            fd=os.open(destination.name,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600,
                       dir_fd=job_lease.fd)
            downloaded=held.enter_context(Lease(destination,uid=uid,gid=gid,mode=0o600,
                parent=job_lease,fd=fd,mutable=True))
            result=transfer(source.fd,downloaded.fd,payload['encrypted'].name,
                            payload['size'],payload['sha256'],uuid.uuid4().hex)
            source.check();downloaded.check();job_lease.check()
            _proofs(value,payload,result)
            require(sha(destination)==payload['sha256'] and destination.stat().st_size==payload['size'])
            # Freeze download identity before authentication; no source URI is
            # trusted as a substitute for the actual bytes returned by readback.
            downloaded.mutable=False
            downloaded.initial=tuple(getattr(os.fstat(downloaded.fd),key) for key in
                ('st_dev','st_ino','st_uid','st_gid','st_mode','st_nlink','st_size','st_mtime_ns','st_ctime_ns'))
            verified=job/'readback-authenticated.zip'
            with _private_creation():
                cipher_sha=decrypt_file(destination,verified,password)
            require(cipher_sha==payload['sha256'] and sha(verified)==payload['raw_sha256'])
            source.check();downloaded.check();job_lease.check()
            require(manager.state==payload['state'])
            if not resumed:
                require(manager._process() is None
                        and database_info(root/'shared/data/carton_erp.sqlite3')==payload['database'])
            from .mac_smb_stream_work import capture
            work=capture(job_lease,payload)
            return dict(type='tianming.mac-smb-stream-candidate-verification.v1',
                job=str(job),filename=payload['encrypted'].name,sha256=payload['sha256'],
                size=payload['size'],database=payload['database'],raw_sha256=payload['raw_sha256'],
                backup_authenticated=True,transfer=result,state_published=False,work=work)
    except Exception:
        raise ValueError(ERROR) from None
