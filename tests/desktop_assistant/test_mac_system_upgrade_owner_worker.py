"""Signed owner authority + signed packages through the actual upgrade worker.

All backups and signatures are freshly synthetic. Root/UID and existing mounted
proof are explicit fixture adapters; no installed instance or NAS is accessed.
"""
from copy import deepcopy
import json
import sqlite3
import sys
import time
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.exceptions import InvalidSignature

from desktop_assistant import mac_system_upgrade_worker as worker
from desktop_assistant import mac_system_upgrade_contract as upgrade_contract
from desktop_assistant import mac_service_manage as manage
from app.core import mac_service_keychain as keychain
from tests.desktop_assistant.test_owner_activation_runtime import (
    owner_case, current_case, current_protocol, runtime, published, contract, protocol, boot, system,
    Manager, activated, root_case, target, case, recovery_case, host_store, signed_activation_protocol,
    native_fixture, execution_protocol, approve_update, owner)
from tests.desktop_assistant.test_mac_system_upgrade_worker import parent_lock, assert_parent_still_locked
from desktop_assistant.storage import sha
from desktop_assistant.mac_publisher_trust import verify_manifest

REAL_METADATA = system.metadata
REAL_PROGRAM_MANIFEST = system._program_manifest
REAL_STOP = Manager.stop


@pytest.fixture(autouse=True)
def monitor_protocol(current_protocol, monkeypatch, request):
    previous = native_fixture.pack_tree
    files = owner.FILES | {worker.MODULE, worker.CONTRACT_MODULE, system.ENTRY,
                          'desktop_assistant/mac_system_service.py',
                          'desktop_assistant/mac_service_manage.py'}
    monkeypatch.setattr(native_fixture.publication, 'SERVICE_RESTORE_FILES',
                        native_fixture.publication.SERVICE_RESTORE_FILES | files)
    def pack(source, destination, meta, signer):
        if meta.get('runtime_platform') == 'macos-arm64':
            meta = deepcopy(meta)
            meta['runtime_capabilities'].update({owner.CAPABILITY: 1, worker.CAPABILITY: 1,
                                                upgrade_contract.FACTORY_CAPABILITY: 1})
            capability = getattr(request, 'param', 1)
            if capability is None: meta['runtime_capabilities'].pop(upgrade_contract.FACTORY_CAPABILITY)
            else: meta['runtime_capabilities'][upgrade_contract.FACTORY_CAPABILITY] = capability
        return previous(source, destination, meta, signer)
    monkeypatch.setattr(native_fixture, 'pack_tree', pack)


