"""Reviewed administrator bootstrap orchestration; never starts ERP.

Run only from the independently SHA-reviewed, root-protected minimal code pack.
Workers use a fresh isolated interpreter with an explicit UID/GID and no extra
groups. Their System *keychain* domain is not a system launchd session proof.
"""
import argparse
import ctypes as C
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

BASE = Path('/Library/TianmingERP')
PYTHON = Path('/usr/bin/python3')
TYPE = 'tianming.keychain-install-plan.v1'
LIMIT = 65536
FILES = ('app/__init__.py', 'app/core/__init__.py', 'app/core/mac_keychain.py',
         'app/core/mac_service_keychain.py', 'desktop_assistant/__init__.py',
         'desktop_assistant/mac_service_identity.py', 'desktop_assistant/mac_keychain_install.py',
         'desktop_assistant/mac_release_publish.py', 'desktop_assistant/mac_system_service.py',
         'desktop_assistant/mac_archive_contract.py', 'desktop_assistant/mac_archive_install.py',
         'desktop_assistant/mac_mount.py', 'desktop_assistant/factory_target_contract.py',
         'desktop_assistant/factory_network.py', 'desktop_assistant/mac_factory_request_install.py')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= LIMIT:
        raise ValueError('安装记录长度无效')
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('安装记录字段重复')
            result[key] = item
        return result
    def constant(_):
        raise ValueError('安装记录不能含非有限数字')
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError('安装记录必须为对象')
    return value


def code_root():
    return Path(__file__).absolute().parent.parent


def no_acl(fd):
    # Bootstrap copy: do not import code from the pack before authenticating it.
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
        raise ValueError('无法核验安装代码ACL')
    try:
        if libc.acl_valid(acl) != 0:
            raise ValueError('安装代码ACL无效')
        entry = C.c_void_p()
        C.set_errno(0)
        if libc.acl_get_entry(acl, 0, C.byref(entry)) != -1 or C.get_errno() != errno.EINVAL:
            raise ValueError('安装代码含ACL或无法核验')
    finally:
        libc.acl_free(acl)


def protected(path, *, directory=False, readable=False):
    # Kept stdlib-only: authenticate the installed pack before importing it.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0))
    try:
        info = os.fstat(fd)
        if (info.st_uid != 0 or info.st_mode & 0o022
                or (directory and not stat.S_ISDIR(info.st_mode))
                or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
                or (readable and not info.st_mode & (0o001 if directory else 0o004))):
            raise ValueError('安装代码或记录不受root保护，不能执行')
        no_acl(fd)
        raw = os.read(fd, LIMIT + 1) if not directory else b''
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_gid,
                current.st_size, current.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_mode,
                info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns) or len(raw) > LIMIT:
            raise ValueError('安装代码或记录读取期间变化或过长')
        return raw
    finally:
        os.close(fd)


def verify_code(*, trusted=True):
    root = code_root()
    if trusted:
        for directory in (root, *root.parents):
            protected(directory, directory=True, readable=True)
        raw = protected(root / 'install-manifest.json', readable=True)
    else:
        raw = (root / 'install-manifest.json').read_bytes()
    manifest = decode(raw)
    if (set(manifest) != {'type', 'source', 'files'} or manifest['type'] != 'tianming.keychain-code.v1'
            or not isinstance(manifest['source'], str) or len(manifest['source']) != 40
            or any(c not in '0123456789abcdef' for c in manifest['source'])
            or not isinstance(manifest['files'], dict)
            or set(manifest['files']) != set(FILES)):
        raise ValueError('不是已审阅的最小钥匙串代码包')
    actual = set()
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    raise ValueError('最小代码包不能含链接')
                if entry.is_dir(follow_symlinks=False):
                    if trusted:
                        protected(path, directory=True, readable=True)
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    actual.add(path.relative_to(root).as_posix())
                else:
                    raise ValueError('代码包有特殊对象')
    if actual != set(FILES) | {'install-manifest.json'}:
        raise ValueError('最小代码包文件清单不完整或有额外文件')
    for name in FILES:
        data = protected(root / name, readable=True) if trusted else (root / name).read_bytes()
        if digest(data) != manifest['files'][name]:
            raise ValueError('安装代码摘要不匹配')
    return digest(raw)


