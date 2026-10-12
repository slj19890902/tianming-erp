"""Isolated root-transaction tests; never operate a production installation."""
import importlib.util
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import ast
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_assistant import mac_system_upgrade as upgrade
from desktop_assistant import mac_system_upgrade_contract as contract


def test_root_transaction_exists_without_deployment():
    assert importlib.util.find_spec('desktop_assistant.mac_system_upgrade') is not None


def put(path, data, mode=0o600):
    path.write_bytes(data); path.chmod(mode)


@pytest.fixture
def case(tmp_path, monkeypatch):
    # Actual no-follow dir-FD and flock IO is POSIX; root/UID/launchd are mocks.
    if os.name != 'posix':
        pytest.skip('POSIX FD transaction IO; portable codecs and adapters run separately')
    import fcntl
    boot, system, publication = upgrade.boot, upgrade.system, upgrade.publication
    base = tmp_path / 'system'; base.mkdir(mode=0o755); base.chmod(0o755)
    root = base / 'instance'; root.mkdir(mode=0o700)
    control = root / 'control'; control.mkdir(mode=0o700)
    plists = tmp_path / 'LaunchDaemons'; plists.mkdir(mode=0o755); plists.chmod(0o755)
    for module in (upgrade, system, publication): monkeypatch.setattr(module, 'BASE', base)
    monkeypatch.setattr(upgrade, 'ROOT', root); monkeypatch.setattr(system, 'ROOT', root)
    monkeypatch.setattr(upgrade, 'DATA', tmp_path)
    monkeypatch.setattr(contract, 'BASE', str(base))
    monkeypatch.setattr(system, 'CONTROL', base / 'system-service.json')
    monkeypatch.setattr(system, 'PLISTS', plists)
    monkeypatch.setattr(system, 'PLIST', plists / (system.LABEL + '.plist'))
    monkeypatch.setattr(upgrade, '_entry', lambda: None)
    monkeypatch.setattr(upgrade, '_owned', lambda info, uid, gid: info.st_uid == os.getuid() and info.st_gid == os.getgid())
    no_acl = Mock(); monkeypatch.setattr(boot, 'no_acl', no_acl)
    monkeypatch.setattr(boot, 'protected', lambda path, **kw: None if kw.get('directory') else Path(path).read_bytes())
    monkeypatch.setattr(publication, 'read_record', lambda path: Path(path).read_bytes())
    code = 'b' * 64; monkeypatch.setattr(upgrade, 'verify_code', lambda: code)
    trace = []
    @contextmanager
    def publication_lock():
        with (base / 'publication.lock').open('rb') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            trace.append('publication-enter')
            try: yield
            finally: trace.append('publication-exit'); fcntl.flock(held, fcntl.LOCK_UN)
    monkeypatch.setattr(publication, 'lock', publication_lock)
    put(base / 'publication.lock', b'publication lock\n')
    for name in ('operation.lock', 'service-supervisor.lock'): put(control / name, b'0')
    for name in ('stdout', 'stderr'): put(base / ('system-service.' + name + '.log'), b'prior\n', 0o660)
    state = {'current': 'c' * 64, 'manual_stop': False, 'operation': 'restore',
        'native_restore_binding': {'package_sha256': 'd' * 64, 'factory_cutover': False},
        'restored_at': '2026-10-10T12:01:00+08:00', 'private_note': ['preserve', 1.25]}
    before = json.dumps(state, indent=2).encode(); put(root / 'state.json', before)
    stopped = boot.canonical({**state, 'manual_stop': True})
    context = {'uid': 499, 'gid': 499}
    registry = boot.canonical({'packages': ['1' * 64, '2' * 64, state['current']]})
    put(base / 'published.json', registry, 0o644)
    metas = {}
    for package in ('1' * 64, '2' * 64):
        tree = base / 'published' / package / 'migration'
        (tree / 'runtime/bin').mkdir(parents=True)
        put(tree / 'runtime/bin/python3.12', b'synthetic interpreter', 0o755)
        metas[package] = {'package': package, 'context': context, 'public': b'public', 'tree': tree,
            'context_sha256': '3' * 64, 'publisher_sha256': '4' * 64,
            'registry_sha256': boot.digest(registry)}
    monkeypatch.setattr(system, 'metadata', lambda package: deepcopy(metas[package]))
    manifest = {'runtime_capabilities': {upgrade.CAPABILITY: 1}, 'files': {name: 'a' * 64 for name in
        (upgrade.WORKER, upgrade.CONTRACT_MODULE, 'desktop_assistant/manager.py', 'desktop_assistant/mac_service_manage.py')}}
    monkeypatch.setattr(system, '_program_manifest', lambda meta: deepcopy(manifest))
    installer = SimpleNamespace(permitted_groups=lambda _: {499, 12, 61, 100, 701})
    keychain = SimpleNamespace(_identity=Mock())
    monkeypatch.setattr(boot, 'modules', lambda: (installer, keychain))
    old = metas['1' * 64]; new = metas['2' * 64]
    control_raw = boot.canonical(system.record_for(old, True))
    put(system.CONTROL, control_raw, 0o644); plist_raw = system._plist(old['package'], old['publisher_sha256'])
    put(system.PLIST, plist_raw, 0o644)
    now = int(time.time())
    plan = {'type': contract.PLAN, 'attempt_id': 'a' * 32, 'created_at': now - 5, 'expires_at': now + 3600,
        'uid': 499, 'gid': 499, 'code_manifest_sha256': code, 'old_package_sha256': old['package'],
        'new_package_sha256': new['package'], 'current_package_sha256': state['current'],
        **{key: old[key] for key in ('context_sha256', 'publisher_sha256', 'registry_sha256')},
        'old_control_sha256': boot.digest(control_raw), 'old_plist_sha256': boot.digest(plist_raw),
        'new_plist_sha256': boot.digest(system._plist(new['package'], new['publisher_sha256'])),
        'state_before_sha256': boot.digest(before), 'state_stopped_sha256': boot.digest(stopped)}
    raw = contract.canonical(plan); sha = boot.digest(raw)
    events = {}; identities = {}
    def identity(pid, meta, born):
        micro = int(born * 1000000)
        return dict(pid=pid, ppid=1, uid=499, gid=499, ruid=499, rgid=499,
                    birth_sec=micro // 1000000, birth_usec=micro % 1000000,
                    exe=str((meta['tree'] / 'runtime/bin/python3.12').resolve()))
    old_identity = identity(10001, old, time.time() - 60)
    identities[10001] = old_identity
    runtime = {'job': {'state': 'running', 'pid': 10001}}
    monkeypatch.setattr(upgrade, '_job', lambda: deepcopy(runtime['job']))
    def process_identity(pid):
        if pid not in identities: raise ProcessLookupError('synthetic vanished process')
        return deepcopy(identities[pid])
    monkeypatch.setattr(upgrade, 'process_identity', process_identity)
    monkeypatch.setattr(upgrade, '_service_pids', lambda: frozenset(identities))
    class Watch:
        def __init__(self, value):
            assert process_identity(value['pid']) == value
            self.seen = False; events['watch'] = self; trace.append('watch-register')
        def poll(self, timeout=0): return self.seen
        def __enter__(self): return self
        def __exit__(self, *_): trace.append('watch-close')
    monkeypatch.setattr(upgrade, 'ExitWatch', Watch)
    def locked(name):
        with (control / name).open('rb') as stream:
            try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: return True
            else: fcntl.flock(stream, fcntl.LOCK_UN); return False
    def launch(action):
        trace.append(action)
        assert locked('operation.lock') is (action in ('enable', 'bootstrap')) or action == 'disable'
        if action == 'bootout':
            events['watch'].seen = True; identities.pop(10001); runtime['job'] = None
        elif action == 'bootstrap':
            assert not locked('service-supervisor.lock')
            value = identity(10002, new, time.time()); identities[10002] = value
            runtime['job'] = {'state': 'running', 'pid': 10002}
            status = {'status': 'manually-stopped', 'pid': 10002, 'configured_mode': 'home',
                'home_rehearsal': True, 'updated_at': datetime.now(timezone.utc).isoformat(),
                'requires_user_login': False, 'keychain_canary_verified': True,
                'system_identity': {'uid': 499, 'euid': 499, 'gid': 499, 'egid': 499,
                                    'kernel_groups': [499, 12, 61, 100, 701]}}
            put(control / 'service-status.json', boot.canonical(status))
    monkeypatch.setattr(upgrade, '_launch', launch)
    def worker(value, digest, meta, phase, fd=None):
        trace.append(phase)
        assert meta == new and value == plan and digest == sha
        assert system.CONTROL.read_bytes() == contract.disabled_control_bytes(plan, phase)
        assert locked('operation.lock') is (phase == 'verify')
        if phase == 'stop':
            assert fd is None and not locked('service-supervisor.lock')
            put(root / 'state.json', stopped)
        else:
            assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            assert locked('service-supervisor.lock')
        return {'phase': phase, 'synthetic_worker_reply': True}
    monkeypatch.setattr(upgrade, '_worker', worker)
    c = SimpleNamespace(base=base, root=root, control=control, plists=plists, plan=plan, raw=raw, sha=sha,
        slot=base / ('system-upgrade-' + plan['attempt_id']), trace=trace, manifest=manifest,
        old=old, new=new, metas=metas, before=before, stopped=stopped, state=state,
        control_raw=control_raw, plist_raw=plist_raw, identities=identities, old_identity=old_identity,
        events=events, runtime=runtime, launch=launch, worker=worker, locked=locked, no_acl=no_acl)
    def prepare(): return upgrade.prepare(c.raw, c.sha)
    def apply(): return upgrade.apply(c.plan['attempt_id'], c.sha)
    c.prepare, c.apply = prepare, apply
    return c


