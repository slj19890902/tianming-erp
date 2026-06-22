from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from phase1_postgres.database import get_session
from phase1_postgres.models import DeliveryItem, DeliveryNote, ReturnReceipt, ReturnReceiptItem


router = APIRouter(prefix="/api/receipts", tags=["receipts"])


class ReceiptItemPayload(BaseModel):
    delivery_item_id: int
    actual_signed_qty: int = Field(ge=0)
    difference_reason: str | None = None


class ReceiptPayload(BaseModel):
    delivery_id: int
    actual_received_date: date
    signed_by: str | None = None
    note: str | None = None
    items: list[ReceiptItemPayload] = Field(min_length=1)


class OwnerConfirmPayload(BaseModel):
    reviewed_by: str | None = None


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00"), rounding=ROUND_HALF_UP)


def receipt_item_dict(item: ReturnReceiptItem) -> dict[str, Any]:
    delivery_item = item.delivery_item
    order_item = delivery_item.order_item
    return {
        "id": item.id,
        "delivery_item_id": item.delivery_item_id,
        "order_number": order_item.order.order_number,
        "product_name": order_item.snapshot_product_name,
        "delivery_qty": delivery_item.delivery_qty,
        "actual_signed_qty": item.actual_signed_qty,
        "unit_price": str(item.unit_price_snapshot),
        "amount": str(item.amount),
        "difference_reason": item.difference_reason,
    }


def receipt_dict(receipt: ReturnReceipt) -> dict[str, Any]:
    return {
        "id": receipt.id,
        "delivery_id": receipt.delivery_id,
        "delivery_number": receipt.delivery.delivery_number,
        "customer_id": receipt.delivery.customer_id,
        "customer_name": receipt.delivery.customer.name if receipt.delivery.customer else None,
        "actual_received_date": receipt.actual_received_date.isoformat(),
        "signed_by": receipt.signed_by,
        "status": receipt.status,
        "owner_reviewed_by": receipt.owner_reviewed_by,
        "owner_reviewed_at": receipt.owner_reviewed_at.isoformat() if receipt.owner_reviewed_at else None,
        "items": [receipt_item_dict(item) for item in receipt.items],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_receipt(payload: ReceiptPayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    delivery = session.get(DeliveryNote, payload.delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="送货单不存在")
    if delivery.status not in {"DELIVERED_WAIT_RECEIPT", "SIGNED_WAIT_OWNER_REVIEW"}:
        raise HTTPException(status_code=400, detail="该送货单当前状态不能录入回单")

    receipt = ReturnReceipt(
        delivery_id=delivery.id,
        actual_received_date=payload.actual_received_date,
        signed_by=payload.signed_by,
        status="SIGNED_WAIT_OWNER_REVIEW",
        note=payload.note,
    )
    for line in payload.items:
        delivery_item = session.get(DeliveryItem, line.delivery_item_id)
        if delivery_item is None or delivery_item.delivery_id != delivery.id:
            raise HTTPException(status_code=400, detail="回单明细不属于该送货单")
        if line.actual_signed_qty != delivery_item.delivery_qty and not line.difference_reason:
            raise HTTPException(status_code=400, detail="实签数量与送货数量不一致时必须填写差异原因")
        amount = money(delivery_item.unit_price_snapshot * Decimal(line.actual_signed_qty))
        receipt.items.append(
            ReturnReceiptItem(
                delivery_item_id=delivery_item.id,
                actual_signed_qty=line.actual_signed_qty,
                unit_price_snapshot=delivery_item.unit_price_snapshot,
                amount=amount,
                difference_reason=line.difference_reason,
            )
        )
    delivery.status = "SIGNED_WAIT_OWNER_REVIEW"
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    return receipt_dict(receipt)


@router.get("")
def list_receipts(status: str | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    query = session.query(ReturnReceipt)
    if status:
        query = query.filter(ReturnReceipt.status == status)
    receipts = query.order_by(ReturnReceipt.actual_received_date.desc(), ReturnReceipt.id.desc()).all()
    return {"total": len(receipts), "items": [receipt_dict(item) for item in receipts]}


@router.post("/{receipt_id}/owner-confirm")
def owner_confirm_receipt(
    receipt_id: int,
    payload: OwnerConfirmPayload,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    receipt = session.get(ReturnReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="回单不存在")
    if receipt.status != "SIGNED_WAIT_OWNER_REVIEW":
        raise HTTPException(status_code=400, detail="只有已回签待核对的回单可以老板确认")
    receipt.status = "OWNER_REVIEWED"
    receipt.owner_reviewed_by = payload.reviewed_by or "老板"
    receipt.owner_reviewed_at = session.scalar(func.now())
    receipt.delivery.status = "OWNER_REVIEWED"
    session.commit()
    session.refresh(receipt)
    return receipt_dict(receipt)
