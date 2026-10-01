from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "admin"
    / "final_password_handoff.py"
)
spec = importlib.util.spec_from_file_location("final_password_handoff", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def _passwords(label: str) -> dict[str, str]:
    return {
        username: f"Isolated{label}{index}{username.title()}"
        for index, username in enumerate(module.VALID_USERS, start=1)
    }


def _settings(
    database_path: Path,
    *,
    production: bool = True,
    transport: str = "https_proxy",
) -> SimpleNamespace:
    public = "https://erp.example.test"
    lan = "http://192.168.3.80:8000"
    allowed = (public,) if transport == "https_proxy" else (lan,)
    private = (lan,) if transport == "https_proxy" else ()
    return SimpleNamespace(
        is_production=production,
        production_transport=transport if production else "development",
        allowed_origins=allowed,
        private_http_origins=private,
        browser_url=public if transport == "https_proxy" else lan,
        database_path=database_path,
        session_cookie_name="erp_session",
        port=8000,
    )


def _create_database(path: Path, *, omit_user: str | None = None) -> None:
    old_passwords = _passwords("Old")
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                is_active INTEGER NOT NULL,
                must_change_password INTEGER NOT NULL,
                password_hash TEXT NOT NULL,
                auth_version INTEGER NOT NULL,
                updated_at TEXT
            );
            CREATE TABLE operation_logs (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                action TEXT,
                resource TEXT,
                details TEXT,
                username TEXT,
                role TEXT,
                entity_type TEXT,
                description TEXT
            );
            CREATE TABLE sales_orders (id INTEGER PRIMARY KEY, order_number TEXT);
            CREATE TABLE sales_order_items (id INTEGER PRIMARY KEY);
            CREATE TABLE legacy_ruida_orders (id INTEGER PRIMARY KEY);
            CREATE TABLE legacy_ruida_order_items (id INTEGER PRIMARY KEY);
            """
        )
        for index, username in enumerate(module.VALID_USERS, start=1):
            if username == omit_user:
                continue
            connection.execute(
                """
                INSERT INTO users (
                    id, username, role, is_active, must_change_password,
                    password_hash, auth_version, updated_at
                ) VALUES (?, ?, ?, 1, 0, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    index,
                    username,
                    module.EXPECTED_ROLES[username],
                    module.hash_password(old_passwords[username]),
                    index,
                ),
            )
        connection.execute(
            "INSERT INTO sales_orders (id, order_number) VALUES (1, 'TEST-1')"
        )
        connection.execute("INSERT INTO sales_order_items (id) VALUES (1)")
        connection.commit()


def _ok_api() -> module.ApiResult:
    return module.ApiResult(200, {"ok": True}, "application/json")


def test_validate_handoff_password_uses_account_specific_policy() -> None:
    assert module.validate_handoff_password("Safe1234", "finance") == []
    assert module.validate_handoff_password("Safe123456", "admin") == []
    assert "至少 10 位" in module.validate_handoff_password("Safe12345", "admin")
    assert "至少包含 1 个数字" in module.validate_handoff_password(
        "abcdefgh", "sales"
    )


def test_collect_new_passwords_reprompts_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _passwords("New")
    answers = iter(
        [
            first["admin"],
            first["admin"],
            first["finance"],
            first["sales"],
            first["workshop"],
        ]
    )
    monkeypatch.setattr(module, "prompt_password", lambda _username: next(answers))
    result = module.collect_new_passwords()
    assert len(result) == 4
    assert len(set(result.values())) == 4


def test_handoff_cli_requires_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "final_password_handoff.py",
            "--sqlite-path",
            "users.sqlite3",
            "--output-json",
            "result.json",
        ],
    )
    with pytest.raises(SystemExit):
        module.parse_args()


def test_password_prompt_refuses_non_interactive_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(RuntimeError, match="禁止回显"):
        module.prompt_password("admin")


def test_production_https_must_match_configured_origin(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "db.sqlite3")
    assert (
        module._validate_api_base_url("https://erp.example.test", settings)
        == "https://erp.example.test"
    )
    with pytest.raises(RuntimeError, match="精确匹配"):
        module._validate_api_base_url("https://other.example.test", settings)