def test_full_transaction_keeps_state_history_and_logs(case):
    c = case
    logs = {p.name: (p.stat().st_ino, p.read_bytes()) for p in c.base.glob('*.log')}
    c.prepare(); result = c.apply()
    assert result['current_package_sha256'] == c.state['current']
    assert result['old_kernel_exit_verified'] and result['system_keychain_canary_verified']
    assert result['manual_stop'] and not result['erp_started'] and not result['factory_activated']
    assert (c.root / 'state.json').read_bytes() == c.stopped
    assert json.loads(c.stopped)['native_restore_binding'] == c.state['native_restore_binding']
    assert logs == {p.name: (p.stat().st_ino, p.read_bytes()) for p in c.base.glob('*.log')}
    assert (c.slot / 'old-control.json').read_bytes() == c.control_raw
    assert (c.slot / 'old.plist').read_bytes() == c.plist_raw
    assert (c.slot / 'pending.json').exists() and (c.slot / 'verified.json').exists()
    assert not (c.slot / 'failed.json').exists()
    assert [step for step in c.trace if step not in {'publication-enter', 'publication-exit', 'watch-register', 'watch-close'}] == [
        'disable', 'stop', 'bootout', 'verify', 'enable', 'bootstrap']
    assert not c.locked('operation.lock') and not c.locked('service-supervisor.lock')
    assert c.no_acl.call_count > 100
    assert 'private_note' not in ''.join(p.read_text() for p in c.slot.iterdir())