@pytest.fixture
def scene(owner_case, monkeypatch):
    c = owner_case
    # The owner fixture adapts trust-file ownership; retain actual Ed25519
    # package verification when using that adapter for a later published tree.
    c.owner['trust'].verify_manifest = verify_manifest
    # Preserve the already-restored business current while approving another
    # signed monitor. This helper normally also switches current; undo only that
    # fixture side effect before constructing the upgrade plan.
    state_path = c.x.root / 'state.json'
    original = state_path.read_bytes()
    identity, slot = approve_update(c)
    for published_slot in (c.x.slot, slot):
        with zipfile.ZipFile(published_slot / 'package.zip') as archive:
            raw = archive.read('manifest.json')
        for kind in ('server', 'migration'):
            (published_slot / kind / 'manifest.json').write_bytes(raw)
    state = json.loads(original); state['manual_stop'] = False
    state_path.write_bytes(boot.canonical(state))
    before = state_path.read_bytes(); stopped = boot.canonical({**state, 'manual_stop': True})
    for module in (worker, system):
        monkeypatch.setattr(module, 'BASE', c.x.base); monkeypatch.setattr(module, 'ROOT', c.x.root)
    monkeypatch.setattr(upgrade_contract, 'BASE', str(c.x.base))
    monkeypatch.setattr(system, 'metadata', REAL_METADATA)
    monkeypatch.setattr(system, '_program_manifest', REAL_PROGRAM_MANIFEST)
    monkeypatch.setattr(boot, 'modules', lambda: (SimpleNamespace(_parent=lambda: None), keychain))
    monkeypatch.setattr(system.publication, 'guard', lambda *a, **kw: None)
    monkeypatch.setattr(worker, '_entry', lambda: None)
    monkeypatch.setattr(worker, '__file__', str(slot / 'migration' / worker.MODULE))
    executable = slot / 'migration/runtime/bin/python3.12'
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setattr(Manager, 'stop', REAL_STOP)
    canary = Mock(); monkeypatch.setattr(system, 'verify_keychain', canary)
    forbidden = []
    def no_operation(*args, **kwargs): raise AssertionError('upgrade cannot start, read business data or access NAS')
    for name in ('start', 'backup', 'restore', '_environment'):
        guard = Mock(side_effect=no_operation); monkeypatch.setattr(Manager, name, guard); forbidden.append(guard)
    for name in ('_contract', 'before_write', 'prepare_destination', 'verified_at'):
        guard = Mock(side_effect=no_operation); monkeypatch.setattr(manage.remote, name, guard); forbidden.append(guard)
    guard = Mock(side_effect=no_operation); monkeypatch.setattr(system, 'launch', guard); forbidden.append(guard)
    guard = Mock(side_effect=no_operation); monkeypatch.setattr(sqlite3, 'connect', guard); forbidden.append(guard)
    from desktop_assistant import credential_store
    for name in ('unprotect', 'unprotect_backup'):
        guard = Mock(side_effect=no_operation); monkeypatch.setattr(credential_store, name, guard); forbidden.append(guard)
    now = int(time.time())
    publisher = sha(c.x.base / 'publisher-public.pem')
    plan = dict(type=upgrade_contract.FACTORY_PLAN, attempt_id='a' * 32, created_at=now - 5, expires_at=now + 3600,
        uid=499, gid=499, code_manifest_sha256='b' * 64,
        old_package_sha256=c.x.id, new_package_sha256=identity, current_package_sha256=c.x.id,
        context_sha256=sha(c.x.base / 'keychain-context.json'), publisher_sha256=publisher,
        registry_sha256=sha(published.REGISTRY), old_control_sha256='c' * 64,
        old_plist_sha256=boot.digest(system._plist(c.x.id, publisher)),
        new_plist_sha256=boot.digest(system._plist(identity, publisher)),
        state_before_sha256=boot.digest(before), state_stopped_sha256=boot.digest(stopped),
        activation_sha256=sha(c.x.base / upgrade_contract.ACTIVATION_NAME),
        owner_publication_sha256=sha(c.x.base / owner.GATE_NAME))
    directory = c.x.base / ('system-upgrade-' + plan['attempt_id']); directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    path = directory / 'plan.json'; path.write_bytes(upgrade_contract.canonical(plan)); path.chmod(0o644)
    request = dict(type=upgrade_contract.REQUEST, attempt_id=plan['attempt_id'], plan_sha256=sha(path),
                   challenge='d' * 32, phase='stop', lock_fd=None)
    system.CONTROL.write_bytes(upgrade_contract.disabled_control_bytes(plan, 'stop')); system.CONTROL.chmod(0o644)
    system.PLIST.write_bytes(system._plist(c.x.id, publisher)); system.PLIST.chmod(0o644)
    lock = c.x.root / 'control/operation.lock'
    def run(phase='stop', fd=None):
        return worker.run({**request, 'phase': phase, 'lock_fd': fd}, script=worker.__file__, executable=executable)
    c.upgrade = SimpleNamespace(plan=plan, request=request, plan_path=path, executable=executable,
        new_slot=slot, before=before, stopped=stopped, canary=canary, forbidden=forbidden, run=run,
        lock_path=lock, base=c.x.base, root=c.x.root)
    return c


def authority_bytes(c):
    return tuple((c.x.base / name).read_bytes() for name in
                 (upgrade_contract.ACTIVATION_NAME, owner.GATE_NAME))


def test_real_signed_owner_configuration_and_two_trees_through_stop_and_verify(scene):
    c = scene; u = c.upgrade; before = authority_bytes(c)
    # Full existing configuration is genuine: owner signature, restored anchors,
    # registry, original request/network and mounted checkpoint proof are checked.
    configured = runtime.configuration(c.x.root, c.x.id, c.t.c.public)
    assert configured['activation_sha256'] == u.plan['activation_sha256']
    assert u.run()['process_stopped'] is True
    assert (c.x.root / 'state.json').read_bytes() == u.stopped
    u.canary.assert_not_called()
    system.CONTROL.write_bytes(upgrade_contract.disabled_control_bytes(u.plan, 'verify'))
    system.PLIST.write_bytes(system._plist(u.plan['new_package_sha256'], u.plan['publisher_sha256']))
    with parent_lock(u) as fd:
        result = u.run('verify', fd)
        assert_parent_still_locked(u)
    assert result['manual_stop'] is result['process_stopped'] is result['home_rehearsal'] is True
    assert result['keychain_canary_verified'] is result['borrowed_lock'] is True
    assert (c.x.root / 'state.json').read_bytes() == u.stopped
    assert json.loads(u.stopped) == {**json.loads(u.before), 'manual_stop': True}
    assert authority_bytes(c) == before
    u.canary.assert_called_once_with(c.x.context)
    for guard in u.forbidden: guard.assert_not_called()


