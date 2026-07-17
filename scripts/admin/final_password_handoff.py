from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.backup_retention import auto_cleanup_regular_backups
from app.core.config import load_settings
from app.core.password_policy import normalize_username, password_policy_issues
from app.core.security import hash_password


VALID_USERS = ("admin", "finance", "sales", "workshop")
@dataclass
class LoginCheck:
    login_status: int
    me_status: int | None
    logout_status: int | None
    after_logout_status: int | None
    must_change_password: bool | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive final password handoff")
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--api-base-url")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--actor", required=True)
    return parser.parse_args()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def db_scalar(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def db_integrity(db_path: Path) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        fk_rows = conn.execute("PRAGMA foreign_key_check;").fetchall()
    return {"integrity_check": integrity, "foreign_key_check_count": len(fk_rows)}


def backup_sqlite(source: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    auto_cleanup_regular_backups(backup_dir=backup.parent, keep=5)


def validate_backup_before_password_write(
    backup: Path,
) -> tuple[str, dict[str, Any]]:
    if not backup.is_file() or backup.stat().st_size <= 0:
        raise RuntimeError("密码交接备份不存在或为空，禁止写入密码")
    integrity = db_integrity(backup)
    if integrity != {"integrity_check": "ok", "foreign_key_check_count": 0}:
        raise RuntimeError(f"密码交接备份完整性校验失败，禁止写入密码：{integrity}")
    return sha256_of(backup), integrity


def prompt_password(username: str) -> str:
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


def validate_handoff_password(password: str, username: str) -> list[str]:
    return password_policy_issues(password, username=username)


def fetch_users(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT username, role, is_active, must_change_password, password_hash
            FROM users
            WHERE username IN ('admin','finance','sales','workshop')
            ORDER BY username
            """
        ).fetchall()
    return [dict(r) for r in rows]


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


def update_passwords(
    db_path: Path,
    passwords: dict[str, str],
    *,
    actor_username: str,
) -> None:
    password_hashes = {
        username: hash_password(password) for username, password in passwords.items()
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor_id, actor_name, actor_role = validate_handoff_actor(
            conn,
            actor_username,
        )
        for username, password_hash in password_hashes.items():
            cursor = conn.execute(
                """
                UPDATE users
                SET password_hash = ?,
                    must_change_password = 1,
                    auth_version = auth_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE username = ?
                """,
                (password_hash, username),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"密码交接目标账号不存在：{username}")
        conn.execute(
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
                    {"target_usernames": sorted(passwords)},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                actor_name,
                actor_role,
            ),
        )
        conn.commit()


def open_api(base_url: str, cookie_jar: CookieJar | None = None) -> urllib.request.OpenerDirector:
    handlers: list[Any] = []
    if cookie_jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    return urllib.request.build_opener(*handlers)


def request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with opener.open(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        body = json.loads(raw.decode("utf-8")) if raw else None
        return exc.code, body


def verify_login(base_url: str, username: str, password: str) -> LoginCheck:
    jar = CookieJar()
    opener = open_api(base_url, jar)
    login_status, login_body = request_json(
        opener,
        f"{base_url}/api/auth/login",
        method="POST",
        payload={"username": username, "password": password, "remember_me": False},
    )
    if login_status != 200:
        return LoginCheck(login_status, None, None, None, None)
    me_status, me_body = request_json(opener, f"{base_url}/api/auth/me")
    logout_status, _ = request_json(opener, f"{base_url}/api/auth/logout", method="POST", payload={})
    after_logout_status, _ = request_json(opener, f"{base_url}/api/auth/me")
    user_payload = (me_body or {}).get("user") if me_body else None
    return LoginCheck(
        login_status=login_status,
        me_status=me_status,
        logout_status=logout_status,
        after_logout_status=after_logout_status,
        must_change_password=(user_payload or {}).get("must_change_password"),
    )


def role_matrix(base_url: str, username: str, password: str) -> dict[str, int]:
    jar = CookieJar()
    opener = open_api(base_url, jar)
    request_json(
        opener,
        f"{base_url}/api/auth/login",
        method="POST",
        payload={"username": username, "password": password, "remember_me": False},
    )
    checks = {
        "system_backups": "/api/system/backups",
        "orders": "/api/orders?page=1&page_size=5",
        "finance": "/api/finance/statements?page=1&page_size=5",
        "incoming": "/api/incoming/pending",
        "products": "/api/master/products?page=1&page_size=5",
        "customers": "/api/customers?page=1&page_size=5",
    }
    return {
        key: request_json(opener, f"{base_url}{path}")[0]
        for key, path in checks.items()
    }


def verify_old_passwords(base_url: str) -> dict[str, int]:
    attempts = {
        "admin_old_admin": ("admin", "admin"),
        "workshop_old_123456": ("workshop", "123456"),
        "finance_old_123456": ("finance", "123456"),
        "sales_old_123456": ("sales", "123456"),
        "generic_wrong_password": ("admin", "WrongPass!2026"),
    }
    result: dict[str, int] = {}
    for key, (username, password) in attempts.items():
        opener = open_api(base_url)
        status, _ = request_json(
            opener,
            f"{base_url}/api/auth/login",
            method="POST",
            payload={"username": username, "password": password, "remember_me": False},
        )
        result[key] = status
    return result


def ensure_user_set(db_path: Path) -> None:
    usernames = {row["username"] for row in fetch_users(db_path)}
    missing = [name for name in VALID_USERS if name not in usernames]
    if missing:
        raise SystemExit(f"missing users: {', '.join(missing)}")


def preflight_handoff(
    db_path: Path,
    api_base_url: str,
    *,
    production: bool,
) -> tuple[int, dict[str, Any] | None]:
    parsed = urlsplit(api_base_url)
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
    if production and parsed.scheme != "https":
        raise RuntimeError("生产密码交接只允许显式 HTTPS 反向代理 API URL")
    if not db_path.is_file():
        raise RuntimeError(f"数据库不存在：{db_path}")
    integrity = db_integrity(db_path)
    if integrity != {"integrity_check": "ok", "foreign_key_check_count": 0}:
        raise RuntimeError(f"数据库预检失败：{integrity}")
    ensure_user_set(db_path)
    with sqlite3.connect(db_path, timeout=15) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        if not {"password_hash", "auth_version"}.issubset(columns):
            raise RuntimeError("users 表缺少密码或会话撤销字段")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("SELECT COUNT(*) FROM users").fetchone()
        connection.rollback()
    health_status, health_body = request_json(
        open_api(api_base_url),
        f"{api_base_url.rstrip('/')}/api/health",
    )
    if health_status != 200 or not health_body or health_body.get("ok") is not True:
        raise RuntimeError(
            f"API 健康预检失败：status={health_status}, body={health_body}"
        )
    return health_status, health_body


def main() -> int:
    args = parse_args()
    db_path = Path(args.sqlite_path).resolve()
    output_json = Path(args.output_json).resolve()
    current = load_settings()
    api_base_url = args.api_base_url or os.getenv("ERP_BROWSER_URL", "").strip()
    if not api_base_url:
        if current.is_production:
            raise RuntimeError(
                "生产密码交接必须通过 --api-base-url 或 ERP_BROWSER_URL 显式提供 HTTPS URL"
            )
        api_base_url = f"http://127.0.0.1:{current.port}"
    api_base_url = api_base_url.rstrip("/")
    health_status, health_body = preflight_handoff(
        db_path,
        api_base_url,
        production=current.is_production,
    )
    output_json.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.parent / "backups" / f"{db_path.stem}_before_final_password_handoff_{timestamp}{db_path.suffix}"

    before_users = [
        {"username": row["username"], "role": row["role"]}
        for row in fetch_users(db_path)
    ]
    source_sha_before = sha256_of(db_path)
    source_integrity_before = db_integrity(db_path)
    counts_before = {
        "sales_orders": db_scalar(db_path, "SELECT COUNT(*) FROM sales_orders"),
        "sales_order_items": db_scalar(db_path, "SELECT COUNT(*) FROM sales_order_items"),
        "legacy_ruida_orders": db_scalar(db_path, "SELECT COUNT(*) FROM legacy_ruida_orders"),
        "legacy_ruida_order_items": db_scalar(db_path, "SELECT COUNT(*) FROM legacy_ruida_order_items"),
        "ruida_prefixed_orders": db_scalar(db_path, "SELECT COUNT(*) FROM sales_orders WHERE order_number LIKE 'RUIDA-%'"),
        "operation_logs": db_scalar(db_path, "SELECT COUNT(*) FROM operation_logs"),
    }

    backup_sqlite(db_path, backup_path)
    backup_sha, backup_integrity = validate_backup_before_password_write(
        backup_path
    )

    passwords = {username: prompt_password(username) for username in VALID_USERS}
    update_passwords(db_path, passwords, actor_username=args.actor)

    source_sha_after = sha256_of(db_path)
    after_users_full = fetch_users(db_path)
    after_users = [{"username": row["username"], "role": row["role"]} for row in after_users_full]
    counts_after = {
        "sales_orders": db_scalar(db_path, "SELECT COUNT(*) FROM sales_orders"),
        "sales_order_items": db_scalar(db_path, "SELECT COUNT(*) FROM sales_order_items"),
        "legacy_ruida_orders": db_scalar(db_path, "SELECT COUNT(*) FROM legacy_ruida_orders"),
        "legacy_ruida_order_items": db_scalar(db_path, "SELECT COUNT(*) FROM legacy_ruida_order_items"),
        "ruida_prefixed_orders": db_scalar(db_path, "SELECT COUNT(*) FROM sales_orders WHERE order_number LIKE 'RUIDA-%'"),
        "operation_logs": db_scalar(db_path, "SELECT COUNT(*) FROM operation_logs"),
    }
    source_integrity_after = db_integrity(db_path)

    login_checks = {
        username: vars(verify_login(api_base_url, username, password))
        for username, password in passwords.items()
    }
    role_checks = {
        username: role_matrix(api_base_url, username, password)
        for username, password in passwords.items()
    }
    old_password_results = verify_old_passwords(api_base_url)
    no_login_status, _ = request_json(open_api(api_base_url), f"{api_base_url}/api/auth/me")
    forged_status, _ = request_json(
        open_api(api_base_url, CookieJar()),
        f"{api_base_url}/api/auth/me",
    )
    incoming_no_login_status, _ = request_json(
        open_api(api_base_url),
        f"{api_base_url}/api/incoming/pending",
    )
    incoming_html_status, _ = request_json(
        open_api(api_base_url),
        f"{api_base_url}/incoming.html",
    )

    result = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "database": str(db_path),
        "backup_path": str(backup_path),
        "source_sha_before": source_sha_before,
        "source_sha_after": source_sha_after,
        "backup_sha": backup_sha,
        "source_backup_sha_equal_before": source_sha_before == backup_sha,
        "integrity_before": source_integrity_before,
        "integrity_backup": backup_integrity,
        "integrity_after": source_integrity_after,
        "before_users": before_users,
        "after_users": after_users,
        "after_users_security": [
            {
                "username": row["username"],
                "role": row["role"],
                "is_active": row["is_active"],
                "must_change_password": row["must_change_password"],
                "modern_hash": row["password_hash"].startswith(("$2a$", "$2b$", "$2y$")),
            }
            for row in after_users_full
        ],
        "counts_before": counts_before,
        "counts_after": counts_after,
        "health": {"status": health_status, "body": health_body},
        "login_checks": login_checks,
        "old_password_results": old_password_results,
        "no_login_status": no_login_status,
        "forged_cookie_status": forged_status,
        "incoming_api_without_login_status": incoming_no_login_status,
        "incoming_html_status": incoming_html_status,
        "role_checks": role_checks,
        "history_counts_unchanged": counts_before["sales_orders"] == counts_after["sales_orders"]
        and counts_before["sales_order_items"] == counts_after["sales_order_items"]
        and counts_before["legacy_ruida_orders"] == counts_after["legacy_ruida_orders"]
        and counts_before["legacy_ruida_order_items"] == counts_after["legacy_ruida_order_items"]
        and counts_before["ruida_prefixed_orders"] == counts_after["ruida_prefixed_orders"],
        "operation_logs_delta": counts_after["operation_logs"] - counts_before["operation_logs"],
    }
    output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
