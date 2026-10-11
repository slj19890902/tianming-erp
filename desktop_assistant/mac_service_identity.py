"""Standalone, create-only Mac service identity provisioning (stdlib only).

Plan is read-only. Provision requires an administrator-run process and a reviewed
plan digest. Existing accounts/directories are never adopted or erased. Explicit
completion accepts only the exact retained post-creation/pre-home checkpoint.
This neither creates keychain items nor installs/starts an ERP service.
"""
from contextlib import contextmanager
import ctypes as C
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import stat
import subprocess
import sys
import time
import uuid

ACCOUNT = '_tianmingerp'
BASE = Path('/Library/TianmingERP')
ATTR = 'dsAttrTypeStandard:'
TYPE = 'tianming.mac-service-identity-plan.v1'
LIMIT = 65536
# Reviewed predecessor that created the retained 2026-10-11 identity records.
# This permits provenance checks and explicit completion, never fresh creation
# with an old program digest or adoption of a different account.
PREVIOUS_INSTALLERS = frozenset({'4ee4cda5f1e7d306bea0a73690b3c0df478fde75771e48c1ee12e10c9a1541b9'})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _platform():
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise ValueError('系统服务身份配置仅支持Mac arm64')


def _administrator():
    _platform()
    if os.getuid() != 0 or os.geteuid() != 0:
        raise ValueError('此步骤需要管理员在本机运行；不尝试获取或绕过管理员密码')
    _trusted_installer()


def _code_sha():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _host():
    _platform()
    result = subprocess.run(['/usr/sbin/ioreg', '-a', '-r', '-d', '1', '-c', 'IOPlatformExpertDevice'],
                            capture_output=True, check=True, timeout=10)
    if len(result.stdout) > 1024 * 1024:
        raise ValueError('硬件身份返回超限')
    tree = plistlib.loads(result.stdout)
    if not isinstance(tree, list) or len(tree) != 1:
        raise ValueError('硬件身份不唯一')
    value = uuid.UUID(tree[0]['IOPlatformUUID']).hex.encode('ascii')
    return hashlib.sha256(b'TIANMING-SERVICE-PROVISION\x00' + value).hexdigest()


class Directory:
    """Local OpenDirectory create/read only; no set/delete/credentials API."""

    def __init__(self):
        _platform()
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.od = C.CDLL('/System/Library/Frameworks/OpenDirectory.framework/OpenDirectory')
        p = C.c_void_p
        declarations = [
            (self.cf, 'CFDataCreate', p, [p, p, C.c_long]),
            (self.cf, 'CFDataGetLength', C.c_long, [p]),
            (self.cf, 'CFDataGetBytePtr', p, [p]),
            (self.cf, 'CFPropertyListCreateWithData', p, [p, p, C.c_ulong, p, C.POINTER(p)]),
            (self.cf, 'CFPropertyListCreateData', p, [p, p, C.c_long, C.c_ulong, C.POINTER(p)]),
            (self.cf, 'CFRelease', None, [p]),
            (self.cf, 'CFErrorGetCode', C.c_long, [p]),
            (self.od, 'ODNodeCreateWithName', p, [p, p, p, C.POINTER(p)]),
            (self.od, 'ODNodeCopyRecord', p, [p, p, p, p, C.POINTER(p)]),
            (self.od, 'ODRecordCopyDetails', p, [p, p, C.POINTER(p)]),
            (self.od, 'ODNodeCreateRecord', p, [p, p, p, p, C.POINTER(p)]),
        ]
        for library, name, result, args in declarations:
            function = getattr(library, name)
            function.restype, function.argtypes = result, args

    @contextmanager
    def owned(self, ref):
        if not ref:
            raise ValueError('目录服务未返回有效对象')
        try:
            yield ref
        finally:
            self.cf.CFRelease(ref)

    def call(self, function, *args, missing=False):
        error = C.c_void_p()
        result = function(*args, C.byref(error))
        if error.value:
            code = self.cf.CFErrorGetCode(error)
            self.cf.CFRelease(error)
            if result:
                self.cf.CFRelease(result)
            # The public API documents NULL + no error for a missing record.
            # Other errors are not interpreted as permission to create.
            raise ValueError('目录服务操作失败（code %s），保留现状' % code)
        if not result and not missing:
            raise ValueError('目录服务未确认成功，保留现状')
        return result

    @contextmanager
    def value(self, value):
        raw = plistlib.dumps(value)
        with self.owned(self.cf.CFDataCreate(None, raw, len(raw))) as data:
            with self.owned(self.call(self.cf.CFPropertyListCreateWithData, None, data, 0, None)) as ref:
                yield ref

    @contextmanager
    def node(self):
        with self.value('/Local/Default') as name:
            session = C.c_void_p.in_dll(self.od, 'kODSessionDefault').value
            with self.owned(self.call(self.od.ODNodeCreateWithName, None, session, name)) as node:
                yield node

    def read(self, kind, name, attributes):
        if kind not in {'Users', 'Groups'} or not attributes:
            raise ValueError('目录查询范围无效')
        with self.node() as node, self.value('dsRecTypeStandard:' + kind) as record_type, \
                self.value(name) as record_name, self.value(attributes) as attrs:
            record = self.call(self.od.ODNodeCopyRecord, node, record_type, record_name, attrs, missing=True)
            if record is None:
                return None
            with self.owned(record), self.owned(self.call(self.od.ODRecordCopyDetails, record, attrs)) as details:
                with self.owned(self.call(self.cf.CFPropertyListCreateData, None, details, 100, 0)) as data:
                    size = self.cf.CFDataGetLength(data)
                    if not 0 < size <= LIMIT:
                        raise ValueError('目录记录超限')
                    return plistlib.loads(C.string_at(self.cf.CFDataGetBytePtr(data), size))

    def create(self, kind, attributes):
        _administrator()
        if kind not in {'Users', 'Groups'}:
            raise ValueError('不支持的专用身份记录')
        # ODNodeCreateRecord fails atomically if this record already exists.
        # Attribute failure is handled by the OS; no explicit rollback/delete.
        with self.node() as node, self.value('dsRecTypeStandard:' + kind) as record_type, \
                self.value(ACCOUNT) as name, self.value(attributes) as attrs:
            with self.owned(self.call(self.od.ODNodeCreateRecord, node, record_type, name, attrs)):
                pass


