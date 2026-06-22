from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def product_lifecycle_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.products import router as products_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "phase14_lifecycle.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        session.add_all(
            [
                User(
                    username=role,
                    password_hash=hash_password("RolePass123!"),
                    role=role,
                    real_name=role,
                    display_name=role,
                    must_change_password=False,
                )
                for role in ("admin", "sales", "finance", "workshop")
            ]
        )
        customer = Customer(
            customer_number=1,
            customer_code="TH",
            name="Tianhua",
            payment_term_days=30,
            credit_limit=0,
        )
        material = Material(
            code="WCX1",
            layer_count=5,
            flute_type="AB",
        )
        session.add_all([customer, material])
        session.flush()
        referenced = Product(
            customer_id=customer.id,
            product_code="001A",
            customer_material_code="001A",
            product_name="001A outer carton",
            material_id=material.id,
            box_category="normal",
        )
        unreferenced = Product(
            customer_id=customer.id,
            product_code="001B",
            customer_material_code="001B",
            product_name="001B inner carton",
            material_id=material.id,
            box_category="normal",
        )
        session.add_all([referenced, unreferenced])
        session.flush()
        order = Order(
            order_number="PO-20260614-901",
            customer_id=customer.id,
            order_date=date(2026, 6, 14),
            delivery_date=date(2026, 6, 21),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("520.00"),
        )
        session.add(order)
        session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=referenced.id,
                quantity=100,
                unit_price=Decimal("5.20"),
                subtotal=Decimal("520.00"),
                snapshot_product_name="001A outer carton",
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(products_router, prefix="/api/master/products")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = session_factory
    return app


def _login(client: TestClient, role: str = "admin") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def test_delete_referenced_product_moves_it_to_trash_without_breaking_order(
    product_lifecycle_app: FastAPI,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product

    with TestClient(product_lifecycle_app) as client:
        _login(client)
        response = client.delete("/api/master/products/1")
        active = client.get("/api/master/products")
        detail = client.get("/api/master/products/1")
        trash = client.get("/api/master/products/trash")

    assert response.status_code == 200, response.text
    assert response.json()["deleted_at"] is not None
    assert active.json()["total"] == 1
    assert detail.status_code == 200
    assert trash.status_code == 200
    assert trash.json()["total"] == 1
    with product_lifecycle_app.state.session_factory() as session:
        assert session.get(Product, 1) is not None
        assert session.scalar(
            select(OrderItem).where(OrderItem.product_id == 1)
        ) is not None


def test_restore_product_returns_it_to_active_search(
    product_lifecycle_app: FastAPI,
) -> None:
    with TestClient(product_lifecycle_app) as client:
        _login(client)
        assert client.delete("/api/master/products/2").status_code == 200
        hidden = client.get("/api/master/products")
        restored = client.put("/api/master/products/2/restore")
        visible = client.get("/api/master/products")

    assert hidden.json()["total"] == 1
    assert restored.status_code == 200, restored.text
    assert restored.json()["deleted_at"] is None
    assert restored.json()["is_active"] is True
    assert visible.json()["total"] == 2


def test_purge_unreferenced_product_physically_deletes_it(
    product_lifecycle_app: FastAPI,
) -> None:
    from app.models.product import Product

    with TestClient(product_lifecycle_app) as client:
        _login(client)
        client.delete("/api/master/products/2")
        response = client.delete("/api/master/products/2/purge")

    assert response.status_code == 204, response.text
    with product_lifecycle_app.state.session_factory() as session:
        assert session.get(Product, 2) is None


def test_purge_referenced_product_archives_without_physical_delete(
    product_lifecycle_app: FastAPI,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product

    with TestClient(product_lifecycle_app) as client:
        _login(client)
        client.delete("/api/master/products/1")
        response = client.delete("/api/master/products/1/purge")

    assert response.status_code == 200, response.text
    assert response.json()["purged_at"] is not None
    with product_lifecycle_app.state.session_factory() as session:
        product = session.get(Product, 1)
        assert product is not None
        assert product.deleted_at is not None
        assert session.scalar(
            select(OrderItem).where(OrderItem.product_id == 1)
        ) is not None


def test_only_admin_can_purge_or_empty_product_trash(
    product_lifecycle_app: FastAPI,
) -> None:
    with TestClient(product_lifecycle_app) as client:
        _login(client, "sales")
        purge = client.delete("/api/master/products/1/purge")
        empty = client.post("/api/master/products/trash/empty")

    assert purge.status_code == 403
    assert empty.status_code == 403


def test_product_lifecycle_actions_write_audit_logs(
    product_lifecycle_app: FastAPI,
) -> None:
    from app.models.audit import OperationLog

    with TestClient(product_lifecycle_app) as client:
        _login(client)
        client.delete("/api/master/products/2")
        client.put("/api/master/products/2/restore")
        client.delete("/api/master/products/2")
        client.delete("/api/master/products/2/purge")

    with product_lifecycle_app.state.session_factory() as session:
        actions = list(
            session.scalars(
                select(OperationLog.action)
                .where(OperationLog.resource == "Product")
                .order_by(OperationLog.id)
            )
        )
    assert "MOVE_TO_TRASH" in actions
    assert "RESTORE" in actions
    assert "PURGE" in actions


def test_trash_reports_30_day_expiry_and_cleans_expired_products(
    product_lifecycle_app: FastAPI,
) -> None:
    from app.models.product import Product

    with product_lifecycle_app.state.session_factory() as session:
        product = session.get(Product, 2)
        product.is_active = False
        product.deleted_at = datetime.now() - timedelta(days=31)
        session.commit()

    with TestClient(product_lifecycle_app) as client:
        _login(client)
        trash = client.get("/api/master/products/trash")

    assert trash.status_code == 200
    assert trash.json()["total"] == 0
    with product_lifecycle_app.state.session_factory() as session:
        assert session.get(Product, 2) is None


def test_trash_item_returns_expiry_and_remaining_days(
    product_lifecycle_app: FastAPI,
) -> None:
    with TestClient(product_lifecycle_app) as client:
        _login(client)
        client.delete("/api/master/products/2")
        trash = client.get("/api/master/products/trash")

    item = trash.json()["items"][0]
    assert item["deleted_expires_at"] is not None
    assert 29 <= item["trash_days_remaining"] <= 30
