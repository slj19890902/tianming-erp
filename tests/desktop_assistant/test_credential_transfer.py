import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from desktop_assistant import credential_transfer as transfer

PASSWORD = 'synthetic-transfer-passphrase-only'


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path.resolve() / 'source'
    (root / 'control').mkdir(parents=True)
    (root / 'shared/data').mkdir(parents=True)
    configs = {
        'preferences.json': {'protected_password': 'windows-backup-cipher', 'nas': r'Z:\ERP-backups', 'retained': 42},
        'control/ai-provider.json': {'provider': 'openai', 'protected_api_key': 'windows-openai-cipher'},
        'control/deepseek-provider.json': {'provider': 'deepseek', 'protected_api_key': 'windows-deepseek-cipher'},
    }
    for name, record in configs.items():
        (root / name).write_text(json.dumps(record))
    database = root / 'shared/data/carton_erp.sqlite3'
    with sqlite3.connect(database) as db:
        db.executescript("CREATE TABLE email_intake_settings(id INTEGER PRIMARY KEY,encrypted_secret TEXT,version INTEGER,uid TEXT); INSERT INTO email_intake_settings VALUES(1,'windows-mailbox-cipher',7,'unchanged-source');")
    monkeypatch.setattr(transfer, 'unprotect', lambda value, purpose: 'synthetic-secret-' + purpose)
    return root


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def test_export_round_trip_readonly_source_no_plaintext_and_no_overwrite(source, tmp_path):
    before = hashes(source)
    destination = tmp_path / 'credentials.tmencrypted'
    receipt = transfer.export(source, destination, PASSWORD)
    raw = destination.read_bytes()
    assert not any(value in raw for value in [b'synthetic-secret', b'windows-mailbox-cipher', b'ERP-backups'])
    assert 'synthetic-secret' not in json.dumps(receipt)
    assert receipt['purposes'] == ['backup', 'deepseek', 'mailbox', 'openai']
    record = transfer.unseal(raw, PASSWORD)
    assert record['entries']['mailbox']['source_version'] == 7
    assert record['entries']['backup']['value'] == 'synthetic-secret-backup'
    assert json.loads(record['files']['preferences.json'])['retained'] == 42
    assert hashes(source) == before
    if os.name != 'nt':
        assert destination.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='覆盖'):
        transfer.export(source, destination, PASSWORD)
    assert destination.read_bytes() == raw


def test_wrong_password_tamper_truncation_and_oversize_return_no_record(source):
    raw = transfer.seal(transfer.collect(source), PASSWORD)
    for data, password in [(raw, 'synthetic-wrong-passphrase'),
                           (raw[:-1] + bytes([raw[-1] ^ 1]), PASSWORD),
                           (raw[:len(transfer.MAGIC)] + b'x' + raw[len(transfer.MAGIC)+1:], PASSWORD),
                           (raw[:-20], PASSWORD), (b'x'*(transfer.LIMIT+1), PASSWORD)]:
        with pytest.raises(ValueError, match='加密凭据包已损坏'):
            transfer.unseal(data, password)


def test_invalid_contents_are_rejected_before_encryption(source):
    valid = transfer.collect(source)
    invalid = []
    item = copy.deepcopy(valid); item['entries']['publisher'] = {}; invalid.append(item)
    item = copy.deepcopy(valid); item['files']['../escape.json'] = '{}'; invalid.append(item)
    item = copy.deepcopy(valid); item['entries']['mailbox']['source_version'] = True; invalid.append(item)
    item = copy.deepcopy(valid); item['entries']['backup']['source_protected_sha256'] = '0'*64; invalid.append(item)
    item = copy.deepcopy(valid); item['files']['preferences.json'] = '{}'; invalid.append(item)
    item = copy.deepcopy(valid); del item['entries']['openai']; invalid.append(item)
    for record in invalid:
        with pytest.raises(ValueError):
            transfer.seal(record, PASSWORD)
    with pytest.raises(ValueError, match='12至'):
        transfer.seal(valid, 'short')


def test_original_user_failure_cannot_create_partial_export(source, tmp_path, monkeypatch):
    def refuse(*args):
        raise ValueError('synthetic-sensitive-error-must-not-leak')
    monkeypatch.setattr(transfer, 'unprotect', refuse)
    destination = tmp_path / 'denied.tmencrypted'
    with pytest.raises(ValueError) as caught:
        transfer.export(source, destination, PASSWORD)
    assert 'synthetic-sensitive' not in str(caught.value)
    assert not destination.exists()


def test_concurrent_config_change_refused(source, monkeypatch):
    def change(value, purpose):
        (source / 'preferences.json').write_text('{}')
        return 'synthetic-secret'
    monkeypatch.setattr(transfer, 'unprotect', change)
    with pytest.raises(ValueError, match='导出期间变化'):
        transfer.collect(source)


def test_missing_database_does_not_create_empty_database(source):
    database = source / 'shared/data/carton_erp.sqlite3'
    database.unlink()
    with pytest.raises(ValueError, match='来源数据库'):
        transfer.collect(source)
    assert not database.exists()


def test_source_links_refused(source, tmp_path):
    path = source / 'control/ai-provider.json'
    target = tmp_path / 'outside.json'
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(ValueError, match='链接'):
        transfer.collect(source)


def test_absent_credentials_are_explicit_and_unknown_business_data_unchanged(source):
    for name in transfer.CONFIGS.values():
        (source / name[0]).unlink()
    with sqlite3.connect(source / 'shared/data/carton_erp.sqlite3') as db:
        db.execute('DELETE FROM email_intake_settings')
    before = hashes(source)
    record = transfer.collect(source)
    assert record['entries'] == {} and record['files'] == {}
    assert hashes(source) == before


def test_cli_refuses_password_pipe_without_reading_source(tmp_path):
    destination = tmp_path / 'must-not-exist'
    result = subprocess.run([sys.executable, '-m', 'desktop_assistant.credential_transfer',
        '--root', str(tmp_path / 'missing'), '--output', str(destination)],
        input=PASSWORD, capture_output=True, text=True)
    assert result.returncode != 0
    assert PASSWORD not in result.stdout + result.stderr
    assert '交互终端' in result.stderr
    assert not destination.exists()
