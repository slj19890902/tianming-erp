from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import os
import secrets
import socket
import sqlite3
import ssl
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


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
    "sales_orders": ({"sales_orders"}, "SELECT COUNT(*) FROM sales_orders"),
    "sales_order_items": (
        {"sales_order_items"},
        "SELECT COUNT(*) FROM sales_order_items",
    ),
    "legacy_ruida_orders": (
        {"legacy_ruida_orders"},
        "SELECT COUNT(*) FROM legacy_ruida_orders",
    ),
    "legacy_ruida_order_items": (
        {"legacy_ruida_order_items"},
        "SELECT COUNT(*) FROM legacy_ruida_order_items",
    ),
    "ruida_prefixed_orders": (
        {"sales_orders"},
        "SELECT COUNT(*) FROM sales_orders WHERE order_number LIKE 'RUIDA-%'",
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


@dataclass(frozen=True, slots=True)
class PinnedTlsConfig:
    public_origin: str
    connect_host: str
    connect_port: int
    upstream_origin: str
    ca_certificate: Path


_ACTIVE_PINNED_TLS: PinnedTlsConfig | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive, auditable final password handoff"
    )
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--api-base-url")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--backup-dir")
    parser.add_argument(
        "--loopback-upstream",
        help=(
            "Optional plain-HTTP upstream restricted to a literal loopback address. "
            "When supplied, the tool creates a temporary verified-TLS loopback proxy "
            "and pins all API sockets to it while retaining the configured HTTPS origin."
        ),
    )
    parser.add_argument("--actor", required=True)
    parser.add_argument(
        "--temporary-reset",
        action="store_true",
        help=(
            "Reset the four approved accounts to distinct temporary passwords, "
            "leave must_change_password enabled for every account, and do not "
            "request or probe any previous password."
        ),
    )
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


