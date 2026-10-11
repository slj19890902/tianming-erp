"""Native SMB mount transport for the dedicated service; not an install entry.

The future approved worker supplies its protected contract and credential in
memory. Nothing is put in process arguments, environment, files or error text.
No preference changes, guest fallback, remount, unmount or backup writes.

Scalar ABI and flags: Apple SMBClient lib/smbclient/{smbclient.h,
smbclient_internal.h,server.c}, https://github.com/apple-oss-distributions/SMBClient.
Actual connection approval remains the sized metadata check in mac_smb_mount.
"""
from contextlib import contextmanager, ExitStack
import ctypes as C
import os
from pathlib import Path
import stat
from urllib.parse import quote_from_bytes

from desktop_assistant import mac_keychain_install as boot
from desktop_assistant import mac_mount as mounts, mac_smb_contract as cfg, mac_smb_mount as observer

LIBRARY='/System/Library/PrivateFrameworks/SMBClient.framework/SMBClient'
# NoPrompt | NoUserPreferences | ForceNewSession | SessionEncrypt | SessionOnly.
OPEN_OPTIONS=0x00010107
# NoStreams | NoNotifications | SoftMount | ForceNewSession | session/share encrypt.
MOUNT_OPTIONS=0x00000317
MOUNT_FLAGS=0x00100018  # MNT_DONTBROWSE | MNT_NOSUID | MNT_NODEV; read/write.
ERROR='专用SMB挂载未确认；保留现场，未允许备份写入'


def require(value):
    if not value:raise ValueError(ERROR)


def library():
    mounts._lib()  # Darwin arm64 and the installed statfs ABI only.
    lib=C.CDLL(LIBRARY,use_errno=True)
    for name,result,args in (
        ('SMBOpenServerEx',C.c_uint32,[C.c_char_p,C.POINTER(C.c_void_p),C.c_uint64]),
        ('SMBMountShareEx',C.c_uint32,[C.c_void_p,C.c_char_p,C.c_char_p,C.c_uint,C.c_uint64,
            C.c_uint16,C.c_uint16,C.c_void_p,C.c_void_p]),
        ('SMBReleaseServer',C.c_uint32,[C.c_void_p]),
    ):
        fn=getattr(lib,name);fn.restype=result;fn.argtypes=args
    return lib


def _url(value,password):
    require(isinstance(password,str))
    try:raw=password.encode('utf-8','strict')
    except UnicodeError:raise ValueError(ERROR) from None
    require(0<len(raw)<=128 and b'\0' not in raw)  # Apple's SMB_MAXPASSWORDLEN.
    return ('smb://'+value['username']+':'+quote_from_bytes(raw,safe='')+'@'+
        value['server_address']+'/'+value['share']).encode('ascii')


def _stamp(info):
    return (info.st_dev,info.st_ino,info.st_uid,info.st_gid,info.st_mode)


def _parent_permissions(path,info):
    # The system Data volume is root:admin 0775 on current macOS. Its descendants
    # must be root-controlled. Dedicated workers may never belong to admin.
    if path==Path('/System/Volumes/Data') and info.st_mode&0o022:
        return info.st_uid==0 and info.st_gid==80 and stat.S_IMODE(info.st_mode)==0o775
    return info.st_uid==0 and not info.st_mode&0o022


@contextmanager
def empty_mountpoint(uid):
    """Pin every existing component; never create or adopt a mounted directory."""
    with ExitStack() as stack:
        chain=[];parent=None
        for path in (Path('/'),*reversed(cfg.MOUNT.parents[:-1]),cfg.MOUNT):
            fd=os.open(path if parent is None else path.name,
                os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            stack.callback(os.close,fd)
            info=os.fstat(fd);require(stat.S_ISDIR(info.st_mode));boot.no_acl(fd)
            if path==cfg.MOUNT:
                require(info.st_uid==uid and info.st_gid==uid and stat.S_IMODE(info.st_mode)==0o700)
            else:require(_parent_permissions(path,info))
            chain.append((path,fd,parent,_stamp(info)));parent=fd
        local=mounts.fd_identity(parent)
        require(local['filesystem']=='apfs' and local['flags']&0x1000 and local['mountpoint']!=str(cfg.MOUNT))
        require(local==mounts.fd_identity(chain[-2][1]))
        def parents():
            for path,fd,pfd,initial in chain[:-1]:
                boot.no_acl(fd)
                named=os.stat(path if pfd is None else path.name,dir_fd=pfd,follow_symlinks=False)
                require(_stamp(os.fstat(fd))==initial==_stamp(named))
        def unmounted():
            parents();path,fd,pfd,initial=chain[-1]
            boot.no_acl(fd)
            require(mounts.fd_identity(fd)==local and mounts._select(path)==local)
            require(_stamp(os.fstat(fd))==initial==_stamp(os.stat(path.name,dir_fd=pfd,follow_symlinks=False)))
            require(not os.listdir(fd))
        unmounted()
        yield unmounted,parents
        parents()


def mount(value,password,*,context_sha,uid):
    """One native attempt; return observed identity, never a backup success.

    Caller must hold its installation/backup lock and authenticate the root
    contract before calling. An uncertain return preserves the mount/scene;
    callers must inspect it before another attempt. This layer has no retries.
    """
    cfg.validate(value,context_sha,uid)
    require((os.getuid(),os.geteuid(),os.getgid(),os.getegid())==(uid,uid,uid,uid))
    require(80 not in boot._actual()['kernel_groups'])
    require(value['server_address'] not in mounts.local_addresses())
    url=_url(value,password)
    with empty_mountpoint(uid) as (unmounted,parents):
        lib=library();connection=C.c_void_p()
        try:
            require(lib.SMBOpenServerEx(url,C.byref(connection),OPEN_OPTIONS)==0 and connection.value)
            unmounted()
            require(lib.SMBMountShareEx(connection,value['share'].encode('ascii'),os.fsencode(cfg.MOUNT),
                MOUNT_FLAGS,MOUNT_OPTIONS,0o600,0o700,None,None)==0)
        except Exception:
            raise ValueError(ERROR) from None
        finally:
            if connection.value:
                # Releases only the userspace handle; does not unmount the volume.
                lib.SMBReleaseServer(connection)
            del url
        parents()
        fd=os.open(cfg.MOUNT,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            result=observer.observe(value,fd,context_sha=context_sha,uid=uid)
            parents()
            return result
        finally:os.close(fd)
