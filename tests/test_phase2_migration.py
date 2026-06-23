from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker


def test_phase2_migration_preserves_legacy_users_and_operation_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.core.security import verify_password
    from app.models.user import User
    from init_db import initialize_users

    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('boss', 'workshop')),
                display_name TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT
            );
            CREATE TABLE operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                role TEXT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                description TEXT,
                ip_address TEXT,
                user_agent TEXT,
                extra_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            INSERT INTO users (username, password_hash, role, display_name)
            VALUES
                ('boss', 'legacy-sha256', 'boss', '老板端'),
                ('workshop', 'legacy-sha256', 'workshop', '车间端');
            INSERT INTO operation_logs (
                user_id, username, role, action, entity_type, entity_id, description
            )
            VALUES (1, 'boss', 'boss', 'login', 'user', 1, '旧登录日志');
            """
        )
        connection.commit()

    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "migration-test-secret")
    alembic_config = Config(
        str(Path(__file__).resolve().parents[1] / "alembic.ini")
    )
    command.upgrade(alembic_config, "head")

    with sqlite3.connect(database_path) as connection:
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        log_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(operation_logs)")
        }
        migrated_users = connection.execute(
            "SELECT username, role, must_change_password FROM users ORDER BY id"
        ).fetchall()
        legacy_log = connection.execute(
            "SELECT action, resource, details FROM operation_logs"
        ).fetchone()

    assert {"real_name", "must_change_password"} <= user_columns
    assert {"resource", "details", "ip_address", "created_at"} <= log_columns
    assert migrated_users == [
        ("admin", "admin", 1),
        ("workshop", "workshop", 1),
    ]
    assert legacy_log == ("login", "user", "旧登录日志")

    engine = create_sqlite_engine(database_path)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    assert (
        initialize_users(
            session_factory,
            initial_password="MigratedPass123!",
        )
        == 4
    )
    with session_factory() as session:
        users = session.scalars(select(User).order_by(User.username)).all()

    assert {user.role for user in users} == {
        "admin",
        "finance",
        "sales",
        "workshop",
    }
    assert all(
        verify_password("MigratedPass123!", user.password_hash) for user in users
    )
    assert all(user.must_change_password for user in users)
