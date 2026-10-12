"""Real synthetic archive/crypto/SQLite; Windows OS adapters are explicit fakes."""
import importlib.util
from contextlib import contextmanager, closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import zipfile

import pytest

from desktop_assistant import retained_source_audit as audit
from desktop_assistant import retained_snapshot_contract as contract
from desktop_assistant import factory_source_audit as original
from desktop_assistant import credential_transfer as credentials, credential_restore, credential_store
from desktop_assistant import restore_provenance as history
from desktop_assistant.operation_lock import operation_lock
from desktop_assistant.storage import (sha, read_json, write_json, pack_recovery, pack_tree,
                                       decrypt_file, extract_verified, encrypt_file, database_info)
from tests.desktop_assistant.test_factory_source_audit import case as original_case, hashes, PASSWORD

TRANSFER_PASSWORD = 'independent-transfer-only-password'
OLD_DATE = '2020-01-01T08:12:13.456789+08:00'
UTC_OLD_DATE = '2020-01-01T00:12:13Z'
NATIVE_WINDOWS_CHECK = original._require_windows


@pytest.fixture
def case(original_case, monkeypatch):
    case = original_case
    work = case.fixture.root / 'historical-fixture'; work.mkdir()
    decrypt_file(case.backup, work / 'original.zip', PASSWORD)
    payload = work / 'payload'
    metadata = extract_verified(work / 'original.zip', payload)
    case.record = credentials.unseal((payload / 'credentials.tmencrypted').read_bytes(), PASSWORD)
    metadata.pop('credential_transfer')
    metadata.pop('files')
    metadata['created'] = OLD_DATE
    pack_recovery(payload / 'shared', {'release.zip': payload / 'release.zip'}, work / 'historical.zip', metadata)
    encrypt_file(work / 'historical.zip', work / 'historical.tmbackup', PASSWORD)
    shutil.copyfile(work / 'historical.tmbackup', case.backup)
    case.expected = sha(case.backup)
    case.record['bound_backup_sha256'] = case.expected
    case.credentials_path = case.fixture.root / 'original-external.tmencrypted'
    case.credentials_path.write_bytes(credentials.seal(case.record, TRANSFER_PASSWORD))
    case.receipt = case.root / 'control/backup-receipts' / (case.backup.stem + '.json')
    receipt = read_json(case.receipt)
    receipt.update(size=case.backup.stat().st_size, sha256=case.expected)
    write_json(case.receipt, receipt)
    # A later last_backup must not replace the explicitly approved history.
    state = read_json(case.root / 'state.json')
    state['last_backup'] = str(case.backup.parent / 'later-unrelated.tmbackup')
    write_json(case.root / 'state.json', state)
    case.request.update(type=contract.REQUEST, retained_snapshot={
        'backup_sha256': case.expected, 'backup_created_at': UTC_OLD_DATE,
        'credentials_sha256': sha(case.credentials_path), 'restore_package_sha256': '1' * 64,
        'restore_receipt_sha256': '2' * 64, 'preparation_sha256': '3' * 64})
    case.active_leases = set()

    @contextmanager
    def lease(path):
        with original.read_file_lease(path):
            case.active_leases.add(path)
            try:
                yield
            finally:
                case.active_leases.remove(path)

    monkeypatch.setattr(audit, 'read_file_lease', lease)
    monkeypatch.setattr(audit, 'private_workspace', original.private_workspace)
    monkeypatch.setattr(audit, 'archive_synced', lambda path: True)
    def forbidden(*args, **kwargs):
        raise AssertionError('audit attempted credential export/restore/protect or signing')
    monkeypatch.setattr(credentials, 'export', forbidden)
    monkeypatch.setattr(credential_restore, 'prepare', forbidden)
    monkeypatch.setattr(credential_store, 'protect', forbidden)
    from desktop_assistant import signing
    monkeypatch.setattr(signing, 'load_dpapi_key', forbidden)
    return case


def run(case, **changes):
    args = dict(root=case.root, backup=case.backup, expected_backup_sha256=case.expected,
        credentials_path=case.credentials_path, transfer_password=TRANSFER_PASSWORD,
        request=case.request, target_package=case.target, public=case.public, private_parent=case.parent)
    args.update(changes)
    return audit.verified_stopped_retained_backup(**args)


def reseal(case):
    case.record['bound_backup_sha256'] = case.expected
    case.credentials_path.write_bytes(credentials.seal(case.record, TRANSFER_PASSWORD))
    case.request['retained_snapshot'].update(backup_sha256=case.expected,
                                            credentials_sha256=sha(case.credentials_path))


