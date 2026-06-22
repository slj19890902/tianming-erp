from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import RoleChecker, get_db
from app.core.config import load_settings, normalize_path
from app.core.database import (
    backup_to_nas,
    create_sqlite_engine,
    engine,
    restore_from_backup,
)
from app.models.audit import OperationLog
from app.models.user import User


router = APIRouter()
admin_only = RoleChecker(["admin"])


class RestoreRequest(BaseModel):
    filename: str


def _safe_backup_path(backup_dir: Path, filename: str) -> Path:
    normalized_name = filename.strip()
    candidate_name = Path(normalized_name)
    if (
        not normalized_name
        or candidate_name.name != normalized_name
        or "/" in normalized_name
        or "\\" in normalized_name
        or candidate_name.suffix.lower() != ".sqlite3"
    ):
        raise HTTPException(status_code=400, detail="备份文件名无效")
    candidate = normalize_path(backup_dir / normalized_name)
    if candidate.parent != normalize_path(backup_dir):
        raise HTTPException(status_code=400, detail="备份文件名无效")
    return candidate


def _validate_erp_backup(path: Path) -> None:
    try:
        with sqlite3.connect(path, timeout=10) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            statement_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(finance_statements)"
                )
            }
    except sqlite3.DatabaseError as error:
        raise HTTPException(status_code=400, detail="备份文件不是有效数据库") from error
    required_tables = {
        "users",
        "operation_logs",
        "alembic_version",
        "finance_statements",
        "finance_invoices",
        "finance_settlement_records",
    }
    required_columns = {"invoiced_amount", "settled_amount"}
    if (
        not required_tables <= tables
        or not required_columns <= statement_columns
    ):
        raise HTTPException(status_code=400, detail="备份文件不是兼容的 ERP 备份")


def _write_restore_audit(
    *,
    database_path: Path,
    user_id: int,
    username: str,
    role: str,
    request: Request,
    source_filename: str,
    emergency_filename: str,
) -> None:
    audit_engine = create_sqlite_engine(database_path)
    factory = sessionmaker(bind=audit_engine, expire_on_commit=False)
    try:
        with factory() as session:
            restored_user = session.get(User, user_id)
            session.add(
                OperationLog(
                    user_id=restored_user.id if restored_user else None,
                    action="RESTORE_DATABASE",
                    resource="System",
                    details=json.dumps(
                        {
                            "source_backup": source_filename,
                            "pre_restore_backup": emergency_filename,
                        },
                        ensure_ascii=False,
                    ),
                    ip_address=request.client.host if request.client else None,
                    username=username,
                    role=role,
                    entity_type="system",
                    description="管理员执行数据库灾难恢复",
                    user_agent=request.headers.get("user-agent"),
                )
            )
            session.commit()
    finally:
        audit_engine.dispose()


@router.get("/backups")
def list_backups(
    _user: User = Depends(admin_only),
) -> dict:
    current = load_settings()
    backup_dir = current.backup_dir
    if not backup_dir.exists():
        anchor = Path(backup_dir.anchor)
        if backup_dir.anchor and not anchor.exists():
            raise HTTPException(
                status_code=503,
                detail="NAS 备份盘当前不可达",
            )
        return {"items": []}
    try:
        files = sorted(
            (
                path
                for path in backup_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".sqlite3"
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return {
            "items": [
                {
                    "filename": path.name,
                    "created_at": datetime.fromtimestamp(
                        path.stat().st_mtime
                    ).isoformat(timespec="seconds"),
                    "size": path.stat().st_size,
                }
                for path in files
            ]
        }
    except OSError as error:
        raise HTTPException(
            status_code=503,
            detail="无法访问 NAS 备份目录",
        ) from error


@router.post("/backups", status_code=status.HTTP_201_CREATED)
def create_backup(
    _user: User = Depends(admin_only),
) -> dict:
    try:
        result = backup_to_nas()
    except (OSError, sqlite3.DatabaseError, RuntimeError) as error:
        raise HTTPException(
            status_code=503,
            detail="备份失败，请检查数据库和 NAS 连接",
        ) from error
    return {
        "filename": result.path.name,
        "created_at": datetime.fromtimestamp(
            result.path.stat().st_mtime
        ).isoformat(timespec="seconds"),
        "size": result.size,
        "sha256": result.sha256,
        "integrity_check": result.integrity_check,
    }


@router.post("/backups/restore")
def restore_backup(
    payload: RestoreRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    current = load_settings()
    source = _safe_backup_path(current.backup_dir, payload.filename)
    if not source.is_file():
        raise HTTPException(status_code=404, detail="备份文件不存在")
    _validate_erp_backup(source)

    actor = {
        "id": user.id,
        "username": user.username,
        "role": user.role,
    }
    db.rollback()
    db.close()
    engine.dispose()
    try:
        result = restore_from_backup(
            source,
            target_path=current.database_path,
            backup_dir=current.backup_dir,
        )
        _write_restore_audit(
            database_path=current.database_path,
            user_id=actor["id"],
            username=actor["username"],
            role=actor["role"],
            request=request,
            source_filename=result.source_backup.name,
            emergency_filename=result.emergency_backup.name,
        )
    except (OSError, sqlite3.DatabaseError, RuntimeError) as error:
        raise HTTPException(
            status_code=500,
            detail="数据库恢复失败，系统已尝试自动回滚",
        ) from error
    return {
        "ok": True,
        "restored_from": result.source_backup.name,
        "pre_restore_backup": result.emergency_backup.name,
        "integrity_check": result.integrity_check,
    }
