from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
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
        db.add(StatementItem(statement_id=statement.id, return_receipt_item_id=receipt_item.id, actual_received_quantity=10, unit_price_snapshot=Decimal("11.30"), unit_cost_snapshot=Decimal("1.00"), receivable_amount=Decimal("113.00"), gross_profit_amount=Decimal("103.00"), price_tax_mode_snapshot="tax_inclusive", tax_rate_snapshot=Decimal("0.13")))
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


def _complete_invoice_profile(
    client: TestClient,
    *,
    price_tax_mode: str = "tax_inclusive",
    unit: str = "个",
    tax_classification_code: str = "109020101",
    spec_source: str = "product_snapshot",
    fill_unit_price: bool = False,
) -> int:
    seller = client.post("/api/finance/invoice-sellers", json={"seller_code":"UAT-SELLER","seller_name":"匿名销方","tax_no":"913200000000000001","address":"匿名地址","phone":"0512-00000000","bank_name":"匿名银行","bank_account":"6222000000000000000","confirmation_status":"confirmed"})
    assert seller.status_code == 201, seller.text
    seller_id = seller.json()["id"]
    rule = client.put("/api/customers/1/invoice-item-rules/default", json={"project_name":"纸箱","tax_classification_code":tax_classification_code,"unit":unit,"tax_rate":"0.13","spec_source":spec_source,"fill_unit_price":fill_unit_price,"confirmation_status":"confirmed"})
    assert rule.status_code == 200, rule.text
    profile = client.put("/api/customers/1/invoice-profile", json={"invoice_title":"匿名购方","tax_no":"913200000000000002","invoice_address":"匿名开票地址","invoice_phone":"0512-11111111","bank_name":"匿名银行","bank_account":"6222000000000000001","default_seller_id":seller_id,"price_tax_mode":price_tax_mode,"default_tax_rate":"0.13","confirmation_status":"confirmed","expected_version":1})
    assert profile.status_code == 200, profile.text
    return seller_id


def test_fin002a_default_rule_contract_uses_pcs_and_full_tax_code(fin001_app) -> None:
    app, _factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        response = client.get("/api/customers/1/invoice-item-rules/default")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "product_id": None,
            "project_name": "纸箱",
            "tax_classification_code": "1060105010000000000",
            "unit": "PCS",
            "tax_rate": "0.13",
            "spec_source": "product_code_snapshot",
            "fill_unit_price": True,
            "effective_from": None,
            "effective_to": None,
            "confirmation_status": "pending",
            "version": 1,
        }


def test_fin002a_profile_change_is_versioned_and_stale_save_is_rejected(
    fin001_app,
) -> None:
    app, _factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        _complete_invoice_profile(client)
        current = client.get("/api/customers/1/invoice-profile").json()
        payload = {
            key: current[key]
            for key in (
                "invoice_title",
                "tax_no",
                "invoice_address",
                "invoice_phone",
                "bank_name",
                "bank_account",
                "default_seller_id",
                "default_tax_rate",
                "is_enabled",
                "confirmation_status",
            )
        }
        payload.update(
            price_tax_mode="tax_exclusive",
            expected_version=current["version"],
        )
        changed = client.put("/api/customers/1/invoice-profile", json=payload)
        assert changed.status_code == 200, changed.text
        assert changed.json()["price_tax_mode"] == "tax_exclusive"
        stale = client.put("/api/customers/1/invoice-profile", json=payload)
        assert stale.status_code == 409, stale.text


