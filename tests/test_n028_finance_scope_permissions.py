from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def scoped_finance_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import (
        Invoice,
        ReturnReceipt,
        ReturnReceiptItem,
        Statement,
        StatementItem,
    )
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n028-finance-scope.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as db:
        scoped_user = User(
            username="n028-finance-scoped",
            password_hash=hash_password("FinanceScope123!"),
            role="finance",
            real_name="Scoped Finance",
            must_change_password=False,
            customer_access_mode="selected",
        )
        empty_user = User(
            username="n028-finance-empty",
            password_hash=hash_password("FinanceScope123!"),
            role="finance",
            real_name="Empty Finance",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(name="N028 Finance Customer A")
        customer_b = Customer(name="N028 Finance Customer B")
        db.add_all([scoped_user, empty_user, customer_a, customer_b])
        db.flush()
        db.add_all(
            [
                UserCustomerScope(
                    user_id=scoped_user.id,
                    customer_id=customer_a.id,
                ),
                UserPermissionOverride(
                    user_id=scoped_user.id,
                    permission_code="cost.view",
                    is_allowed=False,
                ),
                UserPermissionOverride(
                    user_id=empty_user.id,
                    permission_code="cost.view",
                    is_allowed=False,
                ),
            ]
        )

        products = {}
        for label, customer in (("a", customer_a), ("b", customer_b)):
            product = Product(
                customer_id=customer.id,
                product_code=f"N028-{label.upper()}-BOX",
                customer_material_code=f"N028-{label.upper()}-MAT",
                product_name=f"Finance Box {label.upper()}",
                box_category="normal",
                cost_unit_price=Decimal("2.00"),
            )
            db.add(product)
            db.flush()
            products[label] = product

        def add_delivery_chain(
            *,
            label: str,
            customer: Customer,
            kind: str,
            with_receipt: bool,
        ) -> tuple[Delivery, DeliveryItem, ReturnReceiptItem | None]:
            product = products[label]
            order = Order(
                order_number=f"N028-{label.upper()}-{kind}-ORDER",
                customer_id=customer.id,
                customer_po=f"PO-{label.upper()}-{kind}",
                order_date=date(2026, 7, 1),
                delivery_date=date(2026, 7, 13),
                status="pending_reconciliation",
                payment_status="unpaid",
                total_amount=Decimal("30.00"),
            )
            db.add(order)
            db.flush()
            order_item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=10,
                delivered_quantity=10,
                unit_price=Decimal("3.00"),
                subtotal=Decimal("30.00"),
                material_status="received",
                snapshot_product_code=product.product_code,
                snapshot_product_name=product.product_name,
                snapshot_spec="300x200x100",
                snapshot_material="K=A",
            )
            db.add(order_item)
            db.flush()
            delivery = Delivery(
                delivery_number=f"N028-{label.upper()}-{kind}-DELIVERY",
                customer_id=customer.id,
                delivery_date=date(2026, 7, 13),
                status="dispatched",
                total_quantity=10,
            )
            db.add(delivery)
            db.flush()
            delivery_item = DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=order_item.id,
                delivered_quantity=10,
            )
            db.add(delivery_item)
            db.flush()
            if not with_receipt:
                return delivery, delivery_item, None
            receipt = ReturnReceipt(
                delivery_id=delivery.id,
                actual_received_date=date(2026, 7, 13),
                signed_by=f"Signer {label.upper()}",
                status="confirmed",
            )
            db.add(receipt)
            db.flush()
            receipt_item = ReturnReceiptItem(
                return_receipt_id=receipt.id,
                delivery_item_id=delivery_item.id,
                actual_received_quantity=10,
            )
            db.add(receipt_item)
            db.flush()
            return delivery, delivery_item, receipt_item

        ids: dict[str, int] = {
            "customer_a": customer_a.id,
            "customer_b": customer_b.id,
        }
        for label, customer in (("a", customer_a), ("b", customer_b)):
            statement_delivery, _, statement_receipt_item = add_delivery_chain(
                label=label,
                customer=customer,
                kind="STATEMENT",
                with_receipt=True,
            )
            assert statement_receipt_item is not None
            statement = Statement(
                statement_number=(
                    "ST-202607-901" if label == "a" else "ST-202607-902"
                ),
                customer_id=customer.id,
                statement_month="2026-07",
                total_receivable=Decimal("30.00"),
                total_gross_profit=Decimal("10.00"),
                status="unsettled",
            )
            db.add(statement)
            db.flush()
            db.add(
                StatementItem(
                    statement_id=statement.id,
                    return_receipt_item_id=statement_receipt_item.id,
                    actual_received_quantity=10,
                    unit_price_snapshot=Decimal("3.00"),
                    unit_cost_snapshot=Decimal("2.00"),
                    receivable_amount=Decimal("30.00"),
                    gross_profit_amount=Decimal("10.00"),
                )
            )
            invoice = Invoice(
                statement_id=statement.id,
                invoice_number=f"INV-N028-{label.upper()}",
                invoice_date=date(2026, 7, 14),
                invoice_amount=Decimal("5.00"),
            )
            db.add(invoice)
            pending_delivery, _, pending_receipt_item = add_delivery_chain(
                label=label,
                customer=customer,
                kind="PENDING",
                with_receipt=True,
            )
            open_delivery, open_delivery_item, _ = add_delivery_chain(
                label=label,
                customer=customer,
                kind="OPEN",
                with_receipt=False,
            )
            db.flush()
            ids.update(
                {
                    f"statement_{label}": statement.id,
                    f"invoice_{label}": invoice.id,
                    f"receipt_{label}": statement_receipt_item.return_receipt_id,
                    f"receipt_item_{label}": statement_receipt_item.id,
                    f"pending_delivery_{label}": pending_delivery.id,
                    f"pending_receipt_item_{label}": pending_receipt_item.id,
                    f"open_delivery_{label}": open_delivery.id,
                    f"open_delivery_item_{label}": open_delivery_item.id,
                    f"statement_delivery_{label}": statement_delivery.id,
                }
            )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(finance_router, prefix="/api/finance")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "FinanceScope123!"},
    )
    assert response.status_code == 200, response.text


