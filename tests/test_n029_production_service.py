from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.api.production import router as production_router
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import (
    ProductionCompletion,
    ProductionCompletionBatch,
    ProductionStockTransfer,
    ProductionTask,
)
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    OrderItemSemiRequirement,
    SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
    WarehouseLocation,
)
from app.services.production_workflow import (
    CompletionCommand,
    ProductionWorkflowError,
    complete_production_batch,
    completion_batch_request_hash,
    create_or_refresh_production_task,
    list_production_completions,
    normalized_completion_output,
    production_input_quantity,
    production_output_quantity,
    production_pieces_per_box,
    production_ready_quantity,
    refresh_production_task,
)
from app.services.semi_finished_inventory import (
    SemiFinishedLotVersion,
    consume_delivery_item_inventory,
    reserve_semi_finished_inventory,
    reverse_delivery_item_inventory,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    release_finished_reservation,
    reserve_finished_inventory,
)


PASSWORD = "123456"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _order_item(
    *,
    order_id: int,
    product: Product,
    quantity: int,
    material_status: str,
    key: str,
) -> OrderItem:
    return OrderItem(
        order_id=order_id,
        product_id=product.id,
        item_order_number=f"{key}-001",
        item_sequence=1,
        quantity=quantity,
        delivered_quantity=0,
        unit_price=Decimal("1"),
        subtotal=Decimal(quantity),
        material_status=material_status,
        material_received_at=_now() if material_status == "received" else None,
        snapshot_product_name=f"快照-{key}",
        snapshot_product_code=f"SNAP-{key}",
        snapshot_spec="500x300x200",
        snapshot_material="K=A",
        snapshot_production_notes="先压线后模切",
        inventory_deducted_qty=0,
        requisition_qty=quantity,
        requisition_status="未报料",
        special_process="模切" if product.box_category == "die_cut" else "无",
        flute_type="A",
    )


def _add_case(
    db: Session,
    *,
    key: str,
    customer: Customer,
    product: Product,
    quantity: int,
    material_status: str = "received",
    task_status: str = "pending",
    planned_quantity: int | None = None,
    readiness_basis: str | None = "material_received",
) -> tuple[Order, OrderItem, ProductionTask]:
    order = Order(
        order_number=f"N029-{key}",
        customer_id=customer.id,
        order_date=date.today(),
        delivery_date=date.today(),
        status="production",
        payment_status="unpaid",
        total_amount=Decimal(quantity),
    )
    db.add(order)
    db.flush()
    item = _order_item(
        order_id=order.id,
        product=product,
        quantity=quantity,
        material_status=material_status,
        key=key,
    )
    db.add(item)
    db.flush()
    task = ProductionTask(
        order_item_id=item.id,
        status=task_status,
        planned_quantity=(
            quantity if planned_quantity is None else planned_quantity
        ),
        finished_coverage_snapshot=0,
        readiness_basis=readiness_basis,
        ready_at=_now() if task_status in {"pending", "not_required"} else None,
        version=1,
    )
    db.add(task)
    db.flush()
    return order, item, task


