"""History preservation in real signed/encrypted synthetic recovery archives.

The initial history state models the future native-binding result. This suite
does not claim that dedicated-UID binding or a root installation has occurred.
"""
from contextlib import closing
import shutil
import sqlite3
from types import SimpleNamespace
import zipfile

import pytest

from desktop_assistant import credential_restore, credential_store, credential_transfer
from desktop_assistant import cleanup, retention, restore_provenance as history
from desktop_assistant.manager import Manager
from desktop_assistant.storage import (database_info, decrypt_file, encrypt_file, extract_verified,
                                      pack_recovery, pack_tree, read_json, sha, write_json)
from tests.desktop_assistant import test_recovery as recovery
from tests.desktop_assistant.test_credential_restore import recovery_case, host_store

PASSWORD = recovery.PASSWORD


def program(case, name, *, revision='r2', provenance=True, migration=None):
    case.release(name, revision=revision)
    package = case.root / (name + '-signed.zip')
    pack_tree(case.root / ('source-' + name), package,
              {'type': 'tianming.release.v1', 'version': name, 'revision': revision,
               'reader_capabilities': {history.CAPABILITY: 1} if provenance else {},
               **({'migration': migration} if migration else {})}, case.key)
    return package


@pytest.fixture
def migrated(recovery_case):
    case = recovery_case
    old = program(case, 'historical-windows', revision='r0', provenance=False)
    authority = program(case, 'historical-authority', revision='r1', provenance=False,
                        migration={'policy': 'preserve_existing_facts_v1',
                                   'rollback_package_sha256': sha(old)})
    current = program(case, 'later-current', revision='r2')
    for package in (old, authority, current): case.manager.stage_release(package)
    state = case.manager.state
    state.update(current=sha(current), schema_authority=None,
                 **{history.RELEASE: sha(old), history.AUTHORITY: sha(authority)})
    write_json(case.manager.root / 'state.json', state)
    with closing(sqlite3.connect(case.manager.root / 'shared/data/carton_erp.sqlite3')) as db:
        db.execute("UPDATE alembic_version SET version_num='r2'")
        db.commit()
    return SimpleNamespace(case=case, old=old, authority=authority, current=current,
                           values=history.identities(state))


def unpack(backup, destination):
    raw = destination.with_suffix('.zip')
    decrypt_file(backup, raw, PASSWORD)
    return extract_verified(raw, destination)