def modules():
    root = str(code_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    from desktop_assistant import mac_service_identity as identity
    from app.core import mac_service_keychain as keychain
    return identity, keychain


def _origin(value):
    identity, _ = modules()
    expected = {'type', 'account', 'base', 'uid', 'gid', 'user_guid', 'group_guid',
                'installation_id', 'host_sha256', 'installer_sha256', 'created_at', 'expires_at'}
    if (set(value) != expected or value['type'] != identity.TYPE or value['account'] != identity.ACCOUNT
            or value['base'] != str(BASE) or value['installer_sha256'] != identity._code_sha()
            or value['host_sha256'] != identity._host()
            or any(type(value[k]) is not int or not 400 <= value[k] < 500 for k in ('uid', 'gid'))
            or any(type(value[k]) is not int for k in ('created_at', 'expires_at'))
            or not 0 < value['expires_at'] - value['created_at'] <= 7 * 86400):
        raise ValueError('原专用身份计划不匹配')
    for key in ('user_guid', 'group_guid'):
        if str(uuid.UUID(value[key])).upper() != value[key]:
            raise ValueError('原身份GUID不规范')
    if (uuid.UUID(value['installation_id']).hex != value['installation_id']
            or value['user_guid'] == value['group_guid']):
        raise ValueError('原安装身份不规范')
    # Its time window authorized account creation in the past. The completed
    # root receipt proves that operation; this new plan has its own approval TTL.
    return value


def observer(uid):
    import pwd
    if type(uid) is not int or not 500 <= uid < 2**31:
        raise ValueError('负例必须是已存在的普通用户UID')
    user = pwd.getpwuid(uid)
    if user.pw_uid != uid or user.pw_name.startswith('_') or not Path(user.pw_dir).is_absolute():
        raise ValueError('负例不是普通用户身份')
    return {'uid': uid, 'gid': user.pw_gid, 'home': user.pw_dir, 'name': user.pw_name}


def make_plan(identity_raw, observer_uid):
    source_sha = verify_code(trusted=False)
    original = _origin(decode(identity_raw))
    _, keychain = modules()
    now = int(time.time())
    context = {'type': keychain.TYPE, **{key: original[key] for key in
               ('installation_id', 'uid', 'gid', 'host_sha256')}}
    return {'type': TYPE, 'attempt_id': uuid.uuid4().hex, 'identity_plan_sha256': digest(identity_raw),
            'context': context, 'observer': observer(observer_uid), 'code_manifest_sha256': source_sha,
            'created_at': now, 'expires_at': now + 7 * 86400}


def validate_plan(raw, approved_sha, *, trusted=True):
    if digest(raw) != approved_sha:
        raise ValueError('安装计划原字节与批准摘要不符')
    value = decode(raw)
    if (set(value) != {'type', 'attempt_id', 'identity_plan_sha256', 'context', 'observer',
                       'code_manifest_sha256', 'created_at', 'expires_at'} or value['type'] != TYPE
            or uuid.UUID(value['attempt_id']).hex != value['attempt_id']
            or any(type(value[k]) is not int for k in ('created_at', 'expires_at'))
            or not 0 < value['expires_at'] - value['created_at'] <= 7 * 86400
            or not value['created_at'] <= time.time() < value['expires_at']
            or value['code_manifest_sha256'] != verify_code(trusted=trusted)
            or value['observer'] != observer(value['observer']['uid'])):
        raise ValueError('安装计划不完整、过期或代码/负例身份已改变')
    return value


def _write(path, raw, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as output:
        os.fchmod(output.fileno(), mode)
        output.write(raw); output.flush(); os.fsync(output.fileno())
    if protected(path) != raw:
        raise ValueError('安装记录原字节回读失败，保留现场')


def preflight(value):
    identity, keychain = modules()
    identity._administrator()
    context = value['context']
    keychain._identity(context, administrator=True)
    origin_raw = protected(BASE / '00-plan.json')
    origin = _origin(decode(origin_raw))
    if digest(origin_raw) != value['identity_plan_sha256']:
        raise ValueError('实际账号不是计划绑定的创建记录')
    expected_context = {'type': keychain.TYPE, **{k: origin[k] for k in
                        ('installation_id', 'uid', 'gid', 'host_sha256')}}
    if context != expected_context:
        raise ValueError('钥匙串计划与实际账号创建记录不一致')
    receipt = decode(protected(BASE / 'provisioned.json'))
    if receipt != {'type': 'tianming.mac-service-identity.v1', 'plan_sha256': digest(origin_raw),
                   'uid': origin['uid'], 'gid': origin['gid'], 'installation_id': origin['installation_id'],
                   'home': str(BASE / 'home'), 'root': str(BASE / 'instance'), 'account_created': True,
                   'keychain_created': False, 'service_started': False, 'factory_activated': False}:
        raise ValueError('原账号配置尚未完成或回执不符')
    directory = identity.Directory()
    group, user = identity._attributes(origin)
    for kind, attrs in (('Groups', group), ('Users', user)):
        actual = directory.read(kind, identity.ACCOUNT, list(attrs) + [identity.ATTR + 'RecordName'])
        if actual != {**attrs, identity.ATTR + 'RecordName': [identity.ACCOUNT]}:
            raise ValueError('专用账号OD原字段已改变')
    for path in (BASE / 'home/Library', BASE / 'keychain-init-pending.json',
                 BASE / 'keychain-init-verified.json', keychain.CONTEXT):
        if path.exists() or path.is_symlink():
            raise ValueError('存在既有钥匙串初始化现场，不覆盖重试')
    with keychain._checked_path(BASE / 'home', uid=context['uid'], directory=True, mode=0o700):
        pass
    for path in (PYTHON, *PYTHON.parents):
        protected(path, directory=path != PYTHON, readable=True)


def _proof(secret, phase, context, challenge):
    _, keychain = modules()
    data = b'TIANMING-KEYCHAIN-INSTALL\0' + canonical([phase, keychain.binding(context), challenge])
    return hmac.new(secret, data, hashlib.sha256).hexdigest()


def kernel_groups():
    # Modern macOS os.getgroups() queries directory-service memberships, not
    # the credentials changed by Popen's setgroups. Bind the kernel interface.
    # XNU includes egid at index zero even after setgroups([]), setregid(gid,gid).
    libc = C.CDLL('/usr/lib/system/libsystem_kernel.dylib', use_errno=True)
    function = libc.getgroups
    function.argtypes, function.restype = [C.c_int, C.POINTER(C.c_uint32)], C.c_int
    count = function(0, None)
    if not 1 <= count <= 1024:
        raise ValueError('无法读取内核进程组')
    values = (C.c_uint32 * count)()
    if function(count, values) != count:
        raise ValueError('内核进程组读取期间变化')
    return list(values)


def _actual():
    return {'uid': os.getuid(), 'euid': os.geteuid(), 'gid': os.getgid(), 'egid': os.getegid(),
            'kernel_groups': kernel_groups()}


def _expected(uid, gid):
    return {'uid': uid, 'euid': uid, 'gid': gid, 'egid': gid, 'kernel_groups': [gid]}


def _worker(request):
    if set(request) != {'phase', 'context', 'identity', 'challenge'}:
        raise ValueError('子进程请求字段错误')
    verify_code()
    if _actual() != request['identity'] or os.getuid() == 0:
        raise ValueError('子进程没有降到指定独立身份')
    phase, context, challenge = request['phase'], request['context'], request['challenge']
    if not isinstance(challenge, str) or uuid.UUID(challenge).hex != challenge:
        raise ValueError('子进程挑战无效')
    result = {'phase': phase, 'identity': _actual(), 'challenge': challenge}
    identity, keychain = modules()
    if phase == 'preflight':
        # No keychain operations at all in this phase.
        result['code_verified'] = True
        return result
    api = keychain.Security()
    if phase == 'negative':
        # Deliberately bypass service _identity: this must test the native ACL.
        with api.system() as ref:
            try:
                api.get(ref, keychain.BOOTSTRAP_SERVICE, keychain.binding(context))
            except keychain.KeychainError as error:
                status = getattr(error, 'os_status', None)
                if status not in {-25293, -25308}:
                    raise ValueError('负例没有证明钥匙串权限拒绝') from None
                return {**result, 'denied': True, 'os_status': status}
        raise ValueError('其他UID意外读到系统引导秘密，不能发布完成记录')
    keychain._identity(context)
    if phase == 'initialize':
        keychain.initialize_private(context)
    elif phase == 'verify':
        import ctypes as C
        api.api.sec.SecKeychainLock.argtypes = [C.c_void_p]
        api.api.sec.SecKeychainLock.restype = C.c_int32
        # Only this newly provisioned private keychain is locked, never System.
        with keychain.unlocked(api, context) as ref:
            api.api.check(api.api.sec.SecKeychainLock(ref))
        with keychain.unlocked(api, context) as ref:
            for suffix in ('old', 'new'):
                account = challenge + ':' + suffix
                raw = ('synthetic-init-合成\0' + suffix).encode()
                api.put(ref, 'cn.tianming.erp.setup-check.v1', account, raw, context['uid'], context['gid'])
            for suffix in ('old', 'new'):
                if api.get(ref, 'cn.tianming.erp.setup-check.v1', challenge + ':' + suffix) != \
                        ('synthetic-init-合成\0' + suffix).encode():
                    raise ValueError('新服务进程读写轮换验证失败')
    else:
        raise ValueError('子进程阶段不受支持')
    secret = keychain._bootstrap(api, context)
    return {**result, 'proof': _proof(secret, phase, context, challenge)}


def run_child(value, phase, uid, gid, home):
    request = {'phase': phase, 'context': value['context'], 'identity': _expected(uid, gid),
               'challenge': uuid.uuid4().hex}
    verify_code()
    # No preexec_fn or in-process post-fork Python/CF calls; fresh exec each time.
    process = subprocess.Popen([str(PYTHON), '-I', '-B', '-S', str(Path(__file__).absolute()), '_worker'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        user=uid, group=gid, extra_groups=[], umask=0o077, close_fds=True, pass_fds=(),
        shell=False, cwd='/', env={'HOME': home, 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8'})
    # This installer never auto-kills a worker while it may be writing a keychain.
    # The invoking terminal/task must observe this exact process to completion.
    interrupted = False
    payload = canonical(request)
    while True:
        try:
            output, _ = process.communicate(payload)
            break
        except KeyboardInterrupt:
            # Do not abandon a potentially mutating child on Ctrl-C. Continue
            # draining/waiting for this exact process, then refuse completion.
            interrupted, payload = True, None
    if interrupted:
        raise ValueError('已等候被中断的子进程退出；保留现场，不发布完成记录')
    if process.returncode != 0:
        raise ValueError('初始化子进程未通过；保留现场，不输出底层秘密或覆盖重试')
    result = decode(output)
    if any(result.get(key) != request[key] for key in ('phase', 'identity', 'challenge')):
        raise ValueError('初始化子进程身份或挑战回读不符')
    fields = {'phase', 'identity', 'challenge'} | ({'code_verified'} if phase == 'preflight' else
              {'denied', 'os_status'} if phase == 'negative' else {'proof'})
    if set(result) != fields:
        raise ValueError('初始化子进程返回字段不符')
    if phase == 'preflight' and result['code_verified'] is not True:
        raise ValueError('子进程未确认代码可读')
    if phase == 'negative' and (result['denied'] is not True or result['os_status'] not in {-25293, -25308}):
        raise ValueError('子进程未证明其他UID被拒绝')
    return result


def install(raw, approved_sha):
    if os.getuid() != 0 or os.geteuid() != 0:
        raise ValueError('需管理员在本机执行；不请求或绕过密码')
    value = validate_plan(raw, approved_sha)
    preflight(value)
    identity, keychain = modules()
    context, other = value['context'], value['observer']
    service = (context['uid'], context['gid'], str(BASE / 'home'))
    for args in (service, (other['uid'], other['gid'], other['home'])):
        run_child(value, 'preflight', *args)
    validate_plan(raw, approved_sha)
    preflight(value)
    _write(BASE / 'keychain-init-pending.json', raw)
    for path in (BASE / 'home/Library', BASE / 'home/Library/Keychains'):
        identity._owned_directory(path, context['uid'], context['gid'])
    secret = os.urandom(32).hex().encode()
    keychain.create_bootstrap(context, secret)
    for phase in ('initialize', 'verify'):
        result = run_child(value, phase, *service)
        expected = _proof(secret, phase, context, result['challenge'])
        if not isinstance(result['proof'], str) or not hmac.compare_digest(result['proof'], expected):
            raise ValueError('子进程未证明读取本次原始系统引导秘密')
        validate_plan(raw, approved_sha)
    run_child(value, 'negative', other['uid'], other['gid'], other['home'])
    validate_plan(raw, approved_sha)
    keychain._identity(context, administrator=True)
    context_raw = canonical(context)
    receipt = {'type': 'tianming.keychain-initialization.v1', 'plan_sha256': approved_sha,
               'context_sha256': digest(context_raw), 'attempt_id': value['attempt_id'],
               'service_new_process_verified': True, 'system_keychain_acl_denial_verified': True,
               'launchd_system_verified': False, 'logout_reboot_verified': False, 'factory_activated': False}
    # Receipt is evidence of completed checks. Context, written last, enables
    # ordinary credentials; its presence must also be verified independently.
    _write(BASE / 'keychain-init-verified.json', canonical(receipt))
    _write(keychain.CONTEXT, context_raw, mode=0o644)
    return {**receipt, 'context_published': True}


def main():
    parser = argparse.ArgumentParser(description='专用服务身份、程序和系统监护安装；工厂接管另行授权')
    parser.add_argument('action', choices=('plan', 'install', '_worker', 'publish', '_publish_worker',
                                         'service-plan', 'service-apply', 'archive-plan', 'archive-apply', 'manage', 'factory-request'))
    parser.add_argument('--identity-plan', type=Path)
    parser.add_argument('--observer-uid', type=int)
    parser.add_argument('--file', type=Path)
    parser.add_argument('--approved-sha256')
    parser.add_argument('--package', type=Path)
    parser.add_argument('--service-action', choices=('install', 'disable', 'enable'))
    parser.add_argument('--package-sha256')
    parser.add_argument('--nas-address')
    parser.add_argument('--nas-export')
    parser.add_argument('--management-action', choices=('status','configure-backup','backup','start','stop'))
    parser.add_argument('--network-file', type=Path)
    parser.add_argument('--network-sha256')
    args = parser.parse_args()
    try:
        if args.action == 'factory-request':
            verify_code(); modules()
            from desktop_assistant import mac_factory_request_install as target
            result = target.prepare(args.package_sha256, target.read_network(args.network_file, args.network_sha256))
        elif args.action == 'manage':
            verify_code(); modules()
            from desktop_assistant import mac_system_service as service
            result = service.manage(args.package_sha256, args.management_action)
        elif args.action in {'archive-plan', 'archive-apply'}:
            verify_code(trusted=args.action == 'archive-apply')
            modules()
            from desktop_assistant import mac_archive_install as archive
            if args.action == 'archive-plan':
                raw = canonical(archive.make_plan(args.package_sha256, args.nas_address, args.nas_export))
                fd = os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(raw); output.flush(); os.fsync(output.fileno())
                if args.file.read_bytes() != raw: raise ValueError('归档安装计划回读不符')
                result = {'plan_sha256': digest(raw), 'system_modified': False}
            else:
                result = archive.apply(protected(args.file), args.approved_sha256)
        elif args.action in {'service-plan', 'service-apply'}:
            verify_code(trusted=args.action == 'service-apply')
            modules()
            from desktop_assistant import mac_system_service as service
            if args.action == 'service-plan':
                value = service.make_plan(args.service_action, args.package_sha256)
                raw = canonical(value)
                fd = os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(raw); output.flush(); os.fsync(output.fileno())
                if args.file.read_bytes() != raw: raise ValueError('系统服务计划回读不符')
                result = {'plan_sha256': digest(raw), 'system_modified': False}
            else:
                result = service.apply(protected(args.file), args.approved_sha256)
        elif args.action in {'publish', '_publish_worker'}:
            verify_code()
            modules()  # Only now make the protected minimal pack importable.
            from desktop_assistant import mac_release_publish as publication
            if args.action == '_publish_worker':
                result = publication.worker(decode(sys.stdin.buffer.read(LIMIT + 1)))
            else:
                raw = protected(args.file)
                result = publication.publish(raw, args.approved_sha256, args.package)
        elif args.action == '_worker':
            result = _worker(decode(sys.stdin.buffer.read(LIMIT + 1)))
        elif args.action == 'plan':
            with args.identity_plan.open('rb') as source:
                result = make_plan(source.read(LIMIT + 1), args.observer_uid)
            raw = canonical(result)
            fd = os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(raw); output.flush(); os.fsync(output.fileno())
            if args.file.read_bytes() != raw:
                raise ValueError('只读安装计划写出回读失败')
            result = {'plan_sha256': digest(raw), 'read_only_configuration': True}
        else:
            with args.file.open('rb') as source:
                raw = source.read(LIMIT + 1)
            result = install(raw, args.approved_sha256)
    except (Exception, KeyboardInterrupt):
        raise SystemExit('管理员初始化或发布未确认完成；可能已有文件、条目或完成记录，保留现场，不覆盖重试、不自动删除。') from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
