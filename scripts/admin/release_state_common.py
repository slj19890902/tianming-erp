from __future__ import annotations

import ast
import gc
import hashlib
import hmac
import importlib.metadata
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


CONFIG_ENV_NAMES = (
    "ERP_ENVIRONMENT",
    "ERP_DATABASE_PATH",
    "ERP_BACKUP_DIR",
    "ERP_BIND_HOST",
    "ERP_PORT",
    "ERP_WORKERS",
    "ERP_HEALTH_URL",
    "ERP_BROWSER_URL",
    "ERP_PRODUCTION_TRANSPORT",
    "ERP_ALLOWED_ORIGINS",
    "ERP_TRUSTED_HOSTS",
    "ERP_TRUSTED_PROXY_IPS",
    "ERP_SESSION_COOKIE_SECURE",
    "ERP_SECRET_KEY",
)
CONFIG_FINGERPRINT_FILES = (
    ".env",
    "data/session_secret.key",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "alembic.ini",
    "app/core/config.py",
    "scripts/windows/start_erp.ps1",
    "scripts/admin/start_erp_background.ps1",
)
EVIDENCE_SIGNATURE_FIELD = "evidence_hmac"
EVIDENCE_KEY_CONTEXT = b"tm-erp-release-evidence-v1"


class ReleaseStateError(RuntimeError):
    """Release/rollback evidence is missing, inconsistent, or unsafe."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_evidence_key(project_root: Path) -> bytes:
    secret_path = project_root.resolve() / "data" / "session_secret.key"
    if not secret_path.is_file():
        raise ReleaseStateError(f"发布证据签名密钥不存在：{secret_path}")
    secret = secret_path.read_bytes().strip()
    if len(secret) < 32:
        raise ReleaseStateError("发布证据签名密钥长度不足 32 字节")
    return hmac.new(secret, EVIDENCE_KEY_CONTEXT, hashlib.sha256).digest()


def _canonical_evidence(payload: dict[str, Any]) -> bytes:
    unsigned = {
        key: value
        for key, value in payload.items()
        if key != EVIDENCE_SIGNATURE_FIELD
    }
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_evidence(
    payload: dict[str, Any],
    *,
    project_root: Path,
    purpose: str,
) -> dict[str, Any]:
    signed = dict(payload)
    purpose_key = hmac.new(
        _release_evidence_key(project_root),
        b"purpose\0" + purpose.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    key_id = hashlib.sha256(purpose_key).hexdigest()[:16]
    signature = hmac.new(
        purpose_key,
        _canonical_evidence(signed),
        hashlib.sha256,
    ).hexdigest()
    signed[EVIDENCE_SIGNATURE_FIELD] = {
        "algorithm": "hmac-sha256-v1",
        "purpose": purpose,
        "key_id": key_id,
        "value": signature,
    }
    return signed


def verify_evidence(
    payload: dict[str, Any],
    *,
    project_root: Path,
    purpose: str,
) -> None:
    signature = payload.get(EVIDENCE_SIGNATURE_FIELD)
    if not isinstance(signature, dict):
        raise ReleaseStateError("发布证据缺少受保护签名")
    if (
        signature.get("algorithm") != "hmac-sha256-v1"
        or signature.get("purpose") != purpose
    ):
        raise ReleaseStateError("发布证据签名类型或用途不匹配")
    expected_signature = sign_evidence(
        payload,
        project_root=project_root,
        purpose=purpose,
    )[EVIDENCE_SIGNATURE_FIELD]
    if signature.get("key_id") != expected_signature["key_id"]:
        raise ReleaseStateError("发布证据签名密钥已变化，必须转人工离线核验")
    expected = expected_signature["value"]
    actual = str(signature.get("value") or "")
    if not actual or not hmac.compare_digest(actual, expected):
        raise ReleaseStateError("发布证据签名验证失败，文件可能已被修改")


def _hash_part(digest: Any, marker: bytes, payload: bytes) -> None:
    digest.update(marker)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _hash_value(digest: Any, value: Any) -> None:
    if value is None:
        _hash_part(digest, b"N", b"")
    elif isinstance(value, bytes):
        _hash_part(digest, b"B", value)
    elif isinstance(value, int):
        _hash_part(digest, b"I", str(value).encode("ascii"))
    elif isinstance(value, float):
        _hash_part(digest, b"F", value.hex().encode("ascii"))
    else:
        _hash_part(digest, b"S", str(value).encode("utf-8"))


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sqlite_logical_fingerprint(path: Path) -> dict[str, Any]:
    """Hash schema and every application-table row without writing the database.

    The result is independent from WAL checkpoint/file-layout changes and catches
    updates where row counts remain unchanged.
    """

    resolved = path.resolve()
    if not resolved.is_file():
        raise ReleaseStateError(f"数据库文件不存在：{resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    try:
        connection.execute("BEGIN")
        schema_rows = connection.execute(
            """
            SELECT type, name, tbl_name, COALESCE(sql, '')
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' OR name = 'sqlite_sequence'
            ORDER BY type, name
            """
        ).fetchall()
        schema_digest = hashlib.sha256()
        for schema_row in schema_rows:
            for value in schema_row:
                _hash_value(schema_digest, value)

        table_definitions = [
            (str(name), str(sql or ""))
            for object_type, name, _table_name, sql in schema_rows
            if object_type == "table"
        ]
        table_results: dict[str, dict[str, Any]] = {}
        total_digest = hashlib.sha256()
        _hash_part(total_digest, b"SCHEMA", schema_digest.hexdigest().encode("ascii"))

        for table_name, create_sql in table_definitions:
            table_digest = hashlib.sha256()
            _hash_part(table_digest, b"TABLE", table_name.encode("utf-8"))
            _hash_part(table_digest, b"SQL", create_sql.encode("utf-8"))
            quoted_table = _quote_identifier(table_name)
            columns = connection.execute(
                f"PRAGMA table_info({quoted_table})"
            ).fetchall()
            column_names = [str(column[1]) for column in columns]
            for column_name in column_names:
                _hash_part(table_digest, b"COLUMN", column_name.encode("utf-8"))

            primary_key_columns = [
                str(column[1])
                for column in sorted(columns, key=lambda item: int(item[5] or 0))
                if int(column[5] or 0) > 0
            ]
            order_columns = primary_key_columns + [
                name for name in column_names if name not in primary_key_columns
            ]
            order_clause = ", ".join(_quote_identifier(name) for name in order_columns)
            query = f"SELECT * FROM {quoted_table}"
            if order_clause:
                query += f" ORDER BY {order_clause}"

            row_count = 0
            cursor = connection.execute(query)
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for row in rows:
                    _hash_part(table_digest, b"ROW", b"")
                    for value in row:
                        _hash_value(table_digest, value)
                    row_count += 1
            cursor.close()
            table_hash = table_digest.hexdigest()
            table_results[table_name] = {
                "rows": row_count,
                "sha256": table_hash,
            }
            _hash_part(total_digest, b"TABLE_HASH", table_hash.encode("ascii"))

        return {
            "algorithm": "sqlite-logical-v1",
            "schema_sha256": schema_digest.hexdigest(),
            "sha256": total_digest.hexdigest(),
            "table_count": len(table_results),
            "tables": table_results,
        }
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
        gc.collect()


def sqlite_logical_fingerprint_via_copy(path: Path) -> dict[str, Any]:
    """Fingerprint a temporary SQLite Backup API copy.

    Copying in bounded pages avoids holding one long shared-locking read
    transaction on the live ERP database while the expensive full-table hash is
    calculated.
    """

    resolved = path.resolve()
    if not resolved.is_file():
        raise ReleaseStateError(f"数据库文件不存在：{resolved}")
    with tempfile.TemporaryDirectory(prefix="tm-erp-fault-check-") as directory:
        copy_path = Path(directory) / "diagnostic.sqlite3"
        source = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        source.execute("PRAGMA query_only = ON")
        destination = sqlite3.connect(copy_path)
        try:
            source.backup(destination, pages=1024, sleep=0.02)
            destination.commit()
        finally:
            destination.close()
            source.close()
            gc.collect()
        return sqlite_logical_fingerprint(copy_path)


def runtime_config_fingerprint(project_root: Path) -> dict[str, Any]:
    """Return a non-secret fingerprint of runtime config and dependency inputs."""

    root = project_root.resolve()
    files: dict[str, dict[str, Any]] = {}
    material: dict[str, Any] = {"files": {}, "environment": {}}
    for relative_name in CONFIG_FINGERPRINT_FILES:
        path = root / relative_name
        if path.is_file():
            entry = {
                "exists": True,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        else:
            entry = {"exists": False}
        files[relative_name] = entry
        material["files"][relative_name] = entry

    present_environment_names: list[str] = []
    for name in CONFIG_ENV_NAMES:
        if name in os.environ:
            present_environment_names.append(name)
            material["environment"][name] = hashlib.sha256(
                os.environ[name].encode("utf-8")
            ).hexdigest()

    material["python"] = {
        "implementation": sys.implementation.name,
        "version": list(sys.version_info[:3]),
        "executable": str(Path(sys.executable).resolve()),
    }
    installed_distributions: list[dict[str, str]] = []
    for distribution in importlib.metadata.distributions():
        raw_name = str(distribution.metadata.get("Name") or "").strip()
        normalized_name = re.sub(r"[-_.]+", "-", raw_name).lower()
        if not normalized_name:
            continue
        direct_url = distribution.read_text("direct_url.json") or ""
        record = distribution.read_text("RECORD") or ""
        installed_distributions.append(
            {
                "name": normalized_name,
                "version": str(distribution.version),
                "direct_url_sha256": hashlib.sha256(
                    direct_url.encode("utf-8")
                ).hexdigest(),
                "record_sha256": hashlib.sha256(record.encode("utf-8")).hexdigest(),
            }
        )
    installed_distributions.sort(
        key=lambda item: (
            item["name"],
            item["version"],
            item["direct_url_sha256"],
            item["record_sha256"],
        )
    )
    material["installed_distributions"] = installed_distributions
    installed_encoded = json.dumps(
        installed_distributions,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "algorithm": "runtime-config-v2",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "files": files,
        "environment_overrides_present": present_environment_names,
        "python": material["python"],
        "installed_distribution_count": len(installed_distributions),
        "installed_distributions_sha256": hashlib.sha256(
            installed_encoded
        ).hexdigest(),
    }


def _run_git(project_root: Path, arguments: Iterable[str]) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ReleaseStateError(f"Git 证据检查失败：{detail}")
    return result.stdout.strip()


def assert_commit_exists(project_root: Path, commit_sha: str) -> None:
    _run_git(project_root, ["cat-file", "-e", f"{commit_sha}^{{commit}}"])


def assert_ancestor(project_root: Path, ancestor_sha: str, descendant_sha: str) -> None:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode == 1:
        raise ReleaseStateError(
            f"更新前代码 {ancestor_sha} 不是目标代码 {descendant_sha} 的祖先"
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ReleaseStateError(f"无法验证 Git 祖先关系：{detail}")


def _assignment_literal(tree: ast.Module, variable_name: str) -> Any:
    for node in tree.body:
        target_name: str | None = None
        value_node: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                target_name = target.id
                value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value_node = node.value
        if target_name == variable_name and value_node is not None:
            return ast.literal_eval(value_node)
    raise ReleaseStateError(f"迁移文件缺少 {variable_name}")


def code_revision_at(project_root: Path, commit_sha: str) -> str:
    """Find the single Alembic head from migration files at an arbitrary commit."""

    assert_commit_exists(project_root, commit_sha)
    listing = _run_git(
        project_root,
        ["ls-tree", "-r", "--name-only", commit_sha, "--", "alembic/versions"],
    )
    migration_paths = [
        line.strip()
        for line in listing.splitlines()
        if line.strip().endswith(".py") and not line.strip().endswith("/__init__.py")
    ]
    if not migration_paths:
        raise ReleaseStateError(f"提交 {commit_sha} 没有 Alembic 迁移文件")

    revisions: set[str] = set()
    referenced_revisions: set[str] = set()
    for relative_path in migration_paths:
        source = _run_git(project_root, ["show", f"{commit_sha}:{relative_path}"])
        try:
            tree = ast.parse(source, filename=relative_path)
            revision = str(_assignment_literal(tree, "revision"))
            down_revision = _assignment_literal(tree, "down_revision")
        except (SyntaxError, ValueError, TypeError) as error:
            raise ReleaseStateError(
                f"无法解析提交 {commit_sha} 的迁移文件 {relative_path}：{error}"
            ) from error
        if revision in revisions:
            raise ReleaseStateError(f"提交 {commit_sha} 存在重复 revision：{revision}")
        revisions.add(revision)
        if isinstance(down_revision, str):
            referenced_revisions.add(down_revision)
        elif isinstance(down_revision, (tuple, list)):
            referenced_revisions.update(str(item) for item in down_revision if item)
        elif down_revision is not None:
            raise ReleaseStateError(
                f"提交 {commit_sha} 的 {relative_path} down_revision 无法识别"
            )

    heads = sorted(revisions - referenced_revisions)
    if len(heads) != 1:
        raise ReleaseStateError(
            f"提交 {commit_sha} 必须只有一个 Alembic head，实际为：{heads}"
        )
    return heads[0]
