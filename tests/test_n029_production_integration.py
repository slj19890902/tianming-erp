from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def n029_delivery_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deliveries import (
        order_actions_router,
        router as deliveries_router,
    )
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.api.incoming import router as incoming_router
    from app.api.orders import router as orders_router
    from app.api.production import router as production_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import (
        ProductionCompletion,
        ProductionCompletionBatch,
        ProductionTask,
    )
    from app.models.user import User
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        InventoryReservation,
        OrderItemSemiRequirement,
        WarehouseLocation,
    )

    engine = create_sqlite_engine(tmp_path / "n029-integration.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.utcnow()
    with factory() as db:
        user = User(
            username="n029-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="N029管理员",
            display_name="N029管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=2901,
            customer_code="天华",
            name="苏州天华N029测试客户",
            payment_term_days=0,
            credit_limit=0,
        )
        location = WarehouseLocation(
            location_code="N029-LEGACY",
            location_name="N029兼容库位",
            warehouse_type="shared",
        )
        temporary_location = WarehouseLocation(
            location_code="F12-P03",
            location_name="F12-P03",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="F12",
            storage_type="temporary_aisle",
            is_temporary=True,
            source_version="V11",
        )
        return_location = WarehouseLocation(
            location_code="N029-RETURN-01",
            location_name="N029客户退回区",
            warehouse_type="finished",
            is_active=True,
        )
        db.add_all([user, customer, location, temporary_location, return_location])
        db.flush()

        item_specs = (
            ("legacy_received", 50, "received"),
            ("legacy_finished", 40, "pending"),
            ("legacy_semi", 30, "pending"),
            ("task_completed", 100, "received"),
            ("task_pending", 100, "received"),
        )
        items: dict[str, OrderItem] = {}
        orders: dict[str, Order] = {}
        for index, (key, quantity, material_status) in enumerate(item_specs, start=1):
            product = Product(
                customer_id=customer.id,
                product_code=f"N029-{key}",
                customer_material_code=f"N029-{key}",
                product_name=f"N029 {key}",
                box_category="normal",
                box_style="普通箱",
            )
            db.add(product)
            db.flush()
            order = Order(
                order_number=f"N029-ORDER-{index}",
                customer_id=customer.id,
                order_date=date.today(),
                delivery_date=date.today(),
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=Decimal(quantity),
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_sequence=1,
                item_order_number=f"{order.order_number}-001",
                quantity=quantity,
                delivered_quantity=0,
                unit_price=Decimal("1"),
                subtotal=Decimal(quantity),
                material_status=material_status,
                requisition_status="已入库" if material_status == "received" else "未报料",
                snapshot_product_code=product.product_code,
                snapshot_product_name=product.product_name,
            )
            db.add(item)
            db.flush()
            items[key] = item
            orders[key] = order

        finished_lot = InventoryLot(
            lot_number="N029-LEGACY-FG",
            inventory_type="finished",
            warehouse_location_id=location.id,
            quantity_available=0,
            quantity_reserved=40,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date.today(),
            last_movement_at=now,
            version=1,
        )
        semi_lot = InventoryLot(
            lot_number="N029-LEGACY-SI",
            inventory_type="semi_finished",
            warehouse_location_id=location.id,
            quantity_available=0,
            quantity_reserved=30,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="manual",
            stock_date=date.today(),
            last_movement_at=now,
            version=1,
        )
        db.add_all([finished_lot, semi_lot])
        db.flush()
        db.add(
            FinishedGoodsInventoryDetail(
                inventory_lot_id=finished_lot.id,
                owner_customer_id=customer.id,
                owner_customer_name_snapshot=customer.name,
                is_general=False,
                product_id=items["legacy_finished"].product_id,
                inventory_code_snapshot=items["legacy_finished"].snapshot_product_code,
                product_name_snapshot=items["legacy_finished"].snapshot_product_name,
            )
        )
        semi_requirement = OrderItemSemiRequirement(
            order_item_id=items["legacy_semi"].id,
            customer_id=customer.id,
            component_type="whole",
            board_length_mm=800,
            board_width_mm=600,
            material_code_snapshot="A416D",
            normalized_material_code="A416D",
            flute_type="B",
            pieces_per_box=1,
            stock_yield_per_sheet=1,
            required_piece_quantity=30,
        )
        db.add(semi_requirement)
        db.flush()
        db.add_all(
            [
                InventoryReservation(
                    reservation_number="N029-RS-FG",
                    inventory_lot_id=finished_lot.id,
                    reservation_type="finished_order",
                    order_id=orders["legacy_finished"].id,
                    order_item_id=items["legacy_finished"].id,
                    reserved_stock_quantity=40,
                    credited_requirement_quantity=40,
                    consumed_stock_quantity=0,
                    released_stock_quantity=0,
                    consumed_requirement_quantity=0,
                    released_requirement_quantity=0,
                    status="active",
                    reservation_group_key="N029-RG-FG",
                    idempotency_key="N029-RS-FG",
                ),
                InventoryReservation(
                    reservation_number="N029-RS-SI",
                    inventory_lot_id=semi_lot.id,
                    reservation_type="semi_order",
                    order_id=orders["legacy_semi"].id,
                    order_item_id=items["legacy_semi"].id,
                    semi_requirement_id=semi_requirement.id,
                    reserved_stock_quantity=30,
                    credited_requirement_quantity=30,
                    yield_factor=1,
                    consumed_stock_quantity=0,
                    released_stock_quantity=0,
                    consumed_requirement_quantity=0,
                    released_requirement_quantity=0,
                    status="active",
                    reservation_group_key="N029-RG-SI",
                    idempotency_key="N029-RS-SI",
                ),
            ]
        )

        completed_task = ProductionTask(
            order_item_id=items["task_completed"].id,
            status="completed",
            planned_quantity=100,
            finished_coverage_snapshot=0,
            readiness_basis="material_received",
            ready_at=now,
            version=2,
        )
        pending_task = ProductionTask(
            order_item_id=items["task_pending"].id,
            status="pending",
            planned_quantity=100,
            finished_coverage_snapshot=0,
            readiness_basis="material_received",
            ready_at=now,
            version=1,
        )
        batch = ProductionCompletionBatch(
            idempotency_key="N029-COMPLETE-100",
            request_hash="a" * 64,
            item_count=1,
            completed_by=user.id,
            completed_at=now,
        )
        db.add_all([completed_task, pending_task, batch])
        db.flush()
        completion = ProductionCompletion(
            batch_id=batch.id,
            task_id=completed_task.id,
            order_item_id=items["task_completed"].id,
            expected_version=1,
            quantity=100,
            initial_disposition="direct",
            completed_by=user.id,
            completed_at=now,
        )
        db.add(completion)
        db.flush()
        db.commit()
        ids = {
            "customer": customer.id,
            "user": user.id,
            "temporary_location": temporary_location.id,
            "return_location": return_location.id,
            "completion_task_completed": completion.id,
            **{key: item.id for key, item in items.items()},
            **{f"order_{key}": order.id for key, order in orders.items()},
            **{f"product_{key}": item.product_id for key, item in items.items()},
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(order_actions_router, prefix="/api/orders")
    app.include_router(incoming_router, prefix="/api/incoming")
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(finance_router, prefix="/api/finance")
    app.include_router(production_router, prefix="/api/production")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids
    finally:
        engine.dispose()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "n029-admin", "password": "123456"},
    )
    assert response.status_code == 200, response.text


