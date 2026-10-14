"""Fixed signed Mac worker integration; no CLI, second capture, or NAS scan.

Owner functions receive the existing Manager/password capability, never a JSON
success claim. All task records live in its protected receipt directory. Capture
runs under the original short lock; transfer/restore run after ERP is ready.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid
import zipfile

from . import nas_backup_rotation as rotation, nas_backup_rotation_stream as stream
from . import mac_smb_stream_receipt as receipt, mac_smb_stream_runtime as runtime
from . import mac_smb_stream_runtime_contract as cfg, mac_backup_storage as records
from . import mac_service_manage as manage, mac_smb_stream_work as work
from .mac_service_restore import Lease
from .storage import sha, decrypt_file, database_info

MODULE='desktop_assistant/nas_backup_rotation_worker.py'
AUTHORITY='retention-bootstrap-authority.json'


def _binding(manager):
    bound=runtime.load_binding()
    from .mac_smb_stream_target import _check_manager
    _check_manager(manager,bound)
    rotation.require(Path(__file__).resolve()==bound.tree/MODULE,'rotation code is not the approved stream worker')
    _,manifest=runtime.published.resolve(manager.root,bound.control['package_sha256'],manager.public_key,kind='migration')
    cfg.rotation_capability(manifest)
    bound.check()
    return bound


def _adapter(manager):
    return rotation.MacStreamReceiptAdapter(manager)


def _job(job_id,kind,status,path,backup_sha=None,pointer=None):
    return dict(job_id=job_id,kind=kind,status=status,backup_path=str(path),
        backup_sha256=backup_sha,receipt='' if pointer is None else pointer['receipt'])


def _task_name(task_id):
    rotation.require(isinstance(task_id,str) and re.fullmatch('[a-f0-9]{32}',task_id),'fixed task identity required')
    return 'retention-task-'+task_id+'.json'


def _read(adapter,name):
    with adapter.records() as (_,bound,_,directory):
        raw,value=records._local_object(directory,name,bound['context']['uid'])
    return raw,value


def _publish(adapter,name,value,*,expected=None,locked=False):
    """Owner-only exact record/CAS, never overwrite an immutable authority."""
    @contextmanager
    def lock():
        if locked:yield
        else:
            with adapter.manager.lock():yield
    with lock(),adapter.records() as (_,bound,_,directory):
        raw=records.canonical(value)
        rotation.require(len(raw)<1024*1024,'protected task exceeds reader limit')
        try:current,_=records._local_object(directory,name,bound['context']['uid'])
        except FileNotFoundError:current=None
        if current==raw:return
        if expected is None:
            rotation.require(current is None,'different immutable task evidence already exists')
            records._exclusive(directory,name,raw,bound['context']['uid'])
        else:
            rotation.require(current==expected,'task record CAS changed')
            temp='retention-task-'+uuid.uuid4().hex+'.pending'
            records._exclusive(directory,temp,raw,bound['context']['uid'])
            os.replace(temp,name,src_dir_fd=directory,dst_dir_fd=directory)
        os.fsync(directory)


def reserve_bootstrap_second(manager):
    """Explicit ONLINE owner entry, after first new-binding recovery proof."""
    _binding(manager);adapter=_adapter(manager)
    with manager.lock():
        _,index=adapter.read_index();state=adapter.state()
        published=state.get(receipt.INDEX) or {}
        pointer={k:published.get(k) for k in ('receipt','receipt_sha256')}
        if index['bootstrap'] is not None:
            rotation.require(index['bootstrap']['first']==pointer and not index['kept']
                and not index['retired'] and index['staged'] is None,'different bootstrap work must finish')
            adapter.load_verified(pointer)
            return dict(index['bootstrap'],status='bootstrap_second_reserved')
        result=rotation.pre_backup_gate(adapter,bootstrap_pointer=pointer)
        return dict(first=result['first'],task_id=result['task_id'],status='bootstrap_second_reserved')


def local_update_slot_locked(manager):
    """Stopped local update consumes ONLINE reservation, never calls running gate."""
    _binding(manager);adapter=_adapter(manager)
    _,index=adapter.read_index();seed=index['bootstrap']
    rotation.require(seed is not None and not index['kept'] and not index['retired']
        and index['staged'] is None,'local cutover requires its pre-reserved second-version slot')
    actual=manager.state.get(receipt.INDEX) or {}
    rotation.require({k:actual.get(k) for k in ('receipt','receipt_sha256')}==seed['first'],
        'first authenticated business point changed')
    adapter.load_verified(seed['first'])
    # This collector can read a stopped state; state() deliberately cannot.
    rotation.validate_references(adapter.collect_active_references(manager.state),manager.state,index['contract_sha256'])
    return seed


def local_job_locked(manager,job,*,complete=False,proof=None):
    _binding(manager);adapter=_adapter(manager)
    path=manager.root/'backups'/proof['backup_filename'] if proof else job
    adapter.record_job_locked(_job(job.name,'backup','completed' if complete else 'active',path,
        None if proof is None else proof['backup_sha256']))


def capture_locked(manager,password,*,restart_allowed=None):
    """Ordinary/nightly admission BEFORE stop; one local complete capture only."""
    binding=_binding(manager);adapter=_adapter(manager)
    gate=rotation.pre_backup_gate(adapter)
    rotation.require(gate['status']=='allowed','first pair must be explicitly bootstrapped')
    task_id=uuid.uuid4().hex;job=manager.root/'staging'/('smb-stream-'+task_id)
    adapter.record_job_locked(_job(job.name,'backup','active',job))
    os.mkdir(job,0o700)
    running=bool(manager._process());attempted=False
    try:
        manager._backup_preflight(Path('tianming-smb-stream:'+binding.control['contract_sha256']))
        manager.stop()
        inputs=manager._backup_inputs()
        with receipt.builder._private_creation():payload=manager._build_backup_payload(password,job,inputs)
        with _folder(binding,job) as (_,folder,held):
            rotation.require(payload['encrypted'].parent==job,'captured cipher escaped its owned job')
            source=held.enter_context(Lease(payload['encrypted'],uid=folder.uid,gid=folder.gid,mode=0o600,parent=folder))
            rotation.require(os.fstat(source.fd).st_size==payload['size'] and work.digest(source)==payload['sha256'],
                'captured cipher identity/hash/size changed before durability')
            os.fsync(source.fd);os.fsync(folder.fd);os.fsync(folder.parent.fd)
            source.check();binding.check()
            facts=dict(cipher_sha256=payload['sha256'],raw_sha256=payload['raw_sha256'],
                manifest_sha256=rotation.digest(payload['manifest']),database=payload['database'],source_time=payload['manifest']['created'])
            plan=dict(type='tianming.nas-captured-task.v1',task_id=task_id,job_id=job.name,
                filename=payload['encrypted'].name,sha256=payload['sha256'],size=payload['size'],
                local_path=str(payload['encrypted']),proof=facts,roles=rotation.references(payload['state']),phase='captured')
            _publish(adapter,_task_name(task_id),plan,locked=True)
            with receipt.root_lease(binding.context) as root:
                before,state=manage.record(root,'state.json',binding.context)
                rotation.require(rotation.references(state)==plan['roles'],'capture state changed before readiness')
                state['backup_rotation_task_id']=task_id
                manage.replace_record(root,'state.json',state,before,binding.context,binding.check)
            source.check();binding.check()
            attempted=True
            if running and (restart_allowed is None or restart_allowed()):manager.start()
            rotation.require(manager._process() is not None and manager.state.get('manual_stop') is False,
                'ERP readiness failed; retain this exact capture, do not generate another')
            return plan
    except Exception:
        # Retained, unfinished package remains an active task, blocking a fourth.
        # Its owner must explicitly resolve it before asserting failed_closed.
        if running and not attempted and (restart_allowed is None or restart_allowed()):manager.start()
        raise


class _WorkerArchive:
    """Existing signed upload/download dispatcher; each call owns its connection."""
    def __init__(self,binding,job):self.binding=binding;self.job=job;self.value=binding.value
    def _call(self,action,fd,filename,size,cipher_sha,attempt_id,upload=None):
        b=self.binding
        request=dict(challenge=uuid.uuid4().hex,control_sha256=b.control_sha,
            contract_sha256=b.control['contract_sha256'],action=action,socket_fd=2**31-1,
            file_fd=fd,job=self.job,filename=filename,size=size,sha256=cipher_sha,
            attempt_id=attempt_id,upload=upload)
        runtime._request(request,b);runtime.verify_network_policy(b)
        return runtime._dispatch(b,request)
    def upload(self,fd,filename,size,cipher_sha,attempt_id):
        return self._call('upload',fd,filename,size,cipher_sha,attempt_id)
    def download(self,fd,upload):
        return self._call('download',fd,upload['filename'],upload['bytes'],upload['sha256'],uuid.uuid4().hex,upload)


@contextmanager
def _folder(binding,job):
    with ExitStack() as stack:
        root=stack.enter_context(receipt.root_lease(binding.context))
        staging=stack.enter_context(Lease(root.path/'staging',uid=root.uid,gid=root.gid,mode=0o700,
            parent=root,directory=True,mutable=True))
        folder=stack.enter_context(Lease(job,uid=root.uid,gid=root.gid,mode=0o700,parent=staging,directory=True,mutable=True))
        yield root,folder,stack


def _restore(manager,job_id,downloaded,password,raw,observed,receipt_sha):
    """Full existing Manager.restore, isolated fixed owner task, no production start."""
    from .manager import Manager
    from .cleanup import remove_owned_tree
    # No arbitrary restore target and no recursive copy of the running instance.
    owner=manager.root/'control'/'rotation-recovery'
    owner.mkdir(mode=0o700,exist_ok=True)
    rotation.require(owner.resolve()==owner and not owner.is_symlink(),'recovery owner changed')
    target=owner/job_id
    rotation.require(not target.exists(),'unfinished isolated recovery requires its owner; no guessed closure')
    with receipt.builder._private_creation():
        restored=Manager(target,manager.public_key)
        restored.restore(downloaded,password,expected_backup_sha256=observed['sha256'])
        report=rotation.capture_verified_recovery(restored,observed,receipt_sha,downloaded,raw)
    # Compact proof is returned before optional cleanup; failed cleanup is visible.
    try:cleanup={'deleted_bytes':remove_owned_tree(target,owner),'status':'removed'}
    except (OSError,ValueError):cleanup={'status':'retained','reason':'isolated recovery cleanup guard refused'}
    return report,cleanup


def _local_copy(root,binding,plan,folder):
    filename=plan['filename'];path=root.path/'backups'/filename
    if path.exists():
        with receipt.copy_lease(root,binding.context,filename) as copy:
            rotation.require(sha(path)==plan['sha256'] and os.fstat(copy.fd).st_size==plan['size'],'existing local cipher changed')
            return dict(path=str(path),sha256=plan['sha256'],size=plan['size'],identity=receipt.fingerprint(os.fstat(copy.fd)))
    return receipt._copy_authenticated(root,binding.context,dict(plan,job=str(folder.path)))


def _publish_receipt(manager,binding,plan,value):
    """CAS only backup metadata; retain the old immutable receipt and all roles."""
    adapter=_adapter(manager);raw=cfg.canonical(value)
    name=receipt.reauthenticated_receipt_name(value) if plan.get('bootstrap_first') else receipt.receipt_name(value['filename'])
    pointer=dict(receipt=name,receipt_sha256=cfg.digest(raw))
    receipt.validate(value,dict(context=binding.context,control=binding.control,value=binding.value,control_sha=binding.control_sha))
    _publish(adapter,name,value)
    with manager.lock(),receipt.root_lease(binding.context) as root:
        previous,state=manage.record(root,'state.json',binding.context)
        current=state.get(receipt.INDEX) or {}
        actual={k:current.get(k) for k in ('receipt','receipt_sha256')}
        allowed=(plan.get('prior_pointer'),pointer)
        rotation.require(actual in allowed or rotation.references(state)==plan['roles'],'backup publication changed during worker')
        if actual!=pointer:
            for role in rotation.ROLE_KEYS[:8]:
                rotation.require(state.get(role)==plan['roles'].get(role),'runtime role changed during independent verification')
            state.update(last_backup=value['local_copy']['path'],last_backup_at=value['verified_at'],backup_error=None)
            state[receipt.INDEX]=dict(path=value['local_copy']['path'],**pointer)
            state['backup_rotation_task_id']=plan['task_id']
            manage.replace_record(root,'state.json',state,previous,binding.context,binding.check)
    return pointer


def _cleanup_owned(manager,binding,plan,receipt_value):
    """Only this verified task's exact held files; failures keep task active."""
    job=manager.root/'staging'/plan['job_id'];adapter=_adapter(manager)
    expected_raw=records.canonical(plan)
    def protect():
        binding.check()
        rotation.require(_read(adapter,_task_name(plan['task_id']))[0]==expected_raw,'task changed during exact cleanup')
        state=adapter.state();current=state.get(receipt.INDEX) or {}
        rotation.require({k:current.get(k) for k in ('receipt','receipt_sha256')}==plan['pointer'],
            'published cipher changed during exact cleanup')
    if plan.get('work'):
        with _folder(binding,job) as (root,folder,_):
            with receipt.copy_lease(root,binding.context,plan['filename']) as copy:
                work.retire(root,binding.context,dict(job=str(job),filename=plan['filename'],sha256=plan['sha256'],work=plan['work']),
                    copy,cfg.canonical(receipt_value),protect)
        return {'status':'removed','removed_bytes':sum(v['identity']['size'] for v in plan['work']['files'].values())}
    expected={plan['filename']:plan['sha256'],'download.tmbackup':plan['sha256'],
        'readback-authenticated.zip':plan['proof']['raw_sha256']}
    held={}
    with _folder(binding,job) as (_,folder,_):
        rotation.require(set(os.listdir(folder.fd))==set(expected),'owned temporary inventory changed')
        try:
            for name,wanted in expected.items():
                item=Lease(job/name,uid=folder.uid,gid=folder.gid,mode=0o600,parent=folder)
                held[name]=item
                rotation.require(work.digest(item)==wanted,'owned temporary bytes changed')
            removed=[];total=sum(os.fstat(item.fd).st_size for item in held.values())
            for name,item in held.items():
                protect();folder.check()
                rotation.require(set(os.listdir(folder.fd))==set(expected)-set(removed),'owned temporary names changed')
                for other,value in held.items():
                    if other not in removed:value.check()
                os.unlink(name,dir_fd=folder.fd)
                rotation.require(os.fstat(item.fd).st_nlink==0,'owned unlink not confirmed')
                removed.append(name);os.fsync(folder.fd)
            rotation.require(not os.listdir(folder.fd),'owned temporary closure incomplete')
        finally:
            for item in held.values():os.close(item.fd)
    return {'status':'removed','removed_bytes':total}


