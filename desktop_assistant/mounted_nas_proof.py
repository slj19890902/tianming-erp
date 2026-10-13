"""Explicit mounted-NFS proof for the already completed local code update.

Root hashes the actual NAS file while ERP may run. Later root opens only that
approved file and lends a read-only FD to the isolated service audit. Nothing
changes user-home permissions, uploads, stops, restores, or starts ERP. This
proof describes mounted filesystem readback, never SMB session observations.
"""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat

from desktop_assistant import current_mac_cutover_contract as c, mac_mount as mounts
from desktop_assistant import mac_system_upgrade as root

MODULE = 'desktop_assistant/mounted_nas_proof.py'
CAPABILITY = 'current_mac_mounted_audit_v1'
BASE = Path('/Library/TianmingERP')
ROOT = BASE/'instance'
MOUNT = Path('/Users/kimiyf/Public/ZSPACE')
SOURCE = '极空间.local:/极空间-个人'
NAS_ADDRESS = '172.16.3.96'
# Legacy logical NAS identifier, not the observed NFS transport address.
# The complete, unchanged kernel/remote observation is retained in the receipt.
PROXY_ADDRESS = '127.0.0.1'
PROXY_PORT = 52100
ARCHIVE = MOUNT/'sata1-18015598002/BoxERP/backups/mac-mini'
CONTROL = BASE/'mounted-nas-service.json'
CONTRACT = BASE/'mounted-nas-contract.json'
RECEIPT = BASE/'mounted-nas-receipt.json'
TYPE = 'tianming.mounted-filesystem-backup-proof.v1'
CONTROL_TYPE = 'tianming.mounted-filesystem-audit-control.v1'
CONTRACT_TYPE = 'tianming.mounted-filesystem-contract.v1'
IDENTITY = ('dev','ino','uid','gid','mode','nlink','size','mtime_ns','ctime_ns')
ERROR = 'Mounted NAS proof unconfirmed; preserve files, no fallback or ERP action'


def require(ok):
    if not ok: raise ValueError(ERROR)


def fingerprint(info):
    return {key:getattr(info,'st_'+key) for key in IDENTITY}


@contextmanager
def open_regular(path):
    """Open each absolute path component without following links; no chmod."""
    path = Path(path)
    require(path.is_absolute() and os.path.normpath(str(path)) == str(path))
    with ExitStack() as stack:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close,fd)
        for name in path.parts[1:-1]:
            fd = os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            stack.callback(os.close,fd)
        fd = os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
        stack.callback(os.close,fd)
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        yield fd


def full_hash(fd, size):
    require(type(size) is int and size > 0 and os.fstat(fd).st_size == size)
    before = fingerprint(os.fstat(fd)); digest = hashlib.sha256(); offset = 0
    while offset < size:
        chunk = os.pread(fd,min(1024*1024,size-offset),offset)
        require(bool(chunk)); digest.update(chunk); offset += len(chunk)
    require(not os.pread(fd,1,size) and fingerprint(os.fstat(fd)) == before)
    return digest.hexdigest()


def _path(path, filename):
    path = Path(path)
    require(path.name == filename and path.parent.parent == ARCHIVE
        and re.fullmatch('mounted-[0-9a-f]{32}',path.parent.name) is not None
        and re.fullmatch('[A-Za-z0-9_-]{1,128}\\.tmbackup',filename) is not None)
    return path


def observe():
    value = mounts.observe(MOUNT)
    kernel = value.get('kernel',{}); remote = value.get('remote',{})
    require(kernel.get('filesystem') == 'nfs' and kernel.get('mountpoint') == str(MOUNT)
        and kernel.get('source') == SOURCE
        and remote.get('export') == '/极空间-个人')
    direct = (value.get('classification') == 'direct_network_unbound'
        and remote.get('addresses') == [NAS_ADDRESS])
    proxy = (value.get('classification') == 'loopback_proxy'
        and remote.get('addresses') == [PROXY_ADDRESS]
        and remote.get('server') == '极空间.local'
        and remote.get('version') == '4.0' and remote.get('port') == PROXY_PORT
        and remote.get('security') == 'sys' and remote.get('tcp') is True
        and kernel.get('owner') == 501)
    require(direct or proxy)
    return value


