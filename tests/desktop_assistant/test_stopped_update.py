"""Synthetic stopped upgrades: real signatures, SQLite and encrypted archives.

The local NAS adapter does not claim Mac root approval, Windows DPAPI or a real
remote transport. Starting/stopping ERP is an assertion failure, never a no-op.
"""
from contextlib import ExitStack, closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

import pytest

from desktop_assistant import manager as module
from desktop_assistant.storage import decrypt_file, extract_verified, pack_tree, read_json, sha, write_json
from tests.desktop_assistant import test_recovery as recovery_fixture
from tests.desktop_assistant.test_mac_published_runtime import case as published_case

PASSWORD = recovery_fixture.PASSWORD


@pytest.fixture
def case():
    fixture = recovery_fixture.RecoveryTests(methodName='runTest')
    fixture.setUp()
    try:
        state = fixture.manager.state
        state.update(manual_stop=True, operation='restore', restored_at='2026-10-10T20:12:10+08:00')
        write_json(fixture.manager.root / 'state.json', state)
        yield fixture
    finally:
        fixture.tearDown()


def forbidden_runtime(manager):
    stack = ExitStack()
    for name in ('start', 'stop', '_activate_dispatch_reader'):
        stack.enter_context(patch.object(manager, name, side_effect=AssertionError(name + ' must not run')))
    for name in ('rehearse', 'run_migration'):
        stack.enter_context(patch('desktop_assistant.migration.' + name,
                                 side_effect=AssertionError(name + ' must not run')))
    stack.enter_context(patch('desktop_assistant.retention.after_update',
                             side_effect=AssertionError('retention must not run')))
    return stack


def test_stopped_update_uses_full_backup_and_never_starts(case):
    manager = case.manager
    before = deepcopy(manager.state)
    database = manager.root / 'shared/data/carton_erp.sqlite3'
    database_sha = sha(database)
    candidate = case.release('two')
    with forbidden_runtime(manager):
        assert manager.update_stopped(candidate, PASSWORD, case.nas) == 'two'
    after = manager.state
    assert after['current'] == sha(candidate)
    assert after['previous'] == before['current']
    assert after['manual_stop'] is True
    assert after['operation'] == 'update'
    assert after['restored_at'] == before['restored_at']
    assert after['update_backup'] == after['last_backup']
    assert sha(database) == database_sha
    backup = module.Path(after['last_backup'])
    assert backup.is_file()
    receipt = read_json(manager.root / 'control/backup-receipts' / (backup.stem + '.json'))
    assert receipt['sha256'] == sha(backup)
    assert receipt['verified'] is True
    assert receipt['storage'] == 'nas'


def replace_state(case, **values):
    state = case.manager.state
    state.update(values)
    write_json(case.manager.root / 'state.json', state)
    return state


def program(case, name, *, capabilities=None, change_model=None):
    case.release(name)
    source = case.root / ('source-' + name)
    model = source / 'app/models/fixture.py'
    if change_model:
        model.write_text(change_model(model.read_text()), encoding='utf-8')
    package = case.root / (name + '-signed.zip')
    pack_tree(source, package, {'type': 'tianming.release.v1', 'version': name, 'revision': 'r1',
                              'reader_capabilities': capabilities or {}}, case.key)
    return package


def assert_preserved(case, state):
    assert case.manager.state == state
    assert not list(case.nas.glob('*.tmbackup'))


@pytest.mark.parametrize('values', [
    {'manual_stop': False}, {'manual_stop': 1}, {'manual_stop': 'true'}, {'manual_stop': None},
    {'operation': 'migration_running'}, {'operation': 'migration_failed'},
    {'operation': 'update_failed'}, {'operation': 'update_failed_reader_contract'},
    {'operation': 'unknown'}, {'operation': False}, {'operation': ['restore']},
    {'onboarding_pending': True}, {'backup_error': 'unconfirmed'},
    {'backup_work_retained': ['/synthetic/preserved-work']},
    {'current': None}, {'current': '../outside'}, {'current': 'A' * 64},
])
@pytest.mark.parametrize('same_current', [False, True])
def test_unsettled_state_rejected_before_staging_even_same_package(case, values, same_current):
    candidate = case.package if same_current else case.release('two')
    before = replace_state(case, **values)
    with forbidden_runtime(case.manager), patch.object(case.manager, 'stage_release',
            side_effect=AssertionError('must reject before staging')):
        with pytest.raises(ValueError):
            case.manager.update_stopped(candidate, PASSWORD, case.nas)
    assert_preserved(case, before)


