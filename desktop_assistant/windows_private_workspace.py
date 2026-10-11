"""Private plaintext staging for the original Windows factory user.

CreateDirectoryW applies the protected descriptor during exclusive creation.
GetSecurityInfo reads it back from a held handle before the caller may write.
All handles refuse delete sharing. No existing ACL is changed and no directory
is deleted, including on failure. This does not itself authorize a cutover.
"""
from contextlib import ExitStack, contextmanager
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import re
import sys
import uuid

from desktop_assistant.cleanup import local_path


SYSTEM = 'S-1-5-18'
FULL_CONTROL = 0x1F01FF
ERROR = 'Windows私有暂存权限未通过核验，禁止写入明文'


class _SecurityAttributes(C.Structure):
    _fields_ = [('length', W.DWORD), ('descriptor', C.c_void_p), ('inherit', W.BOOL)]


class _SidAndAttributes(C.Structure):
    _fields_ = [('sid', C.c_void_p), ('attributes', W.DWORD)]


class _Acl(C.Structure):
    _fields_ = [('revision', W.BYTE), ('reserved', W.BYTE), ('size', W.WORD),
                ('count', W.WORD), ('reserved2', W.WORD)]


class _Ace(C.Structure):
    _fields_ = [('type', W.BYTE), ('flags', W.BYTE), ('size', W.WORD),
                ('mask', W.DWORD), ('sid_start', W.DWORD)]


class _FileInfo(C.Structure):
    _fields_ = [('attributes', W.DWORD), ('created', W.FILETIME),
                ('accessed', W.FILETIME), ('written', W.FILETIME),
                ('volume', W.DWORD), ('size_high', W.DWORD), ('size_low', W.DWORD),
                ('links', W.DWORD), ('index_high', W.DWORD), ('index_low', W.DWORD)]


def _validate_security(record, sid):
    expected = sorted([(sid, 0, 3, FULL_CONTROL), (SYSTEM, 0, 3, FULL_CONTROL)])
    if (not re.fullmatch(r'S-1-5-21-(?:[0-9]+-){3}[0-9]+', sid)
            or record['owner'] != sid or record['protected'] is not True
            or sorted(record['aces']) != expected):
        raise ValueError(ERROR)


