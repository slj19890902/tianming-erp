from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def composite_requisition_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.api.requisition import router as requisition_router
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n039-requisition.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        admin = User(
            username="admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="admin",
            display_name="admin",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=1,
            customer_code="N039",
            name="N039 测试客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        parent = Product(
            customer_id=1,
            product_code="KIT-001",
            customer_material_code="KIT-001",
            product_name="组合成品",
            box_category="normal",
            is_composite=True,
            unit="套",
        )
        component_a = Product(
            customer_id=1,
            product_code="COMP-A",
            customer_material_code="COMP-A",
            product_name="组件 A",
            box_category="normal",
            is_internal_component=True,
            unit="片",
        )
        component_b = Product(
            customer_id=1,
            product_code="COMP-B",
            customer_material_code="COMP-B",
            product_name="组件 B",
            box_category="normal",
            is_internal_component=True,
            unit="片",
        )
        session.add_all([admin, customer, parent, component_a, component_b])
        session.flush()
        order = Order(
            order_number="N039-PO-001",
            customer_id=customer.id,
            order_date=date(2026, 7, 19),
            delivery_date=date(2026, 7, 25),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("1000"),
        )
        session.add(order)
        session.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=parent.id,
            quantity=10,
            unit_price=Decimal("100"),
            subtotal=Decimal("1000"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=parent.product_code,
            snapshot_product_name=parent.product_name,
            snapshot_spec="组合成品",
            snapshot_material="K616K",
            snapshot_supplier_name="N039 供应商",
            snapshot_report_length_mm=900,
            snapshot_report_width_mm=600,
            special_process="一开一",
        )
        session.add(item)
        session.flush()
        for display_order, component, per_set, length, width in (
            (1, component_a, 2, 1000, 700),
            (2, component_b, 3, 1100, 700),
        ):
            session.add(
                SalesOrderItemBomComponent(
                    sales_order_item_id=item.id,
                    product_bom_component_id=None,
                    component_product_id=component.id,
                    order_set_quantity=10,
                    quantity_per_set=Decimal(per_set),
                    required_piece_quantity=Decimal(10 * per_set),
                    display_order=display_order,
                    internal_component_code=f"KIT-001-S{display_order:02d}",
                    is_die_cut=False,
                    snapshot_mold_tool_id=None,
                    mold_max_yield_per_sheet=None,
                    spare_sheet_quantity=0,
                    display_mode="internal_only",
                    is_required=True,
                    snapshot_component_product_code=component.product_code,
                    snapshot_component_product_name=component.product_name,
                    snapshot_component_spec=f"{length}×{width}",
                    snapshot_component_material="K616K",
                    snapshot_component_supplier_name="N039 供应商",
                    snapshot_component_layer_count=5,
                    snapshot_component_flute_type="AB",
                    snapshot_component_box_category="normal",
                    snapshot_component_box_style="模切内盒",
                    snapshot_component_default_cutting_mode="一开一",
                    snapshot_component_report_length_mm=length,
                    snapshot_component_report_width_mm=width,
                )
            )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(requisition_router, prefix="/api/requisition")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "123456"},
    )
    assert response.status_code == 200, response.text


def _component_payload(snapshot_id: int, *, actual_yield_per_sheet=None) -> dict:
    payload = {
        "order_item_id": 1,
        "bom_snapshot_id": snapshot_id,
        "cardboard_len": 1,
        "cardboard_width": 1,
        "special_process": "一开一",
    }
    if actual_yield_per_sheet is not None:
        payload["actual_yield_per_sheet"] = actual_yield_per_sheet
    return payload


def _parent_payload() -> dict:
    return {
        "order_item_id": 1,
        "cardboard_len": 900,
        "cardboard_width": 600,
        "special_process": "一开一",
    }


def test_composite_pending_keeps_one_parent_with_two_component_requirements(
    composite_requisition_app,
) -> None:
    app, _ = composite_requisition_app
    with TestClient(app) as client:
        _login(client)
        response = client.get("/api/requisition/pending")

    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert len(rows) == 1
    row = rows[0]
    assert row["is_composite_bom"] is True
    assert [component["snapshot_id"] for component in row["component_requirements"]] == [1, 2]
    assert [component["required_piece_quantity"] for component in row["component_requirements"]] == [20, 30]
    assert all(component["can_requisition"] for component in row["component_requirements"])