def _receipt_payload(ids: dict[str, int], label: str) -> dict:
    return {
        "delivery_id": ids[f"open_delivery_{label}"],
        "actual_received_date": "2026-07-15",
        "signed_by": "N028 signer",
        "items": [
            {
                "delivery_item_id": ids[f"open_delivery_item_{label}"],
                "actual_received_quantity": 10,
            }
        ],
    }


def test_selected_scope_filters_finance_reads_and_redacts_costs(
    scoped_finance_app,
) -> None:
    app, ids = scoped_finance_app
    with TestClient(app) as client:
        _login(client, "n028-finance-scoped")

        statements = client.get("/api/finance/statements")
        assert statements.status_code == 200
        assert statements.json()["total"] == 1
        assert [row["id"] for row in statements.json()["items"]] == [
            ids["statement_a"]
        ]
        assert "total_gross_profit" not in statements.json()["items"][0]
        assert (
            client.get(
                "/api/finance/statements",
                params={"customer_id": ids["customer_b"]},
            ).status_code
            == 403
        )
        current = client.get(
            "/api/finance/current-customer-months",
            params={"statement_month": "2026-07"},
        )
        assert current.status_code == 200
        assert current.json()["total"] == 1
        assert [row["customer_id"] for row in current.json()["items"]] == [
            ids["customer_a"]
        ]
        assert "total_gross_profit" not in current.json()["items"][0]
        assert (
            client.get(
                "/api/finance/current-customer-months",
                params={
                    "statement_month": "2026-07",
                    "customer_id": ids["customer_b"],
                },
            ).status_code
            == 403
        )
        settled_history = client.get(
            "/api/finance/settled-customer-months",
            params={"year": 2026},
        )
        assert settled_history.status_code == 200
        assert settled_history.json()["total"] == 0
        assert (
            client.get(
                "/api/finance/settled-customer-months",
                params={"year": 2026, "customer_id": ids["customer_b"]},
            ).status_code
            == 403
        )
        report = client.get(
            "/api/finance/reports/monthly-yearly",
            params={"year": 2026, "statement_month": "2026-07"},
        )
        assert report.status_code == 200
        assert {
            row["customer_id"]
            for row in report.json()["selected_month_customers"]
        } == {ids["customer_a"]}
        assert {
            row["customer_id"]
            for row in report.json()["customer_balances"]
        } == {ids["customer_a"]}
        assert "total_gross_profit" not in report.text
        assert "bank_account" not in report.text
        assert (
            client.get(
                "/api/finance/reports/monthly-yearly",
                params={
                    "year": 2026,
                    "statement_month": "2026-07",
                    "customer_id": ids["customer_b"],
                },
            ).status_code
            == 403
        )

        detail = client.get(f"/api/finance/statements/{ids['statement_a']}")
        assert detail.status_code == 200
        assert "total_gross_profit" not in detail.json()
        assert {
            "unit_cost_snapshot",
            "gross_profit_amount",
        }.isdisjoint(detail.json()["items"][0])
        assert (
            client.get(f"/api/finance/statements/{ids['statement_b']}").status_code
            == 403
        )

        assert (
            client.get(
                f"/api/finance/statements/{ids['statement_a']}/export"
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/finance/statements/{ids['statement_b']}/export"
            ).status_code
            == 403
        )

        invoices = client.get("/api/finance/invoices")
        assert invoices.status_code == 200
        assert [row["id"] for row in invoices.json()["items"]] == [ids["invoice_a"]]
        assert (
            client.get(
                "/api/finance/invoices",
                params={"statement_id": ids["statement_b"]},
            ).status_code
            == 403
        )

        customers = client.get("/api/finance/statement-customers")
        assert customers.status_code == 200
        assert [row["id"] for row in customers.json()["items"]] == [
            ids["customer_a"]
        ]
        pending = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": ids["customer_a"]},
        )
        assert pending.status_code == 200
        assert [row["return_receipt_item_id"] for row in pending.json()["items"]] == [
            ids["pending_receipt_item_a"]
        ]
        assert (
            client.get(
                "/api/finance/pending_statements",
                params={"customer_id": ids["customer_b"]},
            ).status_code
            == 403
        )
        assert (
            client.get(f"/api/finance/return_receipts/{ids['receipt_a']}").status_code
            == 200
        )
        assert (
            client.get(f"/api/finance/return_receipts/{ids['receipt_b']}").status_code
            == 403
        )


