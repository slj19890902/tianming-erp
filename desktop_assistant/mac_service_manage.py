"""Fixed service-instance operations, under one held existing maintenance FD.

Status and stop never require secret access. Configuring the NAS preserves the
restored credential reference and every unrelated preference. No activation,
migration, restore, arbitrary path, credential rotation or data deletion here.
"""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from app.core import mac_service_keychain as keychain
from desktop_assistant import mac_keychain_install as boot
from desktop_assistant import mac_system_service as system
from desktop_assistant import mac_published_runtime as published
from desktop_assistant import mac_backup_storage as remote
from desktop_assistant.mac_service_restore import Lease, _read_complete

BASE = Path('/Library/TianmingERP')
ROOT = BASE/'instance'


def authenticated(request, script, executable):
    if (not isinstance(request, dict) or set(request) != {'action','package_sha256','challenge',
            'context_sha256','publisher_sha256','registry_sha256'} or request['action'] not in system.MANAGE_ACTIONS
            or any(not system._sha(request[k]) for k in request if k.endswith('_sha256'))
            or not isinstance(request['challenge'], str) or not re.fullmatch('[0-9a-f]{32}', request['challenge'])
            or sys.platform != 'darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or not keychain.selected()):
        raise ValueError('专用管理请求或解释器身份无效')
    context = keychain.load_context()
    if boot._actual() != boot._expected(context['uid'], context['gid']):
        raise ValueError('专用管理实际内核身份不符')
    meta = system.metadata(request['package_sha256'])
    if (context != meta['context'] or any(request[k] != meta[k] for k in
            ('context_sha256','publisher_sha256','registry_sha256'))):
        raise ValueError('专用管理本机批准身份已经变化')
    tree, manifest = published.resolve(ROOT, request['package_sha256'], meta['public'], kind='migration')
    system.require_management(manifest)
    if (Path(script).resolve() != tree/system.MANAGE_ENTRY or Path(__file__).resolve() != tree/system.MANAGE_MODULE
            or Path(executable).resolve() != tree/'runtime/bin/python3.12'):
        raise ValueError('专用管理不属于正在执行的批准程序')
    return context, meta['public']


@contextmanager
def instance(context, *, inherited_lock=None):
    """Hold local private parents and the exact existing lock, never initialize."""
    with ExitStack() as stack:
        base = stack.enter_context(Lease(BASE, uid=0, gid=None, mode=0o755, directory=True, mutable=True))
        root = stack.enter_context(Lease(ROOT, uid=context['uid'], gid=context['gid'], mode=0o700,
                                        directory=True, parent=base, mutable=True))
        folders = {'root':root}
        for name in ('releases','shared','control','backups','packages','staging'):
            folders[name] = stack.enter_context(Lease(ROOT/name, uid=context['uid'], gid=context['gid'],
                mode=0o700, directory=True, parent=root, mutable=True))
        if inherited_lock is not None and (type(inherited_lock) is not int or inherited_lock < 3):
            raise ValueError('继承维护锁描述符无效')
        lock = stack.enter_context(Lease(ROOT/'control/operation.lock', uid=context['uid'], gid=context['gid'],
            mode=0o600, parent=folders['control'], fd=os.dup(inherited_lock) if inherited_lock is not None else None))
        if _read_complete(lock, 1) != b'0': raise ValueError('专用管理维护锁不是已有完整锁')
        def recheck():
            for lease in folders.values(): lease.check()
            lock.check()
        recheck()
        yield folders, lock, recheck
        recheck()


@contextmanager
def maintenance(lock, *, borrowed=False):
    import fcntl
    from desktop_assistant.operation_lock import BUSY
    lock.check()
    try: fcntl.flock(lock.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise ValueError(BUSY) from None
    try:
        lock.check(); yield; lock.check()
    finally:
        # An activation worker borrows the administrator's open-file-description.
        # Always acquire/verify the lock above, but leave ownership with the
        # parent until it has published the protected record. Closing this dup
        # cannot release the parent's lock; LOCK_UN would release both.
        if not borrowed: fcntl.flock(lock.fd, fcntl.LOCK_UN)


def record(root, name, context):
    with Lease(root.path/name, uid=context['uid'], gid=context['gid'], mode=0o600, parent=root) as lease:
        raw = _read_complete(lease, 65536)
    return raw, boot.decode(raw)


def settings(root, context):
    raw, value = record(root, 'preferences.json', context)
    reference = value.get('protected_password')
    if not isinstance(reference, str) or not re.fullmatch('tm-keychain-v1:backup:[0-9a-f]{32}', reference):
        raise ValueError('请先完整恢复已移交的备份凭据；不能以个人DPAPI/钥匙串设置替代')
    return raw, value


def approved_destination(context):
    from desktop_assistant import mac_smb_stream_target as stream
    if stream.metadata_present():
        return stream.approved_destination(context)
    value, _, actual = remote._contract()
    if actual != context: raise ValueError('归档合同与管理身份不同')
    path = Path(value['destination'])
    remote.prepare_destination(path)
    return path


def configured(root, context):
    try:
        _, value = settings(root, context)
        from desktop_assistant import mac_smb_stream_target as stream
        if stream.selected(value.get('nas')):
            return stream.configured(value['nas'], context)
        contract, _, actual = remote._contract()  # Local public metadata, no remote IO.
        return actual == context and value.get('nas') == contract['destination']
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError): return False


def save_destination(root, context, recheck):
    previous, saved = settings(root, context)
    destination = approved_destination(context)
    value = {**saved, 'nas':str(destination)}
    # Preserve historical preferences, including the old personal/Windows
    # schedule. Actual system registration comes only from current root control.
    replace_record(root, 'preferences.json', value, previous, context, recheck)


def replace_record(root, name, value, previous, context, recheck):
    if name not in {'preferences.json', 'state.json'}: raise ValueError('不支持的管理记录')
    raw = boot.canonical(value)
    recheck()
    if record(root, name, context)[0] != previous:
        raise ValueError('专用管理记录并发变化，未覆盖')
    temporary = Path(name).stem+'-'+uuid.uuid4().hex+'.pending.json'
    fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root.fd)
    with Lease(root.path/temporary, uid=context['uid'], gid=context['gid'], mode=0o600, parent=root,
               fd=fd, mutable=True) as output:
        remaining = memoryview(raw)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0: raise ValueError('专用管理记录写入不完整，保留现场')
            remaining = remaining[count:]
        os.fsync(fd)
        if _read_complete(output, 65536) != raw: raise ValueError('专用管理记录原字节回读不符')
        recheck()
        if record(root, name, context)[0] != previous:
            raise ValueError('专用管理记录并发变化，保留待核文件')
    os.replace(temporary, name, src_dir_fd=root.fd, dst_dir_fd=root.fd)
    os.fsync(root.fd); recheck()
    if record(root, name, context)[0] != raw:
        raise ValueError('专用管理记录已变更但最终回读未确认')