def _closed_update_reference(manager,adapter,state,path,cipher_sha):
    """A completed local cutover keeps small actual proofs, not a cipher lease."""
    proof=state.get('local_code_update') or {}
    slot=proof.get('rotation_slot') or {}
    rotation.require(proof.get('backup_sha256')==cipher_sha and proof.get('backup_filename')==Path(path).name
        and proof.get('job_id')=='local-code-update-'+slot.get('task_id',''),
        'update backup has no exact completed local owner')
    _,closed=_read(adapter,_task_name(slot.get('task_id')))
    rotation.require(closed.get('phase')=='completed' and closed.get('sha256')==cipher_sha
        and closed.get('proof')==proof.get('authenticated_recovery') and closed.get('local_path')==path,
        'update backup still has unfinished transfer/recovery dependency')
    with adapter.records() as (_,bound,root,_):
        with records._record_directory(root.fd,'staging',bound['context']['uid'],os.fstat(root.fd).st_dev) as stage:
            with records._record_directory(stage,proof['job_id'],bound['context']['uid'],os.fstat(stage).st_dev) as job:
                _,actual=records._local_object(job,'verified.json',bound['context']['uid'])
                rotation.require(actual==proof,'completed update audit proof changed')
    adapter.load_verified(closed['pointer'])


