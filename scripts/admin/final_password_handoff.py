from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_settings
from app.core.password_policy import normalize_username, password_policy_issues
from app.core.security import hash_password


VALID_USERS = ("admin", "finance", "sales", "workshop")
EXPECTED_ROLES = {
    "admin": "admin",
    "finance": "finance",
    "sales": "sales",
    "workshop": "workshop",
}
HISTORY_COUNT_QUERIES = {
    "sales_orders": "SELECT COUNT(*) FROM sales_orders",
    "sales_order_items": "SELECT COUNT(*) FROM sales_order_items",
    "legacy_ruida_orders": "SELECT COUNT(*) FROM legacy_ruida_orders",
    "legacy_ruida_order_items": "SELECT COUNT(*) FROM legacy_ruida_order_items",
    "ruida_prefixed_orders": (
        "SELECT COUNT(*) FROM sales_orders WHERE order_number LIKE 'RUIDA-%'"
    ),
}


@dataclass(frozen=True, slots=True)
class ApiResult:
    status: int | None
    body: dict[str, Any] | None
    content_type: str | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LoginCheck:
    login_status: int | None
    me_status: int | None
    logout_status: int | None
    after_logout_status: int | None
    must_change_password: bool | None
    session_cookie_received: bool
    error: str | None = None


@dataclass(slots=True)
class ExistingSession:
    opener: urllib.request.OpenerDirector
    login_status: int | None
    me_before_status: int | None
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive, auditable final password handoff"
    )
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--api-base-url")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--backup-dir")
    parser.add_argument("--actor", required=True)
    parser.add_argument(
        "--managed-root",
        help=(
            "Desktop-assistant root. In production this allows the tool to verify "
            "that --sqlite-path is the managed shared database."
        ),
    )
    return parser.parse_args()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _atomic_write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _persist_state(
    output_json: Path,
    result: dict[str, Any],
    *,
    overall_status: str,
    write_state: str,
    verification_state: str,
) -> None:
    result.update(
        {
            "updated_at": _now(),
            "overall_status": overall_status,
            "write_state": write_state,
            "verification_state": verification_state,
        }
    )
    _atomic_write_result(output_json, result)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=15,
    )


def db_integrity(db_path: Path) -> dict[str, Any]:
    with closing(_read_only_connection(db_path)) as connection:
        integrity = connection.execute("PRAGMA integrity_check;").fetchone()[0]
        foreign_key_rows = connection.execute(
            "PRAGMA foreign_key_check;"
        ).fetchall()
    return {
        "integrity_check": integrity,
        "foreign_key_check_count": len(foreign_key_rows),
    }


def history_counts(db_path: Path) -> dict[str, int]:
    with closing(_read_only_connection(db_path)) as connection:
        return {
            name: int(connection.execute(sql).fetchone()[0])
            for name, sql in HISTORY_COUNT_QUERIES.items()
        }


def backup_sqlite(source: Path, backup: Path) -> None:
    if backup.exists():
        raise RuntimeError(f"备份目标已存在，拒绝覆盖：{backup}")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with closing(_read_only_connection(source)) as source_connection, closing(
        sqlite3.connect(backup)
    ) as backup_connection:
        source_connection.backup(backup_connection)


