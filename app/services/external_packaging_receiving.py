from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import json
from typing import Any

from sqlalchemy import func, or_, select, update
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
from app.models.order import Order, OrderItem
from app.models.stock_replenishment import (
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.user import User
from app.services.external_packaging_purchase import (
    ExternalPurchaseContractError,
    claim_external_purchase_order,
)
from app.services.order_external_packaging import DISCRETE_PURCHASE_UNITS
from app.services.order_status_policy import (
    ORDER_ITEM_ACTIVE_ORDER_STATUSES,
    order_item_forward_block_message,
    order_item_forward_block_reason,
)
from app.services.stock_replenishment import (
    StockReplenishmentError,
    receive_replenishment_item,
)


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
    from app.services.external_receipt_state import active_receipt_item
    statement = select(
        ExternalPackagingReceiptItem.purchase_item_id.label("purchase_item_id"),
        func.sum(ExternalPackagingReceiptItem.received_quantity).label(
            "received_quantity"
        ),
    ).where(active_receipt_item()).group_by(ExternalPackagingReceiptItem.purchase_item_id)
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
    sales_order: Order | None,
    customer: Customer | None,
    totals: dict[int, Decimal],
    replenishment_order: StockReplenishmentOrder | None = None,
    eligible_sales_order_item_ids: set[int] | None = None,
) -> dict[str, Any]:
    visible_items = [
        item
        for item in purchase.items
        if eligible_sales_order_item_ids is None
        or item.sales_order_item_id is None
        or int(item.sales_order_item_id) in eligible_sales_order_item_ids
    ]
    item_rows = []
    for item in visible_items:
        ordered = Decimal(item.purchase_quantity)
        received = totals.get(item.id, Decimal("0"))
        remaining = max(ordered - received, Decimal("0"))
        converted_finished_quantity: int | None = None
        loose_remainder_quantity: Decimal | None = None
        if (
            item.stock_replenishment_item_id is not None
            and item.order_quantity_basis_snapshot is not None
            and item.purchase_quantity_basis_snapshot is not None
        ):
            order_basis = Decimal(item.order_quantity_basis_snapshot)
            purchase_basis = Decimal(item.purchase_quantity_basis_snapshot)
            converted_finished_quantity = int(
                (
                    received * order_basis / purchase_basis
                ).to_integral_value(rounding=ROUND_FLOOR)
            )
            loose_remainder_quantity = received - (
                Decimal(converted_finished_quantity)
                * purchase_basis
                / order_basis
            )
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
                "converted_finished_quantity": converted_finished_quantity,
                "loose_remainder_quantity": (
                    _text(loose_remainder_quantity)
                    if loose_remainder_quantity is not None
                    else None
                ),
                "order_quantity_basis": (
                    _text(Decimal(item.order_quantity_basis_snapshot))
                    if item.order_quantity_basis_snapshot is not None
                    else None
                ),
                "purchase_quantity_basis": (
                    _text(Decimal(item.purchase_quantity_basis_snapshot))
                    if item.purchase_quantity_basis_snapshot is not None
                    else None
                ),
            }
        )
    return {
        "id": purchase.id,
        "purchase_number": purchase.purchase_number,
        "supplier_id": purchase.supplier_id,
        "supplier_name": purchase.supplier_name_snapshot,
        "source_type": (
            "sales_order" if sales_order is not None else "stock_replenishment"
        ),
        "sales_order_id": sales_order.id if sales_order is not None else None,
        "customer_po": sales_order.customer_po if sales_order is not None else None,
        "stock_replenishment_order_id": (
            replenishment_order.id if replenishment_order is not None else None
        ),
        "order_number": (
            sales_order.order_number
            if sales_order is not None
            else replenishment_order.order_number
            if replenishment_order is not None
            else None
        ),
        "customer_id": (
            sales_order.customer_id
            if sales_order is not None
            else replenishment_order.customer_id
            if replenishment_order is not None
            else None
        ),
        "customer_name": customer.name if customer is not None else "客户待确认",
        "status": _purchase_status(visible_items, totals),
        "items": item_rows,
    }


