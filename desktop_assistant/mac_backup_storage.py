"""Bind Mac backup success to approved direct NFS and independent byte readback.

Historical local sidecars describe the verification at backup time. They do not
prove current availability, NAS disk power-loss durability or headless startup.
Original encrypted backups remain restorable without these new sidecars.
"""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desktop_assistant import mac_mount as mounts
from desktop_assistant import mac_archive_contract as autofs

BASE = Path('/Library/TianmingERP')
MOUNT = BASE/'archive'
DESTINATION = MOUNT/'backups'
CONTRACT = BASE/'archive-contract.json'
CONTRACT_TYPE = 'tianming.mac-archive-contract.v1'
PROOF_TYPE = 'tianming.mac-nfs-readback.v1'
SIDECAR_TYPE = 'tianming.mac-backup-evidence.v1'
CONTRACT_FIELDS = {'type', 'context_sha256', 'mountpoint', 'destination', 'source',
                   'server_address', 'export', 'version', 'port', 'security'}
PROOF_FIELDS = {'type', 'context_sha256', 'contract_sha256', 'mount', 'path', 'sha256',
                'size', 'verified_at', 'file_identity'}
RECEIPT_FIELDS = {'path', 'sha256', 'size', 'verified', 'storage', 'database'}
UNCONFIRMED = 'Mac备份远端保存尚未确认；请先完成专用身份及直连NAS合同验证，原备份保留'


def required(): return sys.platform == 'darwin'
def digest(raw): return hashlib.sha256(raw).hexdigest()
def canonical(value): return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
def _sha(value): return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _contract():
    from app.core import mac_service_keychain as keychain
    from desktop_assistant import mac_release_publish as publication
    context = keychain.load_context()  # Real dedicated identity, public metadata only.
    raw = publication.read_record(CONTRACT)
    value = mounts.decode(raw)
    context_sha = digest(publication.read_record(keychain.CONTEXT))
    if isinstance(value, dict) and value.get('type') == autofs.TYPE:
        autofs.validate(value, context_sha)
        autofs.installed(value, _root_configuration)
        return value, digest(raw), context
    if (not isinstance(value, dict) or set(value) != CONTRACT_FIELDS or value['type'] != CONTRACT_TYPE
            or value['context_sha256'] != context_sha or value['mountpoint'] != str(MOUNT)
            or value['destination'] != str(DESTINATION) or not _sha(context_sha)
            or not isinstance(value['source'], str) or not value['source']
            or not isinstance(value['server_address'], str)
            or not isinstance(value['export'], str) or not value['export'].startswith('/')
            or value['version'] != '4.0' or type(value['port']) is not int or value['port'] != 2049
            or value['security'] != 'sys'):
        raise ValueError(UNCONFIRMED)
    address = mounts.ip(value['server_address'])
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
        raise ValueError(UNCONFIRMED)
    if str(address) != value['server_address']: raise ValueError(UNCONFIRMED)
    return value, digest(raw), context


def _matches(value, observation):
    if value.get('type') == autofs.TYPE: return autofs.matches(value, observation)
    if not isinstance(observation, dict) or observation.get('classification') != 'direct_network_unbound':
        return False
    if set(observation) != {'classification', 'kernel', 'remote'}: return False
    kernel, remote = observation.get('kernel', {}), observation.get('remote', {})
    if (not isinstance(kernel, dict) or set(kernel) != {'fsid', 'filesystem', 'mountpoint', 'source', 'owner', 'flags', 'subtype'}
            or not isinstance(remote, dict) or set(remote) != {'addresses', 'export', 'server', 'version', 'port', 'security', 'tcp'}
            or any(type(kernel[k]) is not int or kernel[k] < 0 for k in ('owner', 'flags', 'subtype'))
            or kernel['flags'] & 0x1000 or not isinstance(remote['server'], str) or not remote['server']
            or type(remote['port']) is not int): return False
    return (kernel.get('mountpoint') == value['mountpoint'] and kernel.get('filesystem') == 'nfs'
        and kernel.get('source') == value['source'] and type(kernel.get('owner')) is int and kernel['owner'] == 0
        and isinstance(kernel.get('fsid'), list) and len(kernel['fsid']) == 2
        and all(type(i) is int for i in kernel['fsid'])
        and remote.get('addresses') == [value['server_address']] and remote.get('export') == value['export']
        and remote.get('version') == value['version'] and remote.get('port') == value['port']
        and remote.get('security') == value['security'] and remote.get('tcp') is True)


