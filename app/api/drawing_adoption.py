"""Explicit adoption for one not-yet-issued task; never rewrites old bindings."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.api.deps import PermissionChecker, get_db, has_permission, require_customer_access
from app.core.time_contract import utc_naive_to_api
from app.api.drawing_design import _published_result
from app.models.drawing_design import DrawingRelease, ProductionTaskDrawingAdoption
from app.models.production import ProductionCompletion, ProductionTask
from app.models.user import User
from app.services.audit_log import append_audit_event
from app.services.drawing_binding import _task_drawing_context, bound_task_release
from app.services.drawing_switch import drawing_v2_enabled, require_drawing_v2_write
from app.services.production_workflow import lock_order_rows_for_production_transition, ProductionWorkflowError


router = APIRouter()
can_read = PermissionChecker("orders.view")
can_adopt = PermissionChecker("orders.status")
CONFIRMATION_TEXT = "我已核实此任务尚未下发；采用后，此任务后续纸单与手机查看改用所选图纸，原采用记录保留。已打印或已下发的纸单不得沿用。"


class DrawingAdoptionWrite(BaseModel):
    release_id: int = Field(gt=0)
    expected_task_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=12, max_length=100)
    confirmed_not_issued: Literal[True]


def _context(db: Session, task_id: int, user: User):
    task = db.get(ProductionTask, task_id)
    if task is None:
        raise HTTPException(404, "生产任务不存在")
    item, order, component = _task_drawing_context(db, task)
    require_customer_access(order.customer_id, current_user=user, db=db)
    product_id = component.component_product_id if component else item.product_id
    return task, item, order, component, product_id


def _blocked_reason(db: Session, task, item, order) -> str | None:
    if task.status not in {"waiting_material", "pending"}:
        return "只有待料或待生产任务可在核实尚未下发后采用图纸"
    if item.is_force_closed or order.status in {"cancelled", "dead", "closed", "archived", "completed"}:
        return "已作废、关闭或完成的订单不能采用新图纸"
    if db.scalar(select(ProductionCompletion.id).where(ProductionCompletion.task_id == task.id).limit(1)) is not None:
        return "该任务已有生产完工记录，不能采用新图纸"
    return None


def _release_info(release: DrawingRelease | None) -> dict | None:
    if release is None:
        return None
    return {"id": release.id, "number": release.external_number, "revision": release.revision,
            "product_id": release.product_id, "published_at": utc_naive_to_api(release.published_at)}


def _event_info(event: ProductionTaskDrawingAdoption) -> dict:
    return {"id": event.id, "old_release_id": event.old_release_id,
            "new_release_id": event.new_release_id, "legacy_reference": event.legacy_reference,
            "expected_task_version": event.expected_task_version,
            "resulting_task_version": event.resulting_task_version,
            "adopted_by": event.adopted_by, "adopted_at": utc_naive_to_api(event.adopted_at)}


@router.get("/tasks/{task_id}/drawing-adoptions")
def get_drawing_adoptions(task_id: int, db: Session = Depends(get_db), user: User = Depends(can_read)) -> dict:
    task, item, order, component, product_id = _context(db, task_id, user)
    current = bound_task_release(db, task_id)
    candidates = db.scalars(select(DrawingRelease).where(DrawingRelease.product_id == product_id,
                           DrawingRelease.customer_id == order.customer_id)
                           .order_by(DrawingRelease.id.desc())).all()
    history = db.scalars(select(ProductionTaskDrawingAdoption).where(ProductionTaskDrawingAdoption.task_id == task_id)
                        .order_by(ProductionTaskDrawingAdoption.id.desc())).all()
    reason = _blocked_reason(db, task, item, order)
    if not has_permission(user, "orders.status"):
        reason = "没有生产操作权限"
    elif not drawing_v2_enabled():
        reason = "图纸V2采用已停用；历史图纸仍可查看和重印"
    return {"task_id": task_id, "task_version": task.version, "task_status": task.status,
            "current_release": _release_info(current),
            "legacy_reference": item.drawing_file or (component.snapshot_die_cut_path if component else None),
            "candidates": [_release_info(release) for release in candidates],
            "history": [_event_info(event) for event in history],
            "can_adopt": reason is None, "block_reason": reason,
            "confirmation_text": CONFIRMATION_TEXT}


def _replay(event, digest, user, db, customer_id):
    if event.request_hash != digest or event.adopted_by != user.id:
        raise HTTPException(409, "同一采用请求键不能用于不同任务、内容或操作员")
    release = db.get(DrawingRelease, event.new_release_id)
    if release is None:
        raise HTTPException(409, "采用记录的发布版缺失")
    _published_result(release, customer_id)
    return {"task_id": event.task_id, "task_version": event.resulting_task_version,
            "adoption": _event_info(event), "adopted_release": _release_info(release), "replayed": True}


@router.post("/tasks/{task_id}/drawing-adoptions")
def adopt_drawing(task_id: int, payload: DrawingAdoptionWrite,
                  db: Session = Depends(get_db), user: User = Depends(can_adopt)) -> dict:
    require_drawing_v2_write()
    digest = hashlib.sha256(json.dumps({"task_id": task_id, **payload.model_dump(mode="json")},
                            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    try:
        task, item, order, component, product_id = _context(db, task_id, user)
        existing = db.scalar(select(ProductionTaskDrawingAdoption).where(
            ProductionTaskDrawingAdoption.idempotency_key == payload.idempotency_key))
        if existing is not None:
            return _replay(existing, digest, user, db, order.customer_id)
        # Same lock used by completion and order termination; task CAS then
        # protects adoption against other edits without changing business state.
        lock_order_rows_for_production_transition(db, [order.id])
        locked_order_id = order.id
        db.refresh(task)
        db.refresh(item)
        if item.order_id != locked_order_id:
            raise HTTPException(409, "任务订单归属已变化，请重新读取")
        if component is not None:
            db.refresh(component)
        existing = db.scalar(select(ProductionTaskDrawingAdoption).where(
            ProductionTaskDrawingAdoption.idempotency_key == payload.idempotency_key))
        if existing is not None:
            return _replay(existing, digest, user, db, order.customer_id)
        task, item, order, component, product_id = _context(db, task_id, user)
        reason = _blocked_reason(db, task, item, order)
        if reason:
            raise HTTPException(409, reason)
        release = db.get(DrawingRelease, payload.release_id)
        if release is None or release.product_id != product_id or release.customer_id != order.customer_id:
            raise HTTPException(404, "所选发布版不属于本任务的产品及客户")
        current = bound_task_release(db, task_id)
        if current is not None and current.id == release.id:
            raise HTTPException(409, "本任务已采用该图纸，无需重复采用")
        _published_result(release, order.customer_id)
        changed = db.execute(update(ProductionTask).where(ProductionTask.id == task_id,
                             ProductionTask.version == payload.expected_task_version,
                             ProductionTask.status.in_(["waiting_material", "pending"]))
                             .values(version=ProductionTask.version + 1))
        if changed.rowcount != 1:
            raise HTTPException(409, "生产任务版本已变化，请重新读取后核对")
        event = ProductionTaskDrawingAdoption(task_id=task_id,
            old_release_id=current.id if current else None, new_release_id=release.id,
            legacy_reference=item.drawing_file or (component.snapshot_die_cut_path if component else None),
            expected_task_version=payload.expected_task_version,
            resulting_task_version=payload.expected_task_version + 1,
            idempotency_key=payload.idempotency_key, request_hash=digest,
            confirmed_not_issued=1, adopted_by=user.id)
        db.add(event)
        append_audit_event(db, event_category="business", result="success", source="web",
            module_code="production", action_code="production.drawing.adopted", resource="ProductionTask",
            actor=user, entity_type="production_task", entity_id=task_id,
            object_ref=f"production_task:{task_id}", customer_id=order.customer_id,
            description="确认尚未下发，显式采用本任务图纸版本", details={
                "old_release_id": event.old_release_id, "new_release_id": release.id,
                "expected_task_version": event.expected_task_version,
                "resulting_task_version": event.resulting_task_version,
                "idempotency_key": payload.idempotency_key,
                "confirmed_not_issued": True, "confirmation_text": CONFIRMATION_TEXT})
        db.commit()
        return {"task_id": task_id, "task_version": event.resulting_task_version,
                "adoption": _event_info(event), "adopted_release": _release_info(release), "replayed": False}
    except ProductionWorkflowError as error:
        db.rollback()
        raise HTTPException(error.status_code, str(error)) from error
    except (IntegrityError, OperationalError) as error:
        db.rollback()
        raise HTTPException(409, "采用发生并发冲突，请使用原请求键重试",
                            headers={"X-Drawing-Adoption-Retry": "same-request"}) from error
    except OSError as error:
        db.rollback()
        raise HTTPException(503, "图纸文件不可读，未完成采用，请核对后使用原请求键重试") from error
    except Exception:
        db.rollback()
        raise
