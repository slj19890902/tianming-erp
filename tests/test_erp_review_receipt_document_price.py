from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


def seed_document_case(factory, suffix: str = "") -> dict[str, int]:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.material import Material
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.supplier import Supplier
    from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    with factory() as db:
        supplier_name, material_code = f"凭据纸板厂{suffix}", f"DOC-BC{suffix}"
        user = User(username=f"document-price-admin{suffix}", password_hash="not-used", role="admin",
                    real_name="Document verifier", must_change_password=False)
        supplier = Supplier(standard_name=supplier_name, normalized_name=normalize_supplier_identity(supplier_name),
                            display_name=supplier_name, is_active=True)
        # Deliberately no current quote or tax contract: the price must come from the document.
        material = Material(code=material_code, supplier_name=supplier_name, quote_price=None,
                            price_unit=None, is_active=True, version=3)
        db.add_all([user, supplier, material])
        db.flush()
        customer = Customer(name=f"凭据核价测试客户{suffix}", is_active=True)
        db.add(customer)
        db.flush()
        product = Product(customer_id=customer.id, product_code=f"DOC-P1{suffix}", customer_material_code=f"DOC-P1{suffix}",
                          product_name="凭据测试箱", length_mm=200, width_mm=100, height_mm=100)
        sales_order = Order(order_number=f"SO-DOCUMENT-1{suffix}", customer_id=customer.id,
                            order_date=date(2026, 8, 1), delivery_date=date(2026, 8, 10),
                            status="pending_production", payment_status="unpaid", total_amount=10)
        db.add_all([product, sales_order])
        db.flush()
        sales_item = OrderItem(order_id=sales_order.id, product_id=product.id, quantity=10,
                              unit_price=1, subtotal=10, snapshot_product_name="凭据测试箱")
        db.add(sales_item)
        db.flush()
        order = SupplierRequisitionOrder(order_number=f"PO-DOCUMENT-1{suffix}", supplier_name=supplier_name,
                                         status="confirmed", total_quantity=10, requisition_qty=10,
                                         created_by=user.id)
        db.add(order)
        db.flush()
        source = SupplierRequisitionOrderItem(supplier_order_id=order.id, material_id=material.id,
                    order_item_id=sales_item.id,
                    material_code_snapshot=material_code, supplier_name_snapshot=supplier_name,
                    report_length_mm=1000, report_width_mm=500, quantity=10, requisition_qty=10)
        receipt = IncomingReceipt(receipt_number=f"IR-DOCUMENT-1{suffix}", status="posted",
                    received_at=datetime(2026, 8, 5, 3), received_by=user.id,
                    idempotency_key=f"document-test-receipt{suffix}")
        db.add_all([source, receipt])
        db.flush()
        item = IncomingReceiptItem(receipt_id=receipt.id, supplier_order_id=order.id,
                    order_id=sales_order.id, order_item_id=sales_item.id,
                    supplier_order_item_id=source.id, planned_quantity=10, received_quantity=10,
                    cumulative_received_quantity=10, variance_quantity=0, variance_type="matched",
                    resolution_status="not_required", status="posted")
        db.add(item)
        db.commit()
        return {"user_id": user.id, "supplier_id": supplier.id, "material_id": material.id,
                "receipt_item_id": item.id, "receipt_id": receipt.id, "source_id": source.id}


@pytest.fixture()
def document_price_app(tmp_path):
    from app.api.deps import get_current_user, get_db
    from app.api.supplier_settlements import router
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "receipt-document-price.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    fixture = seed_document_case(factory)
    app = FastAPI()
    app.include_router(router, prefix="/api/finance")

    def database_override():
        with factory() as db:
            yield db

    def user_override():
        with factory() as db:
            yield db.get(User, fixture["user_id"])

    app.dependency_overrides[get_db] = database_override
    app.dependency_overrides[get_current_user] = user_override
    yield app, factory, fixture
    engine.dispose()


def prepare(client, fixture):
    url = f"/api/finance/supplier-settlements/receipt-price-confirmations/{fixture['receipt_item_id']}"
    response = client.get(url)
    assert response.status_code == 200, response.json()
    payload = {"evidence_reference": "供应商账单B202608第3行", "unit_price": "2.80",
               "price_unit": "per_square_meter", "document_amount": "14.00", "currency": "CNY",
               "tax_included": True, "tax_rate": "0.13", "shipping_fee_mode": "included",
               "expected_source_hash": response.json()["source_hash"]}
    return url, payload


def confirmed_payload(client, fixture):
    url, payload = prepare(client, fixture)
    preview = client.post(url + "/preview", json=payload)
    assert preview.status_code == 200, preview.json()
    return url, {**payload, "expected_plan_hash": preview.json()["plan_hash"],
                 "idempotency_key": "document-price-confirm-test"}


def assert_no_price_writes(factory):
    from app.models.finance import FinanceIdempotencyRecord
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    with factory() as db:
        assert db.scalar(select(func.count(SupplierReceiptSettlementPriceFact.id))) == 0
        assert db.scalar(select(func.count(FinanceIdempotencyRecord.id))) == 0


