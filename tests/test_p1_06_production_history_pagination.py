from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.api.production import router as production_router
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import (
    ProductionCompletion,
    ProductionCompletionBatch,
    ProductionTask,
)
from app.models.user import User


PASSWORD = "123456"


@pytest.fixture()
def production_history_app(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "p1-06-history.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="p106-admin",
            password_hash=hash_password(PASSWORD),
            role="admin",
            real_name="P1-06 Admin",
            must_change_password=False,
            customer_access_mode="all",
        )
        scoped = User(
            username="p106-scoped",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="P1-06 Scoped",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(name="分页客户甲")
        customer_b = Customer(name="分页客户乙")
        db.add_all([admin, scoped, customer_a, customer_b])
        db.flush()
        db.add(
            UserCustomerScope(
                user_id=scoped.id,
                customer_id=customer_a.id,
                assigned_by=admin.id,
            )
        )
        db.add(
            UserPermissionOverride(
                user_id=scoped.id,
                permission_code="orders.view",
                is_allowed=True,
                granted_by=admin.id,
            )
        )

        def add_completion(
            *,
            key: str,
            customer: Customer,
            product_code: str,
            product_name: str,
            customer_po: str,
            completed_at: datetime,
            status: str = "posted",
        ) -> int:
            product = Product(
                customer_id=customer.id,
                product_code=product_code,
                customer_material_code=f"MAT-{key}",
                product_name=product_name,
                box_style="普通箱",
            )
            db.add(product)
            db.flush()
            order = Order(
                order_number=f"ORD-{key}",
                customer_id=customer.id,
                customer_po=customer_po,
                order_date=date(2026, 7, 28),
                delivery_date=date(2026, 7, 30),
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=Decimal("10"),
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_sequence=1,
                item_order_number=f"{order.order_number}-001",
                quantity=10,
                delivered_quantity=0,
                unit_price=Decimal("1"),
                subtotal=Decimal("10"),
                material_status="received",
                snapshot_product_code=product_code,
                snapshot_product_name=product_name,
            )
            db.add(item)
            db.flush()
            task = ProductionTask(
                order_item_id=item.id,
                status="completed",
                planned_quantity=10,
                ordered_quantity_snapshot=10,
                material_received_quantity=10,
                material_input_quantity=10,
                output_factor=1,
                version=1,
            )
            batch = ProductionCompletionBatch(
                idempotency_key=f"batch-{key}",
                request_hash="a" * 64,
                item_count=1,
                completed_by=admin.id,
                completed_at=completed_at,
            )
            db.add_all([task, batch])
            db.flush()
            completion = ProductionCompletion(
                batch_id=batch.id,
                task_id=task.id,
                order_item_id=item.id,
                expected_version=1,
                quantity=10,
                material_input_quantity=10,
                planned_output_quantity=10,
                actual_output_quantity=10,
                defective_quantity=0,
                order_reserved_quantity=10,
                direct_delivery_quantity=10,
                stock_quantity=0,
                surplus_finished_quantity=0,
                completion_type="primary",
                initial_disposition="direct",
                status=status,
                completed_by=admin.id,
                completed_at=completed_at,
            )
            db.add(completion)
            db.flush()
            return completion.id

        ids = {
            "a_old": add_completion(
                key="A-OLD",
                customer=customer_a,
                product_code="BX-001",
                product_name="甲款外箱",
                customer_po="PO-ALPHA",
                completed_at=datetime(2026, 7, 27, 16, 0),
            ),
            "a_new": add_completion(
                key="A-NEW",
                customer=customer_a,
                product_code="BX-002",
                product_name="甲款内盒",
                customer_po="PO-BRAVO",
                completed_at=datetime(2026, 7, 28, 16, 0),
                status="reversed",
            ),
            "b": add_completion(
                key="B",
                customer=customer_b,
                product_code="BX-003",
                product_name="乙款外箱",
                customer_po="PO-CHARLIE",
                completed_at=datetime(2026, 7, 29, 16, 0),
            ),
            "customer_a": customer_a.id,
        }
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(production_router, prefix="/api/production")

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
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def test_production_history_supports_stable_paging_and_filters(
    production_history_app,
) -> None:
    app, ids = production_history_app
    with TestClient(app) as client:
        _login(client, "p106-admin")
        legacy = client.get("/api/production/completions")
        first_page = client.get("/api/production/completions?page=1&page_size=1")
        second_page = client.get("/api/production/completions?page=2&page_size=1")
        by_customer = client.get(
            f"/api/production/completions?customer_id={ids['customer_a']}&page=1&page_size=50"
        )
        by_order = client.get(
            "/api/production/completions?order_keyword=PO-BRAVO&page=1&page_size=50"
        )
        by_code = client.get(
            "/api/production/completions?product_code=BX-001&page=1&page_size=50"
        )
        by_name = client.get(
            "/api/production/completions?product_name=%E5%86%85%E7%9B%92&page=1&page_size=50"
        )
        by_date = client.get(
            "/api/production/completions?completed_date_from=2026-07-30&completed_date_to=2026-07-30&page=1&page_size=50"
        )
        by_status = client.get(
            "/api/production/completions?status=reversed&page=1&page_size=50"
        )

    assert legacy.status_code == 200
    assert list(legacy.json()) == ["items"]
    assert first_page.status_code == 200
    assert first_page.json()["total"] == 3
    assert first_page.json()["page"] == 1
    assert first_page.json()["page_size"] == 1
    assert first_page.json()["items"][0]["id"] == ids["b"]
    assert second_page.json()["items"][0]["id"] == ids["a_new"]
    assert [row["id"] for row in by_customer.json()["items"]] == [
        ids["a_new"],
        ids["a_old"],
    ]
    assert [row["id"] for row in by_order.json()["items"]] == [ids["a_new"]]
    assert [row["id"] for row in by_code.json()["items"]] == [ids["a_old"]]
    assert [row["id"] for row in by_name.json()["items"]] == [ids["a_new"]]
    assert [row["id"] for row in by_date.json()["items"]] == [ids["b"]]
    assert [row["id"] for row in by_status.json()["items"]] == [ids["a_new"]]


def test_production_history_keeps_customer_scope_after_paging(
    production_history_app,
) -> None:
    app, ids = production_history_app
    with TestClient(app) as client:
        _login(client, "p106-scoped")
        visible = client.get("/api/production/completions?page=1&page_size=50")
        hidden_customer = client.get(
            "/api/production/completions?customer_id=999999&page=1&page_size=50"
        )

    assert visible.status_code == 200, visible.text
    assert visible.json()["total"] == 2
    assert {row["id"] for row in visible.json()["items"]} == {
        ids["a_old"],
        ids["a_new"],
    }
    assert hidden_customer.status_code == 200
    assert hidden_customer.json()["total"] == 0
    assert hidden_customer.json()["items"] == []