def test_composite_requires_snapshot_and_creates_one_source_per_component(
    composite_requisition_app,
) -> None:
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.requisition import RequisitionItem

    app, session_factory = composite_requisition_app
    with TestClient(app) as client:
        _login(client)
        rejected = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [_parent_payload()],
            },
        )
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [
                    _parent_payload(),
                    _component_payload(1),
                    _component_payload(2),
                ],
            },
        )
        duplicate = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [_component_payload(1)],
            },
        )

    assert rejected.status_code == 400
    assert created.status_code == 201, created.text
    assert duplicate.status_code == 409
    with session_factory() as session:
        requisition_items = session.scalars(
            select(RequisitionItem).order_by(RequisitionItem.id)
        ).all()
        sources = session.scalars(
            select(RequisitionItemBomSource).order_by(
                RequisitionItemBomSource.sales_order_item_bom_component_id
            )
        ).all()
    assert len(requisition_items) == 3
    assert [row.requisition_qty for row in requisition_items] == [10, 20, 30]
    assert [row.sales_order_item_bom_component_id for row in sources] == [1, 2]
    assert [int(row.quantity_per_set) for row in sources] == [2, 3]
    assert [int(row.required_piece_quantity) for row in sources] == [20, 30]
    assert all(
        row.calculation_rule_version == "bom-demand-cutting-v2"
        for row in sources
    )


def test_composite_rejects_non_integer_actual_yield(
    composite_requisition_app,
) -> None:
    from app.models.requisition import RequisitionItem

    app, session_factory = composite_requisition_app
    with TestClient(app) as client:
        _login(client)
        invalid = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [_component_payload(1, actual_yield_per_sheet=1.5)],
            },
        )

    assert invalid.status_code == 422
    with session_factory() as session:
        assert session.scalar(select(RequisitionItem.id)) is None


def test_legacy_shaped_component_only_batch_is_identified_and_voidable(
    composite_requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.requisition import Requisition, RequisitionItem

    app, session_factory = composite_requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        batch = Requisition(
            requisition_number="BL-LEGACY-BOM-001",
            requisition_date=date(2026, 7, 24),
            supplier_name="N039 供应商",
            status="已报料",
            created_by=1,
        )
        session.add(batch)
        session.flush()
        component_line = RequisitionItem(
            requisition_id=batch.id,
            order_item_id=item.id,
            inventory_deducted_qty=0,
            requisition_qty=20,
            cardboard_len=1000,
            cardboard_width=700,
            pieces_per_box=2,
            required_piece_qty=20,
            special_process="一开一",
            product_code_snapshot="COMP-A",
            product_name_snapshot="组件 A",
            status="有效",
        )
        session.add(component_line)
        session.flush()
        session.add(
            RequisitionItemBomSource(
                requisition_item_id=component_line.id,
                sales_order_item_bom_component_id=1,
                order_set_quantity=10,
                quantity_per_set=Decimal(2),
                required_piece_quantity=Decimal(20),
                demand_basis="order_sets",
                spare_sheet_quantity=0,
                calculated_purchase_quantity=Decimal(20),
                calculation_rule_version="n039-v1",
            )
        )
        item.requisition_status = "已报料"
        item.requisition_qty = 20
        session.commit()
        batch_id = batch.id

    with TestClient(app) as client:
        _login(client)
        reported = client.get("/api/requisition/reported-documents")
        voided = client.put(
            f"/api/requisition/batches/{batch_id}/void",
            json={"reason": "兼容旧组合报料批次"},
        )
        pending = client.get("/api/requisition/pending")

    row = next(
        entry for entry in reported.json()["items"] if entry["id"] == batch_id
    )
    assert row["source_type"] == "composite_bom_requisition"
    assert row["can_void"] is True
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "已取消"
    assert [
        source["source_kind"]
        for source in pending.json()["items"][0]["bom_requisition_sources"]
    ] == ["parent", "component", "component"]


def test_composite_uses_snapshot_linked_semi_reservation_before_purchase(
    composite_requisition_app,
) -> None:
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        WarehouseLocation,
    )

    app, session_factory = composite_requisition_app
    with session_factory() as session:
        location = WarehouseLocation(
            location_code="N039-SEMI-01",
            location_name="N039 半成品",
            warehouse_type="semi_finished",
        )
        session.add(location)
        session.flush()
        lot = InventoryLot(
            lot_number="N039-SEMI-LOT-01",
            inventory_type="semi_finished",
            warehouse_location_id=location.id,
            quantity_available=7,
            quantity_reserved=7,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="manual",
            stock_date=date(2026, 7, 19),
            last_movement_at=datetime.now(),
        )
        session.add(lot)
        session.flush()
        session.add(
            InventoryReservation(
                reservation_number="N039-RES-001",
                inventory_lot_id=lot.id,
                reservation_type="semi_order",
                order_item_id=1,
                sales_order_item_bom_component_id=1,
                reserved_stock_quantity=7,
                credited_requirement_quantity=7,
                yield_factor=1,
                consumed_stock_quantity=0,
                released_stock_quantity=0,
                consumed_requirement_quantity=0,
                released_requirement_quantity=0,
                status="active",
            )
        )
        session.commit()

    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending")
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [_parent_payload(), _component_payload(1)],
            },
        )

    assert pending.status_code == 200, pending.text
    component = pending.json()["items"][0]["component_requirements"][0]
    assert component["required_piece_quantity"] == 20
    assert component["semi_finished_reserved_piece_qty"] == 7
    assert component["remaining_required_piece_qty"] == 13
    assert created.status_code == 201, created.text
    with session_factory() as session:
        source = session.scalar(select(RequisitionItemBomSource))
    assert int(source.calculated_purchase_quantity) == 13