def validate_backup_before_password_write(
    backup: Path,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if not backup.is_file() or backup.stat().st_size <= 0:
        raise RuntimeError("密码交接备份不存在或为空，禁止写入密码")
    backup_integrity = db_integrity(backup)
    if backup_integrity != {
        "integrity_check": "ok",
        "foreign_key_check_count": 0,
    }:
        raise RuntimeError(
            f"密码交接备份完整性校验失败，禁止写入密码：{backup_integrity}"
        )

    fd, restore_name = tempfile.mkstemp(
        prefix="password-handoff-restore-",
        suffix=backup.suffix,
        dir=backup.parent,
    )
    os.close(fd)
    restore_path = Path(restore_name)
    restore_path.unlink(missing_ok=True)
    try:
        with closing(_read_only_connection(backup)) as source_connection, closing(
            sqlite3.connect(restore_path)
        ) as restore_connection:
            source_connection.backup(restore_connection)
        restore_integrity = db_integrity(restore_path)
        if restore_integrity != backup_integrity:
            raise RuntimeError(
                "密码交接备份恢复验证结果与备份完整性不一致，禁止写入密码"
            )
    finally:
        restore_path.unlink(missing_ok=True)

    return (
        sha256_of(backup),
        backup_integrity,
        {
            "readable": True,
            "restore_tested": True,
            "restore_integrity": restore_integrity,
        },
    )


def validate_handoff_password(password: str, username: str) -> list[str]:
    return password_policy_issues(password, username=username)


def _require_secure_interactive_terminal() -> None:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise RuntimeError(
            "当前进程没有可禁止回显的交互终端，拒绝读取任何密码"
        )


def prompt_password(username: str) -> str:
    _require_secure_interactive_terminal()
    while True:
        first = getpass.getpass(f"{username} 新密码: ")
        second = getpass.getpass(f"{username} 确认密码: ")
        if first != second:
            print(f"[{username}] 两次输入不一致，请重试。")
            continue
        issues = validate_handoff_password(first, username)
        if issues:
            print(f"[{username}] 密码不符合要求：")
            for issue in issues:
                print(f"  - {issue}")
            continue
        return first


def collect_new_passwords() -> dict[str, str]:
    passwords: dict[str, str] = {}
    for username in VALID_USERS:
        while True:
            candidate = prompt_password(username)
            if any(
                hmac.compare_digest(candidate, existing)
                for existing in passwords.values()
            ):
                print(f"[{username}] 四个账号必须使用各不相同的新密码，请重试。")
                continue
            passwords[username] = candidate
            break
    return passwords


def prompt_previous_password(username: str) -> str:
    _require_secure_interactive_terminal()
    password = getpass.getpass(f"{username} 旧密码（仅用于失效验证）: ")
    if not password:
        raise RuntimeError(f"[{username}] 旧密码不能为空")
    return password


def fetch_users(db_path: Path) -> list[dict[str, Any]]:
    with closing(_read_only_connection(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, username, role, is_active, must_change_password,
                   password_hash, auth_version
            FROM users
            WHERE username IN ('admin','finance','sales','workshop')
            ORDER BY username
            """
        ).fetchall()
    return [dict(row) for row in rows]


def validate_handoff_actor(
    connection: sqlite3.Connection,
    actor_username: str,
) -> tuple[int, str, str]:
    try:
        normalized_actor = normalize_username(actor_username)
    except (TypeError, ValueError) as error:
        raise RuntimeError("--actor 必须是可识别的启用管理员账号") from error
    row = connection.execute(
        """
        SELECT id, username, role, is_active
        FROM users
        WHERE username = ?
        """,
        (normalized_actor,),
    ).fetchone()
    if row is None or row[2] != "admin" or not bool(row[3]):
        raise RuntimeError("--actor 必须是可识别的启用管理员账号")
    return int(row[0]), str(row[1]), str(row[2])


def _validate_user_table(db_path: Path) -> list[dict[str, Any]]:
    required_columns = {
        "id", "username", "role", "is_active", "must_change_password",
        "password_hash", "auth_version", "updated_at",
    }
    required_audit_columns = {
        "id", "user_id", "action", "resource", "details", "username",
        "role", "entity_type", "description",
    }
    with closing(_read_only_connection(db_path)) as connection:
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        audit_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(operation_logs)")
        }
    missing_user_columns = sorted(required_columns - user_columns)
    if missing_user_columns:
        raise RuntimeError(
            f"users 表缺少密码交接字段：{', '.join(missing_user_columns)}"
        )
    missing_audit_columns = sorted(required_audit_columns - audit_columns)
    if missing_audit_columns:
        raise RuntimeError(
            f"operation_logs 表缺少审计字段：{', '.join(missing_audit_columns)}"
        )

    users = fetch_users(db_path)
    if {row["username"] for row in users} != set(VALID_USERS):
        raise RuntimeError("目标数据库未精确包含四个密码交接账号")
    for row in users:
        username = str(row["username"])
        if row["role"] != EXPECTED_ROLES[username] or not bool(row["is_active"]):
            raise RuntimeError(f"目标账号角色或启用状态异常：{username}")
        if not str(row["password_hash"]).startswith(("$2a$", "$2b$", "$2y$")):
            raise RuntimeError(f"目标账号密码哈希格式不兼容：{username}")
        if isinstance(row["auth_version"], bool) or int(row["auth_version"]) < 1:
            raise RuntimeError(f"目标账号 auth_version 异常：{username}")
    return users


def _resolve_managed_database(managed_root: Path) -> tuple[Path, dict[str, Any]]:
    root = managed_root.resolve()
    state_path = root / "state.json"
    if not state_path.is_file():
        raise RuntimeError(f"托管 ERP state.json 不存在：{state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    release_id = str(state.get("current") or "").strip()
    if not release_id:
        raise RuntimeError("托管 ERP state.json 缺少 current release")
    manifest_path = root / "releases" / release_id / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"当前 release manifest 不存在：{manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_database = (root / "shared" / "data" / "carton_erp.sqlite3").resolve()
    return expected_database, {
        "managed": True,
        "release_id": release_id,
        "version": manifest.get("version"),
        "git_sha": manifest.get("git_sha"),
        "revision": manifest.get("revision"),
    }


def _validate_database_identity(
    db_path: Path,
    *,
    settings: Any,
    managed_root: Path | None,
) -> dict[str, Any]:
    resolved = db_path.resolve()
    if managed_root is not None:
        expected, deployment = _resolve_managed_database(managed_root)
    else:
        expected = Path(settings.database_path).resolve()
        deployment = {"managed": False}
    if resolved != expected:
        raise RuntimeError(
            "--sqlite-path 与当前运行配置确认的数据库不一致；拒绝写入"
        )
    return deployment


def update_passwords(
    db_path: Path,
    passwords: dict[str, str],
    *,
    actor_username: str,
) -> dict[str, Any]:
    if set(passwords) != set(VALID_USERS):
        raise RuntimeError("密码交接必须一次且仅覆盖四个指定账号")
    if len(set(passwords.values())) != len(VALID_USERS):
        raise RuntimeError("四个账号必须使用各不相同的新密码")

    password_hashes = {
        username: hash_password(password) for username, password in passwords.items()
    }
    with closing(sqlite3.connect(db_path, timeout=15)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            actor_id, actor_name, actor_role = validate_handoff_actor(
                connection, actor_username
            )
            rows = connection.execute(
                """
                SELECT username, role, is_active, auth_version
                FROM users
                WHERE username IN ('admin','finance','sales','workshop')
                """
            ).fetchall()
            current = {str(row[0]): row for row in rows}
            if set(current) != set(VALID_USERS):
                raise RuntimeError("密码交接账号集合在写入前发生变化")

            version_changes: dict[str, dict[str, int]] = {}
            for username in VALID_USERS:
                row = current[username]
                if row[1] != EXPECTED_ROLES[username] or not bool(row[2]):
                    raise RuntimeError(f"目标账号角色或启用状态异常：{username}")
                before_version = int(row[3])
                cursor = connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, must_change_password = 1,
                        auth_version = auth_version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE username = ? AND auth_version = ?
                    """,
                    (password_hashes[username], username, before_version),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"密码交接账号并发变化，已回滚：{username}")
                version_changes[username] = {
                    "before": before_version,
                    "after": before_version + 1,
                }

            audit_cursor = connection.execute(
                """
                INSERT INTO operation_logs (
                    user_id, action, resource, details, username,
                    role, entity_type, description
                ) VALUES (
                    ?, 'FINAL_PASSWORD_HANDOFF', 'User', ?,
                    ?, ?, 'user', '最终密码交接'
                )
                """,
                (
                    actor_id,
                    json.dumps(
                        {
                            "target_usernames": list(VALID_USERS),
                            "auth_version_changes": version_changes,
                            "must_change_password": True,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    actor_name,
                    actor_role,
                ),
            )
            audit_log_id = int(audit_cursor.lastrowid)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return {
        "committed": True,
        "audit_log_id": audit_log_id,
        "auth_version_changes": version_changes,
    }


def open_api(
    base_url: str,
    cookie_jar: CookieJar | None = None,
) -> urllib.request.OpenerDirector:
    del base_url
    handlers: list[Any] = [urllib.request.ProxyHandler({})]
    if cookie_jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    return urllib.request.build_opener(*handlers)


def _decode_json_body(
    raw: bytes,
    content_type: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not raw:
        return None, None
    normalized = (content_type or "").lower()
    if "json" not in normalized:
        return None, f"expected JSON response, got {content_type or 'unknown'}"
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, _safe_error(error)
    if not isinstance(decoded, dict):
        return None, "expected a JSON object response"
    return decoded, None


def request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> ApiResult:
    data = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, method=method, headers=request_headers
    )
    try:
        with opener.open(request, timeout=15) as response:
            content_type = response.headers.get_content_type()
            body, decode_error = _decode_json_body(response.read(), content_type)
            return ApiResult(response.status, body, content_type, decode_error)
    except urllib.error.HTTPError as error:
        content_type = error.headers.get_content_type()
        body, decode_error = _decode_json_body(error.read(), content_type)
        return ApiResult(error.code, body, content_type, decode_error)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return ApiResult(None, None, None, _safe_error(error))


def request_status(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> ApiResult:
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with opener.open(request, timeout=15) as response:
            response.read()
            return ApiResult(
                response.status, None, response.headers.get_content_type()
            )
    except urllib.error.HTTPError as error:
        error.read()
        return ApiResult(error.code, None, error.headers.get_content_type())
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return ApiResult(None, None, None, _safe_error(error))


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _validate_api_base_url(api_base_url: str, settings: Any) -> str:
    normalized = api_base_url.rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError("API URL 必须是无路径、无凭据的 HTTP/HTTPS 根地址")

    if not settings.is_production:
        return normalized

    configured_https_origins = {
        _origin(value)
        for value in (
            list(getattr(settings, "allowed_origins", ()))
            + [getattr(settings, "browser_url", "")]
        )
        if value and urlsplit(value).scheme == "https"
    }
    configured_private_http_origins = {
        _origin(value)
        for value in getattr(settings, "private_http_origins", ())
        if value
    }
    if getattr(settings, "production_transport", "") == "lan_http":
        configured_private_http_origins.update(
            _origin(value)
            for value in getattr(settings, "allowed_origins", ())
            if value and urlsplit(value).scheme == "http"
        )

    origin = _origin(normalized)
    if parsed.scheme == "https" and origin in configured_https_origins:
        return normalized
    if parsed.scheme == "http" and origin in configured_private_http_origins:
        return normalized
    raise RuntimeError(
        "生产 API 地址必须精确匹配已配置 HTTPS 入口或受控局域网 HTTP 入口"
    )


def preflight_handoff(
    db_path: Path,
    api_base_url: str,
    *,
    settings: Any,
    managed_root: Path | None = None,
) -> dict[str, Any]:
    normalized_api = _validate_api_base_url(api_base_url, settings)
    if not db_path.is_file():
        raise RuntimeError(f"数据库不存在：{db_path}")
    deployment = _validate_database_identity(
        db_path, settings=settings, managed_root=managed_root
    )
    integrity = db_integrity(db_path)
    if integrity != {"integrity_check": "ok", "foreign_key_check_count": 0}:
        raise RuntimeError(f"数据库预检失败：{integrity}")
    users = _validate_user_table(db_path)

    with closing(sqlite3.connect(db_path, timeout=15)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("SELECT COUNT(*) FROM users").fetchone()
        connection.rollback()

    health = request_json(
        open_api(normalized_api), f"{normalized_api}/api/health"
    )
    if (
        health.status != 200
        or health.error is not None
        or not health.body
        or health.body.get("ok") is not True
    ):
        raise RuntimeError(
            "API 健康预检失败："
            f"status={health.status}, error={health.error or 'none'}"
        )
    return {
        "api_base_url": normalized_api,
        "health": {"status": health.status, "ok": True},
        "integrity": integrity,
        "target_accounts": [
            {
                "username": row["username"],
                "role": row["role"],
                "active": bool(row["is_active"]),
                "hash_compatible": True,
            }
            for row in users
        ],
        "deployment": deployment,
    }


def _login(
    base_url: str,
    opener: urllib.request.OpenerDirector,
    username: str,
    password: str,
) -> ApiResult:
    return request_json(
        opener,
        f"{base_url}/api/auth/login",
        method="POST",
        payload={
            "username": username,
            "password": password,
            "remember_me": False,
        },
    )


def collect_previous_passwords_and_sessions(
    base_url: str,
) -> tuple[dict[str, str], dict[str, ExistingSession], dict[str, Any]]:
    passwords: dict[str, str] = {}
    sessions: dict[str, ExistingSession] = {}
    summary: dict[str, Any] = {}
    for username in VALID_USERS:
        password = prompt_previous_password(username)
        jar = CookieJar()
        opener = open_api(base_url, jar)
        login = _login(base_url, opener, username, password)
        me = request_json(opener, f"{base_url}/api/auth/me")
        error = login.error or me.error
        session = ExistingSession(opener, login.status, me.status, error)
        summary[username] = {
            "login_status": login.status,
            "me_before_status": me.status,
            "session_cookie_received": bool(list(jar)),
            "error": error,
        }
        if login.status != 200 or me.status != 200 or error is not None or not list(jar):
            raise RuntimeError(
                f"[{username}] 旧密码或改密前会话预检失败；数据库尚未写入"
            )
        passwords[username] = password
        sessions[username] = session
    return passwords, sessions, summary


def verify_existing_sessions_revoked(
    base_url: str,
    sessions: dict[str, ExistingSession],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for username, session in sessions.items():
        after = request_json(session.opener, f"{base_url}/api/auth/me")
        result[username] = {
            "before_login_status": session.login_status,
            "before_me_status": session.me_before_status,
            "after_write_me_status": after.status,
            "error": after.error,
        }
    return result


def verify_login(
    base_url: str,
    username: str,
    password: str,
) -> LoginCheck:
    jar = CookieJar()
    opener = open_api(base_url, jar)
    login = _login(base_url, opener, username, password)
    if login.status != 200 or login.error is not None:
        return LoginCheck(
            login.status, None, None, None, None, bool(list(jar)), login.error
        )
    me = request_json(opener, f"{base_url}/api/auth/me")
    origin = _origin(base_url)
    logout = request_json(
        opener,
        f"{base_url}/api/auth/logout",
        method="POST",
        payload={},
        headers={"Origin": origin},
    )
    after_logout = request_json(opener, f"{base_url}/api/auth/me")
    user_payload = (me.body or {}).get("user") if me.body else None
    return LoginCheck(
        login_status=login.status,
        me_status=me.status,
        logout_status=logout.status,
        after_logout_status=after_logout.status,
        must_change_password=(user_payload or {}).get("must_change_password"),
        session_cookie_received=bool(list(jar)),
        error=login.error or me.error or logout.error or after_logout.error,
    )


def verify_old_passwords(
    base_url: str,
    previous_passwords: dict[str, str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for username in VALID_USERS:
        attempt = _login(
            base_url, open_api(base_url), username, previous_passwords[username]
        )
        result[username] = {"status": attempt.status, "error": attempt.error}

    control_password = secrets.token_urlsafe(32)
    control = _login(
        base_url, open_api(base_url), "admin", control_password
    )
    result["generated_wrong_password_control"] = {
        "status": control.status,
        "error": control.error,
    }
    return result


def _security_status_checks(base_url: str, cookie_name: str) -> dict[str, Any]:
    no_login = request_json(open_api(base_url), f"{base_url}/api/auth/me")
    forged = request_json(
        open_api(base_url),
        f"{base_url}/api/auth/me",
        headers={"Cookie": f"{cookie_name}=invalid-non-empty-session"},
    )
    incoming = request_json(
        open_api(base_url), f"{base_url}/api/incoming/pending"
    )
    incoming_html = request_status(
        open_api(base_url),
        f"{base_url}/incoming.html",
        headers={"Accept": "text/html"},
    )
    return {
        "no_login_me": {"status": no_login.status, "error": no_login.error},
        "forged_cookie_me": {
            "status": forged.status,
            "non_empty_cookie_sent": True,
            "error": forged.error,
        },
        "incoming_api_without_login": {
            "status": incoming.status,
            "error": incoming.error,
        },
        "incoming_html": {
            "status": incoming_html.status,
            "content_type": incoming_html.content_type,
            "error": incoming_html.error,
        },
    }


def _audit_log_exists(db_path: Path, audit_log_id: int) -> bool:
    with closing(_read_only_connection(db_path)) as connection:
        row = connection.execute(
            "SELECT action FROM operation_logs WHERE id = ?", (audit_log_id,)
        ).fetchone()
    return row is not None and row[0] == "FINAL_PASSWORD_HANDOFF"


def _sanitized_user_security(db_path: Path) -> list[dict[str, Any]]:
    return [
        {
            "username": row["username"],
            "role": row["role"],
            "active": bool(row["is_active"]),
            "must_change_password": bool(row["must_change_password"]),
            "hash_compatible": str(row["password_hash"]).startswith(
                ("$2a$", "$2b$", "$2y$")
            ),
            "auth_version": int(row["auth_version"]),
        }
        for row in fetch_users(db_path)
    ]


def evaluate_verification(result: dict[str, Any]) -> dict[str, bool]:
    login_checks = result.get("login_checks", {})
    existing_sessions = result.get("prechange_sessions", {})
    old_passwords = result.get("old_password_results", {})
    security = result.get("security_status_checks", {})
    users = result.get("after_users_security", [])

    checks = {
        "all_new_logins": all(
            login_checks.get(username, {}).get("login_status") == 200
            and login_checks.get(username, {}).get("me_status") == 200
            and login_checks.get(username, {}).get("logout_status") == 200
            and login_checks.get(username, {}).get("after_logout_status") == 401
            and login_checks.get(username, {}).get("must_change_password") is True
            and login_checks.get(username, {}).get("session_cookie_received") is True
            and login_checks.get(username, {}).get("error") is None
            for username in VALID_USERS
        ),
        "all_prechange_sessions_revoked": all(
            existing_sessions.get(username, {}).get("before_login_status") == 200
            and existing_sessions.get(username, {}).get("before_me_status") == 200
            and existing_sessions.get(username, {}).get("after_write_me_status") == 401
            and existing_sessions.get(username, {}).get("error") is None
            for username in VALID_USERS
        ),
        "all_old_passwords_rejected": all(
            old_passwords.get(username, {}).get("status") == 401
            and old_passwords.get(username, {}).get("error") is None
            for username in VALID_USERS
        ),
        "wrong_password_control_rejected": (
            old_passwords.get("generated_wrong_password_control", {}).get("status")
            == 401
            and old_passwords.get(
                "generated_wrong_password_control", {}
            ).get("error") is None
        ),
        "anonymous_and_forged_sessions_rejected": (
            security.get("no_login_me", {}).get("status") == 401
            and security.get("forged_cookie_me", {}).get("status") == 401
            and security.get("forged_cookie_me", {}).get("non_empty_cookie_sent")
            is True
            and security.get("incoming_api_without_login", {}).get("status") == 401
        ),
        "history_counts_unchanged": result.get("history_counts_unchanged") is True,
        "integrity_ok": result.get("integrity_after")
        == {"integrity_check": "ok", "foreign_key_check_count": 0},
        "backup_verified_and_restorable": (
            result.get("backup", {}).get("integrity")
            == {"integrity_check": "ok", "foreign_key_check_count": 0}
            and result.get("backup", {}).get("restore", {}).get("readable") is True
            and result.get("backup", {}).get("restore", {}).get("restore_tested")
            is True
        ),
        "handoff_audit_committed": result.get("handoff_audit_exists") is True,
        "four_expected_accounts_secured": (
            len(users) == len(VALID_USERS)
            and {row.get("username") for row in users} == set(VALID_USERS)
            and all(
                row.get("role") == EXPECTED_ROLES[row.get("username")]
                and row.get("active") is True
                and row.get("must_change_password") is True
                and row.get("hash_compatible") is True
                for row in users
            )
        ),
    }
    return checks


def _scrub_password_mapping(passwords: dict[str, str] | None) -> None:
    if passwords is None:
        return
    for username in list(passwords):
        passwords[username] = ""
    passwords.clear()


def main() -> int:
    args = parse_args()
    db_path = Path(args.sqlite_path).resolve()
    output_json = Path(args.output_json).resolve()
    backup_dir = (
        Path(args.backup_dir).resolve()
        if args.backup_dir
        else output_json.parent / "password-rotation-backups"
    )
    managed_root = Path(args.managed_root).resolve() if args.managed_root else None
    result: dict[str, Any] = {
        "schema_version": 2,
        "started_at": _now(),
        "database": str(db_path),
        "output_json": str(output_json),
        "target_usernames": list(VALID_USERS),
        "passwords_recorded": False,
    }
    new_passwords: dict[str, str] | None = None
    previous_passwords: dict[str, str] | None = None
    committed = False
    _persist_state(
        output_json,
        result,
        overall_status="preflight_started",
        write_state="not_written",
        verification_state="not_started",
    )
    try:
        settings = load_settings()
        api_base_url = (
            args.api_base_url or getattr(settings, "browser_url", "")
        ).strip().rstrip("/")
        if not api_base_url:
            raise RuntimeError("必须显式提供 API 根地址或配置 ERP_BROWSER_URL")
        preflight = preflight_handoff(
            db_path,
            api_base_url,
            settings=settings,
            managed_root=managed_root,
        )
        api_base_url = preflight["api_base_url"]
        result["preflight"] = preflight
        counts_before = history_counts(db_path)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = (
            backup_dir
            / f"{db_path.stem}_before_final_password_handoff_{timestamp}{db_path.suffix}"
        )
        backup_sqlite(db_path, backup_path)
        backup_sha, backup_integrity, restore_validation = (
            validate_backup_before_password_write(backup_path)
        )
        result["backup"] = {
            "path": str(backup_path),
            "sha256": backup_sha,
            "integrity": backup_integrity,
            "restore": restore_validation,
        }
        _persist_state(
            output_json,
            result,
            overall_status="backup_verified",
            write_state="not_written",
            verification_state="not_started",
        )

        new_passwords = collect_new_passwords()
        previous_passwords, existing_sessions, existing_session_summary = (
            collect_previous_passwords_and_sessions(api_base_url)
        )
        result["prechange_session_setup"] = existing_session_summary
        _persist_state(
            output_json,
            result,
            overall_status="credentials_collected_in_memory",
            write_state="not_written",
            verification_state="not_started",
        )

        write_result = update_passwords(
            db_path, new_passwords, actor_username=args.actor
        )
        committed = True
        result["write"] = write_result
        _persist_state(
            output_json,
            result,
            overall_status="passwords_committed_verification_pending",
            write_state="committed",
            verification_state="pending",
        )

        result["prechange_sessions"] = verify_existing_sessions_revoked(
            api_base_url, existing_sessions
        )
        result["login_checks"] = {
            username: asdict(
                verify_login(api_base_url, username, new_passwords[username])
            )
            for username in VALID_USERS
        }
        result["old_password_results"] = verify_old_passwords(
            api_base_url, previous_passwords
        )
        result["security_status_checks"] = _security_status_checks(
            api_base_url, getattr(settings, "session_cookie_name", "erp_session")
        )
        result["history_counts_unchanged"] = counts_before == history_counts(db_path)
        result["integrity_after"] = db_integrity(db_path)
        result["handoff_audit_exists"] = _audit_log_exists(
            db_path, int(write_result["audit_log_id"])
        )
        result["after_users_security"] = _sanitized_user_security(db_path)
        verdict = evaluate_verification(result)
        result["verification_checks"] = verdict
        success = all(verdict.values())
        result["success"] = success
        result["completed_at"] = _now()
        _persist_state(
            output_json,
            result,
            overall_status=(
                "verified_success" if success else "committed_verification_failed"
            ),
            write_state="committed",
            verification_state="complete" if success else "failed",
        )
        print(
            "密码交接状态："
            + ("验证完成" if success else "已提交但验证未通过")
            + f"；脱敏结果：{output_json}"
        )
        return 0 if success else 2
    except BaseException as error:
        result["success"] = False
        result["error"] = _safe_error(error)
        result["failed_at"] = _now()
        _persist_state(
            output_json,
            result,
            overall_status=(
                "committed_verification_failed"
                if committed else "failed_without_password_write"
            ),
            write_state="committed" if committed else "not_written",
            verification_state="failed" if committed else "not_started",
        )
        print(
            "密码交接失败："
            + ("密码已提交，禁止自动重跑或恢复旧口令" if committed else "未写入密码")
            + f"；脱敏结果：{output_json}"
        )
        return 2
    finally:
        _scrub_password_mapping(new_passwords)
        _scrub_password_mapping(previous_passwords)


if __name__ == "__main__":
    raise SystemExit(main())
