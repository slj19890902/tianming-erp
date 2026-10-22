"""Fixed, task-scoped export protocol. Standard library only; no native actions.

This is deliberately not a general database download or privileged file copier.
The recipient, task, roots and credential policy are code-reviewed constants.
"""
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import stat
import time

TASK = 'DELIVERY-FULFILLMENT-FACTS-20261022'
CAPABILITY = 'controlled_uat_export_20261022_v1'
BASE = Path('/Library/TianmingERP')
ROOT = BASE / 'instance'
RECIPIENT_UID = 501
RECIPIENT_NAME = 'kimiyf'
RECIPIENT_HOME = Path('/Users/kimiyf')
OUTPUT = RECIPIENT_HOME / 'ERP-Migration/isolated-sources/delivery-fulfillment-20261022'
POLICY = 'tianming.uat-credentials.v1'
PLAN_TYPE = 'tianming.controlled-uat-export-plan.v1'
RECEIPT_TYPE = 'tianming.controlled-uat-export-receipt.v1'
ACTIONS = frozenset({'inspect-uat', 'export-uat'})
FILES = frozenset({'desktop_assistant/uat_export_contract.py',
    'desktop_assistant/uat_export.py', 'desktop_assistant/uat_export_sanitize.py',
    'desktop_assistant/mac_uat_export.py', 'desktop_assistant/mac_service_manage.py',
    'desktop_assistant/service_manage_entry.py'})
REQUEST_FIELDS = {'action', 'package_sha256', 'challenge', 'context_sha256',
                  'publisher_sha256', 'registry_sha256'}
SOURCE_FIELDS = {'application_package_sha256', 'application_git_sha', 'revision',
                 'state_sha256', 'process_sha256', 'environment_sha256', 'map_relative',
                 'pid', 'process_created'}
PLAN_FIELDS = {'type', 'task', 'attempt_id', 'created_at', 'expires_at', 'code_manifest_sha256',
    'package_sha256', 'context_sha256', 'publisher_sha256', 'registry_sha256',
    'supervisor_package_sha256', 'control_sha256', 'plist_sha256', 'recipient',
    'source', 'output', 'policy'}
MAX_BYTES = 8 * 1024**3
MAX_FILES = 20000
MAX_MANIFEST = 16 * 1024**2
ORIGINAL_ROOTS = ('data/private_uploads/', 'data/pdf_training_samples/',
                  'data/invoice_attachments/', 'data/supplier_invoice_attachments/', 'legacy_uploads/')
HOME_DENY_DELETE_ACL = b'!#acl 1\ngroup:ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000000C:everyone:12:deny:delete\n'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(raw): return hashlib.sha256(raw).hexdigest()


def sha(value): return isinstance(value, str) and bool(re.fullmatch('[0-9a-f]{64}', value))


def attempt(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('导出任务编号无效')
    return value


def output(attempt_id): return OUTPUT / attempt(attempt_id)


def staging(attempt_id): return ROOT / 'staging' / ('uat-export-' + attempt(attempt_id))


def plan_path(attempt_id): return BASE / ('uat-export-plan-' + attempt(attempt_id) + '.json')


def relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or value != PurePosixPath(value).as_posix() or value.startswith('/')
            or any(p in {'.', '..'} for p in value.split('/'))):
        raise ValueError('导出相对文件名无效')
    return value


def original_relative(value):
    relative(value)
    if not value.startswith(ORIGINAL_ROOTS): raise ValueError('不属于批准的业务原件目录')
    return value


def home_acl(fd):
    """Permit only the fixed macOS home deny-delete ACL; never alter it."""
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    libc.acl_get_fd_np.argtypes=[ctypes.c_int,ctypes.c_int]; libc.acl_get_fd_np.restype=ctypes.c_void_p
    libc.acl_to_text.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ssize_t)]; libc.acl_to_text.restype=ctypes.c_void_p
    libc.acl_free.argtypes=[ctypes.c_void_p]
    acl = libc.acl_get_fd_np(fd,0x100)
    if not acl: raise ValueError('无法读取固定接收用户主目录 ACL')
    try:
        size=ctypes.c_ssize_t(); text=libc.acl_to_text(acl,ctypes.byref(size))
        if not text: raise ValueError('无法核对固定接收用户主目录 ACL')
        try:
            if not 0 < size.value <= 1024: raise ValueError('接收用户主目录 ACL 大小无效')
            raw=ctypes.string_at(text,size.value)
        finally: libc.acl_free(text)
    finally: libc.acl_free(acl)
    if raw != HOME_DENY_DELETE_ACL: raise ValueError('固定接收用户主目录 ACL 不符合已审查合同')
    return digest(raw)


