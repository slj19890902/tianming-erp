from __future__ import annotations

import sqlite3
from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def finance_api_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "finance.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        users = [
            User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=role,
                display_name=role,
                must_change_password=False,
            )
            for role in ("admin", "finance", "sales", "workshop")
        ]
        customer = Customer(
            customer_number=1,
            customer_code="SME",
            name="苏州思迈尔包装有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add_all([*users, customer])
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="SME-001",
            customer_material_code="KH-001",
            product_name="五层加强纸箱",
            legacy_material_text="K=A-BC",
            box_category="normal",
            cost_unit_price=Decimal("2.70"),
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="PO-20260613-001",
            customer_id=customer.id,
            customer_po="CPO-001",
            order_date=date(2026, 6, 1),
            delivery_date=date(2026, 6, 13),
            status="partially_delivered",
            payment_status="unpaid",
            total_amount=Decimal("360"),
        )
        session.add(order)
        session.flush()
        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=100,
            delivered_quantity=80,
            unit_price=Decimal("3.60"),
            subtotal=Decimal("360"),
            material_status="received",
            snapshot_product_name="五层加强纸箱",
            snapshot_spec="520×350×300mm",
            snapshot_material="K=A-BC",
        )
        session.add(order_item)
        session.flush()
        delivery = Delivery(
            delivery_number="DH-20260613-001",
            customer_id=customer.id,
            delivery_date=date(2026, 6, 13),
            vehicle_number="苏E·12345",
            status="dispatched",
            total_quantity=80,
            dispatched_at=datetime(2026, 6, 13, 9, 0, 0),
        )
        session.add(delivery)
        session.flush()
        session.add(
            DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=order_item.id,
                delivered_quantity=80,
                remarks="第一批",
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(finance_router, prefix="/api/finance")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def _receipt_payload(reason: str | None = "压坏拒收 2 个") -> dict:
    return {
        "delivery_id": 1,
        "actual_received_date": "2026-06-14",
        "signed_by": "王经理",
        "items": [
            {
                "delivery_item_id": 1,
                "actual_received_quantity": 78,
                "difference_reason": reason,
            }
        ],
    }


def test_short_receipt_requires_reason_and_rolls_back(finance_api_app) -> None:
    from app.models.finance import ReturnReceipt

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        response = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(" "),
        )

    assert response.status_code == 400
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ReturnReceipt)) == 0


def test_create_receipt_78_of_80_and_reject_duplicate(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        duplicate = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )

    assert created.status_code == 201, created.text
    assert created.json()["status"] == "confirmed"
    assert created.json()["items"][0]["delivered_quantity"] == 80
    assert created.json()["items"][0]["actual_received_quantity"] == 78
    assert duplicate.status_code == 409


def test_return_receipt_detail_can_be_loaded_for_editing(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        response = client.get(
            f"/api/finance/return_receipts/{created.json()['id']}"
        )

    assert response.status_code == 200
    assert response.json()["delivery_id"] == 1
    assert response.json()["items"][0]["actual_received_quantity"] == 78


def test_receipt_can_be_edited_before_statement_but_not_after(
    finance_api_app,
) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        receipt_id = created.json()["id"]
        receipt_item_id = created.json()["items"][0]["id"]
        edited = client.put(
            f"/api/finance/return_receipts/{receipt_id}",
            json={
                "actual_received_date": "2026-06-14",
                "signed_by": "李经理",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 79,
                        "difference_reason": "压坏拒收1个",
                    }
                ],
            },
        )
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
            },
        )
        locked = client.put(
            f"/api/finance/return_receipts/{receipt_id}",
            json={
                "actual_received_date": "2026-06-14",
                "signed_by": "李经理",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 80,
                        "difference_reason": None,
                    }
                ],
            },
        )

    assert edited.status_code == 200, edited.text
    assert edited.json()["items"][0]["actual_received_quantity"] == 79
    assert statement.status_code == 201, statement.text
    assert locked.status_code == 409


