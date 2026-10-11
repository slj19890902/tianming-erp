"""Read-only ACL check for a provisioned, exclusive Mac service directory."""
import ctypes
import errno
import os
import sys


def require_no_acl(path):
    """Reject extended ACLs instead of assuming POSIX mode bits are complete.

    Provisioning must choose an ACL-free directory and ancestors. This does not
    remove permissions or rewrite an existing user's directory ACLs.
    """
    if sys.platform != 'darwin':
        raise ValueError('服务目录 ACL 核验仅支持 macOS')
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.acl_get_fd_np.restype = ctypes.c_void_p
    libc.acl_valid.argtypes = [ctypes.c_void_p]
    libc.acl_valid.restype = ctypes.c_int
    libc.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    libc.acl_get_entry.restype = ctypes.c_int
    libc.acl_free.argtypes = [ctypes.c_void_p]
    libc.acl_free.restype = ctypes.c_int
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise OSError(error.errno, '不能读取服务目录 ACL') from None
    acl = None
    try:
        before = os.fstat(descriptor)
        ctypes.set_errno(0)
        acl = libc.acl_get_fd_np(descriptor, 0x100)  # ACL_TYPE_EXTENDED, sys/acl.h
        error = ctypes.get_errno()
        after = os.stat(path, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError('服务目录在 ACL 检查期间被替换')
        if not acl:
            # Apple Libc uses filesec_get_property(FILESEC_ACL), which returns
            # ENOENT for an absent ACL. Use an open, verified directory FD so
            # a missing pathname cannot be mistaken for this empty property.
            if error == errno.ENOENT:
                return
            raise OSError(error, '不能读取服务目录 ACL')
        if libc.acl_valid(acl) != 0:
            raise ValueError('服务目录 ACL 无效')
        entry = ctypes.c_void_p()
        ctypes.set_errno(0)
        result = libc.acl_get_entry(acl, 0, ctypes.byref(entry))  # ACL_FIRST_ENTRY
        if result == 0:  # Darwin returns 0 for an entry (not POSIX's 1).
            raise ValueError('服务目录或祖先含扩展 ACL，不能确认独占权限')
        if result != -1 or ctypes.get_errno() != errno.EINVAL:
            raise OSError(ctypes.get_errno(), '不能确认服务目录 ACL 为空')
    finally:
        if acl:
            libc.acl_free(acl)
        os.close(descriptor)
