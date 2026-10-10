from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "ce61v8x9z50"
TARGET_REVISION = "cf62v8x9z51"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n043-migration-test-secret-long-enough")
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.delenv("ERP_EMAIL_INTAKE_ENABLED", raising=False)
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _assert_integrity(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_n043_migration_is_linear_and_roundtrips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "n043-migration.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "email_order_intake_poll_states",
            "email_order_sender_mappings",
            "email_order_intake_messages",
            "email_order_intake_attachments",
            "email_order_intake_drafts",
        } <= tables
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (TARGET_REVISION,)
        mapping_indexes = connection.execute("PRAGMA index_list(email_order_sender_mappings)").fetchall()
        assert any(row[2] == 1 for row in mapping_indexes)
        _assert_integrity(connection)

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "email_order_intake_messages" not in tables
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PARENT_REVISION,)
        _assert_integrity(connection)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (TARGET_REVISION,)
        _assert_integrity(connection)


def test_n043_revision_declares_required_parent() -> None:
    migration = (PROJECT_ROOT / "alembic" / "versions" / "cf62v8x9z51_n043_email_order_intake.py").read_text(encoding="utf-8")
    assert 'revision = "cf62v8x9z51"' in migration
    assert 'down_revision = "ce61v8x9z50"' in migration


def test_n043_downgrade_is_blocked_when_intake_facts_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "n043-populated.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO email_order_intake_poll_states (
                mailbox_key, last_status, examined_count, created_count,
                duplicate_count, failed_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("orders", "success", 1, 1, 0, 0),
        )
        connection.commit()
        _assert_integrity(connection)

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET_REVISION,)
        assert connection.execute(
            "SELECT mailbox_key, last_status FROM email_order_intake_poll_states"
        ).fetchall() == [("orders", "success")]
        _assert_integrity(connection)
