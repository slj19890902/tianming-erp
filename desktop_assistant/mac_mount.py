"""Read-only Darwin arm64 mount observations, never an archive approval.

getfsstat(MNT_NOWAIT) avoids polling every mounted filesystem. NFS's documented
JSON is sampled between two kernel identities; the actual open FD is checked
separately by the backup writer. This is not an atomic/ABA-proof mount query.
"""
import ctypes as C
import ipaddress
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys


class Statfs(C.Structure):
    _fields_ = [('bsize', C.c_uint32), ('iosize', C.c_int32),
                *[(n, C.c_uint64) for n in ('blocks', 'bfree', 'bavail', 'files', 'ffree')],
                ('fsid', C.c_int32 * 2), ('owner', C.c_uint32), ('type', C.c_uint32),
                ('flags', C.c_uint32), ('subtype', C.c_uint32), ('fstype', C.c_char * 16),
                ('mountpoint', C.c_char * 1024), ('source', C.c_char * 1024),
                ('flags_ext', C.c_uint32), ('reserved', C.c_uint32 * 7)]


def decode(raw):
    def pairs(items):
        result = {}
        for k, value in items:
            if k in result: raise ValueError('重复存储元数据字段')
            result[k] = value
        return result
    def constant(_): raise ValueError('存储元数据不能含非有限值')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _lib():
    if (sys.platform != 'darwin' or platform.machine() != 'arm64'
            or C.sizeof(Statfs) != 2168 or C.alignment(Statfs) != 8
            or Statfs.fstype.offset != 72 or Statfs.source.offset != 1112):
        raise ValueError('未经验证的Mac挂载ABI')
    return C.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)


def _identity(value):
    def string(name, size):
        raw = C.string_at(C.addressof(value) + getattr(Statfs, name).offset, size)
        if b'\0' not in raw: raise ValueError('挂载字段未终结')
        result = raw.split(b'\0', 1)[0].decode('utf-8', 'strict')
        if not result: raise ValueError('空挂载字段')
        return result
    mountpoint = string('mountpoint', 1024)
    if not Path(mountpoint).is_absolute() or os.path.normpath(mountpoint) != mountpoint:
        raise ValueError('挂载点不是规范绝对路径')
    return {'fsid': list(value.fsid), 'filesystem': string('fstype', 16),
            'mountpoint': mountpoint, 'source': string('source', 1024),
            'owner': value.owner, 'flags': value.flags, 'subtype': value.subtype}


def mounts():
    lib = _lib()
    lib.getfsstat.argtypes = [C.POINTER(Statfs), C.c_int, C.c_int]
    lib.getfsstat.restype = C.c_int
    for _ in range(2):
        count = lib.getfsstat(None, 0, 2)  # MNT_NOWAIT, cached kernel metadata.
        if not 0 < count < 1024: raise ValueError('挂载列表长度异常')
        values = (Statfs * (count + 16))()
        actual = lib.getfsstat(values, C.sizeof(values), 2)
        if actual < 0: raise ValueError('无法读取挂载元数据')
        if actual >= len(values): continue
        result = [_identity(values[n]) for n in range(actual)]
        if len({v['mountpoint'] for v in result}) != len(result):
            raise ValueError('重复挂载点')
        return result
    raise ValueError('挂载列表持续变化')


def fd_identity(fd):
    lib = _lib(); value = Statfs()
    lib.fstatfs.argtypes = [C.c_int, C.POINTER(Statfs)]
    lib.fstatfs.restype = C.c_int
    if lib.fstatfs(fd, C.byref(value)) != 0: raise ValueError('无法核验已打开的存储设备')
    return _identity(value)


def _select(path):
    path = Path(path)
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        raise ValueError('存储路径必须为规范绝对路径')
    choices = [m for m in mounts() if Path(m['mountpoint']) == path or Path(m['mountpoint']) in path.parents]
    if not choices: raise ValueError('未找到挂载')
    return max(choices, key=lambda m: len(Path(m['mountpoint']).parts))


def ip(value):
    address = ipaddress.ip_address(value)
    return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address


def parse_nfs(raw, kernel):
    value = decode(raw)
    if not isinstance(value, dict) or set(value) != {kernel['source']}:
        raise ValueError('NFS记录不唯一或来源变化')
    record = value[kernel['source']]
    if record['Mount Point'] != kernel['mountpoint']: raise ValueError('NFS挂载点不符')
    current = record['Current mount parameters']
    if int(current['General mount flags']['Bitmask'], 16) != kernel['flags']:
        raise ValueError('NFS内核标志变化')
    if int(record['Status flags']['Bitmask'], 16) != 0: raise ValueError('NFS状态需要检查')
    locations = current['File system locations']
    if not isinstance(locations, list) or len(locations) != 1: raise ValueError('未知NFS多位置语义')
    location = locations[0]
    if any(not isinstance(location[k], str) or not location[k] for k in ('Export', 'Server')):
        raise ValueError('NFS导出身份为空')
    addresses = location['Locations']
    if not isinstance(addresses, list) or not addresses or any(not isinstance(a, str) for a in addresses):
        raise ValueError('NFS实际地址缺失')
    addresses = sorted(set(str(ip(a)) for a in addresses))
    params = current['NFS parameters']
    if not isinstance(params, list) or any(not isinstance(p, str) for p in params):
        raise ValueError('NFS参数类型异常')
    if (len(params) != len(set(params))
            or len({p.split('=', 1)[0] for p in params}) != len(params)
            or params.count('tcp') != 1
            or any(p == 'udp' or p.startswith(('proto=', 'tcp=', 'udp=', 'tcp4', 'tcp6', 'udp4', 'udp6', 'inet')) for p in params)):
        raise ValueError('NFS传输参数缺失、重复或冲突')
    def parameter(name):
        found = [p[len(name)+1:] for p in params if p.startswith(name+'=')]
        if len(found) != 1 or not found[0]: raise ValueError('NFS参数缺失或重复')
        return found[0]
    version, port, security = parameter('vers'), parameter('port'), parameter('sec')
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError('NFS端口无效')
    return {'addresses': addresses, 'export': location['Export'], 'server': location['Server'],
            'version': version, 'port': int(port), 'security': security, 'tcp': 'tcp' in params}


def observe(path):
    """Classify without touching a remote file; failures cannot approve storage."""
    try:
        before = _select(path)
        if before['flags'] & 0x1000 or before['filesystem'] == 'apfs':
            return {'classification': 'local_or_unmounted', 'kernel': before}
        if before['filesystem'] != 'nfs': return {'classification': 'unknown', 'kernel': before}
        result = subprocess.run(['/usr/bin/nfsstat', '-f', 'JSON', '-m', before['mountpoint']],
            capture_output=True, timeout=5, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode or not 0 < len(result.stdout) <= 65536: raise ValueError('NFS查询失败')
        remote = parse_nfs(result.stdout, before)
        if _select(path) != before: raise ValueError('NFS挂载身份变化')
        classification = ('loopback_proxy' if any(ip(a).is_loopback for a in remote['addresses'])
                          else 'direct_network_unbound')
        return {'classification': classification, 'kernel': before, 'remote': remote}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        return {'classification': 'unknown'}


def local_addresses():
    # This is an OS interface inventory, not DNS or an outbound network call.
    import psutil
    return {str(ip(item.address.split('%', 1)[0]))
            for items in psutil.net_if_addrs().values() for item in items
            if item.family in (socket.AF_INET, socket.AF_INET6)}