def local_update(state):
    proof = state.get('local_code_update')
    require(isinstance(proof,dict) and set(proof) == {'type','storage','independent_device',
        'nas_verified','backup_sha256','backup_bytes','current','previous','database_sha256',
        'shared_files','manual_stop','restored_verified','factory_activated'}
        and proof['type'] == 'tianming.local-code-update.v1' and proof['storage'] == 'local'
        and proof['independent_device'] is False and proof['nas_verified'] is False
        and proof['manual_stop'] is True and proof['restored_verified'] is True
        and proof['factory_activated'] is False and proof['current'] == state.get('current')
        and proof['previous'] == state.get('previous') and proof['current'] != proof['previous']
        and state.get('operation') == 'update')
    for key in ('backup_sha256','current','previous','database_sha256'): c._hash(proof[key])
    require(type(proof['backup_bytes']) is int and proof['backup_bytes'] > 0
        and type(proof['shared_files']) is int and proof['shared_files'] > 0)
    backup = Path(state.get('update_backup',''))
    require(backup.parent == ROOT/'backups' and backup.is_absolute())
    return proof,backup


def _original_proof(state):
    proof,backup = local_update(state); matches = []
    for folder in (ROOT/'staging').iterdir():
        if re.fullmatch('local-code-update-[0-9a-f]{32}',folder.name):
            path = folder/'verified.json'
            if path.exists():
                with open_regular(path) as fd:
                    info = os.fstat(fd)
                    require(info.st_uid == info.st_gid == 499 and stat.S_IMODE(info.st_mode) == 0o600)
                    raw = os.pread(fd,c.LIMIT+1,0)
                    if c.decode(raw) == proof: matches.append((path,raw))
    require(len(matches) == 1)
    return proof,backup,matches[0][0],matches[0][1]


def _protected(path):
    require(Path(path).is_absolute() and (Path(path).parent == BASE or BASE in Path(path).parents))
    for parent in Path(path).parents:root.boot.protected(parent,directory=True,readable=True)
    return root.boot.protected(path,readable=True)


def load():
    """Read protected public evidence only; no NAS IO or operational secrets."""
    raw = {p:_protected(p) for p in (CONTROL,CONTRACT,RECEIPT)}
    control,contract,receipt = (c.decode(raw[p]) for p in (CONTROL,CONTRACT,RECEIPT))
    require(set(control) == {'type','package_sha256','context_sha256','publisher_sha256',
        'contract_sha256','receipt_sha256'} and control['type'] == CONTROL_TYPE)
    require(set(contract) == {'type','mountpoint','source','nas_address','path',
        'local_path','local_update_proof_path','local_update_receipt_sha256','sha256','size',
        'checkpoint_attempt_id','checkpoint_result_sha256'}
        and contract['type'] == CONTRACT_TYPE and contract['mountpoint'] == str(MOUNT)
        and contract['source'] == SOURCE and contract['nas_address'] == NAS_ADDRESS)
    require(set(receipt) == {'type','verification_kind','contract_sha256','sha256','size',
        'verified_at','mount','file_identity','local_identity','local_update_receipt_sha256'}
        and receipt['type'] == TYPE and receipt['verification_kind'] == 'mounted_filesystem')
    for item in (control,contract,receipt):
        for key,value in item.items():
            if (key.endswith('_sha256') or key == 'sha256') and value is not None: c._hash(value)
    require(raw[CONTROL] == c.canonical(control) and raw[CONTRACT] == c.canonical(contract)
        and raw[RECEIPT] == c.canonical(receipt)
        and control['contract_sha256'] == c.digest(contract)
        and control['receipt_sha256'] == hashlib.sha256(raw[RECEIPT]).hexdigest()
        and receipt['contract_sha256'] == control['contract_sha256']
        and all(receipt[k] == contract[k] for k in ('sha256','size','local_update_receipt_sha256')))
    _path(contract['path'],Path(contract['local_path']).name)
    require(Path(contract['local_path']).parent == ROOT/'backups')
    if contract['checkpoint_attempt_id'] is None:
        require(contract['checkpoint_result_sha256'] is None)
    else:
        c._id(contract['checkpoint_attempt_id']); c._hash(contract['checkpoint_result_sha256'])
        require(checkpoint_result_hash(contract['checkpoint_attempt_id']) == contract['checkpoint_result_sha256'])
    proof_path = Path(contract['local_update_proof_path'])
    require(proof_path.name == 'verified.json' and proof_path.parent.parent == ROOT/'staging'
        and re.fullmatch('local-code-update-[0-9a-f]{32}',proof_path.parent.name))
    require(type(contract['size']) is int and contract['size'] > 0
        and datetime.fromisoformat(receipt['verified_at']) <= datetime.now(timezone.utc))
    c.normalize_timestamp(receipt['verified_at'])
    for key in ('file_identity','local_identity'):
        require(isinstance(receipt[key],dict) and set(receipt[key]) == set(IDENTITY)
            and all(type(v) is int and v >= 0 for v in receipt[key].values())
            and receipt[key]['size'] == contract['size'] and receipt[key]['nlink'] == 1
            and stat.S_ISREG(receipt[key]['mode']))
    return dict(control=control,contract=contract,receipt=receipt,raw=raw,
        control_sha=hashlib.sha256(raw[CONTROL]).hexdigest())