def test_two_full_recoveries_preserve_original_programs_credentials_and_data(migrated, host_store, monkeypatch):
    c = migrated.case
    manager = c.manager
    def forbidden(*args, **kwargs): raise AssertionError('recovery must not execute any runtime')
    monkeypatch.setattr(Manager, 'start', forbidden)
    monkeypatch.setattr(recovery.TestManager, 'start', forbidden)
    monkeypatch.setattr(Manager, 'update', forbidden)
    from desktop_assistant import migration
    monkeypatch.setattr(migration, 'run_migration', forbidden)
    monkeypatch.setattr(migration, 'rehearse', forbidden)
    original_db = sha(manager.root / 'shared/data/carton_erp.sqlite3')
    original_columns = migration.facts(manager.root / 'shared/data/carton_erp.sqlite3')['columns']
    original_columns['email_intake_settings'].remove('encrypted_secret')
    original_facts = migration.facts(manager.root / 'shared/data/carton_erp.sqlite3', original_columns)
    original_files = {k: v for k, v in migration.files(manager.root / 'shared').items()
                      if k != 'data/carton_erp.sqlite3'}
    first = manager.backup(PASSWORD, c.nas)
    metadata = unpack(first, c.root / 'first-payload')
    assert history.identities(metadata) == migrated.values
    assert metadata['schema_authority'] is None  # Historical r1 is not the active r2 authority.
    assert set(metadata['files']) - {n for n in metadata['files'] if n.startswith('shared/')} == {
        'release.zip', 'credentials.tmencrypted', *history.MEMBERS.values()}
    for name, original in ((history.MEMBERS[history.RELEASE], migrated.old),
                           (history.MEMBERS[history.AUTHORITY], migrated.authority)):
        assert (c.root / 'first-payload' / name).read_bytes() == original.read_bytes()
    restored = recovery.TestManager(c.root / 'first-restore', c.public)
    result = restored.restore(first, PASSWORD, expected_backup_sha256=sha(first))
    assert result['started'] is False and result['credentials']['status'] == 'restored'
    assert history.identities(restored.state) == migrated.values
    assert restored.state['current'] == sha(migrated.current)
    assert restored.state['restored_backup_sha256'] == sha(first)
    monkeypatch.setattr(credential_transfer, 'unprotect', credential_store.unprotect)
    second = restored.backup(PASSWORD, c.nas)
    final = recovery.TestManager(c.root / 'second-restore', c.public)
    final.restore(second, PASSWORD, expected_backup_sha256=sha(second))
    assert sha(second) != sha(first)
    assert final.state['restored_backup_sha256'] == sha(second)
    assert history.identities(final.state) == migrated.values
    assert database_info(final.root / 'shared/data/carton_erp.sqlite3') == database_info(manager.root / 'shared/data/carton_erp.sqlite3')
    assert migration.facts(final.root / 'shared/data/carton_erp.sqlite3', original_columns) == original_facts
    assert {k: v for k, v in migration.files(final.root / 'shared').items()
            if k != 'data/carton_erp.sqlite3'} == original_files
    assert (final.root / 'shared/data/drawing.pdf').read_bytes() == b'synthetic attachment'
    with closing(sqlite3.connect(final.root / 'shared/data/carton_erp.sqlite3')) as db:
        assert db.execute('SELECT version,automatic_enabled,uid FROM email_intake_settings').fetchone() == (9, 1, 'read-position-42')
        assert host_store[db.execute('SELECT encrypted_secret FROM email_intake_settings').fetchone()[0]] == 'synthetic-secret-mailbox'
    assert host_store[read_json(final.root / 'preferences.json')['protected_password']] == 'synthetic-secret-backup'
    assert host_store[read_json(final.root / 'control/ai-provider.json')['protected_api_key']] == 'synthetic-secret-openai'
    assert sha(manager.root / 'shared/data/carton_erp.sqlite3') == original_db
    # Exercise actual retention selection/deletion on synthetic packages, with
    # explicit local archive/idle adapters, never on the real NAS or a runtime.
    state = final.state; state['operation'] = 'update'; write_json(final.root / 'state.json', state)
    obsolete = program(c, 'obsolete')
    obsolete_id = final.stage_release(obsolete)['id']
    archive = c.nas / 'releases'; archive.mkdir()
    shutil.copyfile(obsolete, archive / (obsolete_id + '.zip'))
    monkeypatch.setattr(retention, 'assert_idle', lambda path: None)
    monkeypatch.setattr(cleanup, 'assert_idle', lambda path: None)
    monkeypatch.setattr(retention, 'archive_synced', lambda path: True)
    report = retention.cleanup_releases(final, archive)
    assert set(migrated.values.values()) <= set(report['protected'])
    assert obsolete_id in report['removed_releases'] and obsolete_id in report['removed_packages'], report
    for identity in migrated.values.values():
        assert sha(final.root / 'packages' / (identity + '.zip')) == identity
        assert (final.root / 'releases' / identity).is_dir()


@pytest.mark.parametrize('values', [
    {history.RELEASE: 'a' * 64}, {history.AUTHORITY: None},
    {history.RELEASE: False, history.AUTHORITY: None},
    {history.RELEASE: '', history.AUTHORITY: None},
    {history.RELEASE: 'a' * 64, history.AUTHORITY: False},
    {history.RELEASE: 'A' * 64, history.AUTHORITY: None},
])
def test_partial_or_malformed_history_blocks_backup_and_all_retention(migrated, monkeypatch, values):
    c = migrated.case
    state = {k: v for k, v in c.manager.state.items() if k not in history.STATE_KEYS}
    state.update(values, operation='update')
    write_json(c.manager.root / 'state.json', state)
    before = sorted(p.name for p in (c.manager.root / 'packages').iterdir())
    with pytest.raises(ValueError, match='来源'): c.manager.backup(PASSWORD, c.nas)
    monkeypatch.setattr(retention, 'assert_idle', lambda path: pytest.fail('must not reach deletion'))
    with pytest.raises(ValueError, match='来源'): retention.cleanup_releases(c.manager, c.nas)
    assert sorted(p.name for p in (c.manager.root / 'packages').iterdir()) == before
    assert not list(c.nas.iterdir())


