"""Disposable POSIX transaction scenes; no installed files or services.

Start with a real upgrade apply interrupted between its two actual file CASes.
Only OS identity, launchd/process observations and the UID IPC are adapters.
The separate signed-worker suite covers the unchanged IPC implementation.
"""
import importlib
import ast
import json
import os
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from desktop_assistant import mac_system_upgrade as upgrade
from desktop_assistant import mac_system_upgrade_contract as contract
from tests.desktop_assistant.test_mac_system_upgrade import case, put
from tests.desktop_assistant.test_mac_system_upgrade_activation import factory_case
from tests.desktop_assistant.test_mac_system_upgrade_contract import valid_reply


@pytest.fixture
def split_case(factory_case, monkeypatch, request):
    c = factory_case
    try:
        recovery = recovery_api()
    except ModuleNotFoundError:
        pass  # Initial red test first proves the actual split without a recovery module.
    else:
        monkeypatch.setattr(recovery, '_source', lambda: 'f' * 40)
    c.manifest['git_sha'] = 'f' * 40
    c.manifest['runtime_capabilities']['system_upgrade_split_recovery_v1'] = 1
    for name in ('mac_system_upgrade_recovery.py', 'mac_system_upgrade_recovery_contract.py'):
        c.manifest['files']['desktop_assistant/' + name] = 'a' * 64
    def worker(plan, sha, meta, phase, fd=None):
        c.factory_worker(plan, sha, meta, phase, fd)
        request = dict(type=contract.REQUEST, attempt_id=plan['attempt_id'], plan_sha256=sha,
                       challenge='e' * 32, phase=phase, lock_fd=fd)
        return valid_reply(plan, request)
    monkeypatch.setattr(upgrade, '_worker', worker)
    c.prepare()
    original_cas = upgrade._cas
    fsync = os.fsync
    injected = []
    def fail_plist_sync(fd):
        if (getattr(request, 'param', None) == 'after-plist-rename' and not injected
                and os.fstat(fd).st_ino == c.plists.stat().st_ino
                and upgrade.system.PLIST.read_bytes() != c.plist_raw):
            injected.append(True)
            raise OSError('real parent fsync interrupted after plist rename')
        return fsync(fd)
    def fail_select(parent, name, previous, replacement, temporary):
        if getattr(request, 'param', None) != 'after-plist-rename' and temporary.endswith('-select-new.json'):
            raise OSError('injected interruption after real plist rename')
        return original_cas(parent, name, previous, replacement, temporary)
    monkeypatch.setattr(os, 'fsync', fail_plist_sync)
    monkeypatch.setattr(upgrade, '_cas', fail_select)
    with pytest.raises(upgrade.UpgradeError): c.apply()
    monkeypatch.setattr(upgrade, '_cas', original_cas)
    monkeypatch.setattr(os, 'fsync', fsync)
    if getattr(request, 'param', None) == 'after-plist-rename': assert injected == [True]
    assert upgrade.system.CONTROL.read_bytes() == contract.disabled_control_bytes(c.plan, 'stop')
    assert upgrade.system.PLIST.read_bytes() == upgrade.system._plist(c.new['package'], c.new['publisher_sha256'])
    assert c.runtime['job'] is None and c.identities == {}
    assert (c.root / 'state.json').read_bytes() == c.stopped
    assert (c.slot / 'failed.json').exists()
    c.evidence = {p.name: p.read_bytes() for p in c.slot.iterdir()}
    c.recovery_trace = len(c.trace)
    return c


def recovery_api():
    return importlib.import_module('desktop_assistant.mac_system_upgrade_recovery')


def prepare(c):
    recovery = recovery_api()
    plan = recovery.make_plan(c.plan['attempt_id'])
    raw = contract.canonical(plan); digest = contract.digest(raw)
    recovery.prepare(raw, digest)
    c.recovery_plan, c.recovery_raw, c.recovery_sha = plan, raw, digest
    c.recovery_slot = c.base / ('system-upgrade-recovery-' + plan['recovery_id'])
    return recovery