def test_legacy_delivery_eligibility_and_unfinished_task_gate(n029_delivery_app) -> None:
    from app.api.dashboard import _delivery_ready_filter
    from app.api.deliveries import _delivery_remaining_quantity, _pending_query
    from app.models.order import Order, OrderItem

    _app, factory, ids = n029_delivery_app
    with factory() as db:
        pending_ids = {
            row._mapping["order_item_id"]
            for row in db.execute(_pending_query(customer_id=ids["customer"]))
        }
        dashboard_ids = set(
            db.scalars(
                select(OrderItem.id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(_delivery_ready_filter())
            ).all()
        )
        assert _delivery_remaining_quantity(db, db.get(OrderItem, ids["legacy_received"])) == 50
        assert _delivery_remaining_quantity(db, db.get(OrderItem, ids["legacy_finished"])) == 40
        assert _delivery_remaining_quantity(db, db.get(OrderItem, ids["legacy_semi"])) == 30

    legacy_ids = {
        ids["legacy_received"],
        ids["legacy_finished"],
        ids["legacy_semi"],
    }
    assert legacy_ids <= pending_ids
    assert legacy_ids <= dashboard_ids
    assert ids["task_completed"] in pending_ids
    assert ids["task_pending"] not in pending_ids
    assert ids["task_pending"] not in dashboard_ids


def test_completed_task_rejects_over_delivery_without_physical_inventory(
    n029_delivery_app,
) -> None:
    app, factory, ids = n029_delivery_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 102,
                    }
                ],
            },
        )
        assert created.status_code == 409, created.text
        assert "100" in created.json()["detail"]


def _prepare_103_finished_stock(factory, ids) -> int:
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        InventoryReservation,
    )

    with factory() as db:
        item = db.get(OrderItem, ids["task_completed"])
        order = db.get(Order, item.order_id)
        product = db.get(Product, item.product_id)
        customer = db.get(Customer, order.customer_id)
        completion = db.get(
            ProductionCompletion,
            ids["completion_task_completed"],
        )
        completion.quantity = 103
        completion.material_input_quantity = 103
        completion.planned_output_quantity = 103
        completion.actual_output_quantity = 103
        completion.defective_quantity = 0
        completion.order_reserved_quantity = 100
        completion.direct_delivery_quantity = 0
        completion.stock_quantity = 103
        completion.surplus_finished_quantity = 3
        completion.initial_disposition = "stock"
        completion.warehouse_location_id = ids["temporary_location"]
        lot = InventoryLot(
            lot_number="N029-OVER-103",
            inventory_type="finished",
            warehouse_location_id=ids["temporary_location"],
            quantity_available=3,
            quantity_reserved=100,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="production_surplus",
            source_ref_type="production_completion",
            source_ref_id=completion.id,
            stock_date=date.today(),
            last_movement_at=datetime.utcnow(),
            version=2,
        )
        db.add(lot)
        db.flush()
        db.add(
            FinishedGoodsInventoryDetail(
                inventory_lot_id=lot.id,
                owner_customer_id=customer.id,
                owner_customer_name_snapshot=customer.name,
                is_general=False,
                product_id=product.id,
                inventory_code_snapshot=product.product_code,
                product_name_snapshot=product.product_name,
            )
        )
        db.add(
            InventoryReservation(
                reservation_number="N029-OVER-ORDER-100",
                inventory_lot_id=lot.id,
                reservation_type="finished_order",
                order_id=order.id,
                order_item_id=item.id,
                reserved_stock_quantity=100,
                credited_requirement_quantity=100,
                yield_factor=1,
                status="active",
                idempotency_key="N029-OVER-ORDER-100",
            )
        )
        completion.inventory_lot_id = lot.id
        db.commit()
        return lot.id


def _bind_finished_lot_to_test_pallet(factory, lot_id: int) -> int:
    from app.models.warehouse_inventory import InventoryLot
    from app.services.floor3_locations import bind_finished_lot_to_floor3_pallet

    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert lot is not None
        pallet = bind_finished_lot_to_floor3_pallet(
            db,
            lot=lot,
            operator_id=1,
            pallet_code=f"N029-PALLET-{lot_id}",
            require_empty_pallet=True,
        )
        db.commit()
        return int(pallet.id)