def _finished_lot(
    db: Session,
    *,
    key: str,
    customer: Customer,
    product: Product,
    location: WarehouseLocation,
    quantity_available: int,
    source_type: str = "manual",
    source_ref_type: str | None = None,
    source_ref_id: int | None = None,
) -> InventoryLot:
    lot = InventoryLot(
        lot_number=f"FG-{key}",
        inventory_type="finished",
        warehouse_location_id=location.id,
        quantity_available=quantity_available,
        quantity_reserved=0,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        unit="boxes",
        status="active",
        source_type=source_type,
        source_ref_type=source_ref_type,
        source_ref_id=source_ref_id,
        stock_date=date.today(),
        last_movement_at=_now(),
        version=1,
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
    db.flush()
    return lot


def _finished_reserved(
    db: Session,
    *,
    key: str,
    order: Order,
    item: OrderItem,
    customer: Customer,
    product: Product,
    location: WarehouseLocation,
    quantity: int,
) -> InventoryReservation:
    lot = _finished_lot(
        db,
        key=key,
        customer=customer,
        product=product,
        location=location,
        quantity_available=0,
    )
    lot.quantity_reserved = quantity
    lot.version = 2
    reservation = InventoryReservation(
        reservation_number=f"FR-{key}",
        inventory_lot_id=lot.id,
        reservation_type="finished_order",
        order_id=order.id,
        order_item_id=item.id,
        reserved_stock_quantity=quantity,
        credited_requirement_quantity=quantity,
        yield_factor=1,
        consumed_stock_quantity=0,
        released_stock_quantity=0,
        consumed_requirement_quantity=0,
        released_requirement_quantity=0,
        status="active",
        reserved_at=_now(),
        idempotency_key=f"fr-{key}",
    )
    db.add(reservation)
    db.flush()
    return reservation


def _semi_requirement(
    db: Session,
    *,
    key: str,
    item: OrderItem,
    customer: Customer,
    component: str,
    pieces_per_box: int = 1,
) -> OrderItemSemiRequirement:
    requirement = OrderItemSemiRequirement(
        order_item_id=item.id,
        customer_id=customer.id,
        component_type=component,
        board_length_mm=800,
        board_width_mm=600,
        material_code_snapshot="K=A",
        normalized_material_code="K=A",
        flute_type="A",
        pieces_per_box=pieces_per_box,
        stock_yield_per_sheet=1,
        required_piece_quantity=int(item.quantity) * pieces_per_box,
    )
    db.add(requirement)
    db.flush()
    return requirement


def _semi_reserved(
    db: Session,
    *,
    key: str,
    order: Order,
    item: OrderItem,
    customer: Customer,
    product: Product,
    location: WarehouseLocation,
    credit: int,
    component: str = "whole",
    pieces_per_box: int = 1,
    requirement: OrderItemSemiRequirement | None = None,
) -> tuple[InventoryReservation, InventoryLot, OrderItemSemiRequirement]:
    requirement = requirement or _semi_requirement(
        db,
        key=key,
        item=item,
        customer=customer,
        component=component,
        pieces_per_box=pieces_per_box,
    )
    lot = InventoryLot(
        lot_number=f"SF-{key}",
        inventory_type="semi_finished",
        warehouse_location_id=location.id,
        quantity_available=0,
        quantity_reserved=credit,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        unit="sheets",
        status="active",
        source_type="manual",
        stock_date=date.today(),
        last_movement_at=_now(),
        version=2,
    )
    db.add(lot)
    db.flush()
    db.add(
        SemiFinishedInventoryDetail(
            inventory_lot_id=lot.id,
            owner_customer_id=customer.id,
            owner_customer_name_snapshot=customer.name,
            material_code_snapshot="K=A",
            normalized_material_code="K=A",
            layer_count=3,
            flute_type="A",
            board_length_mm=800,
            board_width_mm=600,
            component_type=component,
            pieces_per_box=pieces_per_box,
            stock_yield_per_sheet=1,
            sheet_type="raw_board",
        )
    )
    db.add(
        SemiFinishedLotAllowedProduct(
            inventory_lot_id=lot.id,
            product_id=product.id,
            confirmed_at=_now(),
        )
    )
    reservation = InventoryReservation(
        reservation_number=f"SR-{key}",
        inventory_lot_id=lot.id,
        reservation_type="semi_order",
        order_id=order.id,
        order_item_id=item.id,
        semi_requirement_id=requirement.id,
        reserved_stock_quantity=credit,
        credited_requirement_quantity=credit,
        yield_factor=1,
        consumed_stock_quantity=0,
        released_stock_quantity=0,
        consumed_requirement_quantity=0,
        released_requirement_quantity=0,
        status="active",
        reserved_at=_now(),
        idempotency_key=f"sr-{key}",
    )
    db.add(reservation)
    db.flush()
    return reservation, lot, requirement


@pytest.fixture()
def production_app(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "n029-service.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="n029-admin",
            password_hash=hash_password(PASSWORD),
            role="admin",
            real_name="N029 Admin",
            must_change_password=False,
            customer_access_mode="all",
        )
        scoped = User(
            username="n029-scoped",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="N029 Scoped",
            must_change_password=False,
            customer_access_mode="selected",
        )
        direct_only = User(
            username="n029-direct-only",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="N029 Direct Only",
            must_change_password=False,
            customer_access_mode="selected",
        )
        viewer = User(
            username="n029-viewer",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="N029 Viewer",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(name="N029客户A")
        customer_b = Customer(name="N029客户B")
        db.add_all([admin, scoped, direct_only, viewer, customer_a, customer_b])
        db.flush()
        product_a = Product(
            customer_id=customer_a.id,
            product_code="MASTER-A",
            customer_material_code="A",
            product_name="主档产品A",
            box_style="普通箱",
        )
        product_b = Product(
            customer_id=customer_b.id,
            product_code="MASTER-B",
            customer_material_code="B",
            product_name="主档产品B",
            box_style="普通箱",
        )
        product_a3 = Product(
            customer_id=customer_a.id,
            product_code="MASTER-A3",
            customer_material_code="A3",
            product_name="天地盖",
            box_style="A3天地盖",
        )
        temp1 = WarehouseLocation(
            location_code="F12-P01",
            location_name="F12-P01",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="F12",
            storage_type="temporary_aisle",
            is_temporary=True,
            source_version="V11",
            sort_order=1,
        )
        temp2 = WarehouseLocation(
            location_code="F12-P02",
            location_name="F12-P02",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="F12",
            storage_type="temporary_aisle",
            is_temporary=True,
            source_version="V11",
            sort_order=2,
        )
        temp3 = WarehouseLocation(
            location_code="F34-P01",
            location_name="F34-P01",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="F34",
            storage_type="temporary_aisle",
            is_temporary=True,
            source_version="V11",
            sort_order=3,
        )
        fixed3 = WarehouseLocation(
            location_code="E1-R01",
            location_name="E1-R01",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="E1",
            storage_type="rack",
            is_temporary=False,
            source_version="V11",
            sort_order=4,
        )
        occupied3 = WarehouseLocation(
            location_code="E1-R02",
            location_name="E1-R02",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="E1",
            storage_type="rack",
            is_temporary=False,
            source_version="V11",
            sort_order=5,
        )
        unplaced3 = WarehouseLocation(
            location_code="SF-TEMP-N029",
            location_name="未放置测试位",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="SF",
            storage_type="temporary_aisle",
            placement_status="unplaced",
            is_temporary=True,
            source_version="V11",
            sort_order=6,
        )
        regular = WarehouseLocation(
            location_code="FG-REGULAR",
            location_name="普通成品位",
            warehouse_type="finished",
            is_active=True,
        )
        semi_location = WarehouseLocation(
            location_code="SEMI-N029",
            location_name="半成品位",
            warehouse_type="semi_finished",
            is_active=True,
        )
        staging_location = WarehouseLocation(
            location_code="F1-DISPATCH-01",
            location_name="一楼待送区",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=1,
            area_code="DISPATCH",
            storage_type="temporary_aisle",
            placement_status="placed",
            is_temporary=True,
            source_version="P1-25C",
        )
        db.add_all(
            [
                product_a,
                product_b,
                product_a3,
                temp1,
                temp2,
                temp3,
                fixed3,
                occupied3,
                unplaced3,
                regular,
                semi_location,
                staging_location,
            ]
        )
        db.flush()
        occupied_pallet = InventoryPallet(
            pallet_code="PLT-N029-OCCUPIED",
            location_id=occupied3.id,
            status="active",
            is_current=True,
            version=1,
        )
        db.add(occupied_pallet)
        db.flush()
        db.add(
            InventoryPalletItem(
                pallet_id=occupied_pallet.id,
                customer_id=customer_a.id,
                product_id=product_a.id,
                inventory_code=product_a.product_code,
                customer_name_snapshot=customer_a.name,
                product_name=product_a.product_name,
                item_type="finished",
                quantity=Decimal("1"),
                unit="boxes",
                match_status="matched",
            )
        )

        cases: dict[str, tuple[Order, OrderItem, ProductionTask]] = {}
        for key, customer, product, quantity, basis in (
            ("direct", customer_a, product_a, 5, "material_received"),
            ("stock", customer_a, product_a, 6, "semi_finished_inventory"),
            ("transfer", customer_a, product_a, 4, "material_received"),
            ("idem", customer_a, product_a, 3, "material_received"),
            ("atomic-a", customer_a, product_a, 2, "material_received"),
            ("atomic-b", customer_a, product_a, 2, "material_received"),
            ("cross", customer_b, product_b, 5, "material_received"),
            ("missing", customer_a, product_a3, 4, "semi_finished_inventory"),
            ("partial", customer_a, product_a, 5, "material_received"),
            ("sync", customer_a, product_a, 4, None),
        ):
            cases[key] = _add_case(
                db,
                key=key,
                customer=customer,
                product=product,
                quantity=quantity,
                material_status="pending" if basis in {"semi_finished_inventory", None} else "received",
                task_status="waiting_material" if key == "sync" else "pending",
                planned_quantity=0 if key == "sync" else quantity,
                readiness_basis=basis,
            )

        _semi_reserved(
            db,
            key="direct",
            order=cases["direct"][0],
            item=cases["direct"][1],
            customer=customer_a,
            product=product_a,
            location=semi_location,
            credit=5,
        )
        _semi_reserved(
            db,
            key="stock",
            order=cases["stock"][0],
            item=cases["stock"][1],
            customer=customer_a,
            product=product_a,
            location=semi_location,
            credit=6,
        )
        cover = _semi_requirement(
            db,
            key="missing-cover",
            item=cases["missing"][1],
            customer=customer_a,
            component="cover",
        )
        _semi_reserved(
            db,
            key="missing-cover",
            order=cases["missing"][0],
            item=cases["missing"][1],
            customer=customer_a,
            product=product_a3,
            location=semi_location,
            credit=4,
            component="cover",
            requirement=cover,
        )
        _semi_requirement(
            db,
            key="missing-base",
            item=cases["missing"][1],
            customer=customer_a,
            component="base",
        )
        _semi_reserved(
            db,
            key="partial",
            order=cases["partial"][0],
            item=cases["partial"][1],
            customer=customer_a,
            product=product_a,
            location=semi_location,
            credit=2,
        )

        db.add_all(
            UserCustomerScope(
                user_id=user.id,
                customer_id=customer_a.id,
                assigned_by=admin.id,
            )
            for user in (scoped, direct_only, viewer)
        )
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="orders.status",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="warehouse.execute",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=direct_only.id,
                    permission_code="orders.status",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
            ]
        )
        db.commit()
        ids = {
            "cases": {
                key: {"order": row[0].id, "item": row[1].id, "task": row[2].id}
                for key, row in cases.items()
            },
            "customer_a": customer_a.id,
            "customer_b": customer_b.id,
            "product_a": product_a.id,
            "regular": regular.id,
            "semi_location": semi_location.id,
            "temp1": temp1.id,
            "temp2": temp2.id,
            "temp3": temp3.id,
            "fixed3": fixed3.id,
            "occupied3": occupied3.id,
            "unplaced3": unplaced3.id,
            "staging": staging_location.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(production_router, prefix="/api/production")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids
    finally:
        engine.dispose()


def _login(client: TestClient, username: str = "n029-admin") -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def _complete(
    client: TestClient,
    ids: dict,
    key: str,
    *,
    idempotency_key: str,
    disposition: str = "direct",
    expected_version: int = 1,
    location_id: int | None = None,
    remarks: str | None = None,
):
    return client.post(
        "/api/production/completion-batches",
        json={
            "idempotency_key": idempotency_key,
            "items": [
                {
                    "task_id": ids["cases"][key]["task"],
                    "expected_version": expected_version,
                    "disposition": disposition,
                    "location_id": location_id,
                    "remarks": remarks,
                }
            ],
        },
    )


def test_production_tasks_paged_contract_matches_legacy_and_customer_scope(
    production_app,
) -> None:
    app, _factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        legacy = client.get("/api/production/tasks", params={"status": "pending"})
        first = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 3},
        )
        second = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 2, "page_size": 3},
        )
        invalid = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 201},
        )

        _login(client, "n029-scoped")
        scoped_legacy = client.get(
            "/api/production/tasks", params={"status": "pending"}
        )
        scoped_page = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 200},
        )

    assert legacy.status_code == 200, legacy.text
    legacy_items = legacy.json()["items"]
    legacy_by_id = {row["id"]: row for row in legacy_items}
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload == {
        "items": legacy_items[:3],
        "total": len(legacy_items),
        "page": 1,
        "page_size": 3,
    }
    assert second_payload == {
        "items": legacy_items[3:6],
        "total": len(legacy_items),
        "page": 2,
        "page_size": 3,
    }
    assert all(legacy_by_id[row["id"]] == row for row in first_payload["items"])
    assert invalid.status_code == 422

    assert scoped_legacy.status_code == 200, scoped_legacy.text
    assert scoped_page.status_code == 200, scoped_page.text
    scoped_items = scoped_legacy.json()["items"]
    assert scoped_page.json()["items"] == scoped_items
    assert scoped_page.json()["total"] == len(scoped_items)
    assert ids["cases"]["cross"]["task"] not in {
        row["id"] for row in scoped_page.json()["items"]
    }
    assert all(row["customer_id"] == ids["customer_a"] for row in scoped_items)