def test_fin002a_tax_inclusive_exact_example(fin001_app) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceiptItem, Statement, StatementItem
    from app.models.order import Order, OrderItem

    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        _complete_invoice_profile(
            client,
            price_tax_mode="tax_inclusive",
            unit="PCS",
            tax_classification_code="1060105010000000000",
            spec_source="product_code_snapshot",
            fill_unit_price=True,
        )
        with factory() as db:
            statement = db.get(Statement, 1)
            statement_item = db.scalar(
                select(StatementItem).where(StatementItem.statement_id == 1)
            )
            order = db.get(Order, 1)
            order_item = db.get(OrderItem, 1)
            delivery = db.get(Delivery, 1)
            delivery_item = db.get(DeliveryItem, 1)
            receipt_item = db.get(ReturnReceiptItem, 1)
            assert all(
                (
                    statement,
                    statement_item,
                    order,
                    order_item,
                    delivery,
                    delivery_item,
                    receipt_item,
                )
            )
            statement.total_receivable = Decimal("1130.00")
            statement_item.actual_received_quantity = 100
            statement_item.unit_price_snapshot = Decimal("11.30")
            statement_item.receivable_amount = Decimal("1130.00")
            statement_item.price_tax_mode_snapshot = "tax_inclusive"
            statement_item.tax_rate_snapshot = Decimal("0.13")
            order.total_amount = Decimal("1130.00")
            order_item.quantity = 100
            order_item.delivered_quantity = 100
            order_item.unit_price = Decimal("11.30")
            order_item.subtotal = Decimal("1130.00")
            delivery.total_quantity = 100
            delivery_item.delivered_quantity = 100
            receipt_item.actual_received_quantity = 100
            db.commit()

        assert client.post(
            "/api/finance/statements/1/confirm",
            json={"expected_version": 1},
        ).status_code == 200
        created = client.post(
            "/api/finance/statements/1/invoice-tasks",
            json={
                "expected_version": 2,
                "idempotency_key": "fin002a-inclusive-exact",
            },
        )
        assert created.status_code == 201, created.text
        assert Decimal(str(created.json()["net_amount"])) == Decimal("1000.00")
        assert Decimal(str(created.json()["tax_amount"])) == Decimal("130.00")
        assert Decimal(str(created.json()["total_amount"])) == Decimal("1130.00")
        detail = client.get(
            f"/api/finance/invoice-tasks/{created.json()['id']}"
        ).json()
        assert detail["items"][0]["unit_price"] is None
        assert Decimal(str(detail["items"][0]["amount"])) == Decimal("1130.00")


def test_fin002a_multiline_tax_rounds_each_line_before_totals(fin001_app) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceiptItem, Statement
    from app.models.order import Order, OrderItem

    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        _complete_invoice_profile(
            client,
            price_tax_mode="tax_exclusive",
            unit="PCS",
            tax_classification_code="1060105010000000000",
            spec_source="product_code_snapshot",
            fill_unit_price=True,
        )
        with factory() as db:
            old_statement = db.get(Statement, 1)
            order = db.get(Order, 1)
            first_item = db.get(OrderItem, 1)
            delivery = db.get(Delivery, 1)
            first_delivery_item = db.get(DeliveryItem, 1)
            first_receipt_item = db.get(ReturnReceiptItem, 1)
            assert all(
                (
                    old_statement,
                    order,
                    first_item,
                    delivery,
                    first_delivery_item,
                    first_receipt_item,
                )
            )
            db.delete(old_statement)
            order.total_amount = Decimal("0.10")
            first_item.quantity = 1
            first_item.delivered_quantity = 1
            first_item.unit_price = Decimal("0.05")
            first_item.subtotal = Decimal("0.05")
            first_item.price_tax_mode_snapshot = "tax_exclusive"
            first_item.tax_rate_snapshot = Decimal("0.13")
            first_delivery_item.delivered_quantity = 1
            first_receipt_item.actual_received_quantity = 1
            second_item = OrderItem(
                order_id=order.id,
                product_id=first_item.product_id,
                quantity=1,
                delivered_quantity=1,
                unit_price=Decimal("0.05"),
                subtotal=Decimal("0.05"),
                material_status="received",
                snapshot_product_code="FIN-BOX-001-B",
                snapshot_product_name="匿名纸箱 B",
                snapshot_spec="100×100×100",
                price_tax_mode_snapshot="tax_exclusive",
                tax_rate_snapshot=Decimal("0.13"),
            )
            db.add(second_item)
            db.flush()
            second_delivery_item = DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=second_item.id,
                delivered_quantity=1,
            )
            db.add(second_delivery_item)
            db.flush()
            db.add(
                ReturnReceiptItem(
                    return_receipt_id=first_receipt_item.return_receipt_id,
                    delivery_item_id=second_delivery_item.id,
                    actual_received_quantity=1,
                )
            )
            delivery.total_quantity = 2
            db.commit()

        statement_response = client.post(
            "/api/finance/statements",
            json={
                "customer_id": 1,
                "statement_month": "2026-08",
                "delivery_ids": [1],
            },
        )
        assert statement_response.status_code == 201, statement_response.text
        assert Decimal(str(statement_response.json()["total_receivable"])) == Decimal(
            "0.12"
        )
        statement_id = statement_response.json()["id"]
        assert client.post(
            f"/api/finance/statements/{statement_id}/confirm",
            json={"expected_version": 1},
        ).status_code == 200
        created = client.post(
            f"/api/finance/statements/{statement_id}/invoice-tasks",
            json={
                "expected_version": 2,
                "idempotency_key": "fin002a-multiline-rounding",
            },
        )
        assert created.status_code == 201, created.text
        assert Decimal(str(created.json()["net_amount"])) == Decimal("0.10")
        assert Decimal(str(created.json()["tax_amount"])) == Decimal("0.02")
        assert Decimal(str(created.json()["total_amount"])) == Decimal("0.12")


