from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def customer_charge_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.api.invoice_tasks import customer_router, router as invoice_task_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order
    from app.models.user import User

    monkeypatch.setenv("ERP_INVOICE_EXPORT_DIR", str(tmp_path / "exports"))
    engine = create_sqlite_engine(tmp_path / "customer-charges.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        finance = User(
            username="charge-finance",
            password_hash=hash_password("RolePass123!"),
            role="finance",
            real_name="Finance",
            must_change_password=False,
        )
        sales = User(
            username="charge-sales",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            real_name="Sales",
            must_change_password=False,
        )
        customer = Customer(
            name="匿名收费客户",
            customer_code="CHARGE001",
            is_active=True,
        )
        db.add_all([finance, sales, customer])
        db.flush()
        order = Order(
            order_number="CHARGE-ORDER-001",
            customer_id=customer.id,
            customer_po="CHARGE-PO-001",
            order_date=date(2026, 8, 1),
            delivery_date=date(2026, 8, 30),
            status="pending_confirmation",
            payment_status="unpaid",
            total_amount=Decimal("0"),
        )
        db.add(order)
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(finance_router, prefix="/api/finance")
    app.include_router(invoice_task_router, prefix="/api/finance")
    app.include_router(customer_router, prefix="/api/customers")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    yield app, factory
    engine.dispose()


def _login(client: TestClient, username: str = "charge-finance") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def _charge_payload(
    *,
    name: str,
    amount: str,
    charge_type: str,
    key: str,
) -> dict:
    return {
        "customer_id": 1,
        "order_id": 1,
        "order_item_id": None,
        "mold_tool_id": None,
        "printing_plate_id": None,
        "charge_type": charge_type,
        "display_name": name,
        "quantity": "1",
        "unit": "项",
        "unit_price": amount,
        "amount": amount,
        "price_tax_mode": "tax_inclusive",
        "tax_rate": "0.13",
        "tax_project_name": name,
        "tax_classification_code": "1060502990000000000",
        "note": "P1-90 隔离测试",
        "idempotency_key": key,
    }


