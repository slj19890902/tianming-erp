from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import PermissionChecker, RoleChecker, get_db
from app.models.customer import Customer
from app.models.order import Order
from app.models.user import User
from app.services.audit_log import append_audit_event
from app.services.external_packaging_purchase import (
    ExternalPurchaseContractError,
    build_external_purchase_preview,
    confirm_external_purchase,
    serialize_external_purchase_batch,
)


router = APIRouter()
admin_only = RoleChecker(["admin"])
can_cost = PermissionChecker("cost.view")


class ExternalPurchaseLinePayload(BaseModel):
    order_component_id: int = Field(gt=0)
    candidate_id: int = Field(gt=0)
    purchase_quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=6)


class ExternalPurchaseConfirmPayload(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    lines: list[ExternalPurchaseLinePayload] = Field(min_length=1, max_length=100)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def clean_key(cls, value: Any) -> str:
        return str(value or "").strip()


def _translate(error: ExternalPurchaseContractError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


@router.get("/orders/{order_id}/external-packaging-purchase")
def get_external_packaging_purchase_preview(
    order_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_only),
    _cost_user: User = Depends(can_cost),
) -> dict[str, Any]:
    try:
        return build_external_purchase_preview(db, order_id)
    except ExternalPurchaseContractError as error:
        raise _translate(error) from error


@router.post("/orders/{order_id}/external-packaging-purchase/confirm")
def confirm_external_packaging_purchase(
    order_id: int,
    payload: ExternalPurchaseConfirmPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
    _cost_user: User = Depends(can_cost),
) -> dict[str, Any]:
    lines = [row.model_dump() for row in payload.lines]
    try:
        batch, created = confirm_external_purchase(
            db,
            order_id=order_id,
            idempotency_key=payload.idempotency_key,
            lines=lines,
            user=user,
        )
        if created:
            order = db.get(Order, order_id)
            customer = db.get(Customer, order.customer_id) if order is not None else None
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="external_packaging_purchase",
                action_code="external_packaging.purchase.confirm",
                resource="ExternalPackagingPurchaseBatch",
                legacy_action="CONFIRM_PURCHASE",
                actor=user,
                entity_type="external_packaging_purchase_batch",
                entity_id=batch.id,
                object_ref=(order.order_number if order is not None else str(order_id)),
                customer_id=(order.customer_id if order is not None else None),
                customer_name=(customer.name if customer is not None else None),
                batch_id=str(batch.id),
                description="人工确认订单外购包装采购",
                details={
                    "sales_order_id": order_id,
                    "component_count": len(lines),
                    "purchase_numbers": [
                        row.purchase_number for row in batch.purchase_orders
                    ],
                    "price_values_redacted": True,
                    "no_receipt_or_inventory_created": True,
                },
            )
            db.commit()
        return {
            "created": created,
            "confirmation": serialize_external_purchase_batch(batch),
        }
    except IntegrityError:
        db.rollback()
        try:
            batch, created = confirm_external_purchase(
                db,
                order_id=order_id,
                idempotency_key=payload.idempotency_key,
                lines=lines,
                user=user,
            )
        except ExternalPurchaseContractError as error:
            raise _translate(error) from error
        if created:
            db.rollback()
            raise HTTPException(status_code=409, detail="采购确认发生并发冲突，请刷新后核对")
        return {
            "created": False,
            "confirmation": serialize_external_purchase_batch(batch),
        }
    except ExternalPurchaseContractError as error:
        db.rollback()
        raise _translate(error) from error