def _local_retention(manager,binding,plan):
    """One current full local cipher; only authenticated indexed predecessors.

    Hash IO runs outside the short lock. Immutable receipts/recovery/local proof
    remain. Unknown files or genuine active/migration/restore references block;
    they are never silently adopted or classified as completed history.
    """
    adapter=_adapter(manager);index_sha,index=adapter.read_index()
    rotation.require(len(index['kept'])==2 and index['kept'][0]==plan['pointer'] and index['staged'] is None,
        'local retention requires the verified newest pair')
    latest=adapter.load_verified(plan['pointer'])['receipt']
    known={}
    for pointer in plan['local_predecessors']:
        value=adapter.load_verified(pointer)['receipt']
        if value['filename']!=latest['filename']:
            rotation.require(value['filename'] not in known or known[value['filename']]==value,'ambiguous local predecessor')
            known[value['filename']]=value
    state=adapter.state();state_sha=rotation.digest(state)
    rotation.require(not state.get('onboarding_pending') and not state.get('backup_work_retained')
        and state.get('operation') in (None,'import_existing','update','rollback','restore','migration_recovered'),
        'local cleanup has pending maintenance dependencies')
    def check_references():
        fresh=adapter.state();active=adapter.active_references(fresh)
        rotation.require(rotation.digest(fresh)==state_sha and adapter.read_index()[0]==index_sha,
            'local cleanup state/index CAS changed')
        actual=fresh.get(receipt.INDEX) or {}
        rotation.require(fresh.get('last_backup')==latest['local_copy']['path']
            and {k:actual.get(k) for k in ('receipt','receipt_sha256')}==plan['pointer'],
            'current complete local backup changed')
        for value in known.values():
            path=value['local_copy']['path'];cipher_sha=value['sha256']
            rotation.require(cipher_sha not in rotation._protected_sha(fresh)
                and cipher_sha not in active['active_sha256']+active['rehearsal_sha256']
                and not any(rotation._comparison_key(path)==rotation._comparison_key(name)
                    for name in active['active_paths']+active['rehearsal_paths']),
                'old local backup is an active restore/migration/rehearsal input')
            # Receipt references protect active owners, even when no path/hash
            # was recorded. Both original and binding-specific aliases count.
            protected_receipts={rotation._comparison_key(receipt.receipt_name(value['filename'])),
                rotation._comparison_key(receipt.reauthenticated_receipt_name(value))}
            rotation.require(not any(rotation._comparison_key(name) in protected_receipts for name in active['active_receipts']),
                'old local backup has an active receipt owner')
            if fresh.get('update_backup')==path:
                _closed_update_reference(manager,adapter,fresh,path,cipher_sha)
        binding.check()
    check_references()
    removed=[];total=0
    with receipt.root_lease(binding.context) as root:
        with Lease(root.path/'backups',uid=root.uid,gid=root.gid,mode=0o700,parent=root,directory=True,mutable=True) as folder:
            names=set(os.listdir(folder.fd));allowed={latest['filename'],*known}
            rotation.require(names<=allowed and latest['filename'] in names,
                'unclassified local backup/cache remains; retain and obtain owner evidence')
            with Lease(folder.path/latest['filename'],uid=root.uid,gid=root.gid,mode=0o600,parent=folder) as current:
                rotation.require(receipt.fingerprint(os.fstat(current.fd))==latest['local_copy']['identity']
                    and work.digest(current)==latest['sha256'],'latest full local cipher identity/hash changed')
                for name in sorted(names-{latest['filename']}):
                    value=known[name]
                    rotation.require(value['local_copy']['path']==str(folder.path/name),'predecessor escaped fixed backups')
                    old=Lease(folder.path/name,uid=root.uid,gid=root.gid,mode=0o600,parent=folder)
                    try:
                        rotation.require(receipt.fingerprint(os.fstat(old.fd))==value['local_copy']['identity']
                            and work.digest(old)==value['sha256'],'old local cipher identity/hash changed')
                        with manager.lock():
                            check_references();current.check();old.check();folder.check()
                            rotation.require(set(os.listdir(folder.fd))==names-set(removed),'local cleanup names changed')
                            os.unlink(name,dir_fd=folder.fd);os.fsync(folder.fd)
                            rotation.require(os.fstat(old.fd).st_nlink==0,'local cipher unlink not confirmed')
                            total+=value['size'];removed.append(name)
                    finally:os.close(old.fd)
                with manager.lock():
                    check_references();current.check();folder.check()
                    rotation.require(os.listdir(folder.fd)==[latest['filename']],'local single complete backup closure failed')
    return dict(status='one_complete',kept=latest['filename'],removed=removed,removed_bytes=total,
        already_absent=sorted(set(known)-names))


