from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200


def _app_with_customer_quote_preferences(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.customers import router as customers_router
    from app.api.quotations import router as quotations_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.supplier_flute_price_rule import SupplierFlutePriceRule
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "customer-quote-preferences.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="quote-admin",
            password_hash=hash_password("QuotePass123!"),
            role="admin",
            real_name="报价管理员",
            must_change_password=False,
        )
        scoped = User(
            username="quote-scoped",
            password_hash=hash_password("QuoteScope123!"),
            role="sales",
            real_name="报价文员",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer = Customer(customer_number=1, customer_code="QP", name="报价客户")
        other = Customer(customer_number=2, customer_code="QO", name="其他客户")
        material = Material(
            code="QP-A",
            supplier_name="测试供应商",
            layer_count=3,
            quote_price=Decimal("2.0000"),
            is_active=True,
        )
        db.add_all(
            [
                admin,
                scoped,
                customer,
                other,
                material,
                SupplierFlutePriceRule(
                    supplier_name="测试供应商",
                    layer_count=3,
                    flute_type="A",
                    price_delta=Decimal("0.1000"),
                    is_active=True,
                ),
            ]
        )
        db.flush()
        db.add(UserCustomerScope(user_id=scoped.id, customer_id=customer.id))
        db.commit()
        ids = {"customer": customer.id, "other": other.id, "material": material.id}

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(customers_router, prefix="/api/customers")
    app.include_router(quotations_router, prefix="/api/quotations")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, ids, factory, engine


def test_customer_quote_preference_crud_estimate_and_audit(tmp_path) -> None:
    from app.models.audit import OperationLog

    app, ids, factory, engine = _app_with_customer_quote_preferences(tmp_path)
    try:
        with TestClient(app) as client:
            _login(client, "quote-admin", "QuotePass123!")
            created = client.post(
                f"/api/customers/{ids['customer']}/quote-preferences",
                json={
                    "box_type": "a1/0201",
                    "material_id": ids["material"],
                    "flute_type": "a",
                    "tax_included_square_price": "3.2500",
                },
            )
            assert created.status_code == 201, created.text
            row = created.json()
            assert row["box_type"] == "A1"
            assert row["flute_type"] == "A"
            assert row["tax_included_square_price"] == "3.2500"
            assert row["layer_count"] == 3
            assert row["material_display"] == "QP-A / 测试供应商"

            preference_estimate = client.post(
                f"/api/customers/{ids['customer']}/quote-preferences/estimate",
                json={
                    "box_type": "a1/0201",
                    "material_id": ids["material"],
                    "flute_type": "a",
                    "length_mm": 100,
                    "width_mm": 200,
                    "height_mm": 300,
                },
            )
            assert preference_estimate.status_code == 200, preference_estimate.text
            assert preference_estimate.json()["price_source"] == "customer_preference"
            assert preference_estimate.json()["estimated_unit_price"] == "1.33"
            assert preference_estimate.json()["final_unit_price"] == "1.33"
            assert preference_estimate.json()["material_effective_square_price"] == "2.1"

            quotation_preview = client.post(
                "/api/quotations/preview",
                json={
                    "customer_id": ids["customer"],
                    "box_type": "A1/0201",
                    "material_id": ids["material"],
                    "flute_type": "A",
                    "length_mm": 100,
                    "width_mm": 200,
                    "height_mm": 300,
                },
            )
            assert quotation_preview.status_code == 200, quotation_preview.text
            assert quotation_preview.json()["suggested_unit_price"] == "1.33"
            assert quotation_preview.json()["estimated_unit_cost"] == "0.8618"
            assert quotation_preview.json()["price_source"] == "customer_preference"

            quotation = client.post(
                f"/api/quotations?customer_id={ids['customer']}",
                json={
                    "items": [
                        {
                            "product_name": "客户尺寸报价测试",
                            "box_type": "A1/0201",
                            "material_id": ids["material"],
                            "flute_type": "A",
                            "length_mm": 100,
                            "width_mm": 200,
                            "height_mm": 300,
                            "quantity": 2,
                        }
                    ]
                },
            )
            assert quotation.status_code == 201, quotation.text
            assert quotation.json()["items"][0]["final_unit_price"] == "1.3300"

            updated = client.put(
                f"/api/customers/{ids['customer']}/quote-preferences/{row['id']}",
                json={
                    "tax_included_square_price": "3.5000",
                    "is_active": True,
                    "expected_version": 1,
                },
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["version"] == 2

            manual = client.post(
                f"/api/customers/{ids['customer']}/quote-preferences/estimate",
                json={
                    "box_type": "A1/0201",
                    "material_id": ids["material"],
                    "flute_type": "A",
                    "length_mm": 100,
                    "width_mm": 200,
                    "height_mm": 300,
                    "manual_unit_price": "9.876",
                },
            )
            assert manual.status_code == 200, manual.text
            assert manual.json()["estimated_unit_price"] == "1.44"
            assert manual.json()["final_unit_price"] == "9.88"
            assert manual.json()["final_price_source"] == "manual_unit_price"

        with factory() as db:
            changes = db.scalars(
                select(OperationLog).where(
                    OperationLog.resource == "CustomerQuotePreference"
                )
            ).all()
            assert [change.action for change in changes] == ["CREATE", "UPDATE"]
            assert "修改客户尺寸报价偏好" in changes[-1].details
    finally:
        engine.dispose()


def test_default_uses_effective_material_price_and_scope_is_enforced(tmp_path) -> None:
    app, ids, _factory, engine = _app_with_customer_quote_preferences(tmp_path)
    try:
        with TestClient(app) as client:
            _login(client, "quote-scoped", "QuoteScope123!")
            estimate = client.post(
                f"/api/customers/{ids['customer']}/quote-preferences/estimate",
                json={
                    "box_type": "A1",
                    "material_id": ids["material"],
                    "flute_type": "A",
                    "length_mm": 100,
                    "width_mm": 200,
                    "height_mm": 300,
                },
            )
            assert estimate.status_code == 200, estimate.text
            body = estimate.json()
            assert body["material_effective_square_price"] is None
            assert body["customer_square_price"] == "2.7300"
            assert body["estimated_unit_price"] == "1.12"
            assert body["preference_id"] is None

            denied = client.get(
                f"/api/customers/{ids['other']}/quote-preferences"
            )
            assert denied.status_code == 403

            unsupported = client.post(
                f"/api/customers/{ids['customer']}/quote-preferences/estimate",
                json={
                    "box_type": "A3",
                    "material_id": ids["material"],
                    "flute_type": "A",
                    "length_mm": 100,
                    "width_mm": 200,
                    "height_mm": 300,
                },
            )
            assert unsupported.status_code == 400
    finally:
        engine.dispose()


def test_migration_downgrade_is_fail_closed_when_preference_facts_exist() -> None:
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "alembic/versions/cv78v8x9z67_customer_quote_preferences.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision: Union[str, Sequence[str], None] = "ct76v8x9z65"' in source
    assert "SELECT 1 FROM customer_quote_preferences LIMIT 1" in source
    assert "禁止破坏性降级" in source
