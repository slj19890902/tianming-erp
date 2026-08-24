"""Authoritative persisted-order status semantics for the order fulfillment chain.

The persisted ``Order.status`` is an order-level aggregate.  In particular,
``partially_delivered`` says that *some* delivery happened; it does not close
every sibling ``OrderItem``.  Forward and reversible item operations therefore
combine this shared order-status policy with the target line's force-close and
remaining-quantity facts.

Keep production status recalculation separate.  Recalculation may update the
pre-delivery workflow states, but it must never downgrade an aggregate
``partially_delivered`` order after work continues on an untouched sibling.
"""

from __future__ import annotations

from typing import Any, Literal


ALL_ORDER_STATUSES = frozenset(
    {
        "pending_confirmation",
        "pending_production",
        "production",
        "pending_delivery",
        "partially_delivered",
        "pending_reconciliation",
        "pending_invoice",
        "pending_payment",
        "delivered",
        "completed",
        "archived",
        "closed",
        "dead",
        "cancelled",
    }
)

PERSISTED_ORDER_STATUS_LABELS = {
    "pending_confirmation": "待确认",
    "pending_production": "待生产",
    "production": "生产中",
    "pending_delivery": "待送货",
    "partially_delivered": "部分送完",
    "pending_reconciliation": "待对账",
    "pending_invoice": "待开票",
    "pending_payment": "待结款",
    "delivered": "已送完",
    "completed": "订单完成",
    "archived": "已归档",
    "closed": "已结档",
    "dead": "死单",
    "cancelled": "已作废",
}

# Statuses in which an individual, still-open order line may continue normal
# requisition, receipt, production, inventory, or delivery work.
ORDER_ITEM_ACTIVE_ORDER_STATUSES = frozenset(
    {
        "pending_confirmation",
        "pending_production",
        "production",
        "pending_delivery",
        "partially_delivered",
    }
)

# Order-level states that must not create new fulfillment facts.  This is the
# exact complement of the active set so a newly added persisted status fails
# closed until its semantics are explicitly classified here.
FULFILLMENT_TERMINAL_ORDER_STATUSES = frozenset(
    ALL_ORDER_STATUSES - ORDER_ITEM_ACTIVE_ORDER_STATUSES
)

MANAGEMENT_TERMINAL_ORDER_STATUSES = frozenset(
    {"archived", "closed", "dead", "cancelled"}
)
MANAGEMENT_ORDER_STATUSES = frozenset(
    {"pending_confirmation", *MANAGEMENT_TERMINAL_ORDER_STATUSES}
)

# Production/material refresh is allowed to recalculate only pre-delivery
# aggregate states.  It intentionally excludes ``partially_delivered``.
PRODUCTION_STATUS_REFRESH_ORDER_STATUSES = frozenset(
    {
        "pending_confirmation",
        "pending_production",
        "production",
        "pending_delivery",
    }
)

# Material-only fallback helpers may advance an order only after all material
# is received and no production task exists.  Confirmation and any delivery
# aggregate must remain untouched by that fallback transition.
MATERIAL_RECEIPT_TO_DELIVERY_ORDER_STATUSES = frozenset(
    {"pending_production", "production"}
)

# Delivery selection does not accept an unconfirmed order, while every other
# active state can contain a deliverable remaining line.
DELIVERY_CANDIDATE_ORDER_STATUSES = frozenset(
    ORDER_ITEM_ACTIVE_ORDER_STATUSES - {"pending_confirmation"}
)

ForwardBlockReason = Literal[
    "order_status_blocked",
    "item_force_closed",
    "item_fully_delivered",
]


def normalized_order_status(status: object) -> str:
    return str(status or "").strip().lower()


def order_status_allows_item_fulfillment(status: object) -> bool:
    return normalized_order_status(status) in ORDER_ITEM_ACTIVE_ORDER_STATUSES


def persisted_order_status_label(status: object) -> str:
    """Return the shared employee-facing label for a persisted status."""

    normalized = normalized_order_status(status)
    return PERSISTED_ORDER_STATUS_LABELS.get(normalized, normalized or "未知")


def order_item_forward_fulfillment_sql_conditions(
    *,
    order_status_column: Any,
    ordered_quantity_column: Any,
    delivered_quantity_column: Any,
    is_force_closed_column: Any,
) -> tuple[Any, Any, Any]:
    """Return the SQL equivalent of the scalar forward-fulfillment policy.

    Query projections, including the warehouse map, must use the same three
    facts as execution entrypoints: an active aggregate status, an open target
    item and remaining quantity on that item.
    """

    return (
        order_status_column.in_(ORDER_ITEM_ACTIVE_ORDER_STATUSES),
        is_force_closed_column.is_(False),
        delivered_quantity_column < ordered_quantity_column,
    )


def order_item_remaining_quantity(
    *,
    ordered_quantity: object,
    delivered_quantity: object,
) -> int:
    return max(int(ordered_quantity or 0) - int(delivered_quantity or 0), 0)


def order_item_forward_block_reason(
    *,
    order_status: object,
    ordered_quantity: object,
    delivered_quantity: object,
    is_force_closed: object,
) -> ForwardBlockReason | None:
    """Return the shared item-level fulfillment blocker, if any.

    The order-level status is checked first so an unknown future status fails
    closed.  A partial delivery remains eligible only while the target item is
    itself open and has quantity left to fulfill.
    """

    if not order_status_allows_item_fulfillment(order_status):
        return "order_status_blocked"
    if bool(is_force_closed):
        return "item_force_closed"
    if (
        order_item_remaining_quantity(
            ordered_quantity=ordered_quantity,
            delivered_quantity=delivered_quantity,
        )
        <= 0
    ):
        return "item_fully_delivered"
    return None


def order_item_allows_forward_fulfillment(
    *,
    order_status: object,
    ordered_quantity: object,
    delivered_quantity: object,
    is_force_closed: object,
) -> bool:
    return (
        order_item_forward_block_reason(
            order_status=order_status,
            ordered_quantity=ordered_quantity,
            delivered_quantity=delivered_quantity,
            is_force_closed=is_force_closed,
        )
        is None
    )


def order_item_forward_block_message(
    reason: ForwardBlockReason,
    *,
    action: str,
    order_status: object,
) -> str:
    """Render one accurate employee-facing reason for a blocked item action."""

    if reason == "item_force_closed":
        return f"订单明细已强制关闭，不能继续{action}"
    if reason == "item_fully_delivered":
        return f"订单明细已全部送货，不能继续{action}"
    status = normalized_order_status(order_status) or "未知"
    return f"订单当前状态不允许继续{action}（当前：{status}）"


def material_receipt_recalculated_order_status(
    *,
    current_status: object,
    remaining_unreceived_items: object,
) -> str:
    """Return the narrow material-only aggregate transition.

    This fallback is used only when an item has no production task projection.
    It must never rewrite confirmation, partial-delivery, finance, or terminal
    aggregates.
    """

    status = normalized_order_status(current_status)
    remaining = max(int(remaining_unreceived_items or 0), 0)
    if remaining == 0 and status in MATERIAL_RECEIPT_TO_DELIVERY_ORDER_STATUSES:
        return "pending_delivery"
    if remaining > 0 and status == "pending_delivery":
        return "pending_production"
    return status
