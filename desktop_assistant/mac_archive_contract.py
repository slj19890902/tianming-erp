"""Strict, public autofs configuration shared by root setup and backup workers.

Only standard-library code. No credentials, ERP imports, mount commands or
remote file access. A valid map is configuration, never storage availability.
"""
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import stat
import unicodedata

BASE = Path('/Library/TianmingERP')
MOUNT = Path('/System/Volumes/Data/Library/TianmingERP/archive')
DESTINATION = MOUNT/'backups'
MASTER = Path('/private/etc/auto_master')
MAP = Path('/private/etc/auto_tianming_erp')
TYPE = 'tianming.mac-autofs-archive.v1'
OPTIONS = 'fstype=nfs,rw,hard,nointr,tcp,vers=4,port=2049,sec=sys,resvport,nosuid,nodev'
FIELDS = {'type', 'context_sha256', 'mountpoint', 'destination', 'source',
          'server_address', 'export', 'version', 'port', 'security', 'map_sha256', 'master_sha256'}


def digest(raw): return hashlib.sha256(raw).hexdigest()
def sha(value): return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def endpoint(address, export):
    # This initial installer supports a single literal IPv4 NFS server. IPv6
    # and hostname/multi-server maps need separately verified source syntax.
    if not isinstance(address, str): raise ValueError('NAS地址必须为明确IPv4字面值')
    ip = ipaddress.IPv4Address(address)
    if (str(ip) != address or ip.is_loopback or ip.is_unspecified or ip.is_multicast
            or ip.is_link_local or address == '255.255.255.255'):
        raise ValueError('NAS不能是本机代理、未指定或多播地址')
    if (not isinstance(export, str) or not export.startswith('/') or export == '/'
            or len(export.encode('utf-8')) > 1024 or unicodedata.normalize('NFC', export) != export
            or any(part in ('', '.', '..') for part in export[1:].split('/'))
            or any(not (c.isalnum() or c in '/-_.') for c in export)):
        raise ValueError('NAS导出必须是可精确表示的字面路径，不能含autofs宏或语法字符')
    return address + ':' + export


def map_bytes(value):
    source = endpoint(value['server_address'], value['export'])
    return (str(MOUNT)+'\t-'+OPTIONS+'\t'+source+'\n').encode('utf-8')


def master_line(): return ('/-\t'+str(MAP)+'\n').encode('utf-8')


def append_master(original):
    if not isinstance(original, bytes) or not 0 < len(original) <= 65536 or b'\0' in original:
        raise ValueError('现有auto_master无效或过长')
    original.decode('utf-8', 'strict')
    if not original.endswith(b'\n'): raise ValueError('auto_master末行必须已有换行，不能隐式修正')
    if MAP.name.encode() in original or str(MOUNT).encode() in original or b'TianmingERP' in original:
        raise ValueError('已有ERP自动挂载定义，不能覆盖或接管')
    lines = original.splitlines(keepends=True)
    index = next((i for i, line in enumerate(lines) if line.lstrip().startswith(b'+')), len(lines))
    lines.insert(index, master_line())
    result = b''.join(lines)
    if len(result) > 65536: raise ValueError('增加ERP定义后auto_master过长')
    return result


def create(context_sha, address, export, original_master):
    value = {'type': TYPE, 'context_sha256': context_sha, 'mountpoint': str(MOUNT),
             'destination': str(DESTINATION), 'source': endpoint(address, export),
             'server_address': address, 'export': export, 'version': '4.0', 'port': 2049,
             'security': 'sys', 'master_sha256': digest(append_master(original_master))}
    value['map_sha256'] = digest(map_bytes(value))
    validate(value, context_sha)
    return value


def validate(value, context_sha):
    if (not isinstance(value, dict) or set(value) != FIELDS or value['type'] != TYPE
            or value['context_sha256'] != context_sha or not sha(context_sha)
            or value['mountpoint'] != str(MOUNT) or value['destination'] != str(DESTINATION)
            or value['version'] != '4.0' or type(value['port']) is not int or value['port'] != 2049
            or value['security'] != 'sys' or any(not sha(value[k]) for k in ('map_sha256', 'master_sha256'))
            or value['source'] != endpoint(value['server_address'], value['export'])
            or value['map_sha256'] != digest(map_bytes(value))):
        raise ValueError('系统自动挂载合同不完整或不属于固定本机路径')
    return value


