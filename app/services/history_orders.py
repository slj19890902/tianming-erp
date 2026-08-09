from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Integer, cast, func, select
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


def build_display_registry_for_order_ids(
    db: Session,
    order_ids: set[int] | list[int] | tuple[int, ...],
) -> DisplayOrderRegistry:
    """Build exact legacy display numbers while returning only requested rows.

    The ordinary registry materializes every historical ``Order`` object.  A
    dashboard projection only needs the handful of historical orders currently
    actionable, but their sequence still has to be calculated against all
    historical siblings on the same effective date.  A window query performs
    that ranking in SQLite and filters the returned rows to the requested IDs.
    """

    normalized_ids = sorted({int(order_id) for order_id in order_ids if order_id})
    if not normalized_ids:
        return DisplayOrderRegistry(by_order_id={}, by_display_number={})

    effective_date = func.coalesce(
        Order.order_date,
        func.date(Order.created_at),
    ).label("effective_date")
    numeric_suffix = cast(
        func.substr(
            Order.order_number,
            func.instr(Order.order_number, "-") + 1,
        ),
        Integer,
    )
    ranked = (
        select(
            Order.id.label("order_id"),
            effective_date,
            func.row_number()
            .over(
                partition_by=effective_date,
                order_by=(numeric_suffix, Order.id),
            )
            .label("display_sequence"),
        )
        .where(Order.order_number.like("RUIDA-%"))
        .subquery()
    )
    rows = db.execute(
        select(
            ranked.c.order_id,
            ranked.c.effective_date,
            ranked.c.display_sequence,
        ).where(ranked.c.order_id.in_(normalized_ids))
    ).all()

    by_order_id: dict[int, str] = {}
    by_display_number: dict[str, int] = {}
    for order_id, raw_date, display_sequence in rows:
        normalized_id = int(order_id)
        if raw_date is None:
            display = f"TM00000000-{normalized_id:04d}"
        else:
            date_text = str(raw_date).split(" ", 1)[0].replace("-", "")
            display = f"TM{date_text}-{int(display_sequence):04d}"
        by_order_id[normalized_id] = display
        by_display_number[display] = normalized_id
    return DisplayOrderRegistry(
        by_order_id=by_order_id,
        by_display_number=by_display_number,
    )


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
