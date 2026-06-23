from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy import case, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.history_orders import build_display_registry, display_order_number


router = APIRouter()
order_actions_router = APIRouter()
can_read = RoleChecker(["admin", "finance", "sales", "workshop"])
can_operate = RoleChecker(["admin", "sales"])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _print_product_code(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.split(r"\s*/\s*|\s+", text, maxsplit=1)[0]


class DeliveryLineCreate(BaseModel):
    order_item_id: int
    delivered_quantity: int
    remarks: str | None = None


class DeliveryCreate(BaseModel):
    customer_id: int
    delivery_date: date | None = None
    vehicle_number: str | None = None
    items: list[DeliveryLineCreate]

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[DeliveryLineCreate]):
        if not value:
            raise ValueError("送货单至少需要一条明细")
        ids = [item.order_item_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("同一订单明细不能重复选择")
        return value


class ForceCloseRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("强制结案原因不能为空")
        return reason


def _next_delivery_number(db: Session, delivery_date: date) -> str:
    sequence = db.execute(
        text(
            """
            INSERT INTO delivery_daily_sequences (sequence_date, last_value)
            VALUES (:sequence_date, 1)
            ON CONFLICT(sequence_date)
            DO UPDATE SET last_value = last_value + 1
            RETURNING last_value
            """
        ),
        {"sequence_date": delivery_date.isoformat()},
    ).scalar_one()
    if sequence > 999:
        raise HTTPException(status_code=409, detail="当日送货单流水号已超过999")
    return f"DH-{delivery_date:%Y%m%d}-{sequence:03d}"


def _pending_query():
    return (
        select(
            OrderItem.id.label("item_id"),
            Order.id.label("order_id"),
            Order.order_number,
            Order.customer_po,
            Order.customer_id,
            Customer.name.label("customer_name"),
            Product.product_code,
            OrderItem.snapshot_product_name.label("product_name"),
            OrderItem.snapshot_spec.label("specification"),
            OrderItem.snapshot_material.label("material"),
            OrderItem.quantity,
            OrderItem.delivered_quantity,
            (
                OrderItem.quantity - OrderItem.delivered_quantity
            ).label("remaining_quantity"),
            Order.delivery_date,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(
            OrderItem.material_status == "received",
            OrderItem.delivered_quantity < OrderItem.quantity,
            OrderItem.is_force_closed.is_(False),
        )
        .order_by(
            case((Order.delivery_date.is_(None), 1), else_=0),
            Order.delivery_date.asc(),
            Order.order_number.asc(),
            OrderItem.id.asc(),
        )
    )


def _delivery_or_404(db: Session, delivery_id: int) -> Delivery:
    delivery = db.get(Delivery, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="送货单不存在")
    return delivery


def _delivery_response(db: Session, delivery_id: int) -> dict:
    from app.models.finance import ReturnReceipt

    delivery = _delivery_or_404(db, delivery_id)
    customer = db.get(Customer, delivery.customer_id)
    return_receipt = db.scalar(
        select(ReturnReceipt).where(ReturnReceipt.delivery_id == delivery_id)
    )
    items = db.execute(
        select(
            DeliveryItem.id,
            DeliveryItem.order_item_id,
            DeliveryItem.delivered_quantity,
            DeliveryItem.remarks,
            Order.id.label("order_id"),
            Order.order_number,
            Order.customer_po,
            Product.product_code,
            OrderItem.snapshot_product_name.label("product_name"),
            OrderItem.snapshot_spec.label("specification"),
        )
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(DeliveryItem.delivery_id == delivery_id)
        .order_by(DeliveryItem.id)
    ).all()
    registry = build_display_registry(db)
    order_ids = {
        row._mapping["order_id"]
        for row in items
    }
    orders = {
        order.id: order for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    } if order_ids else {}
    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "customer_id": delivery.customer_id,
        "customer_name": customer.name if customer else None,
        "delivery_date": delivery.delivery_date,
        "vehicle_number": delivery.vehicle_number,
        "status": delivery.status,
        "total_quantity": delivery.total_quantity,
        "dispatched_at": delivery.dispatched_at,
        "is_printed": delivery.printed_at is not None,
        "printed_at": delivery.printed_at,
        "printed_by": delivery.printed_by,
        "return_receipt_id": return_receipt.id if return_receipt else None,
        "return_receipt_status": (
            return_receipt.status if return_receipt else None
        ),
        "items": [
            {
                **dict(row._mapping),
                "order_number": display_order_number(
                    orders.get(row._mapping["order_id"]),
                    registry,
                )
                if "order_id" in row._mapping
                else row._mapping["order_number"],
                "display_order_number": display_order_number(
                    orders.get(row._mapping["order_id"]),
                    registry,
                )
                if "order_id" in row._mapping
                else row._mapping["order_number"],
            }
            for row in items
        ],
    }


def _write_audit(
    db: Session,
    *,
    user: User,
    action: str,
    resource: str,
    entity_id: int,
    details: dict,
    description: str,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            resource=resource,
            details=json.dumps(details, ensure_ascii=False, default=str),
            username=user.username,
            role=user.role,
            entity_type=resource.lower(),
            entity_id=entity_id,
            description=description,
        )
    )