def test_confirmed_receipt_can_be_cancelled_and_reconfirmed(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        receipt_id = created.json()["id"]
        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")
        reopened = client.put(
            f"/api/finance/return_receipts/{receipt_id}",
            json={
                "actual_received_date": "2026-06-14",
                "signed_by": "李经理",
                "items": [
                    {
                        "delivery_item_id": 1,
                        "actual_received_quantity": 78,
                        "difference_reason": "压坏拒收 2 个",
                    }
                ],
            },
        )
        refreshed = client.get(f"/api/finance/return_receipts/{receipt_id}")

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert reopened.status_code == 200, reopened.text
    assert refreshed.json()["status"] == "confirmed"
    assert refreshed.json()["signed_by"] == "李经理"


def test_cancelled_receipt_is_blocked_when_statement_exists(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        created = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        receipt_id = created.json()["id"]
        receipt_item_id = created.json()["items"][0]["id"]
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
            },
        )
        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")

    assert statement.status_code == 201, statement.text
    assert cancelled.status_code == 409
    assert "对账" in cancelled.json()["detail"]


def test_statement_detail_endpoint_returns_items_and_summary(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        statement = _create_statement(client)
        response = client.get(f"/api/finance/statements/{statement['id']}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["statement_number"] == statement["statement_number"]
    assert body["customer_name"] == "苏州思迈尔包装有限公司"
    assert body["status"] == "unsettled"
    assert body["items"][0]["receivable_amount"] == "280.80"


def test_statement_customer_options_cover_eligible_customers(finance_api_app) -> None:
    app, session_factory = finance_api_app
    with session_factory() as session:
        from app.models.customer import Customer
        from app.models.delivery import Delivery, DeliveryItem
        from app.models.order import Order, OrderItem
        from app.models.product import Product

        customer = Customer(
            customer_number=2,
            customer_code="HT",
            name="昆山宏泰包装有限公司",
            payment_term_days=30,
        )
        session.add(customer)
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="HT-001",
            customer_material_code="HT-001",
            product_name="五层纸箱",
            legacy_material_text="K=A",
            box_category="normal",
            cost_unit_price=Decimal("1.20"),
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="PO-20260613-002",
            customer_id=customer.id,
            customer_po="HT-PO-001",
            order_date=date(2026, 6, 2),
            delivery_date=date(2026, 6, 14),
            status="partially_delivered",
            payment_status="unpaid",
            total_amount=Decimal("120"),
        )
        session.add(order)
        session.flush()
        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=50,
            delivered_quantity=50,
            unit_price=Decimal("2.40"),
            subtotal=Decimal("120"),
            material_status="received",
            snapshot_product_name="五层纸箱",
            snapshot_spec="520×350×300mm",
            snapshot_material="K=A",
        )
        session.add(order_item)
        session.flush()
        delivery = Delivery(
            delivery_number="DH-20260614-002",
            customer_id=customer.id,
            delivery_date=date(2026, 6, 14),
            vehicle_number="苏E·54321",
            status="dispatched",
            total_quantity=50,
            dispatched_at=datetime(2026, 6, 14, 10, 0, 0),
        )
        session.add(delivery)
        session.flush()
        session.add(
            DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=order_item.id,
                delivered_quantity=50,
                remarks="第二批",
            )
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "finance")
        first_receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        receipt = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": 2,
                "actual_received_date": "2026-06-15",
                "signed_by": "王经理",
                "items": [
                    {
                        "delivery_item_id": 2,
                        "actual_received_quantity": 50,
                        "difference_reason": None,
                    }
                ],
            },
        )
        response = client.get("/api/finance/statement-customers", params={"statement_month": "2026-06"})

    assert first_receipt.status_code == 201, first_receipt.text
    assert receipt.status_code == 201, receipt.text
    assert response.status_code == 200, response.text
    customer_names = {row["name"] for row in response.json()["items"]}
    assert "苏州思迈尔包装有限公司" in customer_names
    assert "昆山宏泰包装有限公司" in customer_names


def test_statement_edit_and_cancel_endpoints_exist_for_frontend_controls(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        statement = _create_statement(client)
        edited = client.put(
            f"/api/finance/statements/{statement['id']}",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
            },
        )
        cancelled = client.post(f"/api/finance/statements/{statement['id']}/cancel")

    assert edited.status_code == 200, edited.text
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


@pytest.mark.parametrize("role", ["sales", "workshop"])
def test_finance_routes_reject_non_finance_roles(
    finance_api_app,
    role: str,
) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, role)
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        pending = client.get(
            "/api/finance/pending_statements?customer_id=1"
        )

    assert receipt.status_code == 403
    assert pending.status_code == 403


