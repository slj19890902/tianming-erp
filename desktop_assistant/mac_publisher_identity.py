"""Explicit owner-authorized Mac publisher, with private key in System Keychain.

Root callers must verify the complete protected admin pack before importing.
No build creates/rotates this identity and no service UID receives its key.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE = Path('/Library/TianmingERP')
PUBLIC = BASE/'mac-publisher-public.pem'
COMPLETION = BASE/'mac-publisher-initialized.json'
PENDING = BASE/'mac-publisher-initialization.pending.json'
HELPER = Path(__file__).with_name('mac_publisher_keychain_entry.py')


def require(ok):
    if not ok:
        raise ValueError('Mac独立发布身份未确认，保留现场')


def _root():
    require(sys.platform == 'darwin' and os.getuid() == os.geteuid() == 0
            and sys.flags.isolated and sys.dont_write_bytecode)


def _read(path):
    from desktop_assistant.mac_publisher_trust import protected_read
    return protected_read(path)


def _write(path, raw):
    require(not os.path.lexists(path) and path.parent == BASE)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(raw); stream.flush(); os.fsync(fd)
    finally:
        os.close(fd)
    parent = os.open(BASE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(parent)
    finally: os.close(parent)
    require(_read(path) == raw)


def _bridge(action, context_sha, secret=None):
    _root()
    require(action in {'create', 'read', 'denial-probe'})
    for path in (HELPER, Path(__file__)):
        require(path.resolve() == path.absolute() and path.lstat().st_uid == 0
                and stat.S_IMODE(path.lstat().st_mode) == 0o644)
    argv = ['/usr/bin/python3', '-I', '-B', str(HELPER), action, context_sha]
    read_fd = write_fd = None
    try:
        if action != 'create':
            read_fd, write_fd = os.pipe()
            argv.append(str(write_fd))
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True, pass_fds=(() if write_fd is None else (write_fd,)),
            cwd='/', umask=0o077,
            env={'HOME':'/var/root','PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8'})
        if write_fd is not None: os.close(write_fd); write_fd = None
        output, _ = process.communicate(secret)
        if action == 'denial-probe':
            result = json.loads(output)
            require(process.returncode == 0 and os.read(read_fd, 33) == b''
                    and set(result) == {'publishing_key_denied','uid','gid','os_status'}
                    and result['publishing_key_denied'] is True and result['uid'] == result['gid'] == 499
                    and result['os_status'] in {-25293, -25308})
            return True
        require(process.returncode == 0 and json.loads(output) == {'keychain_operation_confirmed':True})
        if action == 'create': return None
        raw = os.read(read_fd, 33)
        require(len(raw) == 32)
        return raw
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None: os.close(fd)


def initialize(*, owner_authorized=False):
    _root(); require(owner_authorized is True)
    from desktop_assistant import mac_publisher_trust as trust
    context = _read(BASE/'keychain-context.json')
    legacy = _read(BASE/'publisher-public.pem')
    context_sha, legacy_sha = map(lambda raw: hashlib.sha256(raw).hexdigest(), (context, legacy))
    require(not any(os.path.lexists(path) for path in (PUBLIC, COMPLETION, PENDING, trust.RECORD)))
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    fingerprint = hashlib.sha256(public).hexdigest()
    record = dict(type='tianming.mac-publisher-initialization.v1', context_sha256=context_sha,
        legacy_anchor_sha256=legacy_sha, mac_publisher_sha256=fingerprint,
        private_key_storage='root-only System Keychain', owner_authorized=True,
        operational_credentials_changed=False)
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    _write(PENDING, canonical(record))
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    _bridge('create', context_sha, raw); del raw
    fetched = _bridge('read', context_sha)
    restored = Ed25519PrivateKey.from_private_bytes(fetched); del fetched
    require(restored.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo) == public)
    _bridge('denial-probe', context_sha)
    _write(PUBLIC, public)
    authority = dict(type=trust.TYPE, context_sha256=context_sha, legacy_anchor_sha256=legacy_sha,
                    mac_publisher_sha256=fingerprint, packages={})
    trust.validate_record(authority, context_sha256=context_sha,
        legacy_anchor_sha256=legacy_sha, mac_publisher_sha256=fingerprint)
    _write(trust.RECORD, canonical(authority))
    record.update(new_process_keychain_verified=True, service_uid_private_key_denied=True)
    _write(COMPLETION, canonical(record))
    return record


def sign(raw):
    _root(); require(isinstance(raw, bytes) and 0 < len(raw) <= 16*1024*1024)
    from desktop_assistant import mac_publisher_trust as trust
    authority = trust.read_authority()
    record = json.loads(_read(COMPLETION))
    require(record.get('context_sha256') == authority['record']['context_sha256']
            and record.get('mac_publisher_sha256') == authority['record']['mac_publisher_sha256']
            and record.get('service_uid_private_key_denied') is True)
    private = _bridge('read', record['context_sha256'])
    key = Ed25519PrivateKey.from_private_bytes(private); del private
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    require(public == authority['mac_public'])
    signature = key.sign(raw); key.public_key().verify(signature, raw)
    return signature