class _Windows:
    def __init__(self):
        if sys.platform != 'win32':
            raise ValueError('私有切换暂存只支持Windows原用户')
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.a = C.WinDLL('advapi32', use_last_error=True)
        pointer = C.c_void_p
        pp = C.POINTER(pointer)
        dwordp = C.POINTER(W.DWORD)
        bindings = [
            (self.k, 'CloseHandle', W.BOOL, [W.HANDLE]),
            (self.k, 'LocalFree', pointer, [pointer]),
            (self.k, 'GetCurrentProcess', W.HANDLE, []),
            (self.k, 'GetVolumePathNameW', W.BOOL, [W.LPCWSTR, W.LPWSTR, W.DWORD]),
            (self.k, 'GetDriveTypeW', W.UINT, [W.LPCWSTR]),
            (self.k, 'GetVolumeInformationW', W.BOOL,
             [W.LPCWSTR, W.LPWSTR, W.DWORD, dwordp, dwordp, dwordp, W.LPWSTR, W.DWORD]),
            (self.k, 'CreateDirectoryW', W.BOOL, [W.LPCWSTR, C.POINTER(_SecurityAttributes)]),
            (self.k, 'CreateFileW', W.HANDLE,
             [W.LPCWSTR, W.DWORD, W.DWORD, pointer, W.DWORD, W.DWORD, W.HANDLE]),
            (self.k, 'GetFileInformationByHandle', W.BOOL, [W.HANDLE, C.POINTER(_FileInfo)]),
            (self.a, 'OpenProcessToken', W.BOOL, [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]),
            (self.a, 'GetTokenInformation', W.BOOL, [W.HANDLE, W.DWORD, pointer, W.DWORD, dwordp]),
            (self.a, 'ConvertSidToStringSidW', W.BOOL, [pointer, pp]),
            (self.a, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', W.BOOL,
             [W.LPCWSTR, W.DWORD, pp, dwordp]),
            (self.a, 'GetSecurityInfo', W.DWORD, [W.HANDLE, W.DWORD, W.DWORD, pp, pp, pp, pp, pp]),
            (self.a, 'GetSecurityDescriptorControl', W.BOOL, [pointer, C.POINTER(W.WORD), dwordp]),
            (self.a, 'GetAce', W.BOOL, [pointer, W.DWORD, pp]),
        ]
        for library, name, restype, args in bindings:
            fn = getattr(library, name)
            fn.restype, fn.argtypes = restype, args

    @staticmethod
    def _ok(result):
        if not result:
            raise OSError('Windows安全API调用失败')

    def sid_text(self, sid):
        text = C.c_void_p()
        self._ok(self.a.ConvertSidToStringSidW(sid, C.byref(text)))
        try:
            return C.wstring_at(text)
        finally:
            self.k.LocalFree(text)

    def current_sid(self):
        token = W.HANDLE()
        self._ok(self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, C.byref(token)))
        try:
            length = W.DWORD()
            self.a.GetTokenInformation(token, 1, None, 0, C.byref(length))
            if not 0 < length.value <= 65536:
                raise ValueError(ERROR)
            buffer = C.create_string_buffer(length.value)
            self._ok(self.a.GetTokenInformation(token, 1, buffer, len(buffer), C.byref(length)))
            sid = self.sid_text(C.cast(buffer, C.POINTER(_SidAndAttributes)).contents.sid)
            if not re.fullmatch(r'S-1-5-21-(?:[0-9]+-){3}[0-9]+', sid):
                raise ValueError('暂存必须由原Windows用户创建，不能使用系统服务身份')
            return sid
        finally:
            self.k.CloseHandle(token)

    def require_local_acl_volume(self, parent):
        volume = C.create_unicode_buffer(32768)
        self._ok(self.k.GetVolumePathNameW(str(parent), volume, len(volume)))
        flags = W.DWORD()
        self._ok(self.k.GetVolumeInformationW(volume.value, None, 0, None, None,
                                              C.byref(flags), None, 0))
        # DRIVE_FIXED and FILE_PERSISTENT_ACLS; reject NAS, removable/FAT media.
        if self.k.GetDriveTypeW(volume.value) != 3 or not flags.value & 8:
            raise ValueError('明文暂存必须位于支持持久ACL的本地固定磁盘')

    @contextmanager
    def directory(self, path):
        # Include FILE_LIST_DIRECTORY (read-data) so Windows enforces share
        # checks; metadata-only handles do not reliably pin names against delete.
        # READ_CONTROL | FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY.
        handle = self.k.CreateFileW(str(path), 0x20081, 3, None, 3, 0x02200000, None)
        if handle in (None, C.c_void_p(-1).value):
            raise OSError('无法固定私有暂存目录')
        try:
            info = _FileInfo()
            self._ok(self.k.GetFileInformationByHandle(handle, C.byref(info)))
            if not info.attributes & 0x10 or info.attributes & 0x400:
                raise ValueError('暂存路径不是普通目录或包含重解析点')
            yield handle
        finally:
            self.k.CloseHandle(handle)

    def create(self, path, sid):
        descriptor = C.c_void_p()
        sddl = f'O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)'
        self._ok(self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, C.byref(descriptor), None))
        try:
            attributes = _SecurityAttributes(C.sizeof(_SecurityAttributes), descriptor, False)
            if not self.k.CreateDirectoryW(str(path), C.byref(attributes)):
                # Never reuse or change a pre-existing directory, including a race.
                raise OSError('私有暂存目录未独占创建，原目录保持不变')
        finally:
            self.k.LocalFree(descriptor)

    @contextmanager
    def read_file(self, path):
        # GENERIC_READ; FILE_SHARE_READ only. Existing writers and future
        # writers/deleters conflict with this handle, including other aliases.
        handle = self.k.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle in (None, C.c_void_p(-1).value):
            raise OSError('数据库仍可写或无法建立只读租约，保持原状态')
        try:
            info = _FileInfo()
            self._ok(self.k.GetFileInformationByHandle(handle, C.byref(info)))
            if info.attributes & (0x10 | 0x400):
                raise ValueError('数据库不是普通文件或包含重解析点')
            yield handle
        finally:
            self.k.CloseHandle(handle)

    def security(self, handle):
        owner, acl, descriptor = C.c_void_p(), C.c_void_p(), C.c_void_p()
        result = self.a.GetSecurityInfo(handle, 1, 5, C.byref(owner), None,
                                       C.byref(acl), None, C.byref(descriptor))
        if result != 0:
            raise OSError('无法回读暂存目录权限')
        try:
            control, revision = W.WORD(), W.DWORD()
            self._ok(self.a.GetSecurityDescriptorControl(descriptor, C.byref(control), C.byref(revision)))
            if not owner.value or not acl.value or not control.value & 4:
                raise ValueError(ERROR)
            header = C.cast(acl, C.POINTER(_Acl)).contents
            if header.count != 2:
                raise ValueError(ERROR)
            entries = []
            for index in range(header.count):
                ace = C.c_void_p()
                self._ok(self.a.GetAce(acl, index, C.byref(ace)))
                value = C.cast(ace, C.POINTER(_Ace)).contents
                if value.type != 0 or value.size < C.sizeof(_Ace):
                    raise ValueError(ERROR)
                sid = self.sid_text(ace.value + _Ace.sid_start.offset)
                entries.append((sid, value.type, value.flags, value.mask))
            return {'owner': self.sid_text(owner), 'protected': bool(control.value & 0x1000),
                    'aces': entries}
        finally:
            self.k.LocalFree(descriptor)