def _ids():
    import pwd
    import grp
    return ({p.pw_uid for p in pwd.getpwall()}, {g.gr_gid for g in grp.getgrall()})


def _no_acl(fd):
    libc = C.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    libc.acl_get_fd_np.argtypes, libc.acl_get_fd_np.restype = [C.c_int, C.c_int], C.c_void_p
    libc.acl_get_entry.argtypes, libc.acl_get_entry.restype = [C.c_void_p, C.c_int, C.POINTER(C.c_void_p)], C.c_int
    libc.acl_valid.argtypes, libc.acl_valid.restype = [C.c_void_p], C.c_int
    libc.acl_free.argtypes, libc.acl_free.restype = [C.c_void_p], C.c_int
    C.set_errno(0)
    acl = libc.acl_get_fd_np(fd, 0x100)
    if not acl:
        if C.get_errno() == errno.ENOENT:
            return
        raise ValueError('无法核验安装父目录ACL')
    try:
        if libc.acl_valid(acl) != 0:
            raise ValueError('安装父目录ACL无效')
        entry = C.c_void_p()
        C.set_errno(0)
        status = libc.acl_get_entry(acl, 0, C.byref(entry))
        if status != -1 or C.get_errno() != errno.EINVAL:
            raise ValueError('安装父目录含ACL或无法核验，不修改原权限')
    finally:
        libc.acl_free(acl)


def _trusted_installer():
    """Execution location check, not a replacement for independent root SHA review.

    The administrator must first copy and hash the reviewed standalone source
    inside a new root-protected staging directory, using trusted system tools.
    Never elevate a script left writable in the developer's candidate tree.
    """
    source = Path(__file__).absolute()
    for index, path in enumerate((source, *source.parents)):
        flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if index else 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (info.st_uid != 0 or info.st_mode & 0o022
                    or (index == 0 and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))):
                raise ValueError('管理员须先独立核对源码摘要，并在root保护的无链接目录执行')
            _no_acl(fd)
        finally:
            os.close(fd)


def _parent():
    # Fixed /Library parent, no /var or caller-chosen paths/aliases.
    for path in (BASE.parent, *BASE.parent.parents):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISDIR(info.st_mode):
                raise ValueError('安装祖先目录必须由root保护，不修改原权限')
            _no_acl(fd)
        finally:
            os.close(fd)
    if BASE.parent.stat().st_dev != Path('/System/Volumes/Data').stat().st_dev:
        raise ValueError('服务安装必须使用本机数据盘')


