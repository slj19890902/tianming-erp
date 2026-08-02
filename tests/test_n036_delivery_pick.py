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


def test_n036_migration_follows_n035_linearly() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "cc59v8x9z48_generic_delivery_pick_tasks.py"
    ).read_text(encoding="utf-8")

    assert "Revises: be58v8x9z49" in migration
    assert 'down_revision: str | None = "be58v8x9z49"' in migration


def test_p1_21c_assignment_migration_is_linear_and_fail_closed() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "dd86v8x9z75_assign_delivery_pick_tasks.py"
    ).read_text(encoding="utf-8")

    assert "Revises: dc85v8x9z74" in migration
    assert 'down_revision: str | None = "dc85v8x9z74"' in migration
    assert '"assigned_to"' in migration
    assert "WHERE assigned_to IS NOT NULL" in migration


@pytest.fixture()
def pick_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deliveries import pick_router, router as deliveries_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.audit import OperationLog
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n036.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        users = [
            User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=role,
                display_name=role,
                must_change_password=False,
            )
            for role in ("admin", "delivery_picker", "sales")
        ]
        customer = Customer(
            customer_number=1,
            customer_code="N036",
            name="N036测试客户",
            payment_term_days=30,
            credit_limit=Decimal("10000"),
        )
        other = Customer(
            customer_number=2,
            customer_code="N036-OTHER",
            name="N036其他客户",
            payment_term_days=30,
            credit_limit=Decimal("10000"),
        )
        db.add_all([*users, customer, other])
        db.flush()
        products = [
            Product(
                customer_id=customer.id,
                product_code="PICK-001",
                customer_material_code="PICK-001",
                product_name="拿货测试外箱",
                box_category="normal",
            ),
            Product(
                customer_id=customer.id,
                product_code="PICK-002",
                customer_material_code="PICK-002",
                product_name="拿货测试内箱",
                box_category="normal",
            ),
        ]
        db.add_all(products)
        db.flush()
        order = Order(
            order_number="TM-N036-001",
            customer_id=customer.id,
            customer_po="CPO-N036",
            order_date=date(2026, 7, 18),
            delivery_date=date(2026, 7, 19),
            status="pending_delivery",
            payment_status="unpaid",
            total_amount=Decimal("150"),
        )
        db.add(order)
        db.flush()
        order_items = [
            OrderItem(
                order_id=order.id,
                product_id=products[0].id,
                quantity=100,
                unit_price=Decimal("1"),
                subtotal=Decimal("100"),
                material_status="received",
                delivered_quantity=0,
                snapshot_product_name="拿货测试外箱",
                snapshot_spec="500×300×200mm",
            ),
            OrderItem(
                order_id=order.id,
                product_id=products[1].id,
                quantity=50,
                unit_price=Decimal("1"),
                subtotal=Decimal("50"),
                material_status="received",
                delivered_quantity=0,
                snapshot_product_name="拿货测试内箱",
                snapshot_spec="300×200×150mm",
            ),
        ]
        db.add_all(order_items)
        db.flush()
        delivery = Delivery(
            delivery_number="TM-20260718-001",
            customer_id=customer.id,
            delivery_date=date(2026, 7, 18),
            status="pending",
            total_quantity=150,
            created_by=users[0].id,
        )
        db.add(delivery)
        db.flush()
        db.add_all(
            [
                DeliveryItem(
                    delivery_id=delivery.id,
                    order_item_id=order_items[0].id,
                    delivered_quantity=100,
                ),
                DeliveryItem(
                    delivery_id=delivery.id,
                    order_item_id=order_items[1].id,
                    delivered_quantity=50,
                ),
            ]
        )
        db.commit()
        ids = {
            "delivery": delivery.id,
            "customer": customer.id,
            "order": order.id,
            "order_items": [row.id for row in order_items],
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(pick_router, prefix="/api/delivery-picks")

    def override_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    return app, factory, ids, OperationLog


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def _create_task(client: TestClient, delivery_id: int) -> dict:
    response = client.post(f"/api/deliveries/{delivery_id}/pick-task")
    assert response.status_code == 201, response.text
    return response.json()


def test_picker_permission_and_snapshot_contract(pick_app) -> None:
    app, _, ids, _ = pick_app
    with TestClient(app) as client:
        _login(client, "admin")
        task = _create_task(client, ids["delivery"])
        assert task["delivery_number"] == "TM-20260718-001"
        assert task["customer_name"] == "N036测试客户"
        assert task["assigned_to_name"] == "delivery_picker"
        assert [row["planned_quantity"] for row in task["items"]] == [100, 50]
        repeated = _create_task(client, ids["delivery"])
        assert repeated["id"] == task["id"]
        assert repeated["snapshot_version"] == task["snapshot_version"]

        _login(client, "sales")
        assert client.get("/api/delivery-picks").status_code == 403

        _login(client, "delivery_picker")
        listed = client.get("/api/delivery-picks")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == task["id"]
        assert client.get("/api/delivery-picks", params={"status": "unknown"}).status_code == 400
        assert (
            client.post(f"/api/deliveries/{ids['delivery']}/pick-task").status_code
            == 403
        )


def test_p1_21c_picker_only_sees_assigned_task_and_dispatch_can_reassign(pick_app) -> None:
    from app.core.security import hash_password
    from app.models.user import User

    app, factory, ids, _ = pick_app
    with TestClient(app) as client:
        _login(client, "admin")
        task = _create_task(client, ids["delivery"])

    with factory() as db:
        second = User(
            username="delivery_picker_2",
            password_hash=hash_password("RolePass123!"),
            role="delivery_picker",
            real_name="送货员二",
            display_name="送货员二",
            must_change_password=False,
        )
        db.add(second)
        db.commit()
        second_id = second.id

    with TestClient(app) as client:
        _login(client, "admin")
        assignees = client.get("/api/delivery-picks/assignees")
        assert assignees.status_code == 200
        assert {row["id"] for row in assignees.json()["items"]} >= {second_id}
        assigned = client.put(
            f"/api/delivery-picks/{task['id']}/assignment",
            json={"picker_user_id": second_id},
        )
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["assigned_to"] == second_id

        _login(client, "delivery_picker")
        assert client.get("/api/delivery-picks").json()["items"] == []
        hidden = client.get(f"/api/delivery-picks/{task['id']}")
        assert hidden.status_code == 404

        _login(client, "delivery_picker_2")
        listed = client.get("/api/delivery-picks")
        assert [row["id"] for row in listed.json()["items"]] == [task["id"]]
        assert client.get(f"/api/delivery-picks/{task['id']}").status_code == 200


def test_delivery_list_includes_pick_task_summary(pick_app) -> None:
    app, _, ids, _ = pick_app
    with TestClient(app) as client:
        _login(client, "admin")
        task = _create_task(client, ids["delivery"])

        listed = client.get("/api/deliveries", params={"page": 1, "page_size": 50})

        assert listed.status_code == 200, listed.text
        delivery = next(
            row for row in listed.json()["items"] if row["id"] == ids["delivery"]
        )
        assert delivery["pick_task"]["id"] == task["id"]
        assert delivery["pick_task"]["status"] == "pushed"
        assert delivery["pick_task"]["exceptions"] == []


def test_partial_and_no_stock_apply_only_changes_delivery_draft(pick_app) -> None:
    from app.models.delivery import Delivery, DeliveryItem, DeliveryPickTask, DeliveryPickTaskItem
    from app.models.order import OrderItem

    app, factory, ids, operation_log = pick_app
    with TestClient(app) as client:
        _login(client, "admin")
        task = _create_task(client, ids["delivery"])
        task_id = task["id"]
        first, second = task["items"]

        _login(client, "delivery_picker")
        partial = client.put(
            f"/api/delivery-picks/{task_id}/items/{first['id']}",
            json={"pick_status": "partial", "picked_quantity": 80},
        )
        no_stock = client.put(
            f"/api/delivery-picks/{task_id}/items/{second['id']}",
            json={"pick_status": "no_stock", "picked_quantity": 0},
        )
        assert partial.status_code == 200, partial.text
        assert no_stock.status_code == 200, no_stock.text
        submitted = client.post(f"/api/delivery-picks/{task_id}/submit")
        assert submitted.status_code == 200
        assert submitted.json()["status"] == "exception"
        assert len(submitted.json()["exceptions"]) == 2

        _login(client, "admin")
        assert client.put(f"/api/deliveries/{ids['delivery']}/dispatch").status_code == 409
        applied = client.post(f"/api/delivery-picks/{task_id}/apply")
        assert applied.status_code == 200, applied.text
        assert applied.json()["total_quantity"] == 80

    with factory() as db:
        delivery = db.get(Delivery, ids["delivery"])
        lines = db.scalars(
            select(DeliveryItem).where(DeliveryItem.delivery_id == delivery.id)
        ).all()
        assert [line.delivered_quantity for line in lines] == [80]
        assert [db.get(OrderItem, item_id).quantity for item_id in ids["order_items"]] == [100, 50]
        assert [db.get(OrderItem, item_id).delivered_quantity for item_id in ids["order_items"]] == [0, 0]
        saved_task = db.scalar(select(DeliveryPickTask).where(DeliveryPickTask.id == task_id))
        assert saved_task.status == "applied"
        snapshots = db.scalars(
            select(DeliveryPickTaskItem)
            .where(DeliveryPickTaskItem.task_id == task_id)
            .order_by(DeliveryPickTaskItem.id)
        ).all()
        assert len(snapshots) == 2
        assert snapshots[1].delivery_item_id is None
        assert snapshots[1].product_code_snapshot == "PICK-002"
        logs = db.scalars(select(operation_log)).all()
        actions = {row.action for row in logs}
        assert {"CREATE_PICK_TASK", "UPDATE_PICK_ITEM", "SUBMIT_PICK_TASK", "APPLY_PICK_TASK"}.issubset(actions)
        apply_log = next(row for row in logs if row.action == "APPLY_PICK_TASK")
        assert '"product_code": "PICK-002"' in (apply_log.details or "")


def test_over_pick_applies_draft_but_dispatch_still_requires_physical_inventory(
    pick_app,
) -> None:
    from app.models.order import OrderItem

    app, factory, ids, _ = pick_app
    with TestClient(app) as client:
        _login(client, "admin")
        task = _create_task(client, ids["delivery"])
        _login(client, "delivery_picker")
        quantities = [120, 50]
        for item, quantity in zip(task["items"], quantities, strict=True):
            response = client.put(
                f"/api/delivery-picks/{task['id']}/items/{item['id']}",
                json={"pick_status": "picked", "picked_quantity": quantity},
            )
            assert response.status_code == 200, response.text
        assert client.post(f"/api/delivery-picks/{task['id']}/submit").json()["status"] == "exception"

        _login(client, "admin")
        assert client.put(f"/api/deliveries/{ids['delivery']}/dispatch").status_code == 409
        assert client.post(f"/api/delivery-picks/{task['id']}/apply").status_code == 200
        dispatched = client.put(f"/api/deliveries/{ids['delivery']}/dispatch")
        assert dispatched.status_code == 409, dispatched.text
        assert "当前最多可送 100" in dispatched.json()["detail"]

    with factory() as db:
        assert [
            db.get(OrderItem, item_id).delivered_quantity
            for item_id in ids["order_items"]
        ] == [0, 0]


def test_pushed_task_does_not_block_direct_dispatch(pick_app) -> None:
    app, _, ids, _ = pick_app
    with TestClient(app) as client:
        _login(client, "admin")
        _create_task(client, ids["delivery"])
        dispatched = client.put(f"/api/deliveries/{ids['delivery']}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        assert dispatched.json()["pick_task"]["status"] == "dispatched"


def test_editing_delivery_invalidates_old_pick_snapshot(pick_app) -> None:
    app, _, ids, _ = pick_app
    with TestClient(app) as client:
        _login(client, "admin")
        _create_task(client, ids["delivery"])
        edited = client.put(
            f"/api/deliveries/{ids['delivery']}",
            json={
                "delivery_date": "2026-07-18",
                "items": [
                    {"order_item_id": ids["order_items"][0], "delivered_quantity": 90},
                    {"order_item_id": ids["order_items"][1], "delivered_quantity": 50},
                ],
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["pick_task"] is None
        assert client.get("/api/delivery-picks").json()["items"] == []


def test_n083_location_first_plan_and_one_click_normal_completion(pick_app) -> None:
    from app.models.delivery import Delivery, DeliveryPickTaskItem
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        InventoryPallet,
        InventoryPalletItem,
        InventoryReservation,
        WarehouseLocation,
    )
    from app.services.warehouse_inventory import manual_finished_in

    app, factory, ids, operation_log = pick_app
    with factory() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        order_items = [
            db.get(OrderItem, item_id) for item_id in ids["order_items"]
        ]
        locations = [
            WarehouseLocation(
                location_code="B2-L01",
                location_name="二楼B区01",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="B2",
                sort_order=10,
                placement_status="placed",
            ),
            WarehouseLocation(
                location_code="E1-L09",
                location_name="三楼E1区09",
                warehouse_type="finished",
                warehouse_floor=3,
                area_code="E1",
                sort_order=20,
                placement_status="placed",
            ),
        ]
        db.add_all(locations)
        db.flush()
        db.add(
            Floor3LocationLayout(
                location_id=locations[0].id,
                left_pct=Decimal("12"),
                top_pct=Decimal("18"),
                width_pct=Decimal("8"),
                height_pct=Decimal("7"),
                source_type="manual",
                created_by=admin.id,
            )
        )
        lots = []
        for index, (order_item, location, quantity) in enumerate(
            zip(order_items, locations, (60, 50), strict=True),
            start=1,
        ):
            lot = manual_finished_in(
                db,
                customer_id=ids["customer"],
                product_id=order_item.product_id,
                location_id=location.id,
                quantity=quantity,
                stock_date=date(2026, 7, 18),
                source_type="manual",
                remarks=None,
                operator_id=admin.id,
                idempotency_key=f"n083-lot-{index}",
            )
            lot.quantity_available -= quantity
            lot.quantity_reserved += quantity
            db.add(
                InventoryReservation(
                    reservation_number=f"RSV-N083-{index}",
                    inventory_lot_id=lot.id,
                    reservation_type="finished_order",
                    order_id=ids["order"],
                    order_item_id=order_item.id,
                    reserved_stock_quantity=quantity,
                    credited_requirement_quantity=quantity,
                    status="active",
                    reserved_by=admin.id,
                    reservation_group_key=f"n083-group-{index}",
                    idempotency_key=f"n083-reservation-{index}",
                )
            )
            product = db.get(Product, order_item.product_id)
            pallet = InventoryPallet(
                pallet_code=f"PLT-N083-{index}",
                location_id=location.id,
                status="active",
                is_current=True,
                needs_relocation=index == 2,
                created_by=admin.id,
            )
            db.add(pallet)
            db.flush()
            db.add(
                InventoryPalletItem(
                    pallet_id=pallet.id,
                    inventory_lot_id=lot.id,
                    customer_id=ids["customer"],
                    product_id=product.id,
                    inventory_code=product.product_code,
                    product_name=product.product_name,
                    item_type="finished",
                    quantity=quantity,
                    unit="boxes",
                    match_status="matched",
                    created_by=admin.id,
                )
            )
            lots.append(lot)
        db.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        task = _create_task(client, ids["delivery"])
        groups = task["location_groups"]
        assert [group["source_type"] for group in groups] == [
            "finished_inventory",
            "production_direct",
            "finished_inventory",
        ]
        assert groups[0]["location_code"] == "B2-L01"
        assert groups[0]["pallet_code"] == "PLT-N083-1"
        assert groups[0]["lines"][0]["pick_quantity"] == 60
        assert groups[0]["recommended_sequence"] == 1
        assert groups[0]["map_status"] == "mapped"
        assert groups[0]["map_point"]["left_pct"] == 12.0
        assert groups[1]["label"] == "生产区直接拿货"
        assert groups[1]["map_status"] == "text_only"
        assert groups[1]["lines"][0]["pick_quantity"] == 40
        assert groups[2]["location_code"] == "E1-L09"
        assert groups[2]["pallet_code"] == "PLT-N083-2"
        assert groups[2]["needs_relocation"] is True
        assert groups[2]["map_status"] == "text_only"
        assert task["location_plan_complete"] is True

        _login(client, "delivery_picker")
        completed = client.post(
            f"/api/delivery-picks/{task['id']}/complete-planned"
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "driver_confirmed"
        assert {
            (row["pick_status"], row["picked_quantity"])
            for row in completed.json()["items"]
        } == {("picked", 100), ("picked", 50)}
        repeated = client.post(
            f"/api/delivery-picks/{task['id']}/complete-planned"
        )
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "driver_confirmed"

    with factory() as db:
        assert db.get(Delivery, ids["delivery"]).status == "pending"
        saved = db.scalars(
            select(DeliveryPickTaskItem).order_by(DeliveryPickTaskItem.id)
        ).all()
        assert [(row.status, row.picked_quantity) for row in saved] == [
            ("picked", 100),
            ("picked", 50),
        ]
        logs = db.scalars(
            select(operation_log).where(
                operation_log.action == "COMPLETE_PICK_TASK_PLANNED"
            )
        ).all()
        assert len(logs) == 1
