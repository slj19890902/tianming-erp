from __future__ import annotations

import json
import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, status
import jwt
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PermissionChecker, RoleChecker, get_current_user, get_db
from app.core.time_contract import (
    beijing_naive_to_api,
    beijing_now_naive,
    utc_naive_to_api,
)
from app.core.config import load_settings, normalize_path
from app.core.database import (
    backup_to_nas,
    create_sqlite_engine,
    engine,
    restore_from_backup,
)
from app.models.audit import OperationLog
from app.models.company_config import CompanyConfig
from app.models.user import User
from app.services.delivery_print_settings import (
    get_delivery_print_settings,
    save_delivery_print_settings,
)

logger = logging.getLogger(__name__)

router = APIRouter()
admin_only = RoleChecker(["admin"])
can_backup = PermissionChecker("system.backup")

# 始终保留最新 N 个备份，不允许删除
BACKUP_KEEP_COUNT = 5


class RestoreRequest(BaseModel):
    filename: str


class DeleteRequest(BaseModel):
    filename: str


class CompanyConfigUpdate(BaseModel):
    company_name: str
    short_name: str | None = None
    address: str | None = None
    phone: str | None = None
    fax: str | None = None
    tax_number: str | None = None
    bank_name: str | None = None
    bank_account: str | None = None
    contact_person: str | None = None
    contact_phone: str | None = None

    @field_validator("company_name")
    @classmethod
    def validate_company_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("公司名称不能为空")
        return normalized


class DeliveryPrintSettingsUpdate(BaseModel):
    paper_width_mm: float
    paper_height_mm: float


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