def test_make_plan_is_readonly_and_exact_state_binding(case):
    c = case
    before = {str(p): p.read_bytes() for p in c.base.rglob('*') if p.is_file()}
    value = upgrade.make_plan(c.old['package'], c.new['package'])
    assert value['state_before_sha256'] == c.plan['state_before_sha256']
    assert value['state_stopped_sha256'] == c.plan['state_stopped_sha256']
    assert before == {str(p): p.read_bytes() for p in c.base.rglob('*') if p.is_file()}
    assert set(value) == contract.PLAN_FIELDS


@pytest.mark.parametrize('field', ['context_sha256', 'publisher_sha256', 'registry_sha256', 'code_manifest_sha256',
    'current_package_sha256', 'state_before_sha256', 'state_stopped_sha256', 'old_control_sha256', 'old_plist_sha256', 'new_plist_sha256'])
def test_preparation_rejects_changed_approval_before_mutation(case, field):
    c = case; c.plan[field] = 'f' * 64; c.raw = contract.canonical(c.plan); c.sha = contract.digest(c.raw)
    with pytest.raises((ValueError, KeyError)): c.prepare()
    assert not c.slot.exists() and (c.root / 'state.json').read_bytes() == c.before
    assert 'disable' not in c.trace


@pytest.mark.parametrize('fault', ['unknown_slot', 'again', 'state', 'plan', 'control', 'plist', 'pending', 'activation', 'log_mode', 'log_inode', 'capability', 'registry'])
def test_apply_initial_rejection_changes_no_service(case, fault):
    c = case; c.prepare()
    if fault == 'unknown_slot': put(c.slot / 'unexpected.json', b'{}')
    elif fault == 'again': put(c.slot / 'pending.json', b'{}')
    elif fault == 'state': put(c.root / 'state.json', b'{"unexpected":true}')
    elif fault == 'plan': put(c.slot / 'plan.json', b'{}', 0o644)
    elif fault == 'control': put(upgrade.system.CONTROL, b'{}', 0o644)
    elif fault == 'plist': put(upgrade.system.PLIST, b'changed', 0o644)
    elif fault == 'pending': put(c.root / ('state-' + 'f' * 32 + '.pending.json'), b'{}')
    elif fault == 'activation': put(c.base / 'factory-activation.json', b'{}')
    elif fault == 'log_mode': (c.base / 'system-service.stdout.log').chmod(0o620)
    elif fault == 'log_inode':
        path = c.base / 'system-service.stdout.log'; path.unlink(); path.symlink_to(c.base / 'system-service.stderr.log')
    elif fault == 'capability': c.manifest['runtime_capabilities'][upgrade.CAPABILITY] = True
    else: c.metas[c.new['package']]['registry_sha256'] = 'f' * 64
    with pytest.raises((ValueError, OSError)): c.apply()
    assert 'disable' not in c.trace and 'stop' not in c.trace