def _absent(directory):
    if BASE.exists() or BASE.is_symlink():
        raise ValueError('服务根目录已存在，保留现场，不覆盖或接管')
    for kind in ('Users', 'Groups'):
        if directory.read(kind, ACCOUNT, [ATTR + 'RecordName']) is not None:
            raise ValueError('同名用户或组已存在，不接管或修改')
    import pwd
    import grp
    for lookup in (pwd.getpwnam, grp.getgrnam):
        try:
            lookup(ACCOUNT)
        except KeyError:
            continue
        raise ValueError('搜索域已有同名身份，不接管')


def plan():
    _platform()
    _parent()
    _absent(Directory())
    users, groups = _ids()
    available = sorted(set(range(400, 500)) - users - groups, reverse=True)
    if not available:
        raise ValueError('预留服务ID范围没有空闲值，需要管理员核对')
    now = int(time.time())
    return {'type': TYPE, 'account': ACCOUNT, 'base': str(BASE), 'uid': available[0],
            'gid': available[0], 'user_guid': str(uuid.uuid4()).upper(),
            'group_guid': str(uuid.uuid4()).upper(), 'installation_id': uuid.uuid4().hex,
            'host_sha256': _host(), 'installer_sha256': _code_sha(),
            'created_at': now, 'expires_at': now + 7 * 86400}


def _validate(value, *, completing=False):
    fields = {'type', 'account', 'base', 'uid', 'gid', 'user_guid', 'group_guid',
              'installation_id', 'host_sha256', 'installer_sha256', 'created_at', 'expires_at'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('服务身份计划字段不完整')
    if value['type'] != TYPE or value['account'] != ACCOUNT or value['base'] != str(BASE):
        raise ValueError('计划不是固定的ERP专用身份/目录')
    if any(type(value[k]) is not int or not 400 <= value[k] < 500 for k in ('uid', 'gid')):
        raise ValueError('服务UID/GID不在允许范围')
    for key in ('user_guid', 'group_guid'):
        if str(uuid.UUID(value[key])).upper() != value[key]:
            raise ValueError('服务GUID不规范')
    if (uuid.UUID(hex=value['installation_id']).hex != value['installation_id']
            or value['user_guid'] == value['group_guid']):
        raise ValueError('安装身份不规范或重复')
    accepted = {_code_sha()} | (PREVIOUS_INSTALLERS if completing else set())
    if value['host_sha256'] != _host() or value['installer_sha256'] not in accepted:
        raise ValueError('本机或配置器与已批准计划不符')
    if (any(type(value[k]) is not int for k in ('created_at', 'expires_at'))
            or not 0 < value['expires_at'] - value['created_at'] <= 7 * 86400
            or not value['created_at'] <= time.time() < value['expires_at']):
        raise ValueError('服务身份计划已过期或未生效')


def _attributes(value):
    group = {'PrimaryGroupID': [str(value['gid'])], 'GeneratedUID': [value['group_guid']],
             'RealName': ['Tianming ERP service group'], 'Password': ['*']}
    user = {'UniqueID': [str(value['uid'])], 'PrimaryGroupID': [str(value['gid'])],
            'GeneratedUID': [value['user_guid']], 'RealName': ['Tianming ERP service'],
            'NFSHomeDirectory': [str(BASE / 'home')], 'UserShell': ['/usr/bin/false'],
            'Password': ['*'], 'AuthenticationAuthority': [';DisabledUser;']}
    return ({ATTR + k: v for k, v in group.items()}, {ATTR + k: v for k, v in user.items()})


def _write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != raw:
        raise ValueError('专用身份回执原字节回读失败')


def permitted_groups(gid):
    """Recognize only public inherited print/share groups; never grant membership.

    macOS expands localaccounts/everyone into default print/share groups even for
    disabled service accounts. A name or numeric allowlist alone is insufficient:
    read the local group topology and reject direct members and unfamiliar roles.
    """
    import grp
    error = '专用身份的实际UID/GID、组权限或目录不符，保留现场'
    if type(gid) is not int or not 400 <= gid < 500:
        raise ValueError(error)
    groups = os.getgrouplist(ACCOUNT, gid)
    if (not groups or len(groups) > 64
            or any(type(item) is not int or item < 0 for item in groups)):
        raise ValueError(error)
    allowed = {gid, 12, 61}
    extras = set(groups) - allowed
    if not extras:
        return allowed
    directory = Directory()
    listed = grp.getgrall()
    fields = [ATTR + key for key in ('RecordName', 'PrimaryGroupID', 'NestedGroups',
                                    'GroupMembership', 'GroupMembers')]
    for extra in extras:
        group = grp.getgrgid(extra)
        # macOS may enumerate the same built-in record through both local and
        # compatibility nodes. Only byte-equivalent identity tuples may repeat.
        identities = {(item.gr_name, item.gr_gid, tuple(item.gr_mem))
                      for item in listed if item.gr_gid == extra}
        if group.gr_mem or identities != {(group.gr_name, extra, ())}:
            raise ValueError(error)
        if extra == 100 and group.gr_name == '_lpoperator':
            nested = {'ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000003D',
                      'ABCDEFAB-CDEF-ABCD-EFAB-CDEF00000062'}
        elif extra >= 701 and re.fullmatch(r'com\.apple\.sharepoint\.group\.[1-9][0-9]*', group.gr_name):
            nested = {'ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000000C'}
        else:
            raise ValueError(error)
        record = directory.read('Groups', group.gr_name, fields)
        if (not record or record.get(ATTR + 'RecordName') != [group.gr_name]
                or record.get(ATTR + 'PrimaryGroupID') != [str(extra)]
                or record.get(ATTR + 'GroupMembership', []) != []
                or record.get(ATTR + 'GroupMembers', []) != []
                or not isinstance(record.get(ATTR + 'NestedGroups'), list)
                or len(record[ATTR + 'NestedGroups']) != len(nested)
                or set(record[ATTR + 'NestedGroups']) != nested):
            raise ValueError(error)
        allowed.add(extra)
    return allowed


def _check_identity(value):
    import pwd
    import grp
    user, group = pwd.getpwnam(ACCOUNT), grp.getgrnam(ACCOUNT)
    if (user.pw_uid != value['uid'] or user.pw_gid != value['gid']
            or user.pw_dir != str(BASE / 'home') or user.pw_shell != '/usr/bin/false'
            or group.gr_gid != value['gid'] or group.gr_mem
            or sum(p.pw_uid == value['uid'] for p in pwd.getpwall()) != 1
            or sum(g.gr_gid == value['gid'] for g in grp.getgrall()) != 1
            or not set(os.getgrouplist(ACCOUNT, value['gid'])) <= permitted_groups(value['gid'])):
        raise ValueError('专用身份的实际UID/GID、组权限或目录不符，保留现场')


def _owned_directory(path, uid, gid):
    path.mkdir(mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o700)
        info = os.fstat(fd)
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, 0o700):
            raise ValueError('服务专有目录回读不符，保留现场')
        _no_acl(fd)
    finally:
        os.close(fd)