@pytest.mark.parametrize('method', ['update', 'rollback', 'feed'])
@pytest.mark.parametrize('revision', ['r2', 'r3'])
def test_unsupported_program_is_rejected_before_stop_or_migration(migrated, monkeypatch, method, revision):
    c = migrated.case
    package = program(c, 'cannot-preserve', revision=revision, provenance=False,
                      migration={'policy': 'preserve_existing_facts_v1', 'from_revision': 'r2',
                                 'rollback_package_sha256': sha(migrated.current)})
    identity = c.manager.stage_release(package)['id']
    assert not c.manager.compatible(identity, revision)
    assert c.manager.compatible(sha(migrated.current), 'r2')
    monkeypatch.setattr(c.manager, 'stop', lambda: pytest.fail('must reject before stopping'))
    with pytest.raises(ValueError, match='来源'):
        if method == 'feed': c.manager.update_from_feed(package, PASSWORD, c.nas)
        else: c.manager.update(package, PASSWORD, c.nas, rollback=method == 'rollback')
    assert c.manager.state['current'] == sha(migrated.current) and not list(c.nas.iterdir())


@pytest.mark.parametrize('problem', ['old-reader', 'partial-state'])
def test_running_process_does_not_bypass_provenance_gate(migrated, monkeypatch, problem):
    c = migrated.case
    state = c.manager.state
    if problem == 'old-reader':
        old = program(c, 'running-old', provenance=False)
        state['current'] = c.manager.stage_release(old)['id']
    else:
        state.pop(history.AUTHORITY)
    write_json(c.manager.root / 'state.json', state)
    # The synthetic executable is authenticated but never executed on this Mac.
    runtime = c.manager._runtime_python(state['current'], runnable=False)
    monkeypatch.setattr(c.manager, '_runtime_python', lambda identity: runtime)
    monkeypatch.setattr(c.manager, '_process', lambda: (object(), {'exe': str(runtime)}))
    monkeypatch.setattr(c.manager, 'stop', lambda: pytest.fail('must not stop a process'))
    with pytest.raises(ValueError, match='来源'): Manager.start(c.manager)


@pytest.mark.parametrize('role', [history.RELEASE, history.AUTHORITY, 'current'])
def test_backup_rejects_late_cached_package_replacement(migrated, monkeypatch, role):
    c = migrated.case
    export = credential_transfer.export
    def replace_after_initial_validation(*args, **kwargs):
        result = export(*args, **kwargs)
        identity = c.manager.state[role]
        (c.manager.root / 'packages' / (identity + '.zip')).write_bytes(b'late synthetic replacement')
        return result
    monkeypatch.setattr(credential_transfer, 'export', replace_after_initial_validation)
    with pytest.raises(ValueError, match='原包'): c.manager.backup(PASSWORD, c.nas)
    assert not c.manager.state.get('last_backup')
    assert not list(c.nas.glob('*.tmbackup'))
    assert not (c.manager.root / 'control/backup-receipts').exists()


def test_feed_rejects_unsupported_intermediate_even_if_latest_supports_history(migrated, monkeypatch):
    c = migrated.case
    intermediate = program(c, 'middle-old', revision='r3', provenance=False,
                           migration={'policy': 'preserve_existing_facts_v1', 'from_revision': 'r2',
                                      'rollback_package_sha256': sha(migrated.current)})
    c.manager.stage_release(intermediate)
    latest = program(c, 'latest-good', revision='r4',
                     migration={'policy': 'preserve_existing_facts_v1', 'from_revision': 'r3',
                                'rollback_package_sha256': sha(intermediate)})
    monkeypatch.setattr(c.manager, 'stop', lambda: pytest.fail('must check the whole chain first'))
    with pytest.raises(ValueError, match='来源'): c.manager.update_from_feed(latest, PASSWORD, c.nas)
    assert c.manager.state['current'] == sha(migrated.current)
    assert not list(c.nas.iterdir())


def test_interrupted_update_preserves_state_before_an_unsupported_target(migrated, monkeypatch):
    c = migrated.case
    target = program(c, 'interrupted-old', revision='r3', provenance=False,
                     migration={'policy': 'preserve_existing_facts_v1', 'from_revision': 'r2',
                                'rollback_package_sha256': sha(migrated.current)})
    identity = c.manager.stage_release(target)['id']
    state = c.manager.state; state.update(operation='migration_failed', migration_target=identity)
    state_path = c.manager.root / 'state.json'; write_json(state_path, state)
    before = state_path.read_bytes()
    monkeypatch.setattr(c.manager, '_runtime_python', lambda *a: pytest.fail('must reject before runtime access'))
    with pytest.raises(ValueError, match='来源'): c.manager.recover_interrupted_update()
    assert state_path.read_bytes() == before


