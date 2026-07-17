from __future__ import annotations

import importlib.util
import sqlite3
import sys
from argparse import Namespace
from pathlib import Path

from app.core.security import hash_password


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _create_users_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                auth_version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO users (
                id, username, password_hash, role, is_active,
                must_change_password, auth_version
            ) VALUES (1, 'admin', ?, 'admin', 1, 0, 4)
            """,
            (hash_password("OldPassword123!"),),
        )


def test_manage_users_reset_password_revokes_existing_sessions(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_script(
        "n031_manage_users",
        "scripts/admin/manage_users.py",
    )
    database_path = tmp_path / "users.sqlite3"
    _create_users_database(database_path)
    monkeypatch.setattr(module, "prompt_or_env_password", lambda _: "NewPassword123!")
    monkeypatch.setattr(module, "create_local_backup", lambda _: tmp_path / "backup.sqlite3")

    module.reset_password_cmd(
        Namespace(username="admin", password_env=None, apply=True),
        database_path,
    )

    with sqlite3.connect(database_path) as connection:
        auth_version, must_change = connection.execute(
            "SELECT auth_version, must_change_password FROM users WHERE id = 1"
        ).fetchone()
    assert auth_version == 5
    assert must_change == 1


def test_final_handoff_password_update_revokes_existing_sessions(tmp_path) -> None:
    module = _load_script(
        "n031_final_password_handoff",
        "scripts/admin/final_password_handoff.py",
    )
    database_path = tmp_path / "users.sqlite3"
    _create_users_database(database_path)

    module.update_passwords(database_path, {"admin": "NewPassword123!"})

    with sqlite3.connect(database_path) as connection:
        auth_version, must_change = connection.execute(
            "SELECT auth_version, must_change_password FROM users WHERE id = 1"
        ).fetchone()
    assert auth_version == 5
    assert must_change == 1
