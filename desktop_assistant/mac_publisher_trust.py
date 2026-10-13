"""Additive root authorization for exact Mac-signed release archives.

The historical anchor and v1 registry keep their original meanings. A Mac
signature is accepted only for an exact archive/manifest pair authorized in this
fixed root sidecar. No key from an archive, environment, caller-selected trust
path or unprotected record is admitted. Private keys never enter this module.
Public record helpers are stdlib-only for the independently approved root pack.
"""
from contextlib import ExitStack
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

BASE = Path('/Library/TianmingERP')
ROOT = BASE/'instance'
CONTEXT = BASE/'keychain-context.json'
LEGACY_PUBLIC = BASE/'publisher-public.pem'
MAC_PUBLIC = BASE/'mac-publisher-public.pem'
RECORD = BASE/'mac-publisher-trust.json'
TYPE = 'tianming.mac-publisher-trust.v1'
MODULE = 'desktop_assistant/mac_publisher_trust.py'
LIMIT = 65536
MAX_PACKAGES = 256
ERROR = 'Mac publisher authorization unconfirmed; historical anchor unchanged'
FIELDS = {'type','context_sha256','legacy_anchor_sha256','mac_publisher_sha256','packages'}


def require(ok):
    if not ok: raise ValueError(ERROR)


def digest(raw):
    require(type(raw) is bytes)
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    try:
        raw = json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
        require(0 < len(raw) <= LIMIT)
        return raw
    except (TypeError,ValueError,UnicodeError,RecursionError):
        raise ValueError(ERROR) from None


def decode(raw):
    require(type(raw) is bytes and 0 < len(raw) <= LIMIT)
    def unique(items):
        value = {}
        for key,item in items:
            require(key not in value);value[key] = item
        return value
    def invalid(_):raise ValueError(ERROR)
    try:
        value = json.loads(raw.decode('utf-8'),object_pairs_hook=unique,
                           parse_constant=invalid,parse_float=invalid)
        require(type(value) is dict);return value
    except (TypeError,ValueError,UnicodeError,RecursionError):
        raise ValueError(ERROR) from None


def _sha(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None)


def public_fingerprint(raw):
    """Strict canonical Ed25519 SPKI PEM validation without a crypto import."""
    require(type(raw) is bytes and len(raw) <= 1024)
    prefix = b'-----BEGIN PUBLIC KEY-----\n';suffix = b'\n-----END PUBLIC KEY-----\n'
    require(raw.startswith(prefix) and raw.endswith(suffix))
    encoded = raw[len(prefix):-len(suffix)]
    try:der = base64.b64decode(encoded,validate=True)
    except (ValueError,TypeError):raise ValueError(ERROR) from None
    # RFC 8410 Ed25519 SubjectPublicKeyInfo: parameterless OID + 32-byte key.
    require(len(der) == 44 and der[:12] == bytes.fromhex('302a300506032b6570032100')
        and base64.b64encode(der) == encoded)
    return digest(raw)


def validate_record(value, *, context_sha256, legacy_anchor_sha256, mac_publisher_sha256):
    """Pure claims/bindings only; root file authentication belongs to the reader."""
    for expected in (context_sha256,legacy_anchor_sha256,mac_publisher_sha256):_sha(expected)
    require(type(value) is dict and set(value) == FIELDS and value['type'] == TYPE
        and value['context_sha256'] == context_sha256
        and value['legacy_anchor_sha256'] == legacy_anchor_sha256
        and value['mac_publisher_sha256'] == mac_publisher_sha256
        and mac_publisher_sha256 != legacy_anchor_sha256)
    packages = value['packages']
    require(type(packages) is dict and len(packages) <= MAX_PACKAGES)
    for identity,entry in packages.items():
        _sha(identity)
        require(type(entry) is dict and set(entry) == {'manifest_sha256','signer_sha256'})
        _sha(entry['manifest_sha256']);_sha(entry['signer_sha256'])
        require(entry['signer_sha256'] == mac_publisher_sha256)
    canonical(value)
    return value


