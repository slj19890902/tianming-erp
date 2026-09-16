"""Physical dimension lookup and accountable, non-order stock consumption."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.api.deps import PermissionChecker, get_db, has_permission
from app.core.time_contract import utc_now_naive, utc_naive_to_api
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail as Finished, SemiFinishedInventoryDetail as Board,
    InventoryLot, InventoryMovement, WarehouseLocation,
)
from app.services.audit_log import append_audit_event
from app.services.warehouse_display_units import lot_display_unit
from app.services.warehouse_inventory import _balances, _movement
from app.services.warehouse_location_address import location_address_payload

router = APIRouter(prefix="/warehouse/dimension-stock")
PURPOSES = {"cash": "现金取用", "sample": "免费打样"}


def _stock_payload(lot):
    detail = lot.finished_detail or lot.semi_finished_detail
    finished = lot.finished_detail
    location = lot.location
    address = location_address_payload(location)
    return {
        "id": lot.id, "version": lot.version, "lot_number": lot.lot_number,
        "location_id": location.id, "address_version": location.address_version,
        "location_name": address["employee_location_name"],
        "floor": location.warehouse_floor, "status": lot.status,
        "name": (finished.product_name_snapshot if finished else detail.internal_name or "纸板"),
        "code": finished.inventory_code_snapshot if finished else detail.material_code_snapshot,
        "customer": detail.owner_customer_name_snapshot or "通用",
        "dimensions": ([finished.length_mm, finished.width_mm, finished.height_mm] if finished
                       else [detail.board_length_mm, detail.board_width_mm]),
        "flute": finished.flute_type_snapshot if finished else detail.flute_type,
        "unit": lot_display_unit(lot), "available": lot.quantity_available,
        "reserved": lot.quantity_reserved, "damaged": lot.quantity_damaged,
        "physical": lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged,
        "can_take": lot.status == "active" and location.is_active and lot.quantity_available > 0,
    }


@router.get("")
def search(
    response: Response,
    kind: Literal["board", "box"] = "board",
    length: str | None = Query(None, pattern=r"^[0-9]{1,5}$"),
    width: str | None = Query(None, pattern=r"^[0-9]{1,5}$"),
    height: str | None = Query(None, pattern=r"^[0-9]{1,5}$"),
    length_op: Literal["eq", "ge", "le"] = "ge",
    width_op: Literal["eq", "ge", "le"] = "ge",
    height_op: Literal["eq", "ge", "le"] = "ge",
    flute: str | None = Query(None, max_length=20),
    offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db), user: User = Depends(PermissionChecker("warehouse.view")),
):
    from app.api.warehouse import _lot_query, _visible_customer_ids, _visible_lot_condition
    response.headers["Cache-Control"] = "private, no-store"
    values = [length, width, height]
    if not any(value is not None for value in values) and not flute:
        raise HTTPException(422, "请填写尺寸或选择楞型")
    if any(value is not None and int(value) <= 0 for value in values):
        raise HTTPException(422, "尺寸需为1至99999毫米")
    if kind == "board" and height is not None:
        raise HTTPException(422, "纸板只筛选长和宽")
    detail = Board if kind == "board" else Finished
    columns = [Board.board_length_mm, Board.board_width_mm] if kind == "board" else [Finished.length_mm, Finished.width_mm, Finished.height_mm]
    query = _lot_query(require_formal_location=False).join(detail, detail.inventory_lot_id == InventoryLot.id).where(
        InventoryLot.inventory_type == ("semi_finished" if kind == "board" else "finished"),
        InventoryLot.status.in_(["active", "frozen"]),
        InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged > 0,
    )
    scope = _visible_customer_ids(user, db)
    if scope is not None:
        query = query.where(_visible_lot_condition(scope))
    distance = 0
    for column, value, operator in zip(columns, values, [length_op, width_op, height_op]):
        if value is None:
            continue
        value = int(value)
        query = query.where(column.is_not(None), column > 0,
                            {"eq": column == value, "ge": column >= value, "le": column <= value}[operator])
        distance = distance + func.abs(column - value)
    if flute:
        column = Board.flute_type if kind == "board" else Finished.flute_type_snapshot
        query = query.where(func.upper(column) == flute.strip().upper())
    total = db.scalar(select(func.count()).select_from(query.with_only_columns(InventoryLot.id).subquery()))
    if not isinstance(distance, int):
        query = query.order_by(distance)
    lots = db.scalars(query.order_by(InventoryLot.id).offset(offset).limit(limit)).all()
    return {"total": total, "offset": offset, "limit": limit,
            "can_execute": has_permission(user, "warehouse.execute"),
            "items": [_stock_payload(lot) for lot in lots]}


class TakeRequest(BaseModel):
    quantity: int = Field(gt=0, le=2147483647, strict=True)
    purpose: Literal["cash", "sample"]
    expected_version: int = Field(gt=0, strict=True)
    location_id: int = Field(gt=0, strict=True)
    address_version: int = Field(ge=0, strict=True)
    idempotency_key: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@router.post("/{lot_id}/take")
def take(lot_id: int, payload: TakeRequest, request: Request,
         db: Session = Depends(get_db), user: User = Depends(PermissionChecker("warehouse.execute"))):
    from app.api.warehouse import _require_lot_customer_access
    signature = json.dumps({**payload.model_dump(exclude={"idempotency_key"}),
                            "actor_id": user.id, "lot_id": lot_id}, sort_keys=True)
    key = "mobile-take:" + payload.idempotency_key
    try:
        # Serialize receipt lookup and deduction on SQLite, including simultaneous
        # retries after a lost response. No business row is changed by this lock.
        if db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        lot = _require_lot_customer_access(db, lot_id, user)
        previous = db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == key))
        if previous:
            if previous.remarks != signature or previous.movement_type != "consume":
                raise HTTPException(409, "该取用请求已用于其他内容，请刷新后重试")
            return {"movement_id": previous.id, "replayed": True, "quantity": previous.quantity}
        if lot.inventory_type not in {"finished", "semi_finished"} or not (lot.finished_detail or lot.semi_finished_detail):
            raise HTTPException(409, "该批次不支持直接取用")
        if (lot.warehouse_location_id != payload.location_id or not lot.location.is_active
                or lot.location.address_version != payload.address_version):
            raise HTTPException(409, "货位已变化，请重新查询")
        if lot.status != "active":
            raise HTTPException(409, "该库存已冻结或关闭")
        if payload.quantity > lot.quantity_available:
            raise HTTPException(409, "取用不能超过可用数量，已预占数量不能取用")
        before = _balances(lot)
        changed = db.execute(update(InventoryLot).where(
            InventoryLot.id == lot_id, InventoryLot.version == payload.expected_version,
            InventoryLot.status == "active", InventoryLot.warehouse_location_id == payload.location_id,
            InventoryLot.quantity_available >= payload.quantity,
            InventoryLot.warehouse_location_id.in_(select(WarehouseLocation.id).where(
                WarehouseLocation.id == payload.location_id, WarehouseLocation.is_active.is_(True),
                WarehouseLocation.address_version == payload.address_version)),
        ).values(quantity_available=InventoryLot.quantity_available - payload.quantity,
                 quantity_consumed=InventoryLot.quantity_consumed + payload.quantity,
                 version=InventoryLot.version + 1, last_movement_at=utc_now_naive()))
        if changed.rowcount != 1:
            raise HTTPException(409, "库存已变化，请重新查询后取用")
        db.expire(lot)
        movement = _movement(db, lot=lot, movement_type="consume", quantity=payload.quantity,
                             before=before, operator_id=user.id, reason=PURPOSES[payload.purpose],
                             remarks=signature, idempotency_key=key)
        db.flush()
        append_audit_event(db, request=request, actor=user, event_category="business", result="success",
                          source="mobile", module_code="warehouse", action_code="warehouse.mobile_stock.take",
                          resource="InventoryMovement", entity_type="inventory_movement", entity_id=movement.id,
                          object_ref=f"inventory_movement:{movement.id}", description=PURPOSES[payload.purpose],
                          details={"location_id": payload.location_id, "lot_id": lot_id,
                                   "quantity": payload.quantity, "purpose": payload.purpose,
                                   "location_name": location_address_payload(lot.location)["employee_location_name"],
                                   "before": before, "after": _balances(lot)})
        db.commit()
        return {"movement_id": movement.id, "replayed": False, "quantity": payload.quantity}
    except HTTPException as error:
        db.rollback()
        error.headers = {**(error.headers or {}), "X-Stock-Take-Rejected": "1"}
        raise
    except (IntegrityError, OperationalError) as error:
        db.rollback()
        raise HTTPException(409, "库存正在更新，请保留本次请求重试或重新查询") from error
    except Exception:
        db.rollback()
        raise


@router.get("/{lot_id}/detail")
def detail(lot_id: int, response: Response, db: Session = Depends(get_db),
           user: User = Depends(PermissionChecker("warehouse.view"))):
    from app.api.warehouse import _require_lot_customer_access
    response.headers["Cache-Control"] = "private, no-store"
    lot = _require_lot_customer_access(db, lot_id, user)
    if not (lot.finished_detail or lot.semi_finished_detail):
        raise HTTPException(409, "库存资料不完整")
    return {"item": _stock_payload(lot), "can_execute": has_permission(user, "warehouse.execute")}


@router.get("/{lot_id}/history")
def history(lot_id: int, response: Response, db: Session = Depends(get_db),
            user: User = Depends(PermissionChecker("warehouse.view"))):
    from app.api.warehouse import _require_lot_customer_access
    _require_lot_customer_access(db, lot_id, user)
    response.headers["Cache-Control"] = "private, no-store"
    rows = db.execute(select(InventoryMovement, User.username).outerjoin(User, User.id == InventoryMovement.operator_id)
        .where(InventoryMovement.inventory_lot_id == lot_id,
               InventoryMovement.idempotency_key.startswith("mobile-take:"))
        .order_by(InventoryMovement.id.desc()).limit(50)).all()
    return {"items": [{"id": row.id, "account": account or "历史账号", "quantity": row.quantity,
                       "purpose": row.reason, "time": utc_naive_to_api(row.created_at)} for row, account in rows]}
