"""The installed assistant must not reopen an old writer after activation."""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from desktop_assistant.storage import (pack_tree, pack_recovery, write_json, read_json, sha,
                                      decrypt_file, extract_verified, encrypt_file)
from desktop_assistant.manager import Manager
from desktop_assistant import delivery_dispatch_contract as contract
from tests.desktop_assistant import test_recovery as recovery


@pytest.fixture
def installation():
    fixture = recovery.RecoveryTests()
    fixture.setUp()
    try:
        ids = {}
        for name, caps in [('dispatch-old', {}), ('dispatch-new', {'delivery_dispatch_v1': 1})]:
            fixture.release(name, 'r1')
            package = fixture.root / (name + '-signed.zip')
            pack_tree(fixture.root / ('source-' + name), package, {
                'type': 'tianming.release.v1', 'version': name, 'revision': 'r1',
                'reader_capabilities': caps}, fixture.key)
            ids[name] = fixture.manager.stage_release(package)['id']
        fixture.ids = ids
        yield fixture
    finally:
        fixture.tearDown()


def activate(fixture):
    manager = fixture.manager
    contract = {'reader_capability': 'delivery_dispatch_v1', 'version': 1,
                'activated_package': fixture.ids['dispatch-new'],
                'activated_at': '2026-10-10T12:00:00+08:00',
                'pre_activation_package': fixture.ids['dispatch-old'],
                'pre_activation_backup': 'synthetic-backup.tmbackup'}
    marker = manager.root / 'shared/data/delivery-dispatch-contract.json'
    write_json(marker, contract)
    index = {'reader_capability': 'delivery_dispatch_v1', 'version': 1,
             'contract_sha256': sha(marker)}
    state = manager.state
    state.update(current=fixture.ids['dispatch-new'], previous=fixture.ids['dispatch-old'],
                 delivery_dispatch_activation=index)
    write_json(manager.root/'state.json', state)
    return marker, contract, index


def test_active_marker_refuses_old_same_revision_and_forged_cache(installation):
    fixture = installation
    activate(fixture)
    manager = fixture.manager
    assert manager.compatible(fixture.ids['dispatch-new'], 'r1')
    assert not manager.compatible(fixture.ids['dispatch-old'], 'r1')
    cached = manager.manifest(fixture.ids['dispatch-old'])
    cached['reader_capabilities'] = {'delivery_dispatch_v1': 1}
    write_json(manager.root/'releases'/fixture.ids['dispatch-old']/'manifest.json', cached)
    assert not manager.compatible(fixture.ids['dispatch-old'], 'r1')


def test_managed_writer_cannot_hide_control_or_downgrade_environment(installation, monkeypatch):
    fixture = installation
    marker, _, _ = activate(fixture)
    db = fixture.manager.root/'shared/data/carton_erp.sqlite3'
    monkeypatch.delenv('TM_ERP_CONTROL', raising=False)
    monkeypatch.setenv('ERP_ENVIRONMENT', 'test')
    before = sha(db)
    contract.require_managed_dispatch_activation(db)
    marker.unlink()
    with pytest.raises(ValueError):
        contract.require_managed_dispatch_activation(db)
    assert sha(db) == before
    with pytest.raises(ValueError):
        contract.require_managed_dispatch_activation(fixture.root/'elsewhere.db', fixture.manager.root/'control')
    monkeypatch.setenv('ERP_ENVIRONMENT', 'production')
    with pytest.raises(ValueError):
        contract.require_managed_dispatch_activation(fixture.root/'elsewhere.db')
    monkeypatch.setenv('ERP_ENVIRONMENT', 'test')
    contract.require_managed_dispatch_activation(fixture.root/'isolated-test.db')