def approve(nas_cipher_path,audit_package_sha256,checkpoint_attempt_id=None):
    """Root-only, explicit one-shot approval after the mounted copy completes.

    Must run from the externally SHA-approved administrator pack; caller verifies
    that pack before invoking this function. Reads public state and full ciphertext
    only. No service write, backup-state rewrite or operational credential read.
    """
    root._entry(); c._hash(audit_package_sha256)
    from desktop_assistant.current_mac_activation_install import verify_code
    approved_code=verify_code()
    require(not os.path.lexists(BASE/'factory-activation.json'))
    meta = root.system.metadata(audit_package_sha256)
    manifest = root.system._program_manifest(meta)
    require(meta['context']['uid'] == meta['context']['gid'] == 499
        and type(manifest.get('runtime_capabilities',{}).get(CAPABILITY)) is int
        and manifest['runtime_capabilities'][CAPABILITY] == 1 and MODULE in manifest['files'])
    with root._publication(), ExitStack() as stack:
        base = stack.enter_context(root.Pin(BASE,mode=0o755,directory=True,mutable=True))
        instance = stack.enter_context(root.Pin(ROOT,uid=499,gid=499,mode=0o700,
            directory=True,mutable=True,parent=base))
        _,state = root._state(instance,499,499)
        proof,original_local,proof_path,proof_raw = _original_proof(state)
        checkpoint = checkpoint_result(checkpoint_attempt_id) if checkpoint_attempt_id is not None else None
        local = Path(checkpoint['local_path']) if checkpoint else original_local
        expected_sha = checkpoint['sha256'] if checkpoint else proof['backup_sha256']
        expected_size = checkpoint['size'] if checkpoint else proof['backup_bytes']
        if checkpoint:
            require(checkpoint['release_sha256'] == state['current']
                and checkpoint['local_update_receipt_sha256'] == hashlib.sha256(proof_raw).hexdigest())
        path = _path(nas_cipher_path,local.name)
        before = observe()
        with open_regular(path) as nas_fd, open_regular(local) as local_fd:
            require(mounts.fd_identity(nas_fd) == before['kernel'])
            require(full_hash(nas_fd,expected_size) == expected_sha
                and full_hash(local_fd,expected_size) == expected_sha)
            require(observe() == before and mounts.fd_identity(nas_fd) == before['kernel'])
            contract = dict(type=CONTRACT_TYPE,mountpoint=str(MOUNT),source=SOURCE,
                nas_address=NAS_ADDRESS,path=str(path),local_path=str(local),
                local_update_proof_path=str(proof_path),
                local_update_receipt_sha256=hashlib.sha256(proof_raw).hexdigest(),
                sha256=expected_sha,size=expected_size,checkpoint_attempt_id=checkpoint_attempt_id,
                checkpoint_result_sha256=(checkpoint_result_hash(checkpoint_attempt_id) if checkpoint else None))
            receipt = dict(type=TYPE,verification_kind='mounted_filesystem',
                contract_sha256=c.digest(contract),sha256=contract['sha256'],size=contract['size'],
                verified_at=datetime.now(timezone.utc).isoformat(),mount=before,
                file_identity=fingerprint(os.fstat(nas_fd)),local_identity=fingerprint(os.fstat(local_fd)),
                local_update_receipt_sha256=contract['local_update_receipt_sha256'])
            control = dict(type=CONTROL_TYPE,package_sha256=audit_package_sha256,
                context_sha256=meta['context_sha256'],publisher_sha256=meta['publisher_sha256'],
                contract_sha256=c.digest(contract),receipt_sha256=c.digest(receipt))
            # Reconstruct the original proof after the long hashes; live ERP may
            # update ordinary business, but cannot change this approved update.
            require(verify_code()==approved_code)
            _,current = root._state(instance,499,499)
            require(local_update(current) == (proof,original_local)
                and _original_proof(current)[3] == proof_raw)
            checked_bound=dict(contract=contract,receipt=receipt)
            if checkpoint:
                with open_regular(original_local) as old_fd:
                    require(full_hash(old_fd,proof['backup_bytes'])==proof['backup_sha256'])
            check_path(checked_bound)
            for p,value in ((CONTRACT,contract),(RECEIPT,receipt),(CONTROL,control)):
                require(p.parent == BASE); root._new(base,p.name,c.canonical(value),mode=0o644)
            check_path(checked_bound)
    return load()


