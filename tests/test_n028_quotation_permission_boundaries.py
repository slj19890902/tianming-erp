from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from quotation_contract_client import QuotationContractClient as TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def quotation_conversion_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.quotations import router as quotations_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserPermissionOverride
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.quotation import QuotationItem, QuotationOrder
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n028-quotation-permissions.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="quotation-admin",
            password_hash=hash_password("AdminPass123!"),
            role="admin",
            real_name="Admin",
            must_change_password=False,
        )
        denied_boss = User(
            username="quotation-denied-boss",
            password_hash=hash_password("BossPass123!"),
            role="boss",
            real_name="Denied boss",
            must_change_password=False,
        )
        redacted_boss = User(
            username="quotation-redacted-boss",
            password_hash=hash_password("BossPass123!"),
            role="boss",
            real_name="Redacted boss",
            must_change_password=False,
        )
        customer = Customer(name="Quotation permission customer")
        material = Material(
            code="N028-BOARD",
            layer_count=5,
            supplier_name="Board supplier",
            quote_price=Decimal("5.4321"),
            is_active=True,
        )
        db.add_all([admin, denied_boss, redacted_boss, customer, material])
        db.flush()
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=admin.id,
                    permission_code="products.create",
                    is_allowed=False,
                ),
                UserPermissionOverride(
                    user_id=admin.id,
                    permission_code="cost.view",
                    is_allowed=False,
                ),
                UserPermissionOverride(
                    user_id=denied_boss.id,
                    permission_code="products.create",
                    is_allowed=False,
                ),
                UserPermissionOverride(
                    user_id=redacted_boss.id,
                    permission_code="cost.view",
                    is_allowed=False,
                ),
            ]
        )

        item_ids = {}
        for key in ("denied", "redacted", "admin"):
            quotation = QuotationOrder(
                quotation_no=f"N028-{key}",
                customer_id=customer.id,
                customer_name=customer.name,
                quotation_date=date(2026, 7, 13),
                status="accepted",
                total_amount=Decimal("250.00"),
            )
            db.add(quotation)
            db.flush()
            item = QuotationItem(
                quotation_id=quotation.id,
                product_name=f"N028 {key} carton",
                box_type="A1/0201",
                length_mm=Decimal("300"),
                width_mm=Decimal("200"),
                height_mm=Decimal("150"),
                material_id=material.id,
                material_code=material.code,
                flute_type="AB",
                quantity=100,
                estimated_unit_cost=Decimal("1.2345"),
                suggested_unit_price=Decimal("2.3456"),
                final_unit_price=Decimal("2.5000"),
            )
            db.add(item)
            db.flush()
            item_ids[key] = item.id
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(quotations_router, prefix="/api/quotations")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, item_ids
    finally:
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200


def test_convert_requires_products_create_after_explicit_deny(
    quotation_conversion_app,
) -> None:
    app, factory, item_ids = quotation_conversion_app

    with TestClient(app) as client:
        _login(client, "quotation-denied-boss", "BossPass123!")
        response = client.post(
            f"/api/quotations/items/{item_ids['denied']}/convert-to-product",
            json={"product_code": "N028-DENIED"},
        )

    assert response.status_code == 403
    from app.models.product import Product

    with factory() as db:
        assert db.query(Product).filter_by(product_code="N028-DENIED").count() == 0


def test_convert_redacts_persisted_cost_values_after_explicit_deny(
    quotation_conversion_app,
) -> None:
    app, factory, item_ids = quotation_conversion_app

    with TestClient(app) as client:
        _login(client, "quotation-redacted-boss", "BossPass123!")
        response = client.post(
            f"/api/quotations/items/{item_ids['redacted']}/convert-to-product",
            json={"product_code": "N028-REDACTED"},
        )

    assert response.status_code == 201
    from app.models.product import Product

    with factory() as db:
        product = db.get(Product, response.json()["product_id"])
        assert product is not None
        assert (
            product.cost_unit_price,
            product.board_price,
            product.suggested_price,
        ) == (None, None, None)


def test_admin_conversion_retains_cost_values_despite_overrides(
    quotation_conversion_app,
) -> None:
    app, factory, item_ids = quotation_conversion_app

    with TestClient(app) as client:
        _login(client, "quotation-admin", "AdminPass123!")
        response = client.post(
            f"/api/quotations/items/{item_ids['admin']}/convert-to-product",
            json={"product_code": "N028-ADMIN"},
        )

    assert response.status_code == 201
    from app.models.product import Product

    with factory() as db:
        product = db.get(Product, response.json()["product_id"])
        assert product is not None
        assert (
            product.cost_unit_price,
            product.board_price,
            product.suggested_price,
        ) == (
            Decimal("1.2345"),
            Decimal("5.4321"),
            Decimal("2.3456"),
        )
