"""System launchd deployment via the reviewed stdlib administrator pack.

The plist pins an already published, immutable interpreter and supervisor. Root
never imports that runtime. Check/stop workers execute only as the dedicated UID.
Registration is not evidence of healthy operation, logout/reboot or cutover.
"""
import json
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
import uuid

from desktop_assistant import mac_keychain_install as bootstrap
from desktop_assistant import mac_release_publish as publication

BASE = Path('/Library/TianmingERP')
ROOT = BASE / 'instance'
CONTROL = BASE / 'system-service.json'
PLISTS = Path('/Library/LaunchDaemons')
LABEL = 'cn.tianming.erp'
PLIST = PLISTS / (LABEL + '.plist')
LAUNCHCTL = '/bin/launchctl'
TYPE = 'tianming.system-service.v1'
PLAN_TYPE = 'tianming.system-service-plan.v1'
ENTRY = 'desktop_assistant/service_supervisor.py'
FIELDS = {'type', 'package_sha256', 'context_sha256', 'publisher_sha256', 'plist_sha256', 'enabled'}
PLAN_FIELDS = {'type', 'action', 'attempt_id', 'created_at', 'expires_at', 'code_manifest_sha256',
               'package_sha256', 'context_sha256', 'publisher_sha256', 'registry_sha256',
               'control_sha256', 'plist_sha256'}


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)


def definition(package, publisher):
    if not _sha(package) or not _sha(publisher):
        raise ValueError('系统服务程序或公钥身份无效')
    tree = BASE / 'published' / package / 'migration'
    return {
        'Label': LABEL, 'UserName': '_tianmingerp', 'GroupName': '_tianmingerp',
        'InitGroups': True,
        'ProgramArguments': [str(tree/'runtime/bin/python3.12'), '-I', '-B', str(tree/ENTRY),
            '--root', str(ROOT), '--public-key', str(BASE/'publisher-public.pem'),
            '--publisher-sha256', publisher, '--supervisor-package', package, '--system-action', 'run'],
        'WorkingDirectory': str(ROOT), 'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 30,
        # 75s lock wait + 60s graceful stop, with room for an in-flight start.
        # This finite OS shutdown limit is not a promise to survive power loss.
        'ExitTimeOut': 240, 'AbandonProcessGroup': True, 'Umask': 0o077,
        'ProcessType': 'Interactive',
        'EnvironmentVariables': {'HOME': str(BASE/'home'), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                                 'LANG': 'en_US.UTF-8', 'ERP_HOME_REHEARSAL': '1'},
        # Root-controlled parent and files prevent service-created log symlinks.
        'StandardOutPath': str(BASE/'system-service.stdout.log'),
        'StandardErrorPath': str(BASE/'system-service.stderr.log'),
    }


def _plist(package, publisher):
    return plistlib.dumps(definition(package, publisher), sort_keys=True)


def metadata(package):
    """Read public root artifacts only, using stdlib; no application imports."""
    if not _sha(package):
        raise ValueError('系统程序包身份无效')
    identity, keychain = bootstrap.modules()
    identity._parent()
    publication.guard(BASE, directory=True, mode=0o755)
    context_raw = publication.read_record(BASE/'keychain-context.json')
    context = bootstrap.decode(context_raw)
    if (set(context) != keychain.FIELDS or context['type'] != keychain.TYPE
            or any(type(context[k]) is not int or not 400 <= context[k] < 500 for k in ('uid', 'gid'))):
        raise ValueError('系统身份完成记录无效')
    record_raw = publication.read_record(BASE/'published.json')
    record = bootstrap.decode(record_raw)
    public = publication.read_record(BASE/'publisher-public.pem')
    packages = record.get('packages')
    if (set(record) != {'type', 'context_sha256', 'publisher_sha256', 'packages'}
            or record['type'] != publication.REGISTRY_TYPE
            or record['context_sha256'] != bootstrap.digest(context_raw)
            or record['publisher_sha256'] != bootstrap.digest(public)
            or not isinstance(packages, list) or not 1 <= len(packages) <= 32
            or any(not _sha(item) for item in packages) or packages != sorted(set(packages))
            or package not in packages):
        raise ValueError('程序不属于本机root批准记录')
    slot = BASE/'published'/package
    for directory in (slot.parent, slot): publication.guard(directory, directory=True, mode=0o755)
    if {p.name for p in slot.iterdir()} != {'package.zip', 'server', 'migration'}:
        raise ValueError('root程序目录不完整或有额外对象')
    archive = slot/'package.zip'
    publication.guard(archive)
    with archive.open('rb') as stream:
        if publication.digest(stream) != package: raise ValueError('root原包摘要不符')
        raw, manifest, _ = publication.inspect(stream)
    capability = manifest['runtime_capabilities'].get('system_service_v1')
    if (type(capability) is not int or capability != 1
            or not {ENTRY, 'desktop_assistant/mac_system_service.py'} <= manifest['files'].keys()):
        raise ValueError('签名程序尚未实现系统服务入口')
    publication.check_tree(slot/'migration', manifest, raw, server=False)
    return {'package': package, 'context': context, 'context_sha256': bootstrap.digest(context_raw),
            'publisher_sha256': bootstrap.digest(public), 'registry_sha256': bootstrap.digest(record_raw),
            'public': public, 'tree': slot/'migration'}


