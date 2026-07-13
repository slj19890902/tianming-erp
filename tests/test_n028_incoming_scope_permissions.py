from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def scoped_incoming_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.incoming import router as incoming_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n028-incoming-scope.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with factory() as db:
        user = User(
            username="n028-incoming-workshop",
            password_hash=hash_password("ScopedIncoming123!"),
            role="workshop",
            real_name="Scoped Incoming",
            must_change_password=False,
            customer_access_mode="selected",
        )
        empty_user = User(
            username="n028-empty-incoming-workshop",
            password_hash=hash_password("ScopedIncoming123!"),
            role="workshop",
            real_name="Empty Scoped Incoming",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(name="Incoming Customer A")
        customer_b = Customer(name="Incoming Customer B")
        db.add_all([user, empty_user, customer_a, customer_b])
        db.flush()
        product_a = Product(customer_id=customer_a.id, product_code="IN-A", customer_material_code="IN-A", product_name="Box A")
        product_b = Product(customer_id=customer_b.id, product_code="IN-B", customer_material_code="IN-B", product_name="Box B")
        db.add_all([product_a, product_b])
        db.flush()
        orders = [
            Order(order_number="IN-A-PENDING", customer_id=customer_a.id, order_date=date.today(), delivery_date=date.today(), status="pending_production", payment_status="unpaid", total_amount=Decimal("1")),
            Order(order_number="IN-B-PENDING", customer_id=customer_b.id, order_date=date.today(), delivery_date=date.today(), status="pending_production", payment_status="unpaid", total_amount=Decimal("1")),
            Order(order_number="IN-A-RECEIVED", customer_id=customer_a.id, order_date=date.today(), delivery_date=date.today(), status="pending_production", payment_status="unpaid", total_amount=Decimal("1")),
            Order(order_number="IN-B-RECEIVED", customer_id=customer_b.id, order_date=date.today(), delivery_date=date.today(), status="pending_production", payment_status="unpaid", total_amount=Decimal("1")),
        ]
        db.add_all(orders)
        db.flush()
        items = [
            OrderItem(order_id=orders[0].id, product_id=product_a.id, quantity=10, unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="pending", requisition_status="已报料", snapshot_product_name="Box A"),
            OrderItem(order_id=orders[1].id, product_id=product_b.id, quantity=10, unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="pending", requisition_status="已报料", snapshot_product_name="Box B"),
            OrderItem(order_id=orders[2].id, product_id=product_a.id, quantity=10, unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="received", requisition_status="已入库", material_received_at=now - timedelta(hours=1), snapshot_product_name="Box A"),
            OrderItem(order_id=orders[3].id, product_id=product_b.id, quantity=10, unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="received", requisition_status="已入库", material_received_at=now - timedelta(days=2), snapshot_product_name="Box B"),
        ]
        db.add_all(items)
        db.flush()
        ordinary_items = [
            OrderItem(order_id=orders[0].id, product_id=product_a.id, quantity=5, unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="pending", requisition_status="已报料", snapshot_product_name="Ordinary Box A"),
            OrderItem(order_id=orders[1].id, product_id=product_b.id, quantity=5, unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="pending", requisition_status="已报料", snapshot_product_name="Ordinary Box B"),
            OrderItem(order_id=orders[2].id, product_id=product_a.id, quantity=5, unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="received", requisition_status="已入库", material_received_at=now - timedelta(hours=1), snapshot_product_name="Ordinary Box A"),
            OrderItem(order_id=orders[3].id, product_id=product_b.id, quantity=5, unit_price=Decimal("1"), subtotal=Decimal("1"), material_status="received", requisition_status="已入库", material_received_at=now - timedelta(days=2), snapshot_product_name="Ordinary Box B"),
        ]
        db.add_all(ordinary_items)
        db.flush()
        requisition = Requisition(requisition_number="N028-INCOMING", requisition_date=date.today())
        db.add(requisition)
        db.flush()
        components = [
            RequisitionItem(requisition_id=requisition.id, order_item_id=items[0].id, requisition_qty=10, cardboard_len=Decimal("1"), cardboard_width=Decimal("1"), product_name_snapshot="Box A-盖", status="有效"),
            RequisitionItem(requisition_id=requisition.id, order_item_id=items[1].id, requisition_qty=10, cardboard_len=Decimal("1"), cardboard_width=Decimal("1"), product_name_snapshot="Box B-盖", status="有效"),
            RequisitionItem(requisition_id=requisition.id, order_item_id=items[2].id, requisition_qty=10, cardboard_len=Decimal("1"), cardboard_width=Decimal("1"), product_name_snapshot="Box A-盖", status="已入库"),
            RequisitionItem(requisition_id=requisition.id, order_item_id=items[3].id, requisition_qty=10, cardboard_len=Decimal("1"), cardboard_width=Decimal("1"), product_name_snapshot="Box B-盖", status="已入库"),
        ]
        db.add_all([UserCustomerScope(user_id=user.id, customer_id=customer_a.id), *components])
        db.commit()
        ids = {
            "a_pending": items[0].id,
            "b_pending": items[1].id,
            "a_received": items[2].id,
            "b_received": items[3].id,
            "a_pending_component": components[0].id,
            "b_pending_component": components[1].id,
            "a_received_component": components[2].id,
            "b_received_component": components[3].id,
            "a_pending_ordinary": ordinary_items[0].id,
            "b_pending_ordinary": ordinary_items[1].id,
            "a_received_ordinary": ordinary_items[2].id,
            "b_received_ordinary": ordinary_items[3].id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(incoming_router, prefix="/api/incoming")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "ScopedIncoming123!"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize("endpoint", ["pending", "received", "history"])
def test_selected_scope_filters_incoming_rows_and_empty_scope_returns_none(
    scoped_incoming_app,
    endpoint: str,
) -> None:
    app, _, ids = scoped_incoming_app
    with TestClient(app) as client:
        _login(client, "n028-incoming-workshop")
        response = client.get(f"/api/incoming/{endpoint}")
        assert response.status_code == 200
        returned_ids = {item["item_id"] for item in response.json()["items"]}
        if endpoint == "pending":
            assert {ids["a_pending_ordinary"], f"r{ids['a_pending_component']}"} <= returned_ids
            forbidden_ids = {ids["b_pending_ordinary"], f"r{ids['b_pending_component']}"}
        else:
            assert {ids["a_received_ordinary"], f"r{ids['a_received_component']}"} <= returned_ids
            forbidden_ids = {ids["b_received_ordinary"], f"r{ids['b_received_component']}"}
        assert not (forbidden_ids & returned_ids)

        client.post("/api/auth/logout")
        _login(client, "n028-empty-incoming-workshop")
        empty_scope_response = client.get(f"/api/incoming/{endpoint}")
    assert empty_scope_response.status_code == 200
    assert empty_scope_response.json() == {"items": []}


@pytest.mark.parametrize("item_key", ["b_pending", "b_pending_component"])
def test_receive_and_batch_receive_reject_cross_customer_without_writes(
    scoped_incoming_app,
    item_key: str,
) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionItem

    app, factory, ids = scoped_incoming_app
    target = f"r{ids[item_key]}" if item_key.endswith("component") else ids[item_key]
    with TestClient(app) as client:
        _login(client, "n028-incoming-workshop")
        receive = client.put(f"/api/incoming/receive/{target}", json={"received_quantity": 7})
        batch = client.put(
            "/api/incoming/batch-receive",
            json={"items": [{"item_id": ids["a_pending"], "received_quantity": 7}, {"item_id": target, "received_quantity": 7}]},
        )
    assert receive.status_code == 403
    assert batch.status_code == 403
    with factory() as db:
        assert db.get(OrderItem, ids["a_pending"]).material_status == "pending"
        assert db.get(OrderItem, ids["b_pending"]).material_status == "pending"
        assert db.get(RequisitionItem, ids["b_pending_component"]).status == "有效"


@pytest.mark.parametrize("item_key", ["b_received", "b_received_component"])
def test_revert_rejects_cross_customer_without_writes(scoped_incoming_app, item_key: str) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionItem

    app, factory, ids = scoped_incoming_app
    target = f"r{ids[item_key]}" if item_key.endswith("component") else ids[item_key]
    with TestClient(app) as client:
        _login(client, "n028-incoming-workshop")
        response = client.put(f"/api/incoming/revert/{target}", json={"reason": "scope test"})
    assert response.status_code == 403
    with factory() as db:
        assert db.get(OrderItem, ids["b_received"]).material_status == "received"
        assert db.get(RequisitionItem, ids["b_received_component"]).status == "已入库"