def test_production_tasks_paging_limits_payload_and_query_families(
    production_app,
) -> None:
    app, factory, ids = production_app
    with factory() as db:
        db.execute(update(Order).values(status="closed"))
        customer = db.get(Customer, ids["customer_a"])
        product = db.get(Product, ids["product_a"])
        for number in range(30):
            _add_case(
                db,
                key=f"page-{number:02d}",
                customer=customer,
                product=product,
                quantity=number + 1,
            )
        db.commit()
        engine = db.get_bind()

    select_statements: list[str] = []

    def record_selects(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            select_statements.append(statement)

    with TestClient(app) as client:
        _login(client)
        event.listen(engine, "before_cursor_execute", record_selects)
        try:
            one = client.get(
                "/api/production/tasks",
                params={"status": "pending", "page": 1, "page_size": 1},
            )
            one_selects = len(select_statements)
            select_statements.clear()
            twenty = client.get(
                "/api/production/tasks",
                params={"status": "pending", "page": 1, "page_size": 20},
            )
            twenty_selects = len(select_statements)
            select_statements.clear()
            legacy = client.get(
                "/api/production/tasks", params={"status": "pending"}
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_selects)

    assert one.status_code == 200, one.text
    assert twenty.status_code == 200, twenty.text
    assert legacy.status_code == 200, legacy.text
    assert one.json()["total"] == 30
    assert len(one.json()["items"]) == 1
    assert len(twenty.json()["items"]) == 20
    assert one_selects == twenty_selects
    assert one_selects <= 9
    assert len(twenty.content) < len(legacy.content)


def test_double_splice_sixty_pieces_complete_and_deliver_as_thirty_boxes(
    production_app,
) -> None:
    _app, factory, ids = production_app
    with factory() as db:
        customer = db.get(Customer, ids["customer_a"])
        product = db.get(Product, ids["product_a"])
        order, item, task = _add_case(
            db,
            key="double-splice-30",
            customer=customer,
            product=product,
            quantity=30,
            material_status="received",
            task_status="pending",
            planned_quantity=30,
            readiness_basis="incoming_receipt",
        )
        item.requisition_qty = 60
        item.snapshot_splice_mode = "double"
        item.snapshot_pieces_per_box = 2
        task.status = "waiting_material"
        task.planned_quantity = 0
        task.material_received_quantity = 0
        task.material_input_quantity = 0
        task.output_factor = 1
        db.flush()

        assert production_pieces_per_box(item) == 2
        assert production_output_quantity(60, 1, 2) == 30
        assert production_input_quantity(30, 1, 2) == 60
        task = refresh_production_task(db, item.id)
        assert task.status == "pending"
        assert task.material_input_quantity == 60
        assert task.planned_quantity == 30
        # Simulate the legacy task fact left behind after reverting the old
        # 60-piece-as-60-box completion.
        task.planned_quantity = 60
        db.flush()

        result = complete_production_batch(
            db,
            idempotency_key="double-splice-60-to-30",
            commands=[
                CompletionCommand(
                    task_id=task.id,
                    expected_version=task.version,
                    disposition="direct",
                    material_input_quantity=60,
                    actual_output_quantity=30,
                    defective_quantity=0,
                    direct_delivery_quantity=30,
                )
            ],
            operator_id=None,
        )
        completion = result.completions[0]
        assert completion.material_input_quantity == 60
        assert completion.planned_output_quantity == 30
        assert completion.actual_output_quantity == 30
        assert completion.direct_delivery_quantity == 30
        assert completion.stock_quantity == 0
        assert normalized_completion_output(item, completion) == 30
        assert production_ready_quantity(db, item) == 30
        db.refresh(task)
        assert task.planned_quantity == 30
        history = list_production_completions(
            db,
            allowed_customer_ids=None,
            completion_ids=[completion.id],
        )
        assert history[0]["pieces_per_box"] == 2
        assert order.status == "pending_delivery"


def test_old_double_splice_piece_count_is_normalized_for_delivery() -> None:
    item = OrderItem(
        snapshot_splice_mode="double",
        snapshot_pieces_per_box=2,
        special_process="一开一",
    )
    completion = ProductionCompletion(
        material_input_quantity=60,
        planned_output_quantity=60,
        actual_output_quantity=60,
    )
    assert normalized_completion_output(item, completion) == 30


def test_legacy_single_splice_snapshot_does_not_halve_finished_quantity() -> None:
    item = OrderItem(
        snapshot_splice_mode="single",
        snapshot_pieces_per_box=2,
        special_process="一开一",
    )
    completion = ProductionCompletion(
        material_input_quantity=4,
        planned_output_quantity=4,
        actual_output_quantity=4,
    )
    assert production_pieces_per_box(item) == 1
    assert normalized_completion_output(item, completion) == 4


def _delivery_item(db: Session, *, item_id: int, customer_id: int, quantity: int) -> DeliveryItem:
    delivery = Delivery(
        delivery_number=f"D-{item_id}",
        customer_id=customer_id,
        delivery_date=date.today(),
        status="pending",
        total_quantity=quantity,
    )
    db.add(delivery)
    db.flush()
    row = DeliveryItem(
        delivery_id=delivery.id,
        order_item_id=item_id,
        delivered_quantity=quantity,
    )
    db.add(row)
    db.flush()
    return row


def test_admin_can_revert_stock_completion_and_complete_again(production_app) -> None:
    app, factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        completed = _complete(
            client,
            ids,
            "stock",
            idempotency_key="stock-reversal-first",
            disposition="stock",
            location_id=ids["temp1"],
        )
        assert completed.status_code == 200, completed.text
        completion_id = completed.json()["items"][0]["id"]
        reverted = client.post(
            f"/api/production/completions/{completion_id}/revert",
            json={},
        )
        assert reverted.status_code == 200, reverted.text
        with factory() as db:
            reverted_task = db.get(ProductionTask, ids["cases"]["stock"]["task"])
            reverted_order = db.get(Order, ids["cases"]["stock"]["order"])
            assert (reverted_task.status, reverted_task.version) == ("pending", 3)
            assert reverted_order.status == "pending_production"
        recompleted = _complete(
            client,
            ids,
            "stock",
            idempotency_key="stock-reversal-second",
            disposition="stock",
            expected_version=3,
            location_id=ids["temp1"],
        )
        assert recompleted.status_code == 200, recompleted.text

    with factory() as db:
        original = db.get(ProductionCompletion, completion_id)
        original_lot = db.get(InventoryLot, original.inventory_lot_id)
        task = db.get(ProductionTask, ids["cases"]["stock"]["task"])
        order = db.get(Order, ids["cases"]["stock"]["order"])
        audit = db.scalar(
            select(OperationLog).where(
                OperationLog.action == "REVERT_PRODUCTION_COMPLETION",
                OperationLog.entity_id == completion_id,
            )
        )
        completions = db.scalars(
            select(ProductionCompletion)
            .where(ProductionCompletion.task_id == task.id)
            .order_by(ProductionCompletion.id)
        ).all()
        assert original.status == "reversed"
        assert original.reversal_reason == "撤销生产确认（系统记录）"
        assert original.reversed_by is not None and original.reversed_at is not None
        assert (original_lot.status, original_lot.quantity_available, original_lot.quantity_reserved) == (
            "closed",
            0,
            0,
        )
        assert audit is not None and "撤销生产确认（系统记录）" in audit.details
        assert '"before_completion_status": "posted"' in audit.details
        assert '"after_completion_status": "reversed"' in audit.details
        assert [row.status for row in completions] == ["reversed", "posted"]
        assert task.status == "completed"
        assert order.status == "pending_delivery"


def test_production_reversal_is_admin_only_and_downstream_change_is_atomic(
    production_app,
) -> None:
    app, factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        completed = _complete(
            client,
            ids,
            "stock",
            idempotency_key="stock-reversal-blocked",
            disposition="stock",
            location_id=ids["temp1"],
        )
        assert completed.status_code == 200, completed.text
        completion_id = completed.json()["items"][0]["id"]
        _login(client, "n029-direct-only")
        denied = client.post(
            f"/api/production/completions/{completion_id}/revert",
            json={"reason": "非管理员不应成功"},
        )
        assert denied.status_code == 403

        _login(client)
        with factory() as db:
            completion = db.get(ProductionCompletion, completion_id)
            lot = db.get(InventoryLot, completion.inventory_lot_id)
            lot.quantity_damaged = 1
            db.commit()
        blocked = client.post(
            f"/api/production/completions/{completion_id}/revert",
            json={"reason": "存在后续库存变化"},
        )
        assert blocked.status_code == 409
        assert "库存已被使用、调整、报损或数量发生变化" in blocked.json()["detail"]

    with factory() as db:
        completion = db.get(ProductionCompletion, completion_id)
        task = db.get(ProductionTask, ids["cases"]["stock"]["task"])
        finished_reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.inventory_lot_id == completion.inventory_lot_id,
                InventoryReservation.reservation_type == "finished_order",
            )
        )
        assert completion.status == "posted"
        assert completion.reversed_at is None and completion.reversal_reason is None
        assert task.status == "completed"
        assert finished_reservation.status == "active"