@pytest.mark.parametrize('phase', ['disable', 'stop', 'bootout', 'verify', 'enable', 'bootstrap', 'fresh_status'])
def test_failure_keeps_known_gate_disabled_and_pending_without_rollback(case, monkeypatch, phase):
    c = case; c.prepare()
    def fail_launch(action):
        if action == phase: raise ValueError('synthetic private failure text')
        return c.launch(action)
    def fail_worker(*args, **kwargs):
        if args[3] == phase: raise ValueError('synthetic private failure text')
        return c.worker(*args, **kwargs)
    monkeypatch.setattr(upgrade, '_launch', fail_launch); monkeypatch.setattr(upgrade, '_worker', fail_worker)
    if phase == 'fresh_status': monkeypatch.setattr(upgrade, '_status', Mock(side_effect=ValueError('private failure')))
    with pytest.raises(upgrade.UpgradeError) as error: c.apply()
    assert error.value.result['gate_disabled_confirmed'] is True
    assert json.loads(upgrade.system.CONTROL.read_bytes())['enabled'] is False
    assert (c.slot / 'pending.json').exists() and (c.slot / 'failed.json').exists()
    assert (c.slot / 'old-control.json').read_bytes() == c.control_raw
    assert (c.slot / 'old.plist').read_bytes() == c.plist_raw
    assert 'private failure' not in (c.slot / 'failed.json').read_text()
    assert c.trace.count('bootstrap') <= 1 and 'rollback' not in c.trace
    with pytest.raises(ValueError): c.apply()


@pytest.mark.parametrize('fault', ['pid_reuse', 'exited_reused_pid', 'replacement', 'job_absent', 'missing_exit', 'label_persists', 'residual', 'unknown_control', 'state_after', 'log_inode_after', 'activation_after'])
def test_post_stop_race_does_not_promote_unknown_scene(case, monkeypatch, fault):
    c = case; c.prepare()
    def worker(*args, **kwargs):
        value = c.worker(*args, **kwargs)
        if args[3] == 'stop':
            if fault == 'pid_reuse': c.identities[10001]['birth_usec'] += 1
            elif fault == 'exited_reused_pid':
                c.events['watch'].seen = True; c.identities[10001]['birth_usec'] += 1
            elif fault == 'replacement': c.runtime['job']['pid'] = 10003
            elif fault == 'job_absent': c.runtime['job'] = None
            elif fault == 'unknown_control': put(upgrade.system.CONTROL, b'{"unknown":true}', 0o644)
            elif fault == 'state_after': put(c.root / 'state.json', upgrade.boot.canonical({**c.state, 'manual_stop': True, 'extra': True}))
            elif fault == 'log_inode_after':
                path = c.base / 'system-service.stdout.log'; path.unlink(); put(path, b'new', 0o660)
            elif fault == 'activation_after': put(c.base / 'factory-activation.json', b'{}')
        return value
    def launch(action):
        c.launch(action)
        if action == 'bootout':
            if fault == 'missing_exit': c.events['watch'].seen = False
            elif fault == 'label_persists': c.runtime['job'] = {'state': 'waiting', 'pid': None}
            elif fault == 'residual': c.identities[10003] = {**c.old_identity, 'pid': 10003}
    monkeypatch.setattr(upgrade, '_worker', worker); monkeypatch.setattr(upgrade, '_launch', launch)
    monkeypatch.setattr(upgrade, 'EXIT_TIMEOUT', 0)
    with pytest.raises(upgrade.UpgradeError) as error: c.apply()
    assert 'verify' not in c.trace and 'enable' not in c.trace and 'bootstrap' not in c.trace
    if fault != 'unknown_control': assert error.value.result['gate_disabled_confirmed']
    else:
        assert not error.value.result['gate_disabled_confirmed']
        assert upgrade.system.CONTROL.read_bytes() == b'{"unknown":true}'


def test_log_growth_is_allowed_but_preserves_inode_and_mode(case, monkeypatch):
    c = case; c.prepare(); path = c.base / 'system-service.stdout.log'; info = path.stat()
    def worker(*args, **kwargs):
        with path.open('ab') as stream: stream.write(b'child output\n')
        return c.worker(*args, **kwargs)
    monkeypatch.setattr(upgrade, '_worker', worker)
    c.apply(); assert path.stat().st_ino == info.st_ino and path.stat().st_mode == info.st_mode
    assert path.read_bytes() == b'prior\nchild output\nchild output\n'


@pytest.mark.parametrize('value', [b'', b'[]', b'null', b'{"x":1,"x":2}', b'{"x":{"k":1,"k":2}}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e1000}', b'\xff', b'{"x":"\\ud800"}'])
def test_strict_state_decoder_refuses_ambiguous_or_nonfinite(value):
    with pytest.raises(ValueError): upgrade.decode_state(value)