def before_write(path):
    """Check before probes/stopping, and again inside the backup primitive."""
    if not required(): return None
    try:
        value, contract_sha, context = _contract()
        if value.get('type') == autofs.TYPE:
            if Path(path) != autofs.DESTINATION: raise ValueError(UNCONFIRMED)
            return autofs_location(value, contract_sha, context)
        if Path(path) != DESTINATION or not Path(path).is_absolute(): raise ValueError(UNCONFIRMED)
        observation = mounts.observe(path)
        if not _matches(value, observation) or value['server_address'] in mounts.local_addresses():
            raise ValueError(UNCONFIRMED)
        # Open only after OS metadata rules out a local directory or unknown proxy.
        mountfd = os.open(MOUNT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open('backups', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=mountfd)
            try:
                info = os.fstat(fd)
                if (mounts.fd_identity(mountfd) != observation['kernel']
                        or mounts.fd_identity(fd) != observation['kernel']
                        or info.st_uid != context['uid'] or stat.S_IMODE(info.st_mode) != 0o700):
                    raise ValueError(UNCONFIRMED)
            finally: os.close(fd)
        finally: os.close(mountfd)
        if mounts.observe(path) != observation: raise ValueError(UNCONFIRMED)
        return {'contract_sha256': contract_sha, 'context_sha256': value['context_sha256'], 'mount': observation}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        raise ValueError(UNCONFIRMED) from None


def _root_configuration(path):
    return autofs.read_configuration(path)


def autofs_location(value, contract_sha, context):
    """Explicit approved trigger; never treat autofs or a local folder as NAS.

    Used by normal backup after _contract and by the separately approved setup
    probe before publishing that contract. This may wait on hard NFS.
    """
    from app.core import mac_service_keychain as keychain
    autofs.validate(value, value['context_sha256'])
    autofs.installed(value, _root_configuration)
    if not _sha(contract_sha) or value['server_address'] in mounts.local_addresses(): raise ValueError(UNCONFIRMED)
    def observe():
        return mounts.observe(autofs.DESTINATION, semantics=True,
            automount={'mountpoint': str(autofs.MOUNT), 'source': 'map '+str(autofs.MAP)})
    before = observe()
    triggering = before.get('classification') == 'automount_trigger_unbound' and autofs.trigger(before.get('kernel'))
    if not triggering and not _matches(value, before): raise ValueError(UNCONFIRMED)
    with keychain._checked_path(BASE, uid=0, directory=True, mode=0o755) as base:
        local = os.fstat(base); physical = autofs.MOUNT.parent.stat(follow_symlinks=False)
        if (local.st_dev != Path('/System/Volumes/Data').stat().st_dev
                or (local.st_dev, local.st_ino) != (physical.st_dev, physical.st_ino)):
            raise ValueError(UNCONFIRMED)
        # OP_OPEN intentionally triggers this exact root-configured autofs key.
        fd = os.open('archive', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
        try:
            after = observe()
            if (not _matches(value, after) or (not triggering and after != before)
                    or (triggering and after.get('autofs') != before['kernel'])
                    or mounts.fd_identity(fd) != after['kernel']): raise ValueError(UNCONFIRMED)
            child = os.open('backups', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                info = os.fstat(child)
                if (mounts.fd_identity(child) != after['kernel'] or info.st_uid != context['uid']
                        or info.st_gid != context['gid'] or stat.S_IMODE(info.st_mode) != 0o700):
                    raise ValueError(UNCONFIRMED)
                keychain._no_acl(child)
            finally: os.close(child)
            if observe() != after: raise ValueError(UNCONFIRMED)
            autofs.installed(value, _root_configuration)
        finally: os.close(fd)
    return {'contract_sha256': contract_sha, 'context_sha256': value['context_sha256'], 'mount': after}


def _final_path(path):
    value = Path(path)
    if (not value.is_absolute() or value.parent not in (DESTINATION, autofs.DESTINATION) or value.suffix != '.tmbackup'
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}\.tmbackup', value.name)):
        raise ValueError(UNCONFIRMED)
    return value


def _fingerprint(info):
    return {k: getattr(info, 'st_'+k) for k in ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')}


def readback(request):
    """Fresh dedicated-UID process: exact contract + final FD + full byte SHA."""
    import fcntl
    if (not required() or not isinstance(request, dict)
            or set(request) != {'challenge', 'anchor', 'path', 'sha256', 'size'}
            or not isinstance(request['challenge'], str) or not re.fullmatch('[0-9a-f]{32}', request['challenge'])
            or not _sha(request['sha256']) or type(request['size']) is not int or request['size'] <= 0):
        raise ValueError(UNCONFIRMED)
    path = _final_path(request['path'])
    anchor = before_write(path.parent)
    if anchor != request['anchor']: raise ValueError(UNCONFIRMED)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != request['size']
                or mounts.fd_identity(fd) != anchor['mount']['kernel']):
            raise ValueError(UNCONFIRMED)
        fcntl.fcntl(fd, 48, 1)  # macOS F_NOCACHE applies to this independent open.
        value = hashlib.sha256()
        while block := os.read(fd, 1024 * 1024): value.update(block)
        after = os.fstat(fd)
        if (value.hexdigest() != request['sha256'] or _fingerprint(before) != _fingerprint(after)
                or mounts.fd_identity(fd) != anchor['mount']['kernel']
                or _fingerprint(path.stat(follow_symlinks=False)) != _fingerprint(after)):
            raise ValueError(UNCONFIRMED)
    finally: os.close(fd)
    if before_write(path.parent) != anchor: raise ValueError(UNCONFIRMED)
    proof = {'type': PROOF_TYPE, **anchor, 'path': str(path), 'sha256': value.hexdigest(),
             'size': after.st_size, 'verified_at': datetime.now(timezone.utc).isoformat(),
             'file_identity': _fingerprint(after)}
    return {'challenge': request['challenge'], 'proof': proof}


def _child(request):
    process = subprocess.Popen([sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--readback'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
        env={'HOME': str(BASE/'home'), 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'})
    interrupted = False
    payload = canonical(request)
    while True:
        try:
            output, _ = process.communicate(payload)
            break
        except KeyboardInterrupt:
            interrupted = True; payload = None
    # Hard NFS cannot promise a universal kill/timeout bound. Observe the exact
    # worker until it exits; interruption still refuses completion afterward.
    if interrupted or process.returncode or not 0 < len(output) <= 65536: raise ValueError(UNCONFIRMED)
    return mounts.decode(output)


def _validate_proof(proof, anchor, *, expected=None, earliest=None, now=None):
    """One strict schema for child output, publication and historical display."""
    if (not isinstance(anchor, dict) or set(anchor) != {'context_sha256', 'contract_sha256', 'mount'}
            or any(not _sha(anchor[k]) for k in ('context_sha256', 'contract_sha256'))
            or not isinstance(proof, dict) or set(proof) != PROOF_FIELDS or proof['type'] != PROOF_TYPE
            or any(proof[k] != v for k, v in anchor.items())
            or not isinstance(proof['path'], str) or str(_final_path(proof['path'])) != proof['path']
            or not _sha(proof['sha256']) or type(proof['size']) is not int or proof['size'] <= 0
            or not isinstance(proof['verified_at'], str)):
        raise ValueError(UNCONFIRMED)
    if expected is not None and any(proof[k] != expected[k] for k in ('path', 'sha256', 'size')):
        raise ValueError(UNCONFIRMED)
    when = datetime.fromisoformat(proof['verified_at'])
    if (when.utcoffset() is None or when > (now or datetime.now(timezone.utc))+timedelta(minutes=5)
            or (earliest is not None and when < earliest-timedelta(minutes=5))):
        raise ValueError(UNCONFIRMED)
    identity = proof['file_identity']
    if (not isinstance(identity, dict) or set(identity) != {'dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'}
            or any(type(v) is not int or v < 0 for v in identity.values())
            or identity['size'] != proof['size']):
        raise ValueError(UNCONFIRMED)
    return when


def after_write(anchor, path, expected_sha, expected_size):
    if not required(): return None
    path = _final_path(path)
    if not _sha(expected_sha) or type(expected_size) is not int or expected_size <= 0:
        raise ValueError(UNCONFIRMED)
    started = datetime.now(timezone.utc)
    request = {'challenge': uuid.uuid4().hex, 'anchor': anchor, 'path': str(path),
               'sha256': expected_sha, 'size': expected_size}
    result = _child(request)
    if (not isinstance(result, dict) or set(result) != {'challenge', 'proof'}
            or result['challenge'] != request['challenge']): raise ValueError(UNCONFIRMED)
    if before_write(path.parent) != anchor: raise ValueError(UNCONFIRMED)
    proof = result['proof']
    _validate_proof(proof, anchor, expected=request, earliest=started)
    return proof


def _check_fd(fd, *, uid, directory, device=None):
    from app.core.mac_service_keychain import _no_acl
    info = os.fstat(fd)
    if (not (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode) or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1)
            or (device is not None and info.st_dev != device)):
        raise ValueError(UNCONFIRMED)
    _no_acl(fd)
    return info


@contextmanager
def _record_directory(parent, name, uid, device, *, create=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError: pass  # Never repair someone else's existing permissions.
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        before = _check_fd(fd, uid=uid, directory=True, device=device)
        yield fd
        after = _check_fd(fd, uid=uid, directory=True, device=device)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if ((before.st_dev, before.st_ino, before.st_gid) != (after.st_dev, after.st_ino, after.st_gid)
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError(UNCONFIRMED)
    finally: os.close(fd)


@contextmanager
def _records(uid, *, create=False):
    from app.core.mac_service_keychain import _checked_path
    # load_context has authenticated the fixed root and its local disk/ancestors.
    # Hold each directory FD; subsequent child opens never follow a parent link.
    with ExitStack() as stack:
        fd = stack.enter_context(_checked_path(BASE, uid=0, directory=True, mode=0o755))
        device = os.fstat(fd).st_dev
        if device != Path('/System/Volumes/Data').stat().st_dev: raise ValueError(UNCONFIRMED)
        for name in ('instance', 'control', 'backup-receipts'):
            fd = stack.enter_context(_record_directory(fd, name, uid, device,
                                                       create=create and name == 'backup-receipts'))
        yield fd


def _local_object(directory, name, uid):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = _check_fd(fd, uid=uid, directory=False, device=os.fstat(directory).st_dev)
        if not 0 < info.st_size <= 1024*1024: raise ValueError(UNCONFIRMED)
        raw = bytearray()
        while block := os.read(fd, 1024*1024+1-len(raw)):
            raw.extend(block)
            if len(raw) > info.st_size: raise ValueError(UNCONFIRMED)
        after = _check_fd(fd, uid=uid, directory=False, device=info.st_dev)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (len(raw) != info.st_size or _fingerprint(info) != _fingerprint(after)
                or info.st_gid != after.st_gid or _fingerprint(current) != _fingerprint(after)):
            raise ValueError(UNCONFIRMED)
        return bytes(raw), mounts.decode(raw)
    finally: os.close(fd)


def _exclusive(directory, name, raw, uid):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(fd, 'wb') as stream:
        _check_fd(stream.fileno(), uid=uid, directory=False, device=os.fstat(directory).st_dev)
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        _check_fd(stream.fileno(), uid=uid, directory=False)
    if _local_object(directory, name, uid)[0] != raw:
        raise ValueError('备份本地证据回读失败，保留现场')


def publish_receipt(path, receipt, proof):
    from desktop_assistant.storage import write_json
    if not required():
        write_json(path, receipt)
        return None
    value, contract_sha, context = _contract()
    if not isinstance(proof, dict) or not _matches(value, proof.get('mount')): raise ValueError(UNCONFIRMED)
    anchor = {'contract_sha256': contract_sha, 'context_sha256': value['context_sha256'], 'mount': proof['mount']}
    _validate_proof(proof, anchor)
    path = Path(path)
    if (path != BASE/'instance/control/backup-receipts'/(Path(proof['path']).stem+'.json')
            or not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS
            or receipt['verified'] is not True or receipt['storage'] != 'nas'
            or type(receipt['size']) is not int or not isinstance(receipt['database'], dict)
            or any(receipt[k] != proof[k] for k in ('path', 'sha256', 'size'))): raise ValueError(UNCONFIRMED)
    raw = canonical(receipt)
    with _records(context['uid'], create=True) as directory:
        _exclusive(directory, path.name, raw, context['uid'])
        sidecar = {'type': SIDECAR_TYPE, 'receipt_sha256': digest(raw), 'readback': proof}
        _exclusive(directory, path.stem+'.mac-storage.json', canonical(sidecar), context['uid'])
        os.fsync(directory)
    return proof['verified_at']


def verified_at(root, state, *, now=None):
    """Small local records only. No remote path stat, mount query or full hash."""
    if not required(): return state.get('last_backup_at')
    try:
        if Path(root).absolute() != BASE/'instance': return None
        value, contract_sha, context = _contract()
        path = _final_path(state['last_backup'])
        local = Path(root)/'control/backup-receipts'/(path.stem+'.json')
        with _records(context['uid']) as directory:
            raw, receipt = _local_object(directory, local.name, context['uid'])
            _, sidecar = _local_object(directory, path.stem+'.mac-storage.json', context['uid'])
        if (not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS
                or receipt['verified'] is not True or receipt['storage'] != 'nas'
                or receipt['path'] != str(path) or not _sha(receipt['sha256'])
                or type(receipt['size']) is not int or receipt['size'] <= 0
                or not isinstance(receipt['database'], dict)
                or not isinstance(sidecar, dict) or set(sidecar) != {'type', 'receipt_sha256', 'readback'}
                or sidecar['type'] != SIDECAR_TYPE or sidecar['receipt_sha256'] != digest(raw)):
            return None
        proof = sidecar['readback']
        if not isinstance(proof, dict) or not _matches(value, proof.get('mount')): return None
        anchor = {'contract_sha256': contract_sha, 'context_sha256': value['context_sha256'], 'mount': proof['mount']}
        _validate_proof(proof, anchor, expected=receipt, now=now)
        if proof['verified_at'] != state['last_backup_at']: return None
        return proof['verified_at']
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        return None


if __name__ == '__main__':
    try:
        if sys.argv[1:] != ['--readback']: raise ValueError(UNCONFIRMED)
        raw = sys.stdin.buffer.read(65537)
        if not 0 < len(raw) <= 65536: raise ValueError(UNCONFIRMED)
        print(json.dumps(readback(mounts.decode(raw))))
    except (Exception, KeyboardInterrupt):
        print(UNCONFIRMED, file=sys.stderr)
        raise SystemExit(1)
