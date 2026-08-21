from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    RoleChecker,
    customer_scope_ids,
    get_db,
    has_unrestricted_customer_access,
)
from app.models.customer import Customer
from app.models.order import Order
from app.models.user import User
from app.services.audit_log import append_audit_event
from app.services.external_packaging_purchase import (
    ExternalPurchaseContractError,
    build_external_purchase_print,
    build_external_purchase_preview,
    confirm_external_purchase,
    list_external_purchase_routing_rows,
    serialize_external_purchase_batch,
    get_external_purchase_summary,
)
from app.services.external_packaging_purchase_lifecycle import (
    ExternalPackagingPurchaseLifecycleError,
    cancel_unreceived_external_purchases,
)
from app.services.external_packaging_receiving import (
    build_external_receiving_overview,
    record_external_purchase_receipt,
    serialize_external_receipt,
)


router = APIRouter()
admin_only = RoleChecker(["admin"])
can_cost = PermissionChecker("cost.view")
can_incoming_read = PermissionChecker("incoming.view")
can_incoming_execute = PermissionChecker("incoming.execute")
can_requisition_read = PermissionChecker("requisition.view")


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


class ExternalPurchaseCancelPayload(BaseModel):
    expected_batch_id: int = Field(gt=0)
    confirmed: Literal[True]
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def clean_reason(cls, value: Any) -> str:
        return str(value or "").strip()


class ExternalReceiptLinePayload(BaseModel):
    purchase_item_id: int = Field(gt=0)
    received_quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=6)


class ExternalReceiptPayload(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    lines: list[ExternalReceiptLinePayload] = Field(min_length=1, max_length=100)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def clean_key(cls, value: Any) -> str:
        return str(value or "").strip()


def _translate(error: ExternalPurchaseContractError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


@router.get("/external-packaging-purchases/pending-confirmations")
def get_external_packaging_pending_confirmations(
    db: Session = Depends(get_db),
    user: User = Depends(can_requisition_read),
) -> dict[str, Any]:
    items = list_external_purchase_routing_rows(
        db,
        visible_customer_ids=_visible_customer_ids(user, db),
    )
    return {"items": items, "total": len(items)}


@router.get("/external-packaging-purchases/pending-receipts")
def get_external_packaging_pending_receipts(
    db: Session = Depends(get_db),
    user: User = Depends(can_incoming_read),
) -> dict[str, Any]:
    return build_external_receiving_overview(
        db,
        visible_customer_ids=_visible_customer_ids(user, db),
    )


@router.post("/external-packaging-purchases/{purchase_order_id}/receipts")
def receive_external_packaging_purchase(
    purchase_order_id: int,
    payload: ExternalReceiptPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_incoming_execute),
) -> dict[str, Any]:
    lines = [row.model_dump() for row in payload.lines]
    visible_ids = _visible_customer_ids(user, db)
    try:
        receipt, created = record_external_purchase_receipt(
            db,
            purchase_order_id=purchase_order_id,
            idempotency_key=payload.idempotency_key,
            lines=lines,
            user=user,
            visible_customer_ids=visible_ids,
        )
        if created:
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="external_packaging_receiving",
                action_code="external_packaging.receipt.post",
                resource="ExternalPackagingReceipt",
                legacy_action="RECEIVE_EXTERNAL_PACKAGING",
                actor=user,
                entity_type="external_packaging_receipt",
                entity_id=receipt.id,
                object_ref=receipt.receipt_number,
                description="确认外购包装本次实收",
                details={
                    "purchase_order_id": purchase_order_id,
                    "receipt_number": receipt.receipt_number,
                    "line_count": len(lines),
                    "original_units_preserved": True,
                    "prices_redacted": True,
                    "no_inventory_created": True,
                    "no_location_required": True,
                },
            )
            db.commit()
        return {
            "created": created,
            "receipt": serialize_external_receipt(receipt),
            "overview": build_external_receiving_overview(
                db, visible_customer_ids=visible_ids
            ),
        }
    except IntegrityError:
        db.rollback()
        try:
            receipt, created = record_external_purchase_receipt(
                db,
                purchase_order_id=purchase_order_id,
                idempotency_key=payload.idempotency_key,
                lines=lines,
                user=user,
                visible_customer_ids=visible_ids,
            )
        except ExternalPurchaseContractError as error:
            raise _translate(error) from error
        if created:
            db.rollback()
            raise HTTPException(status_code=409, detail="收料发生并发冲突，请刷新后核对")
        return {
            "created": False,
            "receipt": serialize_external_receipt(receipt),
            "overview": build_external_receiving_overview(
                db, visible_customer_ids=visible_ids
            ),
        }
    except ExternalPurchaseContractError as error:
        db.rollback()
        raise _translate(error) from error
    except OperationalError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="订单或采购正在被其他操作处理，请刷新后重试",
        ) from error


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


@router.get("/external-packaging-purchases/{purchase_order_id}/print")
def print_external_packaging_purchase(
    purchase_order_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_only),
    _cost_user: User = Depends(can_cost),
) -> dict[str, Any]:
    try:
        return build_external_purchase_print(db, purchase_order_id)
    except ExternalPurchaseContractError as error:
        raise _translate(error) from error


@router.post("/orders/{order_id}/external-packaging-purchase/cancel")
def cancel_external_packaging_purchase(
    order_id: int,
    payload: ExternalPurchaseCancelPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
    _cost_user: User = Depends(can_cost),
) -> dict[str, Any]:
    try:
        order = db.get(Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="订单不存在")
        summary = get_external_purchase_summary(db, order_id)
        if summary.get("status") != "confirmed":
            raise HTTPException(status_code=409, detail="当前没有可撤销的有效外购包材采购")
        if int(summary.get("batch_id") or 0) != payload.expected_batch_id:
            raise HTTPException(status_code=409, detail="采购记录已变化，请刷新后重新核对")
        changes = cancel_unreceived_external_purchases(
            db,
            order_id=order_id,
            source="manual_purchase_cancel",
            reason=payload.reason,
            cancelled_by=user.id,
        )
        if not changes:
            raise HTTPException(status_code=409, detail="当前没有可撤销的有效外购包材采购")
        append_audit_event(
            db,
            event_category="business",
            result="success",
            source="web",
            module_code="external_packaging_purchase",
            action_code="external_packaging.purchase.cancel",
            resource="ExternalPackagingPurchaseBatch",
            legacy_action="CANCEL_PURCHASE",
            actor=user,
            entity_type="external_packaging_purchase_batch",
            entity_id=payload.expected_batch_id,
            object_ref=order.order_number,
            customer_id=order.customer_id,
            description="人工撤销未实收的外购包材采购",
            details={
                "sales_order_id": order_id,
                "expected_batch_id": payload.expected_batch_id,
                "reason": payload.reason,
                "changes": changes,
                "received_purchase_blocked": True,
                "history_preserved": True,
            },
        )
        db.commit()
        return {
            "cancelled": True,
            "changes": changes,
            "preview": build_external_purchase_preview(db, order_id),
        }
    except ExternalPackagingPurchaseLifecycleError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except HTTPException:
        db.rollback()
        raise
    except (IntegrityError, OperationalError) as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="采购撤销发生并发冲突，请刷新后重新核对",
        ) from error


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
    except OperationalError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="订单或采购正在被其他操作处理，请刷新后重试",
        ) from error
