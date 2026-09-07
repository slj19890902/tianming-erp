from datetime import date, datetime

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from tests.test_erp_review_receipt_document_price import (
    assert_no_price_writes, confirmed_payload, document_price_app, seed_document_case,
)


URL = "/api/finance/supplier-settlements/receipt-price-issues"


def test_check_finds_old_unsettled_and_open_period_receipts_without_writes(document_price_app):
    from app.models.incoming_receipt import IncomingReceipt
    app, factory, old = document_price_app
    current = seed_document_case(factory, "-open")
    with factory() as db:
        db.get(IncomingReceipt, old["receipt_id"]).received_at = datetime(2025, 1, 5, 3)
        db.get(IncomingReceipt, current["receipt_id"]).received_at = datetime(2026, 9, 7, 3)
        db.commit()
    statements = []
    engine = factory.kw["bind"]
    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().split()[0].upper())
    with TestClient(app) as client:
        month = client.get("/api/finance/supplier-settlements", params={"settlement_month": "2026-08"})
        assert month.status_code == 200
        assert month.json()["issues"] == []
        event.listen(engine, "before_cursor_execute", record)
        try:
            response = client.get(URL)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert response.status_code == 200
        body = response.json()
        assert body["scope"] == "all_posted_missing_prices"
        assert {row["incoming_receipt_item_id"] for row in body["issues"]} == {
            old["receipt_item_id"], current["receipt_item_id"],
        }
        assert {row["receipt_date"] for row in body["issues"]} == {"2025-01-05", "2026-09-07"}
        assert all(row["can_confirm"] and row["code"] == "PAPERBOARD_FROZEN_PRICE_MISSING"
                   for row in body["issues"])
        assert body["next_after_id"] is None and body["has_more"] is False
    assert statements and set(statements) <= {"SELECT", "PRAGMA"}
    assert_no_price_writes(factory)


def test_check_is_paged_by_receipt_id_and_validates_bounds(document_price_app):
    app, factory, first = document_price_app
    second = seed_document_case(factory, "-second")
    third = seed_document_case(factory, "-third")
    found, after = [], 0
    with TestClient(app) as client:
        for _ in range(3):
            response = client.get(URL, params={"limit": 1, "after_id": after})
            assert response.status_code == 200
            body = response.json()
            assert body["scanned_count"] == 1
            found.extend(row["incoming_receipt_item_id"] for row in body["issues"])
            after = body["next_after_id"]
            assert body["has_more"] == (after is not None)
        assert after is None
        for params in ({"limit": 0}, {"limit": 101}, {"after_id": -1}):
            assert client.get(URL, params=params).status_code == 422
    assert found == [first["receipt_item_id"], second["receipt_item_id"], third["receipt_item_id"]]


def test_frozen_or_reversed_receipts_do_not_reappear(document_price_app):
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    app, factory, frozen = document_price_app
    reversed_case = seed_document_case(factory, "-reversed")
    with factory() as db:
        db.get(IncomingReceipt, reversed_case["receipt_id"]).status = "reversed"
        db.get(IncomingReceiptItem, reversed_case["receipt_item_id"]).status = "reversed"
        db.commit()
    with TestClient(app) as client:
        url, payload = confirmed_payload(client, frozen)
        assert client.post(url + "/confirm", json=payload).status_code == 201
        response = client.get(URL)
        assert response.status_code == 200
        assert response.json()["issues"] == []
        assert response.json()["scanned_count"] == 0


@pytest.mark.parametrize("blocked", ["period", "linked", "source", "dimensions"])
def test_blocked_rows_keep_reason_without_offering_confirmation(document_price_app, blocked):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
    from app.models.supplier_settlement import SupplierMonthlyStatement, SupplierMonthlyStatementLine
    app, factory, fixture = document_price_app
    expected = {
        "period": "SUPPLIER_RECEIPT_SETTLEMENT_LOCKED",
        "linked": "SUPPLIER_RECEIPT_SETTLEMENT_LOCKED",
        "source": "PAPERBOARD_PURCHASE_SOURCE_MISSING",
        "dimensions": "SUPPLIER_RECEIPT_DIMENSIONS_MISSING",
    }[blocked]
    with factory() as db:
        if blocked in {"period", "linked"}:
            statement = SupplierMonthlyStatement(statement_number="CHECK-CLOSED-PERIOD",
                supplier_id=fixture["supplier_id"], supplier_name_snapshot="凭据纸板厂",
                settlement_month="2026-08", period_start=date(2026, 7, 21), period_end=date(2026, 8, 20),
                currency="CNY", tax_basis="tax_inclusive", status="draft" if blocked == "linked" else "confirmed_pending_invoice",
                erp_amount=14, adjusted_amount=14, confirmed_amount=14, generated_by=fixture["user_id"])
            db.add(statement)
            if blocked == "linked":
                db.flush()
                db.add(SupplierMonthlyStatementLine(statement_id=statement.id, source_type="paperboard",
                    source_key=f"paperboard:{fixture['receipt_item_id']}",
                    incoming_receipt_item_id=fixture["receipt_item_id"], purchase_document_number="PO-DOCUMENT-1",
                    receipt_number="IR-DOCUMENT-1", receipt_date=date(2026, 8, 5), category_label="纸板",
                    material_or_product_snapshot="DOC-BC", received_quantity=10, quantity_unit="张",
                    frozen_unit_price="2.80", price_unit="per_square_meter", currency="CNY",
                    tax_basis="tax_inclusive", tax_rate="0.13", erp_amount=14, tax_amount="1.61",
                    source_link="/test/document"))
        elif blocked == "source":
            db.get(IncomingReceiptItem, fixture["receipt_item_id"]).supplier_order_item_id = None
        else:
            db.get(SupplierRequisitionOrderItem, fixture["source_id"]).report_length_mm = None
        db.commit()
    with TestClient(app) as client:
        response = client.get(URL)
        assert response.status_code == 200
        issue, = response.json()["issues"]
        assert issue["can_confirm"] is False and issue["code"] == expected
        assert issue["message"] and issue["source_key"] == f"paperboard:{fixture['receipt_item_id']}"
        assert "source_hash" not in issue
    assert_no_price_writes(factory)


def test_check_requires_company_finance_scope(document_price_app):
    from app.models.user import User
    app, factory, fixture = document_price_app
    with factory() as db:
        user = db.get(User, fixture["user_id"])
        user.role, user.customer_access_mode = "finance", "selected"
        db.commit()
    with TestClient(app) as client:
        assert client.get(URL).status_code == 403
    assert_no_price_writes(factory)