def test_component_finished_stock_reduces_only_component_requisition(
    composite_requisition_app,
) -> None:
    from app.api.requisition import _bom_snapshot_requirements
    from app.models.order import OrderItem
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.warehouse_inventory import WarehouseLocation
    from app.services.warehouse_inventory import (
        active_finished_reserved_qty,
        manual_finished_in,
        reserve_finished_inventory_for_bom_component,
    )

    _app, session_factory = composite_requisition_app
    with session_factory() as db:
        item = db.get(OrderItem, 1)
        snapshot = db.get(SalesOrderItemBomComponent, 1)
        assert item is not None and snapshot is not None
        item.quantity = 3000
        snapshot.order_set_quantity = 1500
        snapshot.required_piece_quantity = 3000
        snapshot.snapshot_component_default_cutting_mode = "一开二"
        location = WarehouseLocation(
            location_code="N039-FG-01", location_name="组件成品",
            warehouse_type="finished",
        )
        db.add(location)
        db.flush()
        lot = manual_finished_in(
            db, customer_id=1, product_id=snapshot.component_product_id,
            location_id=location.id, quantity=300, stock_date=date.today(),
            source_type="manual", remarks="组件余货", operator_id=1,
            idempotency_key="n039-component-finished-300",
        )
        reservation = reserve_finished_inventory_for_bom_component(
            db, order_item_id=item.id, bom_snapshot_id=snapshot.id,
            inventory_lot_id=lot.id, quantity=300, expected_version=lot.version,
            operator_id=1, idempotency_key="n039-component-reserve-300",
            warning_acknowledged_codes=[],
        )
        assert reservation.sales_order_item_bom_component_id == snapshot.id
        assert active_finished_reserved_qty(db, item.id) == 0
        requirements = _bom_snapshot_requirements(db, snapshot)
        assert requirements["finished_component_reserved_piece_qty"] == 300
        assert requirements["remaining_required_piece_qty"] == 2700
        assert requirements["requisition_qty"] == 1350
        db.commit()

    with TestClient(_app) as client:
        _login(client)
        blocked = client.put(
            "/api/orders/items/1/bom-components/1/demand",
            json={
                "required_piece_quantity": 299,
                "expected_required_piece_quantity": 3000,
                "idempotency_key": "n039-cannot-reduce-below-finished-coverage",
            },
        )
    assert blocked.status_code == 409, blocked.text
    assert "已预占" in blocked.json()["detail"]