def test_task_refresh_tracks_finished_coverage_and_release(production_app) -> None:
    _app, factory, ids = production_app
    case = ids["cases"]["sync"]
    with factory() as db:
        item = db.get(OrderItem, case["item"])
        order = db.get(Order, case["order"])
        customer = db.get(Customer, ids["customer_a"])
        product = db.get(Product, ids["product_a"])
        location = db.get(WarehouseLocation, ids["regular"])
        task = create_or_refresh_production_task(db, item.id)
        assert (task.status, task.planned_quantity) == ("waiting_material", 0)
        lot = _finished_lot(
            db,
            key="sync-reserve",
            customer=customer,
            product=product,
            location=location,
            quantity_available=item.quantity,
        )
        reservation = reserve_finished_inventory(
            db,
            order_item_id=item.id,
            inventory_lot_id=lot.id,
            quantity=item.quantity,
            expected_version=lot.version,
            operator_id=None,
            idempotency_key="sync-finished-reserve",
            warning_acknowledged_codes=[],
        )
        assert (task.status, task.planned_quantity) == ("not_required", 0)
        assert order.status == "pending_delivery"
        release_finished_reservation(
            db,
            reservation_id=reservation.id,
            operator_id=None,
            release_reason="测试释放",
            idempotency_key="sync-finished-release",
        )
        assert (task.status, task.planned_quantity) == ("waiting_material", 0)
        assert order.status == "pending_production"


def test_direct_completion_consumes_semi_once_and_delivery_skips_it(production_app) -> None:
    app, factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        response = _complete(
            client, ids, "direct", idempotency_key="direct-once"
        )
    assert response.status_code == 200, response.text
    with factory() as db:
        item_id = ids["cases"]["direct"]["item"]
        task = db.get(ProductionTask, ids["cases"]["direct"]["task"])
        reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == item_id,
                InventoryReservation.reservation_type == "semi_order",
            )
        )
        assert task.status == "completed"
        assert reservation.consumed_requirement_quantity == 5
        assert reservation.status == "consumed"
        before = reservation.consumed_stock_quantity
        delivery_item = _delivery_item(
            db, item_id=item_id, customer_id=ids["customer_a"], quantity=5
        )
        consume_delivery_item_inventory(
            db,
            delivery_item_id=delivery_item.id,
            delivered_quantity_after_dispatch=5,
            operator_id=None,
            operation_key="direct-delivery",
        )
        reverse_delivery_item_inventory(
            db,
            delivery_item_id=delivery_item.id,
            delivered_quantity_after_cancel=0,
            operator_id=None,
            operation_key="direct-delivery-reverse",
        )
        assert reservation.consumed_stock_quantity == before
        assert production_ready_quantity(db, item_id) == 5