def test_delivery_100_finishes_order_and_keeps_three_customer_surplus(
    n029_delivery_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryPallet

    app, factory, ids = n029_delivery_app
    lot_id = _prepare_103_finished_stock(factory, ids)
    pallet_id = _bind_finished_lot_to_test_pallet(factory, lot_id)
    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/deliveries/pending_items")
        row = next(
            item
            for item in pending.json()["items"]
            if item["order_item_id"] == ids["task_completed"]
        )
        assert row["order_remaining_quantity"] == 100
        assert row["deliverable_quantity"] == 103
        assert row["over_delivery_quantity"] == 3
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 100,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        dispatched = client.put(f"/api/deliveries/{created.json()['id']}/dispatch")
        assert dispatched.status_code == 200, dispatched.text

    with factory() as db:
        item = db.get(OrderItem, ids["task_completed"])
        lot = db.get(InventoryLot, lot_id)
        pallet = db.get(InventoryPallet, pallet_id)
        assert item.quantity == 100
        assert item.delivered_quantity == 100
        assert lot.quantity_consumed == 100
        assert lot.quantity_available == 3
        assert lot.quantity_reserved == 0
        assert pallet is not None
        assert pallet.is_current is True
        assert pallet.location_id == ids["temporary_location"]


def test_delivery_does_not_release_pallet_with_damaged_goods(
    n029_delivery_app,
) -> None:
    from app.models.warehouse_inventory import InventoryLot, InventoryPallet

    app, factory, ids = n029_delivery_app
    lot_id = _prepare_103_finished_stock(factory, ids)
    pallet_id = _bind_finished_lot_to_test_pallet(factory, lot_id)
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert lot is not None
        lot.quantity_available = 2
        lot.quantity_damaged = 1
        db.commit()

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 102,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        dispatched = client.put(f"/api/deliveries/{created.json()['id']}/dispatch")
        assert dispatched.status_code == 200, dispatched.text

    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        pallet = db.get(InventoryPallet, pallet_id)
        assert lot is not None
        assert lot.quantity_available == 0
        assert lot.quantity_reserved == 0
        assert lot.quantity_damaged == 1
        assert pallet is not None
        assert pallet.is_current is True
        assert pallet.location_id == ids["temporary_location"]


def test_authorized_over_delivery_103_consumes_stock_and_records_three(
    n029_delivery_app,
) -> None:
    from app.models.delivery import DeliveryItem
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import (
        InventoryLocationMovement,
        InventoryLot,
        InventoryPallet,
        InventoryReservation,
    )

    app, factory, ids = n029_delivery_app
    lot_id = _prepare_103_finished_stock(factory, ids)
    pallet_id = _bind_finished_lot_to_test_pallet(factory, lot_id)
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 103,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["warnings"][0]["code"] == "OVER_DELIVERY"
        assert created.json()["items"][0]["over_delivery_quantity"] == 3
        dispatched = client.put(f"/api/deliveries/{created.json()['id']}/dispatch")
        assert dispatched.status_code == 200, dispatched.text

    with factory() as db:
        item = db.get(OrderItem, ids["task_completed"])
        lot = db.get(InventoryLot, lot_id)
        pallet = db.get(InventoryPallet, pallet_id)
        line = db.scalar(
            select(DeliveryItem).where(
                DeliveryItem.order_item_id == ids["task_completed"]
            )
        )
        surplus_reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == ids["task_completed"],
                InventoryReservation.reservation_type
                == "finished_surplus_delivery",
            )
        )
        assert item.quantity == 100
        assert item.delivered_quantity == 103
        assert line.ordered_quantity_snapshot == 100
        assert line.over_delivery_quantity == 3
        assert lot.quantity_consumed == 103
        assert lot.quantity_available == 0
        assert lot.quantity_reserved == 0
        assert surplus_reservation.consumed_stock_quantity == 3
        assert pallet is not None
        assert pallet.is_current is False
        assert pallet.location_id is None
        release = db.scalar(
            select(InventoryLocationMovement).where(
                InventoryLocationMovement.pallet_id == pallet_id,
                InventoryLocationMovement.idempotency_key
                == f"delivery-{line.delivery_id}-auto-release-pallet-{pallet_id}",
            )
        )
        assert release is not None
        assert release.movement_type == "clear"
        assert release.from_location_id == ids["temporary_location"]