def rewrite_backup(case, mutate, *, update_date=False, update_database=False):
    job = case.fixture.root / 'rewritten-historical'; job.mkdir()
    decrypt_file(case.backup, job / 'before.zip', PASSWORD)
    metadata = extract_verified(job / 'before.zip', job / 'payload')
    mutate(metadata, job / 'payload')
    members = {p.relative_to(job / 'payload').as_posix(): p for p in (job / 'payload').rglob('*')
               if p.is_file() and p.name != 'manifest.json' and not p.is_relative_to(job / 'payload/shared')}
    metadata.pop('files')
    pack_recovery(job / 'payload/shared', members, job / 'after.zip', metadata)
    encrypt_file(job / 'after.zip', job / 'after.tmbackup', PASSWORD)
    shutil.copyfile(job / 'after.tmbackup', case.backup)
    case.expected = sha(case.backup)
    receipt = read_json(case.receipt)
    receipt.update(sha256=case.expected, size=case.backup.stat().st_size)
    if update_database:
        receipt['database'] = metadata['database']
    write_json(case.receipt, receipt)
    reseal(case)
    if update_date:
        case.request['retained_snapshot']['backup_created_at'] = contract._stamp(datetime.fromisoformat(metadata['created']))


def test_real_historical_backup_and_external_credentials_are_verified_under_live_leases(case):
    before = hashes(case.root)
    backup_before = case.backup.read_bytes()
    credentials_before = case.credentials_path.read_bytes()
    with run(case) as (evidence, recheck):
        assert case.events[-2:] == ['private-enter', 'private-final-check']
        assert {case.root / 'state.json', case.root / 'shared/data/carton_erp.sqlite3',
                case.credentials_path, case.receipt, case.target} <= case.active_leases
        assert evidence['backup_created_at'] == UTC_OLD_DATE
        assert evidence['backup_created_at'] < case.request['created_at']
        assert evidence['backup_sha256'] == case.expected
        assert evidence['credentials_sha256'] == sha(case.credentials_path)
        assert evidence['credential_purposes'] == ['backup', 'deepseek', 'mailbox']
        assert evidence['current_snapshot'] == evidence['snapshot']
        assert evidence['current_snapshot']['database_sha256'] == sha(case.root / 'shared/data/carton_erp.sqlite3')
        assert evidence['stop_proof']['environment_sha256'] == sha(case.root / 'shared/environment.json')
        assert evidence['stop_proof']['process_record_sha256'] is None
        assert contract.validate_source(evidence) == evidence
        assert recheck() == evidence and recheck() is not evidence
        assert PASSWORD not in json.dumps(evidence) and TRANSFER_PASSWORD not in json.dumps(evidence)
        assert 'synthetic-secret' not in json.dumps(evidence)
        assert hashes(case.root, held_lock=True) == {k: v for k, v in before.items() if k != 'control/operation.lock'}
        with pytest.raises(ValueError, match='另一个'):
            with operation_lock(case.root / 'control/operation.lock'):
                pass
    assert not case.active_leases
    assert hashes(case.root) == before
    assert case.backup.read_bytes() == backup_before and case.credentials_path.read_bytes() == credentials_before
    with pytest.raises(ValueError, match='租约已释放'):
        recheck()
    work = next(case.parent.iterdir())
    assert (work / 'recovery.zip').is_file() and (work / 'payload/shared/data/carton_erp.sqlite3').is_file()
    assert not (work / 'payload/credentials.tmencrypted').exists()
    assert not (work / 'payload/credential-config').exists()


@pytest.mark.parametrize('kind', ['backup-argument', 'backup-request', 'credential-sha', 'backup-date',
    'publisher', 'expired', 'future', 'no-offset', 'relative-transfer', 'private-in-source'])
def test_independent_request_binding_and_clock_are_required(case, kind):
    changes = {}
    if kind == 'backup-argument': changes['expected_backup_sha256'] = 'f' * 64
    elif kind == 'backup-request': case.request['retained_snapshot']['backup_sha256'] = 'f' * 64
    elif kind == 'credential-sha': case.request['retained_snapshot']['credentials_sha256'] = 'f' * 64
    elif kind == 'backup-date': case.request['retained_snapshot']['backup_created_at'] = '2020-01-02T00:00:00Z'
    elif kind == 'publisher': case.request['publisher_sha256'] = 'f' * 64
    elif kind == 'expired': changes['now'] = contract._time(case.request['expires_at'])
    elif kind == 'future': changes['now'] = contract._time(case.request['created_at']) - timedelta(seconds=1)
    elif kind == 'no-offset': changes['now'] = datetime.now()
    elif kind == 'relative-transfer': changes['credentials_path'] = Path('cipher.tmencrypted')
    else: changes['private_parent'] = case.root / 'staging'
    with pytest.raises(ValueError):
        with run(case, **changes):
            pytest.fail('must not yield')