@pytest.mark.parametrize('split_case', ['before-control', 'after-plist-rename'], indirect=True)
def test_actual_interrupted_upgrade_can_recover_only_its_known_split(split_case):
    c = split_case
    # The old generic route demonstrably cannot enable either inconsistent tuple.
    for meta in (c.old, c.new):
        with pytest.raises(ValueError):
            upgrade.system.checked_control(meta)
            upgrade.system._installed_plist(meta)
    recovery = prepare(c)
    result = recovery.apply(c.recovery_plan['recovery_id'], c.recovery_sha)
    assert result['activation_preserved'] is True
    assert result['activation_performed'] is result['erp_started'] is False
    assert result['current_package_sha256'] == c.state['current']
    assert result['root_gate_enabled'] is True
    assert (c.root / 'state.json').read_bytes() == c.stopped
    assert (c.active_path.read_bytes(), c.gate_path.read_bytes()) == c.original_authority
    assert {p.name: p.read_bytes() for p in c.slot.iterdir()} == c.evidence
    assert json.loads(upgrade.system.CONTROL.read_bytes())['package_sha256'] == c.new['package']
    assert [v for v in c.trace[c.recovery_trace:] if v not in ('publication-enter', 'publication-exit')] == [
        'verify', 'enable', 'bootstrap']
    assert not c.locked('operation.lock') and not c.locked('service-supervisor.lock')
    assert (c.recovery_slot / 'verified.json').exists()
    assert not (c.recovery_slot / 'failed.json').exists()


@pytest.mark.parametrize('fault', ['missing-evidence', 'extra-evidence', 'evidence-symlink', 'not-failed',
    'missing-exit', 'numeric-exit', 'bad-stop', 'bad-old-control', 'bad-old-plist', 'bad-identity',
    'unconfirmed-gate', 'unconfirmed-label', 'old-old', 'new-new', 'enabled-old', 'unknown-control',
    'unknown-plist', 'state-extra', 'state-current', 'state-running', 'pending-state', 'owner-failed',
    'activation-bytes', 'gate-bytes', 'activation-link', 'missing-cap', 'bool-cap', 'bad-source',
    'missing-code', 'registry', 'live-label', 'old-process', 'new-process', 'expired'])
def test_unproven_scene_is_refused_without_control_mutation(split_case, fault):
    c = split_case; recovery = recovery_api()
    if fault == 'missing-evidence': (c.slot / 'stop.json').unlink()
    elif fault == 'extra-evidence': put(c.slot / 'unknown.json', b'{}')
    elif fault == 'evidence-symlink':
        p = c.slot / 'old.plist'; raw = p.read_bytes(); p.unlink()
        put(c.base / 'moved.plist', raw); p.symlink_to(c.base / 'moved.plist')
    elif fault == 'not-failed': (c.slot / 'failed.json').unlink()
    elif fault in ('missing-exit', 'numeric-exit'):
        p = c.slot / 'old-exited.json'; v = json.loads(p.read_bytes())
        v['kernel_exit_observed'] = False if fault == 'missing-exit' else 1
        put(p, contract.canonical(v))
    elif fault == 'bad-stop': put(c.slot / 'stop.json', b'{}')
    elif fault == 'bad-old-control': put(c.slot / 'old-control.json', b'{}')
    elif fault == 'bad-old-plist': put(c.slot / 'old.plist', b'changed')
    elif fault == 'bad-identity':
        p = c.slot / 'pending.json'; v = json.loads(p.read_bytes()); v['old_monitor']['uid'] = 0
        put(p, contract.canonical(v))
    elif fault in ('unconfirmed-gate', 'unconfirmed-label'):
        p = c.slot / 'failed.json'; v = json.loads(p.read_bytes())
        v['gate_disabled_confirmed' if fault == 'unconfirmed-gate' else 'label_disabled_confirmed'] = False
        put(p, contract.canonical(v))
    elif fault == 'old-old': put(upgrade.system.PLIST, c.plist_raw, 0o644)
    elif fault == 'new-new': put(upgrade.system.CONTROL, contract.disabled_control_bytes(c.plan, 'verify'), 0o644)
    elif fault == 'enabled-old': put(upgrade.system.CONTROL, c.control_raw, 0o644)
    elif fault == 'unknown-control': put(upgrade.system.CONTROL, b'{}', 0o644)
    elif fault == 'unknown-plist': put(upgrade.system.PLIST, b'unknown', 0o644)
    elif fault.startswith('state-'):
        v = json.loads(c.stopped)
        if fault == 'state-extra': v['extra'] = True
        elif fault == 'state-current': v['current'] = c.new['package']
        else: v['manual_stop'] = False
        put(c.root / 'state.json', upgrade.boot.canonical(v))
    elif fault == 'pending-state': put(c.root / 'state.json.pending', b'{}')
    elif fault == 'owner-failed': put(c.base / contract.OWNER_FAILED_NAME, b'{}')
    elif fault == 'activation-bytes': c.active_path.write_bytes(c.active_path.read_bytes() + b' ')
    elif fault == 'gate-bytes': c.gate_path.write_bytes(c.gate_path.read_bytes() + b' ')
    elif fault == 'activation-link':
        raw = c.active_path.read_bytes(); c.active_path.unlink(); put(c.base / 'saved-active.json', raw, 0o644)
        c.active_path.symlink_to(c.base / 'saved-active.json')
    elif fault == 'missing-cap': c.manifest['runtime_capabilities'].pop('system_upgrade_split_recovery_v1')
    elif fault == 'bool-cap': c.manifest['runtime_capabilities']['system_upgrade_split_recovery_v1'] = True
    elif fault == 'bad-source': c.manifest['git_sha'] = 'a' * 40
    elif fault == 'missing-code': c.manifest['files'].pop(recovery.MODULE)
    elif fault == 'registry': c.metas[c.new['package']]['registry_sha256'] = 'f' * 64
    elif fault == 'live-label': c.runtime['job'] = {'state': 'waiting', 'pid': None}
    elif fault in ('old-process', 'new-process'):
        v = dict(c.old_identity, pid=10101)
        if fault == 'new-process': v['exe'] = str((c.new['tree'] / 'runtime/bin/python3.12').resolve())
        c.identities[10101] = v
    else:
        v = dict(c.plan, created_at=1, expires_at=2); put(c.slot / 'plan.json', contract.canonical(v), 0o644)
    before = (upgrade.system.CONTROL.read_bytes(), upgrade.system.PLIST.read_bytes(), (c.root / 'state.json').read_bytes())
    with pytest.raises((ValueError, OSError)): recovery.make_plan(c.plan['attempt_id'])
    assert before == (upgrade.system.CONTROL.read_bytes(), upgrade.system.PLIST.read_bytes(), (c.root / 'state.json').read_bytes())
    assert not list(c.base.glob('system-upgrade-recovery-*'))
    assert 'enable' not in c.trace and 'bootstrap' not in c.trace