def installed(value, read):
    """Caller supplies a protected root record reader; no autofs path opened."""
    master, mapping = read(MASTER), read(MAP)
    if (digest(master) != value['master_sha256'] or digest(mapping) != value['map_sha256']
            or mapping != map_bytes(value) or master.splitlines(keepends=True).count(master_line()) != 1):
        raise ValueError('系统自动挂载定义与批准合同不同')


def trigger(kernel):
    return (isinstance(kernel, dict) and set(kernel) == {'fsid', 'filesystem', 'mountpoint', 'source', 'owner', 'flags', 'subtype'}
        and all(type(kernel[k]) is int and kernel[k] >= 0 for k in ('owner', 'flags', 'subtype'))
        and kernel.get('filesystem') == 'autofs'
        and kernel.get('mountpoint') == str(MOUNT) and kernel.get('source') == 'map '+str(MAP)
        and type(kernel.get('owner')) is int and kernel['owner'] == 0
        and isinstance(kernel.get('fsid'), list) and len(kernel['fsid']) == 2
        and all(type(v) is int for v in kernel['fsid']))


def matches(value, observation):
    """Pure metadata check shared by root IPC and dedicated runtime consumers."""
    if (not isinstance(observation, dict) or set(observation) != {'classification', 'kernel', 'remote', 'autofs'}
            or observation['classification'] != 'direct_network_unbound' or not trigger(observation['autofs'])):
        return False
    kernel, remote = observation['kernel'], observation['remote']
    if (not isinstance(kernel, dict) or set(kernel) != {'fsid','filesystem','mountpoint','source','owner','flags','subtype'}
            or not isinstance(remote, dict) or set(remote) != {'addresses','export','server','version','port','security','tcp','semantics'}
            or any(type(kernel[k]) is not int or kernel[k] < 0 for k in ('owner','flags','subtype'))
            or kernel['flags'] & (0x1000 | 0x1 | 0x40) or kernel['flags'] & 0x18 != 0x18
            or not isinstance(remote['server'], str) or not remote['server'] or type(remote['port']) is not int
            or remote['semantics'] != {'hard':True,'nointr':True,'resvport':True}
            or any(type(v) is not bool for v in remote['semantics'].values())):
        return False
    return (kernel['mountpoint'] == value['mountpoint'] and kernel['filesystem'] == 'nfs'
        and kernel['source'] == value['source'] and kernel['owner'] == 0
        and isinstance(kernel['fsid'], list) and len(kernel['fsid']) == 2 and all(type(v) is int for v in kernel['fsid'])
        and remote['addresses'] == [value['server_address']] and remote['export'] == value['export']
        and remote['version'] == value['version'] and remote['port'] == value['port']
        and remote['security'] == value['security'] and remote['tcp'] is True)


def read_configuration(path):
    """Bounded same-FD read under held root parents; never access an NFS path."""
    from contextlib import ExitStack
    from desktop_assistant.mac_keychain_install import no_acl
    if path not in (MASTER, MAP): raise ValueError('不是固定系统挂载配置')
    device = Path('/System/Volumes/Data').stat().st_dev
    def fingerprint(info):
        return tuple(getattr(info, 'st_'+k) for k in
            ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink', 'size', 'mtime_ns', 'ctime_ns'))
    with ExitStack() as stack:
        chain = []; parent = None
        for part in ('/', *path.parts[1:]):
            directory = part != path.name
            fd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                         (os.O_DIRECTORY if directory else 0), dir_fd=parent)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if (info.st_uid != 0 or info.st_mode & 0o022 or
                    not (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode)
                    or (not directory and (stat.S_IMODE(info.st_mode) != 0o644 or info.st_nlink != 1
                        or not 0 < info.st_size <= 65536))
                    or (part != '/' and info.st_dev != device)):
                raise ValueError('系统挂载配置不受本机root保护')
            no_acl(fd)
            chain.append((fd, parent, part, fingerprint(info)))
            parent = fd
        chunks = []; remaining = info.st_size
        while remaining:
            block = os.read(fd, remaining)
            if not block: raise ValueError('系统挂载配置读取不完整')
            chunks.append(block); remaining -= len(block)
        if os.read(fd, 1): raise ValueError('系统挂载配置读取期间增长')
        for handle, parent, part, initial in chain:
            no_acl(handle)
            if (fingerprint(os.fstat(handle)) != initial or
                    fingerprint(os.stat(part, dir_fd=parent, follow_symlinks=False)) != initial):
                raise ValueError('系统挂载配置或父目录读取期间变化')
        return b''.join(chunks)