def test_stock_completion_reserves_finished_without_double_ready_or_semi_consume(
    production_app,
) -> None:
    app, factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        response = _complete(
            client,
            ids,
            "stock",
            idempotency_key="stock-once",
            disposition="stock",
            location_id=ids["temp1"],
        )
    assert response.status_code == 200, response.text
    completion_id = response.json()["items"][0]["id"]
    with factory() as db:
        completion = db.get(ProductionCompletion, completion_id)
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        finished = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.inventory_lot_id == lot.id,
                InventoryReservation.reservation_type == "finished_order",
            )
        )
        semi = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == completion.order_item_id,
                InventoryReservation.reservation_type == "semi_order",
            )
        )
        assert lot.source_type == "production_surplus"
        assert lot.source_ref_type == "production_completion"
        assert lot.source_ref_id == completion.id
        assert (lot.quantity_available, lot.quantity_reserved) == (0, 6)
        assert finished is not None and finished.status == "active"
        assert semi.consumed_stock_quantity == 6
        assert production_ready_quantity(db, completion.order_item_id) == 6
        delivery_item = _delivery_item(
            db,
            item_id=completion.order_item_id,
            customer_id=ids["customer_a"],
            quantity=6,
        )
        consume_delivery_item_inventory(
            db,
            delivery_item_id=delivery_item.id,
            delivered_quantity_after_dispatch=6,
            operator_id=None,
            operation_key="stock-delivery",
        )
        assert finished.consumed_stock_quantity == 6
        assert semi.consumed_stock_quantity == 6
        reverse_delivery_item_inventory(
            db,
            delivery_item_id=delivery_item.id,
            delivered_quantity_after_cancel=0,
            operator_id=None,
            operation_key="stock-delivery-reverse",
        )
        assert finished.consumed_stock_quantity == 0
        assert semi.consumed_stock_quantity == 6


def test_idempotency_conflict_cas_and_atomic_rollback(production_app) -> None:
    app, factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        first = _complete(client, ids, "idem", idempotency_key="idem-key")
        replay = _complete(client, ids, "idem", idempotency_key="idem-key")
        conflict = _complete(
            client,
            ids,
            "idem",
            idempotency_key="idem-key",
            remarks="不同内容",
        )
        stale = client.post(
            "/api/production/completion-batches",
            json={
                "idempotency_key": "atomic-stale",
                "items": [
                    {
                        "task_id": ids["cases"]["atomic-a"]["task"],
                        "expected_version": 1,
                        "disposition": "direct",
                    },
                    {
                        "task_id": ids["cases"]["atomic-b"]["task"],
                        "expected_version": 99,
                        "disposition": "direct",
                    },
                ],
            },
        )
    assert first.status_code == replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["items"][0]["id"] == first.json()["items"][0]["id"]
    assert conflict.status_code == 409
    assert stale.status_code == 409
    with factory() as db:
        for key in ("atomic-a", "atomic-b"):
            case = ids["cases"][key]
            assert db.get(ProductionTask, case["task"]).status == "pending"
            assert db.scalar(
                select(ProductionCompletion.id).where(
                    ProductionCompletion.order_item_id == case["item"]
                )
            ) is None
        assert db.scalar(
            select(func.count(ProductionCompletionBatch.id)).where(
                ProductionCompletionBatch.idempotency_key == "atomic-stale"
            )
        ) == 0


def test_terminal_or_force_closed_orders_hide_tasks_and_reject_completion_without_writes(
    production_app,
) -> None:
    app, factory, ids = production_app
    cases = {
        "direct": "dead",
        "stock": "cancelled",
        "transfer": "closed",
        "atomic-a": "archived",
        "atomic-b": "production",
    }
    with factory() as db:
        for key, order_status in cases.items():
            case = ids["cases"][key]
            db.get(Order, case["order"]).status = order_status
            if key == "atomic-b":
                db.get(OrderItem, case["item"]).is_force_closed = True
        db.commit()

    with TestClient(app) as client:
        _login(client)
        visible = client.get("/api/production/tasks")
        responses = {
            key: _complete(
                client,
                ids,
                key,
                idempotency_key=f"terminal-{key}",
            )
            for key in cases
        }

    assert visible.status_code == 200, visible.text
    visible_ids = {row["id"] for row in visible.json()["items"]}
    assert all(ids["cases"][key]["task"] not in visible_ids for key in cases)
    assert all(response.status_code == 409 for response in responses.values())
    assert all("生产" in response.json()["detail"] for response in responses.values())
    with factory() as db:
        item_ids = [ids["cases"][key]["item"] for key in cases]
        assert db.scalar(
            select(func.count(ProductionCompletion.id)).where(
                ProductionCompletion.order_item_id.in_(item_ids)
            )
        ) == 0
        assert db.scalar(
            select(func.count(ProductionCompletionBatch.id)).where(
                ProductionCompletionBatch.idempotency_key.in_(
                    [f"terminal-{key}" for key in cases]
                )
            )
        ) == 0


def test_completion_reloads_terminal_order_after_shared_lock(production_app) -> None:
    _app, factory, ids = production_app
    case = ids["cases"]["idem"]
    command = CompletionCommand(
        task_id=case["task"],
        expected_version=1,
        disposition="direct",
    )
    with factory() as stale_session:
        stale_order = stale_session.get(Order, case["order"])
        stale_session.get(OrderItem, case["item"])
        stale_session.get(ProductionTask, case["task"])
        stale_session.commit()
        assert stale_order.status == "production"

        with factory() as terminating_session:
            terminating_session.get(Order, case["order"]).status = "dead"
            terminating_session.commit()

        assert stale_order.status == "production"
        with pytest.raises(
            ProductionWorkflowError,
            match="订单当前状态不允许继续生产完工",
        ) as error:
            complete_production_batch(
                stale_session,
                idempotency_key="terminal-after-stale-read",
                commands=[command],
                operator_id=None,
            )
        assert error.value.status_code == 409
        stale_session.rollback()

    with factory() as db:
        assert db.get(ProductionTask, case["task"]).status == "pending"
        assert db.scalar(
            select(ProductionCompletion.id).where(
                ProductionCompletion.order_item_id == case["item"]
            )
        ) is None
        assert db.scalar(
            select(ProductionCompletionBatch.id).where(
                ProductionCompletionBatch.idempotency_key
                == "terminal-after-stale-read"
            )
        ) is None


