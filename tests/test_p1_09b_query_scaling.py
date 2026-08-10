from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def delivery_scaling_app(tmp_path: Path):
    """A deterministic 24-row page plus 24 pending-candidate fixture.

    It deliberately uses normal business rows instead of mocks, so a future
    batch serializer must preserve the delivery/candidate contract while its
    SQL count no longer grows per row.
    """

    from app.api.auth import router as auth_router
    from app.api.deliveries import router as deliveries_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-09b-scaling.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="p109b-admin",
            password_hash=hash_password("P109bPass123!"),
            role="admin",
            real_name="P1-09B 管理员",
            display_name="P1-09B 管理员",
            must_change_password=False,
        )
        scoped = User(
            username="p109b-scoped",
            password_hash=hash_password("P109bPass123!"),
            role="sales",
            real_name="P1-09B 范围用户",
            display_name="P1-09B 范围用户",
            customer_access_mode="selected",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=901,
            customer_code="P109A",
            name="P1-09B 性能客户",
            payment_term_days=30,
            credit_limit=Decimal("0"),
        )
        outside_customer = Customer(
            customer_number=902,
            customer_code="P109B",
            name="P1-09B 范围外客户",
            payment_term_days=30,
            credit_limit=Decimal("0"),
        )
        db.add_all([admin, scoped, customer, outside_customer])
        db.flush()
        db.add_all(
            [
                UserCustomerScope(user_id=scoped.id, customer_id=customer.id),
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="deliveries.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=scoped.id,
                    permission_code="deliveries.execute",
                    is_allowed=True,
                ),
            ]
        )
        product = Product(
            customer_id=customer.id,
            product_code="P109-A-BOX",
            customer_material_code="P109-A-BOX",
            product_name="性能回归外箱",
            legacy_material_text="A=B",
            box_category="normal",
        )
        outside_product = Product(
            customer_id=outside_customer.id,
            product_code="P109-B-BOX",
            customer_material_code="P109-B-BOX",
            product_name="范围外外箱",
            legacy_material_text="K=K",
            box_category="normal",
        )
        db.add_all([product, outside_product])
        db.flush()

        base = datetime(2026, 7, 29, 9, 0, 0)
        for index in range(24):
            order = Order(
                order_number=f"P109A-SO-{index + 1:03d}",
                customer_id=customer.id,
                customer_po=f"P109A-PO-{index + 1:03d}",
                order_date=date(2026, 7, 1),
                delivery_date=date(2026, 7, 30),
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=Decimal("20"),
            )
            db.add(order)
            db.flush()
            delivered_item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=20,
                unit_price=Decimal("1"),
                subtotal=Decimal("20"),
                material_status="received",
                delivered_quantity=0,
                snapshot_product_name="性能回归外箱",
                snapshot_spec="400×300×200mm",
                snapshot_material="A=B",
            )
            pending_item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=10,
                unit_price=Decimal("1"),
                subtotal=Decimal("10"),
                material_status="received",
                delivered_quantity=0,
                snapshot_product_name="性能候选外箱",
                snapshot_spec="400×300×200mm",
                snapshot_material="A=B",
            )
            db.add_all([delivered_item, pending_item])
            db.flush()
            delivery = Delivery(
                delivery_number=f"P109A-{index + 1:04d}",
                customer_id=customer.id,
                delivery_date=date(2026, 7, 29),
                status="dispatched" if index % 2 == 0 else "pending",
                total_quantity=5,
                created_at=base + timedelta(minutes=index),
            )
            db.add(delivery)
            db.flush()
            db.add(
                DeliveryItem(
                    delivery_id=delivery.id,
                    order_item_id=delivered_item.id,
                    delivered_quantity=5,
                    remarks="P1-09B 金样本",
                )
            )
            if index % 3 == 0:
                db.add(
                    ReturnReceipt(
                        delivery_id=delivery.id,
                        actual_received_date=date(2026, 7, 29),
                        status="confirmed",
                    )
                )

        outside_order = Order(
            order_number="P109B-SO-001",
            customer_id=outside_customer.id,
            customer_po="P109B-PO-001",
            order_date=date(2026, 7, 1),
            delivery_date=date(2026, 7, 30),
            status="pending_delivery",
            payment_status="unpaid",
            total_amount=Decimal("10"),
        )
        db.add(outside_order)
        db.flush()
        outside_item = OrderItem(
            order_id=outside_order.id,
            product_id=outside_product.id,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="received",
            delivered_quantity=0,
            snapshot_product_name="范围外外箱",
            snapshot_spec="400×300×200mm",
            snapshot_material="K=K",
        )
        db.add(outside_item)
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(deliveries_router, prefix="/api/deliveries")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, engine, {"customer_id": customer.id, "outside_customer_id": outside_customer.id}


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "P109bPass123!"},
    )
    assert response.status_code == 200, response.text


