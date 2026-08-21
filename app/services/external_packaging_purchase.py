from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
import hashlib
import json
import re
from typing import Any, Iterable

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.time_contract import beijing_today, utc_naive_to_api
from app.models.company_config import CompanyConfig
from app.models.customer import Customer
from app.models.external_packaging_price import ExternalPackagingPriceVersion
from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseBatch,
    ExternalPackagingPurchaseCancellation,
    ExternalPackagingPurchaseItem,
    ExternalPackagingPurchaseOrder,
    ExternalPackagingReceiptItem,
)
from app.models.order import Order, OrderItem
from app.models.order_external_packaging import (
    SalesOrderItemExternalComponent,
    SalesOrderItemExternalComponentCandidate,
)
from app.models.supplier import ExternalPackagingProduct, Supplier
from app.models.user import User
from app.services.order_external_packaging import DISCRETE_PURCHASE_UNITS
from app.services.corner_guard_pricing import (
    CATEGORY_CODE as CORNER_GUARD_CATEGORY_CODE,
    CornerGuardPricingError,
    calculate_corner_guard_cost,
    current_corner_guard_meter_price,
)


MONEY = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")
PACKAGING_PRICE_MAINTENANCE_PATH = (
    "维护路径：常用箱与材质 → 供应商 → 包材供应商 → "
    "包材产品与报价 → 维护正式报价"
)
CORNER_GUARD_MISSING_PRICE_MESSAGE = (
    "当前没有有效价格：纸护角必须维护每米正式报价；"
    f"{PACKAGING_PRICE_MAINTENANCE_PATH}"
)
PACKAGING_MISSING_PRICE_MESSAGE = (
    "当前没有匹配冻结规格和采购单位的有效价格；"
    f"{PACKAGING_PRICE_MAINTENANCE_PATH}"
)