@pytest.mark.parametrize('operation', [None, 'import_existing', 'update', 'rollback', 'migration_recovered', 'restore'])
def test_successful_operation_history_and_empty_backup_error_are_accepted(case, operation):
    before = replace_state(case, operation=operation, backup_error=None,
                           migration_report='retained historical report', migration_target='a' * 64)
    with forbidden_runtime(case.manager):
        case.manager.update_stopped(case.release('two'), PASSWORD, case.nas)
    assert case.manager.state['migration_report'] == before['migration_report']
    assert case.manager.state['migration_target'] == before['migration_target']


@pytest.mark.parametrize('fault', ['running', 'suspended', 'denied', 'identity'])
def test_process_presence_or_unverifiable_identity_never_means_stopped(case, fault):
    import psutil
    before = case.manager.state
    result = (object(), {'nonce': 'synthetic'}) if fault in ('running', 'suspended') else None
    error = psutil.AccessDenied(100) if fault == 'denied' else ValueError('identity mismatch') if fault == 'identity' else None
    with forbidden_runtime(case.manager), patch.object(case.manager, '_process',
            return_value=result, side_effect=error), patch.object(case.manager, 'stage_release',
            side_effect=AssertionError('must reject before staging')):
        with pytest.raises((ValueError, psutil.AccessDenied)):
            case.manager.update_stopped(case.package, PASSWORD, case.nas)
    assert_preserved(case, before)


@pytest.mark.parametrize('name', ['state.json.pending', 'state-' + 'a' * 32 + '.pending.json',
                                 'shared/data/carton_erp.sqlite3-wal',
                                 'shared/data/carton_erp.sqlite3-shm', 'shared/data/carton_erp.sqlite3-journal'])
def test_pending_state_and_database_sidecars_are_preserved_and_rejected(case, name):
    path = case.manager.root / name
    path.write_bytes(b'synthetic evidence')
    with forbidden_runtime(case.manager), patch.object(case.manager, 'stage_release',
            side_effect=AssertionError('must reject before staging')):
        with pytest.raises(ValueError):
            case.manager.update_stopped(case.package, PASSWORD, case.nas)
    assert path.read_bytes() == b'synthetic evidence'


def test_dangling_sidecar_link_rejected(case):
    sidecar = case.manager.root / 'shared/data/carton_erp.sqlite3-wal'
    try:
        sidecar.symlink_to(case.root / 'absent')
    except OSError as error:
        pytest.skip('symlinks unavailable: ' + type(error).__name__)
    with forbidden_runtime(case.manager), pytest.raises(ValueError):
        case.manager.update_stopped(case.package, PASSWORD, case.nas)
    assert sidecar.is_symlink()


@pytest.mark.parametrize('change', ['revision', 'extra_table', 'extra_column', 'missing_column'])
def test_same_revision_is_not_a_substitute_for_identical_signed_table_column_contract(case, change):
    if change == 'revision':
        candidate = case.release('changed', revision='r2')
    elif change == 'extra_table':
        candidate = case.release('changed', extra_tables=('extra',))
    else:
        def model(raw):
            return (raw.replace('    note: Mapped[str]', '    note: Mapped[str]\n    extra: Mapped[str]')
                    if change == 'extra_column' else raw.replace('    note: Mapped[str]', ''))
        candidate = program(case, 'changed', change_model=model)
    before = case.manager.state
    with forbidden_runtime(case.manager), patch.object(case.manager, 'stage_release',
            side_effect=AssertionError('incompatible schema must reject before staging')):
        with pytest.raises(ValueError, match='相同修订及原签名表列合同'):
            case.manager.update_stopped(candidate, PASSWORD, case.nas)
    assert_preserved(case, before)


def test_missing_actual_required_column_rejected_with_matching_signed_contract(case):
    with closing(sqlite3.connect(case.manager.root / 'shared/data/carton_erp.sqlite3')) as db:
        db.execute('ALTER TABLE users DROP COLUMN note')
        db.commit()
    with forbidden_runtime(case.manager), pytest.raises(ValueError):
        case.manager.update_stopped(case.release('two'), PASSWORD, case.nas)
    assert not list(case.nas.glob('*.tmbackup'))


