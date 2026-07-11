from __future__ import annotations

from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import Delivery
from app.models.order import OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    InventoryLot,
    InventoryReservation,
    OrderItemSemiRequirement,
    WarehouseLocation,
)
from app.services.warehouse_inventory import (
    active_finished_reserved_qty,
    manual_finished_in,
    manual_semi_finished_in,
    release_active_finished_reservations_for_items,
)


@pytest.fixture()
def delivery_inventory_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deliveries import router as deliveries_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.api.requisition import router as requisition_router
    from app.api.warehouse import router as warehouse_router

    engine = create_sqlite_engine(tmp_path / "semi-delivery-b2.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username=role,
                    password_hash=hash_password("RolePass123!"),
                    role=role,
                    real_name=role,
                    display_name=role,
                    must_change_password=False,
                )
                for role in ("admin", "sales")
            ]
        )
        customer = Customer(
            customer_number=9201,
            customer_code="B2-A",
            name="B2库存客户",
            payment_term_days=0,
            credit_limit=0,
        )
        other_customer = Customer(
            customer_number=9202,
            customer_code="B2-B",
            name="B2其他客户",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all([customer, other_customer])
        db.flush()
        products = [
            Product(
                customer_id=customer.id,
                product_code="B2-SINGLE",
                customer_material_code="B2-SINGLE-M",
                product_name="普通单拼",
                box_category="normal",
                box_style="普通箱",
                legacy_material_text="A416D",
                default_material_code="A416D",
                flute_type="B",
                layer_count=3,
                report_length_mm=800,
                report_width_mm=600,
                splice_mode="single",
                pieces_per_box=1,
            ),
            Product(
                customer_id=customer.id,
                product_code="B2-DOUBLE",
                customer_material_code="B2-DOUBLE-M",
                product_name="普通双拼",
                box_category="normal",
                box_style="普通箱",
                legacy_material_text="A416D",
                default_material_code="A416D",
                flute_type="B",
                layer_count=3,
                report_length_mm=800,
                report_width_mm=600,
                splice_mode="double",
                pieces_per_box=2,
            ),
            Product(
                customer_id=customer.id,
                product_code="B2-A3",
                customer_material_code="B2-A3-M",
                product_name="天地盖",
                box_category="normal",
                box_style="A3 天地盖",
                legacy_material_text="A416D",
                default_material_code="A416D",
                flute_type="B",
                layer_count=3,
                report_length_mm=400,
                report_width_mm=300,
                base_report_length_mm=375,
                base_report_width_mm=275,
                splice_mode="single",
                pieces_per_box=1,
            ),
        ]
        db.add_all(products)
        db.add_all(
            [
                WarehouseLocation(
                    location_code="B2-FG",
                    location_name="B2成品库位",
                    warehouse_type="finished",
                ),
                WarehouseLocation(
                    location_code="B2-SI",
                    location_name="B2半成品库位",
                    warehouse_type="semi_finished",
                ),
            ]
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(requisition_router, prefix="/api/requisition")
    app.include_router(warehouse_router, prefix="/api/warehouse")
    app.include_router(deliveries_router, prefix="/api/deliveries")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, factory


def login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "sales", "password": "RolePass123!"},
    )
    assert response.status_code == 200


def add_finished(factory, *, product_id: int, quantity: int, key: str):
    with factory() as db:
        location = db.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "B2-FG"
            )
        )
        lot = manual_finished_in(
            db,
            customer_id=1,
            product_id=product_id,
            location_id=location.id,
            quantity=quantity,
            stock_date=date.today(),
            source_type="manual",
            remarks=None,
            operator_id=1,
            idempotency_key=key,
        )
        db.commit()
        return lot.id, lot.version