def authorize_package(value, package_sha256, manifest_sha256):
    """Construct a new record, never write/overwrite or self-authorize a package."""
    require(type(value) is dict)
    validate_record(value,context_sha256=value.get('context_sha256'),
        legacy_anchor_sha256=value.get('legacy_anchor_sha256'),
        mac_publisher_sha256=value.get('mac_publisher_sha256'))
    _sha(package_sha256);_sha(manifest_sha256)
    require(package_sha256 not in value['packages'] and len(value['packages']) < MAX_PACKAGES)
    result = {**value,'packages':{**value['packages'],package_sha256:{
        'manifest_sha256':manifest_sha256,'signer_sha256':value['mac_publisher_sha256']}}}
    validate_record(result,context_sha256=value['context_sha256'],
        legacy_anchor_sha256=value['legacy_anchor_sha256'],mac_publisher_sha256=value['mac_publisher_sha256'])
    return result


def _fingerprint(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,
            info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _no_acl(fd):
    from desktop_assistant.mac_service_identity import _no_acl as check
    check(fd)


def _protected(info, *, directory=False, mode=0o644, uid=0, gid=None):
    require((stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode)
        and info.st_uid == uid and (gid is None or info.st_gid == gid)
        and (stat.S_IMODE(info.st_mode) == mode if not directory else not info.st_mode & 0o022)
        and (directory or info.st_nlink == 1))


def protected_read(path, *, mode=0o644):
    """Public root record reader. Root owns/writes; UID499 may read mode0644."""
    require(sys.platform == 'darwin')
    path = Path(path)
    require(path.is_absolute() and path.parent == BASE and os.path.normpath(str(path)) == str(path))
    require(mode in (0o600,0o644))
    with ExitStack() as stack:
        directory = os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);stack.callback(os.close,directory)
        _protected(os.fstat(directory),directory=True);_no_acl(directory)
        parents = []
        for name in path.parts[1:-1]:
            old = directory
            directory = os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=old)
            stack.callback(os.close,directory)
            _protected(os.fstat(directory),directory=True);_no_acl(directory)
            parents.append((old,name,directory,_fingerprint(os.fstat(directory))[:5]))
        fd = os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        stack.callback(os.close,fd);info = os.fstat(fd)
        _protected(info,mode=mode);_no_acl(fd)
        require(0 < info.st_size <= LIMIT)
        parts = [];remaining = info.st_size+1
        while remaining:
            chunk = os.read(fd,min(remaining,65536))
            if not chunk:break
            parts.append(chunk);remaining -= len(chunk)
        raw = b''.join(parts)
        require(len(raw) == info.st_size and _fingerprint(os.fstat(fd)) == _fingerprint(info)
            and _fingerprint(os.stat(path.name,dir_fd=directory,follow_symlinks=False)) == _fingerprint(info))
        for parent,name,held,before in parents:
            require(_fingerprint(os.fstat(held))[:5] == before
                and _fingerprint(os.stat(name,dir_fd=parent,follow_symlinks=False))[:5] == before)
            _no_acl(held)
        return raw