class ExternalPurchaseContractError(ValueError):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(Decimal(value), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _order_components(
    db: Session, order_id: int, *, lock_order: bool = False
) -> tuple[Order | None, list[SalesOrderItemExternalComponent]]:
    order_query = select(Order).where(Order.id == order_id)
    if lock_order:
        order_query = order_query.with_for_update(of=Order)
    order = db.scalar(order_query)
    if order is None:
        return None, []
    rows = list(
        db.scalars(
            select(SalesOrderItemExternalComponent)
            .join(
                OrderItem,
                OrderItem.id
                == SalesOrderItemExternalComponent.sales_order_item_id,
            )
            .options(
                selectinload(SalesOrderItemExternalComponent.candidates)
            )
            .where(OrderItem.order_id == order.id)
            .order_by(
                OrderItem.item_sequence,
                OrderItem.id,
                SalesOrderItemExternalComponent.display_order,
            )
        ).all()
    )
    return order, rows


def claim_external_purchase_order(db: Session, order_id: int) -> Order | None:
    """Serialize purchase, receipt and termination on SQLite and PostgreSQL."""

    claimed = db.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(status=Order.status)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None
    db.flush()
    order = db.get(Order, order_id)
    if order is not None:
        db.refresh(order)
    return order


def _current_price(
    db: Session,
    candidate: SalesOrderItemExternalComponentCandidate,
    *,
    component: SalesOrderItemExternalComponent,
    as_of: date,
) -> ExternalPackagingPriceVersion | None:
    if component.category_code == CORNER_GUARD_CATEGORY_CODE:
        return current_corner_guard_meter_price(
            db,
            external_product_id=candidate.external_product_id_snapshot,
            product_version=candidate.external_product_version_snapshot,
            as_of=as_of,
        )
    return db.scalar(
        select(ExternalPackagingPriceVersion)
        .where(
            ExternalPackagingPriceVersion.external_product_id
            == candidate.external_product_id_snapshot,
            ExternalPackagingPriceVersion.product_version
            == candidate.external_product_version_snapshot,
            ExternalPackagingPriceVersion.quote_unit
            == candidate.purchase_unit_snapshot,
            ExternalPackagingPriceVersion.effective_from <= as_of,
            or_(
                ExternalPackagingPriceVersion.effective_to.is_(None),
                ExternalPackagingPriceVersion.effective_to >= as_of,
            ),
        )
        .order_by(
            ExternalPackagingPriceVersion.effective_from.desc(),
            ExternalPackagingPriceVersion.version_number.desc(),
        )
        .limit(1)
    )


def _product_availability(
    db: Session,
    candidate: SalesOrderItemExternalComponentCandidate,
) -> tuple[ExternalPackagingProduct | None, str | None]:
    product = db.scalar(
        select(ExternalPackagingProduct)
        .options(joinedload(ExternalPackagingProduct.supplier))
        .where(
            ExternalPackagingProduct.id
            == candidate.external_product_id_snapshot
        )
    )
    if product is None:
        return None, "冻结的外购产品已不存在"
    if product.supplier_id != candidate.supplier_id_snapshot:
        return product, "冻结候选与当前供应商归属不一致"
    if product.purchase_unit != candidate.purchase_unit_snapshot:
        return product, "冻结候选与当前采购单位不一致"
    if not product.is_active or not product.supplier.is_active:
        return product, "供应商或外购产品已停用"
    return product, None


def _tier_rows(price: ExternalPackagingPriceVersion) -> list[dict[str, str]]:
    try:
        raw = json.loads(price.tier_prices_json or "[]")
    except json.JSONDecodeError:
        return []
    rows: list[dict[str, str]] = []
    for row in raw if isinstance(raw, list) else []:
        try:
            minimum = Decimal(str(row.get("min_quantity")))
            unit_price = Decimal(str(row.get("unit_price")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if minimum > 0 and unit_price > 0:
            rows.append(
                {
                    "min_quantity": _decimal_text(minimum) or "0",
                    "unit_price": _decimal_text(unit_price) or "0",
                }
            )
    return sorted(rows, key=lambda row: Decimal(row["min_quantity"]))


def _effective_unit_price(
    price: ExternalPackagingPriceVersion, quantity: Decimal
) -> tuple[Decimal, list[dict[str, str]]]:
    tiers = _tier_rows(price)
    effective = Decimal(price.unit_price)
    for row in tiers:
        if quantity >= Decimal(row["min_quantity"]):
            effective = Decimal(row["unit_price"])
        else:
            break
    return effective, tiers


def _quantity_error(
    price: ExternalPackagingPriceVersion,
    quantity: Decimal,
    *,
    unit: str,
) -> str | None:
    if quantity <= 0:
        return "采购数量必须大于 0"
    if quantity.quantize(SIX_PLACES) != quantity:
        return "采购数量最多保留 6 位小数"
    if unit in DISCRETE_PURCHASE_UNITS and quantity != quantity.to_integral_value():
        return f"采购单位为“{unit}”，数量必须是整数"
    if price.moq_quantity is not None and quantity < Decimal(price.moq_quantity):
        return (
            f"采购数量不能低于 MOQ {_decimal_text(Decimal(price.moq_quantity))} "
            f"{price.moq_unit or unit}"
        )
    if price.packaging_multiple is not None:
        multiple = Decimal(price.packaging_multiple)
        if quantity % multiple != 0:
            return f"采购数量必须是包装倍数 {_decimal_text(multiple)} {unit} 的整数倍"
    return None


def _purchase_quantity_error(quantity: Decimal, *, unit: str) -> str | None:
    if quantity <= 0:
        return "采购数量必须大于 0"
    if quantity.quantize(SIX_PLACES) != quantity:
        return "采购数量最多保留 6 位小数"
    if unit in DISCRETE_PURCHASE_UNITS and quantity != quantity.to_integral_value():
        return f"采购单位为“{unit}”，数量必须是整数"
    return None


def _resolved_purchase_pricing(
    component: SalesOrderItemExternalComponent,
    price: ExternalPackagingPriceVersion,
    *,
    purchase_quantity: Decimal,
) -> dict[str, Any]:
    if component.category_code != CORNER_GUARD_CATEGORY_CODE:
        effective, tiers = _effective_unit_price(price, purchase_quantity)
        return {
            "purchase_unit_price": effective,
            "pricing_quantity": purchase_quantity,
            "tier_prices": tiers,
            "conversion": None,
            "tax_amount_per_purchase_unit": price.tax_amount_per_unit,
        }
    try:
        specification = json.loads(component.specification_json or "{}")
        conversion = calculate_corner_guard_cost(
            customer_specification=specification,
            root_quantity=purchase_quantity,
            price=price,
        )
    except (TypeError, json.JSONDecodeError, CornerGuardPricingError) as error:
        raise ExternalPurchaseContractError(
            f"组件“{component.purpose}”客户定长规格或每米报价无效：{error}"
        ) from error
    length_m = Decimal(conversion["length_m_per_root"])
    tax_per_purchase = (
        Decimal(price.tax_amount_per_unit) * length_m
        if price.tax_amount_per_unit is not None
        else None
    )
    return {
        "purchase_unit_price": Decimal(conversion["unit_cost_per_root"]),
        "pricing_quantity": Decimal(conversion["pricing_quantity_m"]),
        "tier_prices": _tier_rows(price),
        "conversion": conversion,
        "tax_amount_per_purchase_unit": tax_per_purchase,
    }


def _price_snapshot(
    price: ExternalPackagingPriceVersion,
    *,
    quantity: Decimal,
) -> dict[str, Any]:
    unit_price, tiers = _effective_unit_price(price, quantity)
    return {
        "id": price.id,
        "version_number": price.version_number,
        "product_version": price.product_version,
        "quote_unit": price.quote_unit,
        "unit_price": _decimal_text(unit_price),
        "base_unit_price": _decimal_text(Decimal(price.unit_price)),
        "currency": price.currency,
        "tax_mode": price.tax_mode,
        "tax_rate": _decimal_text(Decimal(price.tax_rate)),
        "tax_amount_per_unit": (
            _decimal_text(Decimal(price.tax_amount_per_unit))
            if price.tax_amount_per_unit is not None
            else None
        ),
        "effective_from": price.effective_from.isoformat(),
        "effective_to": (
            price.effective_to.isoformat() if price.effective_to else None
        ),
        "moq_quantity": (
            _decimal_text(Decimal(price.moq_quantity))
            if price.moq_quantity is not None
            else None
        ),
        "moq_unit": price.moq_unit,
        "packaging_multiple": (
            _decimal_text(Decimal(price.packaging_multiple))
            if price.packaging_multiple is not None
            else None
        ),
        "tier_prices": tiers,
        "shipping_fee_mode": price.shipping_fee_mode,
        "shipping_fee": (
            _decimal_text(Decimal(price.shipping_fee))
            if price.shipping_fee is not None
            else None
        ),
        "sample_fee": (
            _decimal_text(Decimal(price.sample_fee))
            if price.sample_fee is not None
            else None
        ),
        "plate_fee": (
            _decimal_text(Decimal(price.plate_fee))
            if price.plate_fee is not None
            else None
        ),
        "die_fee": (
            _decimal_text(Decimal(price.die_fee))
            if price.die_fee is not None
            else None
        ),
        "evidence_reference": price.evidence_reference,
    }


def _candidate_preview(
    db: Session,
    candidate: SalesOrderItemExternalComponentCandidate,
    *,
    component: SalesOrderItemExternalComponent,
    quantity: Decimal,
    as_of: date,
) -> dict[str, Any]:
    _, blocked_reason = _product_availability(db, candidate)
    price = None if blocked_reason else _current_price(
        db, candidate, component=component, as_of=as_of
    )
    if price is None and blocked_reason is None:
        blocked_reason = (
            CORNER_GUARD_MISSING_PRICE_MESSAGE
            if component.category_code == CORNER_GUARD_CATEGORY_CODE
            else PACKAGING_MISSING_PRICE_MESSAGE
        )
    pricing = None
    quantity_error = _purchase_quantity_error(
        quantity, unit=candidate.purchase_unit_snapshot
    )
    if price is not None and quantity_error is None:
        try:
            pricing = _resolved_purchase_pricing(
                component, price, purchase_quantity=quantity
            )
            quantity_error = _quantity_error(
                price,
                Decimal(pricing["pricing_quantity"]),
                unit=price.quote_unit,
            )
        except ExternalPurchaseContractError as error:
            blocked_reason = error.message
    price_payload = (
        _price_snapshot(
            price,
            quantity=(
                Decimal(pricing["pricing_quantity"])
                if pricing is not None
                else quantity
            ),
        )
        if price
        else None
    )
    if price_payload is not None and pricing is not None:
        price_payload["purchase_unit_price"] = _decimal_text(
            Decimal(pricing["purchase_unit_price"])
        )
        price_payload["purchase_unit"] = candidate.purchase_unit_snapshot
        if pricing["conversion"] is not None:
            price_payload["length_m_per_root"] = _decimal_text(
                Decimal(pricing["conversion"]["length_m_per_root"])
            )
            price_payload["pricing_quantity_m"] = _decimal_text(
                Decimal(pricing["conversion"]["pricing_quantity_m"])
            )
    return {
        "id": candidate.id,
        "external_product_id": candidate.external_product_id_snapshot,
        "external_product_version": candidate.external_product_version_snapshot,
        "is_default": candidate.is_default,
        "supplier_id": candidate.supplier_id_snapshot,
        "supplier_name": candidate.supplier_name_snapshot,
        "supplier_product_code": candidate.supplier_product_code_snapshot,
        "product_name": candidate.product_name_snapshot,
        "purchase_unit": candidate.purchase_unit_snapshot,
        "price": price_payload,
        "blocked_reason": blocked_reason,
        "suggested_quantity_warning": quantity_error,
    }


def _received_totals_by_purchase_item_ids(
    db: Session,
    purchase_item_ids: set[int],
) -> dict[int, Decimal]:
    if not purchase_item_ids:
        return {}
    return {
        int(item_id): Decimal(quantity or 0)
        for item_id, quantity in db.execute(
            select(
                ExternalPackagingReceiptItem.purchase_item_id,
                func.sum(ExternalPackagingReceiptItem.received_quantity),
            )
            .where(
                ExternalPackagingReceiptItem.purchase_item_id.in_(
                    purchase_item_ids
                )
            )
            .group_by(ExternalPackagingReceiptItem.purchase_item_id)
        ).all()
    }


def _external_purchase_summary_response(
    batch: ExternalPackagingPurchaseBatch,
    received_totals: dict[int, Decimal],
    cancelled_purchase_ids: set[int],
) -> dict[str, Any]:
    def receipt_status(purchase: ExternalPackagingPurchaseOrder) -> str:
        if all(
            received_totals.get(item.id, Decimal("0"))
            >= Decimal(item.purchase_quantity)
            for item in purchase.items
        ):
            return "received"
        if any(
            received_totals.get(item.id, Decimal("0")) > 0
            for item in purchase.items
        ):
            return "partially_received"
        return "pending_receipt"

    active_purchase_orders = [
        row
        for row in batch.purchase_orders
        if row.status == "confirmed" and int(row.id) not in cancelled_purchase_ids
    ]
    purchase_statuses = {
        row.id: receipt_status(row) for row in active_purchase_orders
    }
    overall_receipt_status = (
        "received"
        if purchase_statuses
        and all(value == "received" for value in purchase_statuses.values())
        else (
            "partially_received"
            if any(value != "pending_receipt" for value in purchase_statuses.values())
            else "pending_receipt"
        )
    )
    return {
        "status": "confirmed" if active_purchase_orders else "cancelled",
        "receipt_status": overall_receipt_status,
        "batch_id": batch.id,
        "purchase_numbers": [
            row.purchase_number for row in active_purchase_orders
        ],
        "purchase_orders": [
            {
                "id": row.id,
                "purchase_number": row.purchase_number,
                "receipt_status": purchase_statuses[row.id],
            }
            for row in active_purchase_orders
        ],
        "confirmed_at": (
            utc_naive_to_api(batch.confirmed_at) if batch.confirmed_at else None
        ),
    }


def _purchase_history_rows(
    db: Session, order_id: int
) -> list[dict[str, Any]]:
    batches = list(
        db.scalars(
            select(ExternalPackagingPurchaseBatch)
            .options(
                selectinload(
                    ExternalPackagingPurchaseBatch.purchase_orders
                ).selectinload(ExternalPackagingPurchaseOrder.items)
            )
            .where(ExternalPackagingPurchaseBatch.sales_order_id == order_id)
            .order_by(ExternalPackagingPurchaseBatch.id.desc())
        ).all()
    )
    purchase_ids = {
        int(purchase.id) for batch in batches for purchase in batch.purchase_orders
    }
    cancellations = {
        int(row.purchase_order_id): row
        for row in db.scalars(
            select(ExternalPackagingPurchaseCancellation).where(
                ExternalPackagingPurchaseCancellation.purchase_order_id.in_(
                    purchase_ids
                )
            )
        ).all()
    } if purchase_ids else {}
    item_ids = {
        int(item.id)
        for batch in batches
        for purchase in batch.purchase_orders
        for item in purchase.items
    }
    received = _received_totals_by_purchase_item_ids(db, item_ids)
    history: list[dict[str, Any]] = []
    for batch in batches:
        serialized = _serialize_batch(batch)
        for purchase_payload, purchase in zip(
            serialized["purchase_orders"], batch.purchase_orders
        ):
            cancellation = cancellations.get(int(purchase.id))
            purchase_payload["lifecycle_status"] = (
                "cancelled" if cancellation is not None else "active"
            )
            purchase_payload["received_quantity"] = _decimal_text(
                sum(
                    (received.get(int(item.id), Decimal("0")) for item in purchase.items),
                    Decimal("0"),
                )
            )
            purchase_payload["cancellation"] = (
                {
                    "source": cancellation.source,
                    "reason": cancellation.reason,
                    "cancelled_at": utc_naive_to_api(cancellation.cancelled_at),
                }
                if cancellation is not None
                else None
            )
        history.append(serialized)
    return history


def list_external_purchase_history_rows(
    db: Session,
    *,
    visible_customer_ids: set[int] | None = None,
    page: int = 1,
    page_size: int = 25,
    keyword: str | None = None,
) -> dict[str, Any]:
    """List confirmed and cancelled external purchases for the visible workbench."""

    safe_page = max(1, int(page))
    safe_page_size = min(100, max(1, int(page_size)))
    query = (
        select(ExternalPackagingPurchaseBatch)
        .join(Order, Order.id == ExternalPackagingPurchaseBatch.sales_order_id)
        .join(Customer, Customer.id == Order.customer_id)
    )
    count_query = (
        select(func.count(ExternalPackagingPurchaseBatch.id))
        .join(Order, Order.id == ExternalPackagingPurchaseBatch.sales_order_id)
        .join(Customer, Customer.id == Order.customer_id)
    )
    predicates = []
    if visible_customer_ids is not None:
        if not visible_customer_ids:
            return {
                "items": [],
                "total": 0,
                "page": safe_page,
                "page_size": safe_page_size,
            }
        predicates.append(Order.customer_id.in_(visible_customer_ids))
    normalized_keyword = str(keyword or "").strip()
    if normalized_keyword:
        pattern = f"%{normalized_keyword}%"
        matching_purchase_exists = (
            select(ExternalPackagingPurchaseOrder.id)
            .where(
                ExternalPackagingPurchaseOrder.batch_id
                == ExternalPackagingPurchaseBatch.id,
                or_(
                    ExternalPackagingPurchaseOrder.purchase_number.ilike(pattern),
                    ExternalPackagingPurchaseOrder.supplier_name_snapshot.ilike(pattern),
                ),
            )
            .exists()
        )
        predicates.append(
            or_(
                Order.order_number.ilike(pattern),
                Order.customer_po.ilike(pattern),
                Customer.name.ilike(pattern),
                Customer.chinese_short_name.ilike(pattern),
                matching_purchase_exists,
            )
        )
    if predicates:
        query = query.where(*predicates)
        count_query = count_query.where(*predicates)

    total = int(db.scalar(count_query) or 0)
    batches = list(
        db.scalars(
            query.options(
                selectinload(
                    ExternalPackagingPurchaseBatch.purchase_orders
                ).selectinload(ExternalPackagingPurchaseOrder.items)
            )
            .order_by(ExternalPackagingPurchaseBatch.id.desc())
            .offset((safe_page - 1) * safe_page_size)
            .limit(safe_page_size)
        ).all()
    )
    if not batches:
        return {
            "items": [],
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
        }

    order_ids = {int(batch.sales_order_id) for batch in batches}
    orders = {
        int(order.id): order
        for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    }
    customer_ids = {int(order.customer_id) for order in orders.values()}
    customers = {
        int(customer.id): customer
        for customer in db.scalars(
            select(Customer).where(Customer.id.in_(customer_ids))
        ).all()
    }
    latest_batch_ids = {
        int(order_id): int(batch_id)
        for order_id, batch_id in db.execute(
            select(
                ExternalPackagingPurchaseBatch.sales_order_id,
                func.max(ExternalPackagingPurchaseBatch.id),
            )
            .where(ExternalPackagingPurchaseBatch.sales_order_id.in_(order_ids))
            .group_by(ExternalPackagingPurchaseBatch.sales_order_id)
        ).all()
    }
    purchase_ids = {
        int(purchase.id)
        for batch in batches
        for purchase in batch.purchase_orders
    }
    cancellations = (
        {
            int(row.purchase_order_id): row
            for row in db.scalars(
                select(ExternalPackagingPurchaseCancellation).where(
                    ExternalPackagingPurchaseCancellation.purchase_order_id.in_(
                        purchase_ids
                    )
                )
            ).all()
        }
        if purchase_ids
        else {}
    )
    purchase_item_ids = {
        int(item.id)
        for batch in batches
        for purchase in batch.purchase_orders
        for item in purchase.items
    }
    received_totals = _received_totals_by_purchase_item_ids(db, purchase_item_ids)

    rows: list[dict[str, Any]] = []
    for batch in batches:
        order = orders[int(batch.sales_order_id)]
        customer = customers[int(order.customer_id)]
        serialized = _serialize_batch(batch)
        active_purchase_seen = False
        active_purchase_received = False
        active_count = 0
        cancelled_count = 0
        for purchase_payload, purchase in zip(
            serialized["purchase_orders"], batch.purchase_orders
        ):
            cancellation = cancellations.get(int(purchase.id))
            lifecycle_status = "cancelled" if cancellation is not None else "active"
            received_quantity = sum(
                (
                    received_totals.get(int(item.id), Decimal("0"))
                    for item in purchase.items
                ),
                Decimal("0"),
            )
            ordered_quantity = sum(
                (Decimal(item.purchase_quantity) for item in purchase.items),
                Decimal("0"),
            )
            if cancellation is None:
                active_count += 1
                active_purchase_seen = True
                active_purchase_received = (
                    active_purchase_received or received_quantity > 0
                )
            else:
                cancelled_count += 1
            purchase_payload.update(
                {
                    "lifecycle_status": lifecycle_status,
                    "ordered_quantity": _decimal_text(ordered_quantity),
                    "received_quantity": _decimal_text(received_quantity),
                    "receipt_status": (
                        "cancelled"
                        if cancellation is not None
                        else (
                            "received"
                            if ordered_quantity > 0 and received_quantity >= ordered_quantity
                            else (
                                "partially_received"
                                if received_quantity > 0
                                else "pending_receipt"
                            )
                        )
                    ),
                    "cancellation": (
                        {
                            "source": cancellation.source,
                            "reason": cancellation.reason,
                            "cancelled_at": utc_naive_to_api(
                                cancellation.cancelled_at
                            ),
                        }
                        if cancellation is not None
                        else None
                    ),
                }
            )
        serialized.update(
            {
                "order_number": order.order_number,
                "customer_order_number": order.customer_po,
                "customer_id": customer.id,
                "customer_name": customer.name,
                "customer_short_name": customer.chinese_short_name,
                "lifecycle_status": (
                    "cancelled"
                    if active_count == 0 and cancelled_count > 0
                    else ("partially_cancelled" if cancelled_count > 0 else "active")
                ),
                "can_cancel": (
                    latest_batch_ids.get(int(order.id)) == int(batch.id)
                    and active_purchase_seen
                    and not active_purchase_received
                ),
            }
        )
        rows.append(serialized)
    return {
        "items": rows,
        "total": total,
        "page": safe_page,
        "page_size": safe_page_size,
    }


def get_external_purchase_summary(db: Session, order_id: int) -> dict[str, Any]:
    order_status = db.scalar(select(Order.status).where(Order.id == order_id))
    batch = db.scalar(
        select(ExternalPackagingPurchaseBatch)
        .options(
            selectinload(ExternalPackagingPurchaseBatch.purchase_orders).selectinload(
                ExternalPackagingPurchaseOrder.items
            )
        )
        .where(ExternalPackagingPurchaseBatch.sales_order_id == order_id)
        .order_by(ExternalPackagingPurchaseBatch.id.desc())
        .limit(1)
    )
    if batch is None:
        return {"status": "pending", "purchase_numbers": []}
    cancellation_rows = list(
        db.scalars(
            select(ExternalPackagingPurchaseCancellation)
            .join(
                ExternalPackagingPurchaseOrder,
                ExternalPackagingPurchaseOrder.id
                == ExternalPackagingPurchaseCancellation.purchase_order_id,
            )
            .where(ExternalPackagingPurchaseOrder.batch_id == batch.id)
        ).all()
    )
    cancelled_purchase_ids = {int(row.purchase_order_id) for row in cancellation_rows}
    if order_status in {"cancelled", "dead"}:
        return {"status": "cancelled", "purchase_numbers": []}
    purchase_item_ids = {
        item.id for purchase in batch.purchase_orders for item in purchase.items
    }
    response = _external_purchase_summary_response(
        batch,
        _received_totals_by_purchase_item_ids(db, purchase_item_ids),
        cancelled_purchase_ids,
    )
    if response["status"] == "cancelled" and cancellation_rows and all(
        row.source == "manual_purchase_cancel" for row in cancellation_rows
    ):
        return {
            "status": "pending",
            "purchase_numbers": [],
            "has_cancelled_history": True,
            "history_count": len(_purchase_history_rows(db, order_id)),
        }
    return response


def get_external_purchase_summaries_by_order_ids(
    db: Session,
    order_ids: Iterable[int],
) -> dict[int, dict[str, Any]]:
    """Load list-page purchase summaries with a fixed number of queries."""

    resolved_ids = sorted({int(order_id) for order_id in order_ids})
    if not resolved_ids:
        return {}
    batches = list(
        db.scalars(
            select(ExternalPackagingPurchaseBatch)
            .options(
                selectinload(
                    ExternalPackagingPurchaseBatch.purchase_orders
                ).selectinload(ExternalPackagingPurchaseOrder.items)
            )
            .where(
                ExternalPackagingPurchaseBatch.sales_order_id.in_(resolved_ids)
            )
        ).all()
    )
    purchase_item_ids = {
        int(item.id)
        for batch in batches
        for purchase in batch.purchase_orders
        for item in purchase.items
    }
    received_totals = _received_totals_by_purchase_item_ids(
        db, purchase_item_ids
    )
    purchase_order_ids = {
        int(purchase.id) for batch in batches for purchase in batch.purchase_orders
    }
    cancellation_rows = list(
        db.scalars(
            select(ExternalPackagingPurchaseCancellation).where(
                ExternalPackagingPurchaseCancellation.purchase_order_id.in_(
                    purchase_order_ids
                )
            )
        ).all()
    ) if purchase_order_ids else []
    cancelled_purchase_ids = {
        int(row.purchase_order_id) for row in cancellation_rows
    }
    manual_cancelled_purchase_ids = {
        int(row.purchase_order_id)
        for row in cancellation_rows
        if row.source == "manual_purchase_cancel"
    }
    batch_by_order_id: dict[int, ExternalPackagingPurchaseBatch] = {}
    for batch in batches:
        current = batch_by_order_id.get(int(batch.sales_order_id))
        if current is None or int(batch.id) > int(current.id):
            batch_by_order_id[int(batch.sales_order_id)] = batch
    cancelled_order_ids = {
        int(order_id)
        for order_id in db.scalars(
            select(Order.id).where(
                Order.id.in_(resolved_ids),
                Order.status.in_(("cancelled", "dead")),
            )
        ).all()
    }
    responses: dict[int, dict[str, Any]] = {}
    for order_id in resolved_ids:
        if order_id in cancelled_order_ids:
            responses[order_id] = {"status": "cancelled", "purchase_numbers": []}
            continue
        batch = batch_by_order_id.get(order_id)
        if batch is None:
            responses[order_id] = {"status": "pending", "purchase_numbers": []}
            continue
        response = _external_purchase_summary_response(
            batch, received_totals, cancelled_purchase_ids
        )
        batch_purchase_ids = {int(row.id) for row in batch.purchase_orders}
        if (
            response["status"] == "cancelled"
            and batch_purchase_ids
            and batch_purchase_ids <= manual_cancelled_purchase_ids
        ):
            response = {
                "status": "pending",
                "purchase_numbers": [],
                "has_cancelled_history": True,
            }
        responses[order_id] = response
    return responses


def list_external_purchase_routing_rows(
    db: Session,
    *,
    visible_customer_ids: set[int] | None,
) -> list[dict[str, Any]]:
    if visible_customer_ids is not None and not visible_customer_ids:
        return []
    statement = (
        select(
            Order.id.label("order_id"),
            Order.order_number,
            Order.customer_id,
            Customer.name.label("customer_name"),
            Order.delivery_date,
            OrderItem.id.label("order_item_id"),
            OrderItem.item_sequence,
            OrderItem.snapshot_product_code,
            OrderItem.snapshot_product_name,
            OrderItem.quantity,
            OrderItem.supply_mode_snapshot,
            OrderItem.external_packaging_category_code_snapshot,
            OrderItem.external_packaging_specification_summary_snapshot,
            OrderItem.external_packaging_purchase_unit_snapshot,
            func.count(SalesOrderItemExternalComponent.id).label("component_count"),
        )
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(
            SalesOrderItemExternalComponent,
            SalesOrderItemExternalComponent.sales_order_item_id == OrderItem.id,
        )
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            Order.status.notin_(("cancelled", "dead", "closed", "archived")),
            ~select(ExternalPackagingPurchaseOrder.id)
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
            .where(
                ExternalPackagingPurchaseBatch.sales_order_id == Order.id,
                ExternalPackagingPurchaseCancellation.id.is_(None),
            )
            .exists(),
            or_(
                ~select(ExternalPackagingPurchaseBatch.id)
                .where(ExternalPackagingPurchaseBatch.sales_order_id == Order.id)
                .exists(),
                select(ExternalPackagingPurchaseCancellation.id)
                .join(
                    ExternalPackagingPurchaseOrder,
                    ExternalPackagingPurchaseOrder.id
                    == ExternalPackagingPurchaseCancellation.purchase_order_id,
                )
                .join(
                    ExternalPackagingPurchaseBatch,
                    ExternalPackagingPurchaseBatch.id
                    == ExternalPackagingPurchaseOrder.batch_id,
                )
                .where(
                    ExternalPackagingPurchaseBatch.sales_order_id == Order.id,
                    ExternalPackagingPurchaseCancellation.source
                    == "manual_purchase_cancel",
                )
                .exists(),
            ),
        )
        .group_by(
            Order.id,
            Order.order_number,
            Order.customer_id,
            Customer.name,
            Order.delivery_date,
            OrderItem.id,
            OrderItem.item_sequence,
            OrderItem.snapshot_product_code,
            OrderItem.snapshot_product_name,
            OrderItem.quantity,
            OrderItem.supply_mode_snapshot,
            OrderItem.external_packaging_category_code_snapshot,
            OrderItem.external_packaging_specification_summary_snapshot,
            OrderItem.external_packaging_purchase_unit_snapshot,
        )
        .order_by(
            Order.delivery_date.is_(None),
            Order.delivery_date,
            Order.id,
            OrderItem.item_sequence,
            OrderItem.id,
        )
    )
    if visible_customer_ids is not None:
        statement = statement.where(Order.customer_id.in_(visible_customer_ids))
    return [
        {
            "id": int(row.order_id),
            "order_id": int(row.order_id),
            "order_number": row.order_number,
            "customer_id": int(row.customer_id),
            "customer_name": row.customer_name,
            "delivery_date": (
                row.delivery_date.isoformat() if row.delivery_date else None
            ),
            "order_item_id": int(row.order_item_id),
            "item_sequence": row.item_sequence,
            "product_code": row.snapshot_product_code,
            "product_name": row.snapshot_product_name,
            "quantity": int(row.quantity),
            "supply_mode": row.supply_mode_snapshot,
            "category_code": row.external_packaging_category_code_snapshot,
            "specification_summary": (
                row.external_packaging_specification_summary_snapshot
            ),
            "purchase_unit": row.external_packaging_purchase_unit_snapshot,
            "component_count": int(row.component_count),
            "status": "pending_confirmation",
        }
        for row in db.execute(statement).all()
    ]


def build_external_purchase_preview(
    db: Session,
    order_id: int,
) -> dict[str, Any]:
    order, components = _order_components(db, order_id)
    if order is None:
        raise ExternalPurchaseContractError("订单不存在", status_code=404)
    if not components:
        raise ExternalPurchaseContractError(
            "该订单没有冻结的外购包装组件，不能补写或猜测旧订单", status_code=409
        )
    summary = get_external_purchase_summary(db, order.id)
    history = _purchase_history_rows(db, order.id)
    if summary["status"] == "cancelled":
        return {
            "order_id": order.id,
            "order_number": order.order_number,
            "status": "cancelled",
            "message": "该订单原外购包材采购已随订单流程撤回作废，不能重新确认。",
            "history": history,
            "items": [],
        }
    if summary["status"] == "confirmed":
        batch = _load_batch(db, int(summary["batch_id"]))
        return {
            "order_id": order.id,
            "order_number": order.order_number,
            "status": "confirmed",
            "confirmation": _serialize_batch(batch),
            "history": history,
            "items": [],
        }
    if order.status in {"cancelled", "dead", "closed", "archived"}:
        raise ExternalPurchaseContractError("订单已终止，不能确认外购包材采购")
    as_of = beijing_today()
    item_ids = {row.sales_order_item_id for row in components}
    order_items = {
        row.id: row
        for row in db.scalars(
            select(OrderItem).where(OrderItem.id.in_(item_ids))
        ).all()
    }
    items: list[dict[str, Any]] = []
    for component in components:
        order_item = order_items[component.sales_order_item_id]
        default_candidate = next(
            (row for row in component.candidates if row.is_default), None
        )
        suggested_quantity = _suggested_quantity(component, int(order_item.quantity))
        candidate_rows = [
            _candidate_preview(
                db,
                candidate,
                component=component,
                quantity=suggested_quantity,
                as_of=as_of,
            )
            for candidate in component.candidates
        ]
        items.append(
            {
                "order_component_id": component.id,
                "sales_order_item_id": order_item.id,
                "item_sequence": order_item.item_sequence,
                "product_code": order_item.snapshot_product_code,
                "product_name": order_item.snapshot_product_name,
                "purpose": component.purpose,
                "category_code": component.category_code,
                "specification_summary": component.specification_summary,
                "suggested_purchase_quantity": _decimal_text(suggested_quantity),
                "purchase_unit": (
                    default_candidate.purchase_unit_snapshot
                    if default_candidate is not None
                    else None
                ),
                "default_candidate_id": (
                    default_candidate.id if default_candidate is not None else None
                ),
                "candidates": candidate_rows,
            }
        )
    return {
        "order_id": order.id,
        "order_number": order.order_number,
        "status": "pending",
        "as_of": as_of.isoformat(),
        "history": history,
        "items": items,
    }


def _suggested_quantity(
    component: SalesOrderItemExternalComponent,
    order_quantity: int,
) -> Decimal:
    default_candidate = next(
        (row for row in component.candidates if row.is_default), None
    )
    if default_candidate is None:
        raise ExternalPurchaseContractError(
            f"组件“{component.purpose}”没有冻结默认候选"
        )
    required = (
        Decimal(order_quantity)
        * Decimal(component.quantity_per_finished_unit)
        * (Decimal("1") + Decimal(component.waste_rate))
    )
    if default_candidate.purchase_unit_snapshot != component.consumption_unit:
        if component.units_per_purchase_unit is None or not component.conversion_basis:
            raise ExternalPurchaseContractError(
                f"组件“{component.purpose}”缺少采购单位换算依据"
            )
        required = required / Decimal(component.units_per_purchase_unit)
    if default_candidate.purchase_unit_snapshot in DISCRETE_PURCHASE_UNITS:
        return required.to_integral_value(rounding=ROUND_CEILING)
    return required.quantize(SIX_PLACES, rounding=ROUND_HALF_UP)


def _request_fingerprint(order_id: int, lines: list[dict[str, Any]]) -> str:
    payload = {
        "order_id": order_id,
        "lines": sorted(
            [
                {
                    "order_component_id": int(row["order_component_id"]),
                    "candidate_id": int(row["candidate_id"]),
                    "purchase_quantity": _decimal_text(
                        Decimal(str(row["purchase_quantity"])).quantize(SIX_PLACES)
                    ),
                }
                for row in lines
            ],
            key=lambda row: row["order_component_id"],
        ),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _next_purchase_number(db: Session, purchase_date: date) -> str:
    value = db.execute(
        text(
            """
            INSERT INTO external_packaging_purchase_daily_sequences
                (sequence_date, last_value)
            VALUES (:sequence_date, 1)
            ON CONFLICT(sequence_date)
            DO UPDATE SET last_value = last_value + 1
            RETURNING last_value
            """
        ),
        {"sequence_date": purchase_date.isoformat()},
    ).scalar_one()
    if int(value) > 999:
        raise ExternalPurchaseContractError("当日外购采购单流水号已超过 999")
    return f"EP-{purchase_date:%Y%m%d}-{int(value):03d}"


def _amounts(
    price: ExternalPackagingPriceVersion,
    *,
    quantity: Decimal,
    unit_price: Decimal,
    tax_amount_per_purchase_unit: Decimal | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    line_amount = (quantity * unit_price).quantize(MONEY, rounding=ROUND_HALF_UP)
    rate = Decimal(price.tax_rate)
    effective_tax_per_unit = (
        tax_amount_per_purchase_unit
        if tax_amount_per_purchase_unit is not None
        else price.tax_amount_per_unit
    )
    if effective_tax_per_unit is not None:
        tax_amount = (
            quantity * Decimal(effective_tax_per_unit)
        ).quantize(MONEY, rounding=ROUND_HALF_UP)
    elif rate <= 0:
        tax_amount = Decimal("0.00")
    elif price.tax_mode == "tax_exclusive":
        tax_amount = (line_amount * rate).quantize(MONEY, rounding=ROUND_HALF_UP)
    else:
        tax_amount = (
            line_amount - (line_amount / (Decimal("1") + rate))
        ).quantize(MONEY, rounding=ROUND_HALF_UP)
    total = (
        line_amount + tax_amount
        if price.tax_mode == "tax_exclusive"
        else line_amount
    ).quantize(MONEY, rounding=ROUND_HALF_UP)
    return line_amount, tax_amount, total


def confirm_external_purchase(
    db: Session,
    *,
    order_id: int,
    idempotency_key: str,
    lines: list[dict[str, Any]],
    user: User,
) -> tuple[ExternalPackagingPurchaseBatch, bool]:
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 120:
        raise ExternalPurchaseContractError(
            "提交标识缺失或过长，请刷新订单后重试", status_code=422
        )
    try:
        fingerprint = _request_fingerprint(order_id, lines)
    except (InvalidOperation, TypeError, ValueError, KeyError) as error:
        raise ExternalPurchaseContractError("采购明细格式不正确", status_code=422) from error

    order = claim_external_purchase_order(db, order_id)
    if order is None:
        raise ExternalPurchaseContractError("订单不存在", status_code=404)
    if order.status in {"cancelled", "dead", "closed", "archived"}:
        raise ExternalPurchaseContractError("订单已终止，不能确认外购包材采购")
    order, components = _order_components(db, order_id, lock_order=True)

    keyed = db.scalar(
        select(ExternalPackagingPurchaseBatch).where(
            ExternalPackagingPurchaseBatch.idempotency_key == key
        )
    )
    if keyed is not None:
        if (
            keyed.sales_order_id != order_id
            or keyed.request_fingerprint != fingerprint
        ):
            raise ExternalPurchaseContractError(
                "同一提交标识对应的采购内容不同，请刷新订单后重新操作"
            )
        cancelled = db.scalar(
            select(ExternalPackagingPurchaseCancellation.id)
            .join(
                ExternalPackagingPurchaseOrder,
                ExternalPackagingPurchaseOrder.id
                == ExternalPackagingPurchaseCancellation.purchase_order_id,
            )
            .where(ExternalPackagingPurchaseOrder.batch_id == keyed.id)
            .limit(1)
        )
        if cancelled is not None:
            raise ExternalPurchaseContractError(
                "该订单原外购包材采购已随流程撤回作废；如需重新采购，请新建正确订单。"
            )
        return _load_batch(db, keyed.id), False

    if not components:
        raise ExternalPurchaseContractError(
            "该订单没有冻结的外购包装组件，不能补写或猜测旧订单"
        )
    existing = db.scalar(
        select(ExternalPackagingPurchaseBatch)
        .join(
            ExternalPackagingPurchaseOrder,
            ExternalPackagingPurchaseOrder.batch_id
            == ExternalPackagingPurchaseBatch.id,
        )
        .outerjoin(
            ExternalPackagingPurchaseCancellation,
            ExternalPackagingPurchaseCancellation.purchase_order_id
            == ExternalPackagingPurchaseOrder.id,
        )
        .where(
            ExternalPackagingPurchaseBatch.sales_order_id == order.id,
            ExternalPackagingPurchaseCancellation.id.is_(None),
        )
        .limit(1)
    )
    if existing is not None:
        raise ExternalPurchaseContractError("该订单的外购包装已经确认采购，请勿重复提交")

    component_by_id = {row.id: row for row in components}
    line_by_component: dict[int, dict[str, Any]] = {}
    for raw in lines:
        component_id = int(raw["order_component_id"])
        if component_id in line_by_component:
            raise ExternalPurchaseContractError("同一外购组件不能重复提交", status_code=422)
        line_by_component[component_id] = raw
    if set(line_by_component) != set(component_by_id):
        raise ExternalPurchaseContractError("必须一次核对并提交该订单的全部外购组件")

    as_of = beijing_today()
    prepared_by_supplier: dict[int, list[dict[str, Any]]] = defaultdict(list)
    supplier_rows: dict[int, Supplier] = {}
    for component_id in sorted(component_by_id):
        component = component_by_id[component_id]
        raw = line_by_component[component_id]
        candidate_id = int(raw["candidate_id"])
        candidate = next(
            (row for row in component.candidates if row.id == candidate_id), None
        )
        if candidate is None:
            raise ExternalPurchaseContractError(
                f"组件“{component.purpose}”只能选择下单时冻结的供应商候选"
            )
        try:
            quantity = Decimal(str(raw["purchase_quantity"])).quantize(SIX_PLACES)
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ExternalPurchaseContractError(
                f"组件“{component.purpose}”采购数量格式不正确", status_code=422
            ) from error
        product, blocked_reason = _product_availability(db, candidate)
        if blocked_reason or product is None:
            raise ExternalPurchaseContractError(
                f"组件“{component.purpose}”：{blocked_reason}"
            )
        price = _current_price(
            db, candidate, component=component, as_of=as_of
        )
        if price is None:
            raise ExternalPurchaseContractError(
                (
                    f"组件“{component.purpose}”{CORNER_GUARD_MISSING_PRICE_MESSAGE}"
                    if component.category_code == CORNER_GUARD_CATEGORY_CODE
                    else f"组件“{component.purpose}”{PACKAGING_MISSING_PRICE_MESSAGE}"
                )
            )
        quantity_error = _purchase_quantity_error(
            quantity, unit=candidate.purchase_unit_snapshot
        )
        if quantity_error:
            raise ExternalPurchaseContractError(
                f"组件“{component.purpose}”：{quantity_error}", status_code=422
            )
        pricing = _resolved_purchase_pricing(
            component, price, purchase_quantity=quantity
        )
        quantity_error = _quantity_error(
            price,
            Decimal(pricing["pricing_quantity"]),
            unit=price.quote_unit,
        )
        if quantity_error:
            raise ExternalPurchaseContractError(
                f"组件“{component.purpose}”：{quantity_error}", status_code=422
            )
        unit_price = Decimal(pricing["purchase_unit_price"])
        tiers = pricing["tier_prices"]
        line_amount, tax_amount, total_amount = _amounts(
            price,
            quantity=quantity,
            unit_price=unit_price,
            tax_amount_per_purchase_unit=pricing[
                "tax_amount_per_purchase_unit"
            ],
        )
        order_item = db.get(OrderItem, component.sales_order_item_id)
        if order_item is None or order_item.order_id != order.id:
            raise ExternalPurchaseContractError("订单外购组件来源不完整")
        minimum_quantity = _suggested_quantity(
            component, int(order_item.quantity)
        )
        if quantity < minimum_quantity:
            raise ExternalPurchaseContractError(
                (
                    f"组件“{component.purpose}”采购数量不能低于冻结需求 "
                    f"{_decimal_text(minimum_quantity)} "
                    f"{candidate.purchase_unit_snapshot}；MOQ 或包装倍数只允许向上调整"
                ),
                status_code=422,
            )
        supplier_rows[product.supplier.id] = product.supplier
        prepared_by_supplier[product.supplier.id].append(
            {
                "component": component,
                "candidate": candidate,
                "order_item": order_item,
                "price": price,
                "quantity": quantity,
                "unit_price": unit_price,
                "tiers": tiers,
                "pricing": pricing,
                "line_amount": line_amount,
                "tax_amount": tax_amount,
                "total_amount": total_amount,
            }
        )

    for supplier_id, rows in prepared_by_supplier.items():
        currencies = {row["price"].currency for row in rows}
        if len(currencies) != 1:
            supplier = supplier_rows[supplier_id]
            raise ExternalPurchaseContractError(
                f"供应商“{supplier.display_name or supplier.standard_name}”存在不同币种，不能合并为一张采购单"
            )

    batch = ExternalPackagingPurchaseBatch(
        sales_order_id=order.id,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        confirmed_by=user.id,
    )
    db.add(batch)
    db.flush()
    for supplier_id in sorted(prepared_by_supplier):
        rows = prepared_by_supplier[supplier_id]
        supplier = supplier_rows[supplier_id]
        goods = sum((row["line_amount"] for row in rows), Decimal("0.00"))
        taxes = sum((row["tax_amount"] for row in rows), Decimal("0.00"))
        totals = sum((row["total_amount"] for row in rows), Decimal("0.00"))
        purchase_order = ExternalPackagingPurchaseOrder(
            batch_id=batch.id,
            purchase_number=_next_purchase_number(db, as_of),
            supplier_id=supplier.id,
            supplier_name_snapshot=(
                supplier.display_name or supplier.standard_name
            ),
            supplier_business_code_snapshot=supplier.business_code,
            currency=rows[0]["price"].currency,
            status="confirmed",
            goods_amount=goods.quantize(MONEY),
            tax_amount=taxes.quantize(MONEY),
            total_amount=totals.quantize(MONEY),
            confirmed_by=user.id,
        )
        db.add(purchase_order)
        db.flush()
        for row in rows:
            component = row["component"]
            candidate = row["candidate"]
            price = row["price"]
            db.add(
                ExternalPackagingPurchaseItem(
                    purchase_order_id=purchase_order.id,
                    sales_order_id=order.id,
                    sales_order_item_id=row["order_item"].id,
                    order_component_id=component.id,
                    order_candidate_id=candidate.id,
                    purpose_snapshot=component.purpose,
                    category_code_snapshot=component.category_code,
                    specification_summary_snapshot=component.specification_summary,
                    specification_json_snapshot=component.specification_json,
                    external_product_id_snapshot=(
                        candidate.external_product_id_snapshot
                    ),
                    external_product_version_snapshot=(
                        candidate.external_product_version_snapshot
                    ),
                    supplier_product_code_snapshot=(
                        candidate.supplier_product_code_snapshot
                    ),
                    product_name_snapshot=candidate.product_name_snapshot,
                    price_version_id=price.id,
                    price_version_number_snapshot=price.version_number,
                    purchase_quantity=row["quantity"],
                    purchase_unit=candidate.purchase_unit_snapshot,
                    unit_price=row["unit_price"],
                    currency=price.currency,
                    tax_mode=price.tax_mode,
                    tax_rate=price.tax_rate,
                    tax_amount_per_unit=row["pricing"][
                        "tax_amount_per_purchase_unit"
                    ],
                    line_amount=row["line_amount"],
                    tax_amount=row["tax_amount"],
                    total_amount=row["total_amount"],
                    tier_basis_json=json.dumps(
                        row["tiers"], ensure_ascii=False, separators=(",", ":")
                    ),
                    moq_quantity_snapshot=price.moq_quantity,
                    packaging_multiple_snapshot=price.packaging_multiple,
                    shipping_fee_mode=price.shipping_fee_mode,
                    shipping_fee_snapshot=price.shipping_fee,
                    sample_fee_snapshot=price.sample_fee,
                    plate_fee_snapshot=price.plate_fee,
                    die_fee_snapshot=price.die_fee,
                    price_evidence_reference_snapshot=price.evidence_reference,
                )
            )
    confirmed_external_item_ids = {
        int(row["order_item"].id)
        for rows in prepared_by_supplier.values()
        for row in rows
        if row["order_item"].supply_mode_snapshot == "external_purchase"
    }
    for order_item_id in confirmed_external_item_ids:
        order_item = db.get(OrderItem, order_item_id)
        if order_item is not None:
            order_item.requisition_status = "外购包材已采购"
    db.flush()
    return _load_batch(db, batch.id), True


def _load_batch(db: Session, batch_id: int) -> ExternalPackagingPurchaseBatch:
    batch = db.scalar(
        select(ExternalPackagingPurchaseBatch)
        .options(
            selectinload(ExternalPackagingPurchaseBatch.purchase_orders).selectinload(
                ExternalPackagingPurchaseOrder.items
            )
        )
        .where(ExternalPackagingPurchaseBatch.id == batch_id)
    )
    if batch is None:
        raise ExternalPurchaseContractError("外购包装采购确认记录不存在", status_code=404)
    return batch


def _serialize_batch(batch: ExternalPackagingPurchaseBatch) -> dict[str, Any]:
    return {
        "batch_id": batch.id,
        "order_id": batch.sales_order_id,
        "confirmed_at": utc_naive_to_api(batch.confirmed_at) if batch.confirmed_at else None,
        "purchase_orders": [
            {
                "id": order.id,
                "purchase_number": order.purchase_number,
                "supplier_id": order.supplier_id,
                "supplier_name": order.supplier_name_snapshot,
                "currency": order.currency,
                "status": order.status,
                "goods_amount": _decimal_text(Decimal(order.goods_amount)),
                "tax_amount": _decimal_text(Decimal(order.tax_amount)),
                "total_amount": _decimal_text(Decimal(order.total_amount)),
                "items": [
                    {
                        "id": item.id,
                        "order_component_id": item.order_component_id,
                        "purpose": item.purpose_snapshot,
                        "specification_summary": item.specification_summary_snapshot,
                        "specification": _parse_specification_json(
                            item.specification_json_snapshot
                        ),
                        "supplier_product_code": item.supplier_product_code_snapshot,
                        "product_name": item.product_name_snapshot,
                        "purchase_quantity": _decimal_text(
                            Decimal(item.purchase_quantity)
                        ),
                        "purchase_unit": item.purchase_unit,
                        "unit_price": _decimal_text(Decimal(item.unit_price)),
                        "currency": item.currency,
                        "tax_mode": item.tax_mode,
                        "tax_rate": _decimal_text(Decimal(item.tax_rate)),
                        "line_amount": _decimal_text(Decimal(item.line_amount)),
                        "tax_amount": _decimal_text(Decimal(item.tax_amount)),
                        "total_amount": _decimal_text(Decimal(item.total_amount)),
                        "price_version_id": item.price_version_id,
                        "price_version_number": item.price_version_number_snapshot,
                        "shipping_fee_mode": item.shipping_fee_mode,
                        "shipping_fee": (
                            _decimal_text(Decimal(item.shipping_fee_snapshot))
                            if item.shipping_fee_snapshot is not None
                            else None
                        ),
                        "sample_fee": (
                            _decimal_text(Decimal(item.sample_fee_snapshot))
                            if item.sample_fee_snapshot is not None
                            else None
                        ),
                        "plate_fee": (
                            _decimal_text(Decimal(item.plate_fee_snapshot))
                            if item.plate_fee_snapshot is not None
                            else None
                        ),
                        "die_fee": (
                            _decimal_text(Decimal(item.die_fee_snapshot))
                            if item.die_fee_snapshot is not None
                            else None
                        ),
                    }
                    for item in order.items
                ],
            }
            for order in batch.purchase_orders
        ],
    }


def serialize_external_purchase_batch(
    batch: ExternalPackagingPurchaseBatch,
) -> dict[str, Any]:
    return _serialize_batch(batch)


def _parse_specification_json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _purchase_print_specification(row: ExternalPackagingPurchaseItem) -> dict[str, Any]:
    specification = _parse_specification_json(row.specification_json_snapshot)
    material = str(
        specification.get("material")
        or specification.get("material_type")
        or specification.get("substrate")
        or ""
    ).strip()
    aperture = specification.get("aperture_mm")
    dimensions = ""
    length = specification.get("length_mm")
    width = specification.get("width_mm")
    thickness = specification.get("thickness_mm")
    if all(value not in (None, "") for value in (length, width, thickness)):
        dimensions = "×".join(
            _decimal_text(Decimal(str(value))) for value in (length, width, thickness)
        )
    if row.category_code_snapshot == "honeycomb_board":
        summary = str(row.specification_summary_snapshot or "")
        if not material:
            match = re.search(r"材质\s*([^，,\s]+)", summary)
            material = match.group(1) if match else ""
        if aperture in (None, ""):
            match = re.search(r"孔径\s*([0-9]+(?:\.[0-9]+)?)", summary)
            aperture = match.group(1) if match else None
    return {
        "material": material or None,
        "aperture_mm": (
            _decimal_text(Decimal(str(aperture)))
            if aperture not in (None, "")
            else None
        ),
        "dimensions_mm": dimensions or None,
    }


def build_external_purchase_print(
    db: Session,
    purchase_order_id: int,
) -> dict[str, Any]:
    purchase = db.scalar(
        select(ExternalPackagingPurchaseOrder)
        .options(
            joinedload(ExternalPackagingPurchaseOrder.batch),
            selectinload(ExternalPackagingPurchaseOrder.items),
        )
        .where(ExternalPackagingPurchaseOrder.id == purchase_order_id)
    )
    if purchase is None:
        raise ExternalPurchaseContractError("外购包装采购单不存在", status_code=404)

    cancellation = db.scalar(
        select(ExternalPackagingPurchaseCancellation.id).where(
            ExternalPackagingPurchaseCancellation.purchase_order_id == purchase.id
        )
    )
    if cancellation is not None:
        raise ExternalPurchaseContractError("外购包装采购单已作废，禁止继续打印")

    sales_order = db.get(Order, purchase.batch.sales_order_id)
    if sales_order is None:
        raise ExternalPurchaseContractError(
            "采购单关联订单不存在，禁止猜测打印来源", status_code=409
        )
    if sales_order.status in {"cancelled", "dead"}:
        raise ExternalPurchaseContractError("关联订单已终止，禁止继续打印采购单")
    company = db.get(CompanyConfig, 1)

    items: list[dict[str, Any]] = []
    for row in purchase.items:
        specification = _purchase_print_specification(row)
        items.append(
            {
                "specification_summary": row.specification_summary_snapshot,
                **specification,
                "purchase_quantity": _decimal_text(
                    Decimal(row.purchase_quantity)
                ),
                "purchase_unit": row.purchase_unit,
            }
        )

    return {
        "purchase_number": purchase.purchase_number,
        "confirmed_at": (
            utc_naive_to_api(purchase.confirmed_at) if purchase.confirmed_at else None
        ),
        "supplier": {
            "name": purchase.supplier_name_snapshot,
        },
        "buyer": {
            "company_name": company.company_name if company else "",
        },
        "items": items,
    }