def test_new_finished_in_reuses_released_empty_pallet(
    n029_delivery_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import (
        InventoryLocationMovement,
        InventoryLot,
        InventoryPallet,
        InventoryPalletItem,
    )
    from app.services.warehouse_inventory import manual_finished_in

    app, factory, ids = n029_delivery_app
    old_lot_id = _prepare_103_finished_stock(factory, ids)
    pallet_id = _bind_finished_lot_to_test_pallet(factory, old_lot_id)
    with factory() as db:
        pallet = db.get(InventoryPallet, pallet_id)
        assert pallet is not None
        pallet_code = pallet.pallet_code

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 103,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        delivery_id = int(created.json()["id"])
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text

    with factory() as db:
        item = db.get(OrderItem, ids["task_completed"])
        assert item is not None
        new_lot = manual_finished_in(
            db,
            customer_id=ids["customer"],
            product_id=item.product_id,
            location_id=ids["temporary_location"],
            quantity=5,
            stock_date=date.today(),
            source_type="manual",
            remarks="P1-16E-4 空栈板复用测试",
            operator_id=1,
            idempotency_key="P1-16E-4-REUSE-ONCE",
            require_empty_pallet=True,
        )
        db.commit()
        new_lot_id = int(new_lot.id)

    with factory() as db:
        old_lot = db.get(InventoryLot, old_lot_id)
        new_lot = db.get(InventoryLot, new_lot_id)
        pallet = db.get(InventoryPallet, pallet_id)
        pallet_items = list(
            db.scalars(
                select(InventoryPalletItem).where(
                    InventoryPalletItem.pallet_id == pallet_id
                )
            )
        )
        movements = list(
            db.scalars(
                select(InventoryLocationMovement)
                .where(InventoryLocationMovement.pallet_id == pallet_id)
                .order_by(InventoryLocationMovement.id)
            )
        )
        assert old_lot is not None and old_lot.pallet_item is None
        assert new_lot is not None and new_lot.pallet_item is not None
        assert new_lot.pallet_item.pallet_id == pallet_id
        assert pallet is not None
        assert pallet.pallet_code == pallet_code
        assert pallet.is_current is True
        assert pallet.status == "active"
        assert pallet.location_id == ids["temporary_location"]
        assert pallet.closed_at is None
        assert len(pallet_items) == 1
        assert pallet_items[0].inventory_lot_id == new_lot_id
        assert [movement.movement_type for movement in movements][-2:] == [
            "clear",
            "move",
        ]
        assert movements[-1].from_location_id is None
        assert movements[-1].to_location_id == ids["temporary_location"]

        repeated = manual_finished_in(
            db,
            customer_id=ids["customer"],
            product_id=item.product_id,
            location_id=ids["temporary_location"],
            quantity=5,
            stock_date=date.today(),
            source_type="manual",
            remarks="P1-16E-4 空栈板复用测试",
            operator_id=1,
            idempotency_key="P1-16E-4-REUSE-ONCE",
            require_empty_pallet=True,
        )
        assert repeated.id == new_lot_id
        db.commit()
        assert len(
            list(
                db.scalars(
                    select(InventoryLocationMovement).where(
                        InventoryLocationMovement.pallet_id == pallet_id
                    )
                )
            )
        ) == len(movements)


def test_cancel_full_delivery_restores_auto_released_pallet(
    n029_delivery_app,
) -> None:
    from app.models.delivery import Delivery
    from app.models.warehouse_inventory import (
        InventoryLocationMovement,
        InventoryLot,
        InventoryPallet,
    )

    app, factory, ids = n029_delivery_app
    lot_id = _prepare_103_finished_stock(factory, ids)
    pallet_id = _bind_finished_lot_to_test_pallet(factory, lot_id)
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 103,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        delivery_id = int(created.json()["id"])
        assert client.put(f"/api/deliveries/{delivery_id}/dispatch").status_code == 200
        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text

    with factory() as db:
        delivery = db.get(Delivery, delivery_id)
        lot = db.get(InventoryLot, lot_id)
        pallet = db.get(InventoryPallet, pallet_id)
        assert delivery is not None and delivery.status == "pending"
        assert lot is not None
        assert lot.quantity_available + lot.quantity_reserved == 103
        assert lot.quantity_consumed == 0
        assert pallet is not None
        assert pallet.is_current is True
        assert pallet.location_id == ids["temporary_location"]
        restored = db.scalar(
            select(InventoryLocationMovement).where(
                InventoryLocationMovement.idempotency_key
                == f"delivery-{delivery_id}-auto-release-pallet-{pallet_id}:restore"
            )
        )
        assert restored is not None
        assert restored.movement_type == "move"


def test_cancel_full_delivery_fails_closed_when_original_location_is_reused(
    n029_delivery_app,
) -> None:
    from app.models.delivery import Delivery
    from app.models.warehouse_inventory import InventoryLot, InventoryPallet

    app, factory, ids = n029_delivery_app
    lot_id = _prepare_103_finished_stock(factory, ids)
    pallet_id = _bind_finished_lot_to_test_pallet(factory, lot_id)
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 103,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        delivery_id = int(created.json()["id"])
        assert client.put(f"/api/deliveries/{delivery_id}/dispatch").status_code == 200
        with factory() as db:
            db.add(
                InventoryPallet(
                    pallet_code="N029-REUSED-LOCATION",
                    location_id=ids["temporary_location"],
                    status="active",
                    is_current=True,
                    needs_relocation=True,
                    created_by=1,
                    updated_by=1,
                )
            )
            db.commit()
        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel")
        assert cancelled.status_code == 409, cancelled.text
        assert "原库位已被其他栈板占用" in cancelled.json()["detail"]

    with factory() as db:
        delivery = db.get(Delivery, delivery_id)
        lot = db.get(InventoryLot, lot_id)
        pallet = db.get(InventoryPallet, pallet_id)
        assert delivery is not None and delivery.status == "dispatched"
        assert lot is not None
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (
            0,
            0,
            103,
        )
        assert pallet is not None
        assert pallet.is_current is False
        assert pallet.location_id is None


def test_over_delivery_flows_through_receipt_statement_export_and_invoice(
    n029_delivery_app,
) -> None:
    from openpyxl import load_workbook

    from app.models.customer import Customer

    app, factory, ids = n029_delivery_app
    _prepare_103_finished_stock(factory, ids)
    with factory() as db:
        customer = db.get(Customer, ids["customer"])
        customer.statement_cycle_start_day = 1
        db.commit()

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 103,
                        "over_delivery_confirmed": True,
                        "over_delivery_reason": "客户确认接收全部103个合格品",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        delivery_id = created.json()["id"]
        delivery_item_id = created.json()["items"][0]["id"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text

        receipt = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": delivery_id,
                "actual_received_date": date.today().isoformat(),
                "items": [
                    {
                        "delivery_item_id": delivery_item_id,
                        "actual_received_quantity": 103,
                    }
                ],
            },
        )
        assert receipt.status_code == 201, receipt.text

        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": ids["customer"],
                "statement_month": date.today().strftime("%Y-%m"),
                "delivery_ids": [delivery_id],
            },
        )
        assert statement.status_code == 201, statement.text
        statement_id = statement.json()["id"]

        detail = client.get(f"/api/finance/statements/{statement_id}")
        assert detail.status_code == 200, detail.text
        line = detail.json()["items"][0]
        assert line["ordered_quantity_snapshot"] == 100
        assert line["actual_delivery_quantity"] == 103
        assert line["over_delivery_quantity"] == 3
        assert line["actual_received_quantity"] == 103

        exported = client.get(f"/api/finance/statements/{statement_id}/export")
        assert exported.status_code == 200, exported.text
        workbook = load_workbook(BytesIO(exported.content), read_only=True)
        sheet = workbook.active
        headers = [cell.value for cell in sheet[5]]
        values = [cell.value for cell in sheet[6]]
        assert headers[8:12] == [
            "订单数量",
            "实际送货数量",
            "超订单数量",
            "实际签收数量",
        ]
        assert values[8:12] == [100, 103, 3, 103]

        invoice = client.post(
            "/api/finance/invoices",
            json={
                "statement_id": statement_id,
                "invoice_number": "N029-OVER-103-INVOICE",
                "invoice_date": date.today().isoformat(),
                "invoice_amount": "103.00",
            },
        )
        assert invoice.status_code == 201, invoice.text
        assert invoice.json()["statement_id"] == statement_id
        assert Decimal(str(invoice.json()["invoice_amount"])) == Decimal("103.00")


