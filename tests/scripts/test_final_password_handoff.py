from pathlib import Path
import importlib.util
import sqlite3
import sys
from argparse import Namespace
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "admin" / "final_password_handoff.py"
spec = importlib.util.spec_from_file_location("final_password_handoff", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_validate_handoff_password_rejects_weak_and_short() -> None:
    issues = module.validate_handoff_password("admin", "admin")
    assert issues
    assert "至少 10 位" in issues
    assert "不能使用弱密码" in issues


def test_validate_handoff_password_requires_letters_and_digits() -> None:
    assert "至少包含 1 个数字" in module.validate_handoff_password(
        "abcdefgh", "sales"
    )
    assert "至少包含 1 个字母" in module.validate_handoff_password(
        "87654321", "sales"
    )


def test_validate_handoff_password_uses_account_specific_minimums() -> None:
    assert module.validate_handoff_password("safe1234", "finance") == []
    assert "至少 10 位" in module.validate_handoff_password("safe12345", "admin")
    assert module.validate_handoff_password("safe123456", "admin") == []


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


def test_production_preflight_rejects_http_before_database_access(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="HTTPS"):
        module.preflight_handoff(
            tmp_path / "missing.sqlite3",
            "http://127.0.0.1:8000",
            production=True,
        )


def test_handoff_preflight_completes_before_any_password_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: Namespace(
            sqlite_path=str(tmp_path / "users.sqlite3"),
            api_base_url="https://erp.example.com",
            output_json=str(tmp_path / "result.json"),
            actor="admin",
        ),
    )
    monkeypatch.setattr(
        module,
        "load_settings",
        lambda: SimpleNamespace(is_production=True, port=8000),
    )

    def fail_preflight(*_args, **_kwargs):
        calls.append("preflight")
        raise RuntimeError("preflight failed")

    def forbidden_update(*_args, **_kwargs):
        calls.append("password-write")

    monkeypatch.setattr(module, "preflight_handoff", fail_preflight)
    monkeypatch.setattr(module, "update_passwords", forbidden_update)

    with pytest.raises(RuntimeError, match="preflight failed"):
        module.main()
    assert calls == ["preflight"]
    assert not (tmp_path / "result.json").exists()


def test_handoff_database_and_https_api_preflight_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "handoff.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                is_active INTEGER NOT NULL,
                must_change_password INTEGER NOT NULL,
                password_hash TEXT NOT NULL,
                auth_version INTEGER NOT NULL
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO users (
                username, role, is_active, must_change_password,
                password_hash, auth_version
            ) VALUES (?, ?, 1, 1, 'hash', 1)
            """,
            [(name, name) for name in module.VALID_USERS],
        )
    before = module.sha256_of(database_path)
    monkeypatch.setattr(module, "open_api", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        module,
        "request_json",
        lambda *_args, **_kwargs: (200, {"ok": True}),
    )

    assert module.preflight_handoff(
        database_path,
        "https://erp.example.com",
        production=True,
    ) == (200, {"ok": True})
    assert module.sha256_of(database_path) == before


def test_verify_login_sends_same_origin_on_cookie_logout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, str] | None]] = []

    monkeypatch.setattr(module, "open_api", lambda *_args, **_kwargs: object())

    def fake_request_json(
        _opener,
        url,
        *,
        method="GET",
        payload=None,
        headers=None,
    ):
        del payload
        calls.append((url, method, headers))
        if url.endswith("/login"):
            return 200, {"user": {"must_change_password": False}}
        if url.endswith("/me"):
            return 200, {"user": {"must_change_password": False}}
        return 200, {"ok": True}

    monkeypatch.setattr(module, "request_json", fake_request_json)

    result = module.verify_login(
        "https://erp.example.com",
        "admin",
        "StrongPass!2026",
    )

    logout_calls = [call for call in calls if call[0].endswith("/logout")]
    assert result.logout_status == 200
    assert logout_calls == [
        (
            "https://erp.example.com/api/auth/logout",
            "POST",
            {"Origin": "https://erp.example.com"},
        )
    ]


def test_verify_old_passwords_prompts_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompted_users: list[str] = []
    submitted_passwords: list[str] = []

    def fake_getpass(prompt: str) -> str:
        username = prompt.split(" ", 1)[0]
        prompted_users.append(username)
        return f"runtime-only-{username}"

    def fake_request_json(_opener, _url, **kwargs):
        submitted_passwords.append(kwargs["payload"]["password"])
        return 401, None

    monkeypatch.setattr(module.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _size: "generated-control")
    monkeypatch.setattr(module, "open_api", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(module, "request_json", fake_request_json)

    result = module.verify_old_passwords("https://example.test")

    assert prompted_users == ["admin", "workshop", "finance", "sales"]
    assert submitted_passwords == [
        "runtime-only-admin",
        "runtime-only-workshop",
        "runtime-only-finance",
        "runtime-only-sales",
        "generated-control",
    ]
    assert set(result.values()) == {401}


def test_handoff_rejects_corrupt_backup_before_password_write(tmp_path: Path) -> None:
    backup_path = tmp_path / "corrupt.sqlite3"
    backup_path.write_bytes(b"not a sqlite database")

    with pytest.raises((RuntimeError, sqlite3.DatabaseError), match="备份|database"):
        module.validate_backup_before_password_write(backup_path)


def test_handoff_validates_backup_before_prompt_or_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: Namespace(
            sqlite_path=str(tmp_path / "users.sqlite3"),
            api_base_url="https://erp.example.com",
            output_json=str(tmp_path / "result.json"),
            actor="admin",
        ),
    )
    monkeypatch.setattr(
        module,
        "load_settings",
        lambda: SimpleNamespace(is_production=True, port=8000),
    )
    monkeypatch.setattr(
        module,
        "preflight_handoff",
        lambda *_args, **_kwargs: (200, {"ok": True}),
    )
    monkeypatch.setattr(
        module,
        "fetch_users",
        lambda _path: [
            {"username": username, "role": username}
            for username in module.VALID_USERS
        ],
    )
    monkeypatch.setattr(module, "sha256_of", lambda _path: "A" * 64)
    monkeypatch.setattr(
        module,
        "db_integrity",
        lambda _path: {"integrity_check": "ok", "foreign_key_check_count": 0},
    )
    monkeypatch.setattr(module, "db_scalar", lambda *_args, **_kwargs: 0)

    def backup(_source, _destination):
        calls.append("backup")

    def reject_backup(_backup):
        calls.append("validate-backup")
        raise RuntimeError("backup validation failed")

    def forbidden_prompt(_username):
        calls.append("prompt")
        raise AssertionError("password prompt ran before backup validation")

    def forbidden_update(*_args, **_kwargs):
        calls.append("password-write")
        raise AssertionError("password write ran before backup validation")

    monkeypatch.setattr(module, "backup_sqlite", backup)
    monkeypatch.setattr(module, "validate_backup_before_password_write", reject_backup)
    monkeypatch.setattr(module, "prompt_password", forbidden_prompt)
    monkeypatch.setattr(module, "update_passwords", forbidden_update)

    with pytest.raises(RuntimeError, match="backup validation failed"):
        module.main()
    assert calls == ["backup", "validate-backup"]
