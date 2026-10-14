"""Explicit root policy: one verified local recovery point, no NAS operations."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from .storage import (sha,read_json,write_json,signed_release_manifest,decrypt_file,
    extract_verified,database_info)
from .mac_service_restore import Lease
from . import mac_backup_storage as records

ROOT=Path('/Library/TianmingERP/instance')
POLICY=Path('/Library/TianmingERP/local-backup-policy.json')
CAPABILITY='local_backup_only_v1'
MODULE='desktop_assistant/local_backup_only.py'
TYPE='tianming.local-only-backup-policy.v1'
RECEIPT='tianming.local-only-backup-receipt.v1'


def require(ok,message):
    if not ok:raise ValueError('Local complete backup: '+message)


def _authority():
    from . import mac_release_publish as publication,mac_published_runtime as published
    from app.core import mac_service_keychain as keychain
    from .mac_service_restore import runtime_identity
    context=runtime_identity()
    raw=publication.read_record(POLICY)
    value=json.loads(raw)
    require(set(value)=={'type','context_sha256','publisher_sha256','mode','directory','retained_versions'}
        and value['type']==TYPE and value['mode']=='local_only'
        and value['directory']==str(ROOT/'backups') and type(value['retained_versions']) is int
        and value['retained_versions']==1,'invalid fixed root policy')
    require(value['context_sha256']==hashlib.sha256(publication.read_record(keychain.CONTEXT)).hexdigest()
        and value['publisher_sha256']==hashlib.sha256(publication.read_record(published.PUBLIC)).hexdigest(),
        'policy context/publisher changed')
    require(ROOT.resolve()==ROOT and (ROOT/'backups').resolve()==ROOT/'backups','local namespace changed')
    with Lease(ROOT,uid=context['uid'],gid=context['gid'],mode=0o700,directory=True,mutable=True) as root:
        with Lease(ROOT/'backups',uid=root.uid,gid=root.gid,mode=0o700,parent=root,directory=True,mutable=True):pass
    return value,context,hashlib.sha256(raw).hexdigest()


def selected(root):
    if Path(root)!=ROOT:return False
    if not POLICY.exists() and not POLICY.is_symlink():return False
    _authority();return True


def enabled(manager,manifest=None):
    if not selected(manager.root):return False
    if manifest is None:
        identity=manager.state['current'];package=manager.root/'packages'/(identity+'.zip')
        require(sha(package)==identity,'current signed package changed')
        manifest=signed_release_manifest(package,manager.public_key)
    require(type(manifest.get('runtime_capabilities',{}).get(CAPABILITY)) is int
        and manifest['runtime_capabilities'][CAPABILITY]==1
        and manifest.get('files',{}).get(MODULE)==sha(Path(__file__)),
        'signed local-only reader/source capability missing')
    return True


def _receipt_path(root,cipher_sha):
    require(re.fullmatch('[a-f0-9]{64}',cipher_sha or '') is not None,'invalid cipher identity')
    return root/'control'/('local-backup-'+cipher_sha+'.json')


def _owned_read(path,context):
    with Lease(path,uid=context['uid'],gid=context['gid'],mode=0o600) as held:
        require(os.fstat(held.fd).st_size<1024*1024,'local proof exceeds small record bound')
        return json.loads(os.read(held.fd,1024*1024))


def _check_proof(manager,proof,context):
    require(proof.get('restored_verified') is True and proof.get('nas_verified') is False
        and proof.get('storage')=='local' and proof.get('local_backup_only') is True,
        'original local recovery proof missing')
    job_id=proof.get('job_id','')
    require(re.fullmatch('local-code-update-[a-f0-9]{32}|local-backup-[a-f0-9]{32}',job_id) is not None,
        'fixed local owner required')
    require(_owned_read(manager.root/'staging'/job_id/'verified.json',context)==proof,
        'original owner proof bytes changed')
    facts=proof.get('authenticated_recovery') or {}
    require(set(facts)=={'cipher_sha256','raw_sha256','manifest_sha256','database','source_time'}
        and facts['cipher_sha256']==proof['backup_sha256']
        and all(re.fullmatch('[a-f0-9]{64}',facts[key] or '') for key in ('cipher_sha256','raw_sha256','manifest_sha256'))
        and isinstance(facts['database'],dict) and datetime.fromisoformat(facts['source_time']).utcoffset() is not None,
        'authenticated original recovery facts incomplete')
    path=manager.root/'backups'/proof['backup_filename']
    require(path.parent==manager.root/'backups' and path.name==proof['backup_filename']
        and re.fullmatch('[0-9]{8}-[0-9]{6}-[a-f0-9]{8}\\.tmbackup',path.name) is not None,
        'cipher escaped fixed local backups')
    with Lease(path,uid=context['uid'],gid=context['gid'],mode=0o600) as held:
        require(os.fstat(held.fd).st_size==proof['backup_bytes'] and sha(path)==proof['backup_sha256'],
            'complete local cipher size/hash changed')
    return path


def _active_references(manager,context):
    path=manager.root/'control/backup-receipts/retention-jobs.json'
    if not path.exists() and not path.is_symlink():return [],[]
    value=_owned_read(path,context)
    require(value.get('type')=='tianming.nas-registered-jobs.v1' and isinstance(value.get('jobs'),dict),
        'historical activity registry cannot be interpreted')
    from .nas_backup_rotation import validate_job
    paths=[];hashes=[]
    for key,job in value['jobs'].items():
        validate_job(job);require(key==job['job_id'],'activity owner identity changed')
        if job['status']=='active':
            if job['backup_path']:paths.append(job['backup_path'])
            if job['backup_sha256']:hashes.append(job['backup_sha256'])
    return paths,hashes


def _closed_update_reference(manager,path,cipher_sha,context):
    proof=manager.state.get('local_code_update') or {}
    require(proof.get('current')==manager.state['current'] and proof.get('previous')==manager.state.get('previous')
        and proof.get('backup_sha256')==cipher_sha and proof.get('backup_filename')==path.name,
        'previous update recovery remains unresolved')
    _check_proof(manager,proof,context)
    record=_owned_read(_receipt_path(manager.root,cipher_sha),context)
    require(record.get('type')==RECEIPT and record.get('sha256')==cipher_sha
        and record.get('authenticated_recovery')==proof['authenticated_recovery']
        and record.get('retention',{}).get('status')=='one_complete',
        'previous update recovery lacks completed local verification')


def _retention(manager,keep,context):
    """Only authenticated old ciphers; unresolved activity retains and alarms."""
    from .cleanup import assert_idle
    state=manager.state
    require(not state.get('onboarding_pending') and not state.get('backup_work_retained')
        and state.get('operation')!='migration_running' and 'failed' not in (state.get('operation') or ''),
        'pending maintenance protects previous recovery points')
    active_paths,active_hashes=_active_references(manager,context)
    from .nas_full_backup_retention import _comparison_key
    removed=[];retained=[]
    for path in sorted((manager.root/'backups').iterdir()):
        if path==keep:continue
        try:
            require(re.fullmatch('[0-9]{8}-[0-9]{6}-[a-f0-9]{8}\\.tmbackup',path.name) is not None,
                'unknown local backup/cache owner')
            # Existing migration/restore source contracts stay conservative.
            old=Lease(path,uid=context['uid'],gid=context['gid'],mode=0o600)
            try:
                old_sha=sha(path)
                require(old_sha not in {state.get('restored_backup_sha256'),state.get('migration_backup_sha256')},
                    'previous cipher remains a restore/migration source reference')
                require(old_sha not in active_hashes and not any(_comparison_key(str(path))==_comparison_key(name) for name in active_paths),
                    'previous cipher remains an active task source')
                if str(path)==state.get('update_backup'):
                    _closed_update_reference(manager,path,old_sha,context)
                assert_idle(path)
                old.check();os.unlink(path);require(os.fstat(old.fd).st_nlink==0,'old cipher unlink not confirmed')
                removed.append(path.name)
            finally:os.close(old.fd)
        except (OSError,ValueError):retained.append(path.name)
    fd=os.open(manager.root/'backups',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)
    return {'status':'one_complete' if not retained else 'retained','removed':removed,'retained':retained}


def _finalize_locked(manager,proof,sourcepoint):
    enabled(manager);_,context,policy_sha=_authority()
    require(manager._process() is not None and manager.state.get('manual_stop') is False,
        'ERP must be ready before local publication/retention')
    path=_check_proof(manager,proof,context)
    state=manager.state;previous=dict(state)
    value=dict(type=RECEIPT,policy_sha256=policy_sha,path=str(path),sha256=proof['backup_sha256'],
        size=proof['backup_bytes'],authenticated_recovery=proof['authenticated_recovery'],sourcepoint=sourcepoint,
        verified_at=proof['authenticated_recovery']['source_time'],identity={key:getattr(path.stat(),'st_'+key)
            for key in ('dev','ino','uid','gid','mode','nlink','size','mtime_ns','ctime_ns')})
    receipt=_receipt_path(manager.root,proof['backup_sha256'])
    if receipt.exists():
        old=_owned_read(receipt,context)
        require(all(old.get(key)==value[key] for key in value),'existing local receipt differs')
    value['retention']={'status':'pending','removed':[],'retained':[]}
    write_json(receipt,value)  # Durable new proof BEFORE state publication or old unlink.
    control_fd=os.open(receipt.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(control_fd)
    finally:os.close(control_fd)
    state.update(last_backup=str(path),last_backup_at=value['verified_at'],backup_error=None,
        last_backup_local={'receipt':receipt.name,'sha256':value['sha256'],'source_time':value['verified_at']})
    require(manager.state==previous,'runtime state changed before local publication')
    write_json(manager.root/'state.json',state)
    value['retention']=_retention(manager,path,context)
    write_json(receipt,value)
    if value['retention']['status']!='one_complete':
        state=manager.state;state['backup_error']='本地完整备份已验证；旧备份仍受引用或清理守卫保护，请管理员收口'
        write_json(manager.root/'state.json',state)
    return dict(status='verified',backup=str(path),verified_at=value['verified_at'],
        source_time=proof['authenticated_recovery']['source_time'],sourcepoint=sourcepoint,retention=value['retention'])


def finalize_local_update(manager):
    """Caller need not hold lock; reuse ONLY the real cold proof after readiness."""
    with manager.lock():
        proof=manager.state.get('local_code_update') or {}
        require(proof.get('current')==manager.state['current'] and proof.get('previous')==manager.state.get('previous'),
            'local update transition differs from actual state')
        return _finalize_locked(manager,proof,'before-local-code-update')


def verified_at(root,state,*,now=None):
    if not selected(root):return None
    _,context,policy_sha=_authority();index=state.get('last_backup_local') or {}
    try:
        value=_owned_read(_receipt_path(root,index['sha256']),context)
        require(value['type']==RECEIPT and value['policy_sha256']==policy_sha
            and value['path']==state['last_backup'] and value['verified_at']==state['last_backup_at'],
            'local backup publication changed')
        with Lease(Path(value['path']),uid=context['uid'],gid=context['gid'],mode=0o600) as held:
            identity={key:getattr(os.fstat(held.fd),'st_'+key) for key in value['identity']}
            require(identity==value['identity'],'verified complete cipher identity changed')
        return value['verified_at']
    except (OSError,ValueError,KeyError,TypeError):return None


def backup_locked(manager,password,*,restart_allowed=None):
    """Original maintenance caller owns lock; one capture, resume then verify."""
    enabled(manager);_,context,_=_authority()
    old_index=manager.state.get('last_backup_local')
    if old_index:
        old=_owned_read(_receipt_path(manager.root,old_index['sha256']),context)
        require(old['retention']['status']=='one_complete','previous local retention needs owner closure')
    budget=manager._backup_inputs()
    require(shutil.disk_usage(manager.root).free>=budget['required'],'insufficient complete local backup space')
    job=manager.root/'staging'/('local-backup-'+uuid.uuid4().hex);job.mkdir(mode=0o700)
    running=bool(manager._process());attempted=False;resumed_process=None
    try:
        manager.stop();inputs=manager._backup_inputs()
        payload=manager._build_backup_payload(password,job,inputs)
        final=manager.root/'backups'/payload['encrypted'].name
        with payload['encrypted'].open('rb') as src,final.open('xb') as dst:
            shutil.copyfileobj(src,dst,1024*1024);dst.flush();os.fsync(dst.fileno())
        fd=os.open(final.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
        attempted=True
        if running and (restart_allowed is None or restart_allowed()):
            manager.start();resumed_process=manager._process()
        raw=job/'independent-readback.zip'
        require(decrypt_file(final,raw,password)==payload['sha256'] and sha(raw)==payload['raw_sha256'],
            'independent local readback authentication failed')
        recovered=job/'independent-recovery';manifest=extract_verified(raw,recovered)
        require(manifest==payload['manifest'] and database_info(recovered/'shared/data/carton_erp.sqlite3')==inputs['before'],
            'independent full local recovery mismatch')
        require(signed_release_manifest(recovered/'release.zip',manager.public_key)==manager.manifest(inputs['current']),
            'original recovered program signature changed')
        proof=dict(type='tianming.local-only-backup.v1',storage='local',nas_verified=False,restored_verified=True,
            local_backup_only=True,backup_sha256=payload['sha256'],backup_bytes=payload['size'],backup_filename=final.name,
            job_id=job.name,authenticated_recovery=dict(cipher_sha256=payload['sha256'],raw_sha256=sha(raw),
                manifest_sha256=hashlib.sha256(records.canonical(manifest)).hexdigest(),database=inputs['before'],source_time=manifest['created']))
        write_json(job/'verified.json',proof)
        # Original owner guard; retained plaintext is reported, never guessed closed.
        from .cleanup import remove_owned_tree
        try:remove_owned_tree(recovered,job)
        except (OSError,ValueError):
            state=manager.state;state['backup_work_retained']=[str(recovered)]
            write_json(manager.root/'state.json',state)
        for name in ('recovery.zip','check.zip','independent-readback.zip','credentials.tmencrypted',payload['encrypted'].name):
            try:(job/name).unlink()
            except FileNotFoundError:pass
        return Path(_finalize_locked(manager,proof,'before-local-backup')['backup'])
    finally:
        if running and not attempted and (restart_allowed is None or restart_allowed()):manager.start()
        elif (resumed_process is not None and restart_allowed is not None and not restart_allowed()
                and manager._process()==resumed_process):
            manager.stop()