def test_state_preserves_finite_float_and_noncanonical_raw_without_returning_private_data():
    raw = b'{ "current": "abc", "number":1.25, "nested":[true,null,-3] }'
    assert upgrade.decode_state(raw)['number'] == 1.25
    assert upgrade.decode_state(raw)['nested'] == [True, None, -3]


def test_state_bound(monkeypatch):
    monkeypatch.setattr(upgrade, 'STATE_LIMIT', 8)
    with pytest.raises(ValueError): upgrade.decode_state(b'{"number":1}')


def test_portable_stdlib_only_import_in_isolated_no_site_process():
    root = str(Path(upgrade.__file__).resolve().parents[1])
    script = 'import sys;sys.path.insert(0,' + repr(root) + ');from desktop_assistant import mac_system_upgrade;print("stdlib-import-ok")'
    result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', script], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b'stdlib-import-ok\n'
    ast.parse(Path(upgrade.__file__).read_text(), feature_version=(3, 9))


@pytest.mark.parametrize('fault', ['publication', 'log', 'plan', 'state'])
def test_final_validation_failure_still_closes_gate_inside_live_transaction(case, monkeypatch, fault):
    c = case; c.prepare(); original = upgrade._new
    def write(parent, name, raw, **kwargs):
        original(parent, name, raw, **kwargs)
        if name == 'verified.json':
            if fault == 'publication':
                path = c.base / 'publication.lock'; path.unlink(); put(path, b'publication lock\n')
            elif fault == 'log': (c.base / 'system-service.stdout.log').chmod(0o620)
            elif fault == 'plan': put(c.slot / 'plan.json', b'{}', 0o644)
            else: put(c.root / 'state.json', b'{}')
    monkeypatch.setattr(upgrade, '_new', write)
    with pytest.raises(upgrade.UpgradeError) as error: c.apply()
    assert error.value.result['gate_disabled_confirmed'] and error.value.result['label_disabled_confirmed']
    assert json.loads(upgrade.system.CONTROL.read_bytes())['enabled'] is False
    assert (c.slot / 'failed.json').exists()
    assert (c.slot / 'verified.json').exists()  # Preserved, never alone sufficient.


@pytest.mark.parametrize('mutation', ['file', 'extra', 'missing', 'empty_dir', 'link', 'hash', 'source', 'module_origin'])
def test_closed_admin_pack_refuses_tampering(tmp_path, monkeypatch, mutation):
    root = tmp_path / 'pack'; root.mkdir()
    for name in upgrade.CODE_FILES:
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(name.encode())
    hashes = {name: upgrade.boot.digest((root / name).read_bytes()) for name in upgrade.CODE_FILES}
    manifest = {'type': 'tianming.keychain-code.v1', 'source': '1' * 40, 'files': hashes}
    monkeypatch.setattr(upgrade, '_code_root', lambda: root)
    for module, name in ((upgrade.boot, 'desktop_assistant/mac_keychain_install.py'),
                         (upgrade.publication, 'desktop_assistant/mac_release_publish.py'),
                         (upgrade.system, 'desktop_assistant/mac_system_service.py'),
                         (contract, upgrade.CONTRACT_MODULE)):
        monkeypatch.setattr(module, '__file__', str(root / name))
    monkeypatch.setattr(upgrade.boot, 'protected', lambda path, **kw: None if kw.get('directory') else Path(path).read_bytes())
    path = root / upgrade.MODULE
    if mutation == 'file': path.write_bytes(b'changed')
    elif mutation == 'extra': (root / 'other.py').write_bytes(b'extra')
    elif mutation == 'missing': path.unlink()
    elif mutation == 'empty_dir': (root / 'extra').mkdir()
    elif mutation == 'link':
        if os.name != 'posix': pytest.skip('Symlink creation needs POSIX test capability')
        path.unlink(); path.symlink_to(root / upgrade.CONTRACT_MODULE)
    elif mutation == 'hash': manifest['files'][upgrade.MODULE] = True
    elif mutation == 'source': manifest['source'] = '../unknown'
    else: monkeypatch.setattr(upgrade.system, '__file__', str(tmp_path / 'unreviewed.py'))
    (root / 'install-manifest.json').write_bytes(contract.canonical(manifest))
    with pytest.raises((ValueError, OSError)): upgrade.verify_code()