def check_path(bound):
    with open_regular(bound['contract']['path']) as named:
        check_fd(named,bound)


def check_fd(fd,bound):
    """No traversal of the private user's home from UID499; use root's FD."""
    require(type(fd) is int and fd >= 3)
    import fcntl
    require(fcntl.fcntl(fd,fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        and fingerprint(os.fstat(fd)) == bound['receipt']['file_identity']
        and mounts.fd_identity(fd) == bound['receipt']['mount']['kernel'])


@contextmanager
def broker(plan):
    """Root-only fixed-path FD broker; the approved earlier hash stays truthful."""
    root._entry(); bound = load()
    require(bound['control']['package_sha256'] == plan['transport_package_sha256']
        and bound['control_sha'] == plan['transport_control_sha256']
        and bound['control']['contract_sha256'] == plan['transport_contract_sha256']
        and bound['control']['receipt_sha256'] == plan['backup_receipt_sha256'])
    require(observe() == bound['receipt']['mount'])
    with open_regular(bound['contract']['path']) as fd:
        check_fd(fd,bound);check_path(bound)
        yield fd
        check_fd(fd,bound);check_path(bound)
        require(load()['raw'] == bound['raw'] and observe() == bound['receipt']['mount'])


def checkpoint_result_hash(attempt):
    c._id(attempt)
    return hashlib.sha256(_protected(BASE/('current-mac-cutover-'+attempt)/'checkpoint-result.json')).hexdigest()


def checkpoint_result(attempt):
    """Authenticate the protected completed worker reply, never a loose boolean."""
    from desktop_assistant import current_mac_worker_contract as worker
    c._id(attempt); slot = BASE/('current-mac-cutover-'+attempt)
    plan_raw = _protected(slot/'checkpoint-plan.json')
    reply_raw = _protected(slot/'checkpoint-result.json')
    completed = _protected(slot/'checkpoint-completed.json')
    plan = worker.validate_plan(c.decode(plan_raw)); reply = c.decode(reply_raw)
    require(plan['type'] == worker.MOUNTED_PLAN and plan['phase'] == 'checkpoint'
        and plan['attempt_id'] == attempt and plan_raw == c.canonical(plan)
        and reply_raw == c.canonical(reply))
    request = dict(type=worker.REQUEST,**{k:reply[k] for k in
        ('attempt_id','phase','plan_sha256','challenge')},lock_fd=None)
    worker.validate_reply(reply,request,plan,hashlib.sha256(plan_raw).hexdigest())
    require(completed == c.canonical(dict(type='tianming.current-mac-stage-result.v1',
        phase='checkpoint',plan_sha256=hashlib.sha256(plan_raw).hexdigest(),
        reply_sha256=hashlib.sha256(reply_raw).hexdigest())))
    return reply['result']['checkpoint']


def validate_checkpoint(value):
    require(isinstance(value,dict) and set(value) == {'type','local_path','proof_path','sha256',
        'size','raw_sha256','created_at','database','shared_manifest_sha256','release_sha256',
        'state_sha256','local_update_receipt_sha256'}
        and value['type'] == 'tianming.current-mac-local-checkpoint.v1')
    for key in ('sha256','raw_sha256','shared_manifest_sha256','release_sha256','state_sha256',
                'local_update_receipt_sha256'): c._hash(value[key])
    require(type(value['size']) is int and value['size'] > 0 and isinstance(value['database'],dict))
    c.normalize_timestamp(value['created_at'])
    local = Path(value['local_path']); proof = Path(value['proof_path'])
    require(local.parent == ROOT/'backups' and proof.name == 'verified.json'
        and proof.parent.parent == ROOT/'staging'
        and re.fullmatch('mounted-checkpoint-[0-9a-f]{32}',proof.parent.name))
    return value


def build_checkpoint(manager,password,state_sha256,approval_check):
    """Already stopped only; original backup builder, no pointer/state changes."""
    from desktop_assistant import migration, factory_cutover as facts
    from desktop_assistant.storage import (sha, decrypt_file, extract_verified,
        database_info, signed_release_manifest)
    import uuid
    require(manager.root == ROOT)
    state = manager.state; original_raw = (ROOT/'state.json').read_bytes()
    proof,_,_,proof_raw = _original_proof(state)
    require(sha(ROOT/'state.json') == state_sha256 and state.get('manual_stop') is True
        and manager._process() is None and not state.get('onboarding_pending')
        and not state.get('backup_work_retained') and state.get('schema_authority') is None)
    from desktop_assistant.factory_target_audit import _no_journal
    _no_journal(ROOT/'shared')
    def check():
        approval_check(); require(manager._process() is None and manager.state == state
            and (ROOT/'state.json').read_bytes() == original_raw)
        _no_journal(ROOT/'shared')
    check(); inputs = manager._backup_inputs(); before = migration.files(ROOT/'shared')
    job = ROOT/'staging'/('mounted-checkpoint-'+uuid.uuid4().hex)
    old_mask = os.umask(0o077)
    try:
        job.mkdir(mode=0o700)
        payload = manager._build_backup_payload(password,job,inputs)
        final = ROOT/'backups'/payload['encrypted'].name
        with payload['encrypted'].open('rb') as src, final.open('xb') as dst:
            import shutil
            shutil.copyfileobj(src,dst,1024*1024); dst.flush(); os.fsync(dst.fileno())
        require(sha(final) == payload['sha256'] and final.stat().st_size == payload['size'])
        raw = job/'independent-readback.zip'
        require(decrypt_file(final,raw,password) == payload['sha256'] and sha(raw) == payload['raw_sha256'])
        recovered = job/'independent-recovery'; metadata = extract_verified(raw,recovered)
        require(metadata == payload['manifest'] and migration.files(recovered/'shared') == before
            and migration.files(ROOT/'shared') == before
            and database_info(recovered/'shared/data/carton_erp.sqlite3') == inputs['before']
            and signed_release_manifest(recovered/'release.zip',manager.public_key) == manager.manifest(state['current']))
        check()
        result = dict(type='tianming.current-mac-local-checkpoint.v1',local_path=str(final),
            proof_path=str(job/'verified.json'),sha256=payload['sha256'],size=payload['size'],
            raw_sha256=payload['raw_sha256'],created_at=metadata['created'],database=payload['database'],
            shared_manifest_sha256=facts.digest(before),release_sha256=state['current'],
            state_sha256=state_sha256,local_update_receipt_sha256=hashlib.sha256(proof_raw).hexdigest())
        validate_checkpoint(result)
        with (job/'verified.json').open('xb') as out:
            out.write(c.canonical(result)); out.flush(); os.fsync(out.fileno())
        check(); return result
    finally:
        os.umask(old_mask)