def test_pending_statement_and_statement_snapshot_amounts(
    finance_api_app,
) -> None:
    from app.models.finance import Statement, StatementItem

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_receipt_payload(),
        )
        receipt_item_id = receipt.json()["items"][0]["id"]
        pending = client.get(
            "/api/finance/pending_statements?customer_id=1"
        )
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
            },
        )
        pending_after = client.get(
            "/api/finance/pending_statements?customer_id=1"
        )
        duplicate = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-06",
                "return_receipt_item_ids": [receipt_item_id],
            },
        )

    assert pending.status_code == 200
    assert Decimal(
        str(pending.json()["items"][0]["receivable_amount"])
    ) == Decimal("280.80")
    assert statement.status_code == 201, statement.text
    assert Decimal(str(statement.json()["total_receivable"])) == Decimal("280.80")
    assert Decimal(str(statement.json()["total_gross_profit"])) == Decimal("70.20")
    assert pending_after.json()["items"] == []
    assert duplicate.status_code in {400, 409}
    with session_factory() as session:
        master = session.scalar(select(Statement))
        line = session.scalar(select(StatementItem))
    assert master.total_receivable == Decimal("280.80")
    assert line.unit_price_snapshot == Decimal("3.6000")
    assert line.unit_cost_snapshot == Decimal("2.7000")
    assert line.receivable_amount == Decimal("280.80")
    assert line.gross_profit_amount == Decimal("70.20")


def test_finance_api_hides_legacy_history_prefix_in_pending_statement(
    finance_api_app,
) -> None:
    from app.models.order import Order

    app, session_factory = finance_api_app
    with session_factory() as session:
        session.get(Order, 1).order_number = "RUIDA-42001"
        session.commit()
    with TestClient(app) as client:
        _login(client, "finance")
        client.post("/api/finance/return_receipts", json=_receipt_payload())
        pending = client.get("/api/finance/pending_statements?customer_id=1")

    assert pending.status_code == 200
    first = pending.json()["items"][0]
    assert first["display_order_number"] == "TM20260601-0001"
    assert first["order_number"] == "TM20260601-0001"
    assert "RUIDA" not in str(pending.json())