def _require_recovery_cleanup(manager,plan):
    """Retry only the existing isolated target; refusal prevents another copy."""
    if plan.get('recovery_cleanup',{}).get('status')=='removed':return
    from .cleanup import remove_owned_tree
    owner=manager.root/'control/rotation-recovery';target=owner/plan['job_id']
    rotation.require(owner.resolve()==owner and not owner.is_symlink(),'recovery owner changed')
    try:remove_owned_tree(target,owner)
    except (OSError,ValueError):
        raise ValueError('isolated recovery cleanup guard refused; retained owner must close before another backup') from None


def _complete_index(manager,binding,plan):
    adapter=_adapter(manager);pointer=plan['pointer'];name=_task_name(plan['task_id'])
    adapter.load_verified(pointer);_require_recovery_cleanup(manager,plan)
    # The immutable report exists and the original transfer/restore is closed.
    adapter.record_job(_job(plan['job_id'],'upload','completed',plan['local_copy']['path'],plan['sha256'],pointer))
    if plan.get('bootstrap_first'):indexed={'status':'first_authenticated_waiting_second'}
    else:
        if 'local_predecessors' not in plan:
            _,before=adapter.read_index()
            predecessors=list(before['kept'])+list(before['retired'])
            if before['bootstrap']:predecessors.append(before['bootstrap']['first'])
            raw,_=_read(adapter,name);updated=dict(plan,local_predecessors=predecessors)
            _publish(adapter,name,updated,expected=raw);plan=updated
        rotation.stage_candidate(adapter,pointer)
        _,index=adapter.read_index()
        indexed=rotation.advance(adapter,pointer,bootstrap=(index['bootstrap']['first'],pointer) if index['bootstrap'] else ())
        raw,_=_read(adapter,name);updated=dict(plan,phase='indexed',indexed=indexed)
        _publish(adapter,name,updated,expected=raw);plan=updated
        local=_local_retention(manager,binding,plan)
        plan=dict(plan,local_retention=local)
    raw,_=_read(adapter,name);updated=dict(plan,phase='completed',indexed=indexed)
    _publish(adapter,name,updated,expected=raw)
    if not plan.get('bootstrap_first'):runtime.retire_next()
    return dict(status=indexed['status'],backup=plan['local_copy']['path'],pointer=pointer,cleanup=plan['cleanup'],
        local_retention=plan.get('local_retention'))


