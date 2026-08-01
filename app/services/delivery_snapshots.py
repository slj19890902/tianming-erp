from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.delivery import DeliveryItem
from app.models.order import OrderItem
from app.models.product import Product


def _text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _product_specification(product: Product | None) -> str | None:
    if product is None:
        return None
    dimensions = [
        value
        for value in (product.length_mm, product.width_mm, product.height_mm)
        if value is not None
    ]
    if not dimensions:
        return None
    return "×".join(
        str(int(value)) if value == int(value) else str(value)
        for value in dimensions
    )


def build_order_delivery_snapshot(
    db: Session,
    order_item: OrderItem,
) -> dict[str, str | None]:
    """Freeze customer-facing product facts when a delivery line is created."""

    product = db.get(Product, order_item.product_id) if order_item.product_id else None
    return {
        "product_code_snapshot": (
            _text(order_item.snapshot_product_code)
            or _text(product.product_code if product else None)
        ),
        "product_name_snapshot": (
            _text(order_item.snapshot_product_name)
            or _text(product.product_name if product else None)
        ),
        "specification_snapshot": (
            _text(order_item.snapshot_spec) or _product_specification(product)
        ),
    }


def ensure_order_delivery_snapshot(
    db: Session,
    delivery_item: DeliveryItem,
    order_item: OrderItem,
) -> None:
    """Fill missing snapshots without rewriting facts already frozen on the delivery."""

    values = build_order_delivery_snapshot(db, order_item)
    for field, value in values.items():
        if not _text(getattr(delivery_item, field, None)) and value is not None:
            setattr(delivery_item, field, value)