def test_fin002a_legacy_rows_without_snapshot_fall_back_to_current_profile(
    fin001_app,
) -> None:
    from app.models.finance import Statement, StatementItem
    from app.models.order import OrderItem

    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        _complete_invoice_profile(
            client,
            price_tax_mode="tax_exclusive",
            unit="PCS",
            tax_classification_code="1060105010000000000",
            spec_source="product_code_snapshot",
            fill_unit_price=True,
        )
        with factory() as db:
            statement = db.get(Statement, 1)
            statement_item = db.scalar(
                select(StatementItem).where(StatementItem.statement_id == 1)
            )
            order_item = db.get(OrderItem, 1)
            assert statement is not None and statement_item is not None
            assert order_item is not None
            statement.total_receivable = Decimal("127.69")
            statement_item.receivable_amount = Decimal("127.69")
            statement_item.price_tax_mode_snapshot = None
            statement_item.tax_rate_snapshot = None
            order_item.price_tax_mode_snapshot = None
            order_item.tax_rate_snapshot = None
            db.commit()

        confirmed = client.post(
            "/api/finance/statements/1/confirm",
            json={"expected_version": 1},
        )
        assert confirmed.status_code == 200, confirmed.text
        created = client.post(
            "/api/finance/statements/1/invoice-tasks",
            json={
                "expected_version": 2,
                "idempotency_key": "fin002a-legacy-profile-fallback",
            },
        )
        assert created.status_code == 201, created.text
        assert Decimal(str(created.json()["net_amount"])) == Decimal("113.00")
        assert Decimal(str(created.json()["tax_amount"])) == Decimal("14.69")
        assert Decimal(str(created.json()["total_amount"])) == Decimal("127.69")
        assert created.json()["price_tax_mode"] == "tax_exclusive"