def finish(manager,password,plan):
    """Actual post-readiness upload/readback/restore/index/one-retirement pipeline."""
    binding=_binding(manager);adapter=_adapter(manager);adapter.state()
    name=_task_name(plan['task_id']);saved,actual=_read(adapter,name)
    rotation.require(actual==plan,'caller cannot replace the protected capture record')
    if plan['phase']=='completed':
        rotation.require(adapter.load_verified(plan['pointer']) is not None,'completed proof missing')
        if not plan.get('bootstrap_first'):runtime.retire_next()
        return dict(status='replayed',backup=plan['local_copy']['path'],pointer=plan['pointer'])
    if plan['phase']=='indexed' or (plan['phase']=='verified' and plan.get('cleanup',{}).get('status')=='removed'):
        return _complete_index(manager,binding,plan)
    if plan['phase']=='verified':
        cleanup=_cleanup_owned(manager,binding,plan,adapter.load_receipt(plan['pointer']))
        updated=dict(plan,cleanup=cleanup);_publish(adapter,name,updated,expected=saved)
        return _complete_index(manager,binding,updated)
    job=manager.root/'staging'/plan['job_id']
    rotation.require(job.name=='smb-stream-'+plan['task_id'] and job.parent==manager.root/'staging','fixed stream task required')
    with _folder(binding,job) as (root,folder,held):
        if plan['phase']=='captured':
            source_path=Path(plan['local_path'])
            rotation.require(source_path in (job/plan['filename'],manager.root/'backups'/plan['filename']),'source must be existing owned cipher')
            # Protocol file_lease requires a fixed staging file. Local cutover and
            # first reauth stage only these existing bytes; never repack live DB.
            staged=job/plan['filename']
            if source_path!=staged:
                with receipt.copy_lease(root,binding.context,plan['filename']) as source:
                    fd=os.open(staged,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600)
                    with os.fdopen(fd,'wb') as output:
                        offset=0
                        while block:=os.pread(source.fd,1024*1024,offset):output.write(block);offset+=len(block)
                        output.flush();os.fsync(output.fileno())
                    source.check()
            source=held.enter_context(Lease(staged,uid=root.uid,gid=root.gid,mode=0o600,parent=folder))
            rotation.require(sha(staged)==plan['sha256'] and os.fstat(source.fd).st_size==plan['size'],'captured cipher changed')
            fd=os.open(job/'download.tmbackup',os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600)
            download=held.enter_context(Lease(job/'download.tmbackup',uid=root.uid,gid=root.gid,mode=0o600,parent=folder,fd=fd,mutable=True))
            upload=_WorkerArchive(binding,job.name);readback=_WorkerArchive(binding,job.name)
            if plan.get('bootstrap_first'):
                prior=plan['old_receipt']['transfer']['upload']
                transfer=dict(upload=prior,download=readback.download(download.fd,prior))
                receipt.builder._proofs(binding.value,dict(encrypted=staged,size=plan['size'],sha256=plan['sha256']),transfer)
            else:
                transfer=stream.transfer_existing_ciphertext(upload,readback,source.fd,download.fd,
                    plan['filename'],plan['size'],plan['sha256'],uuid.uuid4().hex)
            source.check();download.check()
            updated=dict(plan,phase='transferred',transfer=transfer,verified_at=datetime.now(timezone.utc).isoformat())
            _publish(adapter,name,updated,expected=saved);saved=records.canonical(updated);plan=updated
        downloaded=job/'download.tmbackup';raw=job/'readback-authenticated.zip'
        if raw.exists():
            rotation.require(sha(raw)==plan['proof']['raw_sha256'],'retained authenticated raw changed')
        else:
            with receipt.builder._private_creation():stream.authenticate_existing_readback(downloaded,raw,password,plan['proof'])
        rotation.require(sha(downloaded)==plan['sha256'],'independent downloaded cipher changed')
        local_copy=_local_copy(root,binding,plan,folder)
        value=dict(type=receipt.TYPE,context_sha256=binding.control['context_sha256'],control_sha256=binding.control_sha,
            contract_sha256=binding.control['contract_sha256'],package_sha256=binding.control['package_sha256'],
            filename=plan['filename'],sha256=plan['sha256'],size=plan['size'],raw_sha256=plan['proof']['raw_sha256'],
            database=plan['proof']['database'],verified_at=plan['verified_at'],transfer=plan['transfer'],local_copy=local_copy)
        pointer=_publish_receipt(manager,binding,plan,value)
        if not plan.get('bootstrap_first'):rotation.stage_candidate(adapter,pointer)
        recovery_name=rotation.recovery_name(value,pointer['receipt_sha256'])
        try:_,report=_read(adapter,recovery_name);rotation.validate_recovery(report,value,pointer['receipt_sha256']);cleanup={'status':'replayed'}
        except FileNotFoundError:
            with manager.lock():adapter.record_job_locked(_job(job.name,'rehearsal','active',downloaded,plan['sha256'],pointer))
            report,cleanup=_restore(manager,job.name,downloaded,password,raw,value,pointer['receipt_sha256'])
            adapter.publish_recovery(pointer,report)
        owned_work=None
        if set(os.listdir(folder.fd))==set(work.names(plan['filename'])):
            with zipfile.ZipFile(raw) as archived:manifest=json.loads(archived.read('manifest.json'))
            owned_work=work.capture(folder,dict(encrypted=Path(plan['filename']),sha256=plan['sha256'],
                raw_sha256=plan['proof']['raw_sha256'],manifest=manifest))
        updated=dict(plan,phase='verified',pointer=pointer,local_copy=local_copy,recovery_cleanup=cleanup,work=owned_work)
        _publish(adapter,name,updated,expected=saved);plan=updated
    cleanup=_cleanup_owned(manager,binding,plan,value)
    raw,_=_read(adapter,name);updated=dict(plan,cleanup=cleanup)
    _publish(adapter,name,updated,expected=raw)
    return _complete_index(manager,binding,updated)