def record_for(meta, enabled):
    return {'type': TYPE, 'package_sha256': meta['package'], 'context_sha256': meta['context_sha256'],
            'publisher_sha256': meta['publisher_sha256'],
            'plist_sha256': bootstrap.digest(_plist(meta['package'], meta['publisher_sha256'])), 'enabled': enabled}


def checked_control(meta):
    raw = publication.read_record(CONTROL)
    value = bootstrap.decode(raw)
    if (set(value) != FIELDS or type(value.get('enabled')) is not bool
            or value != record_for(meta, value['enabled'])):
        raise ValueError('系统服务配置不属于当前身份/程序/固定plist')
    return raw, value


def _installed_plist(meta):
    for parent in (PLISTS, *PLISTS.parents):
        bootstrap.protected(parent, directory=True)
    expected = _plist(meta['package'], meta['publisher_sha256'])
    raw = publication.read_record(PLIST)
    if raw != expected: raise ValueError('系统plist不属于本安装，未接管其他服务')
    return raw


def make_plan(action, package):
    if action not in {'install', 'disable', 'enable'}: raise ValueError('系统服务动作无效')
    code_sha = bootstrap.verify_code(trusted=False)
    meta = metadata(package)
    previous = None
    if action == 'install':
        if any(p.exists() or p.is_symlink() for p in (CONTROL, PLIST)):
            raise ValueError('已有系统服务现场，不能重复安装')
    else:
        previous, control = checked_control(meta)
        _installed_plist(meta)
        # A fresh approved disable may finish a previous partial stop. It still
        # binds the exact current control bytes and never reopens the start gate.
        if action == 'enable' and control['enabled']:
            raise ValueError('系统服务状态不允许该动作；先核查失败现场')
    now = int(time.time())
    return {'type': PLAN_TYPE, 'action': action, 'attempt_id': uuid.uuid4().hex,
            'created_at': now, 'expires_at': now+7*86400, 'code_manifest_sha256': code_sha,
            'package_sha256': package, **{k: meta[k] for k in ('context_sha256', 'publisher_sha256', 'registry_sha256')},
            'control_sha256': bootstrap.digest(previous) if previous is not None else None,
            'plist_sha256': bootstrap.digest(_plist(package, meta['publisher_sha256']))}


def plan_header(raw, approved_sha):
    if bootstrap.digest(raw) != approved_sha: raise ValueError('系统服务计划批准摘要不符')
    plan = bootstrap.decode(raw)
    if (set(plan) != PLAN_FIELDS or plan['type'] != PLAN_TYPE
            or plan['action'] not in {'install', 'disable', 'enable'}
            or not isinstance(plan['attempt_id'], str) or not re.fullmatch('[0-9a-f]{32}', plan['attempt_id'])
            or any(type(plan[k]) is not int for k in ('created_at', 'expires_at'))
            or not 0 < plan['expires_at']-plan['created_at'] <= 7*86400
            or not plan['created_at'] <= time.time() < plan['expires_at']
            or any(not _sha(plan[k]) for k in PLAN_FIELDS if k.endswith('_sha256') and k != 'control_sha256')
            or (plan['control_sha256'] is not None and not _sha(plan['control_sha256']))
            or (plan['action'] == 'install') != (plan['control_sha256'] is None)
            or plan['code_manifest_sha256'] != bootstrap.verify_code()):
        raise ValueError('系统服务计划无效、过期或代码包不符')
    return plan


