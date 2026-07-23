from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from PIL import Image


@pytest.fixture()
def n028_order_drawing_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(tmp_path / "private_uploads"))
    monkeypatch.setenv("ERP_UPLOAD_TEMP_DIR", str(tmp_path / "upload_tokens"))
    engine = create_sqlite_engine(tmp_path / "n028-order-drawings.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="n028-drawing-admin",
            password_hash=hash_password("AdminPass123!"),
            role="admin",
            real_name="Drawing Admin",
            must_change_password=False,
        )
        denied_sales = User(
            username="n028-drawing-sales",
            password_hash=hash_password("SalesPass123!"),
            role="sales",
            real_name="Drawing Sales",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer = Customer(
            customer_number=2801,
            customer_code="N028-DRAWING",
            name="N028 Drawing Customer",
        )
        product = Product(
            customer=customer,
            product_code="N028-DRAWING-PRODUCT",
            customer_material_code="N028-DRAWING-PRODUCT",
            product_name="N028 drawing product",
            box_category="normal",
            sale_unit_price=Decimal("9.00"),
        )
        db.add_all([admin, denied_sales, customer, product])
        db.flush()
        order = Order(
            order_number="N028-DRAWING-ORDER",
            customer_id=customer.id,
            order_date=date(2026, 7, 13),
            total_amount=Decimal("9.00"),
            created_by=admin.id,
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=1,
            unit_price=Decimal("9.00"),
            subtotal=Decimal("9.00"),
            snapshot_product_name=product.product_name,
            snapshot_product_code=product.product_code,
        )
        db.add_all(
            [
                UserCustomerScope(
                    user_id=denied_sales.id,
                    customer_id=customer.id,
                ),
                UserPermissionOverride(
                    user_id=denied_sales.id,
                    permission_code="products.edit",
                    is_allowed=False,
                    granted_by=admin.id,
                ),
                item,
            ]
        )
        db.commit()
        ids = {
            "customer": customer.id,
            "product": product.id,
            "item": item.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, factory
    finally:
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200


def _png_bytes() -> bytes:
    image = Image.new("RGB", (32, 32), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _save_to_product_order_payload(
    customer_id: int,
    product_id: int,
    token: str,
) -> dict:
    return {
        "customer_id": customer_id,
        "items": [
            {
                "product_id": product_id,
                "quantity": 1,
                "unit_price": "9.00",
                "temp_drawing_token": token,
                "drawing_save_option": "save_to_product",
            }
        ],
    }


def test_explicit_products_edit_deny_blocks_both_product_drawing_paths_before_persistence(
    n028_order_drawing_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_order_drawing_app
    with TestClient(app) as client:
        _login(client, "n028-drawing-sales", "SalesPass123!")
        standalone = client.post(
            f"/api/orders/items/{ids['item']}/drawing",
            params={"save_to_product": "true"},
            files={"file": ("drawing.png", _png_bytes(), "image/png")},
        )
        assert standalone.status_code == 403

        create = client.post(
            "/api/orders",
            json=_save_to_product_order_payload(
                ids["customer"], ids["product"], "a" * 32
            ),
        )
        assert create.status_code == 403

    with factory() as db:
        item = db.get(OrderItem, ids["item"])
        assert item is not None
        assert item.drawing_file is None
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 0
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 1


def test_products_edit_allows_order_and_standalone_product_drawing_saves(
    n028_order_drawing_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_order_drawing_app
    with TestClient(app) as client:
        _login(client, "n028-drawing-admin", "AdminPass123!")
        draft_upload = client.post(
            "/api/orders/draft-drawing",
            files={"file": ("incoming.png", _png_bytes(), "image/png")},
        )
        assert draft_upload.status_code == 200, draft_upload.text
        created = client.post(
            "/api/orders",
            json=_save_to_product_order_payload(
                ids["customer"], ids["product"], draft_upload.json()["token"]
            ),
        )
        assert created.status_code == 201
        created_item_id = created.json()["items"][0]["id"]

        standalone = client.post(
            f"/api/orders/items/{created_item_id}/drawing",
            params={"save_to_product": "true"},
            files={"file": ("replacement.png", _png_bytes(), "image/png")},
        )
        assert standalone.status_code == 200
        assert standalone.json()["saved_to_product"] is True

    with factory() as db:
        drawings = db.scalars(
            select(ProductDrawing)
            .where(ProductDrawing.product_id == ids["product"])
            .order_by(ProductDrawing.id)
        ).all()
        assert len(drawings) == 2
        assert db.get(OrderItem, created_item_id).drawing_file is not None