def test_closed_pack_good_case_uses_separate_allowlist_without_mutating_boot(tmp_path, monkeypatch):
    root = tmp_path / 'pack'; root.mkdir(); before = tuple(upgrade.boot.FILES)
    for name in upgrade.CODE_FILES:
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(name.encode())
    monkeypatch.setattr(upgrade, '_code_root', lambda: root)
    for module, name in ((upgrade.boot, 'desktop_assistant/mac_keychain_install.py'),
                         (upgrade.publication, 'desktop_assistant/mac_release_publish.py'),
                         (upgrade.system, 'desktop_assistant/mac_system_service.py'), (contract, upgrade.CONTRACT_MODULE)):
        monkeypatch.setattr(module, '__file__', str(root / name))
    monkeypatch.setattr(upgrade.boot, 'protected', lambda path, **kw: None if kw.get('directory') else Path(path).read_bytes())
    raw = contract.canonical({'type': 'tianming.keychain-code.v1', 'source': '1' * 40,
        'files': {name: upgrade.boot.digest((root / name).read_bytes()) for name in upgrade.CODE_FILES}})
    (root / 'install-manifest.json').write_bytes(raw)
    assert upgrade.verify_code() == upgrade.boot.digest(raw)
    assert upgrade.boot.FILES == before and upgrade.MODULE not in before


@pytest.mark.parametrize('fault', ['canary', 'groups', 'group_missing', 'bool_uid', 'login', 'future', 'old', 'extra'])
def test_fresh_system_status_requires_canary_identity_and_bootstrap_time(case, monkeypatch, fault):
    c = case; c.prepare()
    def launch(action):
        c.launch(action)
        if action == 'bootstrap':
            path = c.control / 'service-status.json'; value = json.loads(path.read_bytes())
            if fault == 'canary': value['keychain_canary_verified'] = False
            elif fault == 'groups': value['system_identity']['kernel_groups'].append(0)
            elif fault == 'group_missing': value['system_identity']['kernel_groups'] = [12]
            elif fault == 'bool_uid': value['system_identity']['uid'] = True
            elif fault == 'login': value['requires_user_login'] = True
            elif fault in {'future', 'old'}:
                value['updated_at'] = datetime.fromtimestamp(time.time() + (3600 if fault == 'future' else -3600), timezone.utc).isoformat()
            else: value['unknown'] = True
            put(path, upgrade.boot.canonical(value))
    monkeypatch.setattr(upgrade, '_launch', launch)
    with pytest.raises(upgrade.UpgradeError) as error: c.apply()
    assert error.value.result['gate_disabled_confirmed']


@pytest.mark.parametrize('fault', ['residual', 'uid', 'ruid', 'gid', 'rgid', 'birth', 'collection_changes', 'missing'])
def test_service_uid_clearance_refuses_residuals_or_uncertain_identity(case, monkeypatch, fault):
    c = case
    value = {**c.old_identity, 'exe': str(c.new['tree'] / 'runtime/bin/python3.12')}
    if fault == 'residual': value['exe'] = c.old_identity['exe']
    elif fault in {'uid', 'ruid', 'gid', 'rgid'}: value[fault] = 0
    c.identities[10001] = value
    if fault == 'birth':
        count = iter([deepcopy(value), {**value, 'birth_usec': value['birth_usec'] + 1}])
        monkeypatch.setattr(upgrade, 'process_identity', lambda _: next(count))
    elif fault == 'collection_changes':
        values = iter([frozenset({10001}), frozenset()])
        monkeypatch.setattr(upgrade, '_service_pids', lambda: next(values))
    elif fault == 'missing': monkeypatch.setattr(upgrade, 'process_identity', Mock(side_effect=ProcessLookupError()))
    with pytest.raises((ValueError, OSError)): upgrade._no_old_monitor(c.old)


def test_service_pid_listing_real_effective_union_is_bounded_and_stable(monkeypatch):
    def listing(kind, uid, buffer, size):
        assert uid == 499
        values = [101, 102] if kind == 4 else [102, 103]
        if buffer is not None:
            for index, value in enumerate(values): buffer[index] = value
        return len(values) * 4
    monkeypatch.setattr(upgrade, '_libproc', lambda: SimpleNamespace(proc_listpids=listing))
    assert upgrade._service_pids() == {101, 102, 103}


@pytest.mark.parametrize('fault', ['negative', 'oversize', 'not_aligned', 'full_buffer', 'duplicate', 'zero', 'unstable'])
def test_service_pid_listing_rejects_truncation_or_unstable_result(monkeypatch, fault):
    calls = [0]
    def listing(kind, uid, buffer, size):
        if fault == 'negative': return -1
        if fault == 'oversize': return 65537 * 4
        if fault == 'not_aligned': return 3
        if buffer is None: return 8
        calls[0] += 1
        if fault == 'full_buffer': return size
        buffer[0] = 0 if fault == 'zero' else (100 + calls[0] if fault == 'unstable' else 101)
        buffer[1] = buffer[0] if fault == 'duplicate' else 102
        return 8
    monkeypatch.setattr(upgrade, '_libproc', lambda: SimpleNamespace(proc_listpids=listing))
    with pytest.raises(ValueError): upgrade._service_pids()
    assert calls[0] <= 12


