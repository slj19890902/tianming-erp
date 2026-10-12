"""Pinned dedicated-UID supervisor-upgrade worker; no root transaction here.

Only the existing ERP process can be stopped. This never stops the supervisor,
invokes launchctl, writes root controls, starts ERP, or accesses NAS credentials.
The root plan binds the future administrator pack hash; actually authenticating
that pack belongs to the future root producer, not this signed runtime tree.
Top-level imports are stdlib so the fixed file can bootstrap under -I -B.
"""
import argparse
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import sys
import time

BASE = Path('/Library/TianmingERP')
ROOT = BASE / 'instance'
MODULE = 'desktop_assistant/mac_system_upgrade_worker.py'
CONTRACT_MODULE = 'desktop_assistant/mac_system_upgrade_contract.py'
CAPABILITY = 'system_upgrade_worker_v1'
FAILURE = '监督升级阶段未确认完成；保留现场，不重试、启动或清理。不输出秘密或异常详情。'


def _entry():
    if (sys.platform != 'darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (499, 499, 499, 499)):
        raise ValueError('监督升级需要固定专用身份及隔离解释器')
    import pwd
    if pwd.getpwuid(499).pw_name != '_tianmingerp':
        raise ValueError('监督升级专用账号不符')


@contextmanager
def _plan(request):
    from desktop_assistant import mac_system_upgrade_contract as contract
    from desktop_assistant.mac_service_restore import Lease, _read_complete
    # Validate the locator before opening anything. No caller-selected path.
    path = Path(contract.plan_path(request.get('attempt_id')))
    if path != BASE / ('system-upgrade-' + request['attempt_id']) / 'plan.json':
        raise ValueError('监督升级计划位置不符')
    with ExitStack() as stack:
        base = stack.enter_context(Lease(BASE, uid=0, gid=None, mode=0o755, directory=True, mutable=True))
        slot = stack.enter_context(Lease(path.parent, uid=0, gid=None, mode=0o755, directory=True, parent=base))
        plan_file = stack.enter_context(Lease(path, uid=0, gid=None, mode=0o644, parent=slot))
        raw = _read_complete(plan_file, contract.LIMIT)
        if contract.digest(raw) != request.get('plan_sha256'):
            raise ValueError('监督升级计划原字节批准摘要不符')
        plan = contract.validate_plan(contract.decode(raw), now=time.time())
        contract.validate_request(request, plan, contract.digest(raw))

        def check():
            base.check(); slot.check(); plan_file.check()
            if _read_complete(plan_file, contract.LIMIT) != raw:
                raise ValueError('监督升级计划变化')
            contract.validate_window(plan, time.time())
        check()
        yield plan, check
        check()


def authenticated(request, plan, *, script, executable):
    from desktop_assistant import mac_system_upgrade_contract as contract
    from desktop_assistant import mac_keychain_install as boot, mac_system_service as system
    from desktop_assistant import mac_published_runtime as published
    from app.core import mac_service_keychain as keychain
    _entry()
    if os.path.lexists(BASE / 'factory-activation.json'):
        raise ValueError('本监督升级入口仅接受尚未正式接管的实例')
    if plan['uid'] != 499 or plan['gid'] != 499 or not keychain.selected():
        raise ValueError('监督升级实际服务身份不符')
    contract.validate_identity(boot._actual(), plan)
    context = keychain.load_context()
    if context['uid'] != plan['uid'] or context['gid'] != plan['gid']:
        raise ValueError('监督升级context身份不符')
    resolved = {}
    for prefix in ('old', 'new'):
        identity = plan[prefix + '_package_sha256']
        meta = system.metadata(identity)
        if (meta['context'] != context or any(meta[name] != plan[name] for name in
                ('context_sha256', 'publisher_sha256', 'registry_sha256'))):
            raise ValueError('监督升级原公钥或root批准记录已变化')
        resolved[prefix] = (meta, *published.resolve(ROOT, identity, meta['public'], kind='migration'))
    meta, tree, manifest = resolved['new']
    if resolved['old'][0]['public'] != meta['public']:
        raise ValueError('新旧监护发布公钥不同')
    caps = manifest.get('runtime_capabilities', {})
    if (type(caps.get(CAPABILITY)) is not int or caps[CAPABILITY] != 1
            or not {MODULE, CONTRACT_MODULE, 'desktop_assistant/manager.py',
                    'desktop_assistant/mac_service_manage.py'} <= manifest.get('files', {}).keys()
            or Path(script).resolve() != tree / MODULE or Path(__file__).resolve() != tree / MODULE
            or Path(executable).resolve() != tree / 'runtime/bin/python3.12'
            or Path(sys.executable).resolve() != tree / 'runtime/bin/python3.12'):
        raise ValueError('监督升级没有执行固定的新签名入口')
    prefix = 'old' if request['phase'] == 'stop' else 'new'
    expected_plist = system._plist(plan[prefix + '_package_sha256'], plan['publisher_sha256'])
    if contract.digest(expected_plist) != plan[prefix + '_plist_sha256']:
        raise ValueError('监督升级固定plist与计划不符')
    if system.publication.read_record(system.CONTROL) != contract.disabled_control_bytes(plan, request['phase']):
        raise ValueError('监督升级阶段的root启动门禁未关闭或程序不符')
    # Also check the protected launchd parent chain, without querying launchctl.
    for parent in (system.PLISTS, *system.PLISTS.parents):
        boot.protected(parent, directory=True)
    if system.publication.read_record(system.PLIST) != expected_plist:
        raise ValueError('监督升级阶段的固定plist不符')
    return context, meta['public']


def run(request, *, script, executable):
    _entry()
    from desktop_assistant import mac_system_upgrade_contract as contract
    from desktop_assistant import mac_keychain_install as boot, mac_service_manage as manage
    from desktop_assistant import mac_system_service as system
    from desktop_assistant.manager import Manager
    if type(request) is not dict:
        raise ValueError('监督升级请求必须为公开对象')
    # Detach public caller data, applying the same bounded contract codec.
    request = contract.decode(contract.canonical(request))
    with _plan(request) as (plan, plan_check):
        context, public = authenticated(request, plan, script=script, executable=executable)
        verify = request['phase'] == 'verify'
        with manage.instance(context, inherited_lock=request['lock_fd']) as (folders, lock, paths_check), \
                _held_maintenance(lock, borrowed=verify):
            root = folders['root']
            def recheck():
                paths_check(); plan_check()
                actual_context, actual_public = authenticated(request, plan, script=script, executable=executable)
                if actual_context != context or actual_public != public:
                    raise ValueError('监督升级身份变化')

            raw, state = manage.record(root, 'state.json', context)
            expected_before = plan['state_stopped_sha256'] if verify else plan['state_before_sha256']
            if (boot.digest(raw) != expected_before or state.get('current') != plan['current_package_sha256']
                    or (verify and state.get('manual_stop') is not True)):
                raise ValueError('监督升级原完整状态不符')
            proposed = dict(state); proposed['manual_stop'] = True
            proposed_raw = boot.canonical(proposed)
            manage.decode_record(proposed_raw, 'state.json')
            if boot.digest(proposed_raw) != plan['state_stopped_sha256']:
                raise ValueError('监督升级仅允许改变手动停止字段')
            manager = Manager(ROOT, public)
            # This validates existing root-published code without importing ERP
            # or reading business data. Stop never depends on canary or NAS.
            manager._approve_execution(state['current'])
            expected_executable = manager._runtime_python(state['current'])
            process = manager._process()
            if process is not None and Path(process[1]['exe']).resolve() != expected_executable.resolve():
                raise ValueError('记录进程不是原current的ERP解释器')
            recheck()
            os.environ['ERP_HOME_REHEARSAL'] = '1'
            if verify:
                if process is not None:
                    raise ValueError('ERP尚未停止')
                system.verify_keychain(context)  # Existing random canary only.
            else:
                manage.replace_record(root, 'state.json', proposed, raw, context, recheck)
                manager.stop()  # Existing PID/create-time/exe/nonce graceful stop.

            def stopped():
                recheck()
                final_raw, final = manage.record(root, 'state.json', context)
                if (final_raw != proposed_raw or boot.digest(final_raw) != plan['state_stopped_sha256']
                        or final != proposed or manager._process() is not None):
                    raise ValueError('监督升级停止状态或原恢复事实复验失败')
                manager._approve_execution(state['current'])
            stopped()
            control = contract.disabled_control(plan, request['phase'])
            result = {'type': contract.REPLY, 'attempt_id': plan['attempt_id'],
                'plan_sha256': request['plan_sha256'], 'phase': request['phase'], 'challenge': request['challenge'],
                'new_package_sha256': plan['new_package_sha256'], 'current_package_sha256': state['current'],
                'control_package_sha256': control['package_sha256'], 'plist_sha256': control['plist_sha256'],
                'identity': boot._actual(), 'state_before_sha256': expected_before,
                'state_after_sha256': plan['state_stopped_sha256'], 'manual_stop': True, 'process_stopped': True,
                'root_gate_enabled': False, 'home_rehearsal': True,
                'keychain_canary_verified': verify, 'borrowed_lock': verify}
            contract.validate_reply(result, request, plan, request['plan_sha256'])
            stopped()
    return result


@contextmanager
def _held_maintenance(lock, *, borrowed):
    from desktop_assistant import mac_service_manage as manage
    if borrowed:
        import fcntl
        if fcntl.fcntl(lock.fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
            raise ValueError('借用维护锁必须为原只读描述符')
        # The parent must already hold it. A mere open, unlocked descriptor is
        # not evidence of the protected transaction expected by this phase.
        fd = os.open(lock.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            lock.check()
            if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (os.fstat(lock.fd).st_dev, os.fstat(lock.fd).st_ino):
                raise ValueError('继承维护锁文件身份不符')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise ValueError('父事务没有持有原维护锁')
        finally:
            os.close(fd)
    with manage.maintenance(lock, borrowed=borrowed):
        yield


def _arguments(argv):
    from desktop_assistant import mac_system_upgrade_contract as contract
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError('监督升级仅接受固定公开参数')
    parser = SafeParser(add_help=False, allow_abbrev=False)
    for name in ('attempt', 'plan-sha256', 'challenge', 'phase'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--lock-fd', type=int)
    if len(argv) not in (8, 10) or len(set(argv[::2])) != len(argv[::2]):
        raise ValueError('监督升级公开参数数量或重复项无效')
    args = parser.parse_args(argv)
    return {'type': contract.REQUEST, 'attempt_id': args.attempt, 'plan_sha256': args.plan_sha256,
            'phase': args.phase, 'challenge': args.challenge, 'lock_fd': args.lock_fd}


def main():
    _entry()  # Before importing any runtime dependency or touching a root path.
    os.umask(0o077)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from desktop_assistant import mac_system_upgrade_contract as contract
    request = _arguments(sys.argv[1:])
    result = run(request, script=__file__, executable=sys.executable)
    raw = contract.canonical(result)
    if not 0 < len(raw) <= contract.LIMIT:
        raise ValueError('监督升级公开回复长度无效')
    sys.stdout.buffer.write(raw + b'\n'); sys.stdout.buffer.flush()


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt):
        raise SystemExit(FAILURE) from None
