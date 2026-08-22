from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "de39v8x9z28"
TARGET_REVISION = "dh42v8x9z31"
TABLE_NAME = "production_location_selection_sessions"


def _config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-93-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _health(database_path: Path, expected_revision: str) -> None:
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (expected_revision,)


def test_p1_93_migration_roundtrip_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "p1-93-location-selection.sqlite3"
    config = _config(monkeypatch, database_path)

    command.upgrade(config, TARGET_REVISION)
    _health(database_path, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE_NAME,),
        ).fetchone() == (1,)

    command.downgrade(config, PARENT_REVISION)
    _health(database_path, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE_NAME,),
        ).fetchone() is None

    command.upgrade(config, TARGET_REVISION)
    _health(database_path, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f"""
            INSERT INTO {TABLE_NAME} (
                token, completion_id, user_id, customer_id,
                create_idempotency_key, create_request_hash, status, expires_at
            ) VALUES (?, 1, 1, 1, ?, ?, 'open', '2099-01-01 00:00:00')
            """,
            ("p1-93-test-token", "p1-93-create", "0" * 64),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="P1-93 location selection sessions exist"):
        command.downgrade(config, PARENT_REVISION)

    # The synthetic row deliberately bypasses foreign-key enforcement so this
    # migration-only test does not have to manufacture an entire production
    # workflow.  First prove that the refused downgrade left the schema and
    # revision intact, then remove that synthetic row before the full FK check.
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET_REVISION,)
        assert connection.execute(
            "SELECT COUNT(*) FROM production_location_selection_sessions"
        ).fetchone() == (1,)
        connection.execute(f"DELETE FROM {TABLE_NAME}")
        connection.commit()
    _health(database_path, TARGET_REVISION)