def _instance(context):
    require(type(context) is dict and set(context) == {'type','installation_id','uid','gid','host_sha256'}
        and context['type'] == 'tianming.service-keychain.v1'
        and type(context['uid']) is int and context['uid'] == 499
        and type(context['gid']) is int and context['gid'] == 499
        and type(context['installation_id']) is str and re.fullmatch('[0-9a-f]{32}',context['installation_id']))
    _sha(context['host_sha256'])
    base = os.open(BASE,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        _protected(os.fstat(base),directory=True);_no_acl(base)
        fd = os.open('instance',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=base)
        try:
            info = os.fstat(fd)
            require(stat.S_IMODE(info.st_mode) == 0o700 and info.st_dev == os.fstat(base).st_dev
                and _fingerprint(os.stat('instance',dir_fd=base,follow_symlinks=False)) == _fingerprint(info))
            _protected(info,directory=True,uid=499,gid=499);_no_acl(fd)
        finally:os.close(fd)
    finally:os.close(base)


def read_authority(*, expected_context_sha256=None, expected_legacy_anchor_sha256=None,
                   expected_mac_publisher_sha256=None):
    """Authenticate fixed public files; no private key, secret or authority write."""
    require(sys.platform == 'darwin')
    raw = {path:protected_read(path) for path in (CONTEXT,LEGACY_PUBLIC,MAC_PUBLIC,RECORD)}
    context = decode(raw[CONTEXT]);_instance(context)
    context_sha = digest(raw[CONTEXT]);legacy_sha = public_fingerprint(raw[LEGACY_PUBLIC])
    mac_sha = public_fingerprint(raw[MAC_PUBLIC]);record = decode(raw[RECORD])
    validate_record(record,context_sha256=context_sha,legacy_anchor_sha256=legacy_sha,mac_publisher_sha256=mac_sha)
    require(raw[RECORD] == canonical(record))
    for actual,expected in ((context_sha,expected_context_sha256),
            (legacy_sha,expected_legacy_anchor_sha256),(mac_sha,expected_mac_publisher_sha256)):
        if expected is not None:_sha(expected);require(actual == expected)
    # A second bounded read rejects mixed snapshots during a root transaction.
    require(all(protected_read(path) == value for path,value in raw.items()))
    return dict(record=record,raw=raw[RECORD],record_sha256=digest(raw[RECORD]),
        mac_public=raw[MAC_PUBLIC],legacy_public=raw[LEGACY_PUBLIC],context_raw=raw[CONTEXT])


def _managed():
    if sys.platform != 'darwin':return False
    if os.getuid() == os.geteuid() == 0:return True
    from app.core.mac_service_keychain import selected
    return selected()


def verify_manifest(archive, manifest_raw, signature, legacy_public):
    """Central Ed25519 verification; Mac fallback binds the same open archive.

    Returns public authority metadata plus recheck(), which consumers call after
    their full file checks. Legacy success never consults or depends on sidecar.
    Non-signature errors, missing signatures and malformed keys cannot fallback.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key = serialization.load_pem_public_key(legacy_public)
    require(isinstance(key,Ed25519PublicKey))
    try:
        key.verify(signature,manifest_raw)
        return dict(authority='provided-signature',signer_sha256=digest(legacy_public),recheck=lambda:None)
    except InvalidSignature:
        if not _managed():raise
    bound = read_authority(expected_legacy_anchor_sha256=digest(legacy_public))
    require(legacy_public == bound['legacy_public'])
    stream = archive.fp
    fd = stream.fileno();before = _fingerprint(os.fstat(fd))
    require(stat.S_ISREG(before[2]) and before[5] == 1)
    position = stream.tell();stream.seek(0);sha = hashlib.sha256()
    try:
        while chunk := stream.read(1024*1024):sha.update(chunk)
    finally:stream.seek(position)
    require(_fingerprint(os.fstat(fd)) == before)
    identity = sha.hexdigest();entry = bound['record']['packages'].get(identity)
    require(entry is not None and entry['manifest_sha256'] == digest(manifest_raw)
        and entry['signer_sha256'] == bound['record']['mac_publisher_sha256'])
    mac_key = serialization.load_pem_public_key(bound['mac_public'])
    require(isinstance(mac_key,Ed25519PublicKey));mac_key.verify(signature,manifest_raw)
    def recheck():
        require(_fingerprint(os.fstat(fd)) == before)
        if isinstance(getattr(stream,'name',None),(str,bytes,os.PathLike)):
            require(_fingerprint(os.stat(stream.name,follow_symlinks=False)) == before)
        current = read_authority(expected_context_sha256=bound['record']['context_sha256'],
            expected_legacy_anchor_sha256=bound['record']['legacy_anchor_sha256'],
            expected_mac_publisher_sha256=bound['record']['mac_publisher_sha256'])
        require(current['raw'] == bound['raw'])
    recheck()
    return dict(authority='mac-root-authorized',signer_sha256=entry['signer_sha256'],
                package_sha256=identity,manifest_sha256=entry['manifest_sha256'],recheck=recheck)