def _read_with_sql_count(client: TestClient, engine, path: str, *, params: dict) -> tuple[object, list[str]]:
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(" ".join(statement.lstrip().lower().split()))

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        response = client.get(path, params=params)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    assert response.status_code == 200, response.text
    assert not any(statement.startswith(("insert ", "update ", "delete ", "replace ")) for statement in statements)
    return response, statements


def _select_count(statements: list[str]) -> int:
    return sum(statement.startswith(("select ", "pragma ")) for statement in statements)


def _seed_formal_like_delivery_inventory_sources(engine) -> list[int]:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.warehouse_inventory import (
        DeliveryInventoryAllocation,
        InventoryLot,
        InventoryMovement,
        InventoryReservation,
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )

    with Session(engine) as db:
        floor = WarehouseFloor(
            floor_code="F1",
            floor_name="一楼",
            floor_number=1,
            construction_status="enabled",
            planning_reference_pallet_capacity=100,
        )
        db.add(floor)
        db.flush()
        db.add(
            WarehouseArea(
                floor_id=floor.id,
                area_code="RAW",
                area_name="待送区",
                construction_status="enabled",
            )
        )
        delivery_items = list(
            db.scalars(select(DeliveryItem).order_by(DeliveryItem.id)).all()
        )
        delivery_ids: list[int] = []
        for index, delivery_item in enumerate(delivery_items, start=1):
            delivery = db.get(Delivery, delivery_item.delivery_id)
            assert delivery is not None
            delivery_ids.append(int(delivery.id))
            location = WarehouseLocation(
                location_code=f"P109-LOC-{index:03d}",
                location_name=f"性能库位 {index}",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=1,
                area_code="RAW",
                storage_type="ground",
                placement_status="placed",
                sort_order=index,
            )
            db.add(location)
            db.flush()
            dispatched = delivery.status == "dispatched"
            lot = InventoryLot(
                lot_number=f"P109-LOT-{index:03d}",
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=0 if dispatched else 5,
                quantity_reserved=0 if dispatched else 5,
                quantity_consumed=5 if dispatched else 0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 7, 1),
                stock_date_accuracy="exact",
                last_movement_at=datetime(2026, 7, 29, 8, 0, 0),
                version=1,
            )
            db.add(lot)
            db.flush()
            reservation = InventoryReservation(
                reservation_number=f"P109-RES-{index:03d}",
                inventory_lot_id=lot.id,
                reservation_type="finished_order",
                order_id=delivery_item.order_item.order_id,
                order_item_id=delivery_item.order_item_id,
                reserved_stock_quantity=5,
                credited_requirement_quantity=5,
                yield_factor=1,
                consumed_stock_quantity=5 if dispatched else 0,
                released_stock_quantity=0,
                consumed_requirement_quantity=5 if dispatched else 0,
                released_requirement_quantity=0,
                status="consumed" if dispatched else "active",
                idempotency_key=f"p109-res-{index:03d}",
            )
            db.add(reservation)
            db.flush()
            if dispatched:
                movement = InventoryMovement(
                    movement_number=f"P109-MOV-{index:03d}",
                    inventory_lot_id=lot.id,
                    movement_type="consume",
                    quantity=5,
                    unit="boxes",
                    before_available=0,
                    after_available=0,
                    before_reserved=5,
                    after_reserved=0,
                    before_consumed=0,
                    after_consumed=5,
                    before_damaged=0,
                    after_damaged=0,
                    before_scrapped=0,
                    after_scrapped=0,
                    reservation_id=reservation.id,
                    related_order_id=delivery_item.order_item.order_id,
                    related_order_item_id=delivery_item.order_item_id,
                    related_delivery_id=delivery.id,
                    reason="送货列表批量查询回归",
                    idempotency_key=f"p109-mov-{index:03d}",
                )
                db.add(movement)
                db.flush()
                db.add(
                    DeliveryInventoryAllocation(
                        delivery_item_id=delivery_item.id,
                        reservation_id=reservation.id,
                        consume_movement_id=movement.id,
                        consumed_stock_quantity=5,
                        credited_requirement_quantity=5,
                        reversed_stock_quantity=0,
                        reversed_requirement_quantity=0,
                        status="active",
                    )
                )
        db.commit()
    return delivery_ids