def validate(raw, approved_sha):
    plan = plan_header(raw, approved_sha)
    meta = metadata(plan['package_sha256'])
    if any(plan[k] != meta[k] for k in ('context_sha256', 'publisher_sha256', 'registry_sha256')):
        raise ValueError('系统服务计划的root发布事实已经变化')
    if plan['plist_sha256'] != bootstrap.digest(_plist(meta['package'], meta['publisher_sha256'])):
        raise ValueError('系统服务定义不符')
    if plan['action'] == 'install':
        objects = (CONTROL, PLIST, BASE/'system-service.stdout.log', BASE/'system-service.stderr.log')
        if any(p.exists() or p.is_symlink() for p in objects) or list(BASE.glob('system-service-*.pending.json')):
            raise ValueError('已有系统服务安装现场，不覆盖重试')
    else:
        previous, control = checked_control(meta)
        _installed_plist(meta)
        if (bootstrap.digest(previous) != plan['control_sha256']
                or (plan['action'] == 'enable' and control['enabled'])):
            raise ValueError('系统服务状态变化，不覆盖重试')
    return plan, meta


def _communicate(process):
    interrupted = False
    while True:
        try:
            output, error = process.communicate()
            break
        except KeyboardInterrupt:
            interrupted = True
    if interrupted: raise ValueError('操作被中断；已观察本次进程退出，保留现场')
    return process.returncode, output, error


