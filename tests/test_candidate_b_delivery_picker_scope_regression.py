from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


PASSWORD = "PickerScopePass123!"
SALES_FIELDS = {
    "total_amount",
    "payment_status",
    "unit_price",
    "subtotal",
    "sale_amount",
}


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for child in value.values()
            for key in _all_keys(child)
        }
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()


@pytest.fixture()
def candidate_b_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deliveries import pick_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.delivery import (
        Delivery,
        DeliveryItem,
        DeliveryPickTask,
        DeliveryPickTaskItem,
    )
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "candidate-b-picker-scope.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as db:
        admin = User(
            username="candidate-b-admin",
            password_hash=hash_password(PASSWORD),
            role="admin",
            real_name="Candidate B Admin",
            must_change_password=False,
        )
        scoped_picker = User(
            username="candidate-b-picker",
            password_hash=hash_password(PASSWORD),
            role="delivery_picker",
            real_name="Assigned Picker",
            customer_access_mode="selected",
            must_change_password=False,
        )
        other_picker = User(
            username="candidate-b-other-picker",
            password_hash=hash_password(PASSWORD),
            role="delivery_picker",
            real_name="Other Picker",
            customer_access_mode="all",
            must_change_password=False,
        )
        order_override_picker = User(
            username="candidate-b-order-override",
            password_hash=hash_password(PASSWORD),
            role="delivery_picker",
            real_name="Order Override Picker",
            customer_access_mode="selected",
            must_change_password=False,
        )
        customer_a = Customer(
            customer_number=91001,
            customer_code="CAND-B-A",
            name="Candidate B Allowed Customer",
            payment_term_days=30,
            credit_limit=Decimal("10000"),
        )
        customer_b = Customer(
            customer_number=91002,
            customer_code="CAND-B-B",
            name="Candidate B Hidden Customer",
            payment_term_days=30,
            credit_limit=Decimal("10000"),
        )
        db.add_all(
            [
                admin,
                scoped_picker,
                other_picker,
                order_override_picker,
                customer_a,
                customer_b,
            ]
        )
        db.flush()
        db.add_all(
            [
                UserCustomerScope(
                    user_id=scoped_picker.id,
                    customer_id=customer_a.id,
                    assigned_by=admin.id,
                ),
                UserCustomerScope(
                    user_id=order_override_picker.id,
                    customer_id=customer_a.id,
                    assigned_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=order_override_picker.id,
                    permission_code="orders.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
            ]
        )

        product_a = Product(
            customer_id=customer_a.id,
            product_code="CAND-B-PRODUCT-A",
            customer_material_code="CAND-B-PRODUCT-A",
            product_name="Allowed Picker Product",
            box_category="normal",
        )
        product_b = Product(
            customer_id=customer_b.id,
            product_code="CAND-B-PRODUCT-B",
            customer_material_code="CAND-B-PRODUCT-B",
            product_name="Hidden Picker Product",
            box_category="normal",
        )
        db.add_all([product_a, product_b])
        db.flush()

        def add_task(
            *,
            suffix: str,
            customer: Customer,
            product: Product,
            assigned_to: int,
            amount: Decimal,
        ) -> tuple[DeliveryPickTask, DeliveryPickTaskItem, Order]:
            order = Order(
                order_number=f"CAND-B-ORDER-{suffix}",
                customer_id=customer.id,
                customer_po=f"CAND-B-PO-{suffix}",
                order_date=date(2026, 8, 13),
                delivery_date=date(2026, 8, 14),
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=amount,
                created_by=admin.id,
            )
            db.add(order)
            db.flush()
            order_item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=10,
                unit_price=amount / Decimal("10"),
                subtotal=amount,
                material_status="received",
                snapshot_product_code=product.product_code,
                snapshot_product_name=product.product_name,
                snapshot_spec="380x260x220mm",
            )
            db.add(order_item)
            db.flush()
            delivery = Delivery(
                delivery_number=f"CAND-B-DELIVERY-{suffix}",
                customer_id=customer.id,
                delivery_date=date(2026, 8, 14),
                status="pending",
                total_quantity=10,
                created_by=admin.id,
            )
            db.add(delivery)
            db.flush()
            delivery_item = DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=order_item.id,
                delivered_quantity=10,
            )
            db.add(delivery_item)
            db.flush()
            task = DeliveryPickTask(
                delivery_id=delivery.id,
                customer_id=customer.id,
                status="pushed",
                snapshot_version=1,
                created_by=admin.id,
                assigned_to=assigned_to,
            )
            db.add(task)
            db.flush()
            task_item = DeliveryPickTaskItem(
                task_id=task.id,
                delivery_item_id=delivery_item.id,
                order_item_id=order_item.id,
                original_quantity=10,
                picked_quantity=0,
                status="pending",
                product_code_snapshot=product.product_code,
                product_name_snapshot=product.product_name,
                specification_snapshot="380x260x220mm",
            )
            db.add(task_item)
            db.flush()
            return task, task_item, order

        own_first, own_first_item, order_a = add_task(
            suffix="A-OWN-1",
            customer=customer_a,
            product=product_a,
            assigned_to=scoped_picker.id,
            amount=Decimal("123.40"),
        )
        own_second, _own_second_item, _order_a_second = add_task(
            suffix="A-OWN-2",
            customer=customer_a,
            product=product_a,
            assigned_to=scoped_picker.id,
            amount=Decimal("234.50"),
        )
        other_task, other_task_item, _other_order = add_task(
            suffix="A-OTHER",
            customer=customer_a,
            product=product_a,
            assigned_to=other_picker.id,
            amount=Decimal("987.60"),
        )
        revoked_scope_task, revoked_scope_item, order_b = add_task(
            suffix="B-REVOKED-SCOPE",
            customer=customer_b,
            product=product_b,
            assigned_to=scoped_picker.id,
            amount=Decimal("876.50"),
        )
        db.commit()
        ids = {
            "customer_a": customer_a.id,
            "customer_b": customer_b.id,
            "picker": scoped_picker.id,
            "own_tasks": {own_first.id, own_second.id},
            "own_first": own_first.id,
            "own_first_item": own_first_item.id,
            "other_task": other_task.id,
            "other_task_item": other_task_item.id,
            "revoked_scope_task": revoked_scope_task.id,
            "revoked_scope_item": revoked_scope_item.id,
            "order_a": order_a.id,
            "order_a_po": order_a.customer_po,
            "order_b": order_b.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(pick_router, prefix="/api/delivery-picks")

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
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


def _assert_no_pick_scope_expansion(response, ids: dict) -> None:
    if response.status_code in {400, 403, 404, 422}:
        return
    assert response.status_code == 200, response.text
    payload = response.json()
    returned = {int(row["id"]) for row in payload["items"]}
    assert returned <= ids["own_tasks"]
    assert ids["other_task"] not in returned
    assert ids["revoked_scope_task"] not in returned
    assert all(int(row["customer_id"]) == ids["customer_a"] for row in payload["items"])
    assert all(int(row["assigned_to"]) == ids["picker"] for row in payload["items"])


def test_pick_list_rechecks_assignee_customer_scope_and_pagination(
    candidate_b_app,
) -> None:
    app, ids = candidate_b_app
    with TestClient(app) as client:
        _login(client, "candidate-b-picker")
        page_one = client.get(
            "/api/delivery-picks",
            params={"response_mode": "summary", "page": 1, "page_size": 1},
        )
        page_two = client.get(
            "/api/delivery-picks",
            params={"response_mode": "summary", "page": 2, "page_size": 1},
        )
        past_end = client.get(
            "/api/delivery-picks",
            params={"response_mode": "summary", "page": 99, "page_size": 1},
        )

    assert page_one.status_code == page_two.status_code == past_end.status_code == 200
    assert page_one.json()["total"] == page_two.json()["total"] == 2
    paged_ids = {
        int(page_one.json()["items"][0]["id"]),
        int(page_two.json()["items"][0]["id"]),
    }
    assert paged_ids == ids["own_tasks"]
    assert past_end.json()["items"] == []


@pytest.mark.parametrize(
    "extra_params",
    [
        {"search": "CAND-B-DELIVERY-A-OTHER"},
        {"keyword": "Hidden Picker Product"},
        {"customer_id": 91002},
        {"task_id": 999999},
        {"page": 1, "page_size": 200, "search": "B-REVOKED-SCOPE"},
    ],
)
def test_unknown_search_customer_and_task_parameters_never_expand_pick_scope(
    candidate_b_app,
    extra_params: dict[str, object],
) -> None:
    app, ids = candidate_b_app
    params: dict[str, object] = {
        "response_mode": "summary",
        "page": 1,
        "page_size": 200,
    }
    params.update(extra_params)
    with TestClient(app) as client:
        _login(client, "candidate-b-picker")
        response = client.get("/api/delivery-picks", params=params)
    _assert_no_pick_scope_expansion(response, ids)


def test_existing_other_customer_and_task_query_values_never_expand_pick_scope(
    candidate_b_app,
) -> None:
    app, ids = candidate_b_app
    with TestClient(app) as client:
        _login(client, "candidate-b-picker")
        responses = [
            client.get(
                "/api/delivery-picks",
                params={
                    "response_mode": "summary",
                    "page": 1,
                    "page_size": 200,
                    "customer_id": ids["customer_b"],
                },
            ),
            client.get(
                "/api/delivery-picks",
                params={
                    "response_mode": "summary",
                    "page": 1,
                    "page_size": 200,
                    "task_id": ids["other_task"],
                },
            ),
        ]
    for response in responses:
        _assert_no_pick_scope_expansion(response, ids)


def test_direct_task_and_item_urls_fail_closed_for_other_picker_or_customer(
    candidate_b_app,
) -> None:
    app, ids = candidate_b_app
    with TestClient(app) as client:
        _login(client, "candidate-b-picker")
        own = client.get(f"/api/delivery-picks/{ids['own_first']}")
        other = client.get(f"/api/delivery-picks/{ids['other_task']}")
        other_write = client.put(
            f"/api/delivery-picks/{ids['other_task']}/items/{ids['other_task_item']}",
            json={"pick_status": "no_stock", "picked_quantity": 0},
        )
        revoked_scope = client.get(
            f"/api/delivery-picks/{ids['revoked_scope_task']}"
        )
        revoked_scope_write = client.put(
            f"/api/delivery-picks/{ids['revoked_scope_task']}/items/"
            f"{ids['revoked_scope_item']}",
            json={"pick_status": "no_stock", "picked_quantity": 0},
        )

    assert own.status_code == 200, own.text
    assert int(own.json()["id"]) == ids["own_first"]
    assert not (SALES_FIELDS & _all_keys(own.json()))
    assert other.status_code == 404
    assert other_write.status_code == 404
    assert revoked_scope.status_code == 403
    assert revoked_scope_write.status_code == 403
    assert "Hidden Picker Product" not in revoked_scope.text


def test_default_picker_loses_general_orders_but_explicit_override_stays_redacted(
    candidate_b_app,
) -> None:
    app, ids = candidate_b_app
    with TestClient(app) as client:
        _login(client, "candidate-b-picker")
        assert client.get("/api/orders").status_code == 403

        _login(client, "candidate-b-order-override")
        summary = client.get(
            "/api/orders",
            params={"scope": "all", "detail_level": "summary", "page_size": 50},
        )
        full = client.get(
            "/api/orders",
            params={"scope": "all", "detail_level": "full", "page_size": 50},
        )
        group = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "anchor_order_id": ids["order_a"],
                "customer_po": ids["order_a_po"],
                "scope": "all",
            },
        )
        single = client.get(f"/api/orders/{ids['order_a']}")
        cross_customer = client.get(f"/api/orders/{ids['order_b']}")

    for response in (summary, full, group, single):
        assert response.status_code == 200, response.text
        assert not (SALES_FIELDS & _all_keys(response.json()))
    assert {row["customer_id"] for row in summary.json()["items"]} == {
        ids["customer_a"]
    }
    assert {row["customer_id"] for row in full.json()["items"]} == {
        ids["customer_a"]
    }
    assert cross_customer.status_code == 403
