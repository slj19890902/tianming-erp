from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phase1_postgres.database import get_session
from phase1_postgres.models import Customer, DeliveryItem, DeliveryNote, OrderItem


router = APIRouter(prefix="/api/deliveries", tags=["deliveries"])


class DeliveryItemPayload(BaseModel):
    order_item_id: int
    delivery_qty: int = Field(gt=0)
    remark: str | None = None


class DeliveryPayload(BaseModel):
    customer_id: int
    delivery_date: date
    vehicle_number: str | None = None
    driver_name: str | None = None
    note: str | None = None
    items: list[DeliveryItemPayload] = Field(min_length=1)


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00"), rounding=ROUND_HALF_UP)


def _next_delivery_number(session: Session, delivery_date: date) -> str:
    prefix = f"DH-{delivery_date:%Y%m%d}-"
    max_number = session.scalar(
        select(func.max(DeliveryNote.delivery_number)).where(DeliveryNote.delivery_number.like(f"{prefix}%"))
    )
    if not max_number:
        return f"{prefix}001"
    try:
        serial = int(str(max_number).rsplit("-", 1)[1]) + 1
    except (IndexError, ValueError):
        serial = 1
    return f"{prefix}{serial:03d}"


def pending_item_dict(item: OrderItem) -> dict[str, Any]:
    remaining = max(0, item.quantity - item.delivered_quantity)
    order = item.order
    return {
        "order_item_id": item.id,
        "order_number": order.order_number,
        "customer_id": order.customer_id,
        "customer_name": order.customer.name if order.customer else None,
        "customer_po": order.customer_po,
        "product_code": item.snapshot_product_code,
        "product_name": item.snapshot_product_name,
        "spec": item.snapshot_spec,
        "material": item.snapshot_material,
        "quantity": item.quantity,
        "delivered_quantity": item.delivered_quantity,
        "remaining_qty": remaining,
        "unit_price": str(item.unit_price),
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
    }


def delivery_item_dict(item: DeliveryItem) -> dict[str, Any]:
    order_item = item.order_item
    order = order_item.order
    return {
        "id": item.id,
        "order_item_id": item.order_item_id,
        "order_number": order.order_number,
        "customer_po": order.customer_po,
        "product_code": order_item.snapshot_product_code,
        "product_name": order_item.snapshot_product_name,
        "spec": order_item.snapshot_spec,
        "delivery_qty": item.delivery_qty,
        "unit_price": str(item.unit_price_snapshot),
        "amount": str(item.amount_snapshot),
        "remark": item.remark,
    }


def delivery_dict(delivery: DeliveryNote) -> dict[str, Any]:
    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "customer_id": delivery.customer_id,
        "customer_name": delivery.customer.name if delivery.customer else None,
        "delivery_date": delivery.delivery_date.isoformat(),
        "vehicle_number": delivery.vehicle_number,
        "driver_name": delivery.driver_name,
        "status": delivery.status,
        "total_quantity": delivery.total_quantity,
        "items": [delivery_item_dict(item) for item in delivery.items],
    }


@router.get("/pending-items")
def pending_delivery_items(customer_id: int | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    query = (
        select(OrderItem)
        .where(OrderItem.material_status == "received")
        .where(OrderItem.is_force_closed.is_(False))
        .where(OrderItem.delivered_quantity < OrderItem.quantity)
        .order_by(OrderItem.id)
    )
    if customer_id is not None:
        query = query.join(OrderItem.order).where(OrderItem.order.has(customer_id=customer_id))
    items = session.scalars(query).all()
    return {"total": len(items), "items": [pending_item_dict(item) for item in items]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_delivery(payload: DeliveryPayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    customer = session.get(Customer, payload.customer_id)
    if customer is None or not customer.is_active:
        raise HTTPException(status_code=400, detail="客户不存在或已停用")

    delivery = DeliveryNote(
        delivery_number=_next_delivery_number(session, payload.delivery_date),
        customer_id=payload.customer_id,
        delivery_date=payload.delivery_date,
        vehicle_number=payload.vehicle_number,
        driver_name=payload.driver_name,
        status="DELIVERED_WAIT_RECEIPT",
        note=payload.note,
    )
    total_qty = 0
    for line in payload.items:
        order_item = session.get(OrderItem, line.order_item_id)
        if order_item is None:
            raise HTTPException(status_code=400, detail="订单明细不存在")
        if order_item.order.customer_id != payload.customer_id:
            raise HTTPException(status_code=400, detail="送货单只能包含同一客户的明细")
        if order_item.material_status != "received":
            raise HTTPException(status_code=400, detail="材料未到的订单不能生成送货单")
        amount = money(order_item.unit_price * Decimal(line.delivery_qty))
        delivery.items.append(
            DeliveryItem(
                order_item_id=order_item.id,
                delivery_qty=line.delivery_qty,
                unit_price_snapshot=order_item.unit_price,
                amount_snapshot=amount,
                remark=line.remark,
            )
        )
        order_item.delivered_quantity += line.delivery_qty
        if order_item.delivered_quantity >= order_item.quantity:
            order_item.order.status = "delivered"
        elif order_item.delivered_quantity > 0:
            order_item.order.status = "partially_delivered"
        total_qty += line.delivery_qty
    delivery.total_quantity = total_qty
    session.add(delivery)
    session.commit()
    session.refresh(delivery)
    return delivery_dict(delivery)


@router.get("")
def list_deliveries(status: str | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    query = select(DeliveryNote).order_by(DeliveryNote.delivery_date.desc(), DeliveryNote.id.desc())
    if status:
        query = query.where(DeliveryNote.status == status)
    deliveries = session.scalars(query).unique().all()
    return {"total": len(deliveries), "items": [delivery_dict(item) for item in deliveries]}
