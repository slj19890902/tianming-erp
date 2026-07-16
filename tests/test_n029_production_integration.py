from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
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
            password_hash=hash_password("RolePass123!"),
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
        db.add_all([user, customer, location, temporary_location])
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
        json={"username": "n029-admin", "password": "RolePass123!"},
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


def test_completed_task_allows_n005_over_delivery_warning_and_dispatch(
    n029_delivery_app,
) -> None:
    from app.models.order import OrderItem

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
        assert created.status_code == 201, created.text
        assert created.json()["warnings"][0]["code"] == "OVER_DELIVERY"
        dispatched = client.put(f"/api/deliveries/{created.json()['id']}/dispatch")

    assert dispatched.status_code == 200, dispatched.text
    with factory() as db:
        assert db.get(OrderItem, ids["task_completed"]).delivered_quantity == 102


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
        allowed = client.put(
            f"/api/orders/{order_id}/status",
            json={"status": "pending_delivery", "remark": "保持下游状态"},
        )

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
    assert allowed.status_code == 200, allowed.text


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