@pytest.mark.parametrize('field,value', [('manual_stop', False), ('manual_stop', 1),
    ('onboarding_pending', True), ('backup_error', 'failed'), ('operation', 'restore_failed')])
def test_pending_or_failed_current_source_remains_rejected(case, field, value):
    state = read_json(case.root / 'state.json'); state[field] = value
    write_json(case.root / 'state.json', state)
    with pytest.raises(ValueError):
        with run(case): pytest.fail('must not yield')
    assert not list(case.parent.iterdir())


@pytest.mark.parametrize('field,value', [('verified', 1), ('storage', 'local'), ('size', True),
    ('size', 0), ('sha256', 'f' * 64), ('path', '/unrelated.tmbackup'), ('database', {})])
def test_historical_original_nas_receipt_is_not_replaced_by_a_cached_sha(case, field, value):
    receipt = read_json(case.receipt); receipt[field] = value; write_json(case.receipt, receipt)
    with pytest.raises((ValueError, OSError)):
        with run(case): pytest.fail('must not yield')


@pytest.mark.parametrize('kind', ['receipt-missing', 'nas-unsynced', 'running', 'busy',
    'missing-lock', 'empty-lock', 'missing-install-directory', 'wrong-password'])
def test_preconditions_never_stop_or_reconstruct_the_source(case, monkeypatch, kind):
    changes = {}
    if kind == 'receipt-missing': case.receipt.unlink()
    elif kind == 'nas-unsynced': monkeypatch.setattr(audit, 'archive_synced', lambda path: False)
    elif kind == 'running': monkeypatch.setattr(original._SourceReader, '_process', lambda manager: object())
    elif kind == 'busy': monkeypatch.setattr(original, 'assert_idle', lambda root: (_ for _ in ()).throw(PermissionError('busy')))
    elif kind == 'missing-lock': (case.root / 'control/operation.lock').unlink()
    elif kind == 'empty-lock': (case.root / 'control/operation.lock').write_bytes(b'')
    elif kind == 'missing-install-directory': (case.root / 'backups').rmdir()
    else: changes['transfer_password'] = 'wrong-hidden-transfer-password'
    before = hashes(case.root)
    with pytest.raises((ValueError, OSError)):
        with run(case, **changes): pytest.fail('must not yield')
    assert hashes(case.root) == before and not list(case.parent.iterdir())


@pytest.mark.parametrize('kind', ['value', 'purpose', 'platform', 'config-text', 'version',
    'protected-sha', 'backup-secret', 'binding'])
def test_complete_external_record_must_match_current_original_dpapi_user(case, kind):
    record = case.record
    if kind == 'value': record['entries']['mailbox']['value'] = 'different-plaintext-do-not-leak'
    elif kind == 'purpose': del record['entries']['mailbox']
    elif kind == 'platform': record['source_platform'] = 'darwin'
    elif kind == 'config-text': record['files']['preferences.json'] += ' '
    elif kind == 'version': record['entries']['mailbox']['source_version'] += 1
    elif kind == 'protected-sha': record['entries']['mailbox']['source_protected_sha256'] = 'f' * 64
    elif kind == 'backup-secret': record['entries']['backup']['value'] = 'different-backup-decryption-password'
    else: record['bound_backup_sha256'] = 'f' * 64
    case.credentials_path.write_bytes(credentials.seal(record, TRANSFER_PASSWORD))
    case.request['retained_snapshot']['credentials_sha256'] = sha(case.credentials_path)
    with pytest.raises(ValueError) as error:
        with run(case): pytest.fail('must not yield')
    assert 'different-' not in str(error.value)


@pytest.mark.parametrize('kind', ['business-row', 'same-facts-raw-db', 'attachment', 'environment',
    'mailbox', 'extra-file', 'wal', 'shm', 'journal'])