def _list_backup_files(backup_dir: Path) -> list[Path]:
    """按修改时间降序返回备份目录下所有 .sqlite3 文件。"""
    return sorted(
        (
            path
            for path in backup_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".sqlite3"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _backup_location_type(backup_dir: Path) -> str:
    """判断备份目录类型：nas（UNC 路径）或 local。"""
    path_str = str(backup_dir)
    if path_str.startswith("\\\\") or path_str.startswith("//"):
        return "nas"
    return "local"


def _is_live_db(path: Path, database_path: Path) -> bool:
    """判断文件路径是否与正在运行的数据库相同。"""
    try:
        return path.resolve() == database_path.resolve()
    except OSError:
        return False


@router.get("/backups")
def list_backups(
    _user: User = Depends(can_backup),
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
        return {
            "items": [],
            "backup_dir": str(backup_dir),
            "location_type": _backup_location_type(backup_dir),
            "total_count": 0,
            "total_size": 0,
            "protected_count": 0,
            "deletable_count": 0,
        }
    try:
        files = _list_backup_files(backup_dir)
        items = []
        for index, path in enumerate(files):
            stat = path.stat()
            is_protected = index < BACKUP_KEEP_COUNT
            items.append(
                {
                    "filename": path.name,
                    "created_at": utc_naive_to_api(
                        datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(tzinfo=None)
                    ),
                    "size": stat.st_size,
                    "is_protected": is_protected,
                }
            )
        total_size = sum(item["size"] for item in items)
        protected_count = min(len(items), BACKUP_KEEP_COUNT)
        deletable_count = max(0, len(items) - BACKUP_KEEP_COUNT)
        return {
            "items": items,
            "backup_dir": str(backup_dir),
            "location_type": _backup_location_type(backup_dir),
            "total_count": len(items),
            "total_size": total_size,
            "protected_count": protected_count,
            "deletable_count": deletable_count,
        }
    except OSError as error:
        raise HTTPException(
            status_code=503,
            detail="无法访问备份目录",
        ) from error


@router.post("/backups", status_code=status.HTTP_201_CREATED)
def create_backup(
    _user: User = Depends(can_backup),
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
        "created_at": utc_naive_to_api(
            datetime.fromtimestamp(
                result.path.stat().st_mtime, timezone.utc
            ).replace(tzinfo=None)
        ),
        "size": result.size,
        "sha256": result.sha256,
        "integrity_check": result.integrity_check,
    }


@router.get("/backups/cleanup-preview")
def cleanup_preview(
    _user: User = Depends(can_backup),
) -> dict:
    """预览哪些备份会被清理（不执行删除）。"""
    current = load_settings()
    backup_dir = current.backup_dir
    if not backup_dir.exists():
        raise HTTPException(status_code=503, detail="备份目录不可达")
    try:
        files = _list_backup_files(backup_dir)
    except OSError as error:
        raise HTTPException(status_code=503, detail="无法访问备份目录") from error

    will_keep = []
    will_delete = []
    for index, path in enumerate(files):
        stat = path.stat()
        entry = {
            "filename": path.name,
            "created_at": utc_naive_to_api(
                datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(tzinfo=None)
            ),
            "size": stat.st_size,
        }
        if index < BACKUP_KEEP_COUNT:
            will_keep.append(entry)
        else:
            will_delete.append(entry)

    freed_size = sum(item["size"] for item in will_delete)
    return {
        "will_keep": will_keep,
        "will_delete": will_delete,
        "freed_size": freed_size,
        "deletable_count": len(will_delete),
    }


@router.post("/backups/cleanup")
def cleanup_backups(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_backup),
) -> dict:
    """一键清理旧备份，保留最新 BACKUP_KEEP_COUNT 个。"""
    current = load_settings()
    backup_dir = current.backup_dir
    if not backup_dir.exists():
        raise HTTPException(status_code=503, detail="备份目录不可达")
    try:
        files = _list_backup_files(backup_dir)
    except OSError as error:
        raise HTTPException(status_code=503, detail="无法访问备份目录") from error

    to_delete = files[BACKUP_KEEP_COUNT:]
    deleted_files: list[str] = []
    freed_size = 0
    errors: list[str] = []

    for path in to_delete:
        try:
            size = path.stat().st_size
            path.unlink()
            freed_size += size
            deleted_files.append(path.name)
            logger.info("备份已删除: %s，释放 %d 字节", path.name, size)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            logger.warning("删除备份失败: %s — %s", path.name, exc)

    # 审计日志
    db.add(
        OperationLog(
            user_id=user.id,
            action="CLEANUP_BACKUPS",
            resource="System",
            details=json.dumps(
                {
                    "deleted_files": deleted_files,
                    "freed_size": freed_size,
                    "errors": errors,
                },
                ensure_ascii=False,
            ),
            ip_address=request.client.host if request.client else None,
            username=user.username,
            role=user.role,
            entity_type="system",
            description=f"管理员批量清理旧备份，共删除 {len(deleted_files)} 个",
            user_agent=request.headers.get("user-agent"),
        )
    )
    db.commit()

    return {
        "deleted": deleted_files,
        "freed_size": freed_size,
        "errors": errors,
    }


@router.delete("/backups/{filename}")
def delete_backup(
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_backup),
) -> dict:
    """删除单个备份文件（禁止删除受保护的最新 BACKUP_KEEP_COUNT 个）。"""
    current = load_settings()
    backup_dir = current.backup_dir

    # 路径安全校验优先（返回 400，与目录是否存在无关）
    target = _safe_backup_path(backup_dir, filename)

    if not backup_dir.exists():
        raise HTTPException(status_code=503, detail="备份目录不可达")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="备份文件不存在")

    # 禁止删除正在使用的数据库文件
    if _is_live_db(target, current.database_path):
        raise HTTPException(status_code=409, detail="不能删除正在使用的数据库文件")

    # 确认文件是否在保护范围内（最新 BACKUP_KEEP_COUNT 个）
    try:
        files = _list_backup_files(backup_dir)
    except OSError as error:
        raise HTTPException(status_code=503, detail="无法访问备份目录") from error

    protected_names = {path.name for path in files[:BACKUP_KEEP_COUNT]}
    if filename in protected_names:
        raise HTTPException(
            status_code=409,
            detail=f"该备份在最新 {BACKUP_KEEP_COUNT} 个受保护备份中，不允许删除",
        )

    try:
        freed_size = target.stat().st_size
        target.unlink()
        logger.info("备份已删除: %s，释放 %d 字节（操作人: %s）", filename, freed_size, user.username)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="删除文件失败") from exc

    # 审计日志
    db.add(
        OperationLog(
            user_id=user.id,
            action="DELETE_BACKUP",
            resource="System",
            details=json.dumps(
                {"filename": filename, "freed_size": freed_size},
                ensure_ascii=False,
            ),
            ip_address=request.client.host if request.client else None,
            username=user.username,
            role=user.role,
            entity_type="system",
            description=f"管理员删除备份文件 {filename}",
            user_agent=request.headers.get("user-agent"),
        )
    )
    db.commit()

    return {"deleted": filename, "freed_size": freed_size}


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