def test_https_proxy_rejects_all_plain_http_password_channels(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "db.sqlite3")
    for unsafe in (
        "http://192.168.3.80:8000",
        "http://127.0.0.1:18000",
        "http://192.168.3.80:8001",
        "http://user@192.168.3.80:8000",
        "http://192.168.3.80:8000/path",
    ):
        with pytest.raises(RuntimeError):
            module._validate_api_base_url(unsafe, settings)


def test_lan_http_configuration_still_cannot_send_passwords_in_plaintext(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path / "db.sqlite3", production=True, transport="lan_http"
    )
    with pytest.raises(RuntimeError, match="明文 HTTP"):
        module._validate_api_base_url("http://192.168.3.80:8000", settings)
    with pytest.raises(RuntimeError):
        module._validate_api_base_url("https://192.168.3.80:8000", settings)


def test_managed_root_verifies_the_shared_database(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    database = root / "shared" / "data" / "carton_erp.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()
    release = "release-a"
    manifest = root / "releases" / release / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "version": "v-test",
                "git_sha": "test-commit",
                "revision": "test-revision",
            }
        ),
        encoding="utf-8",
    )
    (root / "state.json").write_text(
        json.dumps({"current": release}), encoding="utf-8"
    )
    deployment = module._validate_database_identity(
        database,
        settings=_settings(tmp_path / "other.sqlite3"),
        managed_root=root,
    )
    assert deployment == {
        "managed": True,
        "release_id": release,
        "version": "v-test",
        "git_sha": "test-commit",
        "revision": "test-revision",
    }
    with pytest.raises(RuntimeError, match="数据库不一致"):
        module._validate_database_identity(
            tmp_path / "wrong.sqlite3",
            settings=_settings(tmp_path / "other.sqlite3"),
            managed_root=root,
        )