def test_document_price_preview_confirm_replay_and_monthly_amount(document_price_app):
    from app.models.material import Material
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    app, factory, fixture = document_price_app
    with TestClient(app) as client:
        url, payload = confirmed_payload(client, fixture)
        assert_no_price_writes(factory)
        result = client.post(url + "/confirm", json=payload)
        assert result.status_code == 201, result.json()
        assert Decimal(result.json()["erp_amount"]) == Decimal("14.00")
        assert Decimal(result.json()["tax_amount"]) == Decimal("1.61")
        replay = client.post(url + "/confirm", json=payload)
        assert replay.status_code == 201
        assert replay.json() == result.json()
        assert client.get(url).status_code == 409
        other_key = {**payload, "idempotency_key": "document-price-second-key"}
        assert client.post(url + "/confirm", json=other_key).status_code == 409
        with factory() as db:
            fact = db.scalar(select(SupplierReceiptSettlementPriceFact))
            assert fact.fact_origin == "historical_document_confirmation"
            assert fact.adoption_evidence_reference == payload["evidence_reference"]
            assert fact.created_by == fixture["user_id"]
            assert fact.source_material_version == 3
            assert fact.unit_price == Decimal("2.800000")
            assert db.get(SupplierRequisitionOrderItem, fixture["source_id"]).purpose_contract_status == "legacy_unset"
            db.get(Material, fixture["material_id"]).quote_price = Decimal("99")
            db.commit()
        monthly = client.post("/api/finance/supplier-settlements/generate", json={
            "settlement_month": "2026-08", "idempotency_key": "document-price-monthly-test"})
        assert monthly.status_code == 200, monthly.json()
        assert Decimal(monthly.json()["items"][0]["erp_amount"]) == Decimal("14.00")


@pytest.mark.parametrize("change", [
    {"evidence_reference": " "}, {"unit_price": "0"}, {"document_amount": "14.01"},
    {"price_unit": "per_ton"}, {"currency": "USD"}, {"tax_rate": None},
    {"tax_rate": "0.09"}, {"tax_included": False}, {"tax_included": "true"},
    {"shipping_fee_mode": "excluded"},
])
def test_invalid_document_or_contract_never_writes(document_price_app, change):
    app, factory, fixture = document_price_app
    with TestClient(app) as client:
        url, payload = prepare(client, fixture)
        response = client.post(url + "/preview", json={**payload, **change})
        assert response.status_code == 422
        response = client.post(url + "/confirm", json={**payload, **change,
            "expected_plan_hash": "a" * 64, "idempotency_key": "document-invalid-confirm"})
        assert response.status_code == 422
    assert_no_price_writes(factory)


def test_contract_values_are_required_not_defaulted(document_price_app):
    app, factory, fixture = document_price_app
    with TestClient(app) as client:
        url, payload = prepare(client, fixture)
        for field in ("currency", "tax_included", "tax_rate", "shipping_fee_mode"):
            partial = {key: value for key, value in payload.items() if key != field}
            assert client.post(url + "/preview", json=partial).status_code == 422
    assert_no_price_writes(factory)


def test_per_sheet_price_uses_received_sheet_quantity(document_price_app):
    app, _factory, fixture = document_price_app
    with TestClient(app) as client:
        url, payload = prepare(client, fixture)
        payload.update(price_unit="per_sheet", document_amount="28.00")
        preview = client.post(url + "/preview", json=payload)
        assert preview.status_code == 200
        result = client.post(url + "/confirm", json={**payload,
            "expected_plan_hash": preview.json()["plan_hash"], "idempotency_key": "document-per-sheet"})
        assert result.status_code == 201
        assert Decimal(result.json()["erp_amount"]) == Decimal("28.00")


@pytest.mark.parametrize("changed", ["receipt", "source_version", "price", "period", "permission"])
def test_confirmation_rechecks_sources_period_permissions_and_hash(document_price_app, changed):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
    from app.models.supplier_settlement import SupplierMonthlyStatement
    from app.models.user import User
    app, factory, fixture = document_price_app
    with TestClient(app) as client:
        url, payload = confirmed_payload(client, fixture)
        with factory() as db:
            if changed == "receipt":
                db.get(IncomingReceiptItem, fixture["receipt_item_id"]).received_quantity = 9
            elif changed == "source_version":
                db.get(SupplierRequisitionOrderItem, fixture["source_id"]).version += 1
            elif changed == "price":
                payload.update(unit_price="4.00", document_amount="20.00")
            elif changed == "permission":
                user = db.get(User, fixture["user_id"])
                user.role, user.customer_access_mode = "finance", "selected"
            elif changed == "period":
                db.add(SupplierMonthlyStatement(statement_number="CLOSED-DOCUMENT-PERIOD",
                    supplier_id=fixture["supplier_id"], supplier_name_snapshot="凭据纸板厂",
                    settlement_month="2026-08", period_start=date(2026, 7, 21), period_end=date(2026, 8, 20),
                    currency="CNY", tax_basis="tax_inclusive", status="confirmed_pending_invoice",
                    erp_amount=14, adjusted_amount=14, confirmed_amount=14, generated_by=fixture["user_id"]))
            db.commit()
        result = client.post(url + "/confirm", json=payload)
        assert result.status_code == (403 if changed == "permission" else 409), result.json()
    assert_no_price_writes(factory)


def test_post_write_audit_failure_rolls_back_fact_and_idempotency(document_price_app, monkeypatch):
    import app.api.supplier_settlements as api
    app, factory, fixture = document_price_app
    with TestClient(app, raise_server_exceptions=False) as client:
        url, payload = confirmed_payload(client, fixture)
        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("test audit failure")
        monkeypatch.setattr(api, "_audit", fail_audit)
        assert client.post(url + "/confirm", json=payload).status_code == 500
    assert_no_price_writes(factory)


def test_concurrent_same_key_returns_one_fact_and_same_response(document_price_app):
    from app.models.finance import FinanceIdempotencyRecord
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    app, factory, fixture = document_price_app
    with TestClient(app) as client:
        url, payload = confirmed_payload(client, fixture)
    def confirm():
        with TestClient(app) as client:
            return client.post(url + "/confirm", json=payload)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: confirm(), range(2)))
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    with factory() as db:
        assert db.scalar(select(func.count(SupplierReceiptSettlementPriceFact.id))) == 1
        assert db.scalar(select(func.count(FinanceIdempotencyRecord.id))) == 1