# ─────────────────────────────────────────────────────────────
# 材质代码映射候选表 API
# ─────────────────────────────────────────────────────────────

class ReviewStatusUpdate(BaseModel):
    review_status: str          # pending / approved / rejected
    review_note: str | None = None


class BatchVersionConfirmation(BaseModel):
    preview_token: str = Field(min_length=1)
    confirmation_tokens: dict[str, str]


SYSTEM_BATCH_TOKEN_TTL_MINUTES = 5


def _batch_actor(user: User) -> str:
    if user.id is not None:
        return f"id:{user.id}"
    return f"username:{user.username}"


def _batch_digest(value: Any) -> str:
    from app.services.master_data_versioning import canonical_json

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalized_batch_plan(items: list[dict]) -> list[dict]:
    plan = sorted((dict(item) for item in items), key=lambda item: item["key"])
    keys = [str(item["key"]) for item in plan]
    if len(keys) != len(set(keys)):
        raise ValueError("系统批量预览包含重复对象")
    return plan


def _encode_batch_token(
    *,
    operation: str,
    user: User,
    scope: str,
    digest: str,
    object_key: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": _batch_actor(user),
        "type": "system_batch_preview_confirmation",
        "operation": operation,
        "scope": scope,
        "digest": digest,
        "iat": now,
        "exp": now + timedelta(minutes=SYSTEM_BATCH_TOKEN_TTL_MINUTES),
    }
    if object_key is not None:
        claims["object_key"] = object_key
    return jwt.encode(claims, load_settings().secret_key, algorithm="HS256")


def _batch_preview_confirmation(
    operation: str,
    items: list[dict],
    user: User,
) -> dict[str, Any]:
    plan = _normalized_batch_plan(items)
    return {
        "preview_token": _encode_batch_token(
            operation=operation,
            user=user,
            scope="plan",
            digest=_batch_digest(plan),
        ),
        "confirmation_tokens": {
            item["key"]: _encode_batch_token(
                operation=operation,
                user=user,
                scope="object",
                object_key=item["key"],
                digest=_batch_digest(item),
            )
            for item in plan
        },
    }


def _batch_confirmation_error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _decode_batch_token(token: str) -> Mapping[str, Any]:
    try:
        return jwt.decode(
            token,
            load_settings().secret_key,
            algorithms=["HS256"],
            options={
                "require": [
                    "sub",
                    "type",
                    "operation",
                    "scope",
                    "digest",
                    "iat",
                    "exp",
                ]
            },
        )
    except jwt.PyJWTError as exc:
        raise _batch_confirmation_error(
            "SYSTEM_BATCH_PREVIEW_STALE",
            "批量预览确认已失效，请重新预览",
        ) from exc


def _require_batch_confirmation(
    body: BatchVersionConfirmation,
    *,
    operation: str,
    items: list[dict],
    user: User,
) -> None:
    plan = _normalized_batch_plan(items)
    expected_plan = {
        "sub": _batch_actor(user),
        "type": "system_batch_preview_confirmation",
        "operation": operation,
        "scope": "plan",
        "digest": _batch_digest(plan),
    }
    preview_claims = _decode_batch_token(body.preview_token)
    if any(preview_claims.get(key) != value for key, value in expected_plan.items()):
        raise _batch_confirmation_error(
            "SYSTEM_BATCH_PREVIEW_STALE",
            "批量对象或版本已变化，请重新预览",
        )

    expected_keys = {str(item["key"]) for item in plan}
    if set(body.confirmation_tokens) != expected_keys:
        raise _batch_confirmation_error(
            "SYSTEM_BATCH_OBJECT_CONFIRMATION_REQUIRED",
            "必须提交本次预览中的全部对象确认 token",
        )
    for item in plan:
        object_key = str(item["key"])
        claims = _decode_batch_token(body.confirmation_tokens[object_key])
        expected_object = {
            "sub": _batch_actor(user),
            "type": "system_batch_preview_confirmation",
            "operation": operation,
            "scope": "object",
            "object_key": object_key,
            "digest": _batch_digest(item),
        }
        if any(claims.get(key) != value for key, value in expected_object.items()):
            raise _batch_confirmation_error(
                "SYSTEM_BATCH_PREVIEW_STALE",
                f"对象 {object_key} 的确认已失效，请重新预览",
            )