def _refresh_order_status(db: Session, order_id: int) -> None:
    items = db.scalars(
        select(OrderItem).where(OrderItem.order_id == order_id)
    ).all()
    if items and all(
        item.is_force_closed or item.delivered_quantity >= item.quantity
        for item in items
    ):
        new_status = "delivered"
    elif any(item.delivered_quantity > 0 for item in items):
        new_status = "partially_delivered"
    else:
        new_status = "pending_delivery"
    db.execute(
        update(Order).where(Order.id == order_id).values(status=new_status)
    )


@router.get("/pending_items")
def pending_delivery_items(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    registry = build_display_registry(db)
    items = []
    for row in db.execute(_pending_query()):
        order = db.get(Order, row._mapping["order_id"])
        display = display_order_number(order, registry)
        items.append(
            {
                **dict(row._mapping),
                "order_number": display,
                "display_order_number": display,
            }
        )
    return {"items": items}


@router.get("")
def list_deliveries(
    customer_id: int | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    query = select(Delivery.id).order_by(
        Delivery.delivery_date.desc(),
        Delivery.id.desc(),
    )
    if customer_id is not None:
        query = query.where(Delivery.customer_id == customer_id)
    if status_filter:
        query = query.where(Delivery.status == status_filter)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    delivery_ids = db.scalars(
        query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            _delivery_response(db, delivery_id)
            for delivery_id in delivery_ids
        ],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_delivery(
    payload: DeliveryCreate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    if db.get(Customer, payload.customer_id) is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    delivery_date = payload.delivery_date or date.today()
    try:
        delivery = Delivery(
            delivery_number=_next_delivery_number(db, delivery_date),
            customer_id=payload.customer_id,
            delivery_date=delivery_date,
            vehicle_number=(payload.vehicle_number or "").strip() or None,
            status="pending",
            total_quantity=0,
            created_by=user.id,
        )
        db.add(delivery)
        db.flush()
        total_quantity = 0
        warnings: list[dict] = []
        for index, line in enumerate(payload.items, start=1):
            if line.delivered_quantity <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条送货数量必须大于0",
                )
            row = db.execute(
                select(OrderItem, Order)
                .join(Order, Order.id == OrderItem.order_id)
                .where(OrderItem.id == line.order_item_id)
            ).one_or_none()
            if row is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条订单明细不存在",
                )
            order_item, order = row
            remaining = order_item.quantity - order_item.delivered_quantity
            if order.customer_id != payload.customer_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条订单明细不属于当前客户",
                )
            if (
                order_item.material_status != "received"
                or order_item.is_force_closed
                or remaining <= 0
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条订单明细当前不可发货",
                )
            if line.delivered_quantity > remaining:
                warnings.append(
                    {
                        "code": "OVER_DELIVERY",
                        "order_item_id": order_item.id,
                        "remaining_quantity": remaining,
                        "delivered_quantity": line.delivered_quantity,
                        "excess_quantity": line.delivered_quantity - remaining,
                        "message": "实际发货数已超过订单可送数量",
                    }
                )
            db.add(
                DeliveryItem(
                    delivery_id=delivery.id,
                    order_item_id=order_item.id,
                    delivered_quantity=line.delivered_quantity,
                    remarks=(line.remarks or "").strip() or None,
                )
            )
            total_quantity += line.delivered_quantity
        delivery.total_quantity = total_quantity
        _write_audit(
            db,
            user=user,
            action="CREATE",
            resource="Delivery",
            entity_id=delivery.id,
            details={
                "delivery_number": delivery.delivery_number,
                "item_count": len(payload.items),
                "total_quantity": total_quantity,
            },
            description="创建待发货送货单",
        )
        db.commit()
        response = _delivery_response(db, delivery.id)
        response["warnings"] = warnings
        return response
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="送货单数据冲突") from error
    except Exception:
        db.rollback()
        raise


