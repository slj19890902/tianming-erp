from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
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
            CREATE TABLE operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                resource TEXT NOT NULL,
                details TEXT,
                username TEXT,
                role TEXT,
                entity_type TEXT,
                entity_id INTEGER,
                description TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO users (
                id, username, password_hash, role, is_active,
                must_change_password, auth_version
            ) VALUES (?, ?, ?, ?, 1, 0, ?)
            """,
            [
                (1, "admin", hash_password("OldPassword123!"), "admin", 4),
                (10, "finance", hash_password("OldFinance123!"), "finance", 1),
                (11, "sales", hash_password("OldSales123!"), "sales", 1),
                (12, "workshop", hash_password("OldWorkshop123!"), "workshop", 1),
            ],
        )


def _add_active_admin(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, username, password_hash, role, is_active,
                must_change_password, auth_version
            ) VALUES (2, 'backup-admin', ?, 'admin', 1, 0, 1)
            """,
            (hash_password("BackupPassword123!"),),
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
        Namespace(username="admin", password_env=None, actor="admin", apply=True),
        database_path,
    )

    with sqlite3.connect(database_path) as connection:
        auth_version, must_change = connection.execute(
            "SELECT auth_version, must_change_password FROM users WHERE id = 1"
        ).fetchone()
    assert auth_version == 5
    assert must_change == 1
    with sqlite3.connect(database_path) as connection:
        audit = connection.execute(
            """
            SELECT action, username, entity_id, details
            FROM operation_logs
            """
        ).fetchone()
    assert audit[:3] == ("MAINT_RESET_PASSWORD", "admin", 1)
    assert "NewPassword123!" not in (audit[3] or "")


def test_manage_users_normalizes_username_before_lookup(monkeypatch, tmp_path) -> None:
    module = _load_script(
        "n031_manage_users_normalized_username",
        "scripts/admin/manage_users.py",
    )
    database_path = tmp_path / "users.sqlite3"
    _create_users_database(database_path)
    monkeypatch.setattr(module, "prompt_or_env_password", lambda _: "NewPassword123!")
    monkeypatch.setattr(module, "create_local_backup", lambda _: tmp_path / "backup.sqlite3")

    results = module.reset_password_cmd(
        Namespace(
            username="　ａｄｍｉｎ　",
            password_env=None,
            actor="admin",
            apply=True,
        ),
        database_path,
    )

    assert results[0].username == "admin"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT auth_version FROM users WHERE username = 'admin'"
        ).fetchone() == (5,)


def test_final_handoff_password_update_revokes_existing_sessions(tmp_path) -> None:
    module = _load_script(
        "n031_final_password_handoff",
        "scripts/admin/final_password_handoff.py",
    )
    database_path = tmp_path / "users.sqlite3"
    _create_users_database(database_path)

    passwords = {
        "admin": "NewPassword123!",
        "finance": "NewFinance123!",
        "sales": "NewSales123!",
        "workshop": "NewWorkshop123!",
    }
    module.update_passwords(
        database_path,
        passwords,
        actor_username="admin",
    )

    with sqlite3.connect(database_path) as connection:
        auth_version, must_change = connection.execute(
            "SELECT auth_version, must_change_password FROM users WHERE id = 1"
        ).fetchone()
    assert auth_version == 5
    assert must_change == 1
    with sqlite3.connect(database_path) as connection:
        handoff_audit = connection.execute(
            """
            SELECT user_id, action, details, username, role
            FROM operation_logs
            """
        ).fetchone()
    assert handoff_audit[:2] == (1, "FINAL_PASSWORD_HANDOFF")
    assert handoff_audit[3:] == ("admin", "admin")
    audit_details = json.loads(handoff_audit[2])
    assert audit_details["target_usernames"] == [
        "admin",
        "finance",
        "sales",
        "workshop",
    ]
    assert audit_details["must_change_password"] is True
    assert set(audit_details["auth_version_changes"]) == set(passwords)
    assert not any(password in (handoff_audit[2] or "") for password in passwords.values())