def test_user_without_over_delivery_permission_is_rejected(
    n029_delivery_app,
) -> None:
    from app.core.security import hash_password
    from app.models.access_control import UserPermissionOverride
    from app.models.user import User

    app, factory, ids = n029_delivery_app
    _prepare_103_finished_stock(factory, ids)
    with factory() as db:
        admin = db.get(User, ids["user"])
        user = User(
            username="n029-delivery-no-over",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="送货测试员",
            must_change_password=False,
            customer_access_mode="all",
        )
        db.add(user)
        db.flush()
        db.add(
            UserPermissionOverride(
                user_id=user.id,
                permission_code="deliveries.execute",
                is_allowed=True,
                granted_by=admin.id,
            )
        )
        db.commit()

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "n029-delivery-no-over", "password": "123456"},
        )
        assert login.status_code == 200, login.text
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 103,
                        "over_delivery_confirmed": True,
                        "over_delivery_reason": "测试无权限超量送货",
                    }
                ],
            },
        )
        assert created.status_code == 403, created.text
        assert "超量送货权限" in created.json()["detail"]


def test_dispatch_locks_order_before_order_item(n029_delivery_app) -> None:
    app, factory, ids = n029_delivery_app
    engine = factory.kw["bind"]
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(" ".join(statement.lower().split()))

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 100,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        event.listen(engine, "before_cursor_execute", record_sql)
        try:
            dispatched = client.put(
                f"/api/deliveries/{created.json()['id']}/dispatch"
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_sql)

    assert dispatched.status_code == 200, dispatched.text
    order_lock_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("update sales_orders set")
        and "status=sales_orders.status" in statement.replace(" ", "")
    )
    item_lock_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("update sales_order_items set")
    )
    delivery_lock_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("update sales_deliveries set")
    )
    assert order_lock_index < delivery_lock_index < item_lock_index


def test_workflow_rollback_rejects_dispatched_inventory_allocation_cleanly(
    n029_delivery_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import DeliveryInventoryAllocation

    app, factory, ids = n029_delivery_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["legacy_finished"],
                        "delivered_quantity": 40,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        delivery_id = created.json()["id"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        rollback = client.put(
            f"/api/orders/{ids['order_legacy_finished']}/rollback-workflow",
            json={"reason": "验证库存发货不可直接删除"},
        )

    assert rollback.status_code == 409
    assert "先在送货管理中撤销发货并恢复库存" in rollback.json()["detail"]
    with factory() as db:
        assert db.get(OrderItem, ids["legacy_finished"]).delivered_quantity == 40
        assert db.get(Delivery, delivery_id).status == "dispatched"
        delivery_item_ids = list(
            db.scalars(
                select(DeliveryItem.id).where(
                    DeliveryItem.delivery_id == delivery_id
                )
            ).all()
        )
        assert len(delivery_item_ids) == 1
        assert db.scalar(
            select(DeliveryInventoryAllocation.id).where(
                DeliveryInventoryAllocation.delivery_item_id.in_(delivery_item_ids)
            )
        ) is not None


def test_dispatched_then_zero_receipt_still_blocks_direct_completion_transfer(
    n029_delivery_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.production import ProductionStockTransfer
    from app.models.warehouse_inventory import InventoryLot

    app, factory, ids = n029_delivery_app
    completion_id = ids["completion_task_completed"]
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 100,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        delivery_item_id = created.json()["items"][0]["id"]
        dispatched = client.put(
            f"/api/deliveries/{created.json()['id']}/dispatch"
        )
        assert dispatched.status_code == 200, dispatched.text
        receipt = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": created.json()["id"],
                "actual_received_date": date.today().isoformat(),
                "items": [
                    {
                        "delivery_item_id": delivery_item_id,
                        "actual_received_quantity": 0,
                        "resolution_action": "continue_delivery",
                        "difference_reason": "整批短收继续待送",
                        "return_location_id": ids["return_location"],
                    }
                ],
            },
        )
        assert receipt.status_code == 201, receipt.text
        history = client.get("/api/production/completions")
        transfer = client.post(
            f"/api/production/completions/{completion_id}/stock-transfers",
            json={
                "idempotency_key": "dispatched-zero-receipt-transfer",
                "location_id": ids["temporary_location"],
            },
        )

    assert history.status_code == 200, history.text
    history_row = next(
        row for row in history.json()["items"] if row["id"] == completion_id
    )
    assert history_row["can_transfer_to_stock"] is False
    assert transfer.status_code == 409
    assert transfer.json()["detail"] == "已经发生真实发货，不能整批转库存"
    with factory() as db:
        assert db.get(OrderItem, ids["task_completed"]).delivered_quantity == 0
        assert db.scalar(
            select(ProductionStockTransfer.id).where(
                ProductionStockTransfer.completion_id == completion_id
            )
        ) is None
        assert db.scalar(
            select(InventoryLot.id).where(
                InventoryLot.source_ref_type == "production_completion",
                InventoryLot.source_ref_id == completion_id,
            )
        ) is None


def test_ordered_inventory_short_receipt_returns_to_location_and_reopens_order(
    n029_delivery_app,
) -> None:
    from app.models.finance import ReturnReceipt
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import (
        DeliveryInventoryAllocation,
        InventoryLot,
        InventoryReservation,
        OrderedFinishedReceiptReturn,
    )

    app, factory, ids = n029_delivery_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["legacy_finished"],
                        "delivered_quantity": 40,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        delivery_id = created.json()["id"]
        delivery_item_id = created.json()["items"][0]["id"]
        assert client.put(f"/api/deliveries/{delivery_id}/dispatch").status_code == 200

        missing_location = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": delivery_id,
                "actual_received_date": date.today().isoformat(),
                "items": [
                    {
                        "delivery_item_id": delivery_item_id,
                        "actual_received_quantity": 34,
                        "resolution_action": "continue_delivery",
                    }
                ],
            },
        )
        assert missing_location.status_code == 400, missing_location.text
        assert "实际存放库位" in missing_location.json()["detail"]

        receipt = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": delivery_id,
                "actual_received_date": date.today().isoformat(),
                "items": [
                    {
                        "delivery_item_id": delivery_item_id,
                        "actual_received_quantity": 34,
                        "resolution_action": "continue_delivery",
                        "return_location_id": ids["return_location"],
                    }
                ],
            },
        )
        assert receipt.status_code == 201, receipt.text
        receipt_id = receipt.json()["id"]
        line = receipt.json()["items"][0]
        assert line["return_location_id"] == ids["return_location"]
        assert line["requires_return_location"] is True
        pending = client.get("/api/deliveries/pending_items")
        assert pending.status_code == 200, pending.text
        pending_line = next(
            row
            for row in pending.json()["items"]
            if row["order_item_id"] == ids["legacy_finished"]
        )
        assert pending_line["remaining_quantity"] == 6

        with factory() as db:
            assert db.scalar(select(func.count()).select_from(ReturnReceipt)) == 1
            fact = db.scalar(select(OrderedFinishedReceiptReturn))
            assert fact is not None
            assert fact.quantity == 6
            assert fact.resolution_action == "continue_delivery"
            lot = db.get(InventoryLot, fact.return_inventory_lot_id)
            reservation = db.get(InventoryReservation, fact.reservation_id)
            source_lot = db.get(InventoryLot, fact.source_inventory_lot_id)
            source_allocation = db.get(
                DeliveryInventoryAllocation,
                fact.delivery_inventory_allocation_id,
            )
            source_reservation = db.get(
                InventoryReservation,
                source_allocation.reservation_id,
            )
            assert lot.warehouse_location_id == ids["return_location"]
            assert (lot.quantity_available, lot.quantity_reserved) == (0, 6)
            assert reservation.order_item_id == ids["legacy_finished"]
            assert reservation.status == "active"
            assert source_lot.quantity_consumed == 34
            assert source_allocation.reversed_stock_quantity == 6
            assert source_allocation.status == "partial"
            assert source_reservation.consumed_stock_quantity == 34
            assert source_reservation.released_stock_quantity == 6
            assert db.get(OrderItem, ids["legacy_finished"]).delivered_quantity == 34

        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text

    with factory() as db:
        fact = db.scalar(select(OrderedFinishedReceiptReturn))
        lot = db.get(InventoryLot, fact.return_inventory_lot_id)
        reservation = db.get(InventoryReservation, fact.reservation_id)
        source_lot = db.get(InventoryLot, fact.source_inventory_lot_id)
        source_allocation = db.get(
            DeliveryInventoryAllocation,
            fact.delivery_inventory_allocation_id,
        )
        source_reservation = db.get(
            InventoryReservation,
            source_allocation.reservation_id,
        )
        assert fact.status == "reconsumed"
        assert lot.status == "closed"
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (
            0,
            0,
            6,
        )
        assert reservation.status == "released"
        assert source_lot.quantity_consumed == 40
        assert source_allocation.reversed_stock_quantity == 0
        assert source_allocation.status == "active"
        assert source_reservation.consumed_stock_quantity == 40
        assert source_reservation.released_stock_quantity == 0
        assert db.get(OrderItem, ids["legacy_finished"]).delivered_quantity == 40