def add_semi(
    factory,
    *,
    quantity: int,
    key: str,
    component: str = "whole",
    pieces_per_box: int = 1,
    yield_factor: int = 1,
    length: int = 800,
    width: int = 600,
):
    with factory() as db:
        location = db.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "B2-SI"
            )
        )
        lot = manual_semi_finished_in(
            db,
            location_id=location.id,
            quantity=quantity,
            stock_date=date.today(),
            source_type="production_surplus",
            material_code="A416D",
            layer_count=3,
            flute_type="B",
            board_length_mm=length,
            board_width_mm=width,
            sheet_type="net_sheet",
            component_type=component,
            pieces_per_box=pieces_per_box,
            stock_yield_per_sheet=yield_factor,
            supplier_name=None,
            customer_id=1,
            crease_type=None,
            crease_left_mm=None,
            crease_middle_mm=None,
            crease_right_mm=None,
            cutting_note=None,
            remarks=None,
            operator_id=1,
            idempotency_key=key,
        )
        db.commit()
        return lot.id, lot.version


def finished_plan(lot_id: int, version: int, quantity: int) -> dict:
    return {
        "lot_id": lot_id,
        "expected_version": version,
        "requested_qty": quantity,
        "recommendation_source": "dedicated",
        "confirmed": True,
    }


def semi_plan(
    lot_id: int,
    version: int,
    pieces: int,
    component: str = "whole",
) -> dict:
    return {
        "lot_id": lot_id,
        "expected_version": version,
        "requested_qty": pieces,
        "component_type": component,
        "recommendation_source": "signature",
        "confirmed": True,
    }