@contextmanager
def private_workspace(parent):
    """Yield a newly verified workspace; retain it after success or failure.

    Callers own disposal only after saving evidence and checking idle state.
    Keep the context open until all plaintext access has finished.
    """
    native = _Windows()  # No filesystem mutation on other hosts.
    parent = Path(parent)
    if not parent.is_absolute():
        raise ValueError('暂存父目录必须是已存在的绝对路径')
    parent = local_path(parent)
    if not parent.is_dir():
        raise ValueError('暂存父目录不存在')
    with ExitStack() as stack:
        # Pin each ancestor before opening the next component; reject reparse
        # handles too, closing the check/open race left by a path-only check.
        for component in (*reversed(parent.parents), parent):
            parent_handle = stack.enter_context(native.directory(component))
        native.require_local_acl_volume(parent)
        sid = native.current_sid()
        # A pinned parent cannot itself be renamed, but DELETE_CHILD rights
        # could still replace a newly created job before it is opened. Require
        # an already private, protected parent; never repair a broad ACL here.
        _validate_security(native.security(parent_handle), sid)
        job = parent / ('cutover-' + uuid.uuid4().hex)
        native.create(job, sid)
        handle = stack.enter_context(native.directory(job))
        _validate_security(native.security(handle), sid)
        yield job
        _validate_security(native.security(handle), sid)


@contextmanager
def private_directory(parent):
    """Pin an existing protected directory; yield a repeatable ACL check.

    Nothing is created and no permissions are changed. This is suitable for
    publishing a nonsecret signed artifact after source authentication.
    """
    native = _Windows()
    original = Path(parent)
    if not original.is_absolute():
        raise ValueError('私有输出目录必须是已有绝对路径')
    parent = local_path(original)
    if not parent.is_dir():
        raise ValueError('私有输出目录不存在')
    with ExitStack() as stack:
        for component in (*reversed(parent.parents), parent):
            handle = stack.enter_context(native.directory(component))
        native.require_local_acl_volume(parent)
        sid = native.current_sid()

        def check():
            _validate_security(native.security(handle), sid)

        check()
        yield check
        check()


@contextmanager
def read_file_lease(path):
    """Pin an existing local Windows file against writes/renames; never edit it."""
    native = _Windows()
    original = Path(path)
    if not original.is_absolute():
        raise ValueError('只读租约要求已有绝对路径')
    path = local_path(original)
    if not path.is_file():
        raise ValueError('只读租约要求已有普通文件')
    with ExitStack() as stack:
        for component in reversed(path.parents):
            stack.enter_context(native.directory(component))
        native.require_local_acl_volume(path.parent)
        stack.enter_context(native.read_file(path))
        yield
