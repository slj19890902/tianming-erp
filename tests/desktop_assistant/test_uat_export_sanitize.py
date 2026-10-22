import hashlib
import json
import os
import sqlite3

import pytest

from desktop_assistant import uat_export_sanitize as subject


PASSWORD = '$2b$12$SYNTHETIC_CREDENTIAL_FOR_UAT_EXPORT_TEST_0123456789'
MAIL_SECRET = 'SYNTHETIC_ENCRYPTED_MAIL_CREDENTIAL_0123456789'


def snapshot(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as db:
        db.executescript('''
            PRAGMA foreign_keys=ON;
            PRAGMA user_version=42;
            PRAGMA application_id=1234;
            CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT,
                password_hash TEXT NOT NULL, auth_version INTEGER NOT NULL,
                must_change_password BOOLEAN NOT NULL, role TEXT, updated_at TEXT);
            CREATE TABLE email_intake_settings(id INTEGER PRIMARY KEY,
                encrypted_secret TEXT NOT NULL, automatic_enabled BOOLEAN NOT NULL,
                sender_addresses_json TEXT, last_sync_status TEXT);
            CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id),
                quantity NUMERIC, amount NUMERIC, notes TEXT, drawing BLOB);
            CREATE TABLE audit(id INTEGER PRIMARY KEY, message TEXT);
            CREATE INDEX orders_user_id ON orders(user_id);
            CREATE VIEW order_summary AS SELECT id,quantity,amount FROM orders;
            CREATE TRIGGER users_audit AFTER UPDATE ON users BEGIN
                INSERT INTO audit(message) VALUES('password changed');
                UPDATE users SET updated_at='changed' WHERE id=NEW.id;
            END;
            CREATE TRIGGER email_protected BEFORE UPDATE ON email_intake_settings BEGIN
                SELECT RAISE(ABORT,'original mail edits are locked');
            END;
            CREATE TABLE ai_analysis_runs(id INTEGER PRIMARY KEY,input_tokens INTEGER,output_tokens INTEGER);
            CREATE TABLE ai_usage_ledger(id INTEGER PRIMARY KEY,input_tokens INTEGER,output_tokens INTEGER);
            INSERT INTO ai_analysis_runs VALUES(1,120,80);
            INSERT INTO ai_usage_ledger VALUES(1,120,80);
        ''')
        db.execute('INSERT INTO users VALUES(1,?,7,1,?,?)', (PASSWORD, 'finance', 'original'))
        db.execute('INSERT INTO email_intake_settings VALUES(1,?,1,?,?)',
                   (MAIL_SECRET, '["customer@example.invalid"]', 'success'))
        db.execute('INSERT INTO orders VALUES(1,1,800,5030,?,?)', ('客户名称/地址保留', b'original'))
        db.execute('CREATE TABLE deleted_payload(value BLOB)')
        db.execute('INSERT INTO deleted_payload VALUES(?)', ((PASSWORD + MAIL_SECRET).encode()*400,))
        db.execute('DROP TABLE deleted_payload')
    path.chmod(0o600)
    return path


def rows(path, sql):
    with sqlite3.connect(path) as db:
        return db.execute(sql).fetchall()


def execute(path, sql, values=()):
    with sqlite3.connect(path) as db:
        db.execute(sql, values)


def test_compacts_secrets_without_touching_source_business_schema_or_triggers(tmp_path):
    source = snapshot(tmp_path)
    original = source.read_bytes()
    assert PASSWORD.encode() in original and MAIL_SECRET.encode() in original
    assert rows(source, 'PRAGMA freelist_count')[0][0] > 0
    target = tmp_path/'clean.db'
    result = subject.sanitize(source, target)
    assert source.read_bytes() == original
    assert result['source_sha256'] == hashlib.sha256(original).hexdigest()
    assert result['sanitized_sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result['pii_anonymized'] is False
    assert result['credential_rows'] == {'users': 1, 'email_intake_settings': 1}
    assert {k: v for k, v in result.items() if k != 'source_sha256'} == subject.verify_sanitized(target)
    assert PASSWORD.encode() not in target.read_bytes()
    assert MAIL_SECRET.encode() not in target.read_bytes()
    assert PASSWORD not in json.dumps(result) and MAIL_SECRET not in json.dumps(result)
    assert rows(target, 'SELECT * FROM users') == [(1, subject.DISABLED_PASSWORD, 8, 1, 'finance', 'original')]
    assert rows(target, 'SELECT * FROM email_intake_settings') == [(1, '', 0, '["customer@example.invalid"]', 'success')]
    for table in ('orders', 'audit', 'sqlite_sequence', 'ai_analysis_runs', 'ai_usage_ledger'):
        assert rows(source, f'SELECT * FROM {table}') == rows(target, f'SELECT * FROM {table}')
    assert rows(source, 'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name') == rows(target, 'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name')
    for pragma in ('user_version', 'application_id', 'foreign_key_check', 'integrity_check'):
        assert rows(source, 'PRAGMA '+pragma) == rows(target, 'PRAGMA '+pragma)
    assert rows(target, 'PRAGMA freelist_count') == [(0,)]
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.stat().st_nlink == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ['clean.db', 'source.db']
    # The original locks still enforce their original behavior after export.
    with pytest.raises(sqlite3.IntegrityError, match='locked'):
        execute(target, 'UPDATE email_intake_settings SET automatic_enabled=1')


@pytest.mark.parametrize('field', ['password', 'refresh_token', 'accessToken', 'client_secret', 'api_key', 'credential_blob', 'input_tokens'])
def test_unknown_credential_columns_fail_closed(tmp_path, field):
    source = snapshot(tmp_path)
    execute(source, f'CREATE TABLE extra({field} TEXT)')
    before = source.read_bytes()
    with pytest.raises(ValueError, match='unrecognized credential'):
        subject.sanitize(source, tmp_path/'clean.db')
    assert source.read_bytes() == before
    assert not (tmp_path/'clean.db').exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['source.db']


def test_token_counter_exception_requires_actual_nonnegative_integers(tmp_path):
    source = snapshot(tmp_path)
    execute(source, "UPDATE ai_analysis_runs SET input_tokens='secret-token'")
    with pytest.raises(ValueError, match='integer counter'):
        subject.sanitize(source, tmp_path/'clean.db')
    assert not (tmp_path/'clean.db').exists()


@pytest.mark.parametrize('value', [None, 0, -1, 2**63-1, 'invalid'])
def test_invalid_auth_version_cannot_be_silently_changed(tmp_path, value):
    source = snapshot(tmp_path)
    # Drop only synthetic trigger to inject the invalid input for this test.
    execute(source, 'DROP TRIGGER users_audit')
    if value is None:
        execute(source, 'ALTER TABLE users RENAME COLUMN auth_version TO unreviewed_version')
    else:
        execute(source, 'UPDATE users SET auth_version=?', (value,))
    before = source.read_bytes()
    with pytest.raises(ValueError, match='authentication schema|credential state'):
        subject.sanitize(source, tmp_path/'clean.db')
    assert source.read_bytes() == before
    assert not (tmp_path/'clean.db').exists()


@pytest.mark.parametrize('present', ['empty', 'nonempty', 'symlink'])
def test_existing_target_is_never_overwritten(tmp_path, present):
    source = snapshot(tmp_path)
    target = tmp_path/'clean.db'
    if present == 'symlink':
        target.symlink_to(source)
    else:
        target.write_bytes(b'' if present == 'empty' else b'existing output')
    before = target.read_bytes()
    with pytest.raises(ValueError, match='already exists'):
        subject.sanitize(source, target)
    assert target.read_bytes() == before


@pytest.mark.parametrize('bad_source', ['symlink', 'hardlink', 'sidecar', 'public'])
def test_source_must_be_private_completed_single_snapshot(tmp_path, bad_source):
    source = snapshot(tmp_path)
    if bad_source == 'symlink':
        linked = tmp_path/'link.db'; linked.symlink_to(source); source = linked
    elif bad_source == 'hardlink':
        os.link(source, tmp_path/'link.db')
    elif bad_source == 'sidecar':
        (tmp_path/'source.db-wal').write_bytes(b'not a completed snapshot')
    else:
        source.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        subject.sanitize(source, tmp_path/'clean.db')
    assert not (tmp_path/'clean.db').exists()


def test_foreign_key_violation_refuses_delivery(tmp_path):
    source = snapshot(tmp_path)
    execute(source, 'UPDATE orders SET user_id=99')
    before = source.read_bytes()
    with pytest.raises(ValueError, match='foreign key'):
        subject.sanitize(source, tmp_path/'clean.db')
    assert source.read_bytes() == before
    assert not (tmp_path/'clean.db').exists()


def test_secret_repeated_in_business_data_refuses_instead_of_editing_business(tmp_path):
    source = snapshot(tmp_path)
    execute(source, 'UPDATE orders SET notes=?', (MAIL_SECRET,))
    before = source.read_bytes()
    with pytest.raises(ValueError, match='retains original credential'):
        subject.sanitize(source, tmp_path/'clean.db')
    assert source.read_bytes() == before
    assert not (tmp_path/'clean.db').exists()


@pytest.mark.parametrize('as_blob', [False, True])
def test_known_secret_split_across_overflow_pages_is_also_rejected(tmp_path, as_blob):
    source = snapshot(tmp_path)
    secret = 'SYNTHETIC_LONG_ENCRYPTED_CREDENTIAL_' * 1000
    execute(source, 'DROP TRIGGER email_protected')
    execute(source, 'UPDATE email_intake_settings SET encrypted_secret=?', (secret,))
    execute(source, 'UPDATE orders SET notes=?', (secret.encode() if as_blob else secret,))
    before = source.read_bytes()
    assert secret.encode() not in before  # SQLite page headers split this value.
    with pytest.raises(ValueError, match='retains original credential'):
        subject.sanitize(source, tmp_path/'clean.db')
    assert source.read_bytes() == before
    assert not (tmp_path/'clean.db').exists()


def test_verifier_rejects_unstripped_credentials_and_noncompacted_files(tmp_path):
    source = snapshot(tmp_path)
    with pytest.raises(ValueError, match='credential state'):
        subject.verify_sanitized(source)
    target = tmp_path/'clean.db'
    subject.sanitize(source, target)
    execute(target, 'CREATE TABLE waste(data BLOB)')
    execute(target, 'INSERT INTO waste VALUES(?)', (b'nonsecret'*5000,))
    execute(target, 'DROP TABLE waste')
    with pytest.raises(ValueError, match='not compacted'):
        subject.verify_sanitized(target)


def test_late_failure_removes_only_our_target(tmp_path, monkeypatch):
    source = snapshot(tmp_path)
    before = source.read_bytes()
    monkeypatch.setattr(subject.os, 'fsync', lambda *_: (_ for _ in ()).throw(OSError('synthetic failure')))
    with pytest.raises(OSError, match='synthetic failure'):
        subject.sanitize(source, tmp_path/'clean.db')
    assert source.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['source.db']


def test_public_parent_is_not_private_staging(tmp_path):
    source = snapshot(tmp_path)
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match='private staging'):
        subject.sanitize(source, tmp_path/'clean.db')
    assert not (tmp_path/'clean.db').exists()


def test_current_application_schema_is_accepted_without_importing_app_in_sanitizer(tmp_path):
    from sqlalchemy import create_engine
    from app.models import Base
    tmp_path.chmod(0o700)
    source = tmp_path/'source.db'
    engine = create_engine('sqlite:///' + str(source))
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    source.chmod(0o600)
    result = subject.sanitize(source, tmp_path/'clean.db')
    assert result['table_counts']['users'] == 0
    assert result['table_counts']['email_intake_settings'] == 0
