from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseBatch,
    ExternalPackagingPurchaseCancellation,
    ExternalPackagingPurchaseItem,
    ExternalPackagingPurchaseOrder,
    ExternalPackagingReceiptItem,
)
from app.models.stock_replenishment import StockReplenishmentOrder
from app.core.time_contract import utc_now_naive
from app.services.external_receipt_state import active_receipt_item


class ExternalPackagingPurchaseLifecycleError(ValueError):
    pass


def cancelled_external_purchase_order_ids(
    db: Session,
    purchase_order_ids: set[int] | None = None,
) -> set[int]:
    statement = select(ExternalPackagingPurchaseCancellation.purchase_order_id)
    if purchase_order_ids is not None:
        if not purchase_order_ids:
            return set()
        statement = statement.where(
            ExternalPackagingPurchaseCancellation.purchase_order_id.in_(
                purchase_order_ids
            )
        )
    return {int(value) for value in db.scalars(statement).all()}


def active_external_purchase_orders_for_order_ids(
    db: Session,
    order_ids: set[int],
) -> list[ExternalPackagingPurchaseOrder]:
    if not order_ids:
        return []
    return list(
        db.scalars(
            select(ExternalPackagingPurchaseOrder)
            .join(
                ExternalPackagingPurchaseBatch,
                ExternalPackagingPurchaseBatch.id
                == ExternalPackagingPurchaseOrder.batch_id,
            )
            .outerjoin(
                ExternalPackagingPurchaseCancellation,
                ExternalPackagingPurchaseCancellation.purchase_order_id
                == ExternalPackagingPurchaseOrder.id,
            )
            .options(selectinload(ExternalPackagingPurchaseOrder.items))
            .where(
                ExternalPackagingPurchaseBatch.sales_order_id.in_(order_ids),
                ExternalPackagingPurchaseOrder.status == "confirmed",
                ExternalPackagingPurchaseCancellation.id.is_(None),
            )
            .order_by(ExternalPackagingPurchaseOrder.id)
            .with_for_update(of=ExternalPackagingPurchaseOrder)
        ).unique().all()
    )


def cancel_unreceived_external_purchases(
    db: Session,
    *,
    order_id: int,
    source: str,
    reason: str,
    cancelled_by: int | None,
    batch_id: int | None = None,
) -> list[dict]:
    purchases = active_external_purchase_orders_for_order_ids(db, {order_id})
    if batch_id is not None:
        purchases = [purchase for purchase in purchases if purchase.batch_id == batch_id]
    if not purchases:
        return []
    purchase_item_ids = {
        int(item.id) for purchase in purchases for item in purchase.items
    }
    received_by_purchase: dict[int, Decimal] = {}
    if purchase_item_ids:
        received_by_purchase = {
            int(purchase_order_id): Decimal(str(quantity or 0))
            for purchase_order_id, quantity in db.execute(
                select(
                    ExternalPackagingPurchaseItem.purchase_order_id,
                    func.sum(ExternalPackagingReceiptItem.received_quantity),
                )
                .join(
                    ExternalPackagingReceiptItem,
                    ExternalPackagingReceiptItem.purchase_item_id
                    == ExternalPackagingPurchaseItem.id,
                )
                .where(ExternalPackagingPurchaseItem.id.in_(purchase_item_ids), active_receipt_item())
                .group_by(ExternalPackagingPurchaseItem.purchase_order_id)
            ).all()
        }
    received = [
        purchase
        for purchase in purchases
        if received_by_purchase.get(int(purchase.id), Decimal("0")) > 0
    ]
    if received:
        numbers = "、".join(row.purchase_number for row in received)
        raise ExternalPackagingPurchaseLifecycleError(
            f"外购包材采购单 {numbers} 已有实收，不能自动撤回或废弃订单；请先处理包材实收。"
        )

    changes: list[dict] = []
    for purchase in purchases:
        cancellation = ExternalPackagingPurchaseCancellation(
            purchase_order_id=purchase.id,
            source=source,
            reason=reason,
            cancelled_by=cancelled_by,
        )
        db.add(cancellation)
        changes.append(
            {
                "purchase_order_id": int(purchase.id),
                "purchase_number": purchase.purchase_number,
                "action": "cancel_purchase",
                "source": source,
            }
        )
    db.flush()
    return changes


def cancel_unreceived_stock_replenishment_purchase(
    db: Session,
    *,
    purchase_order_id: int,
    expected_batch_id: int,
    reason: str,
    cancelled_by: int | None,
) -> dict:
    purchase = db.scalar(
        select(ExternalPackagingPurchaseOrder)
        .options(selectinload(ExternalPackagingPurchaseOrder.items))
        .where(ExternalPackagingPurchaseOrder.id == purchase_order_id)
        .with_for_update(of=ExternalPackagingPurchaseOrder)
    )
    if purchase is None or int(purchase.batch_id) != int(expected_batch_id):
        raise ExternalPackagingPurchaseLifecycleError(
            "采购记录已变化，请刷新后重新核对"
        )
    batch = db.get(ExternalPackagingPurchaseBatch, int(expected_batch_id))
    if (
        batch is None
        or batch.sales_order_id is not None
        or batch.stock_replenishment_order_id is None
    ):
        raise ExternalPackagingPurchaseLifecycleError(
            "当前采购单不是库存预警外购备库来源"
        )
    existing = db.scalar(
        select(ExternalPackagingPurchaseCancellation.id).where(
            ExternalPackagingPurchaseCancellation.purchase_order_id == purchase.id
        )
    )
    if existing is not None:
        raise ExternalPackagingPurchaseLifecycleError("当前采购单已经撤销")
    received = Decimal(
        db.scalar(
            select(func.coalesce(func.sum(ExternalPackagingReceiptItem.received_quantity), 0))
            .join(
                ExternalPackagingPurchaseItem,
                ExternalPackagingPurchaseItem.id
                == ExternalPackagingReceiptItem.purchase_item_id,
            )
            .where(ExternalPackagingPurchaseItem.purchase_order_id == purchase.id, active_receipt_item())
        )
        or 0
    )
    if received > 0:
        raise ExternalPackagingPurchaseLifecycleError(
            f"外购包材采购单 {purchase.purchase_number} 已有实收，不能撤销；请先处理包材实收。"
        )
    replenishment = db.get(
        StockReplenishmentOrder, int(batch.stock_replenishment_order_id)
    )
    if replenishment is None:
        raise ExternalPackagingPurchaseLifecycleError("库存补库来源不存在")
    if replenishment.status in {"partially_stocked", "stocked"}:
        raise ExternalPackagingPurchaseLifecycleError(
            "库存补库已经形成成品库存，不能撤销采购"
        )
    db.add(
        ExternalPackagingPurchaseCancellation(
            purchase_order_id=purchase.id,
            source="manual_purchase_cancel",
            reason=reason,
            cancelled_by=cancelled_by,
        )
    )
    replenishment.status = "voided"
    replenishment.voided_at = utc_now_naive()
    db.flush()
    return {
        "purchase_order_id": int(purchase.id),
        "purchase_number": purchase.purchase_number,
        "stock_replenishment_order_id": int(replenishment.id),
        "stock_replenishment_order_number": replenishment.order_number,
        "action": "cancel_purchase_and_void_replenishment",
    }