def test_accept_short_returns_customer_stock_and_statement_uses_received_quantity(
    n029_delivery_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import (
        InventoryLot,
        OrderedFinishedReceiptReturn,
    )

    app, factory, ids = n029_delivery_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["legacy_finished"],
                        "delivered_quantity": 40,
                    }
                ],
            },
        )
        delivery_id = created.json()["id"]
        delivery_item_id = created.json()["items"][0]["id"]
        assert client.put(f"/api/deliveries/{delivery_id}/dispatch").status_code == 200
        receipt = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": delivery_id,
                "actual_received_date": date.today().isoformat(),
                "items": [
                    {
                        "delivery_item_id": delivery_item_id,
                        "actual_received_quantity": 34,
                        "resolution_action": "accept_short",
                        "return_location_id": ids["return_location"],
                    }
                ],
            },
        )
        assert receipt.status_code == 201, receipt.text
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": ids["customer"],
                "statement_month": date.today().strftime("%Y-%m"),
                "delivery_ids": [delivery_id],
            },
        )
        assert statement.status_code == 201, statement.text
        assert statement.json()["total_receivable"] == "34.00"

    with factory() as db:
        fact = db.scalar(select(OrderedFinishedReceiptReturn))
        lot = db.get(InventoryLot, fact.return_inventory_lot_id)
        item = db.get(OrderItem, ids["legacy_finished"])
        assert fact.resolution_action == "accept_short"
        assert fact.reservation_id is None
        assert (lot.quantity_available, lot.quantity_reserved) == (6, 0)
        assert lot.finished_detail.owner_customer_id == ids["customer"]
        assert item.delivered_quantity == 34
        assert item.is_force_closed is True