@pytest.mark.parametrize('fault', ['home-plan', 'signature', 'restore-anchor', 'gate-bytes', 'failed',
    'context', 'new-code', 'old-code', 'configuration-none', 'configuration-other-sha', 'configuration-late-gate'])
def test_owner_worker_rejects_invalid_authority_without_state_change(scene, monkeypatch, fault):
    c = scene; u = c.upgrade
    if fault == 'home-plan':
        u.plan['type'] = upgrade_contract.PLAN
        u.plan.pop('activation_sha256'); u.plan.pop('owner_publication_sha256')
    elif fault == 'signature':
        path = c.x.base / upgrade_contract.ACTIVATION_NAME; value = owner.decode(path.read_bytes())
        envelope = owner.decode(value['authorization'].encode()); envelope['signature'] = 'A' * 86 + '=='
        value['authorization'] = owner.canonical(envelope).decode()
        value['authorization_sha256'] = boot.digest(value['authorization'].encode())
        path.write_bytes(owner.canonical(value))
        gate_path = c.x.base / owner.GATE_NAME; gate = owner.decode(gate_path.read_bytes())
        gate['activation_sha256'] = sha(path); gate_path.write_bytes(owner.canonical(gate))
        u.plan.update(activation_sha256=sha(path), owner_publication_sha256=sha(gate_path))
    elif fault == 'restore-anchor':
        path = c.x.root / 'state.json'; value = json.loads(path.read_bytes())
        value['native_restore_binding']['backup_sha256'] = 'f' * 64; path.write_bytes(boot.canonical(value))
        u.plan['state_before_sha256'] = sha(path)
        u.plan['state_stopped_sha256'] = boot.digest(boot.canonical({**value, 'manual_stop': True}))
    elif fault == 'gate-bytes':
        path = c.x.base / owner.GATE_NAME; path.write_bytes(path.read_bytes() + b' ')
    elif fault == 'failed': (c.x.base / owner.FAILED).write_bytes(b'failed')
    elif fault == 'context': u.plan['context_sha256'] = 'f' * 64
    elif fault in ('new-code', 'old-code'):
        slot = u.new_slot if fault == 'new-code' else c.x.slot
        (slot / 'migration' / worker.MODULE).write_bytes(b'tampered')
    elif fault == 'configuration-none': monkeypatch.setattr(runtime, 'configuration', lambda *a: None)
    elif fault == 'configuration-other-sha':
        monkeypatch.setattr(runtime, 'configuration', lambda *a: {'activation_sha256': 'f' * 64})
    else:
        original = runtime.configuration
        def mutate(*args):
            result = original(*args)
            path = c.x.base / owner.GATE_NAME; path.write_bytes(path.read_bytes() + b' ')
            return result
        monkeypatch.setattr(runtime, 'configuration', mutate)
    u.plan_path.write_bytes(upgrade_contract.canonical(u.plan)); u.request['plan_sha256'] = sha(u.plan_path)
    state_before = (c.x.root / 'state.json').read_bytes()
    with pytest.raises((ValueError, OSError, InvalidSignature)): u.run()
    assert (c.x.root / 'state.json').read_bytes() == state_before
    u.canary.assert_not_called()
    for guard in u.forbidden: guard.assert_not_called()


@pytest.mark.parametrize('monitor_protocol', [None, True, '1', 0, 2], indirect=True)
def test_new_signed_factory_monitor_requires_exact_preservation_capability(scene):
    c = scene; u = c.upgrade
    with pytest.raises(ValueError, match='保留既有激活的签名能力'): u.run()
    assert (c.x.root / 'state.json').read_bytes() == u.before
    u.canary.assert_not_called()
    for guard in u.forbidden: guard.assert_not_called()
