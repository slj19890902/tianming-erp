"""Explicit file-keychain access for the dedicated macOS ERP service identity.

The System keychain contains only an installation-bound random unlock secret.
Ordinary credential rotation adds a new item to the service-owned private file.
Initialization is explicit; put/get never create a keychain or repair metadata.
No password is accepted from a file, environment variable or command argument.
"""
from contextlib import contextmanager
import ctypes as C
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading

from app.core import mac_keychain
from app.core.mac_keychain import KeychainError, MAX_SECRET_BYTES, SERVICE

ACCOUNT = '_tianmingerp'
BASE = Path('/Library/TianmingERP')
CONTEXT = BASE / 'keychain-context.json'
TYPE = 'tianming.service-keychain.v1'
BOOTSTRAP_SERVICE = 'cn.tianming.erp.keychain-bootstrap.v1'
CANARY_SERVICE = 'cn.tianming.erp.keychain-check.v1'
FIELDS = {'type', 'installation_id', 'uid', 'gid', 'host_sha256'}
_domain_pid = None
_domain_lock = threading.RLock()


def selected():
    if sys.platform != 'darwin':
        return False
    import pwd
    # A partially switched process must fail the service checks, not fall back.
    return any(pwd.getpwuid(uid).pw_name == ACCOUNT for uid in {os.getuid(), os.geteuid()})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def binding(value):
    return hashlib.sha256(b'TIANMING-KEYCHAIN-CONTEXT\0' + canonical(value)).hexdigest()


def keychain_path(value):
    return BASE / 'home/Library/Keychains' / ('tianming-' + value['installation_id'] + '.keychain-db')


def _creation_absent(path):
    if not path.is_absolute() or not path.name.endswith('.keychain-db') or 'login.keychain' in path.name:
        raise KeychainError('不是明确的专用钥匙串新建路径')
    legacy = path.with_name(path.name[:-3])  # remove '-db', retain '.keychain'
    if any(p.exists() or p.is_symlink() for p in (path, legacy)):
        raise KeychainError('服务钥匙串或旧格式现场已存在，拒绝覆盖或旁建')


def _no_acl(fd):
    from desktop_assistant.mac_service_identity import _no_acl as check
    check(fd)


def _identity(value, *, administrator=False):
    from desktop_assistant.mac_service_identity import _platform, _parent, _host, _check_identity, permitted_groups
    _platform()
    if (not isinstance(value, dict) or set(value) != FIELDS or value['type'] != TYPE
            or not isinstance(value['installation_id'], str)
            or not re.fullmatch('[0-9a-f]{32}', value['installation_id'])
            or any(type(value[k]) is not int or not 400 <= value[k] < 500 for k in ('uid', 'gid'))
            or value['host_sha256'] != _host()):
        raise KeychainError('系统服务钥匙串身份无效或不属于本机')
    _parent()
    with _checked_path(BASE, uid=0, directory=True, mode=0o755):
        pass
    _check_identity(value)
    expected_uid, expected_gid = (0, None) if administrator else (value['uid'], value['gid'])
    if (os.getuid() != expected_uid or os.geteuid() != expected_uid
            or (expected_gid is not None and (os.getgid() != expected_gid or os.getegid() != expected_gid
                or not set(os.getgroups()) <= permitted_groups(expected_gid)))):
        raise KeychainError('钥匙串操作不是已配置的实际服务身份')


@contextmanager
def _checked_path(path, *, uid, directory=False, mode=0o600):
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected_type(info.st_mode) or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) != mode or (not directory and info.st_nlink != 1)):
            raise KeychainError('服务钥匙串路径类型、所有者或权限不符')
        _no_acl(fd)
        yield fd
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise KeychainError('服务钥匙串路径在操作期间被替换，保留现场')
    finally:
        os.close(fd)


@contextmanager
def _system_path(path, *, writable):
    from contextlib import ExitStack
    if (not path.is_absolute() or path != path.absolute() or '..' in path.parts
            or path.stat().st_dev != Path('/System/Volumes/Data').stat().st_dev):
        raise KeychainError('系统钥匙串路径不是本机规范路径')
    if writable:
        if os.getuid() != 0 or os.geteuid() != 0:
            raise KeychainError('仅管理员初始化可写系统引导条目')
    elif os.getuid() == 0 or os.geteuid() == 0 or os.access(path, os.W_OK):
        raise KeychainError('服务身份不可拥有系统钥匙串写权限')
    with ExitStack() as stack:
        for parent in reversed(path.parents):
            stack.enter_context(_checked_path(parent, uid=0, directory=True, mode=0o755))
        stack.enter_context(_keychain_file(path, 0, mode=0o644))
        yield