def recipient(*, check_home_acl=True):
    user = pwd.getpwuid(RECIPIENT_UID)
    if (user.pw_name != RECIPIENT_NAME or Path(user.pw_dir) != RECIPIENT_HOME
            or os.stat('/dev/console', follow_symlinks=False).st_uid != RECIPIENT_UID):
        raise ValueError('当前控制台不是本任务固定接收身份')
    acl_sha=digest(HOME_DENY_DELETE_ACL)
    if check_home_acl:
        fd=os.open(RECIPIENT_HOME,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: acl_sha=home_acl(fd)
        finally: os.close(fd)
    return {'uid': RECIPIENT_UID, 'gid': user.pw_gid, 'name': RECIPIENT_NAME,
            'home': str(RECIPIENT_HOME), 'home_acl_sha256':acl_sha}


def require_capability(manifest):
    value = manifest.get('runtime_capabilities', {}).get(CAPABILITY)
    if type(value) is not int or value != 1 or not FILES <= manifest.get('files', {}).keys():
        raise ValueError('签名程序没有完整固定 UAT 导出协议')


def validate_source(source):
    if (not isinstance(source, dict) or set(source) != SOURCE_FIELDS
            or any(not sha(source[k]) for k in SOURCE_FIELDS if k.endswith('_sha256'))
            or not isinstance(source['application_git_sha'], str)
            or not re.fullmatch('[0-9a-f]{40}', source['application_git_sha'])
            or not isinstance(source['revision'], str) or not re.fullmatch('[A-Za-z0-9_]{1,64}', source['revision'])
            or type(source['pid']) is not int or source['pid'] <= 1
            or type(source['process_created']) not in (int, float) or not source['process_created'] > 0):
        raise ValueError('导出来源身份无效')
    relative(source['map_relative'])
    if not source['map_relative'].startswith('data/layout_runtime/'):
        raise ValueError('仅支持固定受管运行地图目录')


def validate_plan(plan, *, approved_sha=None, check_home_acl=True):
    if (not isinstance(plan, dict) or set(plan) != PLAN_FIELDS or plan['type'] != PLAN_TYPE
            or plan['task'] != TASK or plan['policy'] != POLICY
            or any(not sha(plan[k]) for k in PLAN_FIELDS if k.endswith('_sha256'))
            or any(type(plan[k]) is not int for k in ('created_at', 'expires_at'))
            or not 0 < plan['expires_at'] - plan['created_at'] <= 3600
            or not plan['created_at'] <= time.time() < plan['expires_at']):
        raise ValueError('固定导出计划无效或过期')
    if plan['output'] != str(output(plan['attempt_id'])) or plan['recipient'] != recipient(check_home_acl=check_home_acl):
        raise ValueError('导出目的地或当前接收身份变化')
    validate_source(plan['source'])
    if approved_sha is not None and (not sha(approved_sha) or digest(canonical(plan)) != approved_sha):
        raise ValueError('导出批准摘要不符')
    return plan


def validate_request(request):
    if not isinstance(request, dict) or request.get('action') not in ACTIONS:
        raise ValueError('固定导出请求无效')
    fields = REQUEST_FIELDS | ({'plan', 'plan_sha256'} if request['action'] == 'export-uat' else set())
    if (set(request) != fields or any(not sha(request[k]) for k in fields if k.endswith('_sha256'))
            or not re.fullmatch('[0-9a-f]{32}', str(request.get('challenge', '')))):
        raise ValueError('固定导出请求字段无效')
    if request['action'] == 'export-uat':
        # The worker cannot traverse the private console user's home. The root
        # broker validates its exact ACL; this side binds the protected plan.
        plan = validate_plan(request['plan'], approved_sha=request['plan_sha256'], check_home_acl=False)
        if any(plan[k] != request[k] for k in REQUEST_FIELDS - {'action', 'challenge'}):
            raise ValueError('导出请求不属于批准程序或身份')


def stamp(info, *, content=True):
    names = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink')
    if content: names += ('st_size', 'st_mtime_ns', 'st_ctime_ns')
    return tuple(getattr(info, key) for key in names)


def dir_stamp(info):
    # Adding our own child directories legitimately changes a parent's nlink.
    return tuple(getattr(info,key) for key in ('st_dev','st_ino','st_uid','st_gid','st_mode'))


@contextmanager
def regular(path, *, uid, limit=MAX_BYTES, mutable=False):
    """No links, devices, FIFOs or hardlinks. Also check every named parent."""
    path = Path(path)
    parent_fds = []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        parent_fds.append((Path('/'), fd, dir_stamp(os.fstat(fd))))
        current = Path('/')
        for name in path.parent.parts[1:]:
            current /= name
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            parent_fds.append((current, fd, dir_stamp(os.fstat(fd))))
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(file_fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_nlink != 1
                    or before.st_size > limit or before.st_mode & 0o022):
                raise ValueError('导出源文件保护或大小无效')
            from desktop_assistant.mac_keychain_install import no_acl
            no_acl(file_fd)
            yield file_fd, before
            if (stamp(os.fstat(file_fd), content=not mutable) != stamp(before, content=not mutable)
                    or stamp(path.stat(follow_symlinks=False), content=not mutable) != stamp(before, content=not mutable)):
                raise ValueError('导出文件在读取期间变化')
            for named, opened, initial in parent_fds:
                if (dir_stamp(os.fstat(opened)) != initial
                        or dir_stamp(named.stat(follow_symlinks=False)) != initial):
                    raise ValueError('导出目录在读取期间被替换')
        finally: os.close(file_fd)
    finally:
        for _, opened, _ in reversed(parent_fds): os.close(opened)


def read(path, *, uid, limit=MAX_MANIFEST):
    with regular(path, uid=uid, limit=limit) as (fd, _):
        chunks = []; total = 0
        while True:
            data = os.read(fd, min(1024**2, limit + 1))
            if not data: break
            total += len(data)
            if total > limit: raise ValueError('导出记录过长')
            chunks.append(data)
        raw = b''.join(chunks)
        if len(raw) > limit: raise ValueError('导出记录过长')
        return raw
