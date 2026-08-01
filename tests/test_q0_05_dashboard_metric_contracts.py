from datetime import date
from decimal import Decimal

from app.api.finance import _statement_period
from app.services.dashboard_metric_contracts import (
    build_authoritative_snapshot,
    financial_balance_rows,
    metric_definitions,
    stable_identities,
)


def test_same_order_ten_pending_lines_are_ten_business_items() -> None:
    rows = [
        {"item_id": item_id, "order_id": 88, "is_merge_group": False}
        for item_id in range(1, 11)
    ]

    assert stable_identities("pending_material", rows) == [
        "order-item:1",
        "order-item:10",
        "order-item:2",
        "order-item:3",
        "order-item:4",
        "order-item:5",
        "order-item:6",
        "order-item:7",
        "order-item:8",
        "order-item:9",
    ]


def test_incoming_and_production_use_their_own_stable_fact_ids() -> None:
    assert stable_identities(
        "pending_incoming",
        [{"requisition_item_id": 17, "order_item_id": 9}],
    ) == ["requisition-item:17"]
    assert stable_identities("pending_production", []) == []


def test_delivery_and_receipt_count_customers_not_accumulated_lines() -> None:
    delivery_rows = [
        {"customer_id": 5, "item_id": item_id} for item_id in range(1, 101)
    ] + [{"customer_id": 9, "item_id": 101}]
    receipt_rows = [
        {"customer_id": 5, "delivery_id": 1},
        {"customer_id": 5, "delivery_id": 2},
    ]

    assert stable_identities("pending_delivery", delivery_rows) == [
        "customer:5",
        "customer:9",
    ]
    assert stable_identities("pending_receipt", receipt_rows) == ["customer:5"]


def test_financial_balances_ignore_stale_statement_status_text() -> None:
    statements = [
        {
            "customer_id": 5,
            "status": "settled",
            "total_receivable": Decimal("1000.00"),
            "invoiced_amount": Decimal("400.00"),
            "settled_amount": Decimal("250.00"),
        },
        {
            "customer_id": 5,
            "status": "unsettled",
            "total_receivable": Decimal("200.00"),
            "invoiced_amount": Decimal("200.00"),
            "settled_amount": Decimal("200.00"),
        },
        {
            "customer_id": 9,
            "status": "unsettled",
            "total_receivable": Decimal("300.00"),
            "invoiced_amount": Decimal("300.00"),
            "settled_amount": Decimal("0.00"),
        },
    ]

    invoice_rows, payment_rows = financial_balance_rows(statements)

    assert invoice_rows == [{"customer_id": 5, "amount": Decimal("600.00")}]
    assert payment_rows == [
        {"customer_id": 5, "amount": Decimal("750.00")},
        {"customer_id": 9, "amount": Decimal("300.00")},
    ]


def test_full_snapshot_freezes_units_sources_month_and_amount_formulas() -> None:
    snapshot = build_authoritative_snapshot(
        pending_material_rows=[
            {"item_id": item_id, "order_id": 1, "is_merge_group": False}
            for item_id in range(1, 11)
        ],
        pending_incoming_rows=[{"stock_replenishment_item_id": 8}],
        pending_production_rows=[],
        pending_delivery_rows=[{"customer_id": 5}, {"customer_id": 5}],
        pending_receipt_rows=[{"customer_id": 6}, {"customer_id": 6}],
        pending_reconciliation_rows=[
            {"customer_id": 5, "statement_month": "2026-07", "amount": "120.00"},
            {"customer_id": 5, "statement_month": "2026-07", "amount": "80.00"},
        ],
        statement_rows=[
            {
                "customer_id": 5,
                "total_receivable": "1000.00",
                "invoiced_amount": "600.00",
                "settled_amount": "500.00",
            }
        ],
        statement_month="2026-07",
        as_of="2026-07-30T10:00:00+08:00",
    )

    metrics = snapshot["metrics"]
    assert metrics["pending_material"]["count"] == 10
    assert metrics["pending_incoming"]["count"] == 1
    assert metrics["pending_production"]["count"] == 0
    assert metrics["pending_delivery"]["count"] == 1
    assert metrics["pending_receipt"]["count"] == 1
    assert metrics["pending_reconciliation"]["count"] == 1
    assert metrics["pending_reconciliation"]["amount"] == "200.00"
    assert metrics["pending_invoice"]["count"] == 1
    assert metrics["pending_invoice"]["amount"] == "400.00"
    assert metrics["pending_payment"]["count"] == 1
    assert metrics["pending_payment"]["amount"] == "500.00"
    assert snapshot["timezone"] == "Asia/Shanghai"
    assert snapshot["statement_month"] == "2026-07"
    assert {row["key"] for row in metric_definitions()} == set(metrics)


def test_statement_month_uses_customer_cycle_and_cross_year_boundary() -> None:
    assert _statement_period("2026-07", 20) == (
        date(2026, 6, 20),
        date(2026, 7, 19),
    )
    assert _statement_period("2027-01", 20) == (
        date(2026, 12, 20),
        date(2027, 1, 19),
    )