def history_counts(db_path: Path) -> dict[str, int | None]:
    with closing(_read_only_connection(db_path)) as connection:
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        return {
            name: (
                int(connection.execute(sql).fetchone()[0])
                if required_tables <= existing_tables
                else None
            )
            for name, (required_tables, sql) in HISTORY_COUNT_QUERIES.items()
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


def collect_admin_final_password(
    rotation_passwords: dict[str, str],
) -> str:
    while True:
        print("[admin] 请输入完成首次登录改密后使用的最终密码。")
        candidate = prompt_password("admin")
        if any(
            hmac.compare_digest(candidate, existing)
            for existing in rotation_passwords.values()
        ):
            print("[admin] 最终密码必须与本轮四个轮换密码均不相同，请重试。")
            continue
        return candidate


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
    operation: str = "handoff",
) -> dict[str, Any]:
    if set(passwords) != set(VALID_USERS):
        raise RuntimeError("密码交接必须一次且仅覆盖四个指定账号")
    if len(set(passwords.values())) != len(VALID_USERS):
        raise RuntimeError("四个账号必须使用各不相同的新密码")

    if operation not in {"handoff", "temporary_reset"}:
        raise RuntimeError("不支持的密码维护操作")
    audit_action = (
        "ADMIN_TEMP_PASSWORD_RESET"
        if operation == "temporary_reset"
        else "FINAL_PASSWORD_HANDOFF"
    )
    audit_description = (
        "管理员临时密码重置"
        if operation == "temporary_reset"
        else "最终密码交接"
    )

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
                ) VALUES (?, ?, 'User', ?, ?, ?, 'user', ?)
                """,
                (
                    actor_id,
                    audit_action,
                    json.dumps(
                        {
                            "target_usernames": list(VALID_USERS),
                            "auth_version_changes": version_changes,
                            "must_change_password": True,
                            "operation": operation,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    actor_name,
                    actor_role,
                    audit_description,
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
        "audit_action": audit_action,
        "auth_version_changes": version_changes,
    }


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        connect_host: str,
        connect_port: int,
        context: ssl.SSLContext,
        **kwargs: Any,
    ) -> None:
        self._pinned_connect_host = connect_host
        self._pinned_connect_port = connect_port
        super().__init__(host, context=context, **kwargs)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_connect_host, self._pinned_connect_port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self.host,
        )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, config: PinnedTlsConfig) -> None:
        context = ssl.create_default_context(cafile=str(config.ca_certificate))
        super().__init__(context=context, check_hostname=True)
        self._config = config

    def https_open(self, request: urllib.request.Request) -> Any:
        def factory(host: str, **kwargs: Any) -> _PinnedHTTPSConnection:
            return _PinnedHTTPSConnection(
                host,
                connect_host=self._config.connect_host,
                connect_port=self._config.connect_port,
                context=self._context,
                **kwargs,
            )

        return self.do_open(factory, request)


class _LoopbackTlsProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ERPPasswordHandoffLoopback/1"

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def _forward(self) -> None:
        allowed = {
            ("GET", "/api/health"),
            ("POST", "/api/auth/login"),
            ("GET", "/api/auth/me"),
            ("POST", "/api/auth/logout"),
            ("PUT", "/api/auth/password"),
            ("GET", "/api/incoming/pending"),
            ("GET", "/incoming.html"),
        }
        path = urlsplit(self.path).path
        if (self.command, path) not in allowed:
            self.send_error(404)
            return
        if not ipaddress.ip_address(self.client_address[0]).is_loopback:
            self.send_error(403)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if content_length < 0 or content_length > 1024 * 1024:
            self.send_error(413)
            return
        body = self.rfile.read(content_length) if content_length else None
        excluded = {
            "connection",
            "host",
            "keep-alive",
            "proxy-connection",
            "transfer-encoding",
            "upgrade",
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-proto",
        }
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in excluded
        }
        proxy_server = self.server
        headers.update(
            {
                "Host": proxy_server.public_hostname,
                "X-Forwarded-For": "127.0.0.1",
                "X-Forwarded-Host": proxy_server.public_hostname,
                "X-Forwarded-Proto": "https",
                "Connection": "close",
            }
        )
        try:
            connection = http.client.HTTPConnection(
                proxy_server.upstream_host,
                proxy_server.upstream_port,
                timeout=15,
            )
            connection.request(
                self.command,
                self.path,
                body=body,
                headers=headers,
            )
            response = connection.getresponse()
            response_body = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in {
                    "connection",
                    "content-length",
                    "keep-alive",
                    "transfer-encoding",
                }:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response_body)
        except (OSError, http.client.HTTPException):
            error_body = b'{"ok":false}'
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(error_body)
        finally:
            self.close_connection = True
            if "connection" in locals():
                connection.close()


def _write_loopback_certificates(
    directory: Path,
    public_hostname: str,
) -> tuple[Path, Path, Path]:
    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "ERP password handoff local CA")]
    )
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=4))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, public_hostname)]
    )
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(public_hostname)]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    cert_path = directory / "server.pem"
    key_path = directory / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    for path in (ca_path, cert_path, key_path):
        os.chmod(path, 0o600)
    return ca_path, cert_path, key_path


@contextmanager
def pinned_loopback_tls_proxy(
    public_origin: str,
    upstream_origin: str,
    secure_parent: Path,
) -> Iterator[PinnedTlsConfig]:
    global _ACTIVE_PINNED_TLS
    if _ACTIVE_PINNED_TLS is not None:
        raise RuntimeError("回环 TLS 维护通道已在运行")
    public = urlsplit(public_origin)
    upstream = urlsplit(upstream_origin)
    if public.scheme != "https" or not public.hostname:
        raise RuntimeError("回环 TLS 维护通道要求已配置的 HTTPS 公共来源")
    try:
        upstream_address = ipaddress.ip_address(upstream.hostname or "")
    except ValueError as error:
        raise RuntimeError("维护上游必须使用回环 IP 字面量") from error
    if (
        upstream.scheme != "http"
        or not upstream_address.is_loopback
        or upstream.username is not None
        or upstream.password is not None
        or upstream.query
        or upstream.fragment
        or upstream.path not in {"", "/"}
        or upstream.port is None
    ):
        raise RuntimeError("维护上游必须是带端口的纯回环 HTTP 根地址")

    secure_parent.mkdir(parents=True, exist_ok=True)
    certificate_dir = secure_parent / (
        f".password-handoff-tls-{os.getpid()}-{secrets.token_hex(4)}"
    )
    ca_path, cert_path, key_path = _write_loopback_certificates(
        certificate_dir, public.hostname
    )
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _LoopbackTlsProxyHandler,
    )
    server.daemon_threads = True
    server.upstream_host = str(upstream_address)
    server.upstream_port = int(upstream.port)
    server.public_hostname = public.hostname
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(
        target=server.serve_forever,
        name="password-handoff-loopback-tls",
        daemon=True,
    )
    thread.start()
    config = PinnedTlsConfig(
        public_origin=public_origin,
        connect_host="127.0.0.1",
        connect_port=int(server.server_address[1]),
        upstream_origin=upstream_origin,
        ca_certificate=ca_path,
    )
    _ACTIVE_PINNED_TLS = config
    try:
        yield config
    finally:
        _ACTIVE_PINNED_TLS = None
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        for path in (key_path, cert_path, ca_path):
            path.unlink(missing_ok=True)
        certificate_dir.rmdir()


def _transport_security_summary(base_url: str) -> dict[str, Any]:
    config = _ACTIVE_PINNED_TLS
    if config is None:
        return {
            "mode": "direct_https",
            "public_origin": _origin(base_url),
            "environment_proxy_disabled": True,
            "redirects_disabled": True,
            "certificate_verification": "system_trust",
        }
    return {
        "mode": "pinned_loopback_tls",
        "public_origin": config.public_origin,
        "socket_target": f"{config.connect_host}:{config.connect_port}",
        "upstream_origin": config.upstream_origin,
        "environment_proxy_disabled": True,
        "redirects_disabled": True,
        "certificate_verification": "temporary_local_ca",
        "loopback_only": True,
    }


def open_api(
    base_url: str,
    cookie_jar: CookieJar | None = None,
) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [
        urllib.request.ProxyHandler({}),
        _RejectRedirectHandler(),
    ]
    pinned = _ACTIVE_PINNED_TLS
    if pinned is not None:
        if _origin(base_url) != pinned.public_origin:
            raise RuntimeError("API 来源与回环 TLS 固定来源不一致")
        handlers.append(_PinnedHTTPSHandler(pinned))
    elif urlsplit(base_url).scheme == "https":
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
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
    origin = _origin(normalized)
    if parsed.scheme == "https" and origin in configured_https_origins:
        return normalized
    raise RuntimeError(
        "生产密码交接 API 地址必须精确匹配已配置 HTTPS 入口；"
        "普通局域网或公网明文 HTTP 禁止传输密码"
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
        "transport_security": _transport_security_summary(normalized_api),
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
        headers={"Origin": _origin(base_url)},
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
    return result


def complete_admin_forced_password_change(
    base_url: str,
    rotation_password: str,
    final_password: str,
) -> dict[str, Any]:
    initial_jar = CookieJar()
    initial_opener = open_api(base_url, initial_jar)
    initial_login = _login(
        base_url, initial_opener, "admin", rotation_password
    )
    before = request_json(initial_opener, f"{base_url}/api/auth/me")
    before_user = (before.body or {}).get("user") if before.body else None
    result: dict[str, Any] = {
        "rotation_login_status": initial_login.status,
        "before_change_me_status": before.status,
        "before_change_required": (before_user or {}).get(
            "must_change_password"
        ),
        "rotation_session_cookie_received": bool(list(initial_jar)),
        "change_status": None,
        "old_session_after_change_status": None,
        "rotation_password_after_change_status": None,
        "final_login_status": None,
        "final_me_status": None,
        "final_change_required": None,
        "final_logout_status": None,
        "final_after_logout_status": None,
        "error": initial_login.error or before.error,
    }
    if (
        initial_login.status != 200
        or before.status != 200
        or result["before_change_required"] is not True
        or not list(initial_jar)
        or result["error"] is not None
    ):
        return result

    changed = request_json(
        initial_opener,
        f"{base_url}/api/auth/password",
        method="PUT",
        payload={
            "current_password": rotation_password,
            "new_password": final_password,
        },
        headers={"Origin": _origin(base_url)},
    )
    result["change_status"] = changed.status
    result["error"] = changed.error
    if changed.status != 200 or changed.error is not None:
        return result

    old_session = request_json(
        initial_opener, f"{base_url}/api/auth/me"
    )
    result["old_session_after_change_status"] = old_session.status

    intermediate_attempt = _login(
        base_url,
        open_api(base_url),
        "admin",
        rotation_password,
    )
    result["rotation_password_after_change_status"] = (
        intermediate_attempt.status
    )

    final_jar = CookieJar()
    final_opener = open_api(base_url, final_jar)
    final_login = _login(base_url, final_opener, "admin", final_password)
    final_me = request_json(final_opener, f"{base_url}/api/auth/me")
    final_user = (
        (final_me.body or {}).get("user") if final_me.body else None
    )
    final_session_cookie_received = bool(list(final_jar))
    final_logout = request_json(
        final_opener,
        f"{base_url}/api/auth/logout",
        method="POST",
        payload={},
        headers={"Origin": _origin(base_url)},
    )
    final_after_logout = request_json(
        final_opener, f"{base_url}/api/auth/me"
    )
    result.update(
        {
            "final_login_status": final_login.status,
            "final_me_status": final_me.status,
            "final_change_required": (final_user or {}).get(
                "must_change_password"
            ),
            "final_session_cookie_received": final_session_cookie_received,
            "final_logout_status": final_logout.status,
            "final_after_logout_status": final_after_logout.status,
            "error": (
                old_session.error
                or intermediate_attempt.error
                or final_login.error
                or final_me.error
                or final_logout.error
                or final_after_logout.error
            ),
        }
    )
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


def _audit_log_exists(
    db_path: Path, audit_log_id: int, expected_action: str
) -> bool:
    with closing(_read_only_connection(db_path)) as connection:
        row = connection.execute(
            "SELECT action FROM operation_logs WHERE id = ?", (audit_log_id,)
        ).fetchone()
    return row is not None and row[0] == expected_action


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
    admin_change = result.get("admin_forced_password_change", {})
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
        "anonymous_and_forged_sessions_rejected": (
            security.get("no_login_me", {}).get("status") == 401
            and security.get("forged_cookie_me", {}).get("status") == 401
            and security.get("forged_cookie_me", {}).get("non_empty_cookie_sent")
            is True
            and security.get("incoming_api_without_login", {}).get("status") == 401
        ),
        "admin_forced_password_change_completed": (
            admin_change.get("rotation_login_status") == 200
            and admin_change.get("before_change_me_status") == 200
            and admin_change.get("before_change_required") is True
            and admin_change.get("rotation_session_cookie_received") is True
            and admin_change.get("change_status") == 200
            and admin_change.get("old_session_after_change_status") == 401
            and admin_change.get("rotation_password_after_change_status") == 401
            and admin_change.get("final_login_status") == 200
            and admin_change.get("final_me_status") == 200
            and admin_change.get("final_change_required") is False
            and admin_change.get("final_session_cookie_received") is True
            and admin_change.get("final_logout_status") == 200
            and admin_change.get("final_after_logout_status") == 401
            and admin_change.get("error") is None
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
                and row.get("must_change_password")
                is (False if row.get("username") == "admin" else True)
                and row.get("hash_compatible") is True
                for row in users
            )
        ),
    }
    return checks


def evaluate_temporary_reset_verification(
    result: dict[str, Any],
) -> dict[str, bool]:
    login_checks = result.get("login_checks", {})
    security = result.get("security_status_checks", {})
    users = result.get("after_users_security", [])
    version_changes = result.get("write", {}).get("auth_version_changes", {})
    users_by_name = {row.get("username"): row for row in users}
    return {
        "all_temporary_logins": all(
            login_checks.get(username, {}).get("login_status") == 200
            and login_checks.get(username, {}).get("me_status") == 200
            and login_checks.get(username, {}).get("logout_status") == 200
            and login_checks.get(username, {}).get("after_logout_status") == 401
            and login_checks.get(username, {}).get("must_change_password") is True
            and login_checks.get(username, {}).get("session_cookie_received") is True
            and login_checks.get(username, {}).get("error") is None
            for username in VALID_USERS
        ),
        "anonymous_and_forged_sessions_rejected": (
            security.get("no_login_me", {}).get("status") == 401
            and security.get("forged_cookie_me", {}).get("status") == 401
            and security.get("forged_cookie_me", {}).get("non_empty_cookie_sent")
            is True
            and security.get("incoming_api_without_login", {}).get("status") == 401
        ),
        "auth_versions_incremented": (
            set(version_changes) == set(VALID_USERS)
            and all(
                version_changes[username].get("after")
                == version_changes[username].get("before") + 1
                and users_by_name.get(username, {}).get("auth_version")
                == version_changes[username].get("after")
                for username in VALID_USERS
            )
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
        "reset_audit_committed": result.get("handoff_audit_exists") is True,
        "four_expected_accounts_require_change": (
            len(users) == len(VALID_USERS)
            and set(users_by_name) == set(VALID_USERS)
            and all(
                users_by_name[username].get("role") == EXPECTED_ROLES[username]
                and users_by_name[username].get("active") is True
                and users_by_name[username].get("must_change_password") is True
                and users_by_name[username].get("hash_compatible") is True
                for username in VALID_USERS
            )
        ),
    }


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
        "mode": (
            "temporary_reset"
            if bool(getattr(args, "temporary_reset", False))
            else "password_handoff"
        ),
    }
    new_passwords: dict[str, str] | None = None
    admin_final_passwords: dict[str, str] | None = None
    previous_passwords: dict[str, str] | None = None
    proxy_context: Any = None
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
        loopback_upstream = getattr(args, "loopback_upstream", None)
        if loopback_upstream:
            upstream = urlsplit(loopback_upstream)
            if int(upstream.port or 0) != int(getattr(settings, "port", 0)):
                raise RuntimeError("维护上游端口与当前运行配置端口不一致")
            configured_bind = str(getattr(settings, "bind_host", "")).strip()
            try:
                bind_address = ipaddress.ip_address(configured_bind)
            except ValueError as error:
                raise RuntimeError("当前服务绑定地址不是回环 IP") from error
            if not bind_address.is_loopback:
                raise RuntimeError("当前服务未绑定回环地址，拒绝建立维护通道")
            pending_proxy_context = pinned_loopback_tls_proxy(
                _origin(api_base_url),
                loopback_upstream,
                output_json.parent,
            )
            pending_proxy_context.__enter__()
            proxy_context = pending_proxy_context
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

        temporary_reset = bool(getattr(args, "temporary_reset", False))
        new_passwords = collect_new_passwords()
        existing_sessions: dict[str, ExistingSession] = {}
        if temporary_reset:
            result["limitations"] = {
                "old_password_rejection": "not_verified_previous_passwords_not_collected",
                "prechange_live_sessions": "not_observed_no_previous_credentials",
                "first_login_password_change": "pending_for_all_accounts",
            }
        else:
            admin_final_passwords = {
                "admin": collect_admin_final_password(new_passwords)
            }
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
            db_path,
            new_passwords,
            actor_username=args.actor,
            operation="temporary_reset" if temporary_reset else "handoff",
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

        if not temporary_reset:
            result["prechange_sessions"] = verify_existing_sessions_revoked(
                api_base_url, existing_sessions
            )
        result["login_checks"] = {
            username: asdict(
                verify_login(api_base_url, username, new_passwords[username])
            )
            for username in VALID_USERS
        }
        if not temporary_reset:
            result["old_password_results"] = verify_old_passwords(
                api_base_url, previous_passwords
            )
            result["admin_forced_password_change"] = (
                complete_admin_forced_password_change(
                    api_base_url,
                    new_passwords["admin"],
                    admin_final_passwords["admin"],
                )
            )
        result["security_status_checks"] = _security_status_checks(
            api_base_url, getattr(settings, "session_cookie_name", "erp_session")
        )
        result["history_counts_unchanged"] = counts_before == history_counts(db_path)
        result["integrity_after"] = db_integrity(db_path)
        result["handoff_audit_exists"] = _audit_log_exists(
            db_path,
            int(write_result["audit_log_id"]),
            str(write_result["audit_action"]),
        )
        result["after_users_security"] = _sanitized_user_security(db_path)
        verdict = (
            evaluate_temporary_reset_verification(result)
            if temporary_reset
            else evaluate_verification(result)
        )
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
        _scrub_password_mapping(admin_final_passwords)
        _scrub_password_mapping(previous_passwords)
        if proxy_context is not None:
            proxy_context.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