def test_unsigned_publisher_and_tampered_current_package_are_rejected(case):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.exceptions import InvalidSignature
    candidate = case.release('two')
    bad = case.root / 'untrusted.zip'
    pack_tree(case.root / 'source-two', bad,
              {'type': 'tianming.release.v1', 'version': 'two', 'revision': 'r1'}, Ed25519PrivateKey.generate())
    with forbidden_runtime(case.manager), pytest.raises(InvalidSignature):
        case.manager.update_stopped(bad, PASSWORD, case.nas)
    source = case.manager.root / 'packages' / (case.manager.state['current'] + '.zip')
    source.write_bytes(b'broken signed source')
    with forbidden_runtime(case.manager), pytest.raises(ValueError, match='当前程序原包'):
        case.manager.update_stopped(candidate, PASSWORD, case.nas)


@pytest.mark.parametrize('role', ['current_manifest', 'candidate_manifest', 'candidate_code'])
def test_tampered_cached_program_rejected_before_backup(case, role):
    candidate = case.release('two')
    value = case.manager.stage_release(candidate)
    identity = case.manager.state['current'] if role == 'current_manifest' else value['id']
    release = case.manager.root / 'releases' / identity
    if role == 'candidate_code':
        (release / 'main.py').write_bytes(b'changed cached code')
    else:
        manifest = read_json(release / 'manifest.json')
        manifest['version'] = 'changed cache'
        write_json(release / 'manifest.json', manifest)
    with forbidden_runtime(case.manager), patch.object(case.manager, '_backup_stopped',
            side_effect=AssertionError('tamper must reject before backup')):
        with pytest.raises(ValueError):
            case.manager.update_stopped(candidate, PASSWORD, case.nas)


def test_root_approval_failure_before_backup_and_after_backup_never_starts(case):
    candidate = case.release('two')
    before = case.manager.state
    for after_backup in (False, True):
        with forbidden_runtime(case.manager), patch.object(case.manager, '_approve_execution',
                side_effect=[None, ValueError('root approval changed')] if after_backup
                else ValueError('not root-approved')) as approval:
            with pytest.raises(ValueError, match='root'):
                case.manager.update_stopped(candidate, PASSWORD, case.nas)
        assert case.manager.state['current'] == before['current']
        assert case.manager.state['manual_stop'] is True
        assert approval.call_count == (2 if after_backup else 1)
        assert bool(list(case.nas.glob('*.tmbackup'))) is after_backup


@pytest.mark.parametrize('reader', ['order_inventory_v1', 'delivery_dispatch_v1'])
def test_first_reader_activation_is_rejected_without_creating_shared_marker(case, reader):
    candidate = program(case, 'reader', capabilities={reader: 1})
    before = case.manager.state
    with forbidden_runtime(case.manager), patch.object(case.manager, '_backup_stopped',
            side_effect=AssertionError('first activation must reject before backup')):
        with pytest.raises(ValueError):
            case.manager.update_stopped(candidate, PASSWORD, case.nas)
    assert_preserved(case, before)
    assert not (case.manager.root / 'shared/data/delivery-dispatch-contract.json').exists()


def install_readers_and_history(case):
    from desktop_assistant import delivery_dispatch_contract as dispatch, restore_provenance as history
    caps = {'order_inventory_v1': 1, 'delivery_dispatch_v1': 1, history.CAPABILITY: 1}
    original = case.manager.state['current']
    current = program(case, 'current-reader', capabilities=caps)
    current_id = case.manager.stage_release(current)['id']
    marker = {'reader_capability': dispatch.CAPABILITY, 'version': 1,
              'activated_package': current_id, 'pre_activation_package': original,
              'pre_activation_backup': 'synthetic-original.tmbackup', 'activated_at': '2026-10-10T10:00:00+08:00'}
    write_json(case.manager.root / 'shared' / dispatch.MARKER, marker)
    inventory = {'reader_capability': 'order_inventory_v1', 'version': 1,
                 'activated_package': current_id, 'activated_at': marker['activated_at'],
                 'next_release_must_preserve_reader_contract': True}
    replace_state(case, current=current_id, restored_release_sha256=original,
                  restored_schema_authority_sha256=None, restored_backup_sha256='b' * 64,
                  restored_source_time='2026-10-10T20:12:10.643701+08:00',
                  native_restore_binding={'type': 'tianming.mac-native-restore.v1',
                      'package_sha256': original, 'backup_sha256': 'b' * 64,
                      'context_sha256': 'c' * 64, 'database_revision': 'r1',
                      'schema_migrated': False, 'factory_cutover': False},
                  order_inventory_activation=inventory,
                  **{dispatch.STATE_KEY: dispatch.state_index(dispatch.activation(case.manager.root / 'shared'))})
    return caps