@pytest.mark.parametrize("actor", ["missing-admin", "finance", "disabled-admin"])
def test_final_handoff_rejects_unrecognized_non_admin_or_inactive_actor(
    actor: str,
    tmp_path: Path,
) -> None:
    module = _load_script(
        f"n031_final_password_handoff_invalid_{actor}",
        "scripts/admin/final_password_handoff.py",
    )
    database_path = tmp_path / f"users-{actor}.sqlite3"
    _create_users_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO users (
                id, username, password_hash, role, is_active,
                must_change_password, auth_version
            ) VALUES (?, ?, ?, ?, ?, 0, 1)
            """,
            [
                (20, "disabled-admin", hash_password("DisabledPass123!"), "admin", 0),
            ],
        )
        before = connection.execute(
            "SELECT password_hash, auth_version FROM users WHERE username = 'admin'"
        ).fetchone()

    with pytest.raises(RuntimeError, match="--actor"):
        module.update_passwords(
            database_path,
            {
                "admin": "NewPassword123!",
                "finance": "NewFinance123!",
                "sales": "NewSales123!",
                "workshop": "NewWorkshop123!",
            },
            actor_username=actor,
        )

    with sqlite3.connect(database_path) as connection:
        after = connection.execute(
            "SELECT password_hash, auth_version FROM users WHERE username = 'admin'"
        ).fetchone()
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM operation_logs"
        ).fetchone()[0]
    assert after == before
    assert audit_count == 0


def test_manage_users_disable_enable_and_role_changes_revoke_sessions(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_script("n031_manage_users_state", "scripts/admin/manage_users.py")
    database_path = tmp_path / "users.sqlite3"
    _create_users_database(database_path)
    _add_active_admin(database_path)
    monkeypatch.setattr(module, "create_local_backup", lambda _: tmp_path / "backup.sqlite3")

    module.disable_user_cmd(
        Namespace(username="admin", actor="backup-admin", apply=True),
        database_path,
    )
    module.enable_user_cmd(
        Namespace(username="admin", actor="backup-admin", apply=True),
        database_path,
    )
    module.set_role_cmd(
        Namespace(
            username="admin",
            role="finance",
            actor="backup-admin",
            apply=True,
        ),
        database_path,
    )

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT role, is_active, auth_version FROM users WHERE id = 1"
        ).fetchone()
    assert row == ("finance", 1, 7)


@pytest.mark.parametrize("apply", (False, True))
@pytest.mark.parametrize("command", ("disable-user", "set-role"))
def test_manage_users_refuses_to_remove_last_active_admin(
    command,
    apply,
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_script(
        f"n031_manage_users_last_admin_{command}_{apply}",
        "scripts/admin/manage_users.py",
    )
    database_path = tmp_path / "users.sqlite3"
    _create_users_database(database_path)

    def forbidden_backup(_):
        raise AssertionError("backup must not run for a rejected operation")

    monkeypatch.setattr(module, "create_local_backup", forbidden_backup)
    args = Namespace(username="admin", actor="admin", apply=apply)
    operation = module.disable_user_cmd
    if command == "set-role":
        args.role = "finance"
        operation = module.set_role_cmd

    with pytest.raises(SystemExit, match="active admin"):
        operation(args, database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT role, is_active, auth_version FROM users WHERE id = 1"
        ).fetchone() == ("admin", 1, 4)


def test_manage_users_reuses_api_minimum_password_policy(monkeypatch, tmp_path) -> None:
    module = _load_script("n031_manage_users_policy", "scripts/admin/manage_users.py")
    database_path = tmp_path / "users.sqlite3"
    _create_users_database(database_path)
    monkeypatch.setattr(module, "prompt_or_env_password", lambda _: "password123")

    with pytest.raises(SystemExit, match="policy"):
        module.reset_password_cmd(
            Namespace(
                username="admin",
                password_env=None,
                actor="admin",
                apply=True,
            ),
            database_path,
        )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT auth_version FROM users WHERE id = 1"
        ).fetchone() == (4,)


@pytest.mark.parametrize("actor", (None, "worker"))
def test_manage_users_apply_requires_active_admin_actor(
    actor,
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_script(
        f"n031_manage_users_actor_{actor}",
        "scripts/admin/manage_users.py",
    )
    database_path = tmp_path / "users.sqlite3"
    _create_users_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, username, password_hash, role, is_active,
                must_change_password, auth_version
            ) VALUES (2, 'worker', ?, 'finance', 1, 0, 1)
            """,
            (hash_password("WorkerPassword123!"),),
        )
    monkeypatch.setattr(module, "prompt_or_env_password", lambda _: "NewPassword123!")

    def forbidden_backup(_):
        raise AssertionError("backup must not run before actor validation")

    monkeypatch.setattr(module, "create_local_backup", forbidden_backup)
    with pytest.raises(SystemExit, match="--actor"):
        module.reset_password_cmd(
            Namespace(
                username="admin",
                password_env=None,
                actor=actor,
                apply=True,
            ),
            database_path,
        )


def test_manage_users_audit_failure_rolls_back_account_change(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_script(
        "n031_manage_users_atomic_audit",
        "scripts/admin/manage_users.py",
    )
    database_path = tmp_path / "users.sqlite3"
    _create_users_database(database_path)
    monkeypatch.setattr(module, "prompt_or_env_password", lambda _: "NewPassword123!")
    monkeypatch.setattr(module, "create_local_backup", lambda _: tmp_path / "backup.sqlite3")

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(module, "write_maintenance_log", fail_audit)
    with pytest.raises(RuntimeError, match="audit write failed"):
        module.reset_password_cmd(
            Namespace(
                username="admin",
                password_env=None,
                actor="admin",
                apply=True,
            ),
            database_path,
        )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT auth_version, must_change_password FROM users WHERE id = 1"
        ).fetchone() == (4, 0)
        assert connection.execute("SELECT COUNT(*) FROM operation_logs").fetchone() == (0,)


def test_manage_users_rejects_unverified_backup(monkeypatch, tmp_path) -> None:
    module = _load_script(
        "n031_manage_users_backup_validation",
        "scripts/admin/manage_users.py",
    )
    database_path = tmp_path / "users.sqlite3"
    _create_users_database(database_path)
    bad_backup = tmp_path / "bad-backup.sqlite3"
    bad_backup.write_bytes(b"not a database")
    monkeypatch.setattr(
        module,
        "backup_to_nas",
        lambda **_kwargs: SimpleNamespace(
            path=bad_backup,
            integrity_check="corrupt",
            size=bad_backup.stat().st_size,
            sha256="0" * 64,
        ),
    )

    with pytest.raises(RuntimeError, match="verified maintenance backup"):
        module.create_local_backup(database_path)
