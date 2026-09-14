from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.backup_retention import auto_cleanup_regular_backups, result_to_dict

CORE_COUNT_TABLES = (
    "customers",
    "products",
    "sales_orders",
    "sales_order_items",
    "material_requisitions",
    "material_requisition_items",
    "incoming_receipts",
    "incoming_receipt_items",
    "sales_deliveries",
    "sales_delivery_items",
    "finance_return_receipts",
    "finance_statements",
    "finance_invoices",
    "users",
    "operation_logs",
)
REQUIRED_BUSINESS_TABLES = (
    "customers",
    "products",
    "sales_orders",
    "sales_order_items",
    "material_requisitions",
    "incoming_receipts",
    "sales_deliveries",
    "finance_return_receipts",
    "finance_statements",
    "finance_invoices",
    "users",
    "operation_logs",
)


class ReleaseGateError(RuntimeError):
    """A fail-closed release gate rejected the requested operation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ReleaseGateError(f"数据库文件不存在：{resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def code_revision(project_root: Path = PROJECT_ROOT) -> str:
    config = Config(str(project_root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = tuple(script.get_heads())
    if len(heads) != 1:
        raise ReleaseGateError(f"代码必须只有一个 Alembic head，实际为：{heads}")
    return heads[0]


def git_sha(project_root: Path = PROJECT_ROOT) -> str:
    git_executable = os.getenv("ERP_UAT_GIT_PATH", "git").strip() or "git"
    result = subprocess.run(
        [git_executable, "rev-parse", "HEAD"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise ReleaseGateError(f"无法读取当前 Git SHA：{result.stderr.strip()}")
    return result.stdout.strip()


def release_metadata(*, expected_version: str | None = None) -> dict[str, Any]:
    """Read and validate the exact metadata serialized by /api/system/version."""

    try:
        from app.version import current_release_metadata

        return current_release_metadata(expected_version=expected_version)
    except Exception as error:
        raise ReleaseGateError(f"正式版本说明门禁失败：{error}") from error


def release_metadata_sha256(metadata: dict[str, Any]) -> str:
    material = json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def inspect_database(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    with _readonly_connection(resolved) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "alembic_version" not in tables:
            raise ReleaseGateError(f"数据库缺少 alembic_version：{resolved}")
        revisions = [
            str(row[0])
            for row in connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            ).fetchall()
        ]
        if len(revisions) != 1:
            raise ReleaseGateError(
                f"数据库必须只有一个 Alembic current，实际为：{revisions}"
            )
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in CORE_COUNT_TABLES
            if table in tables
        }
    wal_path = Path(str(resolved) + "-wal")
    wal = (
        {"path": str(wal_path), "size": wal_path.stat().st_size, "sha256": sha256_file(wal_path)}
        if wal_path.is_file()
        else None
    )
    return {
        "path": str(resolved),
        "size": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
        "wal": wal,
        "revision": revisions[0],
        "integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
        "core_counts": counts,
        "required_tables_missing": sorted(set(REQUIRED_BUSINESS_TABLES) - tables),
    }


def assert_healthy(snapshot: dict[str, Any], *, label: str) -> None:
    if str(snapshot["integrity_check"]).lower() != "ok":
        raise ReleaseGateError(
            f"{label} integrity_check 失败：{snapshot['integrity_check']}"
        )
    if int(snapshot["foreign_key_violations"]) != 0:
        raise ReleaseGateError(
            f"{label} 存在外键异常：{snapshot['foreign_key_violations']}"
        )


def assert_revision(snapshot: dict[str, Any], expected_revision: str, *, label: str) -> None:
    if snapshot["revision"] != expected_revision:
        raise ReleaseGateError(
            f"{label} Alembic revision 不匹配：current={snapshot['revision']}，"
            f"expected={expected_revision}。普通启动不会自动迁移。"
        )


def assert_business_smoke(snapshot: dict[str, Any], *, label: str) -> None:
    missing = snapshot["required_tables_missing"]
    if missing:
        raise ReleaseGateError(f"{label} 缺少核心业务表：{missing}")


def create_sqlite_copy(source: Path, destination: Path) -> dict[str, Any]:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise ReleaseGateError("SQLite 副本目标不能与来源相同")
    if destination.exists():
        raise ReleaseGateError(f"拒绝覆盖既有 SQLite 文件：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _readonly_connection(source) as source_connection:
            with sqlite3.connect(destination) as target_connection:
                source_connection.backup(target_connection)
        snapshot = inspect_database(destination)
        assert_healthy(snapshot, label="SQLite 副本")
        return snapshot
    except Exception:
        if destination.exists():
            destination.unlink()
        raise


def _run_migration(database: Path, revision: str, project_root: Path = PROJECT_ROOT) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "ERP_ENVIRONMENT": "test",
            "ERP_DATABASE_PATH": str(database.resolve()),
            "ERP_BIND_HOST": "127.0.0.1",
            "ERP_PORT": "18999",
            # Migration subprocess is local/test-only, not the HTTPS service.
            # Do not inherit production proxy origins into test-mode validation.
            "ERP_ALLOWED_ORIGINS": "http://127.0.0.1:18999",
            "ERP_HEALTH_URL": "http://127.0.0.1:18999/api/health",
            "ERP_BROWSER_URL": "http://127.0.0.1:18999",
            "ERP_WORKERS": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "alembic", "upgrade", revision],
        cwd=project_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ReleaseGateError(
            f"Alembic 精确迁移到 {revision} 失败（退出码 {result.returncode}）：{detail}"
        )


def check_startup(database: Path, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    expected_revision = code_revision(project_root)
    snapshot = inspect_database(database)
    assert_healthy(snapshot, label="启动数据库")
    assert_revision(snapshot, expected_revision, label="启动数据库")
    assert_business_smoke(snapshot, label="启动数据库")
    return {
        "ok": True,
        "mode": "read_only_startup_check",
        "code_revision": expected_revision,
        "database": snapshot,
    }


def _approval_token(plan: dict[str, Any]) -> str:
    material = json.dumps(
        {
            "code_sha": plan["code_sha"],
            "expected_revision": plan["expected_revision"],
            "database_path": plan["source"]["path"],
            "source_sha256": plan["source"]["sha256"],
            "source_wal": plan["source"]["wal"],
            "backup_path": plan["backup"]["path"],
            "backup_sha256": plan["backup"]["sha256"],
            "rehearsal_path": plan["rehearsal"]["path"],
            "rehearsal_sha256": plan["rehearsal"]["sha256"],
            "release_metadata": plan["release_metadata"],
            "release_metadata_sha256": plan["release_metadata_sha256"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "APPLY-" + hashlib.sha256(material).hexdigest()[:20].upper()


def _write_plan(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def cleanup_generated_rehearsals(rehearsal_dir: Path, *, keep: int = 5) -> dict[str, Any]:
    """Bound only the disposable copies generated by prepare_release."""
    directory = rehearsal_dir.resolve()
    if keep < 0:
        raise ValueError("keep 不能小于 0")
    if not directory.exists():
        return {"keep": keep, "deleted_count": 0, "deleted_bytes": 0, "errors": []}
    pattern = re.compile(r"^.+_release_rehearsal_\d{8}_\d{6}\.sqlite3$")
    files = sorted(
        (path for path in directory.iterdir() if path.is_file() and pattern.fullmatch(path.name)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    deleted_count = 0
    deleted_bytes = 0
    errors: list[str] = []
    for path in files[keep:]:
        if path.is_symlink() or path.resolve().parent != directory:
            errors.append(f"{path.name}: 路径边界校验失败")
            continue
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            errors.append(f"{path.name}: 存在 SQLite sidecar，已保留")
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            deleted_count += 1
            deleted_bytes += size
        except OSError as error:
            errors.append(f"{path.name}: {error}")
    return {"keep": keep, "deleted_count": deleted_count, "deleted_bytes": deleted_bytes, "errors": errors}


def prepare_release(
    *,
    database: Path,
    backup_dir: Path,
    rehearsal_dir: Path,
    report_path: Path,
    expected_code_sha: str,
    expected_revision: str,
    expected_app_version: str,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    actual_sha = git_sha(project_root)
    if actual_sha != expected_code_sha:
        raise ReleaseGateError(
            f"代码 SHA 不匹配：actual={actual_sha}，expected={expected_code_sha}"
        )
    actual_revision = code_revision(project_root)
    if actual_revision != expected_revision:
        raise ReleaseGateError(
            f"代码 Alembic head 不匹配：actual={actual_revision}，"
            f"expected={expected_revision}"
        )
    metadata = release_metadata(expected_version=expected_app_version)
    metadata_sha256 = release_metadata_sha256(metadata)

    source = inspect_database(database)
    assert_healthy(source, label="正式数据库预检")
    assert_business_smoke(source, label="正式数据库预检")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = database.resolve().stem
    suffix = database.resolve().suffix or ".sqlite3"
    backup_path = backup_dir / f"{stem}_before_release_{timestamp}{suffix}"
    rehearsal_path = rehearsal_dir / f"{stem}_release_rehearsal_{timestamp}{suffix}"

    backup = create_sqlite_copy(database, backup_path)
    assert_business_smoke(backup, label="发布备份")
    if backup["revision"] != source["revision"] or backup["core_counts"] != source["core_counts"]:
        raise ReleaseGateError("备份的 revision 或核心表计数与来源不一致")
    rehearsal_before = create_sqlite_copy(backup_path, rehearsal_path)
    _run_migration(rehearsal_path, expected_revision, project_root)
    rehearsal = inspect_database(rehearsal_path)
    assert_healthy(rehearsal, label="隔离演练数据库")
    assert_revision(rehearsal, expected_revision, label="隔离演练数据库")
    assert_business_smoke(rehearsal, label="隔离演练数据库")
    if rehearsal["core_counts"] != source["core_counts"]:
        raise ReleaseGateError("隔离迁移改变了核心业务表计数，禁止进入正式授权阶段")

    backup_cleanup = result_to_dict(auto_cleanup_regular_backups(backup_dir=backup_dir, keep=5))
    rehearsal_cleanup = cleanup_generated_rehearsals(rehearsal_dir, keep=5)

    plan: dict[str, Any] = {
        "schema_version": 2,
        "status": "awaiting_human_approval",
        "prepared_at": utc_now(),
        "code_sha": actual_sha,
        "expected_revision": expected_revision,
        "expected_app_version": expected_app_version,
        "external_acceptance_required": metadata["external_acceptance_required"],
        "release_metadata": metadata,
        "release_metadata_sha256": metadata_sha256,
        "automated_release_metadata_gate": "passed",
        "completion_scope": "technical_release_only",
        "human_acceptance_status": "not_recorded",
        "source": source,
        "backup": backup,
        "rehearsal_before": rehearsal_before,
        "rehearsal": rehearsal,
        "local_storage_cleanup": {
            "backup": backup_cleanup,
            "rehearsal": rehearsal_cleanup,
        },
        "report_path": str(report_path.resolve()),
    }
    plan["approval_token"] = _approval_token(plan)
    _write_plan(report_path, plan)
    return plan


def apply_release(
    *,
    plan_path: Path,
    approval_token: str,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    plan_path = plan_path.resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != 2:
        raise ReleaseGateError("发布计划版本过旧，必须重新执行 Prepare")
    if plan.get("status") != "awaiting_human_approval":
        raise ReleaseGateError(f"发布计划状态不可执行：{plan.get('status')}")
    if approval_token != plan.get("approval_token"):
        raise ReleaseGateError("人工授权口令不匹配，禁止写入正式数据库")
    if git_sha(project_root) != plan["code_sha"]:
        raise ReleaseGateError("准备与应用阶段之间代码 SHA 已变化")
    if code_revision(project_root) != plan["expected_revision"]:
        raise ReleaseGateError("准备与应用阶段之间 Alembic head 已变化")
    current_metadata = release_metadata(
        expected_version=plan["expected_app_version"],
    )
    if current_metadata != plan["release_metadata"]:
        raise ReleaseGateError("准备与应用阶段之间正式版本说明已变化")
    if release_metadata_sha256(current_metadata) != plan["release_metadata_sha256"]:
        raise ReleaseGateError("准备与应用阶段之间正式版本说明摘要已变化")

    database = Path(plan["source"]["path"])
    current = inspect_database(database)
    assert_healthy(current, label="正式迁移前复检")
    assert_business_smoke(current, label="正式迁移前复检")
    for key in ("path", "sha256", "wal", "revision", "core_counts"):
        if current[key] != plan["source"][key]:
            raise ReleaseGateError(f"准备与应用阶段之间正式数据库 {key} 已变化")

    try:
        _run_migration(database, plan["expected_revision"], project_root)
        applied = inspect_database(database)
        assert_healthy(applied, label="正式迁移后数据库")
        assert_revision(applied, plan["expected_revision"], label="正式迁移后数据库")
        assert_business_smoke(applied, label="正式迁移后数据库")
        if applied["core_counts"] != plan["source"]["core_counts"]:
            raise ReleaseGateError("正式迁移改变了核心业务表计数，禁止自动启动")
    except Exception as error:
        plan["status"] = "apply_failed_service_must_remain_stopped"
        plan["failed_at"] = utc_now()
        plan["error"] = str(error)
        _write_plan(plan_path, plan)
        raise

    plan["status"] = "applied_pending_service_start"
    plan["applied_at"] = utc_now()
    plan["applied"] = applied
    _write_plan(plan_path, plan)
    return plan


def mark_service_started(plan_path: Path) -> dict[str, Any]:
    plan_path = plan_path.resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != 2:
        raise ReleaseGateError("发布计划版本过旧，不能标记发布完成")
    if plan.get("status") != "applied_pending_service_start":
        raise ReleaseGateError(f"发布计划尚不可标记启动：{plan.get('status')}")
    try:
        current_metadata = release_metadata(
            expected_version=plan["expected_app_version"],
        )
        if current_metadata != plan["release_metadata"]:
            raise ReleaseGateError("服务启动后的正式版本说明与发布计划不一致")
        if release_metadata_sha256(current_metadata) != plan["release_metadata_sha256"]:
            raise ReleaseGateError("服务启动后的正式版本说明摘要与发布计划不一致")
    except Exception as error:
        plan["status"] = "service_started_release_metadata_failed"
        plan["release_metadata_failed_at"] = utc_now()
        plan["release_metadata_error"] = str(error)
        _write_plan(plan_path, plan)
        raise
    plan["status"] = "completed"
    plan["service_started_at"] = utc_now()
    plan["verified_release_metadata"] = current_metadata
    _write_plan(plan_path, plan)
    return plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="天明 ERP P0-A 发布与启动安全门禁")
    subparsers = parser.add_subparsers(dest="command", required=True)

    startup = subparsers.add_parser("check-startup", help="只读检查 DB current == 代码 head")
    startup.add_argument("--database", type=Path, required=True)

    metadata = subparsers.add_parser(
        "check-release-metadata",
        help="只读核对版本号、更新说明和验证步骤",
    )
    metadata.add_argument("--expected-version", required=True)

    prepare = subparsers.add_parser("prepare", help="备份并在隔离副本演练，不写正式库")
    prepare.add_argument("--database", type=Path, required=True)
    prepare.add_argument("--backup-dir", type=Path, required=True)
    prepare.add_argument("--rehearsal-dir", type=Path, required=True)
    prepare.add_argument("--report", type=Path, required=True)
    prepare.add_argument("--expected-code-sha", required=True)
    prepare.add_argument("--expected-revision", required=True)
    prepare.add_argument("--expected-app-version", required=True)

    apply = subparsers.add_parser("apply", help="验证计划与人工口令后迁移正式库")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--approval-token", required=True)

    started = subparsers.add_parser("mark-started", help="健康启动后完成发布报告")
    started.add_argument("--plan", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "check-startup":
            result = check_startup(args.database)
        elif args.command == "check-release-metadata":
            result = release_metadata(expected_version=args.expected_version)
        elif args.command == "prepare":
            result = prepare_release(
                database=args.database,
                backup_dir=args.backup_dir,
                rehearsal_dir=args.rehearsal_dir,
                report_path=args.report,
                expected_code_sha=args.expected_code_sha,
                expected_revision=args.expected_revision,
                expected_app_version=args.expected_app_version,
            )
        elif args.command == "apply":
            result = apply_release(
                plan_path=args.plan,
                approval_token=args.approval_token,
            )
        else:
            result = mark_service_started(args.plan)
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