def test_existing_readers_original_restore_binding_and_full_backup_history_preserved(case):
    from desktop_assistant.migration import files
    caps = install_readers_and_history(case)
    candidate = program(case, 'updated-reader', capabilities=caps)
    before = case.manager.state
    original_files = files(case.manager.root / 'shared')
    with forbidden_runtime(case.manager):
        case.manager.update_stopped(candidate, PASSWORD, case.nas)
    after = case.manager.state
    for key in ('native_restore_binding', 'restored_release_sha256', 'restored_schema_authority_sha256',
                'restored_backup_sha256', 'restored_at', 'restored_source_time',
                'order_inventory_activation', 'delivery_dispatch_activation'):
        assert after[key] == before[key]
    assert files(case.manager.root / 'shared') == original_files
    backup = Path(after['last_backup'])
    raw = case.root / 'backup.zip'
    decrypt_file(backup, raw, PASSWORD)
    payload = case.root / 'backup-payload'
    metadata = extract_verified(raw, payload)
    assert metadata['release'] == before['current']
    assert metadata['restored_release_sha256'] == before['restored_release_sha256']
    assert sha(payload / 'restored-release.zip') == before['restored_release_sha256']
    assert sha(payload / 'shared/data/carton_erp.sqlite3') == sha(case.manager.root / 'shared/data/carton_erp.sqlite3')
    assert (payload / 'credentials.tmencrypted').is_file()


@pytest.mark.parametrize('removed', ['order_inventory_v1', 'delivery_dispatch_v1', 'restore_provenance_v1'])
def test_existing_signed_reader_contract_cannot_be_lost(case, removed):
    caps = install_readers_and_history(case)
    caps.pop(removed)
    candidate = program(case, 'incomplete-reader', capabilities=caps)
    before = case.manager.state
    with forbidden_runtime(case.manager), patch.object(case.manager, '_backup_stopped',
            side_effect=AssertionError('reader rejection precedes backup')):
        with pytest.raises(ValueError):
            case.manager.update_stopped(candidate, PASSWORD, case.nas)
    assert_preserved(case, before)


@pytest.mark.parametrize('fault', ['preflight', 'backup', 'nas_copy', 'receipt'])
def test_failed_backup_has_no_restart_or_pointer_fallback(case, fault):
    from desktop_assistant import mac_backup_storage as storage
    candidate = case.release('two')
    before = case.manager.state
    with forbidden_runtime(case.manager), ExitStack() as stack:
        if fault in ('preflight', 'backup'):
            stack.enter_context(patch.object(case.manager, '_backup_preflight' if fault == 'preflight'
                                else '_backup_stopped', side_effect=ValueError('synthetic ' + fault)))
        else:
            def backup(*args, **kwargs):
                # Install fault inside the synthetic storage adapter used by the
                # fixture; the original full backup algorithm still executes.
                from tests.desktop_assistant.synthetic_storage import approved_local_storage
                with approved_local_storage(case.root):
                    if fault == 'receipt':
                        with patch.object(storage, 'publish_receipt', side_effect=ValueError('synthetic receipt')):
                            return module.Manager._backup_stopped(case.manager, *args, **kwargs)
                    real_copy = module.shutil.copyfileobj
                    def damaged(src, dst, *a):
                        real_copy(src, dst, *a)
                        dst.write(b'corrupted copy')
                    with patch.object(module.shutil, 'copyfileobj', damaged):
                        return module.Manager._backup_stopped(case.manager, *args, **kwargs)
            stack.enter_context(patch.object(case.manager, '_backup_stopped', backup))
        with pytest.raises(ValueError):
            case.manager.update_stopped(candidate, PASSWORD, case.nas)
    assert case.manager.state['current'] == before['current']
    assert case.manager.state['manual_stop'] is True
    if fault in ('nas_copy', 'receipt'):
        assert list((case.nas / 'tianming-backup-work').iterdir())  # Failure evidence retained.


@pytest.mark.parametrize('mutation', ['raw_database', 'attachment', 'environment', 'wal',
    'state_history', 'manual_stop', 'process', 'input_package', 'cache_package',
    'cache_manifest', 'cache_code'])