def test_selected_scope_rejects_customer_b_writes_and_allows_customer_a(
    scoped_finance_app,
) -> None:
    app, ids = scoped_finance_app
    with TestClient(app) as client:
        _login(client, "n028-finance-scoped")

        assert (
            client.post(
                "/api/finance/return_receipts",
                json=_receipt_payload(ids, "b"),
            ).status_code
            == 403
        )
        receipt_update = {
            "actual_received_date": "2026-07-16",
            "signed_by": "Blocked",
            "items": [
                {
                    "delivery_item_id": ids["open_delivery_item_b"],
                    "actual_received_quantity": 10,
                }
            ],
        }
        assert (
            client.put(
                f"/api/finance/return_receipts/{ids['receipt_b']}",
                json=receipt_update,
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"/api/finance/return_receipts/{ids['receipt_b']}/reconciliation-month",
                json={
                    "reconciliation_month": "2026-08",
                    "expected_version": 1,
                    "idempotency_key": "p1-89-out-of-scope-month",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/finance/return_receipts/{ids['receipt_b']}/cancel"
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/finance/statements",
                json={
                    "customer_id": ids["customer_b"],
                    "statement_month": "2026-07",
                    "return_receipt_item_ids": [ids["pending_receipt_item_b"]],
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/finance/invoices",
                json={
                    "statement_id": ids["statement_b"],
                    "invoice_number": "BLOCKED-N028",
                    "invoice_date": "2026-07-16",
                    "invoice_amount": "1.00",
                },
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"/api/finance/statements/{ids['statement_b']}/settle",
                json={
                    "amount": "1.00",
                    "settlement_date": "2026-07-16",
                    "account": "Blocked",
                },
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"/api/finance/statements/{ids['statement_b']}",
                json={"statement_month": "2026-07"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/finance/statements/{ids['statement_b']}/cancel"
            ).status_code
            == 403
        )

        created_receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(ids, "a"),
        )
        assert created_receipt.status_code == 201, created_receipt.text
        created_statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": ids["customer_a"],
                "statement_month": "2026-07",
                "return_receipt_item_ids": [ids["pending_receipt_item_a"]],
            },
        )
        assert created_statement.status_code == 201, created_statement.text
        assert "total_gross_profit" not in created_statement.json()


def test_empty_selected_scope_returns_zero_lists_and_forbids_resources(
    scoped_finance_app,
) -> None:
    app, ids = scoped_finance_app
    with TestClient(app) as client:
        _login(client, "n028-finance-empty")

        statements = client.get("/api/finance/statements")
        assert statements.status_code == 200
        assert statements.json()["total"] == 0
        assert statements.json()["items"] == []
        current = client.get(
            "/api/finance/current-customer-months",
            params={"statement_month": "2026-07"},
        )
        assert current.status_code == 200
        assert current.json()["total"] == 0
        assert current.json()["items"] == []
        settled_history = client.get(
            "/api/finance/settled-customer-months",
            params={"year": 2026},
        )
        assert settled_history.status_code == 200
        assert settled_history.json()["total"] == 0
        assert settled_history.json()["items"] == []
        report = client.get(
            "/api/finance/reports/monthly-yearly",
            params={"year": 2026, "statement_month": "2026-07"},
        )
        assert report.status_code == 200
        assert report.json()["selected_month_customers"] == []
        assert report.json()["customer_balances"] == []
        assert report.json()["yearly_summary"]["record_count"] == 0
        assert all(
            Decimal(str(row["reconciled_receivable_amount"]))
            == Decimal("0.00")
            for row in report.json()["monthly"]
        )
        assert client.get("/api/finance/statement-customers").json()["items"] == []
        invoices = client.get("/api/finance/invoices")
        assert invoices.status_code == 200
        assert invoices.json()["total"] == 0
        assert invoices.json()["items"] == []

        assert (
            client.get(f"/api/finance/statements/{ids['statement_a']}").status_code
            == 403
        )
        assert (
            client.get(
                f"/api/finance/statements/{ids['statement_a']}/export"
            ).status_code
            == 403
        )
        assert (
            client.get(f"/api/finance/return_receipts/{ids['receipt_a']}").status_code
            == 403
        )
        assert (
            client.get(
                "/api/finance/pending_statements",
                params={"customer_id": ids["customer_a"]},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/finance/return_receipts",
                json=_receipt_payload(ids, "a"),
            ).status_code
            == 403
        )
