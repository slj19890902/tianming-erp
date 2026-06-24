from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)

router = APIRouter()
admin_only = RoleChecker(["admin"])

# 始终保留最新 N 个备份，不允许删除
BACKUP_KEEP_COUNT = 5


class RestoreRequest(BaseModel):
    filename: str


class DeleteRequest(BaseModel):
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
                    "created_at": datetime.fromtimestamp(
                        stat.st_mtime
                    ).isoformat(timespec="seconds"),
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


@router.get("/backups/cleanup-preview")
def cleanup_preview(
    _user: User = Depends(admin_only),
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
            "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(
                timespec="seconds"
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
    user: User = Depends(admin_only),
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
    user: User = Depends(admin_only),
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
            "created_at": c.created_at.isoformat() if c.created_at else None,
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


@router.post(
    "/material-mapping/apply-high-confidence",
    dependencies=[Depends(admin_only)],
)
def apply_high_confidence_mapping(request: Request, db: Session = Depends(get_db)):
    """对高可信候选自动写入 products.material_id（dry_run 参数为 true 时只预览）。"""
    from app.services.material_mapping import apply_high_confidence_material_mapping
    from pathlib import Path as _Path

    settings = load_settings()
    csv_path = _Path(settings.database_path).parent / "material_code_mapping.csv"

    try:
        result = apply_high_confidence_material_mapping(db, csv_path)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"写入失败: {exc}") from exc

    db.add(
        OperationLog(
            user_id=getattr(request.state, "user_id", None),
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

    return {
        "ok": True,
        "products_updated": result.products_updated,
        "materials_created": result.materials_created,
        "materials_reused": result.materials_reused,
        "skipped_no_match": result.skipped_no_match,
        "skipped_already_set": result.skipped_already_set,
        "changes": result.changes[:100],  # 最多返回前100条
    }


@router.post(
    "/material-mapping/apply-customer-codes",
    dependencies=[Depends(admin_only)],
)
def apply_customer_codes(request: Request, db: Session = Depends(get_db)):
    """批量规范客户料号（从品名中提取，写入 customer_material_code）。"""
    from app.services.material_mapping import apply_customer_code_updates

    try:
        result = apply_customer_code_updates(db)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"写入失败: {exc}") from exc

    db.add(
        OperationLog(
            user_id=getattr(request.state, "user_id", None),
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

    return {
        "ok": True,
        "updated": result.updated,
        "skipped": result.skipped,
        "changes": result.changes[:100],
    }


@router.get("/version")
def get_version():
    """返回当前 ERP 系统版本信息（无需登录）。"""
    from app.version import APP_BUILD_DATE, APP_CHANGELOG, APP_VERSION, APP_VERSION_NAME

    return {
        "version": APP_VERSION,
        "version_name": APP_VERSION_NAME,
        "build_date": APP_BUILD_DATE,
        "changelog": APP_CHANGELOG,
    }


@router.get(
    "/material-mapping/preview-customer-codes",
    dependencies=[Depends(admin_only)],
)
def preview_customer_codes(db: Session = Depends(get_db)):
    """只读预览客户料号规范化结果（不写库）。"""
    from app.services.material_mapping import preview_customer_code_updates

    preview = preview_customer_code_updates(db)
    return {
        "total": preview.total,
        "will_update": preview.will_update,
        "skipped": preview.skipped,
        "samples_update": preview.samples_update,
        "samples_skip": preview.samples_skip[:20],
    }


# ---------------------------------------------------------------------------
# Phase 17: 楞型批量识别端点
# ---------------------------------------------------------------------------

@router.get(
    "/flute-mapping/preview",
    dependencies=[Depends(admin_only)],
)
def preview_flute_mapping(db: Session = Depends(get_db)):
    """只读预览：显示当前所有有效产品的楞型识别结果（不写库）。"""
    from app.services.flute_mapping import preview_flute_mapping as _preview

    preview = _preview(db)
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
                "current_flute_type": r.current_flute_type,
                "proposed_flute_type": r.proposed_flute_type,
                "proposed_layer_count": r.proposed_layer_count,
                "source": r.source,
                "legacy_flute_text": r.legacy_flute_text,
            }
            for r in preview.rows[:200]  # 最多返回前200行
        ],
    }


@router.post(
    "/flute-mapping/apply",
    dependencies=[Depends(admin_only)],
)
def apply_flute_mapping(request: Request, db: Session = Depends(get_db)):
    """
    批量写入楞型（admin only）。
    只更新 flute_type 为 NULL 的有效产品，不覆盖已有值。
    调用前必须已完成数据库备份。
    """
    from app.services.flute_mapping import apply_flute_mapping as _apply

    try:
        result = _apply(db)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"楞型写入失败: {exc}") from exc

    db.add(
        OperationLog(
            user_id=getattr(request.state, "user_id", None),
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

    return {
        "ok": True,
        "updated": result.updated,
        "skipped_already_set": result.skipped_already_set,
        "skipped_unrecognized": result.skipped_unrecognized,
        "changes": result.changes[:100],
    }