def test_phase8_migration_preserves_legacy_finance_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE return_confirmations (
                id INTEGER PRIMARY KEY,
                delivery_id INTEGER NOT NULL
            );
            CREATE TABLE statements (
                id INTEGER PRIMARY KEY,
                statement_no TEXT NOT NULL
            );
            INSERT INTO return_confirmations VALUES (3, 7);
            INSERT INTO statements VALUES (4, 'ST20260500001');
            """
        )
        connection.commit()

    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase8-migration-test")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        receipt = connection.execute(
            "SELECT id, delivery_id FROM return_confirmations"
        ).fetchone()
        statement = connection.execute(
            "SELECT id, statement_no FROM statements"
        ).fetchone()

    assert {
        "finance_return_receipts",
        "finance_return_receipt_items",
        "finance_statements",
        "finance_statement_items",
    } <= tables
    assert receipt == (3, 7)
    assert statement == (4, "ST20260500001")


def _create_statement(client: TestClient) -> dict:
    receipt = client.post(
        "/api/finance/return_receipts",
        json=_receipt_payload(),
    )
    assert receipt.status_code == 201, receipt.text
    receipt_item_id = receipt.json()["items"][0]["id"]
    statement = client.post(
        "/api/finance/statements",
        json={
            "customer_id": 1,
            "statement_month": "2026-06",
            "return_receipt_item_ids": [receipt_item_id],
        },
    )
    assert statement.status_code == 201, statement.text
    return statement.json()


def test_invoice_and_partial_settlement_are_cumulative_and_audited(
    finance_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.finance import Invoice, SettlementRecord, Statement

    app, session_factory = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        statement = _create_statement(client)
        first_invoice = client.post(
            "/api/finance/invoices",
            json={
                "statement_id": statement["id"],
                "invoice_number": "INV-202606-001",
                "invoice_date": "2026-06-14",
                "invoice_amount": "180.80",
            },
        )
        second_invoice = client.post(
            "/api/finance/invoices",
            json={
                "statement_id": statement["id"],
                "invoice_number": "INV-202606-002",
                "invoice_date": "2026-06-14",
                "invoice_amount": "100.00",
            },
        )
        partial_payment = client.put(
            f"/api/finance/statements/{statement['id']}/settle",
            json={
                "amount": "80.80",
                "settlement_date": "2026-06-14",
                "account": "中国银行 6688",
            },
        )

    assert first_invoice.status_code == 201, first_invoice.text
    assert second_invoice.status_code == 201, second_invoice.text
    assert partial_payment.status_code == 200, partial_payment.text
    assert Decimal(str(second_invoice.json()["invoiced_amount"])) == Decimal("280.80")
    assert Decimal(str(partial_payment.json()["settled_amount"])) == Decimal("80.80")
    assert partial_payment.json()["status"] == "unsettled"
    with session_factory() as session:
        master = session.get(Statement, statement["id"])
        assert master.invoiced_amount == Decimal("280.80")
        assert master.settled_amount == Decimal("80.80")
        assert session.scalar(select(func.count()).select_from(Invoice)) == 2
        assert session.scalar(select(func.count()).select_from(SettlementRecord)) == 1
        actions = set(session.scalars(select(OperationLog.action)).all())
        assert {"REGISTER_INVOICE", "SETTLE_STATEMENT"} <= actions


def test_exact_settlement_closes_statement_and_rejects_overpayment(
    finance_api_app,
) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        statement = _create_statement(client)
        settled = client.put(
            f"/api/finance/statements/{statement['id']}/settle",
            json={
                "amount": "280.80",
                "settlement_date": "2026-06-14",
                "account": "现金",
            },
        )
        overpayment = client.put(
            f"/api/finance/statements/{statement['id']}/settle",
            json={
                "amount": "0.01",
                "settlement_date": "2026-06-14",
                "account": "现金",
            },
        )

    assert settled.status_code == 200, settled.text
    assert settled.json()["status"] == "settled"
    assert settled.json()["status_label"] == "已结清"
    assert overpayment.status_code == 409


def test_settlement_accepts_missing_or_blank_account(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        statement = _create_statement(client)
        missing_account = client.put(
            f"/api/finance/statements/{statement['id']}/settle",
            json={
                "amount": "100.00",
                "settlement_date": "2026-06-14",
            },
        )
        blank_account = client.put(
            f"/api/finance/statements/{statement['id']}/settle",
            json={
                "amount": "100.00",
                "settlement_date": "2026-06-15",
                "account": "",
            },
        )

    assert missing_account.status_code == 200, missing_account.text
    assert blank_account.status_code == 200, blank_account.text


@pytest.mark.parametrize("role", ["sales", "workshop"])
def test_invoice_and_settlement_reject_non_finance_roles(
    finance_api_app,
    role: str,
) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, role)
        invoice = client.post(
            "/api/finance/invoices",
            json={
                "statement_id": 1,
                "invoice_number": "NO-ACCESS",
                "invoice_date": "2026-06-14",
                "invoice_amount": "1.00",
            },
        )
        settlement = client.put(
            "/api/finance/statements/1/settle",
            json={
                "amount": "1.00",
                "settlement_date": "2026-06-14",
                "account": "NO-ACCESS",
            },
        )

    assert invoice.status_code == 403
    assert settlement.status_code == 403


def test_finance_lists_statements_and_invoice_records(finance_api_app) -> None:
    app, _ = finance_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        statement = _create_statement(client)
        invoice = client.post(
            "/api/finance/invoices",
            json={
                "statement_id": statement["id"],
                "invoice_number": "INV-LIST-001",
                "invoice_date": "2026-06-14",
                "invoice_amount": "100.00",
            },
        )
        assert invoice.status_code == 201, invoice.text
        statements = client.get("/api/finance/statements")
        invoices = client.get("/api/finance/invoices")

    assert statements.status_code == 200, statements.text
    assert statements.json()["items"][0]["statement_number"].startswith("ST-")
    assert statements.json()["items"][0]["customer_name"]
    assert invoices.status_code == 200, invoices.text
    assert invoices.json()["items"][0]["invoice_number"] == "INV-LIST-001"