@router.get("/material-mapping/stats", dependencies=[Depends(admin_only)])
def material_mapping_stats(db: Session = Depends(get_db)):
    """汇总材质映射候选表统计数据。"""
    from app.models.material_mapping import MaterialCodeMappingCandidate
    from app.models.product import Product
    from sqlalchemy import func as sqlfunc, select

    total = db.scalar(
        select(sqlfunc.count()).select_from(MaterialCodeMappingCandidate)
    ) or 0

    rows = db.execute(
        select(
            MaterialCodeMappingCandidate.confidence_level,
            MaterialCodeMappingCandidate.review_status,
            sqlfunc.count().label("cnt"),
        ).group_by(
            MaterialCodeMappingCandidate.confidence_level,
            MaterialCodeMappingCandidate.review_status,
        )
    ).fetchall()

    by_confidence: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for row in rows:
        by_confidence[row.confidence_level] = (
            by_confidence.get(row.confidence_level, 0) + row.cnt
        )
        by_status[row.review_status] = (
            by_status.get(row.review_status, 0) + row.cnt
        )

    # 命中产品数：products.legacy_material_text 中可匹配到 old_code 的数量
    products_affected = db.scalar(
        select(sqlfunc.count()).select_from(Product).where(
            Product.deleted_at.is_(None),
            Product.material_id.is_(None),
            Product.legacy_material_text.isnot(None),
        )
    ) or 0

    return {
        "total": total,
        "by_confidence": by_confidence,
        "by_status": by_status,
        "products_pending_material": products_affected,
    }


@router.get("/material-mapping", dependencies=[Depends(admin_only)])
def list_material_mapping(
    confidence_level: str | None = None,
    review_status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    """分页列出材质映射候选记录，支持按可信度和审批状态过滤。"""
    from app.models.material_mapping import MaterialCodeMappingCandidate
    from app.models.product import Product
    from sqlalchemy import select, func as sqlfunc

    q = select(MaterialCodeMappingCandidate)
    if confidence_level:
        q = q.where(MaterialCodeMappingCandidate.confidence_level == confidence_level)
    if review_status:
        q = q.where(MaterialCodeMappingCandidate.review_status == review_status)

    total = db.scalar(
        select(sqlfunc.count()).select_from(q.subquery())
    ) or 0

    offset = (page - 1) * page_size
    candidates = db.scalars(
        q.order_by(
            MaterialCodeMappingCandidate.confidence_level,
            MaterialCodeMappingCandidate.old_code,
        ).offset(offset).limit(page_size)
    ).all()

    # 计算每条候选的命中产品数（products with legacy_material_text containing old_code）
    # 批量查询：取这批 old_codes，统计每个匹配到的产品数
    old_codes = [c.old_code for c in candidates]
    hit_counts: dict[str, int] = {}
    if old_codes:
        products_with_legacy = db.scalars(
            select(Product).where(
                Product.deleted_at.is_(None),
                Product.legacy_material_text.isnot(None),
            )
        ).all()
        import re
        for p in products_with_legacy:
            legacy = p.legacy_material_text or ""
            # 去掉前缀数字
            stripped = re.sub(r"^\d+\s+", "", legacy).strip()
            for oc in old_codes:
                if oc == stripped or oc == legacy.strip():
                    hit_counts[oc] = hit_counts.get(oc, 0) + 1

    items = []
    for c in candidates:
        items.append({
            "id": c.id,
            "old_code": c.old_code,
            "new_code": c.new_code,
            "old_supplier": c.old_supplier,
            "new_supplier": c.new_supplier,
            "layer_count": c.layer_count,
            "weight_structure": c.weight_structure,
            "new_price": float(c.new_price) if c.new_price is not None else None,
            "old_price": float(c.old_price) if c.old_price is not None else None,
            "confidence_label": c.confidence_label,
            "confidence_level": c.confidence_level,
            "review_status": c.review_status,
            "review_note": c.review_note,
            "source_file": c.source_file,
            "source_row_number": c.source_row_number,
            "created_at": utc_naive_to_api(c.created_at),
            "hit_count": hit_counts.get(c.old_code, 0),
        })

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    }