def test_component_inventory_auto_cover_uses_only_safe_exact_stock(
    composite_requisition_app,
) -> None:
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        SemiFinishedInventoryDetail,
        SemiFinishedLotAllowedProduct,
        WarehouseLocation,
    )
    from app.services.warehouse_inventory import manual_finished_in

    app, session_factory = composite_requisition_app
    with session_factory() as db:
        snapshot = db.get(SalesOrderItemBomComponent, 1)
        assert snapshot is not None
        snapshot.snapshot_component_default_cutting_mode = "一开二"
        location = WarehouseLocation(
            location_code="N039-AUTO-01",
            location_name="组件自动抵扣",
            warehouse_type="finished",
        )
        semi_location = WarehouseLocation(
            location_code="N039-AUTO-SEMI-01",
            location_name="组件半成品自动抵扣",
            warehouse_type="semi_finished",
        )
        db.add_all([location, semi_location])
        db.flush()
        manual_finished_in(
            db,
            customer_id=1,
            product_id=snapshot.component_product_id,
            location_id=location.id,
            quantity=5,
            stock_date=date.today(),
            source_type="manual",
            remarks="客户专用组件成品",
            operator_id=1,
            idempotency_key="n039-auto-finished-five",
        )
        semi_lot = InventoryLot(
            lot_number="N039-AUTO-SEMI-01",
            inventory_type="semi_finished",
            warehouse_location_id=semi_location.id,
            quantity_available=5,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="manual",
            stock_date=date.today(),
            last_movement_at=datetime.now(),
        )
        db.add(semi_lot)
        db.flush()
        db.add(
            SemiFinishedInventoryDetail(
                inventory_lot_id=semi_lot.id,
                owner_customer_id=1,
                owner_customer_name_snapshot="N039 测试客户",
                material_code_snapshot="K616K",
                normalized_material_code="K616K",
                layer_count=5,
                flute_type="AB",
                board_length_mm=1000,
                board_width_mm=700,
                component_type="whole",
                pieces_per_box=1,
                stock_yield_per_sheet=2,
                sheet_type="net_sheet",
            )
        )
        db.add(
            SemiFinishedLotAllowedProduct(
                inventory_lot_id=semi_lot.id,
                product_id=snapshot.component_product_id,
                confirmed_by=1,
                confirmed_at=datetime.now(),
            )
        )
        db.commit()

    payload = {
        "order_item_id": 1,
        "idempotency_key": "n039-auto-cover-safe-exact",
    }
    with TestClient(app) as client:
        _login(client)
        covered = client.post(
            "/api/warehouse/finished/bom-components/1/auto-cover",
            json=payload,
        )
        replay = client.post(
            "/api/warehouse/finished/bom-components/1/auto-cover",
            json=payload,
        )
        pending = client.get("/api/requisition/pending")

    assert covered.status_code == 200, covered.text
    assert replay.status_code == 200, replay.text
    assert covered.json()["finished_reserved_piece_qty"] == 5
    assert covered.json()["semi_finished_reserved_piece_qty"] == 10
    assert covered.json()["remaining_required_piece_qty"] == 5
    assert replay.json()["inventory_covered_piece_qty"] == 15
    component = pending.json()["items"][0]["component_requirements"][0]
    assert component["finished_component_reserved_piece_qty"] == 5
    assert component["semi_finished_reserved_piece_qty"] == 10
    assert component["remaining_required_piece_qty"] == 5
    with session_factory() as db:
        reservations = db.scalars(
            select(InventoryReservation).where(
                InventoryReservation.sales_order_item_bom_component_id == 1
            )
        ).all()
    assert len(reservations) == 2