@pytest.mark.parametrize('change', ['missing', 'extra', 'partial', 'hash', 'authority-binding', 'capability', 'payload-tamper', 'current-payload-tamper'])
def test_bad_history_rejected_before_credentials_or_shared_promotion(migrated, host_store, monkeypatch, change):
    c = migrated.case
    backup = c.manager.backup(PASSWORD, c.nas)
    payload = c.root / 'edited-payload'
    metadata = unpack(backup, payload)
    packages = {name: payload / name for name in metadata['files'] if not name.startswith('shared/')}
    if change == 'missing': packages.pop(history.MEMBERS[history.AUTHORITY])
    elif change == 'extra':
        metadata.pop(history.RELEASE); metadata.pop(history.AUTHORITY)
    elif change == 'partial': metadata.pop(history.AUTHORITY)
    elif change == 'hash': metadata[history.RELEASE] = 'a' * 64
    elif change == 'authority-binding':
        wrong = program(c, 'wrong-authority', revision='r1', provenance=False,
                        migration={'policy': 'preserve_existing_facts_v1', 'rollback_package_sha256': 'b' * 64})
        packages[history.MEMBERS[history.AUTHORITY]] = wrong
        metadata[history.AUTHORITY] = sha(wrong)
    elif change == 'capability':
        old = program(c, 'unsupported-current', provenance=False)
        packages['release.zip'] = old; metadata.update(release=sha(old), version='unsupported-current')
    elif change in ('payload-tamper', 'current-payload-tamper'):
        path = packages[history.MEMBERS[history.RELEASE] if change == 'payload-tamper' else 'release.zip']
        with zipfile.ZipFile(path) as archive: entries = [(i, archive.read(i)) for i in archive.infolist()]
        with zipfile.ZipFile(path, 'w') as archive:
            for entry, data in entries: archive.writestr(entry, b'tampered code' if entry.filename == 'main.py' else data)
        metadata[history.RELEASE if change == 'payload-tamper' else 'release'] = sha(path)
    raw = c.root / 'edited-recovery.zip'; encrypted = c.root / 'edited.tmbackup'
    pack_recovery(payload / 'shared', packages, raw, {k: v for k, v in metadata.items() if k != 'files'})
    encrypt_file(raw, encrypted, PASSWORD)
    target = recovery.TestManager(c.root / 'rejected', c.public)
    monkeypatch.setattr(credential_restore, 'prepare', lambda *a: pytest.fail('no host credential writes'))
    with pytest.raises(ValueError): target.restore(encrypted, PASSWORD)
    assert target.state['current'] is None and not list((target.root / 'shared').iterdir())
    assert host_store == {}


@pytest.mark.parametrize('caps', [None, [], {history.CAPABILITY: True}, {history.CAPABILITY: 1, 'future': False}])
def test_capability_is_strict(caps):
    assert not history.supported({'reader_capabilities': caps})


def test_original_without_authority_and_legacy_without_history(migrated):
    c = migrated.case
    state = c.manager.state
    state[history.AUTHORITY] = None
    values, paths = history.for_backup(c.manager.root, state, c.manager.manifest(), c.public)
    assert values[history.AUTHORITY] is None and set(paths) == {history.MEMBERS[history.RELEASE]}
    assert history.for_recovery(c.root, {'files': {}}, {}, c.public) == ({}, {})
    assert history.for_backup(c.root, {}, {}, c.public) == ({}, {})


def test_legacy_backup_members_and_receipt_contract_are_unchanged(recovery_case, host_store):
    c = recovery_case
    backup = c.manager.backup(PASSWORD, c.nas)
    metadata = unpack(backup, c.root / 'legacy-payload')
    assert not any(key in metadata for key in history.STATE_KEYS)
    assert {n for n in metadata['files'] if not n.startswith('shared/')} == {'release.zip', 'credentials.tmencrypted'}
    receipt = read_json(c.manager.root / 'control/backup-receipts' / (backup.stem + '.json'))
    assert set(receipt) == {'path', 'sha256', 'size', 'verified', 'storage', 'database'}
    target = recovery.TestManager(c.root / 'legacy-restore', c.public)
    result = target.restore(backup, PASSWORD)
    assert set(result) == {'version', 'data_time', 'started', 'credentials'}
    assert history.identities(target.state) == {}
