from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import uuid
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.admin.release_erp import (  # noqa: E402
    CORE_COUNT_TABLES,
    REQUIRED_BUSINESS_TABLES,
    ReleaseGateError,
    assert_business_smoke,
    assert_healthy,
    assert_revision,
    code_revision,
    git_sha,
    sha256_file,
)


PACKAGE_KIND = "tianming-erp-weekly-home-uat"
PACKAGE_SCHEMA_VERSION = 1
DATABASE_FILENAME = "carton_erp_factory_snapshot.sqlite3"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_CHECKSUM_FILENAME = "manifest.sha256"
RUNTIME_FILENAME = "runtime.json"
WORKING_DATABASE_FILENAME = "carton_erp_home_uat.sqlite3"
FACTORY_HOSTNAME = "PC-20250926DZYH"
CANONICAL_FACTORY_DATABASE = Path(
    r"D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3"
)
HOME_UAT_PORT_MIN = 18000
HOME_UAT_PORT_MAX = 19999


class WeeklyUatError(RuntimeError):
    """A weekly UAT package or isolation gate failed closed."""


def inspect_database(path: Path) -> dict[str, Any]:
    """Inspect a SQLite file and always release Windows file handles."""

    resolved = path.resolve()
    if not resolved.is_file():
        raise WeeklyUatError(f"数据库文件不存在：{resolved}")
    with closing(
        sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=30)
    ) as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "alembic_version" not in tables:
            raise WeeklyUatError(f"数据库缺少 alembic_version：{resolved}")
        revisions = [
            str(row[0])
            for row in connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            ).fetchall()
        ]
        if len(revisions) != 1:
            raise WeeklyUatError(
                f"数据库必须只有一个 Alembic current，实际为：{revisions}"
            )
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in CORE_COUNT_TABLES
            if table in tables
        }
    return {
        "path": str(resolved),
        "size": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
        "wal": None,
        "revision": revisions[0],
        "integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
        "core_counts": counts,
        "required_tables_missing": sorted(set(REQUIRED_BUSINESS_TABLES) - tables),
    }


