"""Local macOS credentials via Security.framework, never shell arguments.

Configuration holds a purpose-scoped random reference, not a portable secret.
Recovery must separately rewrap credentials; copying a keychain file is not a
supported backup strategy. The file-based keychain supports a service user;
the data-protection keychain requires a provisioned, signed application.
"""
from contextlib import contextmanager
import ctypes as C
import os
from pathlib import Path
import re
import sys
import uuid

SERVICE = 'cn.tianming.erp.credentials.v1'
PREFIX = 'tm-keychain-v1'
PURPOSES = frozenset({'mailbox', 'backup', 'openai', 'deepseek'})
MAX_SECRET_BYTES = 16384


class KeychainError(ValueError):
    """Safe error without secret or query contents."""


class Keychain:
    """Short-lived CF objects; no broad search, updates, ACL changes or prompts."""

    def __init__(self):
        if sys.platform != 'darwin':
            raise KeychainError('此凭据需要原 Mac 服务用户的钥匙串')
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.sec = C.CDLL('/System/Library/Frameworks/Security.framework/Security')
        p, n = C.c_void_p, C.c_long
        declarations = [
            (self.cf, 'CFStringCreateWithBytes', p, [p, p, n, C.c_uint32, C.c_bool]),
            (self.cf, 'CFDataCreate', p, [p, p, n]),
            (self.cf, 'CFDataGetLength', n, [p]),
            (self.cf, 'CFDataGetBytePtr', p, [p]),
            (self.cf, 'CFGetTypeID', C.c_ulong, [p]),
            (self.cf, 'CFDataGetTypeID', C.c_ulong, []),
            (self.cf, 'CFDictionaryCreate', p, [p, p, p, n, p, p]),
            (self.cf, 'CFArrayCreate', p, [p, p, n, p]),
            (self.cf, 'CFRelease', None, [p]),
            (self.sec, 'SecKeychainCopyDefault', C.c_int32, [C.POINTER(p)]),
            (self.sec, 'SecKeychainGetPath', C.c_int32, [p, C.POINTER(C.c_uint32), p]),
            (self.sec, 'SecItemAdd', C.c_int32, [p, C.POINTER(p)]),
            (self.sec, 'SecItemCopyMatching', C.c_int32, [p, C.POINTER(p)]),
        ]
        for library, name, result, arguments in declarations:
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments

    def constant(self, name):
        library = self.cf if name.startswith('kCF') else self.sec
        return C.c_void_p.in_dll(library, name).value

    def check(self, status):
        if status:
            raise KeychainError(f'钥匙串操作被拒绝（OSStatus {status}）；请由服务用户解锁或安全重新移交凭据')

    @contextmanager
    def owned(self, value):
        if not value:
            raise KeychainError('无法分配钥匙串查询资源')
        try:
            yield value
        finally:
            self.cf.CFRelease(value)

    @contextmanager
    def text(self, value):
        raw = value.encode('utf-8')
        with self.owned(self.cf.CFStringCreateWithBytes(None, raw, len(raw), 0x08000100, False)) as ref:
            yield ref

    @contextmanager
    def dictionary(self, entries):
        # Values remain owned by their surrounding contexts; no callback retain.
        keys = (C.c_void_p * len(entries))(*(self.constant(k) for k in entries))
        values = (C.c_void_p * len(entries))(*entries.values())
        with self.owned(self.cf.CFDictionaryCreate(None, keys, values, len(entries), None, None)) as ref:
            yield ref

    @contextmanager
    def current_keychain(self):
        ref = C.c_void_p()
        self.check(self.sec.SecKeychainCopyDefault(C.byref(ref)))
        with self.owned(ref.value):
            size = C.c_uint32(4096)
            buffer = C.create_string_buffer(size.value)
            self.check(self.sec.SecKeychainGetPath(ref, C.byref(size), buffer))
            path = Path(os.fsdecode(buffer.value)).resolve(strict=True)
            directory = (Path.home() / 'Library/Keychains').resolve(strict=True)
            if not path.is_relative_to(directory) or path.stat().st_uid != os.getuid():
                raise KeychainError('拒绝在非当前用户的钥匙串保存或读取 ERP 凭据')
            yield ref.value

    @contextmanager
    def query(self, account, *, adding=False):
        with self.current_keychain() as keychain, self.text(SERVICE) as service, self.text(account) as user:
            refs = (C.c_void_p * 1)(keychain)
            with self.owned(self.cf.CFArrayCreate(None, refs, 1, None)) as search:
                entries = {
                    'kSecClass': self.constant('kSecClassGenericPassword'),
                    'kSecAttrService': service,
                    'kSecAttrAccount': user,
                    'kSecUseAuthenticationUI': self.constant('kSecUseAuthenticationUIFail'),
                    'kSecAttrSynchronizable': self.constant('kCFBooleanFalse'),
                    'kSecUseKeychain' if adding else 'kSecMatchSearchList': keychain if adding else search,
                }
                yield entries

    def put(self, account, raw):
        with self.query(account, adding=True) as entries:
            with self.owned(self.cf.CFDataCreate(None, raw, len(raw))) as data:
                entries['kSecValueData'] = data
                with self.dictionary(entries) as query:
                    # Duplicate is an error, never overwrite an existing item.
                    self.check(self.sec.SecItemAdd(query, None))

    def get(self, account):
        with self.query(account) as entries:
            entries['kSecReturnData'] = self.constant('kCFBooleanTrue')
            entries['kSecMatchLimit'] = self.constant('kSecMatchLimitOne')
            with self.dictionary(entries) as query:
                result = C.c_void_p()
                self.check(self.sec.SecItemCopyMatching(query, C.byref(result)))
                with self.owned(result.value) as data:
                    if self.cf.CFGetTypeID(data) != self.cf.CFDataGetTypeID():
                        raise KeychainError('钥匙串返回的凭据格式无效')
                    size = self.cf.CFDataGetLength(data)
                    if not 0 < size <= MAX_SECRET_BYTES:
                        raise KeychainError('钥匙串凭据长度无效')
                    return C.string_at(self.cf.CFDataGetBytePtr(data), size)


def _purpose(purpose):
    if purpose not in PURPOSES:
        raise KeychainError('不支持的 ERP 凭据用途')


def protect(value: str, purpose: str) -> str:
    _purpose(purpose)
    if not isinstance(value, str):
        raise KeychainError('凭据必须为文本')
    try:
        raw = value.encode('utf-8')
    except UnicodeError:
        raise KeychainError('凭据编码无效') from None
    if not 0 < len(raw) <= MAX_SECRET_BYTES:
        raise KeychainError('凭据为空或长度超限')
    reference = f'{PREFIX}:{purpose}:{uuid.uuid4().hex}'
    Keychain().put(reference, raw)
    return reference


def unprotect(reference: str, purpose: str) -> str:
    _purpose(purpose)
    if not isinstance(reference, str) or not re.fullmatch(
            re.escape(f'{PREFIX}:{purpose}:') + '[0-9a-f]{32}', reference):
        raise KeychainError('旧 Windows DPAPI 或其他用途凭据不能在此读取；需原用户安全移交')
    try:
        return Keychain().get(reference).decode('utf-8')
    except UnicodeError:
        raise KeychainError('钥匙串凭据编码无效') from None