@contextmanager
def _keychain_file(path, uid, *, mode=0o600):
    # Security.framework atomically replaces its database while adding items.
    # Pin the private parent directories, but validate the file's protection
    # before and after instead of assuming its inode must remain unchanged.
    with _checked_path(path, uid=uid, mode=mode):
        pass
    try:
        yield
    finally:
        with _checked_path(path, uid=uid, mode=mode):
            pass


@contextmanager
def _private_paths(value, *, existing=True):
    from contextlib import ExitStack
    path = keychain_path(value)
    with ExitStack() as stack:
        for directory in (BASE / 'home', BASE / 'home/Library', path.parent):
            stack.enter_context(_checked_path(directory, uid=value['uid'], directory=True, mode=0o700))
        if existing:
            stack.enter_context(_keychain_file(path, value['uid']))
        else:
            _creation_absent(path)
        yield path


def load_context():
    # Metadata is public, root-owned 0644; contains no secret or operational refs.
    # A missing completion record is a setup error, never a cue to create empty.
    from desktop_assistant.mac_service_identity import _parent
    _parent()
    with _checked_path(BASE, uid=0, directory=True, mode=0o755), \
            _checked_path(CONTEXT, uid=0, mode=0o644) as fd:
        raw = os.read(fd, 65537)
        if not 0 < len(raw) <= 65536:
            raise KeychainError('服务钥匙串完成记录长度无效')
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise KeychainError('服务钥匙串完成记录字段重复')
                result[key] = item
            return result
        value = json.loads(raw, object_pairs_hook=unique)
        _identity(value)
        return value