def test_batch_unique_flush_race_replays_committed_winner(
    production_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.production_workflow as workflow

    _app, factory, ids = production_app
    case = ids["cases"]["idem"]
    command = CompletionCommand(
        task_id=case["task"],
        expected_version=1,
        disposition="direct",
    )
    request_hash = completion_batch_request_hash("race-key", [command])
    with factory() as db:
        batch = ProductionCompletionBatch(
            idempotency_key="race-key",
            request_hash=request_hash,
            item_count=1,
            completed_by=None,
            completed_at=_now(),
        )
        db.add(batch)
        db.flush()
        completion = ProductionCompletion(
            batch_id=batch.id,
            task_id=case["task"],
            order_item_id=case["item"],
            expected_version=1,
            quantity=3,
            initial_disposition="direct",
            completed_at=_now(),
        )
        db.add(completion)
        db.commit()

        original_scalar = db.scalar
        calls = 0

        def first_batch_lookup_misses(statement, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return original_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(db, "scalar", first_batch_lookup_misses)
        # Simulate the exact race window: the first key lookup missed, while a
        # concurrent transaction has already committed the winner by flush.
        monkeypatch.setattr(
            workflow,
            "has_production_completion_facts",
            lambda _db, _item_ids: False,
        )
        result = complete_production_batch(
            db,
            idempotency_key="race-key",
            commands=[command],
            operator_id=None,
        )
        assert result.replayed is True
        assert result.batch.id == batch.id
        assert [row.id for row in result.completions] == [completion.id]


def test_permissions_customer_scope_locations_and_snapshot_fields(production_app) -> None:
    app, _factory, ids = production_app
    with TestClient(app) as client:
        _login(client, "n029-viewer")
        visible = client.get("/api/production/tasks")
        denied = _complete(client, ids, "direct", idempotency_key="viewer-denied")
        client.post("/api/auth/logout")
        _login(client, "n029-direct-only")
        no_warehouse = _complete(
            client,
            ids,
            "stock",
            idempotency_key="no-warehouse",
            disposition="stock",
            location_id=ids["temp2"],
        )
        client.post("/api/auth/logout")
        _login(client, "n029-scoped")
        cross = _complete(client, ids, "cross", idempotency_key="cross-denied")
        invalid_location = _complete(
            client,
            ids,
            "stock",
            idempotency_key="bad-location",
            disposition="stock",
            location_id=ids["regular"],
        )
        unplaced_location = _complete(
            client,
            ids,
            "atomic-b",
            idempotency_key="unplaced-location",
            disposition="stock",
            location_id=ids["unplaced3"],
        )
        locations = client.get("/api/production/temporary-locations")
    assert visible.status_code == 200
    rows = visible.json()["items"]
    assert ids["cases"]["cross"]["task"] not in {row["id"] for row in rows}
    direct_row = next(row for row in rows if row["id"] == ids["cases"]["direct"]["task"])
    assert direct_row["product_code"] == "SNAP-direct"
    assert direct_row["product_name"] == "快照-direct"
    assert direct_row["specification"] == "500x300x200"
    assert direct_row["material"] == "K=A"
    assert direct_row["flute"] == "A"
    assert direct_row["production_notes"] == "先压线后模切"
    assert denied.status_code == no_warehouse.status_code == cross.status_code == 403
    assert invalid_location.status_code == 409
    assert unplaced_location.status_code == 409
    assert "尚未完成空间放置" in unplaced_location.json()["detail"]
    assert locations.status_code == 200
    assert {row["location_code"] for row in locations.json()["items"]} == {
        "F12-P01",
        "F12-P02",
        "F34-P01",
        "E1-R01",
        "E1-R02",
    }
    fixed = next(
        row for row in locations.json()["items"] if row["location_code"] == "E1-R01"
    )
    occupied = next(
        row for row in locations.json()["items"] if row["location_code"] == "E1-R02"
    )
    assert fixed["area_code"] == occupied["area_code"] == "E1"
    assert (fixed["location_kind"], fixed["is_empty"]) == ("fixed", True)
    assert (occupied["location_kind"], occupied["is_empty"]) == ("fixed", False)


def test_fixed_floor_three_location_accepts_stock_and_occupied_location_rejects(
    production_app,
) -> None:
    app, factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        accepted = _complete(
            client,
            ids,
            "idem",
            idempotency_key="fixed-floor-three-stock",
            disposition="stock",
            location_id=ids["fixed3"],
        )
        assert accepted.status_code == 200, accepted.text
        completion = accepted.json()["items"][0]
        assert completion["warehouse_location_code"] == "E1-R01"

    with factory() as db:
        stored = db.get(ProductionCompletion, completion["id"])
        lot = db.get(InventoryLot, stored.inventory_lot_id)
        assert lot.warehouse_location_id == ids["fixed3"]

    with TestClient(app) as client:
        _login(client)
        rejected = _complete(
            client,
            ids,
            "atomic-a",
            idempotency_key="occupied-floor-three-stock",
            disposition="stock",
            location_id=ids["occupied3"],
        )
        assert rejected.status_code == 409
        assert "占用" in rejected.json()["detail"]


def test_direct_transfer_preserves_completion_and_production_reservation_cannot_release(
    production_app,
) -> None:
    app, factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        completed = _complete(
            client, ids, "transfer", idempotency_key="transfer-complete"
        )
        assert completed.status_code == 200, completed.text
        completion_id = completed.json()["items"][0]["id"]
        payload = {
            "idempotency_key": "transfer-stock",
            "location_id": ids["temp2"],
            "pallet_code": "N029-TRANSFER-PALLET",
        }
        transferred = client.post(
            f"/api/production/completions/{completion_id}/stock-transfers",
            json=payload,
        )
        replayed = client.post(
            f"/api/production/completions/{completion_id}/stock-transfers",
            json=payload,
        )
    assert transferred.status_code == replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    with factory() as db:
        completion = db.get(ProductionCompletion, completion_id)
        transfer = db.scalar(
            select(ProductionStockTransfer).where(
                ProductionStockTransfer.completion_id == completion.id
            )
        )
        assert completion.initial_disposition == "direct"
        assert completion.inventory_lot_id == transfer.inventory_lot_id
        assert completion.warehouse_location_id == ids["temp2"]
        assert transfer is not None
        reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.inventory_lot_id == transfer.inventory_lot_id,
                InventoryReservation.reservation_type == "finished_order",
            )
        )
        assert production_ready_quantity(db, completion.order_item_id) == 4
        with pytest.raises(WarehouseInventoryError, match="完工事实") as error:
            release_finished_reservation(
                db,
                reservation_id=reservation.id,
                operator_id=None,
                release_reason="不应允许",
                idempotency_key="release-production-reservation",
                allow_downstream=True,
            )
        assert error.value.status_code == 409


def test_completion_blocks_late_normal_finished_inventory_reservation(
    production_app,
) -> None:
    app, factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        completed = _complete(
            client,
            ids,
            "stock",
            idempotency_key="semi-complete-before-finished-reserve",
            disposition="direct",
        )
    assert completed.status_code == 200, completed.text

    with factory() as db:
        case = ids["cases"]["stock"]
        customer = db.get(Customer, ids["customer_a"])
        product = db.get(Product, ids["product_a"])
        location = db.get(WarehouseLocation, ids["regular"])
        lot = _finished_lot(
            db,
            key="late-after-production",
            customer=customer,
            product=product,
            location=location,
            quantity_available=6,
        )
        with pytest.raises(WarehouseInventoryError, match="已完成生产") as error:
            reserve_finished_inventory(
                db,
                order_item_id=case["item"],
                inventory_lot_id=lot.id,
                quantity=6,
                expected_version=lot.version,
                operator_id=None,
                idempotency_key="late-finished-after-production",
                warning_acknowledged_codes=[],
            )
        assert error.value.status_code == 409
        assert db.scalar(
            select(InventoryReservation.id).where(
                InventoryReservation.idempotency_key
                == "late-finished-after-production"
            )
        ) is None


