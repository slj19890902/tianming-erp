"""Actual signed owner configuration and actual UID worker inside split recovery.

No installed runtime is used. OS UID, launchd, process sampling and mounted
history are explicit synthetic adapters; signatures, trees and FD locks are real.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from desktop_assistant import mac_system_upgrade as upgrade
from desktop_assistant import mac_system_upgrade_contract as original
from desktop_assistant import mac_system_upgrade_recovery as recovery
from desktop_assistant import mac_system_upgrade_recovery_contract as recovery_contract
from tests.desktop_assistant.test_mac_system_upgrade import put
from tests.desktop_assistant.test_mac_system_upgrade_owner_worker import (
    scene, monitor_protocol, owner_case, current_case, current_protocol, runtime, published,
    contract, protocol, boot, system, Manager, activated, root_case, target, case, recovery_case,
    host_store, signed_activation_protocol, native_fixture, execution_protocol, approve_update,
    owner, worker, keychain, authority_bytes)


@pytest.fixture(autouse=True)
def recovery_protocol(monitor_protocol, monkeypatch):
    prior = native_fixture.pack_tree
    monkeypatch.setattr(native_fixture.publication, 'SERVICE_RESTORE_FILES',
        native_fixture.publication.SERVICE_RESTORE_FILES | {recovery.MODULE, recovery.CONTRACT_MODULE})
    def pack(source, destination, meta, signer):
        meta = deepcopy(meta)
        if meta.get('runtime_platform') == 'macos-arm64':
            meta['runtime_capabilities'][recovery_contract.CAPABILITY] = 1
            meta['git_sha'] = 'f' * 40
        return prior(source, destination, meta, signer)
    monkeypatch.setattr(native_fixture, 'pack_tree', pack)


@pytest.fixture
def signed_split(scene, monkeypatch):
    c = scene; base, root = c.x.base, c.x.root
    for module in (upgrade,):
        monkeypatch.setattr(module, 'BASE', base); monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(upgrade, 'DATA', base.parent)
    monkeypatch.setattr(upgrade, '_entry', lambda: None)
    monkeypatch.setattr(upgrade, 'verify_code', lambda: 'b' * 64)
    monkeypatch.setattr(recovery, '_source', lambda: 'f' * 40)
    owner_uid, owner_gid = base.stat().st_uid, base.stat().st_gid
    monkeypatch.setattr(upgrade, '_owned', lambda info, uid, gid: (info.st_uid, info.st_gid) == (owner_uid, owner_gid))
    monkeypatch.setattr(boot, 'no_acl', lambda *a: None)
    # The fixture's synthetic accounts stand in for kernel root/dedicated UID.
    monkeypatch.setattr(keychain, '_identity', lambda *a, **kw: None)
    monkeypatch.setattr(boot, 'modules', lambda: (SimpleNamespace(_parent=lambda: None,
        permitted_groups=lambda gid: {499}), keychain))
    base.chmod(0o755); root.chmod(0o700)
    (root / 'control').chmod(0o700)
    for name in ('operation.lock', 'service-supervisor.lock'): put(root / 'control' / name, b'0')
    for name in ('stdout', 'stderr'): put(base / ('system-service.' + name + '.log'), b'old log\n', 0o660)
    put(base / 'publication.lock', b'publication lock\n')
    @contextmanager
    def publication_lock():
        with (base / 'publication.lock').open('rb') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try: yield
            finally: fcntl.flock(stream, fcntl.LOCK_UN)
    monkeypatch.setattr(upgrade.publication, 'lock', publication_lock)
    # Replace only the scene's not-yet-executed fixture plan. All ensuing
    # original evidence is written by real prepare/apply, not fabricated.
    c.upgrade.plan_path.unlink(); c.upgrade.plan_path.parent.rmdir()
    old = system.metadata(c.x.id); new = system.metadata(c.upgrade.plan['new_package_sha256'])
    put(system.CONTROL, boot.canonical(system.record_for(old, True)), 0o644)
    put(system.PLIST, system._plist(old['package'], old['publisher_sha256']), 0o644)
    ids = {}
    def identity(pid, meta, born):
        micro = int(born * 1000000)
        return dict(pid=pid, ppid=1, uid=499, ruid=499, gid=499, rgid=499,
            birth_sec=micro // 1000000, birth_usec=micro % 1000000,
            exe=str((meta['tree'] / 'runtime/bin/python3.12').resolve()))
    ids[20001] = identity(20001, old, time.time() - 60)
    os_state = {'job': {'state': 'running', 'pid': 20001}, 'exited': False}
    monkeypatch.setattr(upgrade, '_job', lambda: deepcopy(os_state['job']))
    monkeypatch.setattr(upgrade, '_service_pids', lambda: frozenset(ids))
    monkeypatch.setattr(upgrade, 'process_identity', lambda pid: deepcopy(ids[pid]))
    class Watch:
        def __init__(self, identity): assert ids[identity['pid']] == identity
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def poll(self, timeout=0): return os_state['exited']
    monkeypatch.setattr(upgrade, 'ExitWatch', Watch)
    phases = []
    def launch(action):
        if action == 'bootout':
            ids.pop(20001); os_state.update(job=None, exited=True)
        elif action == 'bootstrap':
            ids[20002] = identity(20002, new, time.time())
            os_state['job'] = {'state': 'running', 'pid': 20002}
            value = dict(status='manually-stopped', pid=20002, configured_mode='factory', home_rehearsal=False,
                updated_at=datetime.now(timezone.utc).isoformat(), requires_user_login=False,
                keychain_canary_verified=True, system_identity=dict(uid=499, euid=499, gid=499, egid=499, kernel_groups=[499]))
            put(root / 'control/service-status.json', boot.canonical(value))
    monkeypatch.setattr(upgrade, '_launch', launch)
    def ipc(plan, sha, meta, phase, fd=None):
        phases.append(phase)
        req = dict(type=original.REQUEST, attempt_id=plan['attempt_id'], plan_sha256=sha,
                   challenge='e' * 32, phase=phase, lock_fd=fd)
        # Only the Popen boundary is adapted; the exact production worker does
        # its actual authenticated -> configuration -> signed tree -> FD checks.
        result = worker.run(req, script=worker.__file__, executable=c.upgrade.executable)
        return original.validate_reply(result, req, plan, sha)
    monkeypatch.setattr(upgrade, '_worker', ipc)
    plan = upgrade.make_plan(old['package'], new['package'])
    raw = original.canonical(plan); digest = original.digest(raw)
    upgrade.prepare(raw, digest)
    real_cas = upgrade._cas
    def interrupted(parent, name, previous, replacement, temporary):
        if temporary.endswith('-select-new.json'): raise OSError('actual original CAS interrupted')
        return real_cas(parent, name, previous, replacement, temporary)
    monkeypatch.setattr(upgrade, '_cas', interrupted)
    with pytest.raises(upgrade.UpgradeError): upgrade.apply(plan['attempt_id'], digest)
    monkeypatch.setattr(upgrade, '_cas', real_cas)
    assert phases == ['stop'] and os_state['job'] is None and not ids
    c.recovery = SimpleNamespace(plan=plan, raw=raw, sha=digest, phases=phases, old=old, new=new,
        slot=base / ('system-upgrade-' + plan['attempt_id']))
    return c


@pytest.mark.parametrize('fault', [None, 'signature', 'new-tree', 'old-tree', 'canary', 'restore-anchor'])
def test_real_signed_worker_qualifies_split_before_monitor_enable(signed_split, monkeypatch, fault):
    c = signed_split; r = c.recovery
    authority = authority_bytes(c); state = (c.x.root / 'state.json').read_bytes()
    evidence = {p.name: p.read_bytes() for p in r.slot.iterdir()}
    value = recovery.make_plan(r.plan['attempt_id']); raw = original.canonical(value); digest = original.digest(raw)
    recovery.prepare(raw, digest)
    if fault == 'signature':
        # Owner trust anchor changes after approval; raw activation/gate remain
        # identical, so rejection must come from real configuration authentication.
        path = c.owner['trust'].MAC_PUBLIC; path.write_bytes(b'bad owner signing public key')
    elif fault in ('new-tree', 'old-tree'):
        meta = r.new if fault == 'new-tree' else r.old
        (meta['tree'] / worker.MODULE).write_bytes(b'bad signed source')
    elif fault == 'canary': c.upgrade.canary.side_effect = ValueError('synthetic unavailable canary')
    elif fault == 'restore-anchor': c.owner['checkpoint']['state_sha256'] = 'f' * 64
    if fault:
        with pytest.raises(ValueError): recovery.apply(value['recovery_id'], digest)
        assert json.loads(system.CONTROL.read_bytes())['enabled'] is False
    else:
        result = recovery.apply(value['recovery_id'], digest)
        assert result['activation_preserved'] and result['system_keychain_canary_verified']
        assert not result['activation_performed'] and not result['erp_started']
        assert r.phases == ['stop', 'verify']
        assert json.loads(system.CONTROL.read_bytes())['enabled'] is True
        c.upgrade.canary.assert_called_once_with(c.x.context)
    assert (c.x.root / 'state.json').read_bytes() == state
    assert authority_bytes(c) == authority
    assert {p.name: p.read_bytes() for p in r.slot.iterdir()} == evidence
    for guard in c.upgrade.forbidden: guard.assert_not_called()