@pytest.mark.parametrize('lock', ['operation.lock', 'service-supervisor.lock'])
def test_both_original_locks_are_required(split_case, lock):
    import fcntl
    c = split_case; recovery = prepare(c)
    with (c.control / lock).open('rb') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError): recovery.apply(c.recovery_plan['recovery_id'], c.recovery_sha)
    assert not (c.recovery_slot / 'pending.json').exists()
    assert upgrade.system.CONTROL.read_bytes() == contract.disabled_control_bytes(c.plan, 'stop')


@pytest.mark.parametrize('phase', ['before-select', 'after-select', 'verify', 'after-verify',
    'before-enable', 'after-enable', 'enable', 'bootstrap', 'status', 'unlock', 'receipt', 'last-check'])
def test_each_recovery_failure_retains_all_records_and_closes_known_gate(split_case, monkeypatch, phase):
    c = split_case; recovery = prepare(c)
    cas, worker, launch, write, locked = upgrade._cas, upgrade._worker, upgrade._launch, upgrade._new, upgrade._locked
    def fail_cas(parent, name, previous, replacement, temporary):
        stage = 'select' if temporary.endswith('-select-new.json') else 'enable' if temporary.endswith('-enable-new.json') else None
        if phase == 'before-' + str(stage): raise OSError('synthetic private data')
        result = cas(parent, name, previous, replacement, temporary)
        if phase == 'after-' + str(stage): raise OSError('synthetic post-rename failure')
        return result
    def fail_worker(*args, **kwargs):
        if phase == 'verify': raise ValueError('synthetic canary failure')
        result = worker(*args, **kwargs)
        if phase == 'after-verify': raise ValueError('synthetic reply failure')
        return result
    def fail_launch(action):
        if phase == action: raise OSError('synthetic launch failure')
        return launch(action)
    @contextmanager
    def fail_unlock(pin):
        with locked(pin) as fd: yield fd
        if phase == 'unlock' and pin.path.name == 'operation.lock': raise OSError('synthetic lock exit check')
    def fail_write(parent, name, raw, **kw):
        if name == 'verified.json' and phase == 'receipt': raise OSError('synthetic record failure')
        result = write(parent, name, raw, **kw)
        if name == 'verified.json' and phase == 'last-check':
            c.gate_path.write_bytes(c.gate_path.read_bytes() + b' ')
        return result
    monkeypatch.setattr(upgrade, '_cas', fail_cas); monkeypatch.setattr(upgrade, '_worker', fail_worker)
    monkeypatch.setattr(upgrade, '_launch', fail_launch); monkeypatch.setattr(upgrade, '_locked', fail_unlock)
    monkeypatch.setattr(upgrade, '_new', fail_write)
    if phase == 'status': monkeypatch.setattr(upgrade, '_status', lambda *a: (_ for _ in ()).throw(ValueError('stale')))
    with pytest.raises(recovery.RecoveryError) as error: recovery.apply(c.recovery_plan['recovery_id'], c.recovery_sha)
    assert error.value.result['gate_disabled_confirmed'] is True
    assert error.value.result['label_disabled_confirmed'] is True
    assert json.loads(upgrade.system.CONTROL.read_bytes())['enabled'] is False
    assert (c.root / 'state.json').read_bytes() == c.stopped
    assert {p.name: p.read_bytes() for p in c.slot.iterdir()} == c.evidence
    assert (c.recovery_slot / 'failed.json').exists()
    assert b'synthetic' not in (c.recovery_slot / 'failed.json').read_bytes()
    assert (c.recovery_slot / 'verified.json').exists() is (phase == 'last-check')
    with pytest.raises(ValueError): recovery.apply(c.recovery_plan['recovery_id'], c.recovery_sha)