def test_lost_marker_detected_by_record_and_invalid_marker_not_legacy(installation):
    fixture = installation
    marker, _, _ = activate(fixture)
    marker.write_text('{invalid', encoding='utf-8')
    assert not fixture.manager.compatible(fixture.ids['dispatch-new'], 'r1')
    marker.unlink()
    state = fixture.manager.state
    state.pop(contract.STATE_KEY)
    write_json(fixture.manager.root/'state.json', state)
    dbpath = fixture.manager.root/'shared/data/carton_erp.sqlite3'
    with closing(sqlite3.connect(dbpath)) as db:
        db.execute('CREATE TABLE finance_idempotency_records(id INTEGER PRIMARY KEY,action TEXT)')
        db.execute('INSERT INTO finance_idempotency_records(action) VALUES (?)', (contract.ACTION,))
        db.commit()
    before = sha(dbpath)
    assert not fixture.manager.compatible(fixture.ids['dispatch-old'], 'r1')
    assert not fixture.manager.compatible(fixture.ids['dispatch-new'], 'r1')
    assert sha(dbpath) == before


def test_unknown_or_boolean_activation_version_cannot_alias_v1(installation):
    fixture = installation
    activate(fixture)
    state = fixture.manager.state
    for version in (True, 2):
        state[contract.STATE_KEY]['version'] = version
        write_json(fixture.manager.root/'state.json', state)
        assert not fixture.manager.compatible(fixture.ids['dispatch-new'], 'r1')


def test_reader_rejection_precedes_migration_and_service_stop(installation, monkeypatch):
    fixture = installation
    activate(fixture)
    manager = fixture.manager
    stopped = []
    monkeypatch.setattr(manager, 'stop', lambda: stopped.append(True))
    # Even an apparent migration authorization cannot bypass the new writer gate.
    old_id = fixture.ids['dispatch-old']
    original_stage = manager.stage_release
    def stage(package):
        result = original_stage(package)
        result['migration'] = {'policy': 'preserve_existing_facts_v1', 'from_revision':'r1',
                               'rollback_package_sha256':manager.state['current']}
        return result
    monkeypatch.setattr(manager, 'stage_release', stage)
    with pytest.raises(ValueError, match='发货保护'):
        manager._update_locked(manager.root/'packages'/(old_id+'.zip'), recovery.PASSWORD, fixture.nas)
    assert not stopped


def test_new_contract_is_persisted_before_start_and_failed_start_never_runs_old(installation, monkeypatch):
    import desktop_assistant.nas_probe as probe
    fixture = installation
    manager = fixture.manager
    monkeypatch.setattr(probe, 'check_before_stop', lambda path: None)
    previous = manager.state['current']
    before = sha(manager.root/'shared/data/carton_erp.sqlite3')
    attempted = []
    def fail_start():
        attempted.append(manager.state['current'])
        value = manager._check_dispatch_reader(manager.state['current'], require_active=True)
        assert value['contract']['pre_activation_package'] == previous
        assert Path(value['contract']['pre_activation_backup']).is_file()
        assert manager.state['previous'] is None
        raise ValueError('synthetic new-process failure')
    monkeypatch.setattr(manager, 'start', fail_start)
    candidate = fixture.ids['dispatch-new']
    with pytest.raises(ValueError, match='向前修复'):
        manager._update_locked(manager.root/'packages'/(candidate+'.zip'), recovery.PASSWORD, fixture.nas)
    assert attempted == [candidate]
    assert manager.state['operation'] == 'update_failed_reader_contract'
    assert manager.state['current'] == candidate
    assert manager.state['previous'] is None
    assert sha(manager.root/'shared/data/carton_erp.sqlite3') == before


def test_start_rejects_incompatible_reader_even_when_process_is_running(installation, monkeypatch):
    fixture = installation
    activate(fixture)
    state = fixture.manager.state
    state['current'] = fixture.ids['dispatch-old']
    write_json(fixture.manager.root/'state.json', state)
    monkeypatch.setattr(fixture.manager, '_process', lambda: (object(), {'exe':'old.exe'}))
    # Authenticate this synthetic Windows runtime without trying to run it on
    # Mac; this test targets the independent reader gate before process reuse.
    runtime = fixture.manager._runtime_python
    monkeypatch.setattr(fixture.manager, '_runtime_python', lambda identity: runtime(identity, runnable=False))
    with pytest.raises(ValueError, match='发货保护'):
        Manager.start(fixture.manager)