def provision(raw, approved_sha256):
    _administrator()
    if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMIT or hashlib.sha256(raw).hexdigest() != approved_sha256:
        raise ValueError('服务身份计划原字节与批准摘要不符')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('服务身份计划含重复字段')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    _validate(value)
    _parent()
    directory = Directory()
    _absent(directory)
    users, groups = _ids()
    if value['uid'] in users or value['gid'] in groups:
        raise ValueError('计划UID或GID已被占用，不修改其他身份')
    group, user = _attributes(value)
    # Reserve the fixed root before any OD write. Early failures leave it 0700.
    # Late failures after traversal is enabled retain 0755, private children and
    # 0600 records; never erase or modify the retained scene to simulate rollback.
    BASE.mkdir(mode=0o700)
    _write(BASE / '00-plan.json', raw)
    for index, kind, attrs in ((1, 'Groups', group), (2, 'Users', user)):
        directory.create(kind, attrs)
        actual = directory.read(kind, ACCOUNT, list(attrs) + [ATTR + 'RecordName'])
        if (not actual or any(actual.get(k) != v for k, v in attrs.items())
                or actual.get(ATTR + 'RecordName') != [ACCOUNT]):
            raise ValueError('新建专用身份未通过原字段回读，保留现场')
        _write(BASE / ('0%d-%s.json' % (index, kind.lower())), canonical(actual))
    _check_identity(value)
    for name in ('home', 'instance'):
        _owned_directory(BASE / name, value['uid'], value['gid'])
    receipt = {'type': 'tianming.mac-service-identity.v1', 'plan_sha256': approved_sha256,
               'uid': value['uid'], 'gid': value['gid'], 'installation_id': value['installation_id'],
               'home': str(BASE / 'home'), 'root': str(BASE / 'instance'),
               'account_created': True, 'keychain_created': False, 'service_started': False,
               'factory_activated': False}
    # Allow traversal only after both disabled identity and private homes exist.
    BASE.chmod(0o755)
    _parent()
    _check_identity(value)
    _write(BASE / 'provisioned.json', canonical(receipt))
    return receipt


