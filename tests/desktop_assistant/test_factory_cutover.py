"""Synthetic identities and disposable facts only; no real cutover or keys."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant import factory_cutover as cutover
from desktop_assistant.attachments import rebind_pdf_sources
from desktop_assistant.signing import public_bytes
from desktop_assistant.storage import sha

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


@pytest.fixture
def shared(tmp_path):
    root = tmp_path.resolve() / 'source'
    (root / 'data').mkdir(parents=True)
    sample = root / 'data/source.pdf'
    sample.write_bytes(b'synthetic-pdf-source')
    (root / 'historical-evidence.bin').write_bytes(b'preserve all unregistered evidence')
    (root / 'environment.json').write_text(json.dumps({
        'ERP_ENVIRONMENT': 'production', 'ERP_SECRET_KEY_FILE': '${SHARED}/session.key',
        'ERP_PORT': '8000', 'ERP_ALLOWED_ORIGINS': 'http://192.168.3.80:8000'}))
    with sqlite3.connect(root / 'data/carton_erp.sqlite3') as db:
        db.executescript('''
            CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY);
            INSERT INTO alembic_version VALUES('synthetic_head');
            CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, password_hash TEXT, role TEXT);
            INSERT INTO users(password_hash,role) VALUES('synthetic-hash','admin');
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO customers VALUES(1,'synthetic-customer');
            CREATE TABLE products(id INTEGER PRIMARY KEY, quantity INTEGER);
            INSERT INTO products VALUES(1,5);
            CREATE TABLE sales_orders(id INTEGER PRIMARY KEY, amount NUMERIC);
            INSERT INTO sales_orders VALUES(1,20);
            CREATE TABLE email_intake_settings(id INTEGER PRIMARY KEY,encrypted_secret TEXT,version INTEGER);
            INSERT INTO email_intake_settings VALUES(1,'synthetic-source-ciphertext',9);
            CREATE TABLE pdf_order_training_samples(id INTEGER PRIMARY KEY,file_path TEXT,file_sha256 TEXT);
        ''')
        db.execute('INSERT INTO pdf_order_training_samples VALUES(1,?,?)', (str(sample), sha(sample)))
    return root


def copy_restored(shared):
    target = shared.parent / 'restored'
    shutil.copytree(shared, target)
    rebind_pdf_sources(target / 'data/carton_erp.sqlite3', shared, target, target)
    with sqlite3.connect(target / 'data/carton_erp.sqlite3') as db:
        db.execute("UPDATE email_intake_settings SET encrypted_secret='synthetic-destination-reference'")
    environment = json.loads((target / 'environment.json').read_bytes())
    environment.update(ERP_PORT='18991', ERP_HOME_REHEARSAL='1', ERP_ALLOWED_ORIGINS='http://127.0.0.1:18991')
    (target / 'environment.json').write_text(json.dumps(environment))
    return target


@pytest.fixture
def case(shared):
    key = Ed25519PrivateKey.generate()
    public = public_bytes(key)
    target = {'host_sha256': 'a' * 64, 'installation_id': 'b' * 32,
              'service_uid': 502, 'service_gid': 502, 'service_account': '_tianmingerp',
              'root': '/Library/Application Support/TianmingERP'}
    request = cutover.make_request(target, 'c' * 64, 'd' * 64, public, now=NOW)
    source = {'source_sha256': 'e' * 64, 'backup_sha256': 'f' * 64,
              'source_package_sha256': '1' * 64, 'backup_created_at': '2026-10-11T00:00:10Z',
              'manual_stop': True, 'process_stopped': True,
              'credential_purposes': ['backup', 'deepseek', 'mailbox'],
              'snapshot': cutover.capture_snapshot(shared)}
    raw = cutover.sign_authorization(request, source, key, now=NOW + timedelta(seconds=20))
    return SimpleNamespace(key=key, public=public, request=request, source=source, raw=raw, shared=shared)


def test_original_identity_exact_target_and_full_restore_binding(case):
    target = copy_restored(case.shared)
    restored = cutover.capture_snapshot(target)
    before = sha(case.shared / 'data/carton_erp.sqlite3')
    body = cutover.verify_installation_binding(case.raw, case.public, case.request,
        case.source['backup_sha256'], restored, now=NOW + timedelta(seconds=21))
    assert body['source']['snapshot']['database_sha256'] != restored['database_sha256']
    assert body['source']['snapshot']['facts_sha256'] == restored['facts_sha256']
    assert before == sha(case.shared / 'data/carton_erp.sqlite3')
    assert not (target / 'state.json').exists()  # The protocol never activates.


@pytest.mark.parametrize('field,value', [
    ('host_sha256', '0' * 64), ('installation_id', '0' * 32), ('service_uid', 503),
    ('service_gid', 503), ('root', '/Users/another/ERP'),
])
def test_other_host_account_or_directory_cannot_reuse_ticket(case, field, value):
    expected = deepcopy(case.request)
    expected['target'][field] = value
    with pytest.raises(ValueError, match='不一致'):
        cutover.verify_authorization(case.raw, case.public, expected)


@pytest.mark.parametrize('field', ['request_id', 'package_sha256', 'network_sha256'])
def test_other_request_program_or_network_cannot_reuse_ticket(case, field):
    expected = deepcopy(case.request)
    expected[field] = '0' * len(expected[field])
    with pytest.raises(ValueError, match='不一致'):
        cutover.verify_authorization(case.raw, case.public, expected)


def test_other_publisher_tampering_and_other_signature_purpose_rejected(case):
    with pytest.raises(InvalidSignature):
        cutover.verify_authorization(case.raw, public_bytes(Ed25519PrivateKey.generate()), case.request)
    envelope = json.loads(case.raw)
    envelope['body']['source']['snapshot']['facts_sha256'] = '0' * 64
    with pytest.raises(InvalidSignature):
        cutover.verify_authorization(cutover.canonical(envelope), case.public, case.request)
    envelope = json.loads(case.raw)
    envelope['signature'] = base64.b64encode(case.key.sign(cutover.canonical(envelope['body']))).decode()
    with pytest.raises(InvalidSignature):
        cutover.verify_authorization(cutover.canonical(envelope), case.public, case.request)
    with pytest.raises(ValueError, match='既有'):
        cutover.sign_authorization(case.request, case.source, Ed25519PrivateKey.generate(), now=NOW)


def test_duplicate_unknown_fields_and_oversized_input_rejected(case):
    duplicate = case.raw.replace(b'"body":', b'"body":{},"body":', 1)
    with pytest.raises(ValueError, match='重复'):
        cutover.verify_authorization(duplicate, case.public, case.request)
    envelope = json.loads(case.raw)
    envelope['body']['allow_root'] = True
    with pytest.raises(ValueError, match='字段'):
        cutover.verify_authorization(cutover.canonical(envelope), case.public, case.request)
    with pytest.raises(ValueError, match='大小'):
        cutover.verify_authorization(b'x' * (cutover.LIMIT + 1), case.public, case.request)


@pytest.mark.parametrize('uid', [0, True, -1, '502'])
def test_root_and_ambiguous_identity_rejected(case, uid):
    request = deepcopy(case.request)
    request['target']['service_uid'] = uid
    with pytest.raises(ValueError, match='UID'):
        cutover.validate_request(request)


def test_personal_account_and_privileged_group_rejected(case):
    request = deepcopy(case.request)
    request['target']['service_account'] = 'personal_user'
    with pytest.raises(ValueError, match='专用'):
        cutover.validate_request(request)
    request = deepcopy(case.request)
    request['target']['service_gid'] = 0
    with pytest.raises(ValueError, match='GID'):
        cutover.validate_request(request)


@pytest.mark.parametrize('root', ['/', '//server/ERP', '/a/../ERP', '/a//ERP', 'D:\\ERP', '/ERP\nother'])
def test_noncanonical_target_paths_rejected(case, root):
    request = deepcopy(case.request)
    request['target']['root'] = root
    with pytest.raises(ValueError, match='绝对路径'):
        cutover.validate_request(request)


def test_stop_declaration_old_backup_and_wrong_restore_rejected(case):
    for field in ('manual_stop', 'process_stopped'):
        source = deepcopy(case.source)
        source[field] = 1  # Only literal true is an explicit assertion.
        with pytest.raises(ValueError, match='停写'):
            cutover.sign_authorization(case.request, source, case.key, now=NOW + timedelta(seconds=20))
    source = deepcopy(case.source)
    source['backup_created_at'] = '2026-10-10T23:59:59Z'
    with pytest.raises(ValueError, match='最终备份'):
        cutover.sign_authorization(case.request, source, case.key, now=NOW + timedelta(seconds=20))
    with pytest.raises(ValueError, match='最终停写备份'):
        cutover.verify_installation_binding(case.raw, case.public, case.request, '0' * 64,
            case.source['snapshot'], now=NOW + timedelta(seconds=21))


@pytest.mark.parametrize('purposes', [
    ['deepseek'], ['backup', 'publisher'], ['backup', 'mailbox', 'mailbox'], ['mailbox', 'backup'],
])
def test_missing_backup_private_key_or_ambiguous_credential_scope_rejected(case, purposes):
    source = deepcopy(case.source)
    source['credential_purposes'] = purposes
    with pytest.raises(ValueError, match='凭据用途'):
        cutover.sign_authorization(case.request, source, case.key, now=NOW + timedelta(seconds=20))


def test_installation_expires_without_expiring_a_verified_running_server(case):
    body = cutover.verify_authorization(case.raw, case.public, case.request)
    cutover.require_install_window(body, now=NOW + timedelta(seconds=20))
    for when in (NOW, NOW + timedelta(hours=8, seconds=20), NOW + timedelta(days=8)):
        with pytest.raises(ValueError, match='时窗'):
            cutover.require_install_window(body, now=when)
    # Signature/runtime binding remains verifiable; the installation deadline
    # is not a lease that would silently stop ERP after eight hours.
    assert cutover.verify_authorization(case.raw, case.public, case.request) == body
    with pytest.raises(ValueError, match='时区'):
        cutover.require_install_window(body, now=datetime(2026, 10, 11))


@pytest.mark.parametrize('statement', [
    "UPDATE users SET password_hash='changed'", "UPDATE users SET role='sales'",
    "UPDATE customers SET name='changed'", 'UPDATE products SET quantity=6',
    'UPDATE sales_orders SET amount=21', 'UPDATE email_intake_settings SET version=10',
    "UPDATE sqlite_sequence SET seq=99 WHERE name='users'",
])
def test_every_business_fact_and_sequence_is_retained(case, statement):
    target = copy_restored(case.shared)
    with sqlite3.connect(target / 'data/carton_erp.sqlite3') as db:
        db.execute(statement)
    with pytest.raises(ValueError, match='事实'):
        cutover.require_matching_snapshots(case.source['snapshot'], cutover.capture_snapshot(target))


@pytest.mark.parametrize('kind', ['unregistered_file', 'extra_file', 'preserved_config', 'schema', 'null_pdf'])
def test_unknown_files_config_schema_and_empty_pdf_semantics_preserved(case, kind):
    target = copy_restored(case.shared)
    if kind == 'unregistered_file':
        (target / 'historical-evidence.bin').write_bytes(b'changed')
    elif kind == 'extra_file':
        (target / 'extra.bin').write_bytes(b'extra')
    elif kind == 'preserved_config':
        environment = json.loads((target / 'environment.json').read_bytes())
        environment['ERP_SECRET_KEY_FILE'] = '${SHARED}/another.key'
        (target / 'environment.json').write_text(json.dumps(environment))
    else:
        with sqlite3.connect(target / 'data/carton_erp.sqlite3') as db:
            db.execute('CREATE INDEX unexpected ON products(quantity)' if kind == 'schema'
                       else 'UPDATE pdf_order_training_samples SET file_path=NULL')
    with pytest.raises(ValueError, match='事实'):
        cutover.require_matching_snapshots(case.source['snapshot'], cutover.capture_snapshot(target))


def test_windows_source_paths_normalize_to_identical_relative_references(shared):
    source = cutover.capture_snapshot(shared)
    with sqlite3.connect(shared / 'data/carton_erp.sqlite3') as db:
        db.execute('UPDATE pdf_order_training_samples SET file_path=?', ('D:\\TianmingERP\\shared\\data\\source.pdf',))
    windows = cutover.capture_snapshot(shared, recorded_root='D:\\TianmingERP\\shared')
    cutover.require_matching_snapshots(source, windows)


@pytest.mark.parametrize('suffix', ['-wal', '-journal'])
def test_offline_snapshot_refuses_uncheckpointed_content(shared, suffix):
    Path(str(shared / 'data/carton_erp.sqlite3') + suffix).write_bytes(b'unmerged')
    with pytest.raises(ValueError, match='停写静态库'):
        cutover.capture_snapshot(shared)


def test_missing_attachment_link_and_concurrent_write_fail_closed(shared, monkeypatch):
    sample = shared / 'data/source.pdf'
    sample.unlink()
    with pytest.raises(ValueError, match='附件'):
        cutover.capture_snapshot(shared)
    sample.write_bytes(b'synthetic-pdf-source')
    alias = shared / 'alias'
    alias.symlink_to(sample)
    with pytest.raises(ValueError, match='链接'):
        cutover.capture_snapshot(shared)
    alias.unlink()
    original = cutover.facts
    def mutating_reader(database, columns):
        value = original(database, columns)
        with sqlite3.connect(database) as db:
            db.execute('UPDATE products SET quantity=7')
        return value
    monkeypatch.setattr(cutover, 'facts', mutating_reader)
    with pytest.raises(ValueError, match='期间发生变化'):
        cutover.capture_snapshot(shared)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Mac service-account identity')
def test_local_identity_is_read_only_and_rejects_admin_or_open_permissions(tmp_path, monkeypatch):
    import grp
    import pwd
    root = tmp_path.resolve() / 'service'
    root.mkdir(mode=0o700)
    uid = os.getuid()
    monkeypatch.setattr(cutover.sys, 'platform', 'darwin')
    monkeypatch.setattr(cutover.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(cutover.os, 'geteuid', lambda: uid or 502)
    monkeypatch.setattr(cutover.os, 'getegid', lambda: 502)
    monkeypatch.setattr(cutover.os, 'getgroups', lambda: [])
    monkeypatch.setattr(pwd, 'getpwuid', lambda _: SimpleNamespace(pw_uid=uid, pw_gid=502, pw_name='_tianmingerp'))
    monkeypatch.setattr(grp, 'getgrnam', lambda _: SimpleNamespace(gr_gid=80, gr_mem=[]))
    monkeypatch.setattr(grp, 'getgrgid', lambda _: SimpleNamespace(gr_name='_tianmingerp'))
    original = Path.stat
    def stat(path, *args, **kwargs):
        return original(root) if path == Path('/System/Volumes/Data') else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', stat)
    def ioreg(args, **kwargs):
        assert args == ['/usr/sbin/ioreg', '-a', '-r', '-d', '1', '-c', 'IOPlatformExpertDevice']
        return SimpleNamespace(stdout=plistlib.dumps([{'IOPlatformUUID': '12345678-1234-1234-1234-123456789012'}]))
    monkeypatch.setattr(cutover.subprocess, 'run', ioreg)
    if uid == 0:
        pytest.skip('non-root filesystem identity fixture')
    target = cutover.local_target(root, 'a' * 32)
    assert target['service_account'] == '_tianmingerp' and target['service_uid'] == uid
    assert not list(root.iterdir())
    monkeypatch.setattr(cutover.os, 'getgroups', lambda: [80])
    with pytest.raises(ValueError, match='管理员'):
        cutover.local_target(root, 'a' * 32)
    monkeypatch.setattr(cutover.os, 'getgroups', lambda: [])
    root.chmod(0o755)
    with pytest.raises(ValueError, match='独占'):
        cutover.local_target(root, 'a' * 32)
    root.chmod(0o700)
    root.parent.chmod(0o777)
    with pytest.raises(ValueError, match='祖先目录'):
        cutover.local_target(root, 'a' * 32)
    root.parent.chmod(0o700)