def test_delivery_list_page_scales_without_per_row_sql_and_keeps_summary_contract(delivery_scaling_app) -> None:
    app, engine, ids = delivery_scaling_app
    with TestClient(app) as client:
        _login(client, "p109b-admin")
        small, small_sql = _read_with_sql_count(
            client, engine, "/api/deliveries", params={"page": 1, "page_size": 5}
        )
        large, large_sql = _read_with_sql_count(
            client, engine, "/api/deliveries", params={"page": 1, "page_size": 24}
        )

    assert small.json()["total"] == large.json()["total"] == 24
    assert small.json()["page_size"] == 5
    assert large.json()["page_size"] == 24
    assert [row["id"] for row in small.json()["items"]] == [
        row["id"] for row in large.json()["items"][:5]
    ]
    row = large.json()["items"][0]
    assert {"id", "delivery_number", "customer_id", "customer_name", "delivery_date", "status", "pick_task", "items", "return_receipt_status"} <= set(row)
    assert {"id", "order_item_id", "product_code", "product_name", "specification", "delivered_quantity", "inventory_sources"} <= set(row["items"][0])

    # A batch implementation can spend a fixed number of queries for the page,
    # but must not add one query family per delivery or item.
    assert _select_count(large_sql) <= _select_count(small_sql) + 24

    with TestClient(app) as client:
        _login(client, "p109b-scoped")
        scoped, _ = _read_with_sql_count(
            client, engine, "/api/deliveries", params={"page": 1, "page_size": 24}
        )
        forbidden = client.get("/api/deliveries", params={"customer_id": ids["outside_customer_id"]})
    assert scoped.json()["total"] == 24
    assert forbidden.status_code == 403


def test_delivery_list_batches_formal_like_inventory_sources_with_fixed_query_ceiling(
    delivery_scaling_app,
) -> None:
    app, engine, _ids = delivery_scaling_app
    _seed_formal_like_delivery_inventory_sources(engine)

    with TestClient(app) as client:
        _login(client, "p109b-admin")
        first_page, first_sql = _read_with_sql_count(
            client,
            engine,
            "/api/deliveries",
            params={"page": 1, "page_size": 5},
        )
        full_page, full_sql = _read_with_sql_count(
            client,
            engine,
            "/api/deliveries",
            params={"page": 1, "page_size": 24},
        )
        second_page, _ = _read_with_sql_count(
            client,
            engine,
            "/api/deliveries",
            params={"page": 2, "page_size": 5},
        )

    assert first_page.json()["total"] == full_page.json()["total"] == 24
    assert [row["id"] for row in first_page.json()["items"]] == [
        row["id"] for row in full_page.json()["items"][:5]
    ]
    assert set(row["id"] for row in first_page.json()["items"]).isdisjoint(
        row["id"] for row in second_page.json()["items"]
    )
    assert any(
        item["inventory_sources"]
        for delivery in full_page.json()["items"]
        for item in delivery["items"]
    )

    full_selects = [
        statement
        for statement in full_sql
        if statement.startswith(("select ", "pragma "))
    ]
    assert len(full_selects) <= 24
    assert len(full_selects) <= _select_count(first_sql) + 2
    assert sum(" from inventory_reservations " in row for row in full_selects) == 1
    assert sum(" from inventory_lots " in row for row in full_selects) == 1
    assert sum(" from warehouse_locations " in row for row in full_selects) == 1
    assert sum(" from warehouse_floors" in row for row in full_selects) == 1
    assert sum(" from warehouse_areas " in row for row in full_selects) == 1