def test_terminal_or_force_closed_direct_completion_cannot_transfer_to_stock(
    production_app,
) -> None:
    app, factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        completed = _complete(
            client, ids, "transfer", idempotency_key="terminal-transfer-complete"
        )
        assert completed.status_code == 200, completed.text
        completion_id = completed.json()["items"][0]["id"]
        with factory() as db:
            case = ids["cases"]["transfer"]
            order = db.get(Order, case["order"])
            item = db.get(OrderItem, case["item"])
            order.status = "closed"
            item.is_force_closed = True
            db.commit()

        blocked = client.post(
            f"/api/production/completions/{completion_id}/stock-transfers",
            json={
                "idempotency_key": "terminal-transfer-blocked",
                "location_id": ids["temp2"],
            },
        )
        history = client.get("/api/production/completions")

    assert blocked.status_code == 409
    assert "已结案" in blocked.json()["detail"]
    history_row = next(
        row for row in history.json()["items"] if row["id"] == completion_id
    )
    assert history_row["can_transfer_to_stock"] is False
    with factory() as db:
        assert db.scalar(
            select(ProductionStockTransfer.id).where(
                ProductionStockTransfer.completion_id == completion_id
            )
        ) is None


def test_semi_basis_requires_every_component_but_material_receipt_allows_partial(
    production_app,
) -> None:
    app, factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        missing = _complete(
            client, ids, "missing", idempotency_key="missing-component"
        )
        partial = _complete(
            client, ids, "partial", idempotency_key="partial-material"
        )
    assert missing.status_code == 409
    assert partial.status_code == 200, partial.text
    with factory() as db:
        missing_case = ids["cases"]["missing"]
        assert db.get(ProductionTask, missing_case["task"]).status == "pending"
        assert db.scalar(
            select(ProductionCompletion.id).where(
                ProductionCompletion.order_item_id == missing_case["item"]
            )
        ) is None
        missing_reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == missing_case["item"],
                InventoryReservation.reservation_type == "semi_order",
            )
        )
        assert missing_reservation.consumed_stock_quantity == 0
        partial_case = ids["cases"]["partial"]
        partial_reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == partial_case["item"],
                InventoryReservation.reservation_type == "semi_order",
            )
        )
        assert db.get(ProductionTask, partial_case["task"]).status == "completed"
        assert partial_reservation.consumed_requirement_quantity == 2


def test_cannot_add_semi_reservation_after_completion(production_app) -> None:
    app, factory, ids = production_app
    with TestClient(app) as client:
        _login(client)
        completed = _complete(
            client, ids, "partial", idempotency_key="reserve-after-completion"
        )
    assert completed.status_code == 200, completed.text
    with factory() as db:
        item_id = ids["cases"]["partial"]["item"]
        requirement = db.scalar(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.order_item_id == item_id
            )
        )
        customer = db.get(Customer, ids["customer_a"])
        product = db.get(Product, ids["product_a"])
        location = db.get(WarehouseLocation, ids["semi_location"])
        lot = InventoryLot(
            lot_number="SF-AFTER-COMPLETION",
            inventory_type="semi_finished",
            warehouse_location_id=location.id,
            quantity_available=3,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="manual",
            stock_date=date.today(),
            last_movement_at=_now(),
            version=1,
        )
        db.add(lot)
        db.flush()
        db.add(
            SemiFinishedInventoryDetail(
                inventory_lot_id=lot.id,
                owner_customer_id=customer.id,
                owner_customer_name_snapshot=customer.name,
                material_code_snapshot="K=A",
                normalized_material_code="K=A",
                layer_count=3,
                flute_type="A",
                board_length_mm=800,
                board_width_mm=600,
                component_type="whole",
                pieces_per_box=1,
                stock_yield_per_sheet=1,
                sheet_type="raw_board",
            )
        )
        db.add(
            SemiFinishedLotAllowedProduct(
                inventory_lot_id=lot.id,
                product_id=product.id,
                confirmed_at=_now(),
            )
        )
        db.flush()
        with pytest.raises(WarehouseInventoryError, match="已有生产完工事实") as error:
            reserve_semi_finished_inventory(
                db,
                requirement_id=requirement.id,
                requested_requirement_quantity=1,
                lots=[SemiFinishedLotVersion(lot.id, lot.version)],
                operator_id=None,
                idempotency_key="semi-after-completion",
                confirmed=True,
                warning_acknowledged_codes=[],
            )
        assert error.value.status_code == 409
        assert lot.quantity_available == 3


def _add_all_to_production_receipt(
    db: Session,
    *,
    order: Order,
    item: OrderItem,
    received_quantity: int,
    key: str,
) -> IncomingReceiptItem:
    receipt = IncomingReceipt(
        receipt_number=f"IR-{key}",
        status="posted",
        received_at=_now(),
        idempotency_key=f"ir-{key}",
    )
    db.add(receipt)
    db.flush()
    row = IncomingReceiptItem(
        receipt_id=receipt.id,
        order_id=order.id,
        order_item_id=item.id,
        planned_quantity=int(item.quantity),
        received_quantity=received_quantity,
        cumulative_received_quantity=received_quantity,
        variance_quantity=received_quantity - int(item.quantity),
        variance_type=(
            "matched"
            if received_quantity == int(item.quantity)
            else "over"
            if received_quantity > int(item.quantity)
            else "short"
        ),
        resolution_status="resolved",
        resolution_action=(
            "all_to_production"
            if received_quantity > int(item.quantity)
            else "accept_short"
            if received_quantity < int(item.quantity)
            else None
        ),
        status="posted",
    )
    db.add(row)
    db.flush()
    return row


def test_overreceipt_203_produces_203_and_keeps_three_customer_surplus(
    production_app,
) -> None:
    _app, factory, ids = production_app
    with factory() as db:
        customer = db.get(Customer, ids["customer_a"])
        product = db.get(Product, ids["product_a"])
        order, item, task = _add_case(
            db,
            key="over-203",
            customer=customer,
            product=product,
            quantity=200,
            material_status="received",
        )
        item.special_process = "一开一"
        item.requisition_status = "已入库"
        _add_all_to_production_receipt(
            db,
            order=order,
            item=item,
            received_quantity=203,
            key="over-203",
        )
        refreshed = refresh_production_task(db, item.id)
        assert refreshed is not None
        assert refreshed.material_received_quantity == 203
        assert refreshed.material_input_quantity == 203
        assert refreshed.planned_quantity == 203
        completion_version = refreshed.version

        result = complete_production_batch(
            db,
            idempotency_key="complete-over-203",
            commands=[
                CompletionCommand(
                    task_id=task.id,
                    expected_version=completion_version,
                    disposition="stock",
                    material_input_quantity=203,
                    actual_output_quantity=203,
                    defective_quantity=0,
                    location_id=ids["temp3"],
                )
            ],
            operator_id=None,
        )
        completion = result.completions[0]
        assert completion.actual_output_quantity == 203
        assert completion.order_reserved_quantity == 200
        assert completion.stock_quantity == 203
        assert completion.surplus_finished_quantity == 3
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        assert lot is not None
        assert lot.quantity_reserved == 200
        assert lot.quantity_available == 3
        assert lot.finished_detail.owner_customer_id == customer.id
        assert production_ready_quantity(db, item) == 203
        db.commit()

        replay = complete_production_batch(
            db,
            idempotency_key="complete-over-203",
            commands=[
                CompletionCommand(
                    task_id=task.id,
                        expected_version=completion_version,
                    disposition="stock",
                    material_input_quantity=203,
                    actual_output_quantity=203,
                    defective_quantity=0,
                    location_id=ids["temp3"],
                )
            ],
            operator_id=None,
        )
        assert replay.replayed is True
        assert (
            db.scalar(
                select(func.count(ProductionCompletion.id)).where(
                    ProductionCompletion.task_id == task.id
                )
            )
            == 1
        )


