from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.backup_retention import auto_cleanup_regular_backups
from app.core.config import Settings, load_settings, normalize_path, settings


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    integrity_check: str
    sha256: str
    size: int
    cleanup_deleted_count: int = 0
    cleanup_deleted_bytes: int = 0
    cleanup_error: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreResult:
    source_backup: Path
    target_path: Path
    emergency_backup: Path
    integrity_check: str


def create_sqlite_engine(
    database_path: Path,
    *,
    busy_timeout_ms: int = 5_000,
) -> Engine:
    path = normalize_path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite+pysqlite:///{path.as_posix()}",
        connect_args={
            "check_same_thread": False,
            "timeout": max(busy_timeout_ms / 1_000, 1),
        },
        future=True,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: sqlite3.Connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            cursor.execute("PRAGMA journal_mode = DELETE")
        finally:
            cursor.close()

    return engine


def create_engine_from_settings(app_settings: Settings | None = None) -> Engine:
    current = app_settings or load_settings()
    return create_sqlite_engine(
        current.database_path,
        busy_timeout_ms=current.sqlite_busy_timeout_ms,
    )


engine = create_sqlite_engine(
    settings.database_path,
    busy_timeout_ms=settings.sqlite_busy_timeout_ms,
)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for block in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_integrity_check(path: Path) -> str:
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def _copy_sqlite(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source, timeout=30)) as source_connection:
        source_connection.execute("PRAGMA busy_timeout = 30000")
        with closing(sqlite3.connect(destination, timeout=30)) as target_connection:
            target_connection.execute("PRAGMA busy_timeout = 30000")
            source_connection.backup(target_connection)
            target_connection.commit()


def backup_to_nas(
    *,
    source_path: Path | None = None,
    backup_dir: Path | None = None,
    filename_suffix: str = "",
    keep_regular: int = 5,
) -> BackupResult:
    current = load_settings()
    source = normalize_path(source_path or current.database_path)
    destination_dir = normalize_path(backup_dir or current.backup_dir)
    if not source.is_file():
        raise FileNotFoundError(f"数据库文件不存在: {source}")

    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if filename_suffix and not re.fullmatch(r"_[A-Za-z0-9_-]+", filename_suffix):
        raise ValueError("备份文件后缀只能包含字母、数字、下划线和连字符")
    destination = (
        destination_dir / f"carton_erp_{timestamp}{filename_suffix}.sqlite3"
    )
    temporary = destination.with_suffix(".sqlite3.tmp")

    try:
        with closing(sqlite3.connect(source, timeout=30)) as source_connection:
            source_connection.execute("PRAGMA busy_timeout = 30000")
            with closing(sqlite3.connect(temporary)) as backup_connection:
                source_connection.backup(backup_connection)
                backup_connection.commit()

        integrity_check = _sqlite_integrity_check(temporary)
        if integrity_check.lower() != "ok":
            raise RuntimeError(f"备份完整性检查失败: {integrity_check}")

        temporary.replace(destination)
        cleanup_deleted_count = 0
        cleanup_deleted_bytes = 0
        cleanup_error: str | None = None
        try:
            cleanup_result = auto_cleanup_regular_backups(
                backup_dir=destination_dir,
                keep=keep_regular,
            )
            cleanup_deleted_count = cleanup_result.deleted_count
            cleanup_deleted_bytes = cleanup_result.deleted_bytes
            if cleanup_result.errors:
                cleanup_error = "; ".join(cleanup_result.errors)
        except Exception as error:
            cleanup_error = str(error)
        return BackupResult(
            path=destination,
            integrity_check=integrity_check,
            sha256=_sha256(destination),
            size=destination.stat().st_size,
            cleanup_deleted_count=cleanup_deleted_count,
            cleanup_deleted_bytes=cleanup_deleted_bytes,
            cleanup_error=cleanup_error,
        )
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except PermissionError:
            pass
        raise


def restore_from_backup(
    backup_path: Path,
    *,
    target_path: Path | None = None,
    backup_dir: Path | None = None,
) -> RestoreResult:
    current = load_settings()
    backup_root = normalize_path(backup_dir or current.backup_dir)
    source = normalize_path(backup_path)
    target = normalize_path(target_path or current.database_path)

    if source.parent != backup_root:
        raise ValueError("恢复文件必须位于配置的备份目录中")
    if source.suffix.lower() != ".sqlite3" or not source.is_file():
        raise FileNotFoundError(f"备份文件不存在或格式无效: {source}")
    if not target.is_file():
        raise FileNotFoundError(f"当前数据库文件不存在: {target}")

    source_integrity = _sqlite_integrity_check(source)
    if source_integrity.lower() != "ok":
        raise RuntimeError(f"恢复源完整性检查失败: {source_integrity}")

    emergency = backup_to_nas(
        source_path=target,
        backup_dir=backup_root,
        filename_suffix="_pre_restore",
    )
    try:
        _copy_sqlite(source, target)
        target_integrity = _sqlite_integrity_check(target)
        if target_integrity.lower() != "ok":
            raise RuntimeError(f"恢复后完整性检查失败: {target_integrity}")
    except Exception as restore_error:
        try:
            _copy_sqlite(emergency.path, target)
            rollback_integrity = _sqlite_integrity_check(target)
            if rollback_integrity.lower() != "ok":
                raise RuntimeError(
                    f"紧急回滚完整性检查失败: {rollback_integrity}"
                )
        except Exception as rollback_error:
            raise RuntimeError(
                "数据库恢复失败，且自动回滚失败；请保留现场并使用 "
                f"{emergency.path} 手工恢复"
            ) from rollback_error
        raise RuntimeError("数据库恢复失败，已自动回滚到恢复前状态") from restore_error

    return RestoreResult(
        source_backup=source,
        target_path=target,
        emergency_backup=emergency.path,
        integrity_check=target_integrity,
    )