def test_actual_encrypted_backup_restore_preserves_zero_record_activation(installation):
    fixture = installation
    marker, _, index = activate(fixture)
    original_db = sha(fixture.manager.root/'shared/data/carton_erp.sqlite3')
    original_marker = sha(marker)
    backup = fixture.manager._backup_stopped(recovery.PASSWORD, fixture.nas)
    recovered = recovery.TestManager(fixture.root/'restored', fixture.public)
    result = recovered.restore(backup, recovery.PASSWORD)
    assert result['started'] is False
    assert sha(recovered.root/'shared/data/carton_erp.sqlite3') == original_db
    assert sha(recovered.root/'shared'/contract.MARKER) == original_marker
    assert recovered.state[contract.STATE_KEY] == index
    recovered._check_dispatch_reader(recovered.state['current'], require_active=True)
    contract.require_managed_dispatch_activation(recovered.root/'shared/data/carton_erp.sqlite3')


@pytest.mark.parametrize('damage', ['metadata', 'marker', 'old-reader'])
def test_authenticated_but_incompatible_backup_never_promotes(installation, damage):
    fixture = installation
    activate(fixture)
    backup = fixture.manager._backup_stopped(recovery.PASSWORD, fixture.nas)
    raw = fixture.root/'original-recovery.zip'
    decrypt_file(backup, raw, recovery.PASSWORD)
    payload = fixture.root/'payload'
    metadata = extract_verified(raw, payload)
    packages = {'release.zip':payload/'release.zip'}
    if damage == 'metadata':
        metadata.pop(contract.STATE_KEY)
    elif damage == 'marker':
        (payload/'shared'/contract.MARKER).unlink()
    else:
        old = fixture.ids['dispatch-old']
        metadata['release'] = old
        packages['release.zip'] = fixture.manager.root/'packages'/(old+'.zip')
    forged_raw = fixture.root/'damaged-recovery.zip'
    pack_recovery(payload/'shared', packages, forged_raw, metadata)
    damaged = fixture.root/'damaged.tmbackup'
    encrypt_file(forged_raw, damaged, recovery.PASSWORD)
    recovered = recovery.TestManager(fixture.root/'rejected', fixture.public)
    with pytest.raises(ValueError, match='发货保护'):
        recovered.restore(damaged, recovery.PASSWORD)
    assert recovered.state['current'] is None
    assert not any((recovered.root/'shared').iterdir())


def test_new_first_onboarding_rejected_before_stop_and_pending_unchanged(installation, monkeypatch):
    from desktop_assistant import onboarding
    fixture = installation
    manager = fixture.manager
    state = manager.state
    state.update(current=fixture.ids['dispatch-new'], onboarding_pending=True)
    write_json(manager.root/'state.json', state)
    called = []
    monkeypatch.setattr(manager, '_backup_stopped', lambda *args: called.append('backup'))
    monkeypatch.setattr(manager, 'start', lambda: called.append('start'))
    with pytest.raises(ValueError, match='首次接入'):
        onboarding._finish_locked(manager, recovery.PASSWORD, fixture.nas)
    assert manager.state == state and called == []
    # Exercise the real onboard entry, stubbing only filesystem/runtime preflight.
    empty = recovery.TestManager(fixture.root/'first-install', fixture.public)
    source = fixture.root/'unmanaged-original'
    source.mkdir()
    (source/'.env').write_text('synthetic')
    monkeypatch.setattr(onboarding, '_check_nas', lambda *args: None)
    monkeypatch.setattr(onboarding, 'check_source_environment', lambda *args: None)
    monkeypatch.setattr(onboarding, 'check_source_data', lambda *args: None)
    monkeypatch.setattr(onboarding, 'stop_original', lambda *args: called.append('stop'))
    with pytest.raises(ValueError, match='首次接入'):
        onboarding.onboard(empty, source, manager.root/'packages'/(fixture.ids['dispatch-new']+'.zip'),
                           recovery.PASSWORD, fixture.nas)
    assert called == [] and empty.state['current'] is None