def test_direct_completion_stages_whole_output_and_preserves_order_quantity(
    production_app,
) -> None:
    _app, factory, ids = production_app
    with factory() as db:
        customer = db.get(Customer, ids["customer_a"])
        product = db.get(Product, ids["product_a"])
        order, item, task = _add_case(
            db,
            key="direct-over-203",
            customer=customer,
            product=product,
            quantity=200,
            material_status="received",
        )
        item.special_process = "一开一"
        _add_all_to_production_receipt(
            db,
            order=order,
            item=item,
            received_quantity=203,
            key="direct-over-203",
        )
        refreshed = refresh_production_task(db, item.id)
        assert refreshed is not None
        db.commit()
        result = complete_production_batch(
            db,
            idempotency_key="direct-over-203-staging",
            commands=[
                CompletionCommand(
                    task_id=task.id,
                    expected_version=refreshed.version,
                    disposition="direct",
                    material_input_quantity=203,
                    actual_output_quantity=203,
                    defective_quantity=0,
                    direct_delivery_quantity=200,
                )
            ],
            operator_id=None,
        )
        completion = result.completions[0]
        assert completion.initial_disposition == "direct"
        assert completion.direct_delivery_quantity == 203
        assert completion.stock_quantity == 0
        assert completion.surplus_finished_quantity == 3
        assert completion.warehouse_location_id == ids["staging"]
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        assert lot.source_type == "production_completion"
        assert lot.quantity_available == 3
        assert lot.quantity_reserved == 200
        assert lot.warehouse_location_id == ids["staging"]
        assert item.quantity == 200
        assert production_ready_quantity(db, item) == 203
        assert lot.finished_detail.owner_customer_id == customer.id
        delivery_item = _delivery_item(
            db,
            item_id=item.id,
            customer_id=customer.id,
            quantity=203,
        )
        consume_delivery_item_inventory(
            db,
            delivery_item_id=delivery_item.id,
            delivered_quantity_after_dispatch=203,
            operator_id=None,
            operation_key="direct-over-203-delivery",
        )
        db.refresh(lot)
        surplus_reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == item.id,
                InventoryReservation.reservation_type
                == "finished_surplus_delivery",
            )
        )
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (
            0,
            0,
            203,
        )
        assert surplus_reservation is not None
        assert surplus_reservation.consumed_stock_quantity == 3


def test_one_cut_two_uses_output_factor_and_records_loss(production_app) -> None:
    _app, factory, ids = production_app
    with factory() as db:
        customer = db.get(Customer, ids["customer_a"])
        product = db.get(Product, ids["product_a"])
        order, item, task = _add_case(
            db,
            key="cut-two-203",
            customer=customer,
            product=product,
            quantity=200,
            material_status="received",
        )
        item.special_process = "一开二"
        _add_all_to_production_receipt(
            db,
            order=order,
            item=item,
            received_quantity=203,
            key="cut-two-203",
        )
        refreshed = refresh_production_task(db, item.id)
        assert refreshed is not None
        assert refreshed.output_factor == 2
        assert refreshed.planned_quantity == 406
        result = complete_production_batch(
            db,
            idempotency_key="complete-cut-two-203",
            commands=[
                CompletionCommand(
                    task_id=task.id,
                    expected_version=refreshed.version,
                    disposition="stock",
                    material_input_quantity=203,
                    actual_output_quantity=400,
                    defective_quantity=6,
                    location_id=ids["temp3"],
                )
            ],
            operator_id=None,
        )
        completion = result.completions[0]
        assert completion.planned_output_quantity == 406
        assert completion.actual_output_quantity == 400
        assert completion.defective_quantity == 6


def test_posted_200_can_add_audited_supplemental_three_without_duplication(
    production_app,
) -> None:
    _app, factory, ids = production_app
    with factory() as db:
        customer = db.get(Customer, ids["customer_a"])
        product = db.get(Product, ids["product_a"])
        order, item, task = _add_case(
            db,
            key="supplement-3",
            customer=customer,
            product=product,
            quantity=200,
            material_status="received",
        )
        item.special_process = "一开一"
        _add_all_to_production_receipt(
            db,
            order=order,
            item=item,
            received_quantity=203,
            key="supplement-3",
        )
        task.status = "completed"
        task.planned_quantity = 200
        task.material_received_quantity = 200
        task.material_input_quantity = 200
        task.output_factor = 1
        task.version = 2
        order.status = "pending_delivery"
        batch = ProductionCompletionBatch(
            idempotency_key="legacy-complete-200",
            request_hash="b" * 64,
            item_count=1,
            completed_at=_now(),
        )
        db.add(batch)
        db.flush()
        original = ProductionCompletion(
            batch_id=batch.id,
            task_id=task.id,
            order_item_id=item.id,
            expected_version=1,
            quantity=200,
            completion_type="primary",
            material_input_quantity=200,
            planned_output_quantity=200,
            actual_output_quantity=200,
            defective_quantity=0,
            order_reserved_quantity=200,
            direct_delivery_quantity=200,
            stock_quantity=0,
            surplus_finished_quantity=0,
            initial_disposition="direct",
            completed_at=_now(),
        )
        db.add(original)
        db.flush()

        result = complete_production_batch(
            db,
            idempotency_key="supplement-complete-3",
            commands=[
                CompletionCommand(
                    task_id=task.id,
                    expected_version=2,
                    disposition="stock",
                    completion_type="supplemental",
                    material_input_quantity=3,
                    actual_output_quantity=3,
                    defective_quantity=0,
                    location_id=ids["temp3"],
                    remarks="实收203，原确认200，受控补录余量3",
                )
            ],
            operator_id=None,
        )
        supplemental = result.completions[0]
        assert supplemental.completion_type == "supplemental"
        assert supplemental.actual_output_quantity == 3
        assert supplemental.order_reserved_quantity == 0
        assert supplemental.surplus_finished_quantity == 3
        assert production_ready_quantity(db, item) == 203
        assert (
            db.scalar(
                select(func.sum(ProductionCompletion.actual_output_quantity)).where(
                    ProductionCompletion.task_id == task.id,
                    ProductionCompletion.status == "posted",
                )
            )
            == 203
        )
