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
    from app.api.requisition import router as requisition_router
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
            password_hash=hash_password("RolePass123!"),
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
                    snapshot_component_report_length_mm=length,
                    snapshot_component_report_width_mm=width,
                )
            )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "RolePass123!"},
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
                "items": [
                    {
                        "order_item_id": 1,
                        "cardboard_len": 1000,
                        "cardboard_width": 700,
                        "special_process": "一开一",
                    }
                ],
            },
        )
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [_component_payload(1), _component_payload(2)],
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
    assert len(requisition_items) == 2
    assert [row.requisition_qty for row in requisition_items] == [20, 30]
    assert [row.sales_order_item_bom_component_id for row in sources] == [1, 2]
    assert [int(row.quantity_per_set) for row in sources] == [2, 3]
    assert [int(row.required_piece_quantity) for row in sources] == [20, 30]
    assert all(row.calculation_rule_version == "n039-v1" for row in sources)


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
                "items": [_component_payload(1)],
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