class Security:
    """Native explicit refs; no secret-related command-line tools or UI."""

    def __init__(self):
        self.api = mac_keychain.Keychain()
        p = C.c_void_p
        declarations = [
            ('SecKeychainSetUserInteractionAllowed', C.c_int32, [C.c_bool]),
            ('SecKeychainSetPreferenceDomain', C.c_int32, [C.c_int]),
            ('SecKeychainGetPreferenceDomain', C.c_int32, [C.POINTER(C.c_int)]),
            ('SecKeychainCopyDomainDefault', C.c_int32, [C.c_int, C.POINTER(p)]),
            ('SecKeychainOpen', C.c_int32, [C.c_char_p, C.POINTER(p)]),
            ('SecKeychainGetStatus', C.c_int32, [p, C.POINTER(C.c_uint32)]),
            ('SecKeychainUnlock', C.c_int32, [p, C.c_uint32, p, C.c_bool]),
            ('SecKeychainCreate', C.c_int32, [C.c_char_p, C.c_uint32, p, C.c_bool, p, C.POINTER(p)]),
            ('SecKeychainItemCopyKeychain', C.c_int32, [p, C.POINTER(p)]),
            ('SecAccessCreateWithOwnerAndACL', p, [C.c_uint32, C.c_uint32, C.c_uint32, p, C.POINTER(p)]),
        ]
        for name, result, args in declarations:
            function = getattr(self.api.sec, name)
            function.restype, function.argtypes = result, args

    def status(self, ref):
        status = C.c_uint32()
        self.api.check(self.api.sec.SecKeychainGetStatus(ref, C.byref(status)))
        return status.value

    def path(self, ref):
        size = C.c_uint32(4096)
        buffer = C.create_string_buffer(size.value)
        self.api.check(self.api.sec.SecKeychainGetPath(ref, C.byref(size), buffer))
        return Path(os.fsdecode(buffer.value))

    def service_domain(self):
        # Process-global, once per PID, before the service's first keychain use.
        # The caller must not have modified defaults/search lists beforehand.
        # Apple's switch can flush an already-dirty preference cache.
        global _domain_pid
        with _domain_lock:
            self.api.check(self.api.sec.SecKeychainSetUserInteractionAllowed(False))
            if _domain_pid != os.getpid():
                self.api.check(self.api.sec.SecKeychainSetPreferenceDomain(1))
                _domain_pid = os.getpid()
            domain = C.c_int()
            self.api.check(self.api.sec.SecKeychainGetPreferenceDomain(C.byref(domain)))
            if domain.value != 1:
                raise KeychainError('服务进程钥匙串域已改变，拒绝继续')

    @contextmanager
    def system(self, *, writable=False):
        self.service_domain()
        ref = C.c_void_p()
        self.api.check(self.api.sec.SecKeychainCopyDomainDefault(1, C.byref(ref)))
        with self.api.owned(ref.value):
            status = self.status(ref.value)
            if not status & 2:
                raise KeychainError('系统引导钥匙串不可读')
            default = C.c_void_p()
            self.api.check(self.api.sec.SecKeychainCopyDefault(C.byref(default)))
            with self.api.owned(default.value):
                if self.path(default.value) != self.path(ref.value):
                    raise KeychainError('服务进程默认域不是系统域，拒绝凭据操作')
            # GetStatus's WritePerm bit does not measure actual BSD write access.
            # Never open a caller-chosen System path or unlock/lock this keychain.
            with _system_path(self.path(ref.value), writable=writable):
                yield ref.value

    @contextmanager
    def opened(self, path):
        ref = C.c_void_p()
        self.api.check(self.api.sec.SecKeychainOpen(os.fsencode(path), C.byref(ref)))
        with self.api.owned(ref.value):
            if self.path(ref.value) != path or not self.status(ref.value) & 2:
                raise KeychainError('专用钥匙串不存在或不可读')
            yield ref.value

    def unlock(self, ref, secret):
        _unlock_secret(secret)
        self.api.check(self.api.sec.SecKeychainUnlock(ref, len(secret), secret, True))

    @contextmanager
    def created(self, path, secret):
        _unlock_secret(secret)
        _creation_absent(path)
        ref = C.c_void_p()
        self.api.check(self.api.sec.SecKeychainCreate(os.fsencode(path), len(secret), secret,
                                                     False, None, C.byref(ref)))
        with self.api.owned(ref.value):
            if self.path(ref.value) != path or self.status(ref.value) & 7 != 7:
                raise KeychainError('专用钥匙串创建回读未通过，保留现场')
            # Security.framework may create 0644 regardless of its private parent.
            # Tighten only the file this successful Create just created, before
            # adding any item; never repair an existing keychain's permissions.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise KeychainError('新建专用钥匙串归属不符，保留现场')
                _no_acl(fd)
                os.fchmod(fd, 0o600)
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                    raise KeychainError('新建专用钥匙串私有权限未生效')
            finally:
                os.close(fd)
            yield ref.value

    @contextmanager
    def uid_access(self, uid, gid):
        api = self.api
        # This API takes CFString authorization tags, not SecACLRef objects.
        # One UID process subject; no honor-root or trusted-application allow.
        refs = (C.c_void_p * 1)(api.constant('kSecACLAuthorizationDecrypt'))
        with api.owned(api.cf.CFArrayCreate(None, refs, 1, None)) as tags:
            error = C.c_void_p()
            ref = api.sec.SecAccessCreateWithOwnerAndACL(uid, gid, 1, tags, C.byref(error))
            if error.value:
                api.cf.CFRelease(error.value)
                if ref:
                    api.cf.CFRelease(ref)
                raise KeychainError('不能建立专用UID钥匙串权限')
            with api.owned(ref) as access:
                yield access

    @contextmanager
    def query(self, ref, service_name, account, *, adding=False):
        api = self.api
        with api.text(service_name) as service, api.text(account) as user:
            refs = (C.c_void_p * 1)(ref)
            with api.owned(api.cf.CFArrayCreate(None, refs, 1, None)) as search:
                yield {'kSecClass': api.constant('kSecClassGenericPassword'),
                       'kSecAttrService': service, 'kSecAttrAccount': user,
                       'kSecUseAuthenticationUI': api.constant('kSecUseAuthenticationUIFail'),
                       'kSecUseDataProtectionKeychain': api.constant('kCFBooleanFalse'),
                       'kSecUseKeychain' if adding else 'kSecMatchSearchList': ref if adding else search}

    def get(self, ref, service, account):
        api = self.api
        with self.query(ref, service, account) as entries:
            entries['kSecReturnData'] = api.constant('kCFBooleanTrue')
            entries['kSecMatchLimit'] = api.constant('kSecMatchLimitOne')
            with api.dictionary(entries) as query:
                result = C.c_void_p()
                api.check(api.sec.SecItemCopyMatching(query, C.byref(result)))
                with api.owned(result.value) as data:
                    if api.cf.CFGetTypeID(data) != api.cf.CFDataGetTypeID():
                        raise KeychainError('服务钥匙串条目类型无效')
                    size = api.cf.CFDataGetLength(data)
                    if not 0 < size <= MAX_SECRET_BYTES:
                        raise KeychainError('服务钥匙串条目长度无效')
                    return C.string_at(api.cf.CFDataGetBytePtr(data), size)

    def put(self, ref, service, account, raw, uid, gid):
        expected = self.path(ref)
        system_write = service == BOOTSTRAP_SERVICE and os.getuid() == os.geteuid() == 0
        # An opened/cached ref can outlive a missing file. Check immediately
        # before Add as well as at the enclosing private-context boundary.
        with _keychain_file(expected, 0 if system_write else os.getuid(),
                            mode=0o644 if system_write else 0o600):
            self._put(ref, service, account, raw, uid, gid, expected)

    def _put(self, ref, service, account, raw, uid, gid, expected):
        api = self.api
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_SECRET_BYTES:
            raise KeychainError('服务钥匙串条目长度无效')
        if self.status(ref) & 7 != 7:
            raise KeychainError('目标钥匙串未解锁或不可写，拒绝新增')
        with self.uid_access(uid, gid) as access, self.query(ref, service, account, adding=True) as entries:
            with api.owned(api.cf.CFDataCreate(None, raw, len(raw))) as data:
                entries.update(kSecAttrAccess=access, kSecValueData=data,
                               kSecReturnRef=api.constant('kCFBooleanTrue'))
                with api.dictionary(entries) as query:
                    item = C.c_void_p()
                    api.check(api.sec.SecItemAdd(query, C.byref(item)))
                    with api.owned(item.value):
                        actual = C.c_void_p()
                        api.check(api.sec.SecKeychainItemCopyKeychain(item, C.byref(actual)))
                        with api.owned(actual.value):
                            if self.path(actual.value) != expected:
                                raise KeychainError('凭据实际存储位置不符，可能已写入；保留现场，不自动删除')