@pytest.mark.parametrize('fault', ['evidence', 'state', 'activation', 'owner-gate', 'registry', 'control', 'plist', 'new-process'])
def test_changes_during_real_verify_never_reopen_gate(split_case, monkeypatch, fault):
    c = split_case; recovery = prepare(c); worker = upgrade._worker
    def mutate(*args, **kwargs):
        result = worker(*args, **kwargs)
        if fault == 'evidence': put(c.slot / 'failed.json', b'{}')
        elif fault == 'state': put(c.root / 'state.json', b'{}')
        elif fault == 'activation': c.active_path.write_bytes(c.active_path.read_bytes() + b' ')
        elif fault == 'owner-gate': c.gate_path.write_bytes(c.gate_path.read_bytes() + b' ')
        elif fault == 'registry': c.metas[c.new['package']]['registry_sha256'] = 'f' * 64
        elif fault == 'control': put(upgrade.system.CONTROL, b'{}', 0o644)
        elif fault == 'plist': put(upgrade.system.PLIST, b'unknown', 0o644)
        else: c.identities[10101] = dict(c.old_identity, pid=10101,
                exe=str((c.new['tree'] / 'runtime/bin/python3.12').resolve()))
        return result
    monkeypatch.setattr(upgrade, '_worker', mutate)
    with pytest.raises(recovery.RecoveryError) as error: recovery.apply(c.recovery_plan['recovery_id'], c.recovery_sha)
    assert error.value.result['gate_disabled_confirmed'] is (fault != 'control')
    assert not (c.recovery_slot / 'verified.json').exists()
    assert 'enable' not in c.trace and 'bootstrap' not in c.trace
    if fault == 'control': assert upgrade.system.CONTROL.read_bytes() == b'{}'


@pytest.mark.parametrize('fault', ['extra', 'missing', 'unknown', 'bad-token', 'bad-sha', 'bad-evidence',
    'extra-evidence', 'source', 'bool-time', 'expired', 'future', 'too-long', 'wrong-plan', 'wrong-activation'])
def test_recovery_plan_exact_shape_and_binding(split_case, fault):
    c = split_case; recovery = recovery_api(); v = recovery.make_plan(c.plan['attempt_id'])
    if fault == 'extra': v['allow_unknown'] = True
    elif fault == 'missing': v.pop('activation_sha256')
    elif fault == 'unknown': v['type'] += '.unknown'
    elif fault == 'bad-token': v['attempt_id'] = '../elsewhere'
    elif fault == 'bad-sha': v['owner_publication_sha256'] = True
    elif fault == 'bad-evidence': v['evidence_sha256']['old.plist'] = 'f' * 64
    elif fault == 'extra-evidence': v['evidence_sha256']['extra.json'] = 'f' * 64
    elif fault == 'source': v['source'] = 'a' * 40
    elif fault == 'bool-time': v['created_at'] = True
    elif fault == 'expired': v.update(created_at=1, expires_at=2)
    elif fault == 'future': v['created_at'] += 60; v['expires_at'] += 60
    elif fault == 'too-long': v['expires_at'] += 8 * 86400
    elif fault == 'wrong-plan': v['upgrade_plan_sha256'] = 'f' * 64
    else: v['activation_sha256'] = 'f' * 64
    raw = contract.canonical(v)
    with pytest.raises((ValueError, OSError)): recovery.prepare(raw, contract.digest(raw))
    assert not list(c.base.glob('system-upgrade-recovery-*'))


