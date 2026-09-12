import sqlite3
import pytest
from alembic import command
from tests.test_email_automatic_sync_migration import _config


def test_sender_filter_roundtrip_and_configured_downgrade_guard(monkeypatch, tmp_path):
    path = tmp_path / 'sender-migration.sqlite3'
    config = _config(monkeypatch, path)
    command.upgrade(config, 'sr30v8x9z92')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO email_intake_settings(id,encrypted_secret,version) VALUES(1,'ciphertext',1)")
    command.upgrade(config, 'ss31v8x9z93')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT sender_addresses_json FROM email_intake_settings').fetchone() == (None,)
    command.downgrade(config, 'sr30v8x9z92')
    command.upgrade(config, 'ss31v8x9z93')
    with sqlite3.connect(path) as db:
        db.execute("UPDATE email_intake_settings SET sender_addresses_json='[]'")
    with pytest.raises(RuntimeError, match='禁止降级'):
        command.downgrade(config, 'sr30v8x9z92')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT sender_addresses_json FROM email_intake_settings').fetchone() == ('[]',)
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