def test_component_inventory_auto_cover_leaves_no_empty_requirement_draft(
    composite_requisition_app,
) -> None:
    from app.models.warehouse_inventory import OrderItemSemiRequirement

    app, session_factory = composite_requisition_app
    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/warehouse/finished/bom-components/1/auto-cover",
            json={
                "order_item_id": 1,
                "idempotency_key": "n039-auto-cover-no-stock",
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["inventory_covered_piece_qty"] == 0
    assert response.json()["remaining_required_piece_qty"] == 20
    with session_factory() as db:
        requirement = db.scalar(select(OrderItemSemiRequirement.id))
    assert requirement is None


def test_t250_order_specific_demand_expands_parent_and_component_with_cutting_mode(
    composite_requisition_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_bom import (
        RequisitionItemBomSource,
        SalesOrderItemBomComponent,
        SalesOrderItemBomDemandAdjustment,
    )
    from app.models.requisition import RequisitionItem

    app, session_factory = composite_requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        parent = session.get(Product, item.product_id)
        component = session.get(SalesOrderItemBomComponent, 1)
        extra = session.get(SalesOrderItemBomComponent, 2)
        session.delete(extra)
        item.quantity = 3000
        item.snapshot_product_code = "T250-OUTER"
        item.snapshot_product_name = "T250 外包装盒"
        item.snapshot_report_length_mm = 470
        item.snapshot_report_width_mm = 600
        item.special_process = "一开一"
        parent.product_code = "T250-OUTER"
        parent.product_name = "T250 外包装盒"
        component.order_set_quantity = 3000
        component.quantity_per_set = Decimal(1)
        component.required_piece_quantity = Decimal(3000)
        component.snapshot_component_product_code = "T250-LINER"
        component.snapshot_component_product_name = "T250 内衬"
        component.snapshot_component_report_length_mm = 575
        component.snapshot_component_report_width_mm = 550
        component.snapshot_component_box_style = "刀卡"
        component.snapshot_component_default_cutting_mode = "一开二"
        component.snapshot_schema_version = 3
        component_product = session.get(Product, component.component_product_id)
        component_product.product_code = "T250-LINER"
        component_product.product_name = "T250 内衬"
        component_product.box_style = "刀卡"
        component_product.default_cutting_mode = "一开二"
        other_order = Order(
            order_number="N039-PO-OTHER",
            customer_id=1,
            order_date=date(2026, 7, 24),
            delivery_date=date(2026, 7, 30),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("3000"),
        )
        session.add(other_order)
        session.flush()
        other_item = OrderItem(
            order_id=other_order.id,
            product_id=parent.id,
            quantity=3000,
            unit_price=Decimal("1"),
            subtotal=Decimal("3000"),
            material_status="pending",
            requisition_status="已报料",
            snapshot_product_code=parent.product_code,
            snapshot_product_name=parent.product_name,
        )
        session.add(other_item)
        session.flush()
        other_snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=other_item.id,
            component_product_id=component_product.id,
            snapshot_schema_version=3,
            order_set_quantity=3000,
            quantity_per_set=Decimal(1),
            required_piece_quantity=Decimal(3000),
            display_order=1,
            internal_component_code="T250-OTHER-S01",
            is_die_cut=False,
            spare_sheet_quantity=0,
            display_mode="internal_only",
            is_required=True,
            snapshot_component_product_code="T250-LINER",
            snapshot_component_product_name="T250 内衬",
            snapshot_component_box_category="normal",
            snapshot_component_box_style="刀卡",
            snapshot_component_default_cutting_mode="一开二",
        )
        session.add(other_snapshot)
        session.commit()
        other_item_id = other_item.id

    with TestClient(app) as client:
        _login(client)
        adjusted = client.put(
            "/api/orders/items/1/bom-components/1/demand",
            json={
                "required_piece_quantity": 2700,
                "expected_required_piece_quantity": 3000,
                "idempotency_key": "t250-demand-3000-to-2700",
            },
        )
        replay = client.put(
            "/api/orders/items/1/bom-components/1/demand",
            json={
                "required_piece_quantity": 2700,
                "expected_required_piece_quantity": 3000,
                "idempotency_key": "t250-demand-3000-to-2700",
            },
        )
        pending = client.get("/api/requisition/pending")

    assert adjusted.status_code == 200, adjusted.text
    assert replay.status_code == 200, replay.text
    assert adjusted.json()["ordered_sets"] == 3000
    assert adjusted.json()["components"][0]["effective_required_piece_quantity"] == 2700
    assert pending.status_code == 200, pending.text
    rows = pending.json()["items"]
    assert len(rows) == 1
    sources = rows[0]["bom_requisition_sources"]
    assert [(row["source_kind"], row["product_name"]) for row in sources] == [
        ("parent", "T250 外包装盒"),
        ("component", "T250 内衬"),
    ]
    assert sources[0]["required_piece_quantity"] == 3000
    assert sources[0]["report_length_mm"] == 470
    assert sources[0]["report_width_mm"] == 600
    assert sources[0]["cutting_mode"] == "一开一"
    assert sources[0]["requisition_qty"] == 3000
    assert sources[1]["required_piece_quantity"] == 2700
    assert sources[1]["report_length_mm"] == 575
    assert sources[1]["report_width_mm"] == 550
    assert sources[1]["cutting_mode"] == "一开二"
    assert sources[1]["yield_per_sheet"] == 2
    assert sources[1]["requisition_qty"] == 1350

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [
                    {
                        **_parent_payload(),
                        "cardboard_len": 470,
                        "cardboard_width": 600,
                    },
                    {
                        **_component_payload(1),
                        "cardboard_len": 575,
                        "cardboard_width": 550,
                        "special_process": "一开二",
                    },
                ],
            },
        )
        duplicate = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [_component_payload(1)],
            },
        )
        blocked_history_change = client.put(
            "/api/orders/items/1/bom-components/1/demand",
            json={
                "required_piece_quantity": 2600,
                "expected_required_piece_quantity": 2700,
                "idempotency_key": "t250-demand-after-report",
            },
        )
        reported = client.get("/api/requisition/reported-documents")

    assert created.status_code == 201, created.text
    assert duplicate.status_code == 409
    assert blocked_history_change.status_code == 409
    assert "历史单据不能自动重算" in blocked_history_change.text
    assert reported.status_code == 200, reported.text
    reported_row = next(
        row
        for row in reported.json()["items"]
        if row["id"] == created.json()["id"]
        and row["source_type"] == "composite_bom_requisition"
    )
    assert reported_row["incoming_status"] == "待入库"
    assert reported_row["can_void"] is True

    with TestClient(app) as client:
        _login(client)
        voided = client.put(
            f"/api/requisition/batches/{created.json()['id']}/void",
            json={"reason": "UAT 组合 BOM 报料修正"},
        )
        voided_replay = client.put(
            f"/api/requisition/batches/{created.json()['id']}/void",
            json={"reason": "UAT 幂等重试"},
        )
        pending_after_void = client.get("/api/requisition/pending")
        reported_after_void = client.get("/api/requisition/reported-documents")
        restored_template_demand = client.put(
            "/api/orders/items/1/bom-components/1/demand",
            json={
                "required_piece_quantity": 3000,
                "expected_required_piece_quantity": 2700,
                "idempotency_key": "t250-demand-restore-template",
            },
        )
        reduced_again = client.put(
            "/api/orders/items/1/bom-components/1/demand",
            json={
                "required_piece_quantity": 2700,
                "expected_required_piece_quantity": 3000,
                "idempotency_key": "t250-demand-reduce-again",
            },
        )

    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "已取消"
    assert voided_replay.status_code == 200, voided_replay.text
    assert pending_after_void.status_code == 200, pending_after_void.text
    pending_rows = pending_after_void.json()["items"]
    assert len(pending_rows) == 1
    assert [
        source["source_kind"]
        for source in pending_rows[0]["bom_requisition_sources"]
    ] == ["parent", "component"]
    voided_reported_row = next(
        row
        for row in reported_after_void.json()["items"]
        if row["id"] == created.json()["id"]
        and row["source_type"] == "composite_bom_requisition"
    )
    assert voided_reported_row["incoming_status"] == "已作废"
    assert voided_reported_row["can_void"] is False
    assert restored_template_demand.status_code == 200
    assert (
        restored_template_demand.json()["components"][0][
            "effective_required_piece_quantity"
        ]
        == 3000
    )
    assert reduced_again.status_code == 200
    assert (
        reduced_again.json()["components"][0]["effective_required_piece_quantity"]
        == 2700
    )
    with session_factory() as session:
        from app.services.composite_bom_workflow import effective_component_demands

        items = session.scalars(
            select(RequisitionItem).order_by(RequisitionItem.id)
        ).all()
        source = session.scalar(select(RequisitionItemBomSource))
        adjustments = session.scalars(
            select(SalesOrderItemBomDemandAdjustment)
        ).all()
        component_product = session.scalar(
            select(Product).where(Product.product_code == "T250-LINER")
        )
        other_demand = effective_component_demands(session, other_item_id)[0]
    assert [row.requisition_qty for row in items] == [3000, 1350]
    assert [row.status for row in items] == ["已取消", "已取消"]
    assert int(source.required_piece_quantity) == 2700
    assert int(source.calculated_purchase_quantity) == 1350
    assert len(adjustments) == 3
    assert component_product.default_cutting_mode == "一开二"
    assert other_demand.required_piece_quantity == 3000
