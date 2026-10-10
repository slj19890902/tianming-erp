import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from desktop_assistant import credential_restore as restore, credential_store, credential_transfer
from desktop_assistant.storage import sha, write_json
from tests.desktop_assistant.test_credential_transfer import source, PASSWORD
from tests.desktop_assistant import test_recovery as recovery_fixtures


@pytest.fixture
def host_store(monkeypatch):
    values = {}
    def protect(value, purpose):
        reference = f'new-local-{purpose}-{len(values)}'
        values[reference] = value
        return reference
    monkeypatch.setattr(credential_store, 'protect', protect)
    monkeypatch.setattr(credential_store, 'unprotect', lambda reference, purpose: values[reference])
    return values


def payload_for(source, tmp_path):
    payload = tmp_path / 'payload'
    shutil.copytree(source / 'shared', payload / 'shared')
    return payload, payload / 'shared/data/carton_erp.sqlite3'


def test_rewrap_changes_only_secret_and_preserves_config_and_mail_state(source, tmp_path, host_store):
    record = credential_transfer.collect(source)
    payload, database = payload_for(source, tmp_path)
    original = sha(source / 'shared/data/carton_erp.sqlite3')
    paths, report = restore.prepare(record, payload)
    assert report['restored'] == ['backup', 'deepseek', 'mailbox', 'openai']
    with sqlite3.connect(database) as db:
        row = db.execute('SELECT encrypted_secret,version,uid FROM email_intake_settings').fetchone()
    assert row[0].startswith('new-local-mailbox-') and row[1:] == (7, 'unchanged-source')
    preferences = json.loads(paths['preferences.json'].read_text())
    assert preferences['retained'] == 42 and preferences['nas'] == r'Z:\ERP-backups'
    assert host_store[preferences['protected_password']] == 'synthetic-secret-backup'
    assert 'synthetic-secret' not in ''.join(p.read_text() for p in paths.values())
    assert sha(source / 'shared/data/carton_erp.sqlite3') == original
    with pytest.raises(ValueError, match='重复'):
        restore.prepare(record, payload)


@pytest.mark.parametrize('change', ['version', 'cipher', 'absent', 'wal'])
def test_mismatch_refused_before_host_storage(source, tmp_path, host_store, change):
    record = credential_transfer.collect(source)
    payload, database = payload_for(source, tmp_path)
    if change == 'wal':
        Path(str(database)+'-wal').write_bytes(b'synthetic-active-marker')
    else:
        with sqlite3.connect(database) as db:
            db.execute({'version':'UPDATE email_intake_settings SET version=8',
                        'cipher':"UPDATE email_intake_settings SET encrypted_secret='other'",
                        'absent':'DELETE FROM email_intake_settings'}[change])
    before = sha(database)
    with pytest.raises(ValueError):
        restore.prepare(record, payload)
    assert host_store == {} and sha(database) == before
    assert not (payload / 'credential-config').exists()


def test_host_readback_failure_preserves_database(source, tmp_path, host_store, monkeypatch):
    record = credential_transfer.collect(source)
    payload, database = payload_for(source, tmp_path)
    before = sha(database)
    monkeypatch.setattr(credential_store, 'unprotect', lambda *args: 'wrong-synthetic-value')
    with pytest.raises(ValueError, match='回读'):
        restore.prepare(record, payload)
    assert sha(database) == before
    assert not (payload / 'credential-config').exists()


def test_trigger_business_side_effect_rolls_back(source, tmp_path, host_store):
    record = credential_transfer.collect(source)
    payload, database = payload_for(source, tmp_path)
    with sqlite3.connect(database) as db:
        db.executescript("CREATE TABLE business(id INTEGER); CREATE TRIGGER unexpected AFTER UPDATE ON email_intake_settings BEGIN INSERT INTO business VALUES(1); END;")
    with pytest.raises(ValueError, match='已回滚'):
        restore.prepare(record, payload)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT count(*) FROM business').fetchone()[0] == 0
        assert db.execute('SELECT encrypted_secret FROM email_intake_settings').fetchone()[0] == 'windows-mailbox-cipher'


def test_external_package_requires_exact_backup_binding(source, tmp_path):
    backup = tmp_path / 'original.tmbackup'
    backup.write_bytes(b'synthetic-encrypted-backup-identity')
    record = credential_transfer.collect(source, bound_backup_sha256=sha(backup))
    package = tmp_path / 'credentials.tmencrypted'
    package.write_bytes(credential_transfer.seal(record, PASSWORD))
    assert restore.load_for_recovery({}, tmp_path, backup, 'unused', package, PASSWORD) == record
    backup.write_bytes(b'another-backup')
    with pytest.raises(ValueError, match='SHA256'):
        restore.load_for_recovery({}, tmp_path, backup, 'unused', package, PASSWORD)
    with pytest.raises(ValueError, match='来源不唯一'):
        restore.load_for_recovery({'credential_transfer':'credentials.tmencrypted'}, tmp_path, backup, PASSWORD, package, PASSWORD)