def test_source_requires_original_raw_database_all_files_and_environment(case, kind):
    shared = case.root / 'shared'; database = shared / 'data/carton_erp.sqlite3'
    if kind in {'business-row', 'same-facts-raw-db', 'mailbox'}:
        with closing(sqlite3.connect(database)) as db, db:
            db.execute({'business-row': "UPDATE users SET note='new business'",
                'same-facts-raw-db': 'PRAGMA user_version=17',
                'mailbox': "UPDATE email_intake_settings SET encrypted_secret='new storage'"}[kind])
    elif kind == 'attachment': (shared / 'data/drawing.pdf').write_bytes(b'new attachment')
    elif kind == 'environment':
        with (shared / 'environment.json').open('ab') as stream: stream.write(b' ')
    elif kind == 'extra-file': (shared / 'unregistered-evidence.txt').write_text('retained fact')
    else: Path(str(database) + '-' + kind).write_bytes(b'')
    before = hashes(case.root)
    with pytest.raises(ValueError):
        with run(case): pytest.fail('must not yield')
    assert hashes(case.root) == before


@pytest.mark.parametrize('kind', ['embedded', 'credential-config', 'unknown-file', 'bad-date', 'naive-date'])
def test_original_recovery_format_and_unique_external_credential_source_are_enforced(case, kind):
    def change(metadata, payload):
        if kind == 'embedded':
            metadata['credential_transfer'] = 'credentials.tmencrypted'
            record = deepcopy(case.record); record['bound_backup_sha256'] = None
            (payload / 'credentials.tmencrypted').write_bytes(credentials.seal(record, PASSWORD))
        elif kind == 'credential-config':
            (payload / 'credential-config').mkdir()
            (payload / 'credential-config/preferences.json').write_text('{}')
        elif kind == 'unknown-file': (payload / 'unknown').write_text('unexpected')
        elif kind == 'bad-date': metadata['created'] = '2099-01-01T00:00:00+00:00'
        else: metadata['created'] = '2020-01-01T00:12:13'
    rewrite_backup(case, change)
    with pytest.raises(ValueError):
        with run(case): pytest.fail('must not yield')


def test_final_private_acl_check_precedes_any_usable_evidence(case, monkeypatch):
    @contextmanager
    def failed(parent):
        with case.workspace(parent) as job: yield job
        raise ValueError('synthetic final DACL refusal')
    monkeypatch.setattr(audit, 'private_workspace', failed)
    with pytest.raises(ValueError, match='DACL'):
        with run(case): pytest.fail('must not yield before final DACL')


@pytest.mark.parametrize('when', ['private-exit', 'caller', 'normal-exit'])
@pytest.mark.parametrize('kind', ['state', 'receipt', 'config', 'source', 'backup', 'credentials', 'target', 'process'])
def test_mutations_around_yield_and_signing_are_never_accepted(case, monkeypatch, when, kind):
    def mutate():
        paths = {'state': case.root / 'state.json', 'receipt': case.receipt,
            'config': case.root / 'preferences.json', 'source': case.root / 'shared/environment.json',
            'backup': case.backup, 'credentials': case.credentials_path, 'target': case.target,
            'process': case.root / 'control/process.json'}
        path = paths[kind]
        if kind == 'process': write_json(path, {'pid': -1})
        else:
            with path.open('ab') as stream: stream.write(b' ')
    if when == 'private-exit':
        @contextmanager
        def changed(parent):
            with case.workspace(parent) as job: yield job
            mutate()
        monkeypatch.setattr(audit, 'private_workspace', changed)
        with pytest.raises((ValueError, OSError, KeyError)):
            with run(case): pytest.fail('must not yield')
    else:
        with pytest.raises((ValueError, OSError, KeyError)):
            with run(case) as (_, recheck):
                mutate()
                if when == 'caller': recheck()
    assert not case.active_leases


def test_caller_cannot_modify_or_reuse_yielded_evidence(case):
    with pytest.raises(ValueError, match='调用者改变'):
        with run(case) as (evidence, recheck):
            evidence['stop_proof']['state_sha256'] = 'f' * 64
            recheck()
    with pytest.raises(ValueError, match='租约已释放'):
        recheck()


