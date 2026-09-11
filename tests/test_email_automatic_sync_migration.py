from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv('ERP_DATABASE_PATH', str(path))
    config = Config(str(ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(ROOT / 'alembic'))
    config.set_main_option('sqlalchemy.url', 'sqlite:///' + path.as_posix())
    return config


def test_email_automatic_sync_upgrade_downgrade_roundtrip(monkeypatch, tmp_path: Path):
    db_path = tmp_path / 'mail-auto.sqlite3'
    config = _config(monkeypatch, db_path)
    command.upgrade(config, 'sq29v8x9z91')
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO email_intake_settings(id,encrypted_secret,version) VALUES(1,'ciphertext',1)")
        db.commit()
    command.upgrade(config, 'sr30v8x9z92')
    with sqlite3.connect(db_path) as db:
        row = db.execute('SELECT automatic_enabled,sync_interval_minutes,last_sync_status FROM email_intake_settings').fetchone()
        assert row == (0, 5, 'never')
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    command.downgrade(config, 'sq29v8x9z91')
    command.upgrade(config, 'sr30v8x9z92')


def test_email_automatic_sync_facts_block_downgrade(monkeypatch, tmp_path: Path):
    db_path = tmp_path / 'mail-auto-block.sqlite3'
    config = _config(monkeypatch, db_path)
    command.upgrade(config, 'sr30v8x9z92')
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO email_intake_settings(id,encrypted_secret,version,automatic_enabled,sync_interval_minutes,last_sync_status,last_sync_received,last_sync_remaining) VALUES(1,'ciphertext',1,1,5,'never',0,0)")
        db.commit()
    try:
        command.downgrade(config, 'sq29v8x9z91')
    except RuntimeError as error:
        assert '禁止降级' in str(error)
    else:
        raise AssertionError('automatic mail facts must block downgrade')