def test_recovery_is_stdlib_only_under_root_no_site_python():
    root = str(Path(upgrade.__file__).resolve().parents[1])
    script = 'import sys;sys.path.insert(0,' + repr(root) + ');from desktop_assistant import mac_system_upgrade_recovery;print("stdlib-ok")'
    result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', script], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b'stdlib-ok\n'
    recovery = recovery_api()
    for module in (recovery, recovery.recovery):
        ast.parse(Path(module.__file__).read_text(), feature_version=(3, 9))


@pytest.mark.parametrize('module', ['executor', 'contract'])
def test_loaded_recovery_modules_must_belong_to_closed_pack(split_case, monkeypatch, module):
    c = split_case; recovery = recovery_api()
    selected = recovery if module == 'executor' else recovery.recovery
    monkeypatch.setattr(selected, '__file__', str(c.base / 'unknown.py'))
    with pytest.raises(ValueError): recovery.make_plan(c.plan['attempt_id'])
    assert upgrade.system.CONTROL.read_bytes() == contract.disabled_control_bytes(c.plan, 'stop')


@pytest.mark.parametrize('selected', ['old', 'new'])
@pytest.mark.parametrize('registered', [False, True])
def test_consistent_tuple_uses_existing_conditional_disable_enable_route(split_case, monkeypatch, selected, registered):
    c = split_case; system = upgrade.system; meta = getattr(c, selected)
    phase = 'stop' if selected == 'old' else 'verify'
    put(system.CONTROL, contract.disabled_control_bytes(c.plan, phase), 0o644)
    put(system.PLIST, system._plist(meta['package'], meta['publisher_sha256']), 0o644)
    with pytest.raises(ValueError): recovery_api().make_plan(c.plan['attempt_id'])
    state = {'registered': registered}; trace = []
    monkeypatch.setattr(upgrade.boot, 'verify_code', lambda **kw: c.plan['code_manifest_sha256'])
    monkeypatch.setattr(upgrade.boot, 'modules', lambda: (
        SimpleNamespace(_administrator=lambda: None), SimpleNamespace(_identity=lambda *a, **kw: None)))
    monkeypatch.setattr(system, 'present', lambda: state['registered'])
    def launch(args):
        trace.append(args[0])
        if args[0] == 'bootout': state['registered'] = False
        if args[0] == 'bootstrap': state['registered'] = True
        return 0, b'', b''
    def worker(current, action):
        # Generic worker IPC is adapted; its real implementation and required
        # stopped-state canary are separately covered by the unchanged suite.
        assert current == meta and action in ('stop', 'check')
        assert json.loads(system.CONTROL.read_bytes())['enabled'] is False
        assert (c.root / 'state.json').read_bytes() == c.stopped
        assert c.identities == {}
        trace.append(action)
    monkeypatch.setattr(system, 'launch', launch); monkeypatch.setattr(system, 'worker', worker)
    if registered:
        value = system.make_plan('enable', meta['package']); raw = contract.canonical(value)
        with pytest.raises(ValueError): system.apply(raw, contract.digest(raw))
        value = system.make_plan('disable', meta['package']); raw = contract.canonical(value)
        result = system.apply(raw, contract.digest(raw)); assert result['registered'] is False
    value = system.make_plan('enable', meta['package']); raw = contract.canonical(value)
    result = system.apply(raw, contract.digest(raw))
    assert result['registered'] is True and result['health_verified'] is False
    assert trace == (['stop', 'bootout', 'disable'] if registered else []) + ['check', 'enable', 'bootstrap']
    assert (c.root / 'state.json').read_bytes() == c.stopped
    assert {p.name: p.read_bytes() for p in c.slot.iterdir()} == c.evidence
    assert (c.active_path.read_bytes(), c.gate_path.read_bytes()) == c.original_authority