@router.put(
    "/material-mapping/{candidate_id}/status",
    dependencies=[Depends(admin_only)],
)
def update_mapping_status(
    candidate_id: int,
    body: ReviewStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    """更新候选记录的审批状态。"""
    from app.models.material_mapping import MaterialCodeMappingCandidate
    from sqlalchemy import select

    valid_statuses = {"pending", "approved", "rejected"}
    if body.review_status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"无效状态，必须是 {sorted(valid_statuses)} 之一",
        )

    cand = db.scalar(
        select(MaterialCodeMappingCandidate).where(
            MaterialCodeMappingCandidate.id == candidate_id
        )
    )
    if cand is None:
        raise HTTPException(status_code=404, detail="候选记录不存在")

    old_status = cand.review_status
    cand.review_status = body.review_status
    if body.review_note is not None:
        cand.review_note = body.review_note

    db.add(
        OperationLog(
            user_id=getattr(request.state, "user_id", None),
            action="material_mapping_review",
            resource="material_mapping",
            entity_type="material_code_mapping_candidates",
            entity_id=candidate_id,
            details=json.dumps(
                {
                    "old_status": old_status,
                    "new_status": body.review_status,
                    "note": body.review_note,
                },
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    return {"ok": True, "id": candidate_id, "review_status": cand.review_status}


@router.post("/material-mapping/import-csv", dependencies=[Depends(admin_only)])
def import_mapping_csv(request: Request, db: Session = Depends(get_db)):
    """从本地 data/material_code_mapping.csv 导入材质映射候选数据。"""
    from app.services.material_mapping import import_csv_to_candidates
    from pathlib import Path as _Path

    settings = load_settings()
    csv_path = _Path(settings.database_path).parent / "material_code_mapping.csv"
    if not csv_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"CSV 文件不存在: {csv_path}",
        )

    try:
        stats = import_csv_to_candidates(
            db,
            csv_path,
            source_file_name=csv_path.name,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"导入失败: {exc}") from exc

    db.add(
        OperationLog(
            user_id=getattr(request.state, "user_id", None),
            action="material_mapping_csv_import",
            resource="material_mapping",
            entity_type="material_code_mapping_candidates",
            details=json.dumps(
                {
                    "total": stats.total,
                    "high": stats.high,
                    "mid": stats.mid,
                    "low": stats.low,
                    "conflicts": stats.conflicts,
                    "csv_path": str(csv_path),
                },
                ensure_ascii=False,
            ),
        )
    )
    db.commit()

    return {
        "ok": True,
        "total": stats.total,
        "high": stats.high,
        "mid": stats.mid,
        "low": stats.low,
        "conflicts": stats.conflicts,
        "empty_old": stats.empty_old,
        "empty_new": stats.empty_new,
    }


@router.get("/material-mapping/preview-high-confidence")
def preview_high_confidence_mapping(
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    from pathlib import Path as _Path
    from app.services.material_mapping import preview_high_confidence_material_mapping

    settings = load_settings()
    csv_path = _Path(settings.database_path).parent / "material_code_mapping.csv"
    preview = preview_high_confidence_material_mapping(db, csv_path)
    confirmation = _batch_preview_confirmation(
        "material_high_confidence",
        preview.changes,
        user,
    )
    return {
        "products_updated": preview.products_updated,
        "materials_created": preview.materials_created,
        "materials_reused": preview.materials_reused,
        "skipped_no_match": preview.skipped_no_match,
        "skipped_already_set": preview.skipped_already_set,
        "changes": preview.changes[:100],
        **confirmation,
    }


@router.post("/material-mapping/apply-high-confidence")
def apply_high_confidence_mapping(
    request: Request,
    body: BatchVersionConfirmation,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    """对高可信候选自动写入 products.material_id（dry_run 参数为 true 时只预览）。"""
    from app.services.material_mapping import (
        apply_high_confidence_material_mapping,
        preview_high_confidence_material_mapping,
    )
    from pathlib import Path as _Path

    settings = load_settings()
    csv_path = _Path(settings.database_path).parent / "material_code_mapping.csv"
    preview = preview_high_confidence_material_mapping(db, csv_path)
    _require_batch_confirmation(
        body,
        operation="material_high_confidence",
        items=preview.changes,
        user=user,
    )

    try:
        result = apply_high_confidence_material_mapping(
            db,
            csv_path,
            user=user,
            confirmation_tokens=body.confirmation_tokens,
            preview_confirmed=True,
        )
        db.add(
            OperationLog(
                user_id=user.id,
                action="material_mapping_apply_high_confidence",
                resource="material_mapping",
                entity_type="products",
                details=json.dumps(
                    {
                        "products_updated": result.products_updated,
                        "materials_created": result.materials_created,
                        "materials_reused": result.materials_reused,
                        "skipped_no_match": result.skipped_no_match,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "ok": True,
        "products_updated": result.products_updated,
        "materials_created": result.materials_created,
        "materials_reused": result.materials_reused,
        "skipped_no_match": result.skipped_no_match,
        "skipped_already_set": result.skipped_already_set,
        "changes": result.changes[:100],  # 最多返回前100条
    }


@router.post("/material-mapping/apply-customer-codes")
def apply_customer_codes(
    request: Request,
    body: BatchVersionConfirmation,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    """批量规范客户料号（从品名中提取，写入 customer_material_code）。"""
    from app.services.material_mapping import (
        apply_customer_code_updates,
        preview_customer_code_updates,
    )

    preview = preview_customer_code_updates(db)
    _require_batch_confirmation(
        body,
        operation="customer_codes",
        items=preview.changes,
        user=user,
    )

    try:
        result = apply_customer_code_updates(
            db,
            user=user,
            confirmation_tokens=body.confirmation_tokens,
            preview_confirmed=True,
        )
        db.add(
            OperationLog(
                user_id=user.id,
                action="customer_code_batch_update",
                resource="material_mapping",
                entity_type="products",
                details=json.dumps(
                    {
                        "updated": result.updated,
                        "skipped": result.skipped,
                        "sample_changes": result.changes[:20],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "ok": True,
        "updated": result.updated,
        "skipped": result.skipped,
        "changes": result.changes[:100],
    }


@router.get("/version")
def get_version(
    _user: User = Depends(get_current_user),
):
    """Return the exact ERP release fingerprint to an authenticated user."""
    from app.version import current_release_metadata

    return current_release_metadata()


@router.get("/version/changelog")
def get_version_changelog(
    _user: User = Depends(get_current_user),
):
    """Return the complete changelog only to an authenticated user."""
    from app.version import APP_CHANGELOG

    return {"changelog": APP_CHANGELOG}


@router.get(
    "/material-mapping/preview-customer-codes",
    dependencies=[Depends(admin_only)],
)
def preview_customer_codes(
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    """只读预览客户料号规范化结果（不写库）。"""
    from app.services.material_mapping import preview_customer_code_updates

    preview = preview_customer_code_updates(db)
    confirmation = _batch_preview_confirmation(
        "customer_codes",
        preview.changes,
        user,
    )
    return {
        "total": preview.total,
        "will_update": preview.will_update,
        "skipped": preview.skipped,
        "samples_update": preview.samples_update,
        "samples_skip": preview.samples_skip[:20],
        **confirmation,
    }


# ---------------------------------------------------------------------------
# Phase 17: 楞型批量识别端点
# ---------------------------------------------------------------------------

@router.get(
    "/flute-mapping/preview",
    dependencies=[Depends(admin_only)],
)
def preview_flute_mapping(
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    """只读预览：显示当前所有有效产品的楞型识别结果（不写库）。"""
    from app.services.flute_mapping import preview_flute_mapping as _preview

    preview = _preview(db)
    plan = [
        {
            "key": f"product:{row.product_id}",
            "object_type": "product",
            "object_id": row.product_id,
            "expected_version": row.expected_version,
            "updates": row.updates,
        }
        for row in preview.rows
    ]
    confirmation = _batch_preview_confirmation("flute_mapping", plan, user)
    return {
        "total_products": preview.total_products,
        "will_update": preview.will_update,
        "already_set": preview.already_set,
        "unrecognized": preview.unrecognized,
        "rows": [
            {
                "product_id": r.product_id,
                "product_code": r.product_code,
                "product_name": r.product_name,
                "expected_version": r.expected_version,
                "current_flute_type": r.current_flute_type,
                "proposed_flute_type": r.proposed_flute_type,
                "proposed_layer_count": r.proposed_layer_count,
                "source": r.source,
                "legacy_flute_text": r.legacy_flute_text,
            }
            for r in preview.rows[:200]  # 最多返回前200行
        ],
        **confirmation,
    }


@router.post("/flute-mapping/apply")
def apply_flute_mapping(
    request: Request,
    body: BatchVersionConfirmation,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    """
    批量写入楞型（admin only）。
    只更新 flute_type 为 NULL 的有效产品，不覆盖已有值。
    调用前必须已完成数据库备份。
    """
    from app.services.flute_mapping import apply_flute_mapping as _apply
    from app.services.flute_mapping import preview_flute_mapping as _preview

    preview = _preview(db)
    plan = [
        {
            "key": f"product:{row.product_id}",
            "object_type": "product",
            "object_id": row.product_id,
            "expected_version": row.expected_version,
            "updates": row.updates,
        }
        for row in preview.rows
    ]
    _require_batch_confirmation(
        body,
        operation="flute_mapping",
        items=plan,
        user=user,
    )

    try:
        result = _apply(
            db,
            user=user,
            confirmation_tokens=body.confirmation_tokens,
            preview_confirmed=True,
        )
        db.add(
            OperationLog(
                user_id=user.id,
                action="flute_mapping_apply",
                resource="products",
                entity_type="products",
                details=json.dumps(
                    {
                        "updated": result.updated,
                        "skipped_already_set": result.skipped_already_set,
                        "skipped_unrecognized": result.skipped_unrecognized,
                        "sample_changes": result.changes[:20],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "ok": True,
        "updated": result.updated,
        "skipped_already_set": result.skipped_already_set,
        "skipped_unrecognized": result.skipped_unrecognized,
        "changes": result.changes[:100],
    }


@router.get("/flute-mapping/preview-consistency")
def preview_flute_consistency(
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    from app.services.flute_mapping import preview_flute_consistency_fix

    preview = preview_flute_consistency_fix(db)
    confirmation = _batch_preview_confirmation(
        "flute_consistency",
        preview.changes,
        user,
    )
    return {
        "fixed_5layer_to_3": preview.fixed_5layer_to_3,
        "fixed_3layer_to_null": preview.fixed_3layer_to_null,
        "changes": preview.changes[:100],
        **confirmation,
    }


@router.post("/flute-mapping/fix-consistency")
def fix_flute_consistency(
    request: Request,
    body: BatchVersionConfirmation,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
):
    """
    修复非法楞型/层数组合（admin only）：
    - 5层 + A/B/E → layer_count 改为 3（单楞必然是三层）
    - 3层 + AB/BE → flute_type 置 null（留人工确认）
    调用前请先完成数据库备份。
    """
    from app.services.flute_mapping import (
        apply_flute_consistency_fix,
        preview_flute_consistency_fix,
    )

    preview = preview_flute_consistency_fix(db)
    _require_batch_confirmation(
        body,
        operation="flute_consistency",
        items=preview.changes,
        user=user,
    )

    try:
        result = apply_flute_consistency_fix(
            db,
            user=user,
            confirmation_tokens=body.confirmation_tokens,
            preview_confirmed=True,
        )
        db.add(
            OperationLog(
                user_id=user.id,
                action="flute_consistency_fix",
                resource="products",
                entity_type="products",
                details=json.dumps(
                    {
                        "fixed_5layer_to_3": result.fixed_5layer_to_3,
                        "fixed_3layer_to_null": result.fixed_3layer_to_null,
                        "sample_changes": result.changes[:20],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "ok": True,
        "fixed_5layer_to_3": result.fixed_5layer_to_3,
        "fixed_3layer_to_null": result.fixed_3layer_to_null,
        "changes": result.changes[:100],
    }


# ─────────────────────────────────────────────────────────────
# 公司信息维护（v0.20.3）
# ─────────────────────────────────────────────────────────────

def _find_company_row(db: Session) -> CompanyConfig | None:
    """只读获取单行公司配置。"""
    return db.scalar(select(CompanyConfig).where(CompanyConfig.id == 1))


def _company_dict(row: CompanyConfig | None) -> dict:
    return {
        "company_name": row.company_name or "" if row else "",
        "short_name": row.short_name if row else None,
        "address": row.address if row else None,
        "phone": row.phone if row else None,
        "fax": row.fax if row else None,
        "tax_number": row.tax_number if row else None,
        "bank_name": row.bank_name if row else None,
        "bank_account": row.bank_account if row else None,
        "contact_person": row.contact_person if row else None,
        "contact_phone": row.contact_phone if row else None,
        "updated_at": (
            beijing_naive_to_api(row.updated_at)
            if row and row.updated_at
            else None
        ),
    }


@router.get("/company", dependencies=[Depends(admin_only)])
def get_company(db: Session = Depends(get_db)) -> dict:
    """管理员读取公司信息。打印和导出由各自受保护接口直接读取配置。"""
    return _company_dict(_find_company_row(db))


@router.put("/company", dependencies=[Depends(admin_only)])
def update_company(
    body: CompanyConfigUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """更新公司信息（admin only）。"""
    row = _find_company_row(db)
    if row is None:
        row = CompanyConfig(id=1, company_name=body.company_name)
        db.add(row)
    row.company_name = body.company_name
    row.short_name = body.short_name.strip() if body.short_name else None
    row.address = body.address.strip() if body.address else None
    row.phone = body.phone.strip() if body.phone else None
    row.fax = body.fax.strip() if body.fax else None
    row.tax_number = body.tax_number.strip() if body.tax_number else None
    row.bank_name = body.bank_name.strip() if body.bank_name else None
    row.bank_account = body.bank_account.strip() if body.bank_account else None
    row.contact_person = body.contact_person.strip() if body.contact_person else None
    row.contact_phone = body.contact_phone.strip() if body.contact_phone else None
    row.updated_at = beijing_now_naive()
    db.add(
        OperationLog(
            user_id=user.id,
            action="UPDATE_COMPANY_CONFIG",
            resource="System",
            details=json.dumps(
                {"company_name": row.company_name},
                ensure_ascii=False,
            ),
            ip_address=request.client.host if request.client else None,
            username=user.username,
            role=user.role,
            entity_type="company_config",
            description=f"管理员更新公司信息：{row.company_name}",
            user_agent=request.headers.get("user-agent"),
        )
    )
    db.commit()
    return _company_dict(row)


# ---------------------------------------------------------------------------
# Delivery-note print paper calibration (N037)
# ---------------------------------------------------------------------------


@router.get("/delivery-print-settings")
def get_delivery_print_paper_settings() -> dict:
    """Public, read-only paper size contract used by the print-only page.

    This has no customer, order, or financial information, so it is deliberately
    readable before login as well as by a logged-in print window.
    """
    return get_delivery_print_settings()


@router.put("/delivery-print-settings", dependencies=[Depends(admin_only)])
def update_delivery_print_paper_settings(
    body: DeliveryPrintSettingsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Persist printer/paper calibration.  Only administrators may change it."""
    try:
        settings = save_delivery_print_settings(body.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        logger.exception("保存送货单打印纸张设置失败")
        raise HTTPException(status_code=503, detail="保存送货单打印纸张设置失败") from error

    db.add(
        OperationLog(
            user_id=user.id,
            action="UPDATE_DELIVERY_PRINT_SETTINGS",
            resource="System",
            details=json.dumps(settings, ensure_ascii=False),
            ip_address=request.client.host if request.client else None,
            username=user.username,
            role=user.role,
            entity_type="delivery_print_settings",
            description=(
                "管理员更新送货单打印纸张："
                f"{settings['paper_width_mm']:g}×{settings['paper_height_mm']:g}mm"
            ),
            user_agent=request.headers.get("user-agent"),
        )
    )
    db.commit()
    return settings