def test_used_ordered_return_inventory_blocks_old_receipt_change(
    n029_delivery_app,
) -> None:
    from app.models.finance import ReturnReceipt
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import (
        InventoryLot,
        OrderedFinishedReceiptReturn,
    )

    app, factory, ids = n029_delivery_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["legacy_finished"],
                        "delivered_quantity": 40,
                    }
                ],
            },
        )
        delivery_id = created.json()["id"]
        delivery_item_id = created.json()["items"][0]["id"]
        assert client.put(f"/api/deliveries/{delivery_id}/dispatch").status_code == 200
        receipt = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": delivery_id,
                "actual_received_date": date.today().isoformat(),
                "items": [
                    {
                        "delivery_item_id": delivery_item_id,
                        "actual_received_quantity": 34,
                        "resolution_action": "accept_short",
                        "return_location_id": ids["return_location"],
                    }
                ],
            },
        )
        assert receipt.status_code == 201, receipt.text
        receipt_id = receipt.json()["id"]
        with factory() as db:
            fact = db.scalar(select(OrderedFinishedReceiptReturn))
            lot = db.get(InventoryLot, fact.return_inventory_lot_id)
            lot.quantity_available = 5
            lot.quantity_consumed = 1
            lot.version = 2
            db.commit()

        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")

    assert cancelled.status_code == 409, cancelled.text
    assert "已被移动或使用" in cancelled.json()["detail"]
    with factory() as db:
        assert db.get(ReturnReceipt, receipt_id).status == "confirmed"
        assert db.get(OrderItem, ids["legacy_finished"]).delivered_quantity == 34
        fact = db.scalar(select(OrderedFinishedReceiptReturn))
        assert fact.status == "active"


def test_continue_delivery_consumes_the_returned_lot_on_next_dispatch(
    n029_delivery_app,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        OrderedFinishedReceiptReturn,
    )

    app, factory, ids = n029_delivery_app
    with TestClient(app) as client:
        _login(client)
        first = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["legacy_finished"],
                        "delivered_quantity": 40,
                    }
                ],
            },
        )
        first_delivery_id = first.json()["id"]
        first_item_id = first.json()["items"][0]["id"]
        assert client.put(f"/api/deliveries/{first_delivery_id}/dispatch").status_code == 200
        receipt = client.post(
            "/api/finance/return_receipts",
            json={
                "delivery_id": first_delivery_id,
                "actual_received_date": date.today().isoformat(),
                "items": [
                    {
                        "delivery_item_id": first_item_id,
                        "actual_received_quantity": 34,
                        "resolution_action": "continue_delivery",
                        "return_location_id": ids["return_location"],
                    }
                ],
            },
        )
        assert receipt.status_code == 201, receipt.text
        second = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["legacy_finished"],
                        "delivered_quantity": 6,
                    }
                ],
            },
        )
        assert second.status_code == 201, second.text
        second_item = second.json()["items"][0]
        with factory() as db:
            fact = db.scalar(select(OrderedFinishedReceiptReturn))
            return_reservation_id = fact.reservation_id
            return_lot_id = fact.return_inventory_lot_id
        assert any(
            int(source.get("reservation_id") or 0) == return_reservation_id
            for source in second_item["inventory_sources"]
        )
        dispatched = client.put(f"/api/deliveries/{second.json()['id']}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        blocked = client.post(
            f"/api/finance/return_receipts/{receipt.json()['id']}/cancel"
        )

    assert blocked.status_code == 409, blocked.text
    with factory() as db:
        lot = db.get(InventoryLot, return_lot_id)
        reservation = db.get(InventoryReservation, return_reservation_id)
        assert (lot.quantity_reserved, lot.quantity_consumed) == (0, 6)
        assert reservation.consumed_stock_quantity == 6
        assert reservation.status == "consumed"


def test_transfer_before_dispatch_consumes_new_finished_reservation_once(
    n029_delivery_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.api.deliveries import _delivery_remaining_quantity

    app, factory, ids = n029_delivery_app
    completion_id = ids["completion_task_completed"]
    with TestClient(app) as client:
        _login(client)
        transferred = client.post(
            f"/api/production/completions/{completion_id}/stock-transfers",
            json={
                "idempotency_key": "transfer-before-dispatch",
                "location_id": ids["temporary_location"],
            },
        )
        assert transferred.status_code == 200, transferred.text
        force_closed = client.put(
            f"/api/orders/items/{ids['task_completed']}/force_close",
            json={"reason": "不应绕过生产库存预占结案"},
        )
        assert force_closed.status_code == 409, force_closed.text
        assert "生产完工成品库存预占" in force_closed.json()["detail"]
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_completed"],
                        "delivered_quantity": 100,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        dispatched = client.put(
            f"/api/deliveries/{created.json()['id']}/dispatch"
        )

    assert dispatched.status_code == 200, dispatched.text
    with factory() as db:
        reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == ids["task_completed"],
                InventoryReservation.reservation_type == "finished_order",
            )
        )
        assert reservation is not None
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        assert reservation.status == "consumed"
        assert reservation.consumed_stock_quantity == 100
        assert reservation.consumed_requirement_quantity == 100
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (
            0,
            0,
            100,
        )
        assert db.get(OrderItem, ids["task_completed"]).delivered_quantity == 100
        assert _delivery_remaining_quantity(
            db, db.get(OrderItem, ids["task_completed"])
        ) == 0


def test_unfinished_task_cannot_be_collected_for_delivery(n029_delivery_app) -> None:
    app, _factory, ids = n029_delivery_app
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/deliveries",
            json={
                "customer_id": ids["customer"],
                "items": [
                    {
                        "order_item_id": ids["task_pending"],
                        "delivered_quantity": 1,
                    }
                ],
            },
        )

    assert response.status_code == 400
    assert "当前可送数量为 0" in response.json()["detail"]


def test_tianhua_legacy_received_order_remains_available(n029_delivery_app) -> None:
    from app.services.tianhua_pre_delivery import RecognizedRow, preprocess_row

    _app, factory, ids = n029_delivery_app
    with factory() as db:
        result = preprocess_row(
            db,
            RecognizedRow(
                row_no=1,
                raw_text="N029-legacy_received 50",
                stock_code="N029-legacy_received",
                image_qty=50,
            ),
            pre_delivery_date=date.today(),
        )

    assert result["order_item_id"] == ids["legacy_received"]
    assert result["available_qty"] == 50
    assert result["system_pending_qty"] == 50
    assert result["status"] == "ok"