def create_order(
    client: TestClient,
    *,
    po: str,
    product_id: int,
    quantity: int,
    finished: list[dict] | None = None,
    semi: list[dict] | None = None,
) -> int:
    response = client.post(
        "/api/orders",
        json={
            "customer_id": 1,
            "customer_po": po,
            "order_date": "2026-07-10",
            "import_integrity_status": "ok",
            "items": [
                {
                    "client_line_id": f"{po}-LINE",
                    "product_id": product_id,
                    "quantity": quantity,
                    "unit_price": "1.00",
                    "reservation_plan": {
                        "finished": finished or [],
                        "semi": semi or [],
                    },
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["items"][0]["id"]


def create_delivery(client: TestClient, order_item_id: int, quantity: int) -> dict:
    response = client.post(
        "/api/deliveries",
        json={
            "customer_id": 1,
            "delivery_date": "2026-07-10",
            "items": [
                {
                    "order_item_id": order_item_id,
                    "delivered_quantity": quantity,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def all_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for child in value.values() for key in all_keys(child)
        }
    if isinstance(value, list):
        return {key for child in value for key in all_keys(child)}
    return set()


def test_finished_then_semi_multi_dispatch_cancel_and_desktop_sources(
    delivery_inventory_app,
) -> None:
    app, factory = delivery_inventory_app
    finished_id, finished_version = add_finished(
        factory, product_id=1, quantity=4, key="b2-mixed-finished"
    )
    semi_id, semi_version = add_semi(
        factory, quantity=6, key="b2-mixed-semi"
    )
    with TestClient(app) as client:
        login(client)
        item_id = create_order(
            client,
            po="B2-MIXED",
            product_id=1,
            quantity=10,
            finished=[finished_plan(finished_id, finished_version, 4)],
            semi=[semi_plan(semi_id, semi_version, 6)],
        )
        pending = client.get("/api/deliveries/pending_items")
        first = create_delivery(client, item_id, 5)
        first_detail = client.get(f"/api/deliveries/{first['id']}")
        dispatched_first = client.put(
            f"/api/deliveries/{first['id']}/dispatch"
        )
        requisition_after_first = client.get("/api/requisition/pending")
        second = create_delivery(client, item_id, 5)
        dispatched_second = client.put(
            f"/api/deliveries/{second['id']}/dispatch"
        )
        cancelled_second = client.put(
            f"/api/deliveries/{second['id']}/cancel"
        )
        listed = client.get("/api/deliveries")
        printed = client.get(f"/api/deliveries/{first['id']}/print")

    assert pending.status_code == 200
    pending_row = next(
        row for row in pending.json()["items"] if row["order_item_id"] == item_id
    )
    assert {row["location_code"] for row in pending_row["inventory_sources"]} == {
        "B2-FG",
        "B2-SI",
    }
    assert first_detail.json()["items"][0]["inventory_sources"]
    assert dispatched_first.status_code == 200, dispatched_first.text
    actual_sources = {
        row["source_type"]: row
        for row in dispatched_first.json()["items"][0]["inventory_sources"]
    }
    assert actual_sources["finished"]["quantity_to_pick_stock"] == 4
    assert actual_sources["semi_finished"]["quantity_to_pick_stock"] == 1
    first_listed = next(
        row for row in listed.json()["items"] if row["id"] == first["id"]
    )
    assert first_listed["items"][0]["inventory_sources"]
    # The order is fully covered by its finished + semi-finished reservations,
    # so it must not reappear as a zero-sheet pending requisition row.
    assert not any(
        row["product_code"] == "B2-SINGLE"
        for row in requisition_after_first.json()["items"]
    )
    assert dispatched_second.status_code == 200, dispatched_second.text
    assert cancelled_second.status_code == 200, cancelled_second.text
    assert printed.status_code == 200
    forbidden_fragments = ("location", "lot", "source")
    assert not any(
        fragment in key.lower()
        for key in all_keys(printed.json())
        for fragment in forbidden_fragments
    )
    with factory() as db:
        item = db.get(OrderItem, item_id)
        finished_lot = db.get(InventoryLot, finished_id)
        semi_lot = db.get(InventoryLot, semi_id)
        reservations = db.scalars(
            select(InventoryReservation)
            .where(InventoryReservation.order_item_id == item_id)
            .order_by(InventoryReservation.reservation_type)
        ).all()
        finished = next(
            row for row in reservations if row.reservation_type == "finished_order"
        )
        semi = next(
            row for row in reservations if row.reservation_type == "semi_order"
        )
        assert item.delivered_quantity == 5
        assert active_finished_reserved_qty(db, item_id) == 4
        assert finished.consumed_stock_quantity == 4
        assert semi.consumed_requirement_quantity == 1
        assert (finished_lot.quantity_reserved, finished_lot.quantity_consumed) == (0, 4)
        assert (semi_lot.quantity_reserved, semi_lot.quantity_consumed) == (5, 1)


def test_inventory_sources_exclude_zero_quantity_later_reservations(
    delivery_inventory_app,
) -> None:
    app, factory = delivery_inventory_app
    first_lot_id, first_version = add_semi(
        factory, quantity=2, key="b2-source-first"
    )
    second_lot_id, second_version = add_semi(
        factory, quantity=2, key="b2-source-second"
    )
    with TestClient(app) as client:
        login(client)
        item_id = create_order(
            client,
            po="B2-SOURCE-FILTER",
            product_id=1,
            quantity=4,
            semi=[
                semi_plan(first_lot_id, first_version, 2),
                semi_plan(second_lot_id, second_version, 2),
            ],
        )
        pending = create_delivery(client, item_id, 1)
        dispatched = client.put(f"/api/deliveries/{pending['id']}/dispatch")
        cancelled = client.put(f"/api/deliveries/{pending['id']}/cancel")
        printed = client.get(f"/api/deliveries/{pending['id']}/print")

    assert [
        row["lot_id"] for row in pending["items"][0]["inventory_sources"]
    ] == [first_lot_id]
    assert [
        row["lot_id"]
        for row in dispatched.json()["items"][0]["inventory_sources"]
    ] == [first_lot_id]
    assert [
        row["lot_id"]
        for row in cancelled.json()["items"][0]["inventory_sources"]
    ] == [first_lot_id]
    for response in (dispatched, cancelled):
        sources = response.json()["items"][0]["inventory_sources"]
        assert all(
            row["quantity_to_pick_stock"] > 0
            or row["quantity_to_pick_requirement"] > 0
            for row in sources
        )
        assert second_lot_id not in {row["lot_id"] for row in sources}
    assert printed.status_code == 200
    assert not any(
        fragment in key.lower()
        for key in all_keys(printed.json())
        for fragment in ("location", "lot", "source")
    )


def test_finished_release_returns_only_unconsumed_and_keeps_consumed_coverage(
    delivery_inventory_app,
) -> None:
    app, factory = delivery_inventory_app
    lot_id, version = add_finished(
        factory, product_id=1, quantity=6, key="b2-finished-release"
    )
    with TestClient(app) as client:
        login(client)
        item_id = create_order(
            client,
            po="B2-FINISHED-RELEASE",
            product_id=1,
            quantity=6,
            finished=[finished_plan(lot_id, version, 6)],
        )
        delivery = create_delivery(client, item_id, 4)
        dispatched = client.put(f"/api/deliveries/{delivery['id']}/dispatch")
    assert dispatched.status_code == 200, dispatched.text
    with factory() as db:
        release_active_finished_reservations_for_items(
            db,
            order_item_ids=[item_id],
            operator_id=1,
            reason="测试仅释放未消耗余额",
            idempotency_prefix="b2-release-unconsumed",
            allow_downstream=True,
        )
        db.commit()
        reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == item_id,
                InventoryReservation.reservation_type == "finished_order",
            )
        )
        lot = db.get(InventoryLot, lot_id)
        assert reservation.consumed_stock_quantity == 4
        assert reservation.released_stock_quantity == 2
        assert active_finished_reserved_qty(db, item_id) == 4
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (
            2,
            0,
            4,
        )


@pytest.mark.parametrize(
    ("product_id", "quantity", "dispatch_quantity", "expected_components"),
    [
        (2, 3, 1, {"whole": 2}),
        (3, 2, 1, {"cover": 1, "base": 1}),
    ],
)
def test_double_and_a3_components_consume_exact_pieces(
    delivery_inventory_app,
    product_id: int,
    quantity: int,
    dispatch_quantity: int,
    expected_components: dict[str, int],
) -> None:
    app, factory = delivery_inventory_app
    if product_id == 2:
        lot_id, version = add_semi(
            factory,
            quantity=6,
            key="b2-double",
            pieces_per_box=2,
        )
        plans = [semi_plan(lot_id, version, 6)]
    else:
        cover_id, cover_version = add_semi(
            factory,
            quantity=2,
            key="b2-cover",
            component="cover",
            length=400,
            width=300,
        )
        base_id, base_version = add_semi(
            factory,
            quantity=2,
            key="b2-base",
            component="base",
            length=375,
            width=275,
        )
        plans = [
            semi_plan(cover_id, cover_version, 2, "cover"),
            semi_plan(base_id, base_version, 2, "base"),
        ]
    with TestClient(app) as client:
        login(client)
        item_id = create_order(
            client,
            po=f"B2-COMP-{product_id}",
            product_id=product_id,
            quantity=quantity,
            semi=plans,
        )
        delivery = create_delivery(client, item_id, dispatch_quantity)
        dispatched = client.put(f"/api/deliveries/{delivery['id']}/dispatch")
    assert dispatched.status_code == 200, dispatched.text
    with factory() as db:
        requirements = db.scalars(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.order_item_id == item_id
            )
        ).all()
        consumed = {}
        for requirement in requirements:
            consumed[requirement.component_type] = sum(
                row.consumed_requirement_quantity
                for row in db.scalars(
                    select(InventoryReservation).where(
                        InventoryReservation.semi_requirement_id == requirement.id
                    )
                ).all()
            )
        assert consumed == expected_components


def test_yield_whole_sheet_multi_delivery_cancel_preserves_remaining_need(
    delivery_inventory_app,
) -> None:
    app, factory = delivery_inventory_app
    lot_id, version = add_semi(
        factory,
        quantity=2,
        key="b2-yield-three",
        yield_factor=3,
    )
    with TestClient(app) as client:
        login(client)
        item_id = create_order(
            client,
            po="B2-YIELD",
            product_id=1,
            quantity=5,
            semi=[semi_plan(lot_id, version, 5)],
        )
        first = create_delivery(client, item_id, 1)
        assert client.put(f"/api/deliveries/{first['id']}/dispatch").status_code == 200
        second = create_delivery(client, item_id, 2)
        assert client.put(f"/api/deliveries/{second['id']}/dispatch").status_code == 200
        cancelled_first = client.put(f"/api/deliveries/{first['id']}/cancel")
        assert cancelled_first.status_code == 200, cancelled_first.text
        with factory() as db:
            interim_reservation = db.scalar(
                select(InventoryReservation).where(
                    InventoryReservation.order_item_id == item_id,
                    InventoryReservation.reservation_type == "semi_order",
                )
            )
            assert db.get(OrderItem, item_id).delivered_quantity == 2
            assert interim_reservation.consumed_stock_quantity == 1
            assert interim_reservation.consumed_requirement_quantity == 3
        cancelled_second = client.put(f"/api/deliveries/{second['id']}/cancel")
    assert cancelled_second.status_code == 200, cancelled_second.text
    with factory() as db:
        reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == item_id,
                InventoryReservation.reservation_type == "semi_order",
            )
        )
        lot = db.get(InventoryLot, lot_id)
        item = db.get(OrderItem, item_id)
        allocations = db.scalars(select(DeliveryInventoryAllocation)).all()
        assert item.delivered_quantity == 0
        assert reservation.consumed_stock_quantity == 0
        assert reservation.consumed_requirement_quantity == 0
        assert (lot.quantity_reserved, lot.quantity_consumed) == (2, 0)
        assert any(row.status == "reversed" for row in allocations)


def test_dispatch_inventory_failure_rolls_back_all_lines(
    delivery_inventory_app,
) -> None:
    app, factory = delivery_inventory_app
    first_lot_id, first_version = add_semi(
        factory, quantity=2, key="b2-rollback-first"
    )
    second_lot_id, second_version = add_semi(
        factory, quantity=2, key="b2-rollback-second"
    )
    with TestClient(app) as client:
        login(client)
        first_item = create_order(
            client,
            po="B2-ROLLBACK-1",
            product_id=1,
            quantity=2,
            semi=[semi_plan(first_lot_id, first_version, 2)],
        )
        second_item = create_order(
            client,
            po="B2-ROLLBACK-2",
            product_id=1,
            quantity=2,
            semi=[semi_plan(second_lot_id, second_version, 2)],
        )
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [
                    {"order_item_id": first_item, "delivered_quantity": 1},
                    {"order_item_id": second_item, "delivered_quantity": 1},
                ],
            },
        )
        assert created.status_code == 201, created.text
        with factory() as db:
            second_lot = db.get(InventoryLot, second_lot_id)
            second_lot.quantity_reserved = 0
            db.commit()
        dispatched = client.put(
            f"/api/deliveries/{created.json()['id']}/dispatch"
        )
    assert dispatched.status_code == 409
    with factory() as db:
        first_lot = db.get(InventoryLot, first_lot_id)
        first_reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == first_item,
                InventoryReservation.reservation_type == "semi_order",
            )
        )
        assert db.get(Delivery, created.json()["id"]).status == "pending"
        assert db.get(OrderItem, first_item).delivered_quantity == 0
        assert db.get(OrderItem, second_item).delivered_quantity == 0
        assert first_reservation.consumed_stock_quantity == 0
        assert (first_lot.quantity_reserved, first_lot.quantity_consumed) == (2, 0)


def test_partial_semi_coverage_is_not_eligible_but_full_coverage_is(
    delivery_inventory_app,
) -> None:
    app, factory = delivery_inventory_app
    partial_id, partial_version = add_semi(
        factory, quantity=2, key="b2-partial"
    )
    full_id, full_version = add_semi(factory, quantity=5, key="b2-full")
    with TestClient(app) as client:
        login(client)
        partial_item = create_order(
            client,
            po="B2-PARTIAL",
            product_id=1,
            quantity=5,
            semi=[semi_plan(partial_id, partial_version, 2)],
        )
        rejected = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [
                    {"order_item_id": partial_item, "delivered_quantity": 1}
                ],
            },
        )
        full_item = create_order(
            client,
            po="B2-FULL",
            product_id=1,
            quantity=5,
            semi=[semi_plan(full_id, full_version, 5)],
        )
        accepted = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [{"order_item_id": full_item, "delivered_quantity": 1}],
            },
        )
    assert rejected.status_code == 400
    assert "当前未送数量为 0" in rejected.json()["detail"]
    assert accepted.status_code == 201, accepted.text