def create_sqlite_copy(source: Path, destination: Path) -> dict[str, Any]:
    """Create a committed SQLite snapshot with explicit connection closing."""

    source = source.resolve()
    destination = destination.resolve()
    if _same_path(source, destination):
        raise WeeklyUatError("SQLite 副本目标不能与来源相同")
    if not source.is_file():
        raise WeeklyUatError(f"SQLite 来源不存在：{source}")
    if destination.exists():
        raise WeeklyUatError(f"拒绝覆盖既有 SQLite 文件：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(
            sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=30)
        ) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            source_connection.execute("PRAGMA busy_timeout = 30000")
            with closing(sqlite3.connect(destination, timeout=30)) as target_connection:
                source_connection.backup(target_connection)
                target_connection.commit()
        snapshot = inspect_database(destination)
        assert_healthy(snapshot, label="SQLite 副本")
        return snapshot
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_bytes(_json_bytes(payload))


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _normalized(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _same_path(left: Path, right: Path) -> bool:
    return _normalized(left) == _normalized(right)


def _assert_under(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise WeeklyUatError(
            f"{label}必须位于家庭 UAT 根目录内：path={resolved}，root={resolved_root}"
        ) from error
    return resolved


def _safe_package_id(value: str) -> str:
    candidate = value.strip()
    if not candidate or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for character in candidate
    ):
        raise WeeklyUatError(f"UAT package_id 非法：{candidate!r}")
    return candidate


def _git_branch(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise WeeklyUatError(f"无法读取 Git 分支：{result.stderr.strip()}")
    return result.stdout.strip() or "detached"


def _manifest_database(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": DATABASE_FILENAME,
        "size": int(snapshot["size"]),
        "sha256": str(snapshot["sha256"]),
        "revision": str(snapshot["revision"]),
        "integrity_check": str(snapshot["integrity_check"]),
        "foreign_key_violations": int(snapshot["foreign_key_violations"]),
        "core_counts": dict(snapshot["core_counts"]),
        "required_tables_missing": list(snapshot["required_tables_missing"]),
    }


def export_package(
    *,
    database: Path,
    output_root: Path,
    project_root: Path,
    now: datetime | None = None,
    hostname: str | None = None,
) -> dict[str, Any]:
    database = database.resolve()
    output_root = output_root.resolve()
    project_root = project_root.resolve()
    if not database.is_file():
        raise WeeklyUatError(f"导出源数据库不存在：{database}")
    if not (project_root / ".git").exists() and not (
        project_root / ".git"
    ).is_file():
        raise WeeklyUatError(f"导出代码目录不是 Git 工作树：{project_root}")

    exact_git_sha = git_sha(project_root)
    expected_revision = code_revision(project_root)
    timestamp = (now or datetime.now().astimezone()).astimezone()
    package_id = _safe_package_id(
        f"factory-{timestamp:%Y%m%d-%H%M%S}-{exact_git_sha[:8]}"
    )
    package_dir = output_root / package_id
    if package_dir.exists():
        raise WeeklyUatError(f"拒绝覆盖既有 UAT 包：{package_dir}")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary_dir = output_root / f".{package_id}.{uuid.uuid4().hex}.tmp"
    temporary_dir.mkdir(parents=False, exist_ok=False)
    try:
        target_database = temporary_dir / DATABASE_FILENAME
        snapshot = create_sqlite_copy(database, target_database)
        assert_healthy(snapshot, label="家庭 UAT 导出副本")
        assert_business_smoke(snapshot, label="家庭 UAT 导出副本")
        assert_revision(
            snapshot,
            expected_revision,
            label="家庭 UAT 导出副本",
        )
        manifest = {
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "package_kind": PACKAGE_KIND,
            "package_id": package_id,
            "created_at": timestamp.isoformat(timespec="seconds"),
            "source": {
                "hostname": hostname or socket.gethostname(),
                "git_sha": exact_git_sha,
                "git_branch": _git_branch(project_root),
                "code_alembic_head": expected_revision,
                "database_path": str(database),
            },
            "database": _manifest_database(snapshot),
            "attachments": [],
        }
        manifest_content = _json_bytes(manifest)
        (temporary_dir / MANIFEST_FILENAME).write_bytes(manifest_content)
        (temporary_dir / MANIFEST_CHECKSUM_FILENAME).write_text(
            _sha256_bytes(manifest_content) + "\n",
            encoding="ascii",
        )
        shutil.move(str(temporary_dir), str(package_dir))
        return {
            "package_dir": str(package_dir),
            "package_id": package_id,
            "git_sha": exact_git_sha,
            "revision": snapshot["revision"],
            "database_sha256": snapshot["sha256"],
            "database_size": snapshot["size"],
            "integrity_check": snapshot["integrity_check"],
            "foreign_key_violations": snapshot["foreign_key_violations"],
        }
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


def _load_manifest(package_dir: Path) -> dict[str, Any]:
    manifest_path = package_dir / MANIFEST_FILENAME
    checksum_path = package_dir / MANIFEST_CHECKSUM_FILENAME
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise WeeklyUatError("UAT 包缺少 manifest.json 或 manifest.sha256")
    content = manifest_path.read_bytes()
    expected_checksum = checksum_path.read_text(encoding="ascii").strip().lower()
    actual_checksum = _sha256_bytes(content)
    if expected_checksum != actual_checksum:
        raise WeeklyUatError(
            "UAT manifest 校验失败，文件可能未完整传输或已被修改"
        )
    try:
        manifest = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WeeklyUatError("UAT manifest 不是有效 UTF-8 JSON") from error
    if manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        raise WeeklyUatError("UAT manifest schema_version 不受支持")
    if manifest.get("package_kind") != PACKAGE_KIND:
        raise WeeklyUatError("文件不是天明 ERP 每周家庭 UAT 包")
    package_id = _safe_package_id(str(manifest.get("package_id") or ""))
    if package_dir.name != package_id:
        raise WeeklyUatError(
            f"UAT 包目录名与 package_id 不一致：{package_dir.name} != {package_id}"
        )
    return manifest


def verify_package(package_dir: Path) -> dict[str, Any]:
    package_dir = package_dir.resolve()
    manifest = _load_manifest(package_dir)
    database_meta = manifest.get("database")
    if not isinstance(database_meta, dict):
        raise WeeklyUatError("UAT manifest 缺少 database")
    filename = str(database_meta.get("filename") or "")
    if filename != DATABASE_FILENAME:
        raise WeeklyUatError(f"UAT 数据库文件名非法：{filename}")
    database = package_dir / filename
    if not database.is_file():
        raise WeeklyUatError(f"UAT 包缺少数据库：{database}")
    snapshot = inspect_database(database)
    assert_healthy(snapshot, label="收到的家庭 UAT 包")
    expected = {
        "size": int(database_meta.get("size", -1)),
        "sha256": str(database_meta.get("sha256") or "").lower(),
        "revision": str(database_meta.get("revision") or ""),
        "integrity_check": str(database_meta.get("integrity_check") or ""),
        "foreign_key_violations": int(
            database_meta.get("foreign_key_violations", -1)
        ),
        "core_counts": database_meta.get("core_counts"),
        "required_tables_missing": database_meta.get("required_tables_missing"),
    }
    actual = {
        "size": int(snapshot["size"]),
        "sha256": str(snapshot["sha256"]).lower(),
        "revision": str(snapshot["revision"]),
        "integrity_check": str(snapshot["integrity_check"]),
        "foreign_key_violations": int(snapshot["foreign_key_violations"]),
        "core_counts": snapshot["core_counts"],
        "required_tables_missing": snapshot["required_tables_missing"],
    }
    if actual != expected:
        raise WeeklyUatError(
            "UAT 数据库与 manifest 不一致："
            f"expected={expected}，actual={actual}"
        )
    return {
        "manifest": manifest,
        "database": database,
        "snapshot": snapshot,
    }


def _assert_home_computer(hostname: str | None = None) -> str:
    current = (hostname or socket.gethostname()).strip()
    if current.casefold() == FACTORY_HOSTNAME.casefold():
        raise WeeklyUatError("家庭 UAT 工具禁止在工厂正式主机运行")
    return current


def _copy_exact(source: Path, destination: Path) -> None:
    if destination.exists():
        raise WeeklyUatError(f"拒绝覆盖既有文件：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        if sha256_file(temporary) != sha256_file(source):
            raise WeeklyUatError("数据库复制后 SHA-256 不一致")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def import_package(
    *,
    package_dir: Path,
    uat_root: Path,
    project_root: Path,
    hostname: str | None = None,
) -> dict[str, Any]:
    home_hostname = _assert_home_computer(hostname)
    package_dir = package_dir.resolve()
    uat_root = uat_root.resolve()
    project_root = project_root.resolve()
    verified = verify_package(package_dir)
    manifest = verified["manifest"]
    source_database = verified["database"]
    package_id = _safe_package_id(manifest["package_id"])
    manifest_git_sha = str(manifest["source"].get("git_sha") or "")
    current_git_sha = git_sha(project_root)
    if current_git_sha != manifest_git_sha:
        raise WeeklyUatError(
            "家庭 UAT 代码 SHA 与工厂数据包不一致："
            f"code={current_git_sha}，package={manifest_git_sha}"
        )
    current_code_revision = code_revision(project_root)
    database_revision = str(manifest["database"].get("revision") or "")
    if current_code_revision != database_revision:
        raise WeeklyUatError(
            "家庭 UAT 代码 head 与工厂数据库 revision 不一致："
            f"code={current_code_revision}，database={database_revision}"
        )

    received_dir = _assert_under(
        uat_root / "received" / package_id,
        uat_root,
        label="收到件目录",
    )
    if received_dir.exists():
        received_verified = verify_package(received_dir)
        if (
            received_verified["snapshot"]["sha256"]
            != verified["snapshot"]["sha256"]
        ):
            raise WeeklyUatError("既有只读收到件与本次导入包不一致")
    else:
        received_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary_container = received_dir.parent / (
            f".{package_id}.{uuid.uuid4().hex}.incoming"
        )
        temporary_received = temporary_container / package_id
        try:
            shutil.copytree(package_dir, temporary_received)
            verify_package(temporary_received)
            shutil.move(str(temporary_received), str(received_dir))
        except Exception:
            shutil.rmtree(temporary_container, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(temporary_container, ignore_errors=True)

    received_database = received_dir / DATABASE_FILENAME
    received_database.chmod(stat.S_IREAD)
    run_dir = _assert_under(
        uat_root / "runs" / package_id,
        uat_root,
        label="UAT 运行目录",
    )
    working_database = run_dir / WORKING_DATABASE_FILENAME
    if not working_database.exists():
        _copy_exact(received_database, working_database)
        working_database.chmod(stat.S_IREAD | stat.S_IWRITE)
    if sha256_file(working_database) != sha256_file(received_database):
        existing_working_copy = True
    else:
        existing_working_copy = False
    working_snapshot = inspect_database(working_database)
    assert_healthy(working_snapshot, label="家庭 UAT 工作副本")
    assert_revision(
        working_snapshot,
        database_revision,
        label="家庭 UAT 工作副本",
    )

    runtime = {
        "schema_version": 1,
        "package_id": package_id,
        "home_hostname": home_hostname,
        "imported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_root": str(project_root),
        "git_sha": current_git_sha,
        "revision": database_revision,
        "received_dir": str(received_dir),
        "received_database": str(received_database),
        "received_database_sha256": sha256_file(received_database),
        "working_database": str(working_database),
    }
    _write_json(run_dir / RUNTIME_FILENAME, runtime)
    return {
        **runtime,
        "runtime_file": str(run_dir / RUNTIME_FILENAME),
        "existing_working_copy": existing_working_copy,
        "integrity_check": working_snapshot["integrity_check"],
        "foreign_key_violations": working_snapshot["foreign_key_violations"],
    }


def _load_runtime(runtime_file: Path, uat_root: Path) -> dict[str, Any]:
    runtime_file = _assert_under(runtime_file, uat_root, label="UAT runtime 文件")
    if runtime_file.name != RUNTIME_FILENAME or not runtime_file.is_file():
        raise WeeklyUatError(f"UAT runtime 文件不存在或命名错误：{runtime_file}")
    try:
        runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WeeklyUatError("UAT runtime 文件损坏") from error
    if runtime.get("schema_version") != 1:
        raise WeeklyUatError("UAT runtime schema_version 不受支持")
    return runtime


def reset_working_copy(
    *,
    runtime_file: Path,
    uat_root: Path,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise WeeklyUatError("重置会清除家庭工作副本中的测试操作，必须显式确认")
    uat_root = uat_root.resolve()
    runtime = _load_runtime(runtime_file, uat_root)
    received_database = _assert_under(
        Path(runtime["received_database"]),
        uat_root / "received",
        label="只读收到件",
    )
    working_database = _assert_under(
        Path(runtime["working_database"]),
        uat_root / "runs",
        label="家庭 UAT 工作副本",
    )
    if working_database.name != WORKING_DATABASE_FILENAME:
        raise WeeklyUatError("拒绝重置非标准命名的数据库文件")
    if not received_database.is_file() or not working_database.is_file():
        raise WeeklyUatError("收到件或工作副本不存在，拒绝重置")
    received_sha256 = sha256_file(received_database)
    if received_sha256 != runtime.get("received_database_sha256"):
        raise WeeklyUatError("只读收到件 SHA-256 已变化，拒绝重置")
    working_database.chmod(stat.S_IREAD | stat.S_IWRITE)
    # Reset intentionally discards only this disposable UAT database's sidecars.
    # The paths are derived after the strict runs-root and filename gates above.
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(working_database) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    temporary = working_database.with_name(
        f".{working_database.name}.{uuid.uuid4().hex}.reset"
    )
    try:
        shutil.copy2(received_database, temporary)
        temporary.chmod(stat.S_IREAD | stat.S_IWRITE)
        if sha256_file(temporary) != received_sha256:
            raise WeeklyUatError("重置临时副本 SHA-256 不一致")
        os.replace(temporary, working_database)
        working_database.chmod(stat.S_IREAD | stat.S_IWRITE)
    finally:
        if temporary.exists():
            temporary.chmod(stat.S_IREAD | stat.S_IWRITE)
            temporary.unlink(missing_ok=True)
    snapshot = inspect_database(working_database)
    assert_healthy(snapshot, label="重置后的家庭 UAT 工作副本")
    runtime["last_reset_at"] = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    _write_json(runtime_file.resolve(), runtime)
    return {
        "working_database": str(working_database),
        "sha256": snapshot["sha256"],
        "revision": snapshot["revision"],
        "integrity_check": snapshot["integrity_check"],
        "foreign_key_violations": snapshot["foreign_key_violations"],
        "last_reset_at": runtime["last_reset_at"],
    }


def validate_home_runtime(
    *,
    database: Path,
    uat_root: Path,
    project_root: Path,
    port: int,
    environment: str = "test",
    bind_host: str = "127.0.0.1",
    workers: int = 1,
    hostname: str | None = None,
) -> dict[str, Any]:
    current_hostname = _assert_home_computer(hostname)
    if environment != "test":
        raise WeeklyUatError("家庭副 ERP 只允许 ERP_ENVIRONMENT=test")
    if bind_host != "127.0.0.1":
        raise WeeklyUatError("家庭副 ERP 只允许监听 127.0.0.1")
    if not HOME_UAT_PORT_MIN <= int(port) <= HOME_UAT_PORT_MAX or int(port) == 8000:
        raise WeeklyUatError("家庭副 ERP 端口必须在 18000～19999 且不能为 8000")
    if int(workers) != 1:
        raise WeeklyUatError("家庭副 ERP 必须使用单 worker")
    uat_root = uat_root.resolve()
    database = _assert_under(
        database,
        uat_root / "runs",
        label="家庭副 ERP 数据库",
    )
    project_root = project_root.resolve()
    protected = (
        CANONICAL_FACTORY_DATABASE,
        project_root / "data" / "carton_erp.sqlite3",
    )
    if any(_same_path(database, item) for item in protected):
        raise WeeklyUatError(f"家庭副 ERP 禁止连接正式数据库：{database}")
    runtime_file = database.parent / RUNTIME_FILENAME
    runtime = _load_runtime(runtime_file, uat_root)
    if not _same_path(Path(runtime["working_database"]), database):
        raise WeeklyUatError("runtime 记录的工作数据库与启动目标不一致")
    if runtime.get("git_sha") != git_sha(project_root):
        raise WeeklyUatError("家庭副 ERP 代码 SHA 与 runtime 不一致")
    if runtime.get("home_hostname") != current_hostname:
        raise WeeklyUatError("家庭副 ERP runtime 不属于当前电脑")
    snapshot = inspect_database(database)
    assert_healthy(snapshot, label="家庭副 ERP 启动数据库")
    assert_revision(snapshot, code_revision(project_root), label="家庭副 ERP 启动数据库")
    return {
        "database": str(database),
        "git_sha": runtime["git_sha"],
        "revision": snapshot["revision"],
        "port": int(port),
        "bind_host": bind_host,
        "environment": environment,
        "workers": int(workers),
        "hostname": current_hostname,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="天明 ERP 每周工厂真实数据家庭副 ERP 工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="工厂侧创建一致性 UAT 包")
    export.add_argument("--database", type=Path, required=True)
    export.add_argument("--output-root", type=Path, required=True)
    export.add_argument("--project-root", type=Path, default=PROJECT_ROOT)

    verify = subparsers.add_parser("verify", help="只读验证 UAT 包")
    verify.add_argument("--package-dir", type=Path, required=True)

    receive = subparsers.add_parser("import", help="家庭侧导入并建立工作副本")
    receive.add_argument("--package-dir", type=Path, required=True)
    receive.add_argument("--uat-root", type=Path, required=True)
    receive.add_argument("--project-root", type=Path, default=PROJECT_ROOT)

    reset = subparsers.add_parser("reset", help="从只读收到件重置工作副本")
    reset.add_argument("--runtime-file", type=Path, required=True)
    reset.add_argument("--uat-root", type=Path, required=True)
    reset.add_argument("--confirm-reset", action="store_true")

    check = subparsers.add_parser("check-start", help="家庭副 ERP 启动前隔离检查")
    check.add_argument("--database", type=Path, required=True)
    check.add_argument("--uat-root", type=Path, required=True)
    check.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    check.add_argument("--port", type=int, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "export":
            result = export_package(
                database=args.database,
                output_root=args.output_root,
                project_root=args.project_root,
            )
        elif args.command == "verify":
            verified = verify_package(args.package_dir)
            result = {
                "package_id": verified["manifest"]["package_id"],
                "git_sha": verified["manifest"]["source"]["git_sha"],
                "revision": verified["snapshot"]["revision"],
                "database_sha256": verified["snapshot"]["sha256"],
                "integrity_check": verified["snapshot"]["integrity_check"],
                "foreign_key_violations": verified["snapshot"][
                    "foreign_key_violations"
                ],
            }
        elif args.command == "import":
            result = import_package(
                package_dir=args.package_dir,
                uat_root=args.uat_root,
                project_root=args.project_root,
            )
        elif args.command == "reset":
            result = reset_working_copy(
                runtime_file=args.runtime_file,
                uat_root=args.uat_root,
                confirmed=args.confirm_reset,
            )
        elif args.command == "check-start":
            result = validate_home_runtime(
                database=args.database,
                uat_root=args.uat_root,
                project_root=args.project_root,
                port=args.port,
            )
        else:  # pragma: no cover
            raise WeeklyUatError(f"未知命令：{args.command}")
    except (WeeklyUatError, ReleaseGateError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
