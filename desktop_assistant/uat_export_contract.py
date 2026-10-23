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
CAPABILITY = 'controlled_uat_export_20261023_v2'
BASE = Path('/Library/TianmingERP')
ROOT = BASE / 'instance'
RECIPIENT_UID = 501
RECIPIENT_NAME = 'kimiyf'
RECIPIENT_HOME = Path('/Users/kimiyf')
OUTPUT = RECIPIENT_HOME / 'ERP-Migration/isolated-sources/delivery-fulfillment-20261022'
POLICY = 'tianming.uat-credentials.v2'
ENVIRONMENT_POLICY = 'tianming.uat-environment-credentials.v1'
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
ENVIRONMENT_PATHS = frozenset({'ERP_FILE_STORAGE_DIR', 'ERP_LEGACY_UPLOAD_DIR',
    'ERP_PDF_TRAINING_DIR', 'ERP_INVOICE_ATTACHMENT_DIR',
    'ERP_SUPPLIER_INVOICE_ATTACHMENT_DIR', 'ERP_TWIN_LAYOUT_RUNTIME_PATH'})
INLINE_CREDENTIALS = frozenset({'ERP_SECRET_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY'})
CREDENTIAL_REFERENCES = frozenset({'ERP_SECRET_KEY_FILE'})
# These names have concrete non-credential roles in config.py, import_existing,
# gui network settings and Manager. Unknown settings are not guessed safe.
IGNORED_ENVIRONMENT_FIELDS = frozenset({'ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS',
    'ERP_ALLOWED_ORIGINS', 'ERP_BACKUP_DIR', 'ERP_BIND_HOST', 'ERP_BROWSER_URL',
    'ERP_DATABASE_PATH', 'ERP_ENVIRONMENT', 'ERP_HEALTH_URL', 'ERP_LAN_HTTP_ORIGIN',
    'ERP_PORT', 'ERP_PRODUCTION_TRANSPORT', 'ERP_SESSION_COOKIE_NAME',
    'ERP_SESSION_COOKIE_SECURE', 'ERP_SESSION_EXPIRE_MINUTES', 'ERP_SQLITE_BUSY_TIMEOUT_MS',
    'ERP_TRUSTED_HOSTS', 'ERP_TRUSTED_PROXY_IPS', 'ERP_WORKERS',
    'ERP_AI_INVENTORY_MODEL', 'ERP_AI_INVENTORY_PROVIDER', 'ERP_HOME_REHEARSAL',
    'ERP_FACTORY_ACTIVATION_SHA256', 'ERP_FACTORY_PACKAGE_SHA256'})


def _environment(raw):
    """Private parse only. Never return this dictionary in a plan or receipt."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 65536:
        raise ValueError('运行配置长度不符合固定合同')
    def unique(pairs):
        result = {}
        folded = set()
        for name, value in pairs:
            if name.upper() in folded:
                raise ValueError('运行配置有重复或歧义字段')
            folded.add(name.upper())
            result[name] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('运行配置数值无效')))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('运行配置格式无效') from None
    if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
        raise ValueError('运行配置必须为字符串字典')
    for name in value:
        if name not in ENVIRONMENT_PATHS | CREDENTIAL_REFERENCES | IGNORED_ENVIRONMENT_FIELDS and name.upper() not in INLINE_CREDENTIALS:
            raise ValueError('运行配置含未审阅的字段')
    reference = value.get('ERP_SECRET_KEY_FILE')
    if reference is not None and reference.replace('${SHARED}', str(ROOT/'shared')) != str(ROOT/'shared/data/session_secret.key'):
        # This exact file is outside every original root and map directory.
        # A credential reference cannot become an indirectly copied original.
        raise ValueError('凭据文件引用不属于固定非导出位置')
    try:
        for item in value.values(): item.encode('utf-8')
    except UnicodeError:
        raise ValueError('运行配置文本编码无效') from None
    return value


def environment_paths(raw):
    """Only six approved path settings survive; no credential is projected."""
    return {k: v for k, v in _environment(raw).items() if k in ENVIRONMENT_PATHS}


def environment_credentials(raw):
    """Known inline values remain private in worker/root memory for rejection scans.

    This never follows secret-file references or reads a credential store.
    No caller may serialize the result or pass it to a recipient-owned process.
    """
    values = set()
    for key, value in _environment(raw).items():
        if key.upper() in INLINE_CREDENTIALS:
            # ERP config strips its inline key. Reject both stored and effective
            # forms; no value is emitted merely to explain a mismatch.
            expanded = value.replace('${SHARED}', str(ROOT/'shared'))
            values.update(v for v in (value, value.strip(), expanded, expanded.strip()) if v)
    if sum(len(v.encode('utf-8')) for v in values) > 65536:
        raise ValueError('运行凭据派生匹配范围超过固定上限')
    return tuple(sorted(values))


def reject_environment_credentials(raw, payload):
    """Check a bounded public status/plan before it crosses the UID boundary."""
    reject_credential_json(environment_credentials(raw), payload)


def reject_credential_json(values, payload):
    """Check raw and decoded JSON strings/keys, without reporting their content."""
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_MANIFEST:
        raise ValueError('导出JSON核对长度无效')
    if any(value.encode('utf-8') in payload for value in values):
        raise ValueError('导出元信息残留已知内联凭据')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('导出JSON包含重复字段')
            result[key] = value
        return result
    try:
        decoded = json.loads(payload, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('导出JSON数值无效')))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('导出JSON核对格式无效') from None
    pending = [decoded]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and any(value in item for value in values):
            raise ValueError('导出元信息残留已知内联凭据')
        if isinstance(item, dict): pending.extend(item.keys()); pending.extend(item.values())
        elif isinstance(item, list): pending.extend(item)


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
                    or stamp(path.lstat(), content=not mutable) != stamp(before, content=not mutable)):
                raise ValueError('导出文件在读取期间变化')
            for named, opened, initial in parent_fds:
                if (dir_stamp(os.fstat(opened)) != initial
                        or dir_stamp(named.lstat()) != initial):
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