def capture_first_authority(manager,password):
    """Explicit owner entry BEFORE replacing the SMB binding.

    The candidate caller must itself already be root-published/approved. Read the
    OLD authority through its actual validator, and authenticate the original
    local ciphertext/manifest plus real Manager.restore, while ERP keeps running.
    """
    from . import mac_published_runtime as published,mac_system_service as system
    tree=Path(__file__).resolve().parent.parent
    identity=tree.parent.name
    meta=system.metadata(identity)
    approved,manifest=published.resolve(manager.root,identity,meta['public'],kind='migration')
    rotation.require(approved==tree and meta['context']==runtime.keychain.load_context(),'bootstrap code approval changed')
    cfg.rotation_capability(manifest)
    bound=receipt.approval();context=bound['context'];adapter=_adapter(manager)
    rotation.require(manager.root==cfg.BASE/'instance' and manager._process() is not None
        and manager.state.get('manual_stop') is False,'bootstrap authentication must keep ERP online')
    state=manager.state;old_pointer={k:state[receipt.INDEX][k] for k in ('receipt','receipt_sha256')}
    with adapter.records() as (_,actual,root,directory):
        rotation.require(actual==bound,'old authority changed')
        raw,old=records._local_object(directory,old_pointer['receipt'],context['uid'])
        rotation.require(cfg.digest(raw)==old_pointer['receipt_sha256'],'old receipt bytes changed')
        receipt.validate(old,bound)
        try:_,saved=records._local_object(directory,AUTHORITY,context['uid'])
        except FileNotFoundError:saved=None
        if saved is not None:
            rotation.require(saved['old_pointer']==old_pointer and saved['old_bound']==bound
                and saved['old_receipt']==old and sha(Path(old['local_copy']['path']))==old['sha256'],
                'different bootstrap authority already retained')
            rotation.validate_recovery(saved['recovery'],old,old_pointer['receipt_sha256'])
            _require_recovery_cleanup(manager,dict(job_id=saved['job_id'],recovery_cleanup=saved['cleanup']))
            adapter.record_job(_job(saved['job_id'],'rehearsal','completed',old['local_copy']['path'],old['sha256'],old_pointer))
            return saved
    task_id=uuid.uuid4().hex;job_id='smb-stream-'+task_id;job=manager.root/'staging'/job_id
    adapter.record_job(_job(job_id,'rehearsal','active',old['local_copy']['path'],old['sha256'],old_pointer))
    job.mkdir(mode=0o700)
    raw_zip=job/'bootstrap-authenticated.zip'
    with receipt.root_lease(context) as root,receipt.copy_lease(root,context,old['filename']) as source:
        rotation.require(receipt.fingerprint(os.fstat(source.fd))==old['local_copy']['identity'],'original local cipher identity changed')
        with receipt.builder._private_creation():cipher_sha=decrypt_file(source.path,raw_zip,password)
        rotation.require(cipher_sha==old['sha256'] and sha(raw_zip)==old['raw_sha256'],'original local cipher/raw changed')
        with zipfile.ZipFile(raw_zip) as archive:manifest=json.loads(archive.read('manifest.json'))
        facts=dict(cipher_sha256=cipher_sha,raw_sha256=sha(raw_zip),manifest_sha256=rotation.digest(manifest),
            database=manifest['database'],source_time=manifest['created'])
        rotation.require(manifest['database']==old['database'],'original receipt database changed')
        report,cleanup=_restore(manager,job_id,source.path,password,raw_zip,old,old_pointer['receipt_sha256'])
        source.check()
    authority=dict(type='tianming.nas-bootstrap-authority.v1',job_id=job_id,old_pointer=old_pointer,old_bound=bound,
        old_receipt=old,old_receipt_bytes=raw.hex(),old_control_bytes=cfg.canonical(bound['control']).hex(),
        proof=facts,recovery=report,cleanup=cleanup)
    _publish(adapter,AUTHORITY,authority)
    # Exact own plaintext only; no old task adoption or unrelated cleanup.
    rotation.require(set(p.name for p in job.iterdir())=={'bootstrap-authenticated.zip'},'bootstrap staging changed')
    raw_zip.unlink();job.rmdir()
    _require_recovery_cleanup(manager,dict(job_id=job_id,recovery_cleanup=cleanup))
    adapter.record_job(_job(job_id,'rehearsal','completed',old['local_copy']['path'],old['sha256'],old_pointer))
    return authority


