from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import case, select, update
from sqlalchemy.orm import Session, aliased

from app.api.deps import RoleChecker, get_db
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.user import User
from app.services.history_orders import build_display_registry, display_order_number


router = APIRouter()
can_read = RoleChecker(["admin", "workshop"])
can_operate = RoleChecker(["admin", "workshop"])


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class RevertRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("撤回原因不能为空")
        return reason


def _rows(db: Session, *, received_since: datetime | None = None) -> list[dict]:
    receiver = aliased(User)
    query = (
        select(
            OrderItem.id.label("item_id"),
            Order.id.label("order_id"),
            Order.order_number,
            Order.customer_po,
            Customer.name.label("customer_name"),
            OrderItem.snapshot_product_name.label("product_name"),
            OrderItem.snapshot_spec.label("specification"),
            OrderItem.snapshot_material.label("material"),
            OrderItem.quantity,
            Order.delivery_date,
            Order.status.label("order_status"),
            OrderItem.material_status,
            OrderItem.requisition_status,
            OrderItem.requisition_qty,
            OrderItem.requisition_spec,
            OrderItem.special_process,
            OrderItem.supplier_delivery_time,
            OrderItem.supplier_order_number,
            OrderItem.material_received_at,
            OrderItem.material_received_by,
            receiver.real_name.label("received_by_name"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .outerjoin(receiver, receiver.id == OrderItem.material_received_by)
    )
    if received_since is None:
        query = query.where(
            OrderItem.material_status == "pending",
            OrderItem.requisition_status.in_(["已报料", "供应商已排单"]),
        ).order_by(
            case(
                (OrderItem.requisition_status == "供应商已排单", 0),
                else_=1,
            ),
            case((OrderItem.supplier_delivery_time.is_(None), 1), else_=0),
            OrderItem.supplier_delivery_time.asc(),
            case((Order.delivery_date.is_(None), 1), else_=0),
            Order.delivery_date.asc(),
            Order.order_number.asc(),
            OrderItem.id.asc(),
        )
    else:
        query = query.where(
            OrderItem.material_status == "received",
            OrderItem.material_received_at >= received_since,
        ).order_by(
            OrderItem.material_received_at.desc(),
            OrderItem.id.desc(),
        )
    registry = build_display_registry(db)
    rows = []
    for row in db.execute(query):
        data = dict(row._mapping)
        rows.append(data)
    return _decorate_rows_with_display_numbers(db, rows, registry)


def _decorate_rows_with_display_numbers(
    db: Session, rows: list[dict], registry
) -> list[dict]:
    if not rows:
        return rows
    order_ids = {row["order_id"] for row in rows if row.get("order_id")}
    history_orders = {
        order.id: order
        for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    }
    for row in rows:
        order = history_orders.get(row.get("order_id"))
        display = display_order_number(order, registry) if order is not None else row.get("order_number")
        row["order_number"] = display
        row["display_order_number"] = display
    return rows


def _audit(
    db: Session,
    *,
    user: User,
    action: str,
    item_id: int,
    details: dict,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            resource="OrderItem",
            details=json.dumps(details, ensure_ascii=False, default=str),
            username=user.username,
            role=user.role,
            entity_type="order_item",
            entity_id=item_id,
            description=(
                "车间来料入库"
                if action == "RECEIVE_MATERIAL"
                else "撤回来料入库"
            ),
        )
    )


def _item_response(db: Session, item_id: int) -> dict:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    return {
        "item_id": item.id,
        "material_status": item.material_status,
        "requisition_status": item.requisition_status,
        "material_received_at": item.material_received_at,
        "material_received_by": item.material_received_by,
    }


@router.get("/pending")
def pending_items(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    return {"items": _rows(db)}


@router.get("/received")
def recently_received_items(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    return {"items": _rows(db, received_since=_utc_now() - timedelta(hours=24))}


@router.put("/receive/{item_id}")
def receive_item(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    received_at = _utc_now()
    try:
        result = db.execute(
            update(OrderItem)
            .where(
                OrderItem.id == item_id,
                OrderItem.material_status == "pending",
                OrderItem.requisition_status.in_(["已报料", "供应商已排单"]),
            )
            .values(
                material_status="received",
                requisition_status="已入库",
                material_received_at=received_at,
                material_received_by=user.id,
            )
        )
        if result.rowcount != 1:
            exists = db.scalar(
                select(OrderItem.id).where(OrderItem.id == item_id)
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="订单明细不存在")
            raise HTTPException(status_code=409, detail="该明细已入库，请勿重复操作")
        _audit(
            db,
            user=user,
            action="RECEIVE_MATERIAL",
            item_id=item_id,
            details={"received_at": received_at},
        )
        db.commit()
        return _item_response(db, item_id)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.put("/revert/{item_id}")
def revert_item(
    item_id: int,
    payload: RevertRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == item_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    item, order = row
    if order.status in {"partially_delivered", "delivered"}:
        raise HTTPException(status_code=409, detail="订单已发货，禁止撤回来料")
    if item.material_status != "received":
        raise HTTPException(status_code=409, detail="该明细当前不是已入库状态")

    previous_received_at = item.material_received_at
    previous_received_by = item.material_received_by
    try:
        result = db.execute(
            update(OrderItem)
            .where(
                OrderItem.id == item_id,
                OrderItem.material_status == "received",
            )
            .values(
                material_status="pending",
                requisition_status="已报料",
                material_received_at=None,
                material_received_by=None,
            )
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="状态已变化，请刷新后重试")
        _audit(
            db,
            user=user,
            action="REVERT_MATERIAL",
            item_id=item_id,
            details={
                "reason": payload.reason,
                "previous_received_at": previous_received_at,
                "previous_received_by": previous_received_by,
            },
        )
        db.commit()
        return _item_response(db, item_id)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