def test_database_preflight_is_read_only_and_checks_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "handoff.sqlite3"
    _create_database(database)
    before = module.sha256_of(database)
    monkeypatch.setattr(module, "open_api", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(module, "request_json", lambda *_args, **_kwargs: _ok_api())

    result = module.preflight_handoff(
        database,
        "https://erp.example.test",
        settings=_settings(database),
    )
    assert result["health"] == {"status": 200, "ok": True}
    assert len(result["target_accounts"]) == 4
    assert module.sha256_of(database) == before


def test_database_preflight_rejects_wrong_account_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "handoff.sqlite3"
    _create_database(database, omit_user="sales")
    monkeypatch.setattr(module, "open_api", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(module, "request_json", lambda *_args, **_kwargs: _ok_api())
    with pytest.raises(RuntimeError, match="四个"):
        module.preflight_handoff(
            database,
            "https://erp.example.test",
            settings=_settings(database),
        )


def test_sqlite_backup_is_readable_and_restorable(tmp_path: Path) -> None:
    database = tmp_path / "handoff.sqlite3"
    backup = tmp_path / "secure" / "handoff-backup.sqlite3"
    _create_database(database)
    module.backup_sqlite(database, backup)
    digest, integrity, restore = module.validate_backup_before_password_write(backup)
    assert len(digest) == 64
    assert integrity == {"integrity_check": "ok", "foreign_key_check_count": 0}
    assert restore["readable"] is True
    assert restore["restore_tested"] is True


def test_corrupt_backup_is_rejected(tmp_path: Path) -> None:
    backup = tmp_path / "corrupt.sqlite3"
    backup.write_bytes(b"not a sqlite database")
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        module.validate_backup_before_password_write(backup)


def test_update_passwords_commits_four_accounts_and_one_audit(tmp_path: Path) -> None:
    database = tmp_path / "handoff.sqlite3"
    _create_database(database)
    before_counts = module.history_counts(database)
    before_versions = {
        row["username"]: row["auth_version"] for row in module.fetch_users(database)
    }
    new_passwords = _passwords("New")

    result = module.update_passwords(
        database, new_passwords, actor_username="admin"
    )

    assert result["committed"] is True
    assert module.history_counts(database) == before_counts
    with sqlite3.connect(database) as connection:
        for username in module.VALID_USERS:
            password_hash, must_change, version = connection.execute(
                "SELECT password_hash, must_change_password, auth_version "
                "FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            assert module.hash_password is not None
            from app.core.security import verify_password

            assert verify_password(new_passwords[username], password_hash)
            assert must_change == 1
            assert version == before_versions[username] + 1
        audits = connection.execute(
            "SELECT COUNT(*) FROM operation_logs "
            "WHERE action = 'FINAL_PASSWORD_HANDOFF'"
        ).fetchone()[0]
        assert audits == 1


def test_history_counts_records_retired_tables_as_absent(tmp_path: Path) -> None:
    database = tmp_path / "current-production-shape.sqlite3"
    _create_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE legacy_ruida_order_items")
        connection.execute("DROP TABLE legacy_ruida_orders")
        connection.commit()

    counts = module.history_counts(database)

    assert counts["sales_orders"] == 1
    assert counts["sales_order_items"] == 1
    assert counts["legacy_ruida_orders"] is None
    assert counts["legacy_ruida_order_items"] is None


def test_update_passwords_rolls_back_all_accounts_on_mid_transaction_error(
    tmp_path: Path,
) -> None:
    database = tmp_path / "handoff.sqlite3"
    _create_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_sales_password_update
            BEFORE UPDATE OF password_hash ON users
            WHEN OLD.username = 'sales'
            BEGIN
                SELECT RAISE(ABORT, 'isolated rollback test');
            END;
            """
        )
        before = connection.execute(
            "SELECT username, password_hash, auth_version FROM users ORDER BY username"
        ).fetchall()
    with pytest.raises(sqlite3.IntegrityError):
        module.update_passwords(
            database, _passwords("New"), actor_username="admin"
        )
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT username, password_hash, auth_version FROM users ORDER BY username"
        ).fetchall()
        assert after == before
        assert connection.execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0


def test_update_passwords_rejects_duplicate_before_database_write(tmp_path: Path) -> None:
    database = tmp_path / "handoff.sqlite3"
    _create_database(database)
    passwords = _passwords("New")
    passwords["sales"] = passwords["finance"]
    before = module.sha256_of(database)
    with pytest.raises(RuntimeError, match="各不相同"):
        module.update_passwords(database, passwords, actor_username="admin")
    assert module.sha256_of(database) == before


def test_json_decoder_rejects_html_without_attempting_page_json_parse() -> None:
    body, error = module._decode_json_body(b"<html></html>", "text/html")
    assert body is None
    assert "expected JSON" in error


def test_non_empty_forged_cookie_probe_is_distinct_from_no_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[dict[str, str] | None] = []
    monkeypatch.setattr(module, "open_api", lambda *_args, **_kwargs: object())

    def fake_json(_opener, _url, **kwargs):
        seen_headers.append(kwargs.get("headers"))
        return module.ApiResult(401, {"detail": "unauthorized"}, "application/json")

    monkeypatch.setattr(module, "request_json", fake_json)
    monkeypatch.setattr(
        module,
        "request_status",
        lambda *_args, **_kwargs: module.ApiResult(200, None, "text/html"),
    )
    result = module._security_status_checks("http://127.0.0.1:8000", "erp_session")
    cookie_headers = [headers for headers in seen_headers if headers and "Cookie" in headers]
    assert len(cookie_headers) == 1
    assert cookie_headers[0]["Cookie"].startswith("erp_session=")
    assert cookie_headers[0]["Cookie"] != "erp_session="
    assert result["forged_cookie_me"]["non_empty_cookie_sent"] is True


def test_verdict_fails_when_any_required_check_fails() -> None:
    good_login = {
        "login_status": 200,
        "me_status": 200,
        "logout_status": 200,
        "after_logout_status": 401,
        "must_change_password": True,
        "session_cookie_received": True,
        "error": None,
    }
    result = {
        "login_checks": {username: dict(good_login) for username in module.VALID_USERS},
        "prechange_sessions": {
            username: {
                "before_login_status": 200,
                "before_me_status": 200,
                "after_write_me_status": 401,
                "error": None,
            }
            for username in module.VALID_USERS
        },
        "old_password_results": {
            username: {"status": 401, "error": None}
            for username in module.VALID_USERS
        },
        "security_status_checks": {
            "no_login_me": {"status": 401},
            "forged_cookie_me": {
                "status": 401,
                "non_empty_cookie_sent": True,
            },
            "incoming_api_without_login": {"status": 401},
        },
        "admin_forced_password_change": {
            "rotation_login_status": 200,
            "before_change_me_status": 200,
            "before_change_required": True,
            "rotation_session_cookie_received": True,
            "change_status": 200,
            "old_session_after_change_status": 401,
            "rotation_password_after_change_status": 401,
            "final_login_status": 200,
            "final_me_status": 200,
            "final_change_required": False,
            "final_session_cookie_received": True,
            "final_logout_status": 200,
            "final_after_logout_status": 401,
            "error": None,
        },
        "history_counts_unchanged": True,
        "integrity_after": {"integrity_check": "ok", "foreign_key_check_count": 0},
        "backup": {
            "integrity": {"integrity_check": "ok", "foreign_key_check_count": 0},
            "restore": {"readable": True, "restore_tested": True},
        },
        "handoff_audit_exists": True,
        "after_users_security": [
            {
                "username": username,
                "role": module.EXPECTED_ROLES[username],
                "active": True,
                "must_change_password": username != "admin",
                "hash_compatible": True,
            }
            for username in module.VALID_USERS
        ],
    }
    assert all(module.evaluate_verification(result).values())
    result["login_checks"]["sales"]["me_status"] = 500
    verdict = module.evaluate_verification(result)
    assert verdict["all_new_logins"] is False
    assert not all(verdict.values())


def test_main_records_not_written_when_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.json"
    database = tmp_path / "db.sqlite3"
    database.touch()
    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: Namespace(
            sqlite_path=str(database),
            api_base_url="https://erp.example.test",
            output_json=str(output),
            backup_dir=None,
            actor="admin",
            managed_root=None,
        ),
    )
    monkeypatch.setattr(module, "load_settings", lambda: _settings(database))
    monkeypatch.setattr(
        module,
        "preflight_handoff",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("preflight")),
    )
    assert module.main() == 2
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["overall_status"] == "failed_without_password_write"
    assert result["write_state"] == "not_written"
    assert result["passwords_recorded"] is False


def test_main_records_not_written_when_settings_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.json"
    database = tmp_path / "db.sqlite3"
    database.touch()
    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: Namespace(
            sqlite_path=str(database),
            api_base_url="https://erp.example.test",
            output_json=str(output),
            backup_dir=None,
            actor="admin",
            managed_root=None,
        ),
    )
    monkeypatch.setattr(
        module,
        "load_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("invalid isolated config")),
    )

    assert module.main() == 2
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["overall_status"] == "failed_without_password_write"
    assert result["write_state"] == "not_written"
    assert result["passwords_recorded"] is False


def test_main_records_committed_when_post_write_verification_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.json"
    database = tmp_path / "db.sqlite3"
    database.touch()
    backup = tmp_path / "backup.sqlite3"
    backup.touch()
    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: Namespace(
            sqlite_path=str(database),
            api_base_url="https://erp.example.test",
            output_json=str(output),
            backup_dir=str(tmp_path / "backups"),
            actor="admin",
            managed_root=None,
        ),
    )
    monkeypatch.setattr(module, "load_settings", lambda: _settings(database))
    monkeypatch.setattr(
        module,
        "preflight_handoff",
        lambda *_args, **_kwargs: {
            "api_base_url": "https://erp.example.test",
            "health": {"status": 200, "ok": True},
        },
    )
    monkeypatch.setattr(module, "history_counts", lambda *_args: {})
    monkeypatch.setattr(module, "backup_sqlite", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "validate_backup_before_password_write",
        lambda *_args: (
            "A" * 64,
            {"integrity_check": "ok", "foreign_key_check_count": 0},
            {"readable": True, "restore_tested": True},
        ),
    )
    monkeypatch.setattr(module, "collect_new_passwords", lambda: _passwords("New"))
    monkeypatch.setattr(
        module,
        "collect_admin_final_password",
        lambda *_args: "IsolatedFinalAdmin9",
    )
    monkeypatch.setattr(
        module,
        "collect_previous_passwords_and_sessions",
        lambda *_args: (_passwords("Old"), {}, {}),
    )
    monkeypatch.setattr(
        module,
        "update_passwords",
        lambda *_args, **_kwargs: {"committed": True, "audit_log_id": 1},
    )
    monkeypatch.setattr(
        module,
        "verify_existing_sessions_revoked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("network")),
    )
    assert module.main() == 2
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["overall_status"] == "committed_verification_failed"
    assert result["write_state"] == "committed"
    assert result["success"] is False
    assert result["passwords_recorded"] is False


def test_real_auth_flow_revokes_old_sessions_and_forces_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.api.auth import router
    from app.api.deps import get_db
    from app.core.config import load_settings
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.user import User

    database = tmp_path / "isolated-auth.sqlite3"
    upload_dir = tmp_path / "uploads"
    backup_dir = tmp_path / "backups"
    old_passwords = _passwords("Old")
    new_passwords = _passwords("New")
    environment = {
        "ERP_ENVIRONMENT": "production",
        "ERP_PRODUCTION_TRANSPORT": "lan_http",
        "ERP_DATABASE_PATH": str(database),
        "ERP_BACKUP_DIR": str(backup_dir),
        "ERP_UPLOAD_DIR": str(upload_dir),
        "ERP_SECRET_KEY": "isolated-password-handoff-test-secret-value",
        "ERP_BIND_HOST": "127.0.0.1",
        "ERP_PORT": "18082",
        "ERP_ALLOWED_ORIGINS": "http://127.0.0.1:18082",
        "ERP_TRUSTED_HOSTS": "127.0.0.1,localhost",
        "ERP_BROWSER_URL": "http://127.0.0.1:18082/",
        "ERP_HEALTH_URL": "http://127.0.0.1:18082/api/health",
        "ERP_SESSION_COOKIE_SECURE": "false",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        for username in module.VALID_USERS:
            session.add(
                User(
                    username=username,
                    password_hash=module.hash_password(old_passwords[username]),
                    role=module.EXPECTED_ROLES[username],
                    real_name=f"Isolated {username}",
                    display_name=f"Isolated {username}",
                    is_active=True,
                    must_change_password=False,
                    customer_access_mode="all" if username == "admin" else "selected",
                )
            )
        session.commit()

    app = FastAPI()
    app.state.erp_settings = settings
    app.include_router(router, prefix="/api/auth")

    def isolated_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = isolated_db
    old_clients: dict[str, TestClient] = {}
    try:
        for username in module.VALID_USERS:
            client = TestClient(app, base_url="http://127.0.0.1:18082")
            response = client.post(
                "/api/auth/login",
                json={"username": username, "password": old_passwords[username]},
            )
            assert response.status_code == 200
            assert client.get("/api/auth/me").status_code == 200
            old_clients[username] = client

        write = module.update_passwords(
            database, new_passwords, actor_username="admin"
        )
        assert write["committed"] is True

        for username, old_client in old_clients.items():
            assert old_client.get("/api/auth/me").status_code == 401
            with TestClient(app, base_url="http://127.0.0.1:18082") as client:
                rejected = client.post(
                    "/api/auth/login",
                    json={"username": username, "password": old_passwords[username]},
                )
                assert rejected.status_code == 401
                accepted = client.post(
                    "/api/auth/login",
                    json={"username": username, "password": new_passwords[username]},
                )
                assert accepted.status_code == 200
                assert accepted.json()["user"]["must_change_password"] is True

        final_password = "IsolatedFinalOwner9"
        with TestClient(app, base_url="http://127.0.0.1:18082") as client:
            assert client.post(
                "/api/auth/login",
                json={"username": "admin", "password": new_passwords["admin"]},
            ).status_code == 200
            changed = client.put(
                "/api/auth/password",
                json={
                    "current_password": new_passwords["admin"],
                    "new_password": final_password,
                },
                headers={"Origin": "http://127.0.0.1:18082"},
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["user"]["must_change_password"] is False
            assert client.get("/api/auth/me").status_code == 401
        with TestClient(app, base_url="http://127.0.0.1:18082") as client:
            assert client.post(
                "/api/auth/login",
                json={
                    "username": "admin",
                    "password": new_passwords["admin"],
                },
            ).status_code == 401
            assert client.post(
                "/api/auth/login",
                json={"username": "admin", "password": final_password},
            ).status_code == 200
            assert client.get("/api/auth/me").json()["user"][
                "must_change_password"
            ] is False
            assert client.post(
                "/api/auth/logout",
                headers={"Origin": "http://127.0.0.1:18082"},
            ).status_code == 200
            assert client.get("/api/auth/me").status_code == 401
    finally:
        for client in old_clients.values():
            client.close()
        engine.dispose()

    assert database.is_file()
    assert upload_dir.parent == tmp_path
    assert backup_dir.parent == tmp_path


def test_pinned_loopback_tls_uses_real_secure_cookie_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import socket
    import threading
    import time

    import uvicorn
    from fastapi import FastAPI
    from sqlalchemy.orm import sessionmaker

    from app.api.auth import router
    from app.api.deps import get_db
    from app.core.config import load_settings
    from app.core.database import create_sqlite_engine
    from app.main import apply_transport_security
    from app.models import Base
    from app.models.user import User

    database = tmp_path / "pinned-loopback.sqlite3"
    public_origin = "https://erp.example.test"
    environment = {
        "ERP_ENVIRONMENT": "production",
        "ERP_PRODUCTION_TRANSPORT": "https_proxy",
        "ERP_DATABASE_PATH": str(database),
        "ERP_BACKUP_DIR": str(tmp_path / "backups"),
        "ERP_UPLOAD_DIR": str(tmp_path / "uploads"),
        "ERP_SECRET_KEY": "isolated-pinned-loopback-test-secret-value",
        "ERP_BIND_HOST": "127.0.0.1",
        "ERP_PORT": "18083",
        "ERP_ALLOWED_ORIGINS": public_origin,
        "ERP_TRUSTED_HOSTS": "erp.example.test",
        "ERP_TRUSTED_PROXY_IPS": "127.0.0.1",
        "ERP_BROWSER_URL": f"{public_origin}/",
        "ERP_HEALTH_URL": f"{public_origin}/api/health",
        "ERP_SESSION_COOKIE_SECURE": "true",
        "HTTPS_PROXY": "http://192.0.2.1:9",
        "HTTP_PROXY": "http://192.0.2.1:9",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    rotation_password = "IsolatedRotationAdmin9"
    final_password = "IsolatedFinalOwner10"
    with factory() as session:
        session.add(
            User(
                username="admin",
                password_hash=module.hash_password(rotation_password),
                role="admin",
                real_name="Isolated admin",
                display_name="Isolated admin",
                is_active=True,
                must_change_password=True,
                customer_access_mode="all",
            )
        )
        session.commit()

    app = FastAPI()
    app.state.erp_settings = settings

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router, prefix="/api/auth")

    def isolated_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = isolated_db
    apply_transport_security(app, settings)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    upstream_port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", lifespan="off")
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        with module.pinned_loopback_tls_proxy(
            public_origin,
            f"http://127.0.0.1:{upstream_port}",
            tmp_path,
        ):
            health_result = module.request_json(
                module.open_api(public_origin),
                f"{public_origin}/api/health",
            )
            assert health_result.status == 200
            assert health_result.body == {"ok": True}
            transport = module._transport_security_summary(public_origin)
            assert transport["mode"] == "pinned_loopback_tls"
            assert transport["environment_proxy_disabled"] is True
            assert transport["redirects_disabled"] is True
            assert transport["loopback_only"] is True

            completed = module.complete_admin_forced_password_change(
                public_origin,
                rotation_password,
                final_password,
            )
            assert completed["change_status"] == 200
            assert completed["old_session_after_change_status"] == 401
            assert completed["rotation_password_after_change_status"] == 401
            assert completed["final_login_status"] == 200
            assert completed["final_me_status"] == 200
            assert completed["final_change_required"] is False
            assert completed["final_logout_status"] == 200
            assert completed["final_after_logout_status"] == 401
            assert completed["error"] is None
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        engine.dispose()

    assert module._ACTIVE_PINNED_TLS is None
    assert not list(tmp_path.glob(".password-handoff-tls-*"))