def _unlock_secret(secret):
    if not isinstance(secret, bytes) or not re.fullmatch(b'[0-9a-f]{64}', secret):
        raise KeychainError('系统引导秘密格式无效')


def _canary(secret, value):
    return hmac.new(secret, b'TIANMING-KEYCHAIN-CHECK\0' + binding(value).encode(), hashlib.sha256).digest()


def _bootstrap(api, value):
    with api.system() as system:
        secret = api.get(system, BOOTSTRAP_SERVICE, binding(value))
    _unlock_secret(secret)
    return secret


@contextmanager
def unlocked(api, value):
    _identity(value)
    with _private_paths(value) as path:
        secret = _bootstrap(api, value)
        with api.opened(path) as ref:
            api.unlock(ref, secret)
            actual = api.get(ref, CANARY_SERVICE, binding(value))
            if not hmac.compare_digest(actual, _canary(secret, value)):
                raise KeychainError('专用钥匙串与系统引导秘密不匹配')
            # A successful Unlock on an already unlocked file proves no password.
            # The installation-bound canary above is required for every operation.
            yield ref


class ServiceStore:
    def __init__(self):
        self.value = load_context()
        self.api = Security()

    def get(self, account):
        with unlocked(self.api, self.value) as ref:
            return self.api.get(ref, SERVICE, account)

    def put(self, account, raw):
        with unlocked(self.api, self.value) as ref:
            self.api.put(ref, SERVICE, account, raw, self.value['uid'], self.value['gid'])
            if not hmac.compare_digest(self.api.get(ref, SERVICE, account), raw):
                raise KeychainError('新凭据回读未通过，可能已写入；保留现场')


def create_bootstrap(value, secret):
    """Administrator provisioning primitive, not a complete installer/receipt."""
    _identity(value, administrator=True)
    _unlock_secret(secret)
    api = Security()
    with api.system(writable=True) as ref:
        api.put(ref, BOOTSTRAP_SERVICE, binding(value), secret, value['uid'], value['gid'])
    # Only the target UID can decrypt. The service initialization must verify it.


def initialize_private(value):
    """Explicit service-UID initialization; caller provisions private parent dirs.

    Never called by normal get/put. Failure leaves the new file for inspection.
    An administrator publishes root-owned CONTEXT only after this and separate
    system-domain/cross-UID checks succeed; this function publishes no receipt.
    """
    _identity(value)
    api = Security()
    with _private_paths(value, existing=False) as path:
        secret = _bootstrap(api, value)
        with api.created(path, secret) as ref:
            with _keychain_file(path, value['uid']):
                api.put(ref, CANARY_SERVICE, binding(value), _canary(secret, value), value['uid'], value['gid'])
    with unlocked(api, value):
        pass
    return {'keychain_created': True, 'canary_verified': True, 'context_published': False}