def test_customer_charges_share_one_statement_and_enter_fin001(
    customer_charge_app,
) -> None:
    app, factory = customer_charge_app
    with TestClient(app) as client:
        _login(client)
        first_payload = _charge_payload(
            name="本订单模具费",
            amount="500.00",
            charge_type="mold",
            key="charge-create-mold-001",
        )
        first = client.post("/api/finance/customer-charges", json=first_payload)
        assert first.status_code == 201, first.text
        replay = client.post("/api/finance/customer-charges", json=first_payload)
        assert replay.status_code == 201
        assert replay.json()["id"] == first.json()["id"]
        second = client.post(
            "/api/finance/customer-charges",
            json=_charge_payload(
                name="本订单制版费",
                amount="200.00",
                charge_type="printing_plate",
                key="charge-create-plate-001",
            ),
        )
        assert second.status_code == 201, second.text

        for index, charge in enumerate((first.json(), second.json()), start=1):
            confirmed = client.post(
                f"/api/finance/customer-charges/{charge['id']}/confirm",
                json={
                    "expected_version": charge["version"],
                    "reconciliation_month": "2026-08",
                    "idempotency_key": f"charge-confirm-202608-{index}",
                },
            )
            assert confirmed.status_code == 200, confirmed.text

        pending = client.get(
            "/api/finance/pending_statements",
            params={"customer_id": 1, "statement_month": "2026-08"},
        )
        assert pending.status_code == 200, pending.text
        assert pending.json()["deliveries"] == []
        assert len(pending.json()["customer_charges"]) == 2

        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-08",
                "delivery_ids": [],
                "customer_charge_ids": [first.json()["id"], second.json()["id"]],
                "idempotency_key": "charge-statement-202608-001",
            },
        )
        assert statement.status_code == 201, statement.text
        assert Decimal(str(statement.json()["total_receivable"])) == Decimal("700.00")
        detail = client.get(
            f"/api/finance/statements/{statement.json()['id']}"
        )
        assert detail.status_code == 200, detail.text
        assert {item["source_type"] for item in detail.json()["items"]} == {
            "customer_charge"
        }

        locked = client.post(
            f"/api/finance/customer-charges/{first.json()['id']}/cancel-confirmation",
            json={
                "expected_version": 2,
                "idempotency_key": "charge-cancel-confirm-locked",
            },
        )
        assert locked.status_code == 409

        seller = client.post(
            "/api/finance/invoice-sellers",
            json={
                "seller_code": "CHARGE-SELLER",
                "seller_name": "匿名销方",
                "tax_no": "913200000000000001",
                "address": "匿名地址",
                "phone": "0512-00000000",
                "bank_name": "匿名银行",
                "bank_account": "6222000000000000000",
                "confirmation_status": "confirmed",
            },
        )
        assert seller.status_code == 201, seller.text
        rule = client.put(
            "/api/customers/1/invoice-item-rules/default",
            json={
                "project_name": "纸箱",
                "tax_classification_code": "1060105010000000000",
                "unit": "PCS",
                "tax_rate": "0.13",
                "spec_source": "product_code_snapshot",
                "fill_unit_price": True,
                "confirmation_status": "confirmed",
            },
        )
        assert rule.status_code == 200, rule.text
        profile = client.put(
            "/api/customers/1/invoice-profile",
            json={
                "invoice_title": "匿名购方",
                "tax_no": "913200000000000002",
                "invoice_address": "匿名开票地址",
                "invoice_phone": "0512-11111111",
                "bank_name": "匿名银行",
                "bank_account": "6222000000000000001",
                "default_seller_id": seller.json()["id"],
                "price_tax_mode": "tax_inclusive",
                "default_tax_rate": "0.13",
                "confirmation_status": "confirmed",
                "expected_version": 1,
            },
        )
        assert profile.status_code == 200, profile.text
        confirmed_statement = client.post(
            f"/api/finance/statements/{statement.json()['id']}/confirm",
            json={"expected_version": 1},
        )
        assert confirmed_statement.status_code == 200, confirmed_statement.text
        with factory() as db:
            from app.models.customer_charge import CustomerCharge

            charge = db.get(CustomerCharge, first.json()["id"])
            charge.tax_project_name = None
            db.commit()
        blocked_task = client.post(
            f"/api/finance/statements/{statement.json()['id']}/invoice-tasks",
            json={
                "expected_version": 2,
                "idempotency_key": "charge-fin001-missing-tax",
            },
        )
        assert blocked_task.status_code == 409
        assert "本订单模具费" in blocked_task.text
        with factory() as db:
            from app.models.customer_charge import CustomerCharge

            charge = db.get(CustomerCharge, first.json()["id"])
            charge.tax_project_name = "本订单模具费"
            db.commit()
        task = client.post(
            f"/api/finance/statements/{statement.json()['id']}/invoice-tasks",
            json={
                "expected_version": 2,
                "idempotency_key": "charge-fin001-task-001",
            },
        )
        assert task.status_code == 201, task.text
        assert Decimal(str(task.json()["total_amount"])) == Decimal("700.00")
        task_detail = client.get(
            f"/api/finance/invoice-tasks/{task.json()['id']}"
        )
        assert {
            (item["source_type"], item["product_name_snapshot"])
            for item in task_detail.json()["items"]
        } == {
            ("customer_charge", "本订单模具费"),
            ("customer_charge", "本订单制版费"),
        }

        with factory() as db:
            from app.models.customer_charge import CustomerCharge
            from app.models.finance import StatementItem

            assert db.scalar(select(CustomerCharge).where(CustomerCharge.id == 1))
            assert db.scalar(
                select(StatementItem).where(StatementItem.customer_charge_id == 1)
            )


def test_sales_role_cannot_write_customer_charge(customer_charge_app) -> None:
    app, _factory = customer_charge_app
    with TestClient(app) as client:
        _login(client, "charge-sales")
        response = client.post(
            "/api/finance/customer-charges",
            json=_charge_payload(
                name="越权收费",
                amount="100.00",
                charge_type="other",
                key="charge-sales-forbidden-001",
            ),
        )
        assert response.status_code == 403