def test_delivery_list_batch_response_matches_single_delivery_serializer(
    delivery_scaling_app,
) -> None:
    from app.api.deliveries import _delivery_list_page_context, _delivery_response
    from app.models.delivery import Delivery

    _app, engine, _ids = delivery_scaling_app
    _seed_formal_like_delivery_inventory_sources(engine)
    with Session(engine) as db:
        delivery_ids = list(
            db.scalars(
                select(Delivery.id).order_by(
                    Delivery.created_at.desc(),
                    Delivery.id.desc(),
                )
            ).all()
        )
        expected = [
            _delivery_response(db, delivery_id)
            for delivery_id in delivery_ids
        ]
    with Session(engine) as db:
        context = _delivery_list_page_context(db, delivery_ids)
        actual = [
            _delivery_response(db, delivery_id, list_context=context)
            for delivery_id in delivery_ids
        ]

    assert actual == expected


def test_delivery_summary_view_defers_heavy_items_and_shrinks_first_paint(delivery_scaling_app) -> None:
    app, engine, _ids = delivery_scaling_app
    with TestClient(app) as client:
        _login(client, "p109b-admin")
        small, small_sql = _read_with_sql_count(
            client,
            engine,
            "/api/deliveries",
            params={"page": 1, "page_size": 5, "view": "summary"},
        )
        large, large_sql = _read_with_sql_count(
            client,
            engine,
            "/api/deliveries",
            params={"page": 1, "page_size": 24, "view": "summary"},
        )
        full = client.get(
            "/api/deliveries",
            params={"page": 1, "page_size": 24},
        )

    assert full.status_code == 200, full.text
    assert small.json()["view"] == large.json()["view"] == "summary"
    assert small.json()["total"] == large.json()["total"] == 24
    assert [row["id"] for row in small.json()["items"]] == [
        row["id"] for row in large.json()["items"][:5]
    ]
    row = large.json()["items"][0]
    assert {
        "id",
        "delivery_number",
        "customer_id",
        "customer_name",
        "delivery_date",
        "status",
        "pick_task",
        "return_receipt_status",
        "item_count",
        "total_actual_goods_quantity",
    } <= set(row)
    assert "items" not in row
    assert row["item_count"] == 1
    assert row["total_actual_goods_quantity"] == 5
    assert full.json()["items"][0]["items"]
    assert len(large.content) < len(full.content) * 0.45
    assert _select_count(large_sql) <= _select_count(small_sql) + 2