def snapshot(manager, root, context):
    _, state = record(root, 'state.json', context)
    if not system._sha(state.get('current')): raise ValueError('专用实例尚未完整恢复')
    from desktop_assistant.mac_factory_runtime import configured_mode
    return {'current':state['current'], 'configured_mode':configured_mode(manager),
        'manual_stop':state.get('manual_stop') is True,
        'running':bool(manager._process()), 'backup_configured':configured(root, context),
        'backup_verified_at':remote.verified_at(ROOT, state),
        'backup_attention': bool(state.get('backup_error') or state.get('backup_work_retained')),
        'maintenance_attention': bool(state.get('onboarding_pending') or state.get('operation') == 'migration_running'
                                      or 'failed' in state.get('operation', ''))}


def run(request, *, script, executable):
    context, public = authenticated(request, script, executable)
    with instance(context) as (folders, lock, recheck), maintenance(lock):
        root = folders['root']; state_raw, state = record(root, 'state.json', context)
        if not system._sha(state.get('current')): raise ValueError('专用实例尚未完整恢复')
        from desktop_assistant.manager import Manager
        manager = Manager(ROOT, public)
        action = request['action']; os.environ['ERP_HOME_REHEARSAL'] = '1'
        if action == 'configure-backup':
            save_destination(root, context, recheck)
        elif action == 'stop':
            state['manual_stop'] = True
            replace_record(root, 'state.json', state, state_raw, context, recheck)  # Persist before requesting stop.
            manager.stop()
            if manager._process(): raise ValueError('专用ERP尚未正常退出')
        elif action == 'start':
            if (state.get('onboarding_pending') or state.get('operation') == 'migration_running'
                    or 'failed' in state.get('operation', '')):
                raise ValueError('先完成未决维护，不能恢复启动')
            if not all(system.management_service_state().values()):
                raise ValueError('系统监护未安装或已停用')
            system.verify_keychain(context)
            recheck(); manager.start()
            state_raw, state = record(root, 'state.json', context); state['manual_stop'] = False
            replace_record(root, 'state.json', state, state_raw, context, recheck)
        elif action == 'backup':
            _, saved = settings(root, context)
            destination = approved_destination(context)
            if saved.get('nas') != str(destination): raise ValueError('先配置已批准的系统备份位置')
            system.verify_keychain(context)
            from desktop_assistant.credential_store import unprotect_backup
            password = unprotect_backup(saved['protected_password'])
            recheck()
            backup = manager._backup_locked(password, destination)
            del password
            final = manager.state
            if final.get('last_backup') != str(backup) or remote.verified_at(ROOT, final) is None:
                raise ValueError('完整备份尚未独立验证，不能报告完成')
        recheck()
        result = {'type':'tianming.service-management.v1', 'action':action,
            'package_sha256':request['package_sha256'], 'challenge':request['challenge'],
            'identity':boot._actual(), 'home_rehearsal':True, 'snapshot':snapshot(manager, root, context)}
        return system.management_reply(result, request, context)


def main(*, script, executable):
    result = run(boot.decode(sys.stdin.buffer.read(boot.LIMIT+1)), script=script, executable=executable)
    sys.stdout.buffer.write(boot.canonical(result)+b'\n'); sys.stdout.buffer.flush()