def complete_identity(raw, approved_sha256):
    """Complete only the exact retained post-OD/pre-home installation checkpoint.

    No account/group changes, old-record rewrites, deletion, generic repair or
    retry after a new partial completion. The original creation evidence stays.
    """
    _administrator()
    # The independently reviewed bootstrap authenticates the remaining pack.
    from desktop_assistant import mac_keychain_install as bootstrap
    bootstrap.verify_code()
    value = bootstrap.decode(raw)
    if hashlib.sha256(raw).hexdigest() != approved_sha256:
        raise ValueError('续接原计划摘要不符')
    _validate(value, completing=True)
    _parent()
    fd = os.open(BASE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_dev != BASE.parent.stat().st_dev):
            raise ValueError('续接现场不是原root私有目录')
        _no_acl(fd)
        names = {'00-plan.json', '01-groups.json', '02-users.json'}
        if set(os.listdir(fd)) != names:
            raise ValueError('续接仅接受三份原创建记录；已有其他现场时不覆盖重试')
        for name in names:
            item = os.stat(BASE / name, follow_symlinks=False)
            if (item.st_uid != 0 or item.st_gid != 0 or stat.S_IMODE(item.st_mode) != 0o600
                    or item.st_dev != info.st_dev):
                raise ValueError('原创建记录的保护属性不符')
        records = {name: bootstrap.protected(BASE / name) for name in names}
        if records['00-plan.json'] != raw:
            raise ValueError('续接不是原创建计划')
        directory = Directory()
        for index, kind, attrs in ((1, 'Groups', _attributes(value)[0]),
                                  (2, 'Users', _attributes(value)[1])):
            expected = {**attrs, ATTR + 'RecordName': [ACCOUNT]}
            if (records['0%d-%s.json' % (index, kind.lower())] != canonical(expected)
                    or directory.read(kind, ACCOUNT, list(expected)) != expected):
                raise ValueError('原身份创建记录或实际禁用账号字段不符，不接管')
        _check_identity(value)
        current = os.stat(BASE, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('续接目录身份变化')
        if any(bootstrap.protected(BASE / name) != data for name, data in records.items()):
            raise ValueError('续接原记录变化')
        _write(BASE / 'identity-completion-attempt.json', canonical({
            'type': 'tianming.mac-service-identity-completion-attempt.v1',
            'plan_sha256': approved_sha256, 'installer_sha256': _code_sha(),
            'original_installer_sha256': value['installer_sha256'],
            'original_records': {name: hashlib.sha256(data).hexdigest() for name, data in records.items()},
            'directory_groups': sorted(permitted_groups(value['gid'])),
            'account_modified': False, 'group_modified': False,
            'original_records_modified': False, 'factory_activated': False}))
        for name in ('home', 'instance'):
            _owned_directory(BASE / name, value['uid'], value['gid'])
        receipt = {'type': 'tianming.mac-service-identity.v1', 'plan_sha256': approved_sha256,
                   'uid': value['uid'], 'gid': value['gid'], 'installation_id': value['installation_id'],
                   'home': str(BASE / 'home'), 'root': str(BASE / 'instance'),
                   'account_created': True, 'keychain_created': False, 'service_started': False,
                   'factory_activated': False}
        os.fchmod(fd, 0o755)
        _parent()
        _check_identity(value)
        _write(BASE / 'provisioned.json', canonical(receipt))
        os.fsync(fd)
        return receipt
    finally:
        os.close(fd)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='ERP专用Mac服务身份配置；不安装/启动ERP或钥匙串')
    parser.add_argument('action', choices=('plan', 'provision', 'complete'))
    parser.add_argument('--file', type=Path, required=True)
    parser.add_argument('--approved-sha256')
    args = parser.parse_args()
    try:
        if args.action == 'plan':
            raw = canonical(plan())
            _write(args.file, raw)
            result = {'plan': str(args.file), 'sha256': hashlib.sha256(raw).hexdigest(), 'read_only': True}
        else:
            with args.file.open('rb') as stream:
                raw = stream.read(LIMIT + 1)
            if args.action == 'complete':
                root = str(Path(__file__).absolute().parent.parent)
                if root not in sys.path:
                    sys.path.insert(0, root)
                result = complete_identity(raw, args.approved_sha256)
            else:
                result = provision(raw, args.approved_sha256)
    except Exception:
        raise SystemExit('服务身份配置未确认完成；保留原账号及本次现场，不覆盖重试或自动清理。') from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