def test_late_changes_after_real_backup_prevent_pointer_commit_and_preserve_evidence(case, mutation):
    manager = case.manager
    candidate = case.release('two')
    identity = sha(candidate)
    before = manager.state
    original = manager._backup_stopped
    def backup(*args, **kwargs):
        result = original(*args, **kwargs)
        if mutation == 'raw_database':
            with closing(sqlite3.connect(manager.root / 'shared/data/carton_erp.sqlite3')) as db:
                db.execute("UPDATE users SET note='changed'")
                db.commit()
        elif mutation in ('attachment', 'environment', 'wal'):
            path = {'attachment': 'shared/data/drawing.pdf', 'environment': 'shared/environment.json',
                    'wal': 'shared/data/carton_erp.sqlite3-wal'}[mutation]
            (manager.root / path).write_bytes(b'preserved late evidence')
        elif mutation == 'state_history':
            replace_state(case, restored_at='different history')
        elif mutation == 'manual_stop':
            replace_state(case, manual_stop=False)
        elif mutation == 'process':
            manager._process = lambda: (object(), {'nonce': 'synthetic'})
        elif mutation == 'input_package':
            candidate.write_bytes(b'changed input')
        elif mutation == 'cache_package':
            (manager.root / 'packages' / (identity + '.zip')).write_bytes(b'changed cache')
        elif mutation == 'cache_code':
            (manager.root / 'releases' / identity / 'main.py').write_bytes(b'changed code')
        else:
            path = manager.root / 'releases' / identity / 'manifest.json'
            manifest = read_json(path)
            manifest['version'] = 'changed version'
            write_json(path, manifest)
        return result
    with forbidden_runtime(manager), patch.object(manager, '_backup_stopped', backup):
        with pytest.raises(ValueError):
            manager.update_stopped(candidate, PASSWORD, case.nas)
    assert manager.state['current'] == before['current']
    # A foreign state mutation is preserved, never overwritten by rollback.
    assert manager.state['manual_stop'] is (mutation != 'manual_stop')
    assert Path(manager.state['last_backup']).is_file()


def test_noop_still_checks_approval_and_idle_without_backup_or_retention(case):
    before = case.manager.state
    with forbidden_runtime(case.manager), patch.object(case.manager, '_approve_execution',
            wraps=case.manager._approve_execution) as approval, patch.object(case.manager, '_backup_stopped',
            side_effect=AssertionError('no-op needs no new backup')):
        assert case.manager.update_stopped(case.package, PASSWORD, case.nas) == '已经是该版本'
    assert approval.call_count == 1
    assert_preserved(case, before)


def test_state_publish_failure_preserves_old_pointer_and_pending_evidence(case):
    candidate = case.release('two')
    before = case.manager.state
    original = module.write_json
    def failed_commit(path, state):
        if path == case.manager.root / 'state.json' and state.get('current') == sha(candidate):
            (case.manager.root / 'state.json.pending').write_bytes(b'preserved failed write')
            raise OSError('synthetic publication failure')
        original(path, state)
    with forbidden_runtime(case.manager), patch.object(module, 'write_json', failed_commit), pytest.raises(OSError):
        case.manager.update_stopped(candidate, PASSWORD, case.nas)
    assert case.manager.state['current'] == before['current']
    assert case.manager.state['manual_stop'] is True
    assert (case.manager.root / 'state.json.pending').read_bytes() == b'preserved failed write'
    assert Path(case.manager.state['last_backup']).is_file()


def test_maintenance_lock_covers_staging_backup_and_state_commit(case):
    from desktop_assistant.operation_lock import operation_lock, BUSY
    seen = []
    lock = case.manager.root / 'control/operation.lock'
    def locked(phase):
        with pytest.raises(ValueError, match=BUSY), operation_lock(lock, existing_only=True):
            raise AssertionError('second maintenance operation acquired the lock')
        seen.append(phase)
    original_stage = case.manager.stage_release
    original_backup = case.manager._backup_stopped
    original_write = module.write_json
    candidate = case.release('two')
    def stage(*args, **kwargs):
        locked('stage')
        return original_stage(*args, **kwargs)
    def backup(*args, **kwargs):
        locked('backup')
        result = original_backup(*args, **kwargs)
        locked('backup_returned')
        return result
    def write(path, value):
        if path == case.manager.root / 'state.json' and value.get('current') == sha(candidate):
            locked('commit')
        return original_write(path, value)
    with forbidden_runtime(case.manager), patch.object(case.manager, 'stage_release', stage), \
            patch.object(case.manager, '_backup_stopped', backup), patch.object(module, 'write_json', write):
        case.manager.update_stopped(candidate, PASSWORD, case.nas)
    assert seen == ['stage', 'backup', 'backup_returned', 'commit']
    with operation_lock(lock, existing_only=True):
        pass  # No retained maintenance lock after success.