def reauthenticate_first(manager,password):
    """After new binding, fresh DOWNLOAD only of the same authenticated old file."""
    binding=_binding(manager);adapter=_adapter(manager);adapter.state()
    _,authority=_read(adapter,AUTHORITY)
    _require_recovery_cleanup(manager,dict(job_id=authority['job_id'],recovery_cleanup=authority['cleanup']))
    old=authority['old_receipt'];old_bound=authority['old_bound']
    receipt.validate(old,old_bound)
    rotation.require(bytes.fromhex(authority['old_receipt_bytes'])==cfg.canonical(old)
        and cfg.digest(bytes.fromhex(authority['old_receipt_bytes']))==authority['old_pointer']['receipt_sha256']
        and bytes.fromhex(authority['old_control_bytes'])==cfg.canonical(old_bound['control'])
        and cfg.digest(bytes.fromhex(authority['old_control_bytes']))==old_bound['control_sha'],
        'original authority bytes changed')
    rotation.require(old_bound['context']==binding.context and old_bound['value']==binding.value
        and old_bound['control']['contract_sha256']==binding.control['contract_sha256'],
        'old authenticated storage/identity differs; no contract translation')
    rotation.require(cfg.digest(cfg.canonical(old))==authority['old_pointer']['receipt_sha256'],
        'old authority raw receipt digest changed')
    rotation.validate_recovery(authority['recovery'],old,authority['old_pointer']['receipt_sha256'])
    alias=receipt.reauthenticated_receipt_name(dict(old,control_sha256=binding.control_sha))
    actual=manager.state.get(receipt.INDEX) or {}
    if actual.get('receipt')==alias:
        pointer={k:actual[k] for k in ('receipt','receipt_sha256')};adapter.load_verified(pointer)
        _,pending=_read(adapter,_task_name(manager.state['backup_rotation_task_id']))
        rotation.require(pending.get('bootstrap_first') is True and pending.get('pointer')==pointer,
            'first authenticated task owner differs')
        if pending['phase']!='completed':return finish(manager,password,pending)
        return dict(status='replayed',pointer=pointer,backup=old['local_copy']['path'])
    rotation.require({k:actual.get(k) for k in ('receipt','receipt_sha256')}==authority['old_pointer'],
        'another original receipt needs authenticated owner review')
    task_id=uuid.uuid4().hex;job_id='smb-stream-'+task_id;job=manager.root/'staging'/job_id
    adapter.record_job(_job(job_id,'upload','active',old['local_copy']['path'],old['sha256']))
    job.mkdir(mode=0o700)
    plan=dict(type='tianming.nas-captured-task.v1',task_id=task_id,job_id=job_id,
        filename=old['filename'],sha256=old['sha256'],size=old['size'],local_path=old['local_copy']['path'],
        proof=authority['proof'],roles=rotation.references(manager.state),phase='captured',
        prior_pointer=authority['old_pointer'],bootstrap_first=True,old_receipt=old)
    _publish(adapter,_task_name(task_id),plan)
    return finish(manager,password,plan)