@pytest.mark.parametrize('fault', ['short', 'path_empty', 'path_full', 'changed_birth', 'wrong_pid', 'uid_change', 'zombie', 'exiting'])
def test_kernel_identity_sandwich_requires_exact_bsdinfo_and_path(monkeypatch, fault):
    calls = [0]
    def info(pid, flavor, arg, pointer, size):
        assert flavor == 3 and size == 136
        if fault == 'short': return size - 1
        calls[0] += 1; data = pointer._obj
        data.pid = 999 if fault == 'wrong_pid' else pid
        data.ppid = 1; data.uid = data.ruid = data.gid = data.rgid = 499
        data.birth_sec = 100; data.birth_usec = 5
        if fault == 'zombie': data.status = 5
        if fault == 'exiting': data.flags = 4
        if calls[0] > 1 and fault == 'changed_birth': data.birth_usec += 1
        if calls[0] > 1 and fault == 'uid_change': data.ruid = 0
        return size
    def path(pid, buffer, size):
        buffer.value = b'/fixed/python'
        return 0 if fault == 'path_empty' else size if fault == 'path_full' else len(buffer.value)
    monkeypatch.setattr(upgrade, '_libproc', lambda: SimpleNamespace(proc_pidinfo=info, proc_pidpath=path))
    with pytest.raises(ValueError): upgrade.process_identity(101)


@pytest.mark.parametrize('kind', ['normal', 'stdout_overflow', 'stderr_large', 'exit_failure'])
def test_worker_output_collector_drains_own_child_and_never_exposes_stderr(kind):
    if os.name != 'posix': pytest.skip('POSIX pipe selector transport')
    script = 'import sys;sys.stdout.write("{}")'
    if kind == 'stdout_overflow': script = 'import sys;sys.stdout.write("x"*70000)'
    elif kind == 'stderr_large': script += ';sys.stderr.write("private"*30000)'
    elif kind == 'exit_failure': script += ';sys.exit(9)'
    child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if kind in {'stdout_overflow', 'exit_failure'}:
        with pytest.raises(ValueError): upgrade._collect(child)
    else: assert upgrade._collect(child) == b'{}'
    assert child.poll() is not None and child.stdout.closed and child.stderr.closed


def test_collector_error_still_waits_own_child(monkeypatch):
    if os.name != 'posix': pytest.skip('POSIX pipe selector transport')
    child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', 'import time;time.sleep(.05)'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = Mock(); selector.__enter__ = Mock(side_effect=OSError('synthetic selector error'))
    selector.__exit__ = Mock()
    monkeypatch.setattr(upgrade.selectors, 'DefaultSelector', lambda: selector)
    with pytest.raises(OSError): upgrade._collect(child)
    assert child.poll() == 0


def test_native_libproc_kqueue_observes_only_own_synthetic_child():
    if sys.platform != 'darwin': pytest.skip('Darwin native libproc/kqueue synthetic child only')
    # No root, launchd, existing ERP, credentials or service UID is touched.
    child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', 'import sys;sys.stdin.read(1)'],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        value = upgrade.process_identity(child.pid)
        assert value['uid'] == value['ruid'] == os.getuid() and value['ppid'] == os.getpid()
        with upgrade.ExitWatch(value) as watch:
            assert not watch.poll()
            child.stdin.write(b'x'); child.stdin.close()
            deadline = time.monotonic() + 10
            while not watch.poll(.1): assert time.monotonic() < deadline
            assert watch.seen and child.wait(timeout=5) == 0
    finally:
        if child.stdin and not child.stdin.closed: child.stdin.close()
        child.wait(timeout=10)