def launch(arguments):
    return _communicate(subprocess.Popen([LAUNCHCTL, *arguments], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
        env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8', 'LC_ALL': 'C'}))


def present():
    code, output, error = launch(['print', 'system/'+LABEL])
    if code == 0: return True
    if code == 113 and b'Could not find service "'+LABEL.encode()+b'" in domain for system' in error+output:
        return False
    raise ValueError('无法确认精确系统服务是否存在；未修改注册')


def worker(meta, action):
    if action not in {'check', 'stop'}: raise ValueError('系统子进程动作无效')
    spec = definition(meta['package'], meta['publisher_sha256'])
    args = spec['ProgramArguments'][:-1] + [action, '--challenge', uuid.uuid4().hex]
    challenge = args[-1]
    context = meta['context']
    code, output, _ = _communicate(subprocess.Popen(args, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, user=context['uid'], group=context['gid'],
        extra_groups=[], close_fds=True, pass_fds=(), umask=0o077, cwd='/',
        env=spec['EnvironmentVariables']))
    if code != 0: raise ValueError('专用身份检查/正常停止未通过，保留现场')
    expected = {'type': 'tianming.system-service-worker.v1', 'action': action, 'challenge': challenge,
                'package_sha256': meta['package'], 'identity': bootstrap._expected(context['uid'], context['gid']),
                'manual_stop': True, 'process_stopped': True, 'home_rehearsal': True,
                'keychain_canary_verified': action == 'check'}
    if bootstrap.decode(output) != expected: raise ValueError('专用身份子进程回执不符')


def _set_control(meta, enabled, attempt, *, initial=False):
    raw = bootstrap.canonical(record_for(meta, enabled))
    if initial:
        bootstrap._write(CONTROL, raw, mode=0o644)
    else:
        temporary = BASE/('system-service-'+attempt+'.json')
        bootstrap._write(temporary, raw, mode=0o644)
        os.replace(temporary, CONTROL)
    publication.sync_directory(BASE)
    if publication.read_record(CONTROL) != raw: raise ValueError('系统服务状态回读失败，可能已生效，保留现场')


def create_log(path, gid, *, readable=False):
    # launchd documents O_RDWR for StandardErrorPath. Root owns the file and
    # protected parent; the service group needs both bits for that open mode.
    mode = 0o660 if readable else 0o620
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchown(fd, 0, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    publication.guard(path, mode=mode)
    if path.stat().st_gid != gid: raise ValueError('系统日志组不符')


def apply(raw, approved_sha):
    identity, keychain = bootstrap.modules()
    identity._administrator()
    plan_header(raw, approved_sha)  # Reject bad inputs before even creating a lock file.
    with publication.lock():  # Also excludes concurrent root package publication.
        plan, meta = validate(raw, approved_sha)
        keychain._identity(meta['context'], administrator=True)
        for parent in (PLISTS, *PLISTS.parents): bootstrap.protected(parent, directory=True)
        exists = present()
        if plan['action'] in {'install', 'enable'} and exists:
            raise ValueError('精确系统标签已有注册，未覆盖或接管')
        # A disabled-state preflight may verify data without enabling execution.
        if plan['action'] != 'disable': worker(meta, 'check')
        plan_header(raw, approved_sha)
        bootstrap._write(BASE/('system-service-'+plan['attempt_id']+'.pending.json'), raw)
        if plan['action'] == 'install':
            for name in ('system-service.stdout.log', 'system-service.stderr.log'):
                create_log(BASE/name, meta['context']['gid'], readable=name.endswith('.stderr.log'))
            _set_control(meta, True, plan['attempt_id'], initial=True)
            bootstrap._write(PLIST, _plist(meta['package'], meta['publisher_sha256']), mode=0o644)
            publication.sync_directory(PLISTS)
            _installed_plist(meta)
        elif plan['action'] == 'disable':
            # Closing this root gate first blocks concurrent resume/start calls.
            _set_control(meta, False, plan['attempt_id'])
            worker(meta, 'stop')
            if exists:
                if launch(['bootout', 'system', str(PLIST)])[0] != 0:
                    raise ValueError('正常停止后系统注销未确认，保留禁用状态与现场')
            if present(): raise ValueError('系统服务仍已注册，未确认停用')
            if launch(['disable', 'system/'+LABEL])[0] != 0:
                raise ValueError('系统持久禁用未确认；root启动门禁已关闭，保留现场')
        else:
            _set_control(meta, True, plan['attempt_id'])
            if launch(['enable', 'system/'+LABEL])[0] != 0:
                raise ValueError('系统启用未确认，保留现场')
        if plan['action'] != 'disable':
            if launch(['bootstrap', 'system', str(PLIST)])[0] != 0 or not present():
                raise ValueError('系统注册未确认；可能已经启动监护进程，保留现场')
        receipt = {'type': 'tianming.system-service-result.v1', 'action': plan['action'],
                   'plan_sha256': approved_sha, 'registered': plan['action'] != 'disable',
                   'health_verified': False, 'system_keychain_session_verified': False,
                   'logout_reboot_verified': False, 'factory_activated': False}
        bootstrap._write(BASE/('system-service-'+plan['attempt_id']+'.verified.json'), bootstrap.canonical(receipt))
        return receipt


def check_start():
    """Dedicated Manager.start gate; metadata cannot be removed by that UID."""
    if not CONTROL.exists() and not CONTROL.is_symlink():
        return  # Pre-install isolated commands still require the publication gate.
    raw = publication.read_record(CONTROL)
    value = bootstrap.decode(raw)
    if (set(value) != FIELDS or value['type'] != TYPE or type(value['enabled']) is not bool
            or any(not _sha(value[k]) for k in FIELDS if k.endswith('_sha256'))):
        raise ValueError('系统服务启动门禁无效')
    if not value['enabled']: raise ValueError('管理员已停用系统服务，拒绝恢复启动')
    meta = {'package': value['package_sha256'],
            'context_sha256': bootstrap.digest(publication.read_record(BASE/'keychain-context.json')),
            'publisher_sha256': bootstrap.digest(publication.read_record(BASE/'publisher-public.pem'))}
    if value != record_for(meta, True): raise ValueError('系统启动门禁绑定已改变')
    _installed_plist(meta)


def runtime_configuration(args, *, script, executable):
    """Runs in the pinned runtime, after launchd/Popen has selected its UID."""
    from app.core import mac_service_keychain as keychain
    from desktop_assistant import mac_published_runtime as published
    context = keychain.load_context()
    actual = bootstrap._actual()
    if (any(actual[k] != context['uid'] for k in ('uid', 'euid'))
            or any(actual[k] != context['gid'] for k in ('gid', 'egid'))
            or not actual['kernel_groups'] or not set(actual['kernel_groups']) <= {context['gid'], 12, 61}):
        raise ValueError('系统服务实际内核身份或附加组不符')
    if (Path(args.root).absolute() != ROOT or Path(args.public_key).absolute() != BASE/'publisher-public.pem'
            or not _sha(args.supervisor_package)):
        raise ValueError('系统服务参数不是固定本机实例')
    public = published._read(published.PUBLIC)
    if bootstrap.digest(public) != args.publisher_sha256: raise ValueError('系统服务公钥身份不符')
    tree, manifest = published.resolve(ROOT, args.supervisor_package, public, kind='migration')
    capability = manifest.get('runtime_capabilities', {}).get('system_service_v1')
    if (type(capability) is not int or capability != 1 or Path(script).resolve() != tree/ENTRY
            or Path(executable).resolve() != tree/'runtime/bin/python3.12'):
        raise ValueError('系统服务没有运行指定的不可变原生入口')
    meta = {'package': args.supervisor_package, 'context': context,
            'context_sha256': bootstrap.digest(published._read(keychain.CONTEXT)),
            'publisher_sha256': bootstrap.digest(public)}
    if args.system_action != 'check':
        _, control = checked_control(meta)
        _installed_plist(meta)
        if args.system_action == 'stop' and control['enabled']:
            raise ValueError('正常停用前必须先关闭root启动门禁')
    else:
        control = None
    # Reject incomplete/linked/private-path mismatches before Manager can mkdir.
    for folder in ('', 'releases', 'shared', 'control', 'backups', 'packages', 'staging'):
        path = ROOT/folder
        if path.resolve() != path.absolute() or path.stat().st_dev != BASE.stat().st_dev:
            raise ValueError('系统实例必须是完整本机私有目录')
        with keychain._checked_path(path, uid=context['uid'], directory=True, mode=0o700): pass
    for path in (ROOT/'state.json', ROOT/'control/operation.lock'):
        with keychain._checked_path(path, uid=context['uid'], mode=0o600) as fd:
            if os.fstat(fd).st_size == 0: raise ValueError('系统实例缺少已有状态或维护锁')
    if args.system_action == 'check' or (args.system_action == 'run' and control['enabled']):
        verify_keychain(context)
    return context, public, control


def verify_keychain(context):
    # Read only the random installation bootstrap and HMAC canary, never an
    # operational credential. Stop must remain possible when this check fails.
    from app.core import mac_service_keychain as keychain
    with keychain.unlocked(keychain.Security(), context):
        pass


@contextmanager
def stop_lock(manager, timeout=75):
    """Wait only for lock acquisition, never repeat an executed stop body."""
    from desktop_assistant.operation_lock import BUSY
    deadline = time.monotonic() + timeout
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(manager.lock())
                break
            except ValueError as error:
                if str(error) != BUSY or time.monotonic() >= deadline:
                    raise
                time.sleep(.5)
        yield


def runtime_worker(manager, args, context):
    from desktop_assistant.storage import write_json
    if args.system_action not in {'check', 'stop'} or not re.fullmatch('[0-9a-f]{32}', args.challenge or ''):
        raise ValueError('系统服务子进程动作或挑战无效')
    with stop_lock(manager) if args.system_action == 'stop' else manager.lock():
        state = manager.state
        if args.system_action == 'check':
            if (state.get('manual_stop') is not True or not _sha(state.get('current'))
                    or state.get('onboarding_pending') or state.get('operation') == 'migration_running'
                    or 'failed' in state.get('operation', '')):
                raise ValueError('仅完整恢复且明确停止的实例可以安装/启用系统服务')
            manager._approve_execution(state['current'])
            authority = state.get('schema_authority')
            if authority and authority != state['current']:
                from desktop_assistant.mac_published_runtime import verify_authority
                verify_authority(manager.root, authority, manager.public_key)
            # On first install CONTROL does not exist yet. Require the actual
            # current application, independently of the chosen supervisor, to
            # support the persistent administrator gate before creating it.
            _, manifest = manager._published_release(state['current'])
            capability = manifest.get('runtime_capabilities', {}).get('system_service_v1')
            if type(capability) is not int or capability != 1:
                raise ValueError('当前程序尚未实现系统服务停用门禁')
        else:
            state['manual_stop'] = True
            write_json(manager.root/'state.json', state)
            manager.stop()
        if manager._process(): raise ValueError('ERP尚未正常退出，不能注册/注销系统服务')
        return {'type': 'tianming.system-service-worker.v1', 'action': args.system_action,
                'challenge': args.challenge, 'package_sha256': args.supervisor_package,
                'identity': bootstrap._actual(), 'manual_stop': True, 'process_stopped': True,
                'home_rehearsal': True, 'keychain_canary_verified': args.system_action == 'check'}