def resume_local_update(manager,password):
    """Existing backup action resumes the ONE cold ciphertext after ERP readiness."""
    binding=_binding(manager);adapter=_adapter(manager);adapter.state()
    state=manager.state;proof=state.get('local_code_update') or {}
    slot=proof.get('rotation_slot');job_id=proof.get('job_id','')
    rotation.require(isinstance(slot,dict) and job_id=='local-code-update-'+slot.get('task_id','')
        and proof.get('current')==state['current'] and proof.get('previous')==state.get('previous'),
        'approved cold capture/slot missing')
    with adapter.records() as (_,bound,root,_):
        with records._record_directory(root.fd,'staging',bound['context']['uid'],os.fstat(root.fd).st_dev) as stages:
            with records._record_directory(stages,job_id,bound['context']['uid'],os.fstat(root.fd).st_dev) as job:
                _,verified=records._local_object(job,'verified.json',bound['context']['uid'])
        rotation.require(verified==proof,'local cold recovery owner record changed')
    task_id=slot['task_id'];name=_task_name(task_id)
    try:_,plan=_read(adapter,name)
    except FileNotFoundError:
        _,index=adapter.read_index()
        rotation.require(index['bootstrap']==slot and index['staged'] is None and not index['retired'],
            'reserved cold-capture task changed')
        local_path=manager.root/'backups'/proof['backup_filename']
        rotation.require(state.get('update_backup')==str(local_path) and sha(local_path)==proof['backup_sha256'],
            'existing local cold cipher changed')
        stream_job='smb-stream-'+task_id;job=manager.root/'staging'/stream_job
        adapter.record_job(_job(stream_job,'upload','active',local_path,proof['backup_sha256']))
        job.mkdir(mode=0o700)
        plan=dict(type='tianming.nas-captured-task.v1',task_id=task_id,job_id=stream_job,
            filename=proof['backup_filename'],sha256=proof['backup_sha256'],size=proof['backup_bytes'],
            local_path=str(local_path),proof=proof['authenticated_recovery'],roles=rotation.references(state),phase='captured',
            prior_pointer=slot['first'])
        _publish(adapter,name,plan)
    rotation.require(plan['sha256']==proof['backup_sha256'] and plan['proof']==proof['authenticated_recovery'],
        'same task cannot be used for another local cold cipher')
    return finish(manager,password,plan)



def pending_local_update(manager):
    """No new capture while this approved cold task still owns bootstrap."""
    _binding(manager);proof=manager.state.get('local_code_update') or {}
    slot=proof.get('rotation_slot')
    if not slot:return False
    _,index=_adapter(manager).read_index()
    if index['bootstrap']==slot:return True
    try:_,plan=_read(_adapter(manager),_task_name(slot['task_id']))
    except FileNotFoundError:return False
    return plan['phase']!='completed'



def pending_locked(manager):
    """Return only known persistent work, before another capture can be admitted."""
    _binding(manager);adapter=_adapter(manager);_,index=adapter.read_index()
    if index['retired']:return {'kind':'retired'}
    if pending_local_update(manager):return {'kind':'local'}
    task_id=manager.state.get('backup_rotation_task_id')
    if task_id is not None:
        _,plan=_read(adapter,_task_name(task_id))
        if plan['phase']!='completed':return {'kind':'captured','plan':plan}
    rotation.require(index['staged'] is None,'unowned staged recovery must finish before a new capture')
    return None


def finish_pending(manager,password,pending):
    if pending['kind']=='retired':
        runtime.retire_next()
        return {'backup':manager.state['last_backup'],'status':'retirement_resumed'}
    if pending['kind']=='local':return resume_local_update(manager,password)
    if pending['kind']=='captured':return finish(manager,password,pending['plan'])
    raise ValueError('unknown pending owner task')


def retained_recovery_targets(manager):
    """Read-only exact successful-proof targets for the original root guard.

    Root may remove only these verified owned targets using remove_owned_tree;
    it does not supply a success JSON or modify our protected report. Repeating
    the SAME capture_first_authority/reauthenticate_first/resume_local_update or
    ordinary backup then observes absence and continues without another restore.
    """
    adapter=_adapter(manager);result=[]
    try:_,authority=_read(adapter,AUTHORITY)
    except FileNotFoundError:authority=None
    if authority and authority['cleanup']['status']=='retained':
        receipt.validate(authority['old_receipt'],authority['old_bound'])
        rotation.validate_recovery(authority['recovery'],authority['old_receipt'],authority['old_pointer']['receipt_sha256'])
        result.append(dict(job_id=authority['job_id'],entry='capture_first_authority',pointer=authority['old_pointer'],
            proof=authority['proof'],recovery=authority['recovery']))
    task_id=manager.state.get('backup_rotation_task_id')
    if task_id:
        _,plan=_read(adapter,_task_name(task_id))
        if plan.get('recovery_cleanup',{}).get('status')=='retained':
            verified=adapter.load_verified(plan['pointer'])
            result.append(dict(job_id=plan['job_id'],entry='reauthenticate_first' if plan.get('bootstrap_first') else 'finish',
                task_id=task_id,pointer=plan['pointer'],proof=plan['proof'],recovery_sha256=verified['recovery_sha256']))
    for value in result:
        owner=manager.root/'control/rotation-recovery';target=owner/value['job_id']
        rotation.require(re.fullmatch('smb-stream-[a-f0-9]{32}',value['job_id']) is not None
            and owner.resolve()==owner and not owner.is_symlink() and not target.is_symlink(),'fixed recovery target changed')
        value.update(owner=str(owner),target=str(target),exists=target.exists())
    return result
