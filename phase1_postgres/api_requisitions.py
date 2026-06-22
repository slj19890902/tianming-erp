from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from phase1_postgres.database import get_session
from phase1_postgres.models import OrderItem, Requisition, RequisitionItem, SalesOrder


router = APIRouter(prefix="/api/requisitions", tags=["requisitions"])


class RequisitionLinePayload(BaseModel):
    order_item_id: int
    requisition_qty: int = Field(gt=0)
    note: str | None = None


class RequisitionPayload(BaseModel):
    supplier_name: str = Field(min_length=1, max_length=255)
    requisition_date: date | None = None
    note: str | None = None
    items: list[RequisitionLinePayload] = Field(min_length=1)


def _next_requisition_number(session: Session, today: date) -> str:
    prefix = f"BL-{today:%Y%m%d}-"
    max_number = session.scalar(
        select(func.max(Requisition.requisition_number)).where(
            Requisition.requisition_number.like(f"{prefix}%")
        )
    )
    if not max_number:
        return f"{prefix}001"
    return f"{prefix}{int(str(max_number).rsplit('-', 1)[1]) + 1:03d}"


def pending_item_dict(item: OrderItem) -> dict[str, Any]:
    order = item.order
    return {
        "order_item_id": item.id,
        "order_number": order.order_number,
        "customer_name": order.customer.name if order.customer else None,
        "product_code": item.snapshot_product_code,
        "product_name": item.snapshot_product_name,
        "spec": item.snapshot_spec,
        "material": item.snapshot_material,
        "flute_type": item.snapshot_flute_type,
        "paper_length_mm": None if item.snapshot_cardboard_length_mm is None else str(item.snapshot_cardboard_length_mm),
        "paper_width_mm": None if item.snapshot_cardboard_width_mm is None else str(item.snapshot_cardboard_width_mm),
        "score_line": item.snapshot_score_line,
        "quantity": item.quantity,
        "requisition_qty": item.requisition_qty or item.quantity,
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
    }


def requisition_item_dict(item: RequisitionItem) -> dict[str, Any]:
    order_item = item.order_item
    remaining = max(0, item.requisition_qty - item.received_qty)
    return {
        "id": item.id,
        "requisition_id": item.requisition_id,
        "order_item_id": item.order_item_id,
        "order_number": order_item.order.order_number,
        "customer_name": order_item.order.customer.name if order_item.order.customer else None,
        "product_name": order_item.snapshot_product_name,
        "material": order_item.snapshot_material,
        "paper_length_mm": None if order_item.snapshot_cardboard_length_mm is None else str(order_item.snapshot_cardboard_length_mm),
        "paper_width_mm": None if order_item.snapshot_cardboard_width_mm is None else str(order_item.snapshot_cardboard_width_mm),
        "score_line": order_item.snapshot_score_line,
        "requisition_qty": item.requisition_qty,
        "received_qty": item.received_qty,
        "remaining_qty": remaining,
        "status": item.status,
    }


def requisition_dict(req: Requisition) -> dict[str, Any]:
    total_qty = sum(item.requisition_qty for item in req.items)
    return {
        "id": req.id,
        "requisition_number": req.requisition_number,
        "supplier_name": req.supplier_name,
        "requisition_date": req.requisition_date.isoformat(),
        "status": req.status,
        "total_qty": total_qty,
        "items": [requisition_item_dict(item) for item in req.items],
        "print_url": f"/requisition-print.html?id={req.id}",
        "mobile_receive_url": f"/mobile-receive?requisition_id={req.id}",
    }


@router.get("/pending-items")
def list_pending_items(session: Session = Depends(get_session)) -> dict[str, Any]:
    existing = select(RequisitionItem.order_item_id)
    query = (
        select(OrderItem)
        .join(SalesOrder)
        .where(
            OrderItem.requisition_status == "未报料",
            OrderItem.production_source != "STOCK_MATERIAL",
            ~OrderItem.id.in_(existing),
        )
        .order_by(SalesOrder.delivery_date, OrderItem.id)
    )
    items = session.scalars(query).all()
    return {"total": len(items), "items": [pending_item_dict(item) for item in items]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_requisition(payload: RequisitionPayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    today = payload.requisition_date or date.today()
    req = Requisition(
        requisition_number=_next_requisition_number(session, today),
        supplier_name=payload.supplier_name.strip(),
        requisition_date=today,
        status="PENDING",
        note=payload.note,
    )
    for line in payload.items:
        order_item = session.get(OrderItem, line.order_item_id)
        if order_item is None:
            raise HTTPException(status_code=400, detail="订单明细不存在")
        if order_item.production_source == "STOCK_MATERIAL":
            raise HTTPException(status_code=400, detail="库存料生产明细不能报料")
        order_item.requisition_status = "已报料"
        order_item.requisition_qty = line.requisition_qty
        order_item.requisition_date = today
        req.items.append(
            RequisitionItem(
                order_item_id=order_item.id,
                requisition_qty=line.requisition_qty,
                received_qty=0,
                status="PENDING",
                note=line.note,
            )
        )
    session.add(req)
    session.commit()
    session.refresh(req)
    return requisition_dict(req)