@router.put("/{delivery_id}/dispatch")
def dispatch_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    dispatched_at = _utc_now()
    try:
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "pending",
            )
            .values(
                status="dispatched",
                dispatched_by=user.id,
                dispatched_at=dispatched_at,
            )
        )
        if claimed.rowcount != 1:
            exists = db.scalar(
                select(Delivery.id).where(Delivery.id == delivery_id)
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            raise HTTPException(status_code=409, detail="送货单已确认发货")

        lines = db.scalars(
            select(DeliveryItem)
            .where(DeliveryItem.delivery_id == delivery_id)
            .order_by(DeliveryItem.id)
        ).all()
        affected_order_ids: set[int] = set()
        for line in lines:
            order_id = db.scalar(
                select(OrderItem.order_id).where(
                    OrderItem.id == line.order_item_id
                )
            )
            result = db.execute(
                update(OrderItem)
                .where(
                    OrderItem.id == line.order_item_id,
                    OrderItem.material_status == "received",
                    OrderItem.is_force_closed.is_(False),
                )
                .values(
                    delivered_quantity=(
                        OrderItem.delivered_quantity + line.delivered_quantity
                    )
                )
            )
            if result.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}状态或可发数量已变化",
                )
            if order_id is not None:
                affected_order_ids.add(order_id)
        for order_id in affected_order_ids:
            _refresh_order_status(db, order_id)
        _write_audit(
            db,
            user=user,
            action="DISPATCH",
            resource="Delivery",
            entity_id=delivery_id,
            details={
                "dispatched_at": dispatched_at,
                "item_count": len(lines),
            },
            description="确认送货单发货",
        )
        db.commit()
        return _delivery_response(db, delivery_id)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.put("/{delivery_id}/printed")
def mark_delivery_printed(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    printed_at = _utc_now()
    delivery = _delivery_or_404(db, delivery_id)
    if delivery.status != "dispatched":
        raise HTTPException(status_code=409, detail="送货单尚未确认发货")
    delivery.printed_at = printed_at
    delivery.printed_by = user.id
    _write_audit(
        db,
        user=user,
        action="PRINT_DELIVERY",
        resource="Delivery",
        entity_id=delivery.id,
        details={"printed_at": printed_at},
        description="打印送货单",
    )
    db.commit()
    return _delivery_response(db, delivery.id)


@order_actions_router.put("/items/{item_id}/force_close")
def force_close_order_item(
    item_id: int,
    payload: ForceCloseRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    if item.delivered_quantity >= item.quantity:
        raise HTTPException(status_code=409, detail="订单明细已全部发货")
    try:
        result = db.execute(
            update(OrderItem)
            .where(
                OrderItem.id == item_id,
                OrderItem.is_force_closed.is_(False),
                OrderItem.delivered_quantity < OrderItem.quantity,
            )
            .values(is_force_closed=True)
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="订单明细已结案")
        _refresh_order_status(db, item.order_id)
        _write_audit(
            db,
            user=user,
            action="FORCE_CLOSE_ORDER_ITEM",
            resource="OrderItem",
            entity_id=item_id,
            details={
                "reason": payload.reason,
                "ordered_quantity": item.quantity,
                "delivered_quantity": item.delivered_quantity,
            },
            description="缺货尾数强制结案",
        )
        db.commit()
        return {
            "item_id": item_id,
            "is_force_closed": True,
            "reason": payload.reason,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.get("/{delivery_id}")
def get_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    return _delivery_response(db, delivery_id)


@router.get("/{delivery_id}/print")
def get_delivery_print_data(
    delivery_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    delivery = _delivery_or_404(db, delivery_id)
    customer = db.get(Customer, delivery.customer_id)
    rows = db.execute(
        select(
            Order.customer_po,
            Product.product_code,
            OrderItem.snapshot_product_name.label("product_name"),
            OrderItem.snapshot_spec.label("specification"),
            DeliveryItem.delivered_quantity.label("quantity"),
            DeliveryItem.remarks,
        )
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(DeliveryItem.delivery_id == delivery_id)
        .order_by(DeliveryItem.id)
    ).all()
    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "delivery_date": delivery.delivery_date,
        "vehicle_number": delivery.vehicle_number,
        "status": delivery.status,
        "total_quantity": delivery.total_quantity,
        "created_at": delivery.created_at,
        "customer": {
            "name": customer.name if customer else "",
            "contact_person": customer.contact_person if customer else None,
            "phone": customer.phone if customer else None,
            "address": customer.address if customer else None,
        },
        "items": [
            {
                "customer_po": row.customer_po,
                "product_code": _print_product_code(row.product_code),
                "product_name": row.product_name,
                "specification": row.specification,
                "unit": "PCS",
                "quantity": row.quantity,
                "remarks": row.remarks,
            }
            for row in rows
        ],
    }