def test_composite_delivery_summary_keeps_fixed_query_families_and_full_quantity(
    delivery_scaling_app,
) -> None:
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent

    app, engine, ids = delivery_scaling_app
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = db.get(Customer, ids["customer_id"])
        component = Product(
            customer_id=customer.id,
            product_code="P109-COMPONENT",
            customer_material_code="P109-COMPONENT",
            product_name="性能回归组合子件",
            legacy_material_text="A=B",
            box_category="die_cut",
            box_style="模切内盒",
            is_internal_component=True,
        )
        db.add(component)
        db.flush()
        rows = db.execute(
            select(DeliveryItem, OrderItem)
            .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
            .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
            .where(Delivery.customer_id == customer.id)
            .order_by(Delivery.id)
        ).all()
        assert len(rows) == 24
        for index, (_delivery_item, order_item) in enumerate(rows, start=1):
            db.add(
                SalesOrderItemBomComponent(
                    sales_order_item_id=order_item.id,
                    component_product_id=component.id,
                    parent_product_version=1,
                    component_product_version=1,
                    snapshot_schema_version=3,
                    order_set_quantity=20,
                    quantity_per_set=Decimal("1"),
                    required_piece_quantity=Decimal("20"),
                    display_order=1,
                    internal_component_code=f"P109-COMP-{index:03d}",
                    is_die_cut=False,
                    spare_sheet_quantity=0,
                    display_mode="show_on_delivery",
                    is_required=True,
                    snapshot_component_product_code=component.product_code,
                    snapshot_component_product_name=component.product_name,
                    snapshot_component_spec="匿名组件规格",
                    snapshot_component_box_category="die_cut_inner",
                    snapshot_component_default_cutting_mode="一开一",
                )
            )
        db.commit()

    with TestClient(app) as client:
        _login(client, "p109b-admin")
        small, small_sql = _read_with_sql_count(
            client,
            engine,
            "/api/deliveries",
            params={"page": 1, "page_size": 5, "view": "summary"},
        )
        large, large_sql = _read_with_sql_count(
            client,
            engine,
            "/api/deliveries",
            params={"page": 1, "page_size": 24, "view": "summary"},
        )
        full = client.get("/api/deliveries", params={"page": 1, "page_size": 24})

    assert full.status_code == 200, full.text
    full_by_id = {row["id"]: row for row in full.json()["items"]}
    assert _select_count(large_sql) <= _select_count(small_sql) + 2
    for summary_row in large.json()["items"]:
        full_row = full_by_id[summary_row["id"]]
        assert summary_row["total_actual_goods_quantity"] == full_row["total_actual_goods_quantity"]
        if summary_row["status"] == "pending":
            assert summary_row["total_actual_goods_quantity"] == 10
        else:
            assert summary_row["total_actual_goods_quantity"] == 5


def test_pending_delivery_search_scales_by_limit_without_writes_or_scope_leak(delivery_scaling_app) -> None:
    app, engine, ids = delivery_scaling_app
    with TestClient(app) as client:
        _login(client, "p109b-admin")
        small, small_sql = _read_with_sql_count(
            client,
            engine,
            "/api/deliveries/pending-items/search",
            params={"customer_id": ids["customer_id"], "list_all": "true", "limit": 5},
        )
        large, large_sql = _read_with_sql_count(
            client,
            engine,
            "/api/deliveries/pending-items/search",
            params={"customer_id": ids["customer_id"], "list_all": "true", "limit": 20},
        )
        all_items, all_sql = _read_with_sql_count(
            client,
            engine,
            "/api/deliveries/pending-items/search",
            params={"customer_id": ids["customer_id"], "list_all": "true"},
        )

    assert len(small.json()["items"]) == 5
    assert len(large.json()["items"]) == 20
    assert len(all_items.json()["items"]) == all_items.json()["total"]
    assert all_items.json()["total"] > 20
    assert all_items.json()["total_pages"] == 1
    first = large.json()["items"][0]
    assert {"order_item_id", "order_id", "customer_id", "product_code", "product_name", "specification", "remaining_quantity", "deliverable_quantity", "inventory_sources"} <= set(first)
    assert _select_count(large_sql) <= _select_count(small_sql) + 24
    assert _select_count(all_sql) <= _select_count(small_sql) + 24

    with TestClient(app) as client:
        _login(client, "p109b-scoped")
        scoped, _ = _read_with_sql_count(
            client,
            engine,
            "/api/deliveries/pending-items/search",
            params={"customer_id": ids["customer_id"], "list_all": "true", "limit": 5},
        )
        forbidden = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": ids["outside_customer_id"], "list_all": "true", "limit": 5},
        )
    assert len(scoped.json()["items"]) == 5
    assert forbidden.status_code == 403
