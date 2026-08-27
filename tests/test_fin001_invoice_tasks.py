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
def fin001_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.api.invoice_tasks import customer_router, router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt, ReturnReceiptItem, Statement, StatementItem
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    monkeypatch.setenv("ERP_INVOICE_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("ERP_INVOICE_ATTACHMENT_DIR", str(tmp_path / "attachments"))
    engine = create_sqlite_engine(tmp_path / "fin001.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(username="fin001-admin", password_hash=hash_password("RolePass123!"), role="admin", real_name="Admin", must_change_password=False)
        finance = User(username="fin001-finance", password_hash=hash_password("RolePass123!"), role="finance", real_name="Finance", must_change_password=False)
        sales = User(username="fin001-sales", password_hash=hash_password("RolePass123!"), role="sales", real_name="Sales", must_change_password=False)
        customer = Customer(name="匿名开票客户", customer_code="FIN001", is_active=True)
        db.add_all([admin, finance, sales, customer])
        db.flush()
        product = Product(customer_id=customer.id, product_code="FIN-BOX-001", customer_material_code="FIN001-MAT", product_name="匿名纸箱", box_category="normal", cost_unit_price=Decimal("1.00"))
        db.add(product)
        db.flush()
        order = Order(order_number="FIN001-ORDER", customer_id=customer.id, customer_po="FIN001-PO", order_date=date(2026, 8, 1), delivery_date=date(2026, 8, 2), status="pending_reconciliation", payment_status="unpaid", total_amount=Decimal("113.00"))
        db.add(order)
        db.flush()
        order_item = OrderItem(order_id=order.id, product_id=product.id, quantity=10, delivered_quantity=10, unit_price=Decimal("11.30"), subtotal=Decimal("113.00"), material_status="received", snapshot_product_code="FIN-BOX-001", snapshot_product_name="匿名纸箱", snapshot_spec="300×200×100")
        db.add(order_item)
        db.flush()
        delivery = Delivery(delivery_number="FIN001-D", customer_id=customer.id, delivery_date=date(2026, 8, 2), status="dispatched", total_quantity=10)
        db.add(delivery)
        db.flush()
        delivery_item = DeliveryItem(delivery_id=delivery.id, order_item_id=order_item.id, delivered_quantity=10)
        db.add(delivery_item)
        db.flush()
        receipt = ReturnReceipt(delivery_id=delivery.id, actual_received_date=date(2026, 8, 2), status="confirmed")
        db.add(receipt)
        db.flush()
        receipt_item = ReturnReceiptItem(return_receipt_id=receipt.id, delivery_item_id=delivery_item.id, actual_received_quantity=10)
        db.add(receipt_item)
        db.flush()
        statement = Statement(statement_number="ST-FIN001", customer_id=customer.id, statement_month="2026-08", total_receivable=Decimal("113.00"), total_gross_profit=Decimal("13.00"), status="unsettled")
        db.add(statement)
        db.flush()
        db.add(StatementItem(statement_id=statement.id, return_receipt_item_id=receipt_item.id, actual_received_quantity=10, unit_price_snapshot=Decimal("11.30"), unit_cost_snapshot=Decimal("1.00"), receivable_amount=Decimal("113.00"), gross_profit_amount=Decimal("103.00")))
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(finance_router, prefix="/api/finance")
    app.include_router(router, prefix="/api/finance")
    app.include_router(customer_router, prefix="/api/customers")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, factory


def _login(client: TestClient, username: str = "fin001-finance") -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "RolePass123!"})
    assert response.status_code == 200, response.text


def _complete_invoice_profile(client: TestClient) -> int:
    seller = client.post("/api/finance/invoice-sellers", json={"seller_code":"UAT-SELLER","seller_name":"匿名销方","tax_no":"913200000000000001","address":"匿名地址","phone":"0512-00000000","bank_name":"匿名银行","bank_account":"6222000000000000000","confirmation_status":"confirmed"})
    assert seller.status_code == 201, seller.text
    seller_id = seller.json()["id"]
    rule = client.put("/api/customers/1/invoice-item-rules/default", json={"project_name":"纸箱","tax_classification_code":"109020101","unit":"个","tax_rate":"0.13","confirmation_status":"confirmed"})
    assert rule.status_code == 200, rule.text
    profile = client.put("/api/customers/1/invoice-profile", json={"invoice_title":"匿名购方","tax_no":"913200000000000002","invoice_address":"匿名开票地址","invoice_phone":"0512-11111111","bank_name":"匿名银行","bank_account":"6222000000000000001","default_seller_id":seller_id,"price_tax_mode":"tax_inclusive","default_tax_rate":"0.13","confirmation_status":"confirmed","expected_version":1})
    assert profile.status_code == 200, profile.text
    return seller_id


