"""Read-only SMB metadata through sized Darwin FSCTLs and Apple's smbutil.

No SMBGetShareAttributes unbounded pointer ABI, mounting, secret access or file
contents. An FD is required, and the mount/session are sampled around smbutil.
The future backup writer must retain that FD and recheck it around every IO.

ABI facts: Apple SMBClient kernel/smbfs/smbfs.h, smbfs_vnops.c and netsmb/smb.h,
https://github.com/apple-oss-distributions/SMBClient . IOCTL command numbers
encode the exact structure size; a changed layout is not silently accepted.
"""
import ctypes as C
import errno
import os
import socket
import subprocess

from desktop_assistant import mac_mount as mounts, mac_smb_contract as cfg


class Access(C.Structure):
    _fields_ = [('auth',C.c_int32),('flags',C.c_int32),('error',C.c_int32),('length',C.c_int32),
                ('unique',C.c_uint64*160),('user',C.c_char*129),('padding',C.c_char*7)]


class Peer(C.Structure):
    _fields_ = [('uuid',C.c_ubyte*16),('address',C.c_uint64*16)]


def _commands():
    if (C.sizeof(Access) != 1432 or C.alignment(Access) != 8 or Access.user.offset != 1296
            or Access.unique.offset != 16 or C.sizeof(Peer) != 144 or C.alignment(Peer) != 8
            or Peer.address.offset != 16):
        raise ValueError('未经验证的SMB元数据ABI')
    return (0xC0000000 | (C.sizeof(Access)<<16) | (ord('z')<<8) | 19,
            0x40000000 | (C.sizeof(Peer)<<16) | (ord('z')<<8) | 30)


def _decode(access, peer):
    # EEXIST is Apple's success marker for a matched mount's access query.
    if access.flags != 1 or access.error != errno.EEXIST or access.auth not in (0,1,2,4):
        raise ValueError(cfg.ERROR)
    raw = C.string_at(C.addressof(access)+Access.user.offset, 129)
    if b'\0' not in raw: raise ValueError(cfg.ERROR)
    username = raw.split(b'\0',1)[0].decode('utf-8','strict')
    address = C.string_at(C.addressof(peer)+Peer.address.offset,128)
    # This contract approves literal IPv4 on the NAS's direct SMB port only.
    if address[0] != 16 or address[1] != socket.AF_INET: raise ValueError(cfg.ERROR)
    result = {'address':socket.inet_ntop(socket.AF_INET,address[4:8]),
        'port':int.from_bytes(address[2:4],'big'),'uuid':bytes(peer.uuid).hex(),
        'auth_type':{0:'user',1:'guest',2:'anonymous',4:'kerberos'}[access.auth],'username':username}
    if result['uuid'] == '0'*32 or result['port'] == 0: raise ValueError(cfg.ERROR)
    return result


def session(fd):
    """Query only a mounted SMB FD. Guest results remain observations, not approval."""
    before = mounts.fd_identity(fd)
    if before['filesystem'] != 'smbfs': raise ValueError(cfg.ERROR)
    access_command, peer_command = _commands()
    lib = mounts._lib()  # Includes real Darwin arm64 / statfs ABI checks.
    lib.ffsctl.argtypes = [C.c_int,C.c_ulong,C.c_void_p,C.c_uint]
    lib.ffsctl.restype = C.c_int
    access = Access(); access.flags = 1; access.auth = -1
    before_peer, peer = Peer(), Peer()
    if (lib.ffsctl(fd,peer_command,C.byref(before_peer),0) != 0
            or lib.ffsctl(fd,access_command,C.byref(access),0) != 0
            or lib.ffsctl(fd,peer_command,C.byref(peer),0) != 0
            or bytes(before_peer) != bytes(peer)):
        raise ValueError(cfg.ERROR)
    result = _decode(access,peer)
    if mounts.fd_identity(fd) != before: raise ValueError(cfg.ERROR)
    return result


def parse_share(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 65536: raise ValueError(cfg.ERROR)
    records = mounts.decode(raw)
    if not isinstance(records,list) or len(records) != 1 or not isinstance(records[0],dict):
        raise ValueError(cfg.ERROR)
    record = records[0]
    try:
        if (record['SMB_SHARE_TYPE'] != 'DISK' or record['VOLUME_RDONLY'] is not False
                or record['DFS_SHARE'] is not False or record['SNAPSHOT_MOUNT'] is not False
                or record['ENCRYPTION_REQUIRED'] is not True): raise ValueError(cfg.ERROR)
        result = {'name':record['share_name'],'server_name':record['SERVER_NAME'],'uid':record['USER_ID'],
            'dialect':record['SMB_VERSION'],'cipher':record['SMB_CURR_ENCRYPT_ALGORITHM'],
            'encrypted':record['ENCRYPTION_REQUIRED'],'signing':record['SIGNING_ON'],
            'reconnect_count':record['SESSION_RECONNECT_COUNT']}
    except KeyError:
        raise ValueError(cfg.ERROR) from None
    return result


def observe(value, fd, *, context_sha, uid):
    cfg.validate(value,context_sha,uid)
    if (os.getuid(),os.geteuid()) != (uid,uid): raise ValueError(cfg.ERROR)
    if value['server_address'] in mounts.local_addresses(): raise ValueError(cfg.ERROR)
    def snapshot():
        kernel = mounts.fd_identity(fd)
        if not cfg.kernel_matches(value,kernel) or mounts._select(value['mountpoint']) != kernel:
            raise ValueError(cfg.ERROR)
        connection = session(fd)
        if (connection['auth_type'] != 'user' or connection['username'] != value['username']
                or connection['address'] != value['server_address'] or connection['port'] != value['port']):
            raise ValueError(cfg.ERROR)
        return kernel,connection
    kernel,connection = snapshot()
    result = subprocess.run(['/usr/bin/smbutil','statshares','-m',value['mountpoint'],'-f','JSON'],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5,
        env={'PATH':'/usr/bin:/bin','LC_ALL':'C','HOME':'/var/empty'})
    if result.returncode or snapshot() != (kernel,connection): raise ValueError(cfg.ERROR)
    observation = {'type':cfg.OBSERVATION,'kernel':kernel,'session':connection,'share':parse_share(result.stdout)}
    return cfg.require_observation(value,observation)
