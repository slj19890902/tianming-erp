from __future__ import annotations

import re

import pytest

from app.services.order_status_policy import (
    ALL_ORDER_STATUSES,
    DELIVERY_CANDIDATE_ORDER_STATUSES,
    FULFILLMENT_TERMINAL_ORDER_STATUSES,
    MANAGEMENT_ORDER_STATUSES,
    MANAGEMENT_TERMINAL_ORDER_STATUSES,
    MATERIAL_RECEIPT_TO_DELIVERY_ORDER_STATUSES,
    ORDER_ITEM_ACTIVE_ORDER_STATUSES,
    PRODUCTION_STATUS_REFRESH_ORDER_STATUSES,
    material_receipt_recalculated_order_status,
    order_item_allows_forward_fulfillment,
    order_item_forward_block_message,
    order_item_forward_block_reason,
)


EXPECTED_ORDER_STATUSES = frozenset(
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
EXPECTED_ACTIVE_STATUSES = frozenset(
    {
        "pending_confirmation",
        "pending_production",
        "production",
        "pending_delivery",
        "partially_delivered",
    }
)


def test_persisted_statuses_are_exhaustively_classified() -> None:
    assert ALL_ORDER_STATUSES == EXPECTED_ORDER_STATUSES
    assert ORDER_ITEM_ACTIVE_ORDER_STATUSES == EXPECTED_ACTIVE_STATUSES
    assert FULFILLMENT_TERMINAL_ORDER_STATUSES == (
        EXPECTED_ORDER_STATUSES - EXPECTED_ACTIVE_STATUSES
    )
    assert not (
        ORDER_ITEM_ACTIVE_ORDER_STATUSES
        & FULFILLMENT_TERMINAL_ORDER_STATUSES
    )


def test_model_check_constraint_and_policy_cannot_drift_apart() -> None:
    from app.models.order import Order

    constraint = next(
        row
        for row in Order.__table__.constraints
        if row.name == "ck_sales_orders_status"
    )
    persisted_statuses = frozenset(
        re.findall(r"'([^']+)'", str(constraint.sqltext))
    )
    assert persisted_statuses == ALL_ORDER_STATUSES


def test_aggregate_and_operation_specific_status_sets_stay_distinct() -> None:
    assert PRODUCTION_STATUS_REFRESH_ORDER_STATUSES == frozenset(
        {
            "pending_confirmation",
            "pending_production",
            "production",
            "pending_delivery",
        }
    )
    assert "partially_delivered" not in PRODUCTION_STATUS_REFRESH_ORDER_STATUSES
    assert MATERIAL_RECEIPT_TO_DELIVERY_ORDER_STATUSES == frozenset(
        {"pending_production", "production"}
    )
    assert DELIVERY_CANDIDATE_ORDER_STATUSES == frozenset(
        {
            "pending_production",
            "production",
            "pending_delivery",
            "partially_delivered",
        }
    )
    assert MANAGEMENT_TERMINAL_ORDER_STATUSES == frozenset(
        {"archived", "closed", "dead", "cancelled"}
    )
    assert MANAGEMENT_ORDER_STATUSES == frozenset(
        {
            "pending_confirmation",
            "archived",
            "closed",
            "dead",
            "cancelled",
        }
    )


@pytest.mark.parametrize("status", sorted(EXPECTED_ACTIVE_STATUSES))
def test_every_active_status_allows_only_an_open_remaining_item(status: str) -> None:
    assert order_item_allows_forward_fulfillment(
        order_status=status,
        ordered_quantity=100,
        delivered_quantity=0,
        is_force_closed=False,
    )
    assert order_item_forward_block_reason(
        order_status=status,
        ordered_quantity=100,
        delivered_quantity=0,
        is_force_closed=True,
    ) == "item_force_closed"
    assert order_item_forward_block_reason(
        order_status=status,
        ordered_quantity=100,
        delivered_quantity=100,
        is_force_closed=False,
    ) == "item_fully_delivered"


@pytest.mark.parametrize(
    "status",
    sorted(EXPECTED_ORDER_STATUSES - EXPECTED_ACTIVE_STATUSES),
)
def test_every_terminal_status_blocks_new_item_fulfillment(status: str) -> None:
    assert order_item_forward_block_reason(
        order_status=status,
        ordered_quantity=100,
        delivered_quantity=0,
        is_force_closed=False,
    ) == "order_status_blocked"


def test_partial_delivery_is_an_order_aggregate_not_a_sibling_line_closure() -> None:
    assert order_item_forward_block_reason(
        order_status="partially_delivered",
        ordered_quantity=100,
        delivered_quantity=0,
        is_force_closed=False,
    ) is None
    assert order_item_forward_block_reason(
        order_status="partially_delivered",
        ordered_quantity=100,
        delivered_quantity=40,
        is_force_closed=False,
    ) is None
    assert order_item_forward_block_reason(
        order_status="partially_delivered",
        ordered_quantity=100,
        delivered_quantity=100,
        is_force_closed=False,
    ) == "item_fully_delivered"


def test_unknown_future_status_fails_closed_with_an_accurate_reason() -> None:
    reason = order_item_forward_block_reason(
        order_status="future_unclassified_status",
        ordered_quantity=100,
        delivered_quantity=0,
        is_force_closed=False,
    )
    assert reason == "order_status_blocked"
    assert order_item_forward_block_message(
        reason,
        action="收料",
        order_status="future_unclassified_status",
    ) == "订单当前状态不允许继续收料（当前：future_unclassified_status）"


@pytest.mark.parametrize(
    ("status", "remaining", "expected"),
    (
        ("pending_production", 0, "pending_delivery"),
        ("production", 0, "pending_delivery"),
        ("pending_delivery", 1, "pending_production"),
        ("pending_delivery", 0, "pending_delivery"),
        ("pending_confirmation", 0, "pending_confirmation"),
        ("partially_delivered", 0, "partially_delivered"),
        ("partially_delivered", 1, "partially_delivered"),
        ("closed", 0, "closed"),
        ("future_unclassified_status", 0, "future_unclassified_status"),
    ),
)
def test_material_only_recalculation_never_downgrades_delivery_or_terminal_state(
    status: str,
    remaining: int,
    expected: str,
) -> None:
    assert material_receipt_recalculated_order_status(
        current_status=status,
        remaining_unreceived_items=remaining,
    ) == expected