def test_fin001_missing_data_fails_closed_then_freezes_exports_and_registers(fin001_app) -> None:
    from app.models.finance import Invoice, Statement
    from app.models.invoice_task import FinanceInvoiceAttachment, FinanceInvoiceTask

    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        confirm = client.post("/api/finance/statements/1/confirm", json={"expected_version":1})
        assert confirm.status_code == 200, confirm.text
        missing = client.post("/api/finance/statements/1/invoice-tasks", json={"expected_version":2,"idempotency_key":"fin001-missing-key"})
        assert missing.status_code == 409
        assert "缺少" in str(missing.json()) or "资料" in str(missing.json())
        _complete_invoice_profile(client)
        created = client.post("/api/finance/statements/1/invoice-tasks", json={"expected_version":2,"idempotency_key":"fin001-create-key"})
        assert created.status_code == 201, created.text
        task = created.json()
        repeated = client.post("/api/finance/statements/1/invoice-tasks", json={"expected_version":2,"idempotency_key":"fin001-second-key"})
        assert repeated.status_code == 201
        assert repeated.json()["id"] == task["id"]
        detail = client.get(f"/api/finance/invoice-tasks/{task['id']}")
        assert detail.status_code == 200
        assert Decimal(str(detail.json()["items"][0]["amount"])) == Decimal("113.00")
        assert detail.json()["items"][0]["tax_category_code"] == "109020101"
        ready = client.post(f"/api/finance/invoice-tasks/{task['id']}/confirm", json={"expected_version":1})
        assert ready.status_code == 200, ready.text
        exported = client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx")
        assert exported.status_code == 200, exported.text
        assert exported.content[:2] == b"PK"
        issued = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json={"status":"issued","invoice_number":"FIN001-INV-001","invoice_date":"2026-08-03","expected_version":2})
        assert issued.status_code == 200, issued.text
        uploaded = client.post(f"/api/finance/invoice-tasks/{task['id']}/attachments/original", files={"file":("source.pdf", b"%PDF-1.4\nfin001\n%%EOF", "application/pdf")})
        assert uploaded.status_code == 201, uploaded.text
        attachment = client.get(f"/api/finance/invoice-tasks/{task['id']}").json()["attachments"]
        assert {item["attachment_type"] for item in attachment} == {"original", "organized"}

    with factory() as db:
        task = db.scalar(select(FinanceInvoiceTask))
        statement = db.get(Statement, 1)
        invoice = db.scalar(select(Invoice).where(Invoice.invoice_task_id == task.id))
        assert task.status == "issued"
        assert Decimal(str(statement.invoiced_amount)) == Decimal("113.00")
        assert invoice is not None and invoice.source == "invoice_task"
        assert db.scalar(select(FinanceInvoiceAttachment)) is not None


def test_fin001_permissions_and_failed_result_do_not_increase_invoice_amount(fin001_app) -> None:
    from app.models.finance import Statement

    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client, "fin001-sales")
        assert client.get("/api/finance/invoice-tasks").status_code == 403
    with TestClient(app) as client:
        _login(client)
        assert client.post("/api/finance/statements/1/confirm", json={"expected_version":1}).status_code == 200
        _complete_invoice_profile(client)
        task = client.post("/api/finance/statements/1/invoice-tasks", json={"expected_version":2,"idempotency_key":"fin001-failed-key"}).json()
        assert client.post(f"/api/finance/invoice-tasks/{task['id']}/confirm", json={"expected_version":1}).status_code == 200
        assert client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx").status_code == 200
        failed = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json={"status":"failed","failure_reason":"匿名税局校验失败","expected_version":2})
        assert failed.status_code == 200
        assert failed.json()["status"] == "failed"
    with factory() as db:
        assert Decimal(str(db.get(Statement, 1).invoiced_amount)) == Decimal("0.00")


def test_fin001_confirmed_statement_allows_manual_invoice_and_full_settlement(
    fin001_app,
) -> None:
    """Boss rule: confirmed statements still accept audited manual invoices."""

    from app.models.finance import Invoice, SettlementRecord, Statement

    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        confirmed = client.post(
            "/api/finance/statements/1/confirm",
            json={"expected_version": 1},
        )
        assert confirmed.status_code == 200, confirmed.text
        manual = client.post(
            "/api/finance/invoices",
            json={
                "statement_id": 1,
                "invoice_number": "FIN001-MANUAL-ISSUED",
                "invoice_date": "2026-08-03",
                "invoice_amount": "113.00",
            },
        )
        assert manual.status_code == 201, manual.text
        assert Decimal(str(manual.json()["invoiced_amount"])) == Decimal("113.00")

        current = client.get(
            "/api/finance/current-customer-months",
            params={"statement_month": "2026-08", "page": 1, "page_size": 25},
        )
        assert current.status_code == 200, current.text
        current_statement = current.json()["items"][0]["statements"][0]
        assert current_statement["confirmation_status"] == "confirmed"
        assert current_statement["version"] == 2
        assert current_statement["invoice_status"] == "invoiced"
        assert Decimal(str(current_statement["pending_invoice_amount"])) == Decimal("0.00")

        settled = client.put(
            "/api/finance/statements/1/settle",
            json={
                "amount": "113.00",
                "settlement_date": "2026-08-05",
                "account": "测试银行",
            },
        )
        assert settled.status_code == 200, settled.text
        assert settled.json()["status"] == "settled"

    with factory() as db:
        statement = db.get(Statement, 1)
        assert statement is not None
        assert statement.confirmation_status == "confirmed"
        assert statement.status == "settled"
        assert db.scalar(select(Invoice).where(Invoice.statement_id == 1)) is not None
        assert db.scalar(
            select(SettlementRecord).where(SettlementRecord.statement_id == 1)
        ) is not None