@pytest.mark.parametrize('phase', ['stop', 'verify'])
@pytest.mark.parametrize('fault', [None, 'challenge', 'phase', 'canary', 'extra', 'identity'])
def test_fixed_worker_dispatch_and_bound_public_reply(monkeypatch, phase, fault):
    from tests.desktop_assistant.test_mac_system_upgrade_contract import valid_plan, valid_reply
    from desktop_assistant import home_process
    plan = valid_plan(); sha = contract.digest(contract.canonical(plan))
    request = {'type': contract.REQUEST, 'attempt_id': plan['attempt_id'], 'plan_sha256': sha,
        'phase': phase, 'challenge': '9' * 32, 'lock_fd': None if phase == 'stop' else 9}
    reply = valid_reply(plan, request)
    if fault == 'challenge': reply['challenge'] = '8' * 32
    elif fault == 'phase': reply['phase'] = 'verify' if phase == 'stop' else 'stop'
    elif fault == 'canary': reply['keychain_canary_verified'] = phase != 'verify'
    elif fault == 'extra': reply['verified_monitor_exit'] = True
    elif fault == 'identity': reply['identity']['uid'] = 0
    monkeypatch.setattr(upgrade.uuid, 'uuid4', lambda: SimpleNamespace(hex='9' * 32))
    monkeypatch.setattr(home_process, 'command', lambda argv, env: ['synthetic-sandbox', *argv])
    sentinel = object(); popen = Mock(return_value=sentinel)
    monkeypatch.setattr(upgrade.subprocess, 'Popen', popen)
    monkeypatch.setattr(upgrade, '_collect', lambda process: contract.canonical(reply) if process is sentinel else b'{}')
    if fault:
        with pytest.raises(ValueError): upgrade._worker(plan, sha, {'tree': Path('/fixed/new')}, phase, request['lock_fd'])
    else: assert upgrade._worker(plan, sha, {'tree': Path('/fixed/new')}, phase, request['lock_fd']) == reply
    args, options = popen.call_args
    assert args[0][:4] == ['synthetic-sandbox', str(Path('/fixed/new/runtime/bin/python3.12')), '-I', '-B']
    assert options['user'] == options['group'] == 499 and options['extra_groups'] == []
    assert options['pass_fds'] == (() if phase == 'stop' else (9,)) and options['close_fds'] is True
    assert options['env']['ERP_HOME_REHEARSAL'] == '1' and options['umask'] == 0o077
    assert options['stdin'] == subprocess.DEVNULL and options['cwd'] == '/'
    assert '--plan-sha256' in args[0] and '--challenge' in args[0] and not any('password' in arg for arg in args[0])


@pytest.mark.parametrize('fault', [None, 'event_error', 'event_pid', 'event_filter', 'event_flags', 'after_identity', 'register_failure'])
def test_kqueue_registration_events_preserved_and_bound(monkeypatch, fault):
    import select
    value = {'pid': 101, 'birth_sec': 100, 'birth_usec': 5, 'exe': '/fixed'}
    event = SimpleNamespace(ident=101, filter=-5, flags=0, fflags=0x80000000)
    if fault == 'event_error': event.flags = 0x4000
    elif fault == 'event_pid': event.ident = 102
    elif fault == 'event_filter': event.filter = -1
    elif fault == 'event_flags': event.fflags = 1
    queue = Mock(); queue.control.side_effect = [[event], []]
    if fault == 'register_failure': queue.control.side_effect = ProcessLookupError()
    monkeypatch.setattr(upgrade.sys, 'platform', 'darwin')
    for key, data in {'KQ_FILTER_PROC': -5, 'KQ_EV_ADD': 1, 'KQ_EV_ENABLE': 4,
                      'KQ_EV_ONESHOT': 16, 'KQ_NOTE_EXIT': 0x80000000, 'KQ_EV_ERROR': 0x4000}.items():
        monkeypatch.setattr(select, key, data, raising=False)
    monkeypatch.setattr(select, 'kqueue', lambda: queue, raising=False)
    monkeypatch.setattr(select, 'kevent', lambda *args, **kwargs: (args, kwargs), raising=False)
    identities = [value, {**value, 'birth_usec': 6} if fault == 'after_identity' else value]
    monkeypatch.setattr(upgrade, 'process_identity', Mock(side_effect=identities))
    if fault:
        with pytest.raises((ValueError, OSError)): upgrade.ExitWatch(value)
        queue.close.assert_called_once()
    else:
        with upgrade.ExitWatch(value) as watch:
            assert watch.seen and watch.poll()  # Initial event not discarded.
        queue.close.assert_called_once()


@pytest.mark.parametrize('fault', [None, 'wrong_code', 'wrong_label', 'empty', 'duplicate_pid', 'duplicate_state', 'garbage_utf8'])
def test_launchctl_metadata_parser_rejects_ambiguous_status(monkeypatch, fault):
    missing = b'Could not find service "cn.tianming.erp" in domain for system'
    result = (113, b'', missing)
    if fault == 'wrong_code': result = (1, b'', missing)
    elif fault == 'wrong_label': result = (113, b'', b'Could not find service "other" in domain for system')
    elif fault == 'empty': result = (0, b'', b'')
    elif fault == 'duplicate_pid': result = (0, b'state = running\npid = 2\npid = 3\n', b'')
    elif fault == 'duplicate_state': result = (0, b'state = running\nstate = waiting\n', b'')
    elif fault == 'garbage_utf8': result = (0, b'\xff', b'')
    monkeypatch.setattr(upgrade.system, 'launch', Mock(return_value=result))
    if fault:
        with pytest.raises((ValueError, UnicodeError)): upgrade._job()
    else: assert upgrade._job() is None
