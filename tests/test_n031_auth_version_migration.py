from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "az53v8x9z43"
TARGET_REVISION = "ba54v8x9z44"
CONFIRMATION_ENV = "N031_AUTH_VERSION_DOWNGRADE_CONFIRM"
CONFIRMATION_VALUE = "DOWNTIME_COMPLETE_AND_SESSION_SECRET_ROTATED"


def _alembic_config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n031-migration-test-secret")
    monkeypatch.delenv(CONFIRMATION_ENV, raising=False)
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _insert_az53_user(connection: sqlite3.Connection, username: str) -> None:
    connection.execute(
        """
        INSERT INTO users (
            username, password_hash, role, real_name, display_name,
            is_active, must_change_password, customer_access_mode
        ) VALUES (?, 'hash', 'admin', ?, NULL, 1, 0, 'all')
        """,
        (username, username),
    )
    connection.commit()


def _assert_integrity(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_auth_version_backfills_az53_users_and_refuses_data_loss_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "n031-existing-user.sqlite3"
    config = _alembic_config(monkeypatch, database_path)
    command.upgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_az53_user(connection, "legacy-admin")

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT username, auth_version FROM users"
        ).fetchall() == [("legacy-admin", 1)]
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET_REVISION,)
        _assert_integrity(connection)

    with pytest.raises(RuntimeError, match="Refusing to drop users.auth_version"):
        command.downgrade(config, PARENT_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT username, auth_version FROM users"
        ).fetchall() == [("legacy-admin", 1)]
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET_REVISION,)
        _assert_integrity(connection)


def test_auth_version_empty_database_round_trips_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "n031-empty-roundtrip.sqlite3"
    config = _alembic_config(monkeypatch, database_path)
    command.upgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    command.downgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone() == (0,)
        assert "auth_version" in {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET_REVISION,)
        _assert_integrity(connection)


def test_auth_version_data_loss_downgrade_requires_explicit_operational_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "n031-confirmed-downgrade.sqlite3"
    config = _alembic_config(monkeypatch, database_path)
    command.upgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_az53_user(connection, "rollback-admin")
    command.upgrade(config, TARGET_REVISION)

    monkeypatch.setenv(CONFIRMATION_ENV, CONFIRMATION_VALUE)
    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert "auth_version" not in {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        assert connection.execute("SELECT username FROM users").fetchall() == [
            ("rollback-admin",)
        ]
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (PARENT_REVISION,)
        _assert_integrity(connection)

    monkeypatch.delenv(CONFIRMATION_ENV)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT username, auth_version FROM users"
        ).fetchall() == [("rollback-admin", 1)]
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET_REVISION,)
        _assert_integrity(connection)
