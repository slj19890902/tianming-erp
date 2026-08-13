from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.customer import Customer
from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseBatch,
    ExternalPackagingPurchaseCancellation,
    ExternalPackagingPurchaseItem,
    ExternalPackagingPurchaseOrder,
    ExternalPackagingReceipt,
    ExternalPackagingReceiptItem,
)
from app.models.order import Order
from app.models.user import User
from app.services.external_packaging_purchase import (
    ExternalPurchaseContractError,
    claim_external_purchase_order,
)
from app.services.order_external_packaging import DISCRETE_PURCHASE_UNITS


SIX_PLACES = Decimal("0.000001")


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ExternalPurchaseContractError(
            "实收数量格式不正确", status_code=422
        ) from error
    if not result.is_finite() or result <= 0:
        raise ExternalPurchaseContractError("实收数量必须大于 0", status_code=422)
    if result.quantize(SIX_PLACES) != result:
        raise ExternalPurchaseContractError(
            "实收数量最多保留 6 位小数", status_code=422
        )
    return result


def _text(value: Decimal | None) -> str:
    rendered = format(Decimal(value or 0).quantize(SIX_PLACES), "f")
    return rendered.rstrip("0").rstrip(".") or "0"


def _request_fingerprint(
    purchase_order_id: int, lines: list[dict[str, Any]]
) -> str:
    normalized = sorted(
        [
            {
                "purchase_item_id": int(row["purchase_item_id"]),
                "received_quantity": _text(_decimal(row["received_quantity"])),
            }
            for row in lines
        ],
        key=lambda row: row["purchase_item_id"],
    )
    return hashlib.sha256(
        json.dumps(
            {"purchase_order_id": purchase_order_id, "lines": normalized},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _received_quantity_aggregate(
    purchase_item_ids: set[int] | None = None,
):
    statement = select(
        ExternalPackagingReceiptItem.purchase_item_id.label("purchase_item_id"),
        func.sum(ExternalPackagingReceiptItem.received_quantity).label(
            "received_quantity"
        ),
    ).group_by(ExternalPackagingReceiptItem.purchase_item_id)
    if purchase_item_ids is not None:
        statement = statement.where(
            ExternalPackagingReceiptItem.purchase_item_id.in_(purchase_item_ids)
        )
    return statement


def _received_quantity_at_formal_precision(value):
    return func.round(func.coalesce(value, 0), 6)


def _received_totals(
    db: Session, purchase_item_ids: set[int]
) -> dict[int, Decimal]:
    if not purchase_item_ids:
        return {}
    aggregate = _received_quantity_aggregate(purchase_item_ids).subquery()
    rows = db.execute(
        select(
            aggregate.c.purchase_item_id,
            _received_quantity_at_formal_precision(aggregate.c.received_quantity),
        )
    ).all()
    return {
        int(item_id): Decimal(str(quantity or 0)).quantize(SIX_PLACES)
        for item_id, quantity in rows
    }


def _purchase_status(
    items: list[ExternalPackagingPurchaseItem], totals: dict[int, Decimal]
) -> str:
    received = sum((totals.get(row.id, Decimal("0")) for row in items), Decimal("0"))
    if received <= 0:
        return "pending_receipt"
    if all(totals.get(row.id, Decimal("0")) >= Decimal(row.purchase_quantity) for row in items):
        return "received"
    return "partially_received"


def purchase_receipt_progress(
    db: Session, purchase_order_ids: set[int]
) -> dict[int, dict[str, Any]]:
    if not purchase_order_ids:
        return {}
    cancelled_purchase_ids = set(
        db.scalars(
            select(ExternalPackagingPurchaseCancellation.purchase_order_id).where(
                ExternalPackagingPurchaseCancellation.purchase_order_id.in_(
                    purchase_order_ids
                )
            )
        ).all()
    )
    purchases = list(
        db.scalars(
            select(ExternalPackagingPurchaseOrder)
            .options(selectinload(ExternalPackagingPurchaseOrder.items))
            .where(
                ExternalPackagingPurchaseOrder.id.in_(purchase_order_ids),
                ExternalPackagingPurchaseOrder.id.notin_(cancelled_purchase_ids),
            )
        ).all()
    )
    all_item_ids = {item.id for purchase in purchases for item in purchase.items}
    totals = _received_totals(db, all_item_ids)
    result: dict[int, dict[str, Any]] = {}
    for purchase in purchases:
        result[purchase.id] = {
            "status": _purchase_status(purchase.items, totals),
            "received_line_count": sum(
                1 for item in purchase.items if totals.get(item.id, Decimal("0")) > 0
            ),
            "complete_line_count": sum(
                1
                for item in purchase.items
                if totals.get(item.id, Decimal("0")) >= Decimal(item.purchase_quantity)
            ),
            "line_count": len(purchase.items),
        }
    return result


def _purchase_payload(
    purchase: ExternalPackagingPurchaseOrder,
    *,
    sales_order: Order,
    customer: Customer | None,
    totals: dict[int, Decimal],
) -> dict[str, Any]:
    item_rows = []
    for item in purchase.items:
        ordered = Decimal(item.purchase_quantity)
        received = totals.get(item.id, Decimal("0"))
        remaining = max(ordered - received, Decimal("0"))
        item_rows.append(
            {
                "purchase_item_id": item.id,
                "purpose": item.purpose_snapshot,
                "supplier_product_code": item.supplier_product_code_snapshot,
                "product_name": item.product_name_snapshot,
                "specification_summary": item.specification_summary_snapshot,
                "ordered_quantity": _text(ordered),
                "received_quantity": _text(received),
                "remaining_quantity": _text(remaining),
                "purchase_unit": item.purchase_unit,
            }
        )
    return {
        "id": purchase.id,
        "purchase_number": purchase.purchase_number,
        "supplier_id": purchase.supplier_id,
        "supplier_name": purchase.supplier_name_snapshot,
        "sales_order_id": sales_order.id,
        "order_number": sales_order.order_number,
        "customer_id": sales_order.customer_id,
        "customer_name": customer.name if customer is not None else "客户待确认",
        "status": _purchase_status(purchase.items, totals),
        "items": item_rows,
    }


def build_external_receiving_overview(
    db: Session,
    *,
    visible_customer_ids: set[int] | None,
    include_completed: bool = False,
) -> dict[str, Any]:
    query = (
        select(ExternalPackagingPurchaseOrder)
        .join(
            ExternalPackagingPurchaseBatch,
            ExternalPackagingPurchaseBatch.id
            == ExternalPackagingPurchaseOrder.batch_id,
        )
        .join(Order, Order.id == ExternalPackagingPurchaseBatch.sales_order_id)
        .outerjoin(
            ExternalPackagingPurchaseCancellation,
            ExternalPackagingPurchaseCancellation.purchase_order_id
            == ExternalPackagingPurchaseOrder.id,
        )
        .options(
            joinedload(ExternalPackagingPurchaseOrder.batch),
            selectinload(ExternalPackagingPurchaseOrder.items),
        )
        .order_by(
            ExternalPackagingPurchaseOrder.confirmed_at,
            ExternalPackagingPurchaseOrder.id,
        )
        .where(
            ExternalPackagingPurchaseOrder.status == "confirmed",
            ExternalPackagingPurchaseCancellation.id.is_(None),
            Order.status.notin_(("cancelled", "dead", "closed", "archived")),
        )
    )
    if visible_customer_ids is not None:
        query = query.where(Order.customer_id.in_(visible_customer_ids))
    if not include_completed:
        received_by_item = _received_quantity_aggregate().subquery()
        has_any_item = (
            select(ExternalPackagingPurchaseItem.id)
            .where(
                ExternalPackagingPurchaseItem.purchase_order_id
                == ExternalPackagingPurchaseOrder.id
            )
            .exists()
        )
        has_pending_item = (
            select(ExternalPackagingPurchaseItem.id)
            .outerjoin(
                received_by_item,
                received_by_item.c.purchase_item_id
                == ExternalPackagingPurchaseItem.id,
            )
            .where(
                ExternalPackagingPurchaseItem.purchase_order_id
                == ExternalPackagingPurchaseOrder.id,
                ExternalPackagingPurchaseItem.purchase_quantity
                > _received_quantity_at_formal_precision(
                    received_by_item.c.received_quantity
                ),
            )
            .exists()
        )
        query = query.where(or_(~has_any_item, has_pending_item))
    purchases = list(db.scalars(query).unique().all())
    order_ids = {purchase.batch.sales_order_id for purchase in purchases}
    sales_orders = {
        row.id: row
        for row in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    }
    customer_ids = {row.customer_id for row in sales_orders.values()}
    customers = {
        row.id: row
        for row in db.scalars(select(Customer).where(Customer.id.in_(customer_ids))).all()
    }
    item_ids = {item.id for purchase in purchases for item in purchase.items}
    totals = _received_totals(db, item_ids)
    rows = []
    for purchase in purchases:
        sales_order = sales_orders.get(purchase.batch.sales_order_id)
        if sales_order is None:
            continue
        payload = _purchase_payload(
            purchase,
            sales_order=sales_order,
            customer=customers.get(sales_order.customer_id),
            totals=totals,
        )
        if not include_completed:
            payload["items"] = [
                item
                for item in payload["items"]
                if Decimal(item["remaining_quantity"]) > 0
            ]
        if include_completed or payload["status"] != "received":
            rows.append(payload)
    return {
        "purchase_orders": rows,
        "summary": {
            "purchase_order_count": len(rows),
            "pending_line_count": sum(
                1
                for row in rows
                for item in row["items"]
                if Decimal(item["remaining_quantity"]) > 0
            ),
        },
    }


def _load_receipt(db: Session, receipt_id: int) -> ExternalPackagingReceipt:
    receipt = db.scalar(
        select(ExternalPackagingReceipt)
        .options(selectinload(ExternalPackagingReceipt.items))
        .where(ExternalPackagingReceipt.id == receipt_id)
    )
    if receipt is None:
        raise ExternalPurchaseContractError("外购包装收料记录不存在", status_code=404)
    return receipt


def serialize_external_receipt(receipt: ExternalPackagingReceipt) -> dict[str, Any]:
    return {
        "id": receipt.id,
        "purchase_order_id": receipt.purchase_order_id,
        "receipt_number": receipt.receipt_number,
        "received_at": receipt.received_at.isoformat() if receipt.received_at else None,
        "items": [
            {
                "purchase_item_id": row.purchase_item_id,
                "received_quantity": _text(Decimal(row.received_quantity)),
                "purchase_unit": row.purchase_unit_snapshot,
            }
            for row in receipt.items
        ],
    }


def record_external_purchase_receipt(
    db: Session,
    *,
    purchase_order_id: int,
    idempotency_key: str,
    lines: list[dict[str, Any]],
    user: User,
    visible_customer_ids: set[int] | None,
) -> tuple[ExternalPackagingReceipt, bool]:
    fingerprint = _request_fingerprint(purchase_order_id, lines)
    existing = db.scalar(
        select(ExternalPackagingReceipt).where(
            ExternalPackagingReceipt.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if (
            existing.purchase_order_id != purchase_order_id
            or existing.request_fingerprint != fingerprint
        ):
            raise ExternalPurchaseContractError(
                "该收料请求编号已用于其他内容，请刷新后重试"
            )
        return _load_receipt(db, existing.id), False

    sales_order_id = db.scalar(
        select(ExternalPackagingPurchaseBatch.sales_order_id)
        .join(
            ExternalPackagingPurchaseOrder,
            ExternalPackagingPurchaseOrder.batch_id
            == ExternalPackagingPurchaseBatch.id,
        )
        .where(ExternalPackagingPurchaseOrder.id == purchase_order_id)
    )
    if sales_order_id is None:
        raise ExternalPurchaseContractError("外购包装采购单不存在", status_code=404)
    sales_order = claim_external_purchase_order(db, int(sales_order_id))
    if sales_order is None:
        raise ExternalPurchaseContractError("采购单关联订单不存在")
    if sales_order.status in {"cancelled", "dead", "closed", "archived"}:
        raise ExternalPurchaseContractError("关联订单已终止，不能继续收料")

    purchase = db.scalar(
        select(ExternalPackagingPurchaseOrder)
        .options(
            joinedload(ExternalPackagingPurchaseOrder.batch),
            selectinload(ExternalPackagingPurchaseOrder.items),
        )
        .where(ExternalPackagingPurchaseOrder.id == purchase_order_id)
        .with_for_update(of=ExternalPackagingPurchaseOrder)
    )
    if purchase is None:
        raise ExternalPurchaseContractError("外购包装采购单不存在", status_code=404)
    cancellation = db.scalar(
        select(ExternalPackagingPurchaseCancellation.id).where(
            ExternalPackagingPurchaseCancellation.purchase_order_id == purchase.id
        )
    )
    if purchase.status != "confirmed" or cancellation is not None:
        raise ExternalPurchaseContractError("外购包装采购单已作废，不能继续收料")
    if (
        visible_customer_ids is not None
        and sales_order.customer_id not in visible_customer_ids
    ):
        raise ExternalPurchaseContractError("无权操作该客户的外购包装收料", status_code=403)
    if not lines:
        raise ExternalPurchaseContractError("请至少填写一条实收数量", status_code=422)

    purchase_items = {row.id: row for row in purchase.items}
    submitted_ids = [int(row["purchase_item_id"]) for row in lines]
    if len(submitted_ids) != len(set(submitted_ids)):
        raise ExternalPurchaseContractError("同一采购明细不能重复提交", status_code=422)
    if any(item_id not in purchase_items for item_id in submitted_ids):
        raise ExternalPurchaseContractError("收料明细不属于当前采购单", status_code=422)
    totals = _received_totals(db, set(purchase_items))
    normalized: list[tuple[ExternalPackagingPurchaseItem, Decimal]] = []
    for line in lines:
        item = purchase_items[int(line["purchase_item_id"])]
        quantity = _decimal(line["received_quantity"])
        if item.purchase_unit in DISCRETE_PURCHASE_UNITS and quantity != quantity.to_integral_value():
            raise ExternalPurchaseContractError(
                f"“{item.product_name_snapshot}”单位为{item.purchase_unit}，实收必须是整数",
                status_code=422,
            )
        remaining = Decimal(item.purchase_quantity) - totals.get(item.id, Decimal("0"))
        if remaining <= 0:
            raise ExternalPurchaseContractError(
                f"“{item.product_name_snapshot}”已经收齐，请刷新"
            )
        if quantity > remaining:
            raise ExternalPurchaseContractError(
                f"“{item.product_name_snapshot}”本次最多可收 {_text(remaining)} {item.purchase_unit}"
            )
        normalized.append((item, quantity))

    ordinal = int(
        db.scalar(
            select(func.count(ExternalPackagingReceipt.id)).where(
                ExternalPackagingReceipt.purchase_order_id == purchase.id
            )
        )
        or 0
    ) + 1
    receipt = ExternalPackagingReceipt(
        purchase_order_id=purchase.id,
        receipt_number=f"ER-{purchase.purchase_number.removeprefix('EP-')}-{ordinal:02d}",
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        received_by=user.id,
    )
    db.add(receipt)
    db.flush()
    for item, quantity in normalized:
        db.add(
            ExternalPackagingReceiptItem(
                receipt_id=receipt.id,
                purchase_item_id=item.id,
                received_quantity=quantity,
                purchase_unit_snapshot=item.purchase_unit,
            )
        )
    db.flush()
    return _load_receipt(db, receipt.id), True