def build_external_receiving_overview(
    db: Session,
    *,
    visible_customer_ids: set[int] | None,
    include_completed: bool = False,
) -> dict[str, Any]:
    has_forward_eligible_item = (
        select(ExternalPackagingPurchaseItem.id)
        .join(
            OrderItem,
            OrderItem.id == ExternalPackagingPurchaseItem.sales_order_item_id,
        )
        .where(
            ExternalPackagingPurchaseItem.purchase_order_id
            == ExternalPackagingPurchaseOrder.id,
            OrderItem.is_force_closed.is_(False),
            OrderItem.delivered_quantity < OrderItem.quantity,
        )
        .exists()
    )
    has_stock_replenishment_item = (
        select(ExternalPackagingPurchaseItem.id)
        .where(
            ExternalPackagingPurchaseItem.purchase_order_id
            == ExternalPackagingPurchaseOrder.id,
            ExternalPackagingPurchaseItem.stock_replenishment_item_id.is_not(
                None
            ),
        )
        .exists()
    )
    query = (
        select(ExternalPackagingPurchaseOrder)
        .join(
            ExternalPackagingPurchaseBatch,
            ExternalPackagingPurchaseBatch.id
            == ExternalPackagingPurchaseOrder.batch_id,
        )
        .outerjoin(Order, Order.id == ExternalPackagingPurchaseBatch.sales_order_id)
        .outerjoin(
            StockReplenishmentOrder,
            StockReplenishmentOrder.id
            == ExternalPackagingPurchaseBatch.stock_replenishment_order_id,
        )
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
            or_(
                (
                    ExternalPackagingPurchaseBatch.sales_order_id.is_not(None)
                    & Order.status.in_(ORDER_ITEM_ACTIVE_ORDER_STATUSES)
                    & has_forward_eligible_item
                ),
                (
                    ExternalPackagingPurchaseBatch.stock_replenishment_order_id.is_not(
                        None
                    )
                    & StockReplenishmentOrder.status.in_(
                        ("confirmed", "partially_stocked", "stocked")
                    )
                    & has_stock_replenishment_item
                ),
            ),
        )
    )
    if visible_customer_ids is not None:
        query = query.where(
            or_(
                Order.customer_id.in_(visible_customer_ids),
                StockReplenishmentOrder.customer_id.in_(visible_customer_ids),
            )
        )
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
    order_ids = {
        int(purchase.batch.sales_order_id)
        for purchase in purchases
        if purchase.batch.sales_order_id is not None
    }
    sales_orders = (
        {
            row.id: row
            for row in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
        }
        if order_ids
        else {}
    )
    replenishment_ids = {
        int(purchase.batch.stock_replenishment_order_id)
        for purchase in purchases
        if purchase.batch.stock_replenishment_order_id is not None
    }
    replenishment_orders = (
        {
            int(row.id): row
            for row in db.scalars(
                select(StockReplenishmentOrder).where(
                    StockReplenishmentOrder.id.in_(replenishment_ids)
                )
            ).all()
        }
        if replenishment_ids
        else {}
    )
    customer_ids = {row.customer_id for row in sales_orders.values()} | {
        int(row.customer_id)
        for row in replenishment_orders.values()
        if row.customer_id is not None
    }
    customers = (
        {
            row.id: row
            for row in db.scalars(
                select(Customer).where(Customer.id.in_(customer_ids))
            ).all()
        }
        if customer_ids
        else {}
    )
    item_ids = {item.id for purchase in purchases for item in purchase.items}
    totals = _received_totals(db, item_ids)
    eligible_sales_order_item_ids: set[int] | None = None
    if not include_completed:
        sales_order_item_ids = {
            int(item.sales_order_item_id)
            for purchase in purchases
            for item in purchase.items
            if item.sales_order_item_id is not None
        }
        order_items = list(
            db.scalars(
                select(OrderItem).where(OrderItem.id.in_(sales_order_item_ids))
            ).all()
        )
        eligible_sales_order_item_ids = {
            int(item.id)
            for item in order_items
            if (
                (sales_order := sales_orders.get(int(item.order_id))) is not None
                and order_item_forward_block_reason(
                    order_status=sales_order.status,
                    ordered_quantity=item.quantity,
                    delivered_quantity=item.delivered_quantity,
                    is_force_closed=item.is_force_closed,
                )
                is None
            )
        }
    rows = []
    for purchase in purchases:
        sales_order = (
            sales_orders.get(int(purchase.batch.sales_order_id))
            if purchase.batch.sales_order_id is not None
            else None
        )
        replenishment_order = (
            replenishment_orders.get(
                int(purchase.batch.stock_replenishment_order_id)
            )
            if purchase.batch.stock_replenishment_order_id is not None
            else None
        )
        if sales_order is None and replenishment_order is None:
            continue
        customer_id = (
            int(sales_order.customer_id)
            if sales_order is not None
            else int(replenishment_order.customer_id)
        )
        payload = _purchase_payload(
            purchase,
            sales_order=sales_order,
            replenishment_order=replenishment_order,
            customer=customers.get(customer_id),
            totals=totals,
            eligible_sales_order_item_ids=eligible_sales_order_item_ids,
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
    def serialize_item(row: ExternalPackagingReceiptItem) -> dict[str, Any]:
        payload = {
            "purchase_item_id": row.purchase_item_id,
            "received_quantity": _text(Decimal(row.received_quantity)),
            "purchase_unit": row.purchase_unit_snapshot,
        }
        converted = int(row.converted_finished_quantity or 0)
        remainder = Decimal(row.loose_remainder_quantity_after or 0)
        if converted > 0 or remainder > 0:
            payload.update(
                {
                    "converted_finished_quantity": converted,
                    "loose_remainder_quantity_after": _text(remainder),
                }
            )
        return payload

    return {
        "id": receipt.id,
        "purchase_order_id": receipt.purchase_order_id,
        "receipt_number": receipt.receipt_number,
        "received_at": receipt.received_at.isoformat() if receipt.received_at else None,
        "items": [serialize_item(row) for row in receipt.items],
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
        from app.models.external_packaging_purchase import ExternalPackagingReceiptReversal
        if db.get(ExternalPackagingReceiptReversal, existing.id) is not None:
            raise ExternalPurchaseContractError('该实收已撤销，请使用新的收料请求')
        if (
            existing.purchase_order_id != purchase_order_id
            or existing.request_fingerprint != fingerprint
        ):
            raise ExternalPurchaseContractError(
                "该收料请求编号已用于其他内容，请刷新后重试"
            )
        return _load_receipt(db, existing.id), False

    source_batch = db.scalar(
        select(ExternalPackagingPurchaseBatch)
        .join(
            ExternalPackagingPurchaseOrder,
            ExternalPackagingPurchaseOrder.batch_id
            == ExternalPackagingPurchaseBatch.id,
        )
        .where(ExternalPackagingPurchaseOrder.id == purchase_order_id)
    )
    if source_batch is None:
        raise ExternalPurchaseContractError("外购包装采购单不存在", status_code=404)
    sales_order: Order | None = None
    replenishment_order: StockReplenishmentOrder | None = None
    if source_batch.sales_order_id is not None:
        sales_order = claim_external_purchase_order(
            db, int(source_batch.sales_order_id)
        )
        if sales_order is None:
            raise ExternalPurchaseContractError("采购单关联订单不存在")
        if sales_order.status not in ORDER_ITEM_ACTIVE_ORDER_STATUSES:
            raise ExternalPurchaseContractError("关联订单已终止，不能继续收料")
        source_customer_id = int(sales_order.customer_id)
    elif source_batch.stock_replenishment_order_id is not None:
        replenishment_order_id = int(source_batch.stock_replenishment_order_id)
        claimed = db.execute(
            update(StockReplenishmentOrder)
            .where(StockReplenishmentOrder.id == replenishment_order_id)
            .values(status=StockReplenishmentOrder.status)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise ExternalPurchaseContractError("采购单关联补库来源不存在")
        db.flush()
        replenishment_order = db.get(
            StockReplenishmentOrder, replenishment_order_id
        )
        if replenishment_order is None:
            raise ExternalPurchaseContractError("采购单关联补库来源不存在")
        if replenishment_order.status == "voided":
            raise ExternalPurchaseContractError("关联补库来源已作废，不能继续收料")
        if replenishment_order.customer_id is None:
            raise ExternalPurchaseContractError("关联补库来源缺少客户")
        source_customer_id = int(replenishment_order.customer_id)
    else:
        raise ExternalPurchaseContractError("采购单缺少有效业务来源")

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
        and source_customer_id not in visible_customer_ids
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
    target_order_items: dict[int, OrderItem] = {}
    stock_items: dict[int, StockReplenishmentOrderItem] = {}
    if sales_order is not None:
        target_order_item_ids = {
            int(purchase_items[item_id].sales_order_item_id)
            for item_id in submitted_ids
        }
        target_order_items = {
            int(row.id): row
            for row in db.scalars(
                select(OrderItem).where(
                    OrderItem.id.in_(target_order_item_ids),
                    OrderItem.order_id == sales_order.id,
                )
            ).all()
        }
        if set(target_order_items) != target_order_item_ids:
            raise ExternalPurchaseContractError("外购包装收料来源不完整")
    else:
        stock_item_ids = {
            int(purchase_items[item_id].stock_replenishment_item_id)
            for item_id in submitted_ids
            if purchase_items[item_id].stock_replenishment_item_id is not None
        }
        stock_items = {
            int(row.id): row
            for row in db.scalars(
                select(StockReplenishmentOrderItem).where(
                    StockReplenishmentOrderItem.id.in_(stock_item_ids),
                    StockReplenishmentOrderItem.replenishment_order_id
                    == replenishment_order.id,
                )
            ).all()
        }
        if len(stock_item_ids) != len(submitted_ids) or set(stock_items) != stock_item_ids:
            raise ExternalPurchaseContractError("外购备库收料来源不完整")
    totals = _received_totals(db, set(purchase_items))
    normalized: list[tuple[ExternalPackagingPurchaseItem, Decimal]] = []
    for line in lines:
        item = purchase_items[int(line["purchase_item_id"])]
        if sales_order is not None:
            order_item = target_order_items[int(item.sales_order_item_id)]
            block = order_item_forward_block_reason(
                order_status=sales_order.status,
                ordered_quantity=order_item.quantity,
                delivered_quantity=order_item.delivered_quantity,
                is_force_closed=order_item.is_force_closed,
            )
            if block is not None:
                raise ExternalPurchaseContractError(
                    order_item_forward_block_message(
                        block,
                        action="收取外购包材",
                        order_status=sales_order.status,
                    )
                )
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
        if quantity > remaining and replenishment_order is None:
            raise ExternalPurchaseContractError(
                f"“{item.product_name_snapshot}”本次最多可收 {_text(remaining)} {item.purchase_unit}"
            )
        normalized.append((item, quantity))

    from app.services.multilevel_bom_external_receipts import graph_receipt_conversions
    from app.services.multilevel_bom_plan import BomPlanError
    try:
        graph_conversions = graph_receipt_conversions(
            db, normalized, totals, customer_id=source_customer_id
        )
    except BomPlanError as error:
        raise ExternalPurchaseContractError(str(error), status_code=409) from error

    from app.services.direct_external_finished import conversions as direct_conversions
    direct_outputs = direct_conversions(db, normalized, customer_id=source_customer_id)

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
        converted_quantity, remainder = graph_conversions.get(item.id, direct_outputs.get(item.id, (0, Decimal("0"))))
        stock_item: StockReplenishmentOrderItem | None = None
        if replenishment_order is not None:
            stock_item = stock_items[int(item.stock_replenishment_item_id)]
            order_basis = _decimal(item.order_quantity_basis_snapshot)
            purchase_basis = _decimal(item.purchase_quantity_basis_snapshot)
            cumulative_before = totals.get(item.id, Decimal("0"))
            cumulative_received = cumulative_before + quantity
            completed_before = int(
                (
                    cumulative_before * order_basis / purchase_basis
                ).to_integral_value(rounding=ROUND_FLOOR)
            )
            completed_after = int(
                (
                    cumulative_received * order_basis / purchase_basis
                ).to_integral_value(rounding=ROUND_FLOOR)
            )
            converted_quantity = completed_after - completed_before
            if converted_quantity < 0:
                raise ExternalPurchaseContractError(
                    "外购备库累计换算数量异常，已停止收料"
                )
            remainder = cumulative_received - (
                Decimal(completed_after) * purchase_basis / order_basis
            )
            if remainder < 0:
                raise ExternalPurchaseContractError(
                    "外购备库散件余量计算异常，已停止收料"
                )
        receipt_item = ExternalPackagingReceiptItem(
            receipt_id=receipt.id,
            purchase_item_id=item.id,
            received_quantity=quantity,
            purchase_unit_snapshot=item.purchase_unit,
            converted_finished_quantity=converted_quantity,
            loose_remainder_quantity_after=remainder.quantize(SIX_PLACES),
        )
        db.add(receipt_item)
        db.flush()
        if item.id in direct_outputs:
            from app.services.direct_external_finished import post as post_direct
            from app.services.production_workflow import ProductionWorkflowError
            from app.services.warehouse_inventory import WarehouseInventoryError
            try:
                post_direct(db, purchase=item, receipt=receipt_item,
                    customer_id=source_customer_id, operator_id=user.id)
            except (ProductionWorkflowError, WarehouseInventoryError) as error:
                raise ExternalPurchaseContractError(str(error), status_code=409) from error
        if item.id in graph_conversions:
            from app.services.multilevel_bom_external_receipts import post_graph_receipt_inventory
            from app.services.production_workflow import ProductionWorkflowError
            from app.services.warehouse_inventory import WarehouseInventoryError
            try:
                post_graph_receipt_inventory(db, purchase_item=item, receipt_item=receipt_item,
                    customer_id=source_customer_id, operator_id=user.id)
            except (BomPlanError, ProductionWorkflowError, WarehouseInventoryError) as error:
                raise ExternalPurchaseContractError(str(error), status_code=409) from error
        if replenishment_order is not None and stock_item is not None:
            if converted_quantity > 0:
                planned_quantity = min(
                    converted_quantity,
                    max(
                        int(stock_item.quantity or 0)
                        - int(stock_item.stocked_quantity or 0),
                        0,
                    ),
                )
                try:
                    receive_replenishment_item(
                        db,
                        order=replenishment_order,
                        item=stock_item,
                        quantity=planned_quantity,
                        operator_id=user.id,
                        receipt_item_id=receipt_item.id,
                        source_ref_type="external_packaging_receipt_item",
                        actual_inventory_quantity=converted_quantity,
                    )
                except StockReplenishmentError as error:
                    raise ExternalPurchaseContractError(
                        str(error), status_code=error.status_code
                    ) from error
    graph_output_orders = {item.sales_order_item_id for item, _ in normalized
        if item.id in graph_conversions and graph_conversions[item.id][0] > 0}
    graph_receipt_orders = {item.sales_order_item_id for item, _ in normalized if item.id in graph_conversions}
    if graph_receipt_orders:
        from app.services.multilevel_bom_receipts import assemble_graph_order_receipt, refresh_graph_main_task, graph_material_receipts_closed
        from app.services.multilevel_bom_external_receipts import reserve_external_picking
        from app.services.multilevel_bom_orders import read_compiled_order_bom
        from app.core.time_contract import utc_now_naive
        from app.services.bom_subkits import SubkitError
        from app.services.production_workflow import ProductionWorkflowError
        from app.services.warehouse_inventory import WarehouseInventoryError
        try:
            for oid in sorted(graph_receipt_orders):
                item = target_order_items[oid]
                if oid in graph_output_orders:
                    assemble_graph_order_receipt(db, compiled=read_compiled_order_bom(db, oid),
                        order_item_id=oid, operation_key=f'bom-external-receipt:{receipt.id}:{oid}', operator_id=user.id)
                    reserve_external_picking(db, receipt_id=receipt.id, item=item, operator_id=user.id)
                if graph_material_receipts_closed(db, item):
                    if item.material_status != 'received':
                        item.material_received_at = utc_now_naive()
                        item.material_received_by = user.id
                    item.material_status, item.requisition_status = 'received', '已入库'
                else:
                    item.material_status = 'pending'
                    if item.requisition_status == '已入库':
                        item.requisition_status = '已报料'
                    item.material_received_at = item.material_received_by = None
                refresh_graph_main_task(db, item, create_if_missing=oid in graph_output_orders)
        except (BomPlanError, SubkitError, ProductionWorkflowError, WarehouseInventoryError) as error:
            raise ExternalPurchaseContractError(str(error), status_code=409) from error
    db.flush()
    return _load_receipt(db, receipt.id), True