@pytest.mark.parametrize('fault', ['missing_backup_state', 'unverified_receipt', 'work_retained', 'foreign_stream_index'])
def test_unconfirmed_backup_metadata_cannot_commit(case, fault):
    from desktop_assistant import mac_backup_storage as storage
    before = case.manager.state
    real_backup = case.manager._backup_stopped
    def backup(*args, **kwargs):
        if fault == 'missing_backup_state':
            return case.root / 'never-published.tmbackup'
        result = real_backup(*args, **kwargs)
        if fault == 'unverified_receipt':
            # Deliberately replace the adapter only after real backup completed.
            storage.verified_at = lambda *a, **kw: None
        elif fault == 'work_retained':
            replace_state(case, backup_work_retained=['synthetic-preserved-job'])
        else:
            replace_state(case, last_backup_stream={'foreign': 'unapproved metadata change'})
        return result
    with forbidden_runtime(case.manager), patch.object(case.manager, '_backup_stopped', backup):
        with pytest.raises(ValueError):
            case.manager.update_stopped(case.release('two'), PASSWORD, case.nas)
    assert case.manager.state['current'] == before['current']
    assert case.manager.state['manual_stop'] is True


def test_stream_dispatch_preserves_only_original_stream_receipt_state_changes(case):
    """Transport/OS adapter is simulated; the legacy full archive stays real."""
    from desktop_assistant import mac_smb_stream_target as stream
    manager = case.manager
    destination = 'synthetic-smb-stream-target'
    real_backup = manager._backup_stopped
    calls = []
    def stream_archive(actual, password, path):
        assert actual is manager and path == destination
        calls.append('stream_archive')
        result = real_backup(password, case.nas)
        receipt = manager.root / 'control/backup-receipts' / (result.stem + '.json')
        replace_state(case, last_backup_stream={'path': str(result), 'receipt': receipt.name,
                                                'receipt_sha256': sha(receipt)})
        return result
    def preflight(path):
        assert path == destination
        calls.append('stream_preflight')
    with forbidden_runtime(manager), patch.object(stream, 'selected', lambda p: p == destination), \
            patch.object(stream, 'archive_stopped', stream_archive), \
            patch.object(manager, '_backup_preflight', preflight):
        manager.update_stopped(case.release('two'), PASSWORD, destination)
    assert calls == ['stream_preflight', 'stream_archive']
    assert manager.state['last_backup_stream']['path'] == manager.state['last_backup']
    assert manager.state['manual_stop'] is True
    assert Path(manager.state['last_backup']).is_file()


@pytest.mark.skipif(sys.platform != 'darwin', reason='Mac published tree protocol; root/UID metadata simulated')
@pytest.mark.parametrize('fault', [None, 'initial_registry', 'late_registry', 'late_root_code'])
def test_actual_published_protocol_is_rechecked_with_synthetic_root_metadata(published_case, fault):
    from desktop_assistant import mac_published_runtime as published
    c = published_case
    manager = c.manager
    candidate = c.publish('native-updated')
    replace_state(c, manual_stop=True, operation='restore')
    before = manager.state
    registry = read_json(published.REGISTRY)
    def remove_approval():
        modified = deepcopy(registry)
        modified['packages'].remove(candidate.id)
        write_json(published.REGISTRY, modified)
    if fault == 'initial_registry':
        remove_approval()
    real_backup = manager._backup_stopped
    def backup(*args, **kwargs):
        result = real_backup(*args, **kwargs)
        if fault == 'late_registry':
            remove_approval()
        elif fault == 'late_root_code':
            (candidate.slot / 'server/main.py').write_bytes(b'changed root-protected tree (synthetic)')
        return result
    with forbidden_runtime(manager), patch.object(manager, '_backup_stopped', backup):
        if fault:
            with pytest.raises(ValueError):
                manager.update_stopped(candidate.package, PASSWORD, c.nas)
        else:
            assert manager.update_stopped(candidate.package, PASSWORD, c.nas) == 'native-updated'
    assert manager.state['current'] == (before['current'] if fault else candidate.id)
    assert manager.state['manual_stop'] is True
    assert bool(list(c.nas.glob('*.tmbackup'))) is (fault != 'initial_registry')
