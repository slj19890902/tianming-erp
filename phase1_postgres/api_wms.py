from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from phase1_postgres.database import get_session
from phase1_postgres.models import Requisition, RequisitionItem, WmsReceiveLog


router = APIRouter(prefix="/api/wms", tags=["wms"])


class ReceivePayload(BaseModel):
    actual_receive_qty: int = Field(gt=0)
    received_by: str | None = None
    note: str | None = None


def wms_item_dict(item: RequisitionItem) -> dict[str, Any]:
    order_item = item.order_item
    req = item.requisition
    remaining = max(0, item.requisition_qty - item.received_qty)
    return {
        "id": item.id,
        "requisition_id": item.requisition_id,
        "requisition_number": req.requisition_number,
        "supplier_name": req.supplier_name,
        "order_number": order_item.order.order_number,
        "customer_name": order_item.order.customer.name if order_item.order.customer else None,
        "product_name": order_item.snapshot_product_name,
        "spec": order_item.snapshot_spec,
        "material": order_item.snapshot_material,
        "flute_type": order_item.snapshot_flute_type,
        "paper_length_mm": None if order_item.snapshot_cardboard_length_mm is None else str(order_item.snapshot_cardboard_length_mm),
        "paper_width_mm": None if order_item.snapshot_cardboard_width_mm is None else str(order_item.snapshot_cardboard_width_mm),
        "score_line": order_item.snapshot_score_line,
        "requisition_qty": item.requisition_qty,
        "received_qty": item.received_qty,
        "remaining_qty": remaining,
        "status": item.status,
    }


@router.get("/pending")
def pending_wms(
    supplier_name: str | None = None,
    requisition_id: int | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = select(RequisitionItem).join(Requisition).where(RequisitionItem.status != "COMPLETED")
    if supplier_name:
        query = query.where(Requisition.supplier_name == supplier_name)
    if requisition_id is not None:
        query = query.where(RequisitionItem.requisition_id == requisition_id)
    items = session.scalars(query.order_by(Requisition.supplier_name, RequisitionItem.id)).all()
    summary_map: dict[str, int] = {}
    for item in items:
        summary_map[item.requisition.supplier_name] = summary_map.get(item.requisition.supplier_name, 0) + max(
            0, item.requisition_qty - item.received_qty
        )
    return {
        "total": len(items),
        "summary": [
            {"supplier_name": supplier, "remaining_qty": qty}
            for supplier, qty in summary_map.items()
        ],
        "items": [wms_item_dict(item) for item in items],
    }


@router.put("/receive/{requisition_item_id}")
def receive_item(
    requisition_item_id: int,
    payload: ReceivePayload,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    item = session.get(RequisitionItem, requisition_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="报料明细不存在")
    remaining_before = max(0, item.requisition_qty - item.received_qty)
    if remaining_before <= 0:
        raise HTTPException(status_code=400, detail="该批次已完成收料")
    if payload.actual_receive_qty > remaining_before:
        raise HTTPException(status_code=400, detail="实收数量不能大于剩余待收数量")

    item.received_qty += payload.actual_receive_qty
    remaining_after = max(0, item.requisition_qty - item.received_qty)
    item.status = "COMPLETED" if remaining_after == 0 else "PARTIAL"
    item.order_item.material_status = "received" if remaining_after == 0 else "pending"
    item.order_item.requisition_status = "已入库" if remaining_after == 0 else "已报料"
    session.add(
        WmsReceiveLog(
            requisition_item_id=item.id,
            actual_receive_qty=payload.actual_receive_qty,
            received_by=payload.received_by,
            note=payload.note,
        )
    )
    session.commit()
    session.refresh(item)
    return wms_item_dict(item)


@router.get("/requisition-items/{requisition_item_id}/process-panel")
def process_panel(requisition_item_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    item = session.get(RequisitionItem, requisition_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="报料明细不存在")
    order_item = item.order_item
    order = order_item.order
    route = ["印刷", "开槽/模切", "打钉/粘箱", "待送货"]
    return {
        "requisition_item_id": item.id,
        "order_number": order.order_number,
        "customer_name": order.customer.name if order.customer else None,
        "product_name": order_item.snapshot_product_name,
        "spec": order_item.snapshot_spec,
        "material": order_item.snapshot_material,
        "paper_length_mm": None if order_item.snapshot_cardboard_length_mm is None else str(order_item.snapshot_cardboard_length_mm),
        "paper_width_mm": None if order_item.snapshot_cardboard_width_mm is None else str(order_item.snapshot_cardboard_width_mm),
        "score_line": order_item.snapshot_score_line,
        "process_route": route,
        "drawing_status": "暂无图纸，按工艺说明生产",
    }