def test_source_business_sql_stays_on_authenticated_copy_and_fresh_credentials_are_immutable(case, monkeypatch):
    connect = sqlite3.connect
    source = case.root / 'shared/data/carton_erp.sqlite3'
    observed = []
    def check(path, *args, **kwargs):
        text = str(path)
        if source.as_uri() in text:
            assert text == source.as_uri() + '?mode=ro&immutable=1'
            assert source in case.active_leases
            observed.append(text)
        elif str(source) == text:
            pytest.fail('source database opened without immutable read lease')
        return connect(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', check)
    with run(case) as (_, recheck):
        recheck()
    assert len(observed) >= 4


@pytest.mark.parametrize('when', ['before-yield', 'during-yield'])
def test_request_expiring_during_audit_or_signing_is_rejected(case, monkeypatch, when):
    start = datetime.now(timezone.utc)
    expired = contract._time(case.request['expires_at'])
    ticks = iter([start, expired] if when == 'before-yield' else [start, start, expired])
    monkeypatch.setattr(audit, '_now', lambda override: next(ticks))
    with pytest.raises(ValueError, match='过期'):
        with run(case) as (_, recheck):
            if when == 'before-yield': pytest.fail('must not yield expired audit')
            recheck()


def test_v1_still_rejects_our_historical_backup_and_external_only_format(case):
    legacy = {k: v for k, v in case.request.items() if k != 'retained_snapshot'}
    legacy['type'] = 'tianming.factory-target.v1'
    state = read_json(case.root / 'state.json'); state['last_backup'] = str(case.backup)
    write_json(case.root / 'state.json', state)
    with pytest.raises(ValueError):
        with original.verified_stopped_backup(case.root, case.backup, case.expected, PASSWORD,
                legacy, case.target, case.public, case.parent):
            pytest.fail('v1 must remain unchanged')


@pytest.mark.parametrize('bad_policy', [False, True])
def test_original_signed_schema_authority_gate_survives_historical_mode(case, bad_policy):
    current = read_json(case.root / 'state.json')['current']
    case.fixture.release('retained-authority', revision='r2')
    authority = case.fixture.root / 'retained-authority.zip'
    # release() already created this filename, so retain it and use a new one.
    authority = authority.with_name('retained-authority-policy.zip')
    pack_tree(case.fixture.root / 'source-retained-authority', authority,
        {'type': 'tianming.release.v1', 'version': 'authority', 'revision': 'r2',
         'migration': {'policy': 'wrong' if bad_policy else 'preserve_existing_facts_v1',
                       'rollback_package_sha256': current}}, case.fixture.key)
    identity = sha(authority)
    shutil.copyfile(authority, case.root / 'packages' / (identity + '.zip'))
    state = read_json(case.root / 'state.json'); state['schema_authority'] = identity
    write_json(case.root / 'state.json', state)
    database = case.root / 'shared/data/carton_erp.sqlite3'
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE alembic_version SET version_num='r2'")
    info = database_info(database)
    target = case.fixture.root / 'retained-target-r2.zip'
    pack_tree(case.fixture.root / 'mac-target', target,
        {'type': 'tianming.release.v1', 'version': 'synthetic-mac', 'revision': 'r2',
         'runtime_platform': 'macos-arm64', 'reader_capabilities': {'factory_cutover_v1': 1}}, case.fixture.key)
    case.target = target; case.request['package_sha256'] = sha(target)
    def changed(metadata, payload):
        metadata.update(database=info, schema_authority=identity)
        shutil.copyfile(database, payload / 'shared/data/carton_erp.sqlite3')
        shutil.copyfile(authority, payload / 'compatibility.zip')
    rewrite_backup(case, changed, update_database=True)
    if bad_policy:
        with pytest.raises(ValueError):
            with run(case): pytest.fail('must not yield')
    else:
        with run(case) as (evidence, recheck):
            assert evidence['snapshot']['revision'] == 'r2'
            assert recheck() == evidence


@pytest.mark.parametrize('kind', ['reader-missing', 'reader-boolean', 'platform', 'entry', 'schema', 'tamper'])
def test_target_keeps_complete_signature_reader_platform_and_schema_gates(case, kind):
    metadata = {'type': 'tianming.release.v1', 'version': 'reviewed-target', 'revision': 'r1',
        'runtime_platform': 'macos-arm64', 'reader_capabilities': {'factory_cutover_v1': 1}}
    source = case.fixture.root / 'mac-target'
    if kind == 'reader-missing': metadata['reader_capabilities'] = {}
    elif kind == 'reader-boolean': metadata['reader_capabilities']['factory_cutover_v1'] = True
    elif kind == 'platform': metadata['runtime_platform'] = 'windows'
    elif kind == 'entry': (source / 'main.py').unlink()
    elif kind == 'schema': metadata['revision'] = 'r2'
    target = case.fixture.root / 'rejected-target.zip'
    pack_tree(source, target, metadata, case.fixture.key)
    if kind == 'tamper':
        corrupted = target.with_name('bad-target.zip')
        with zipfile.ZipFile(target) as old, zipfile.ZipFile(corrupted, 'w') as new:
            for name in old.namelist():
                new.writestr(name, b'changed unsigned bytes' if name == 'main.py' else old.read(name))
        target = corrupted
    case.target = target; case.request['package_sha256'] = sha(target)
    with pytest.raises(ValueError):
        with run(case): pytest.fail('must not yield')


@pytest.mark.parametrize('when', ['private-exit', 'during-yield'])
def test_independent_archive_status_and_fresh_dpapi_are_rechecked(case, monkeypatch, when):
    calls = 0
    real_collect = credentials.collect
    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        record = real_collect(*args, **kwargs)
        if calls == (2 if when == 'private-exit' else 3):
            record['entries']['mailbox']['value'] = 'changed-original-user-secret'
        return record
    monkeypatch.setattr(credentials, 'collect', changed)
    with pytest.raises(ValueError):
        with run(case) as (_, recheck):
            if when == 'private-exit': pytest.fail('must not yield changed credentials')
            recheck()


@pytest.mark.parametrize('when', ['private-exit', 'during-yield'])
def test_remote_upload_status_never_inferred_from_matching_ciphertext(case, monkeypatch, when):
    states = iter([True, False] if when == 'private-exit' else [True, True, False])
    monkeypatch.setattr(audit, 'archive_synced', lambda path: next(states))
    with pytest.raises(ValueError, match='上传状态'):
        with run(case) as (_, recheck):
            if when == 'private-exit': pytest.fail('must not yield pending archive')
            recheck()


def test_native_platform_gate_is_not_implicitly_bypassed(case, monkeypatch):
    if sys.platform == 'win32':
        pytest.skip('Native platform denial is meaningful only off Windows')
    monkeypatch.setattr(original, '_require_windows', NATIVE_WINDOWS_CHECK)
    with pytest.raises(ValueError, match='原Windows'):
        with run(case): pytest.fail('must not yield on another OS')
    assert not list(case.parent.iterdir())


@pytest.mark.parametrize('wrong_original', [False, True])
def test_original_restore_provenance_and_signed_original_package_are_retained(case, wrong_original):
    old_state = read_json(case.root / 'state.json')
    old_identity = old_state['current']
    old_package = case.root / 'packages' / (old_identity + '.zip')
    source_package = case.fixture.root / 'history-aware-source.zip'
    pack_tree(case.fixture.root / 'source-one', source_package,
        {'type': 'tianming.release.v1', 'version': 'history-aware', 'revision': 'r1',
         'reader_capabilities': {history.CAPABILITY: 1}}, case.fixture.key)
    source_id = sha(source_package)
    shutil.copyfile(source_package, case.root / 'packages' / (source_id + '.zip'))
    manifest = audit.inspect_package(source_package, case.public)[1]
    cache = case.root / 'releases' / source_id; cache.mkdir()
    write_json(cache / 'manifest.json', manifest)
    provenance = {history.RELEASE: old_identity, history.AUTHORITY: None}
    write_json(case.root / 'state.json', {**old_state, 'current': source_id, **provenance})
    target = case.fixture.root / 'history-aware-target.zip'
    pack_tree(case.fixture.root / 'mac-target', target,
        {'type': 'tianming.release.v1', 'version': 'history-aware-target', 'revision': 'r1',
         'runtime_platform': 'macos-arm64',
         'reader_capabilities': {'factory_cutover_v1': 1, history.CAPABILITY: 1}}, case.fixture.key)
    case.target = target; case.request['package_sha256'] = sha(target)
    def changed(metadata, payload):
        metadata.update(release=source_id, version='history-aware', **provenance)
        shutil.copyfile(source_package, payload / 'release.zip')
        shutil.copyfile(source_package if wrong_original else old_package,
                        payload / history.MEMBERS[history.RELEASE])
    rewrite_backup(case, changed)
    if wrong_original:
        with pytest.raises(ValueError, match='原包摘要'):
            with run(case): pytest.fail('must not yield wrong original program')
    else:
        with run(case) as (evidence, recheck):
            assert evidence['source_package_sha256'] == source_id
            assert old_package in case.active_leases
            assert recheck() == evidence


def test_retained_source_audit_exists_as_independent_entry():
    assert importlib.util.find_spec('desktop_assistant.retained_source_audit') is not None
