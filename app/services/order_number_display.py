from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Order


@dataclass(frozen=True)
class DisplayOrderRegistry:
    """Current ERP order numbers keyed for shared list/print serializers."""

    by_order_id: dict[int, str]


def build_display_registry(db: Session) -> DisplayOrderRegistry:
    rows = db.execute(select(Order.id, Order.order_number)).all()
    return DisplayOrderRegistry(
        by_order_id={int(order_id): str(order_number) for order_id, order_number in rows}
    )


def build_display_registry_for_order_ids(
    db: Session,
    order_ids: set[int] | list[int] | tuple[int, ...],
) -> DisplayOrderRegistry:
    normalized_ids = sorted({int(order_id) for order_id in order_ids if order_id})
    if not normalized_ids:
        return DisplayOrderRegistry(by_order_id={})
    rows = db.execute(
        select(Order.id, Order.order_number).where(Order.id.in_(normalized_ids))
    ).all()
    return DisplayOrderRegistry(
        by_order_id={int(order_id): str(order_number) for order_id, order_number in rows}
    )


def display_order_number(
    order: Order,
    registry: DisplayOrderRegistry | dict[int, str] | None = None,
) -> str:
    if isinstance(registry, DisplayOrderRegistry):
        return registry.by_order_id.get(int(order.id), str(order.order_number))
    if isinstance(registry, dict):
        return str(registry.get(int(order.id), order.order_number))
    return str(order.order_number)


def serialize_order_number_fields(
    order: Order,
    registry: DisplayOrderRegistry | dict[int, str] | None = None,
) -> dict[str, Any]:
    display = display_order_number(order, registry)
    return {
        "order_number": display,
        "display_order_number": display,
        "customer_po": order.customer_po,
    }