@pytest.fixture
def recovery_case(monkeypatch):
    case = recovery_fixtures.RecoveryTests()
    case.setUp()
    try:
        root = case.manager.root
        write_json(root / 'preferences.json', {'protected_password': 'old-backup-cipher', 'nas': 'original-nas'})
        write_json(root / 'control/ai-provider.json', {'provider':'openai','protected_api_key':'old-ai-cipher'})
        with sqlite3.connect(root / 'shared/data/carton_erp.sqlite3') as db:
            db.executescript("CREATE TABLE email_intake_settings(id INTEGER PRIMARY KEY, encrypted_secret TEXT, version INTEGER, automatic_enabled INTEGER, uid TEXT); INSERT INTO email_intake_settings VALUES(1,'old-mail-cipher',9,1,'read-position-42');")
        monkeypatch.setattr(credential_transfer, 'unprotect', lambda value, purpose: 'synthetic-secret-' + purpose)
        yield case
    finally:
        case.tearDown()


def test_full_encrypted_backup_and_restore_include_host_credentials(recovery_case, host_store):
    case = recovery_case
    database = case.manager.root / 'shared/data/carton_erp.sqlite3'
    before = sha(database)
    backup = case.manager.backup(PASSWORD, case.nas)
    assert b'synthetic-secret' not in backup.read_bytes()
    target = recovery_fixtures.TestManager(case.root / 'restored', case.public)
    result = target.restore(backup, PASSWORD)
    assert result['credentials']['status'] == 'restored' and not result['started']
    assert result['credentials']['restored'] == ['backup', 'mailbox', 'openai']
    with sqlite3.connect(target.root / 'shared/data/carton_erp.sqlite3') as db:
        row = db.execute('SELECT encrypted_secret,version,automatic_enabled,uid FROM email_intake_settings').fetchone()
    assert row[1:] == (9, 1, 'read-position-42')
    assert host_store[row[0]] == 'synthetic-secret-mailbox'
    assert sha(database) == before
    assert (target.root / 'control/ai-provider.json').is_file()
    assert 'synthetic-secret' not in (target.root / 'preferences.json').read_text()


def test_existing_target_config_prevents_activation_and_host_writes(recovery_case, host_store):
    case = recovery_case
    backup = case.manager.backup(PASSWORD, case.nas)
    target = recovery_fixtures.TestManager(case.root / 'existing-config', case.public)
    write_json(target.root / 'preferences.json', {'keep':True})
    before = (target.root / 'preferences.json').read_bytes()
    with pytest.raises(ValueError, match='不能覆盖'):
        target.restore(backup, PASSWORD)
    assert host_store == {} and target.state['current'] is None
    assert not list((target.root / 'shared').iterdir())
    assert (target.root / 'preferences.json').read_bytes() == before


def test_legacy_backup_with_separate_bound_transfer(recovery_case, host_store):
    from desktop_assistant.storage import pack_recovery, database_info, encrypt_file
    case = recovery_case
    root = case.manager.root
    raw, backup = case.root / 'legacy.zip', case.root / 'legacy.tmbackup'
    pack_recovery(root / 'shared', {'release.zip':case.package}, raw,
        {'type':'tianming.recovery.v1','created':'2026-10-10T20:12:10+08:00',
         'release':case.manager.state['current'],'version':'one',
         'database':database_info(root / 'shared/data/carton_erp.sqlite3'),
         'source_shared':str(root / 'shared')})
    encrypt_file(raw, backup, PASSWORD)
    package = case.root / 'external.tmencrypted'
    credential_transfer.export(root, package, 'separate-synthetic-transfer-password', backup=backup)
    target = recovery_fixtures.TestManager(case.root / 'legacy-restored', case.public)
    result = target.restore(backup, PASSWORD, credentials=package,
                            credential_password='separate-synthetic-transfer-password')
    assert result['credentials']['status'] == 'restored' and not result['started']
    assert (target.root / 'control/ai-provider.json').is_file()
    if os.name != 'nt':
        assert target.root.stat().st_mode & 0o777 == 0o700


def test_keychain_failure_keeps_recovery_unactivated(recovery_case, monkeypatch):
    case = recovery_case
    backup = case.manager.backup(PASSWORD, case.nas)
    target = recovery_fixtures.TestManager(case.root / 'keychain-denied', case.public)
    def deny(*args):
        raise ValueError('synthetic-host-locked')
    monkeypatch.setattr(credential_store, 'protect', deny)
    with pytest.raises(ValueError, match='host-locked'):
        target.restore(backup, PASSWORD)
    assert target.state['current'] is None
    assert not list((target.root / 'shared').iterdir())
    assert not (target.root / 'preferences.json').exists()