def test_new_order_creates_task_and_unfinished_quantity_edit_refreshes_it(
    n029_delivery_app,
) -> None:
    from app.models.production import ProductionTask

    app, factory, ids = n029_delivery_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/orders",
            json={
                "customer_id": ids["customer"],
                "customer_po": "N029-NEW-TASK",
                "order_date": date.today().isoformat(),
                "import_integrity_status": "ok",
                "items": [
                    {
                        "client_line_id": "N029-NEW-TASK-L1",
                        "product_id": ids["product_legacy_received"],
                        "quantity": 100,
                        "unit_price": "1.00",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        item = created.json()["items"][0]
        updated = client.put(
            f"/api/orders/items/{item['id']}",
            json={
                "quantity": 200,
                "unit_price": "1.00",
                "product_code": item["snapshot_product_code"],
                "product_name": item["snapshot_product_name"],
                "material": item.get("snapshot_material"),
                "specification": item.get("snapshot_spec"),
                "sync_product": False,
            },
        )

    assert updated.status_code == 200, updated.text
    assert updated.json()["quantity"] == 200
    with factory() as db:
        task = db.scalar(
            select(ProductionTask).where(ProductionTask.order_item_id == item["id"])
        )
        assert task is not None
        assert (task.status, task.planned_quantity) == ("waiting_material", 0)


def test_production_completion_blocks_meaning_changes_and_dangerous_lifecycle(
    n029_delivery_app,
) -> None:
    app, _factory, ids = n029_delivery_app
    item_id = ids["task_completed"]
    order_id = ids["order_task_completed"]
    with TestClient(app) as client:
        _login(client)
        changed_quantity = client.put(
            f"/api/orders/items/{item_id}",
            json={
                "quantity": 200,
                "unit_price": "1.00",
                "product_code": "N029-task_completed",
                "product_name": "N029 task_completed",
                "material": None,
                "specification": None,
                "sync_product": False,
            },
        )
        regressions = [
            client.put(
                f"/api/orders/{order_id}/status",
                json={"status": status, "remark": "N029状态保护"},
            )
            for status in ("pending_confirmation", "pending_production", "production")
        ]
        dead = client.put(
            f"/api/orders/{order_id}/status",
            json={"status": "dead", "remark": "N029状态保护"},
        )
        cancelled = client.put(
            f"/api/orders/{order_id}/status",
            json={"status": "cancelled", "remark": "N029状态保护"},
        )
        rollback = client.put(
            f"/api/orders/{order_id}/rollback-workflow",
            json={"reason": "N029撤回保护"},
        )
        delete_item = client.delete(f"/api/orders/items/{item_id}")
        delete_order = client.delete(f"/api/orders/{order_id}?confirm=true")
        delete_group = client.post(
            "/api/orders/group-delete",
            json={"order_ids": [order_id], "confirm": True},
        )
        manual_pending_delivery = client.put(
            f"/api/orders/{order_id}/status",
            json={"status": "pending_delivery", "remark": "保持下游状态"},
        )
        detail = client.get(f"/api/orders/{order_id}")

    protected = [
        changed_quantity,
        *regressions,
        dead,
        cancelled,
        rollback,
        delete_item,
        delete_order,
        delete_group,
    ]
    assert all(response.status_code == 409 for response in protected)
    assert all("生产" in response.json()["detail"] for response in protected)
    assert manual_pending_delivery.status_code == 409, manual_pending_delivery.text
    assert "真实业务单据自动判断" in manual_pending_delivery.json()["detail"]
    assert detail.status_code == 200, detail.text
    assert detail.json()["business_status"] == "pending_delivery"


def test_material_revert_blocks_completion_but_unfinished_task_returns_to_waiting(
    n029_delivery_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.production import ProductionTask

    app, factory, ids = n029_delivery_app
    with TestClient(app) as client:
        _login(client)
        blocked = client.put(
            f"/api/incoming/revert/{ids['task_completed']}",
            json={"reason": "不得撤销完工来料"},
        )
        reverted = client.put(
            f"/api/incoming/revert/{ids['task_pending']}",
            json={"reason": "正常撤销未完工来料"},
        )

    assert blocked.status_code == 409
    assert "生产完工事实" in blocked.json()["detail"]
    assert reverted.status_code == 200, reverted.text
    with factory() as db:
        item = db.get(OrderItem, ids["task_pending"])
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == ids["task_pending"]
            )
        )
        assert item.material_status == "pending"
        assert (task.status, task.planned_quantity) == ("waiting_material", 0)


def test_n005_receipt_revert_rechecks_completion_facts_after_order_lock(
    n029_delivery_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.order import OrderItem

    app, factory, ids = n029_delivery_app
    with factory() as db:
        receipt = IncomingReceipt(
            receipt_number="N029-LOCKED-REVERT",
            status="posted",
            received_at=datetime.utcnow(),
            received_by=ids["user"],
            idempotency_key="N029-LOCKED-REVERT",
        )
        db.add(receipt)
        db.flush()
        receipt_item = IncomingReceiptItem(
            receipt_id=receipt.id,
            order_id=ids["order_task_completed"],
            order_item_id=ids["task_completed"],
            planned_quantity=100,
            received_quantity=100,
            cumulative_received_quantity=100,
            variance_quantity=0,
            variance_type="matched",
            resolution_status="not_required",
            status="posted",
        )
        db.add(receipt_item)
        db.commit()
        receipt_item_id = receipt_item.id

    with TestClient(app) as client:
        _login(client)
        blocked = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={"reason": "旧快照不得越过生产事实"},
        )

    assert blocked.status_code == 409
    assert "生产完工事实" in blocked.json()["detail"]
    with factory() as db:
        assert db.get(IncomingReceiptItem, receipt_item_id).status == "posted"
        assert db.get(OrderItem, ids["task_completed"]).material_status == "received"


def test_create_app_repeated_calls_do_not_duplicate_production_routes() -> None:
    from app.main import create_app

    first = create_app()
    second = create_app()
    for application in (first, second):
        task_routes = [
            route
            for route in application.routes
            if getattr(route, "path", None) == "/api/production/tasks"
            and "GET" in getattr(route, "methods", set())
        ]
        assert len(task_routes) == 1
