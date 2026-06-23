from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Order


LEGACY_PREFIXES = ("RUIDA-", "ruida-", "Ruida-")
LEGACY_TOKENS = ("RUIDA", "ruida", "Ruida", "瑞达")
SAFE_HISTORY_LABEL = "旧系统历史订单"


@dataclass
class DisplayOrderRegistry:
    by_order_id: dict[int, str]
    by_display_number: dict[str, int]


def is_history_order_number(order_number: str | None) -> bool:
    if not order_number:
        return False
    return any(order_number.startswith(prefix) for prefix in LEGACY_PREFIXES)


def sanitize_user_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    for token in LEGACY_TOKENS:
        text = text.replace(token, "旧系统")
    return text


def history_effective_date(order: Order) -> date | None:
    if order.order_date is not None:
        return order.order_date
    if order.created_at is not None:
        if isinstance(order.created_at, datetime):
            return order.created_at.date()
    return None


def build_display_registry(db: Session) -> DisplayOrderRegistry:
    rows = db.scalars(
        select(Order)
        .where(Order.order_number.like("RUIDA-%"))
        .order_by(Order.order_date.asc(), Order.created_at.asc(), Order.id.asc())
    ).all()
    groups: dict[date, list[Order]] = {}
    no_date: list[Order] = []
    for order in rows:
        effective = history_effective_date(order)
        if effective is None:
            no_date.append(order)
            continue
        groups.setdefault(effective, []).append(order)

    by_order_id: dict[int, str] = {}
    by_display_number: dict[str, int] = {}
    for effective_date, orders in groups.items():
        for index, order in enumerate(
            sorted(orders, key=lambda item: (_legacy_numeric_suffix(item.order_number), item.id)),
            start=1,
        ):
            display = f"TM{effective_date:%Y%m%d}-{index:04d}"
            by_order_id[order.id] = display
            by_display_number[display] = order.id
    for order in sorted(no_date, key=lambda item: (_legacy_numeric_suffix(item.order_number), item.id)):
        display = f"TM00000000-{order.id:04d}"
        by_order_id[order.id] = display
        by_display_number[display] = order.id
    return DisplayOrderRegistry(by_order_id=by_order_id, by_display_number=by_display_number)


def display_order_number(order: Order, registry: DisplayOrderRegistry | None = None) -> str:
    if not is_history_order_number(order.order_number):
        return order.order_number
    if registry is not None and order.id in registry.by_order_id:
        return registry.by_order_id[order.id]
    effective = history_effective_date(order)
    if effective is None:
        return f"TM00000000-{order.id:04d}"
    return f"TM{effective:%Y%m%d}-{order.id:04d}"


def filter_order_ids_for_display_search(
    db: Session, keyword: str | None, registry: DisplayOrderRegistry | None = None
) -> set[int]:
    if not keyword:
        return set()
    normalized = keyword.strip().upper()
    if not normalized.startswith("TM"):
        return set()
    registry = registry or build_display_registry(db)
    return {
        order_id
        for display, order_id in registry.by_display_number.items()
        if normalized in display.upper()
    }


def _legacy_numeric_suffix(order_number: str | None) -> int:
    raw = str(order_number or "")
    try:
        return int(raw.split("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def serialize_order_number_fields(
    order: Order, registry: DisplayOrderRegistry | None = None
) -> dict[str, Any]:
    display = display_order_number(order, registry)
    return {
        "order_number": display,
        "display_order_number": display,
        "is_history_order": is_history_order_number(order.order_number),
        "history_order_tag": "历史订单" if is_history_order_number(order.order_number) else None,
    }