def test_fin002a_tax_exclusive_statement_task_and_export_are_frozen(fin001_app) -> None:
    import hashlib

    from app.api.invoice_tasks import TAX_TEMPLATE_PATH
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceiptItem, Statement, StatementItem
    from app.models.invoice_task import FinanceInvoiceTask
    from app.models.order import Order, OrderItem

    app, factory = fin001_app
    source_template_hash = hashlib.sha256(TAX_TEMPLATE_PATH.read_bytes()).hexdigest()
    with TestClient(app) as client:
        _login(client)
        _complete_invoice_profile(
            client,
            price_tax_mode="tax_exclusive",
            unit="PCS",
            tax_classification_code="1060105010000000000",
            spec_source="product_code_snapshot",
            fill_unit_price=True,
        )

        with factory() as db:
            old_statement = db.get(Statement, 1)
            assert old_statement is not None
            db.delete(old_statement)
            order = db.get(Order, 1)
            order_item = db.get(OrderItem, 1)
            delivery = db.get(Delivery, 1)
            delivery_item = db.get(DeliveryItem, 1)
            receipt_item = db.get(ReturnReceiptItem, 1)
            assert all((order, order_item, delivery, delivery_item, receipt_item))
            order.total_amount = Decimal("1000.00")
            order_item.quantity = 100
            order_item.delivered_quantity = 100
            order_item.unit_price = Decimal("10.00")
            order_item.subtotal = Decimal("1000.00")
            order_item.price_tax_mode_snapshot = "tax_exclusive"
            order_item.tax_rate_snapshot = Decimal("0.13")
            delivery.total_quantity = 100
            delivery_item.delivered_quantity = 100
            receipt_item.actual_received_quantity = 100
            db.commit()

        statement_response = client.post(
            "/api/finance/statements",
            json={"customer_id": 1, "statement_month": "2026-08", "delivery_ids": [1]},
        )
        assert statement_response.status_code == 201, statement_response.text
        statement_id = statement_response.json()["id"]
        assert Decimal(str(statement_response.json()["total_receivable"])) == Decimal("1130.00")

        with factory() as db:
            statement_item = db.scalar(
                select(StatementItem).where(StatementItem.statement_id == statement_id)
            )
            assert statement_item is not None
            assert statement_item.price_tax_mode_snapshot == "tax_exclusive"
            assert Decimal(str(statement_item.tax_rate_snapshot)) == Decimal("0.1300")
            assert Decimal(str(statement_item.receivable_amount)) == Decimal("1130.00")

        confirmed = client.post(
            f"/api/finance/statements/{statement_id}/confirm",
            json={"expected_version": 1},
        )
        assert confirmed.status_code == 200, confirmed.text
        created = client.post(
            f"/api/finance/statements/{statement_id}/invoice-tasks",
            json={"expected_version": 2, "idempotency_key": "fin002a-exclusive-task"},
        )
        assert created.status_code == 201, created.text
        task = created.json()
        assert Decimal(str(task["net_amount"])) == Decimal("1000.00")
        assert Decimal(str(task["tax_amount"])) == Decimal("130.00")
        assert Decimal(str(task["total_amount"])) == Decimal("1130.00")
        assert task["price_tax_mode"] == "tax_exclusive"

        detail = client.get(f"/api/finance/invoice-tasks/{task['id']}")
        assert detail.status_code == 200, detail.text
        line = detail.json()["items"][0]
        assert line["specification"] == "FIN-BOX-001"
        assert line["unit"] == "PCS"
        assert Decimal(str(line["quantity"])) == Decimal("100")
        assert Decimal(str(line["unit_price"])) == Decimal("10.00")
        assert Decimal(str(line["amount"])) == Decimal("1000.00")
        assert line["tax_category_code"] == "1060105010000000000"

        ready = client.post(
            f"/api/finance/invoice-tasks/{task['id']}/confirm",
            json={"expected_version": 1},
        )
        assert ready.status_code == 200, ready.text

        current_profile = client.get("/api/customers/1/invoice-profile").json()
        changed_profile = client.put(
            "/api/customers/1/invoice-profile",
            json={
                **{key: current_profile[key] for key in (
                    "invoice_title", "tax_no", "invoice_address", "invoice_phone",
                    "bank_name", "bank_account", "default_seller_id",
                    "default_tax_rate", "is_enabled", "confirmation_status",
                )},
                "price_tax_mode": "tax_inclusive",
                "expected_version": current_profile["version"],
            },
        )
        assert changed_profile.status_code == 200, changed_profile.text

        exported = client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx")
        assert exported.status_code == 200, exported.text
        workbook = load_workbook(BytesIO(exported.content), read_only=False, data_only=False)
        try:
            assert workbook.sheetnames == ["1-明细模板", "excelVersion", "xzqhdm", "2-特定业务信息"]
            sheet = workbook["1-明细模板"]
            assert sheet["B4"].value == "1060105010000000000"
            assert sheet["C4"].value == "FIN-BOX-001"
            assert sheet["D4"].value == "PCS"
            assert Decimal(str(sheet["E4"].value)) == Decimal("100")
            assert Decimal(str(sheet["F4"].value)) == Decimal("10.00")
            assert sheet["F4"].number_format in {"0.00", "#,##0.00"}
            assert Decimal(str(sheet["G4"].value)) == Decimal("1000.00")
            assert Decimal(str(sheet["H4"].value)) == Decimal("0.13")
        finally:
            workbook.close()
        assert hashlib.sha256(TAX_TEMPLATE_PATH.read_bytes()).hexdigest() == source_template_hash

        with factory() as db:
            frozen = db.get(FinanceInvoiceTask, task["id"])
            assert frozen is not None
            assert frozen.price_tax_mode == "tax_exclusive"
            assert Decimal(str(frozen.total_amount)) == Decimal("1130.00")

        issued = client.post(
            f"/api/finance/invoice-tasks/{task['id']}/result",
            json={
                "status": "issued",
                "invoice_number": "FIN002A-UAT-001",
                "invoice_date": "2026-08-27",
                "expected_version": 2,
            },
        )
        assert issued.status_code == 200, issued.text
        assert issued.json()["status"] == "issued"
        with factory() as db:
            statement = db.get(Statement, statement_id)
            assert statement is not None
            assert Decimal(str(statement.invoiced_amount)) == Decimal("1130.00")


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
        same_key = client.post("/api/finance/statements/1/invoice-tasks", json={"expected_version":2,"idempotency_key":"fin001-create-key"})
        assert same_key.status_code == 201, same_key.text
        assert same_key.json()["id"] == task["id"]
        conflicting_payload = client.post("/api/finance/statements/1/invoice-tasks", json={"expected_version":2,"idempotency_key":"fin001-create-key","seller_entity_id":999999})
        assert conflicting_payload.status_code == 409, conflicting_payload.text
        assert "幂等键" in str(conflicting_payload.json())
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
