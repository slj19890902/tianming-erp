"""Synthetic fixed-plan worker IO; root/UID and keychain adapters are explicit.

No production instance, launchd registration, NAS or operational secret is used.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_assistant import mac_system_upgrade_worker as worker


def test_fixed_entry_is_present():
    assert callable(worker.main) and callable(worker.run)


@pytest.fixture
def case(tmp_path, monkeypatch):
    if sys.platform != 'darwin':
        pytest.skip('Darwin private FD/ACL contract; root and UID are simulated')
    from desktop_assistant import mac_system_upgrade_contract as contract
    from desktop_assistant import mac_service_manage as manage, mac_service_restore as restore
    from desktop_assistant import mac_keychain_install as boot, mac_system_service as system
    from desktop_assistant import manager as manager_module
    from desktop_assistant.storage import write_json
    monkeypatch.setenv('ERP_HOME_REHEARSAL', '1')
    base = tmp_path.resolve() / 'system'; base.mkdir(mode=0o755); base.chmod(0o755)
    root = base / 'instance'; root.mkdir(mode=0o700)
    for name in ('releases', 'shared', 'control', 'backups', 'packages', 'staging'):
        (root / name).mkdir(mode=0o700)
    lock_path = root / 'control/operation.lock'; lock_path.write_bytes(b'0'); lock_path.chmod(0o600)
    state = {'current': 'c' * 64, 'manual_stop': False, 'operation': 'restore',
             'native_restore_binding': {'package_sha256': 'e' * 64, 'factory_cutover': False},
             'restored_at': '2026-10-10T00:00:00+08:00', 'retained': ['private synthetic note', 1.25]}
    write_json(root / 'state.json', state); (root / 'state.json').chmod(0o600)
    before = (root / 'state.json').read_bytes()
    stopped = boot.canonical({**state, 'manual_stop': True})
    now = int(time.time())
    plan = {'type': contract.PLAN, 'attempt_id': 'a' * 32, 'created_at': now - 5, 'expires_at': now + 3600,
            'uid': 499, 'gid': 499, 'code_manifest_sha256': 'b' * 64, 'old_package_sha256': '1' * 64,
            'new_package_sha256': '2' * 64, 'current_package_sha256': state['current'],
            'context_sha256': '3' * 64, 'publisher_sha256': '4' * 64, 'registry_sha256': '5' * 64,
            'old_control_sha256': '6' * 64, 'old_plist_sha256': '7' * 64, 'new_plist_sha256': '8' * 64,
            'state_before_sha256': boot.digest(before), 'state_stopped_sha256': boot.digest(stopped)}
    slot = base / ('system-upgrade-' + plan['attempt_id']); slot.mkdir(mode=0o755); slot.chmod(0o755)
    plan_path = slot / 'plan.json'; plan_path.write_bytes(contract.canonical(plan)); plan_path.chmod(0o644)
    request = {'type': contract.REQUEST, 'attempt_id': plan['attempt_id'], 'plan_sha256': boot.digest(plan_path.read_bytes()),
               'phase': 'stop', 'challenge': 'd' * 32, 'lock_fd': None}
    context = {'uid': os.getuid(), 'gid': os.getgid()}
    for module in (worker, manage):
        monkeypatch.setattr(module, 'BASE', base); monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(contract, 'BASE', str(base))
    original_lease = restore.Lease
    def lease(path, **kwargs):
        if kwargs['uid'] == 0:
            kwargs['uid'] = os.getuid()
        return original_lease(path, **kwargs)
    monkeypatch.setattr(restore, 'Lease', lease); monkeypatch.setattr(manage, 'Lease', lease)
    monkeypatch.setattr(worker, '_entry', lambda: None)
    auth = Mock(return_value=(context, b'synthetic public'))
    monkeypatch.setattr(worker, 'authenticated', auth)
    monkeypatch.setattr(boot, '_actual', lambda: boot._expected(499, 499))
    manager = manager_module.Manager(root, b'synthetic public')
    executable = Path(sys.executable).resolve()
    approval = Mock(); monkeypatch.setattr(manager, '_approve_execution', approval)
    monkeypatch.setattr(manager, '_runtime_python', lambda identity: executable)
    stop = Mock(wraps=manager.stop); monkeypatch.setattr(manager, 'stop', stop)
    monkeypatch.setattr(manager, 'start', Mock(side_effect=AssertionError('must never start')))
    constructor = Mock(return_value=manager); monkeypatch.setattr(manager_module, 'Manager', constructor)
    canary = Mock(); monkeypatch.setattr(system, 'verify_keychain', canary)
    launch = Mock(side_effect=AssertionError('worker cannot use launchctl')); monkeypatch.setattr(system, 'launch', launch)
    forbidden = []
    for name in ('_contract', 'before_write', 'prepare_destination', 'verified_at'):
        guard = Mock(side_effect=AssertionError('worker cannot access NAS'))
        monkeypatch.setattr(manage.remote, name, guard); forbidden.append(guard)
    def run(phase='stop', fd=None):
        return worker.run({**request, 'phase': phase, 'lock_fd': fd}, script='synthetic', executable='synthetic')
    return SimpleNamespace(base=base, root=root, slot=slot, plan_path=plan_path, plan=plan, request=request,
        context=context, before=before, stopped=stopped, state=state, manager=manager, stop=stop, canary=canary,
        auth=auth, approval=approval, constructor=constructor, forbidden=forbidden, launch=launch, run=run,
        lock_path=lock_path, contract=contract, boot=boot, manage=manage)


def test_stop_changes_only_manual_stop_preserves_history_without_canary_or_nas(case):
    c = case
    c.canary.side_effect = ValueError('synthetic inaccessible canary secret')
    result = c.run()
    assert (c.root / 'state.json').read_bytes() == c.stopped
    assert result['state_before_sha256'] == c.plan['state_before_sha256']
    assert result['state_after_sha256'] == c.plan['state_stopped_sha256']
    assert result['manual_stop'] is result['process_stopped'] is True
    assert result['keychain_canary_verified'] is result['borrowed_lock'] is False
    assert 'private synthetic note' not in json.dumps(result)
    c.stop.assert_called_once(); c.canary.assert_not_called(); c.launch.assert_not_called()
    for guard in c.forbidden: guard.assert_not_called()
    assert c.auth.call_count >= 4


@contextmanager
def parent_lock(c, *, writable=False, locked=True):
    import fcntl
    with c.lock_path.open('r+b' if writable else 'rb') as held:
        if locked: fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield held.fileno()


def assert_parent_still_locked(c):
    import fcntl
    with c.lock_path.open('rb') as contender:
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_verify_borrows_original_lock_checks_canary_and_is_readonly(case):
    c = case; c.run(); c.stop.reset_mock()
    original = {str(p): p.read_bytes() for p in c.base.rglob('*') if p.is_file()}
    with parent_lock(c) as fd:
        result = c.run('verify', fd)
        assert_parent_still_locked(c)
    assert result['keychain_canary_verified'] is result['borrowed_lock'] is True
    assert result['state_before_sha256'] == c.plan['state_stopped_sha256']
    assert {str(p): p.read_bytes() for p in c.base.rglob('*') if p.is_file()} == original
    c.stop.assert_not_called(); c.canary.assert_called_once_with(c.context)


@pytest.mark.parametrize('fault', ['unlocked', 'writable', 'different_inode', 'closed', 'bad_content'])
def test_invalid_inherited_lock_cannot_become_a_new_transaction(case, fault):
    c = case; c.run()
    with parent_lock(c, writable=fault == 'writable', locked=fault != 'unlocked') as fd:
        if fault == 'different_inode':
            other = c.root / 'other.lock'; other.write_bytes(b'0'); other.chmod(0o600)
            extra = os.open(other, os.O_RDONLY)
            descriptor = extra
        elif fault == 'closed':
            extra = None; descriptor = 2**20
        else:
            extra = None; descriptor = fd
        if fault == 'bad_content':
            c.lock_path.write_bytes(b'x')
        try:
            with pytest.raises((ValueError, OSError)):
                c.run('verify', descriptor)
        finally:
            if extra is not None: os.close(extra)
        if fault not in ('unlocked', 'bad_content'): assert_parent_still_locked(c)
    c.canary.assert_not_called()


@pytest.mark.parametrize('fault', ['before', 'current', 'native', 'stopped_binding', 'unknown_phase', 'extra', 'bool_fd', 'stop_fd'])
def test_wrong_request_or_state_binding_rejected_without_stop(case, fault):
    c = case; request = deepcopy(c.request)
    if fault in ('before', 'current', 'native'):
        value = deepcopy(c.state)
        if fault == 'before': value['retained'].append('changed')
        elif fault == 'current': value['current'] = 'f' * 64
        else: value['native_restore_binding']['package_sha256'] = 'f' * 64
        (c.root / 'state.json').write_bytes(c.boot.canonical(value))
    elif fault == 'stopped_binding':
        c.plan['state_stopped_sha256'] = 'f' * 64
        c.plan_path.write_bytes(c.contract.canonical(c.plan))
        request['plan_sha256'] = c.boot.digest(c.plan_path.read_bytes())
    elif fault == 'unknown_phase': request['phase'] = 'run'
    elif fault == 'extra': request['path'] = '/unapproved'
    elif fault == 'bool_fd': request.update(phase='verify', lock_fd=True)
    else: request['lock_fd'] = 5
    before = (c.root / 'state.json').read_bytes()
    with pytest.raises(ValueError): worker.run(request, script='synthetic', executable='synthetic')
    assert (c.root / 'state.json').read_bytes() == before
    c.stop.assert_not_called(); c.canary.assert_not_called()


@pytest.mark.parametrize('fault', ['hash', 'expired', 'future', 'duplicate', 'large', 'mode', 'plan_link', 'slot_link'])
def test_bad_root_plan_refused_before_instance_constructor(case, fault):
    c = case
    if fault in ('expired', 'future'):
        delta = -7200 if fault == 'expired' else 7200
        c.plan['created_at'] += delta; c.plan['expires_at'] += delta
        c.plan_path.write_bytes(c.contract.canonical(c.plan))
        c.request['plan_sha256'] = c.boot.digest(c.plan_path.read_bytes())
    elif fault == 'hash': c.request['plan_sha256'] = 'f' * 64
    elif fault == 'duplicate':
        c.plan_path.write_bytes(b'{"type":1,"type":2}')
        c.request['plan_sha256'] = c.boot.digest(c.plan_path.read_bytes())
    elif fault == 'large': c.plan_path.write_bytes(b'x' * 65537)
    elif fault == 'mode': c.plan_path.chmod(0o600)
    elif fault == 'plan_link':
        saved = c.slot / 'saved'; c.plan_path.rename(saved); c.plan_path.symlink_to(saved)
    else:
        saved = c.base / 'saved-slot'; c.slot.rename(saved); c.slot.symlink_to(saved, target_is_directory=True)
    with pytest.raises((ValueError, OSError)): c.run()
    c.constructor.assert_not_called(); c.stop.assert_not_called()


@pytest.mark.parametrize('fault', ['stop_failure', 'state_drift', 'plan_drift', 'approval_drift', 'still_running'])
def test_failure_after_cas_preserves_stopped_state_and_does_not_restart(case, monkeypatch, fault):
    c = case
    def stop():
        if fault == 'stop_failure': raise ValueError('synthetic stop failure')
        if fault == 'state_drift':
            value = json.loads(c.stopped); value['retained'].append('external change')
            (c.root / 'state.json').write_bytes(c.boot.canonical(value))
        if fault == 'plan_drift': c.plan_path.write_bytes(b'{}')
        if fault == 'approval_drift': c.auth.side_effect = ValueError('root approval changed')
        if fault == 'still_running': monkeypatch.setattr(c.manager, '_process', lambda: (object(), {'exe': sys.executable}))
    c.stop.side_effect = stop
    with pytest.raises(ValueError): c.run()
    value = json.loads((c.root / 'state.json').read_bytes())
    assert value['manual_stop'] is True and value['current'] == c.state['current']
    c.manager.start.assert_not_called(); c.canary.assert_not_called()


def test_verify_failure_does_not_unlock_parent_or_modify_state(case):
    c = case; c.run(); c.canary.side_effect = ValueError('synthetic canary failure')
    with parent_lock(c) as fd:
        with pytest.raises(ValueError): c.run('verify', fd)
        assert_parent_still_locked(c)
    assert (c.root / 'state.json').read_bytes() == c.stopped


@pytest.mark.parametrize('fault', [None, 'duplicate', 'nonfinite', 'too_large'])
def test_state_uses_strict_existing_16mib_bound_not_public_record_limit(case, fault):
    c = case
    value = {**c.state, 'large_inventory': 'x' * 70000}
    raw = c.boot.canonical(value)
    if fault == 'duplicate': raw = raw[:-1] + b',"manual_stop":true}'
    elif fault == 'nonfinite': raw = raw[:-1] + b',"bad":NaN}'
    elif fault == 'too_large': raw = b' ' * (c.manage.STATE_LIMIT + 1)
    c.plan['state_before_sha256'] = c.boot.digest(raw)
    c.plan['state_stopped_sha256'] = c.boot.digest(c.boot.canonical({**value, 'manual_stop': True}))
    c.plan_path.write_bytes(c.contract.canonical(c.plan))
    c.request['plan_sha256'] = c.boot.digest(c.plan_path.read_bytes())
    (c.root / 'state.json').write_bytes(raw)
    if fault is None:
        assert c.run()['process_stopped'] is True
        assert json.loads((c.root / 'state.json').read_bytes()) == {**value, 'manual_stop': True}
    else:
        with pytest.raises(ValueError): c.run()
        c.stop.assert_not_called()
        assert (c.root / 'state.json').read_bytes() == raw
    c.canary.assert_not_called()


def test_inherited_lock_across_real_isolated_subprocess_does_not_unlock_parent(case):
    c = case
    # The child borrows only a synthetic lock; no service run or identity mock
    # occurs in that process. Its actual -I -B startup ignores user PYTHONPATH.
    code = '''import os,pathlib,sys
sys.path.insert(0,sys.argv[1])
from desktop_assistant.mac_system_upgrade_worker import _held_maintenance
from types import SimpleNamespace
lock=SimpleNamespace(fd=int(sys.argv[2]),path=pathlib.Path(sys.argv[3]),check=lambda:None)
with _held_maintenance(lock,borrowed=True): pass
os.close(lock.fd)
'''
    with parent_lock(c) as fd:
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code,
            str(Path(worker.__file__).resolve().parent.parent), str(fd), str(c.lock_path)],
            pass_fds=(fd,), capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr.decode()
        assert result.stdout == b''
        assert_parent_still_locked(c)


def test_real_synthetic_managed_child_is_gracefully_stopped(case):
    c = case
    import psutil
    nonce = '9' * 32
    ready = c.root / 'child-ready'
    code = "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); pathlib.Path(sys.argv[2]).touch()\nwhile not p.exists(): time.sleep(.01)\n"
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code,
                              str(c.root / 'control' / (nonce + '.stop')), str(ready)])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline: time.sleep(.01)
        assert ready.exists()
        proc = psutil.Process(child.pid)
        from desktop_assistant.storage import write_json
        write_json(c.root / 'control/process.json', {'pid': child.pid, 'created': proc.create_time(),
                   'exe': proc.exe(), 'nonce': nonce})
        c.manager._track_child(child)
        assert c.run()['process_stopped'] is True
        assert child.wait(timeout=2) == 0
        assert (c.root / 'control/process.json').exists()
    finally:
        if child.poll() is None: child.terminate(); child.wait(timeout=5)


@pytest.mark.parametrize('flags', [[], ['-I'], ['-I', '-B']])
def test_actual_fixed_entry_subprocess_refuses_before_any_root_io(flags, tmp_path):
    script = Path(worker.__file__).resolve()
    # No real UID499 transition: this is actual fixed-file bootstrap rejection,
    # not a claim of privileged successful execution.
    result = subprocess.run([sys.executable, *flags, str(script), '--secret', 'never-echo-this'],
                            cwd=tmp_path, capture_output=True, timeout=10)
    assert result.returncode != 0 and result.stdout == b''
    assert result.stderr.decode().strip() == worker.FAILURE
    assert b'never-echo-this' not in result.stderr and b'Traceback' not in result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('argv', [[], ['--path', '/tmp'], ['--attempt', 'a'] * 4,
    ['--attempt', 'a' * 32, '--plan-sha256', 'b' * 64, '--challenge', 'c' * 32, '--phase', 'stop', '--secret', 'hidden']])
def test_argument_parser_has_no_arbitrary_path_command_or_secret(argv):
    with pytest.raises(ValueError): worker._arguments(argv)


def test_argument_parser_refuses_abbreviated_flags():
    with pytest.raises(ValueError):
        worker._arguments(['--att', 'a' * 32, '--plan-sha256', 'b' * 64,
                           '--challenge', 'c' * 32, '--phase', 'stop'])


@pytest.fixture
def signed_case(monkeypatch, request):
    if sys.platform != 'darwin':
        pytest.skip('Mac signed publication protocol with simulated root/UID metadata')
    import zipfile
    from tests.desktop_assistant import test_mac_published_runtime as fixture_module
    from desktop_assistant import mac_system_upgrade_contract as contract
    from desktop_assistant import mac_system_service as system, mac_keychain_install as boot
    from desktop_assistant import mac_published_runtime as published, mac_release_publish as publication
    from desktop_assistant import mac_service_manage as manage, mac_service_restore as restore
    from desktop_assistant.storage import read_json, write_json, sha
    from app.core import mac_service_keychain as keychain
    original_pack = fixture_module.pack_tree
    def pack(source, destination, metadata, signer):
        metadata = deepcopy(metadata)
        metadata['runtime_capabilities']['system_service_v1'] = 1
        capability = getattr(request, 'param', 1)
        if metadata['version'] != 'native' and capability is not None:
            metadata['runtime_capabilities'][worker.CAPABILITY] = capability
        for name in (worker.MODULE, worker.CONTRACT_MODULE, 'desktop_assistant/manager.py',
                     'desktop_assistant/mac_service_manage.py', system.ENTRY, 'desktop_assistant/mac_system_service.py'):
            path = source / name; path.parent.mkdir(exist_ok=True)
            path.write_text('# signed synthetic module, never executed\n')
        return original_pack(source, destination, metadata, signer)
    monkeypatch.setattr(fixture_module, 'pack_tree', pack)
    generator = fixture_module.case.__wrapped__(monkeypatch)
    fixture = next(generator)
    try:
        base = published.BASE; root = fixture.manager.root
        context = {'type': keychain.TYPE, 'uid': 499, 'gid': 499, 'installation_id': 'a' * 32, 'host_sha256': 'b' * 64}
        fixture.context.write_bytes(boot.canonical(context))
        new = fixture.publish('new-worker')
        old = fixture.initial
        # Move the private fixture instance under the actual fixed parent shape.
        # Retarget only the declared managed links in its synthetic server trees.
        root.rename(base / 'instance'); root = base / 'instance'
        fixture.manager.root = root
        monkeypatch.setattr(published, 'ROOT', root)
        from desktop_assistant.release_verification import MANAGED
        for release in (old, new):
            for relative, folder in MANAGED.items():
                link = release.slot / 'server' / relative
                link.unlink(); link.symlink_to(root / 'shared' / folder, target_is_directory=True)
        # The reused publication fixture has deliberately generic filenames;
        # this integration exercises the system service's actual fixed names.
        fixed_context = base / 'keychain-context.json'
        fixed_context.write_bytes(fixture.context.read_bytes())
        fixture.context = fixed_context
        monkeypatch.setattr(keychain, 'CONTEXT', fixed_context)
        fixed_public = base / 'publisher-public.pem'
        fixed_public.write_bytes(fixture.public)
        monkeypatch.setattr(published, 'PUBLIC', fixed_public)
        for release in (old, new):
            with zipfile.ZipFile(release.package) as archive: raw = archive.read('manifest.json')
            for kind in ('server', 'migration'): (release.slot / kind / 'manifest.json').write_bytes(raw)
        for module in (worker, system, manage):
            monkeypatch.setattr(module, 'BASE', base); monkeypatch.setattr(module, 'ROOT', root)
        monkeypatch.setattr(contract, 'BASE', str(base))
        monkeypatch.setattr(system, 'CONTROL', base / 'system-service.json')
        plists = base / 'plists'; plists.mkdir(); monkeypatch.setattr(system, 'PLISTS', plists)
        monkeypatch.setattr(system, 'PLIST', plists / (system.LABEL + '.plist'))
        monkeypatch.setattr(boot, 'modules', lambda: (SimpleNamespace(_parent=lambda: None), keychain))
        monkeypatch.setattr(boot, 'protected', lambda p, **kw: b'' if kw.get('directory') else Path(p).read_bytes())
        monkeypatch.setattr(publication, 'guard', lambda *a, **kw: None)
        monkeypatch.setattr(keychain, 'load_context', lambda: deepcopy(context))
        monkeypatch.setattr(keychain, 'selected', lambda: True)
        monkeypatch.setattr(worker, '_entry', lambda: None)
        monkeypatch.setattr(boot, '_actual', lambda: boot._expected(499, 499))
        monkeypatch.setattr(worker, '__file__', str(new.slot / 'migration' / worker.MODULE))
        executable = new.slot / 'migration/runtime/bin/python3.12'
        monkeypatch.setattr(sys, 'executable', str(executable))
        # The fixed UID/filesystem protection is adapted; signature verification,
        # both full trees, public registry and fixed control/plist remain real.
        original_lease = restore.Lease
        def lease(path, **kwargs):
            if kwargs['uid'] in (0, 499): kwargs['uid'] = os.getuid()
            if kwargs['gid'] == 499: kwargs['gid'] = os.getgid()
            return original_lease(path, **kwargs)
        monkeypatch.setattr(restore, 'Lease', lease); monkeypatch.setattr(manage, 'Lease', lease)
        for path in (base,): path.chmod(0o755)
        for name in ('', 'releases', 'shared', 'control', 'backups', 'packages', 'staging'):
            (root / name).chmod(0o700)
        lock = root / 'control/operation.lock'; lock.write_bytes(b'0'); lock.chmod(0o600)
        state = fixture.manager.state; state.update(manual_stop=False, operation='restore', original_history={'retain': 1})
        write_json(root / 'state.json', state); (root / 'state.json').chmod(0o600)
        before = (root / 'state.json').read_bytes(); stopped = boot.canonical({**state, 'manual_stop': True})
        now = int(time.time()); publisher = sha(published.PUBLIC)
        old_plist = system._plist(old.id, publisher); new_plist = system._plist(new.id, publisher)
        plan = {'type': contract.PLAN, 'attempt_id': 'a' * 32, 'created_at': now - 5, 'expires_at': now + 3600,
                'uid': 499, 'gid': 499, 'code_manifest_sha256': 'b' * 64,
                'old_package_sha256': old.id, 'new_package_sha256': new.id, 'current_package_sha256': old.id,
                'context_sha256': sha(fixture.context), 'publisher_sha256': publisher,
                'registry_sha256': sha(published.REGISTRY), 'old_control_sha256': 'c' * 64,
                'old_plist_sha256': boot.digest(old_plist), 'new_plist_sha256': boot.digest(new_plist),
                'state_before_sha256': boot.digest(before), 'state_stopped_sha256': boot.digest(stopped)}
        slot = base / ('system-upgrade-' + plan['attempt_id']); slot.mkdir(mode=0o755); slot.chmod(0o755)
        plan_file = slot / 'plan.json'; plan_file.write_bytes(contract.canonical(plan)); plan_file.chmod(0o644)
        request = {'type': contract.REQUEST, 'attempt_id': plan['attempt_id'], 'plan_sha256': sha(plan_file),
                   'challenge': 'd' * 32, 'phase': 'stop', 'lock_fd': None}
        system.CONTROL.write_bytes(contract.disabled_control_bytes(plan, 'stop'))
        system.PLIST.write_bytes(old_plist)
        canary = Mock(); monkeypatch.setattr(system, 'verify_keychain', canary)
        monkeypatch.setattr(system, 'launch', Mock(side_effect=AssertionError('no launchctl')))
        monkeypatch.setenv('ERP_HOME_REHEARSAL', '1')
        def authenticate():
            return worker.authenticated(request, plan, script=new.slot / 'migration' / worker.MODULE, executable=executable)
        yield SimpleNamespace(fixture=fixture, base=base, root=root, old=old, new=new, context=context,
            plan=plan, plan_file=plan_file, request=request, executable=executable, authenticate=authenticate,
            system=system, published=published, boot=boot, contract=contract, stopped=stopped, before=before,
            canary=canary, lock_path=lock, new_plist=new_plist)
    finally:
        try: next(generator)
        except StopIteration: pass


def test_real_signed_old_and_new_publication_through_stop_then_verify(signed_case):
    c = signed_case
    assert c.authenticate() == (c.context, c.fixture.public)
    assert worker.run(c.request, script=worker.__file__, executable=c.executable)['process_stopped'] is True
    assert (c.root / 'state.json').read_bytes() == c.stopped
    c.canary.assert_not_called()
    c.system.CONTROL.write_bytes(c.contract.disabled_control_bytes(c.plan, 'verify'))
    c.system.PLIST.write_bytes(c.new_plist)
    with parent_lock(c) as fd:
        reply = worker.run({**c.request, 'phase': 'verify', 'lock_fd': fd},
                           script=worker.__file__, executable=c.executable)
        assert_parent_still_locked(c)
    assert reply['keychain_canary_verified'] is True
    c.canary.assert_called_once_with(c.context)


@pytest.mark.parametrize('signed_case', [None, True, '1', 0, 2], indirect=True)
def test_new_signed_worker_requires_exact_declared_capability(signed_case):
    c = signed_case
    with pytest.raises(ValueError, match='固定的新签名入口'): c.authenticate()
    assert (c.root / 'state.json').read_bytes() == c.before
    c.canary.assert_not_called()


@pytest.mark.parametrize('fault', ['actual_exe', 'argument_exe', 'script', 'module', 'groups', 'uid',
    'context', 'registry', 'old_unapproved', 'old_package', 'old_code', 'new_code',
    'enabled_control', 'wrong_control', 'wrong_plist', 'factory_record', 'factory_link'])
def test_real_signed_authentication_refuses_wrong_identity_or_root_binding(signed_case, monkeypatch, fault):
    c = signed_case; script = Path(worker.__file__); executable = c.executable
    assert c.authenticate() == (c.context, c.fixture.public)
    if fault == 'actual_exe': monkeypatch.setattr(sys, 'executable', '/wrong/python')
    elif fault == 'argument_exe': executable = '/wrong/python'
    elif fault == 'script': script = '/wrong/entry'
    elif fault == 'module': monkeypatch.setattr(worker, '__file__', '/wrong/module')
    elif fault in ('groups', 'uid'):
        value = c.boot._expected(499, 499)
        if fault == 'groups': value['kernel_groups'] = [499, 80]
        else: value['uid'] = 0
        monkeypatch.setattr(c.boot, '_actual', lambda: value)
    elif fault == 'context': c.fixture.context.write_bytes(b'{}')
    elif fault in ('registry', 'old_unapproved'):
        value = json.loads(c.published.REGISTRY.read_bytes())
        if fault == 'old_unapproved': value['packages'].remove(c.old.id)
        else: value['publisher_sha256'] = 'f' * 64
        c.published.REGISTRY.write_bytes(c.boot.canonical(value))
        if fault == 'old_unapproved': c.plan['registry_sha256'] = c.boot.digest(c.published.REGISTRY.read_bytes())
    elif fault == 'old_package': (c.old.slot / 'package.zip').write_bytes(b'tampered')
    elif fault in ('old_code', 'new_code'):
        release = c.old if fault == 'old_code' else c.new
        (release.slot / 'migration/main.py').write_bytes(b'tampered')
    elif fault in ('enabled_control', 'wrong_control'):
        value = c.contract.disabled_control(c.plan, 'stop')
        if fault == 'enabled_control': value['enabled'] = True
        else: value['package_sha256'] = c.new.id
        c.system.CONTROL.write_bytes(c.boot.canonical(value))
    elif fault == 'wrong_plist': c.system.PLIST.write_bytes(c.new_plist)
    elif fault == 'factory_record': (c.base / 'factory-activation.json').write_bytes(b'broken')
    else: (c.base / 'factory-activation.json').symlink_to(c.base / 'absent')
    messages = {
        **{k: '固定的新签名入口' for k in ('actual_exe', 'argument_exe', 'script', 'module')},
        'groups': c.contract.ERROR, 'uid': c.contract.ERROR,
        'context': '身份完成记录无效', 'registry': 'root批准记录', 'old_unapproved': 'root批准记录',
        'old_package': 'root原包摘要不符', 'old_code': '完整执行树逐项回读不符',
        'new_code': '完整执行树逐项回读不符', 'enabled_control': 'root启动门禁',
        'wrong_control': 'root启动门禁', 'wrong_plist': '固定plist不符',
        'factory_record': '尚未正式接管', 'factory_link': '尚未正式接管'}
    with pytest.raises(ValueError, match=messages[fault]):
        worker.authenticated(c.request, c.plan, script=script, executable=executable)
    assert (c.root / 'state.json').read_bytes() == c.before
    c.canary.assert_not_called()
