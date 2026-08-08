"""P1-15B 无订单成品受控送货契约测试。

本文件刻意不改动现有订单送货行为。基线尚未实现
``source_mode='unordered_finished'`` 时整组测试跳过；实现合入后，这些
测试应直接成为该新来源分支的 API/库存服务回归门禁。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem, DeliveryPickTask
from app.models.finance import ReturnReceiptItem, StatementItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryMovement,
    InventoryReservation,
    UnorderedFinishedDeliveryReversal,
    UnorderedFinishedDeliveryAllocation,
    WarehouseLocation,
)
from app.core.security import hash_password
from app.services.warehouse_inventory import manual_finished_in


_DELIVERIES_SOURCE = Path(__file__).parents[1] / "app" / "api" / "deliveries.py"


def _unordered_finished_contract_is_implemented() -> bool:
    """Keep the pre-implementation baseline green without weakening the contract.

    This guard is intentionally tied to production code, rather than an env var:
    once the new API source mode is introduced, every assertion below is enabled
    automatically in normal CI.
    """

    source = _DELIVERIES_SOURCE.read_text(encoding="utf-8")
    return "source_mode" in source and "unordered_finished" in source


pytestmark = pytest.mark.skipif(
    not _unordered_finished_contract_is_implemented(),
    reason="P1-15B source_mode='unordered_finished' API has not been implemented",
)


UNORDERED_CANDIDATES_PATH = "/api/deliveries/unordered-finished-candidates"


@dataclass(frozen=True)
class UnorderedFinishedSeed:
    customer_a_id: int
    customer_b_id: int
    priced_product_id: int
    no_price_product_id: int
    regular_order_id: int
    regular_order_item_id: int
    free_first_lot_id: int
    free_second_lot_id: int
    reserved_lot_id: int
    general_lot_id: int
    other_customer_lot_id: int
    frozen_lot_id: int


@pytest.fixture()
def unordered_finished_delivery_app(tmp_path: Path):
    """Small, isolated application containing delivery, order and inventory facts."""

    from app.api.auth import router as auth_router
    from app.api.deliveries import pick_router, router as deliveries_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.api.orders import router as orders_router
    from app.api.warehouse import router as warehouse_router

    engine = create_sqlite_engine(tmp_path / "p1-15b-unordered-finished.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="admin",
            password_hash=hash_password("AdminPass123!"),
            role="admin",
            real_name="测试管理员",
            display_name="测试管理员",
            must_change_password=False,
        )
        customer_a = Customer(
            customer_number=15101,
            customer_code="P115B-A",
            name="P1-15B 客户甲",
            payment_term_days=0,
            credit_limit=0,
        )
        customer_b = Customer(
            customer_number=15102,
            customer_code="P115B-B",
            name="P1-15B 客户乙",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all([admin, customer_a, customer_b])
        db.flush()
        db.add_all(
            [
                Product(
                    customer_id=customer_a.id,
                    product_code="P115B-BOX-A",
                    customer_material_code="P115B-BOX-A-M",
                    product_name="P1-15B 客户甲成品箱",
                    box_category="normal",
                    box_style="普通箱",
                    sale_unit_price=Decimal("3.6000"),
                    sale_unit_price_no_tax=Decimal("3.6000"),
                ),
                Product(
                    customer_id=customer_a.id,
                    product_code="P115B-NOPRICE",
                    customer_material_code="P115B-NOPRICE-M",
                    product_name="P1-15B 待定价成品箱",
                    box_category="normal",
                    box_style="普通箱",
                ),
                Product(
                    customer_id=customer_a.id,
                    product_code="P115B-ORDERED",
                    customer_material_code="P115B-ORDERED-M",
                    product_name="P1-15B 已有订单成品箱",
                    box_category="normal",
                    box_style="普通箱",
                    sale_unit_price=Decimal("3.6000"),
                ),
                Product(
                    customer_id=customer_b.id,
                    product_code="P115B-BOX-B",
                    customer_material_code="P115B-BOX-B-M",
                    product_name="P1-15B 客户乙成品箱",
                    box_category="normal",
                    box_style="普通箱",
                    sale_unit_price=Decimal("8.0000"),
                ),
                WarehouseLocation(
                    location_code="P115B-FG",
                    location_name="P1-15B 成品库",
                    warehouse_type="finished",
                ),
            ]
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(warehouse_router, prefix="/api/warehouse")
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(pick_router, prefix="/api/delivery-picks")
    app.include_router(finance_router, prefix="/api/finance")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, factory


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "AdminPass123!"},
    )
    assert response.status_code == 200, response.text


def _add_finished(
    factory,
    *,
    customer_id: int | None,
    product_id: int,
    quantity: int,
    key: str,
    is_general: bool = False,
) -> tuple[int, int]:
    with factory() as db:
        location = db.scalar(
            select(WarehouseLocation).where(
                WarehouseLocation.location_code == "P115B-FG"
            )
        )
        assert location is not None
        lot = manual_finished_in(
            db,
            customer_id=customer_id,
            product_id=product_id,
            location_id=location.id,
            quantity=quantity,
            stock_date=date(2026, 7, 29),
            source_type="manual",
            remarks=None,
            operator_id=1,
            idempotency_key=key,
            is_general=is_general,
        )
        db.commit()
        return int(lot.id), int(lot.version)


def _reserve_for_normal_order(
    client: TestClient,
    *,
    product_id: int,
    lot_id: int,
    lot_version: int,
    order_quantity: int = 10,
    reserved_quantity: int = 5,
) -> tuple[int, int]:
    response = client.post(
        "/api/orders",
        json={
            "customer_id": 1,
            "customer_po": "P115B-RESERVED",
            "order_date": "2026-07-29",
            "import_integrity_status": "ok",
            "items": [
                {
                    "client_line_id": "P115B-RESERVED-1",
                    "product_id": product_id,
                    "quantity": order_quantity,
                    "unit_price": "3.60",
                    "reservation_plan": {
                        "finished": [
                            {
                                "lot_id": lot_id,
                                "expected_version": lot_version,
                                "requested_qty": reserved_quantity,
                                "recommendation_source": "dedicated",
                                "confirmed": True,
                            }
                        ],
                        "semi": [],
                    },
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return int(body["id"]), int(body["items"][0]["id"])


def _seed(app: FastAPI, factory) -> UnorderedFinishedSeed:
    with factory() as db:
        customer_a = db.scalar(select(Customer).where(Customer.customer_code == "P115B-A"))
        customer_b = db.scalar(select(Customer).where(Customer.customer_code == "P115B-B"))
        priced = db.scalar(select(Product).where(Product.product_code == "P115B-BOX-A"))
        no_price = db.scalar(select(Product).where(Product.product_code == "P115B-NOPRICE"))
        ordered = db.scalar(select(Product).where(Product.product_code == "P115B-ORDERED"))
        other = db.scalar(select(Product).where(Product.product_code == "P115B-BOX-B"))
        assert customer_a and customer_b and priced and no_price and ordered and other
        customer_a_id, customer_b_id = customer_a.id, customer_b.id
        priced_id, no_price_id, ordered_id, other_id = (
            priced.id,
            no_price.id,
            ordered.id,
            other.id,
        )

    free_first_lot_id, _ = _add_finished(
        factory, customer_id=customer_a_id, product_id=priced_id, quantity=12, key="p115b-free-1"
    )
    free_second_lot_id, _ = _add_finished(
        factory, customer_id=customer_a_id, product_id=priced_id, quantity=8, key="p115b-free-2"
    )
    reserved_lot_id, reserved_version = _add_finished(
        factory, customer_id=customer_a_id, product_id=ordered_id, quantity=10, key="p115b-reserved"
    )
    general_lot_id, _ = _add_finished(
        factory,
        customer_id=None,
        product_id=priced_id,
        quantity=10,
        key="p115b-general",
        is_general=True,
    )
    other_customer_lot_id, _ = _add_finished(
        factory, customer_id=customer_b_id, product_id=other_id, quantity=10, key="p115b-other"
    )
    frozen_lot_id, _ = _add_finished(
        factory, customer_id=customer_a_id, product_id=priced_id, quantity=9, key="p115b-frozen"
    )
    with factory() as db:
        frozen = db.get(InventoryLot, frozen_lot_id)
        assert frozen is not None
        frozen.status = "frozen"
        db.commit()

    with TestClient(app) as client:
        _login(client)
        regular_order_id, regular_order_item_id = _reserve_for_normal_order(
            client,
            product_id=ordered_id,
            lot_id=reserved_lot_id,
            lot_version=reserved_version,
        )

    return UnorderedFinishedSeed(
        customer_a_id=customer_a_id,
        customer_b_id=customer_b_id,
        priced_product_id=priced_id,
        no_price_product_id=no_price_id,
        regular_order_id=regular_order_id,
        regular_order_item_id=regular_order_item_id,
        free_first_lot_id=free_first_lot_id,
        free_second_lot_id=free_second_lot_id,
        reserved_lot_id=reserved_lot_id,
        general_lot_id=general_lot_id,
        other_customer_lot_id=other_customer_lot_id,
        frozen_lot_id=frozen_lot_id,
    )


def _unordered_payload(seed: UnorderedFinishedSeed, *, quantity: int = 10, price: str = "3.60") -> dict:
    return {
        "customer_id": seed.customer_a_id,
        "delivery_date": "2026-07-29",
        "source_mode": "unordered_finished",
        "lines": [
            {
                "source_type": "finished_stock",
                "product_id": seed.priced_product_id,
                "delivered_quantity": quantity,
                "unit_price": price,
                "allocations": [
                    {"inventory_lot_id": seed.free_first_lot_id, "quantity": min(quantity, 7)},
                    *(
                        [{"inventory_lot_id": seed.free_second_lot_id, "quantity": quantity - 7}]
                        if quantity > 7
                        else []
                    ),
                ],
            }
        ],
    }


def _create_unordered_delivery(client: TestClient, payload: dict) -> dict:
    response = client.post("/api/deliveries", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _stock_snapshot(factory, seed: UnorderedFinishedSeed) -> dict:
    lot_ids = [
        seed.free_first_lot_id,
        seed.free_second_lot_id,
        seed.reserved_lot_id,
        seed.general_lot_id,
        seed.other_customer_lot_id,
        seed.frozen_lot_id,
    ]
    with factory() as db:
        lots = {
            lot.id: (
                int(lot.quantity_available),
                int(lot.quantity_reserved),
                int(lot.quantity_consumed),
                int(lot.version),
            )
            for lot in db.scalars(select(InventoryLot).where(InventoryLot.id.in_(lot_ids))).all()
        }
        order = db.get(Order, seed.regular_order_id)
        item = db.get(OrderItem, seed.regular_order_item_id)
        assert order is not None and item is not None
        return {
            "lots": lots,
            "reservation_count": db.scalar(select(func.count()).select_from(InventoryReservation)),
            "movement_count": db.scalar(select(func.count()).select_from(InventoryMovement)),
            "order_status": order.status,
            "order_delivered": int(item.delivered_quantity),
        }


def _delivery_line(body: dict) -> dict:
    lines = body.get("lines") or body.get("items") or []
    assert len(lines) == 1, body
    return lines[0]


def _mixed_delivery_fixture(
    client: TestClient,
    factory,
    seed: UnorderedFinishedSeed,
) -> tuple[int, int, int]:
    order_lot_id, order_lot_version = _add_finished(
        factory,
        customer_id=seed.customer_a_id,
        product_id=seed.priced_product_id,
        quantity=10,
        key="p115b-mixed-order-lot",
    )
    order_id, order_item_id = _reserve_for_normal_order(
        client,
        product_id=seed.priced_product_id,
        lot_id=order_lot_id,
        lot_version=order_lot_version,
        order_quantity=10,
        reserved_quantity=10,
    )
    return order_id, order_item_id, order_lot_id


def _mixed_payload(
    seed: UnorderedFinishedSeed,
    *,
    order_item_id: int,
    order_quantity: int,
    unordered_quantity: int = 3,
) -> dict:
    return {
        "customer_id": seed.customer_a_id,
        "delivery_date": "2026-08-02",
        "source_mode": "mixed",
        "items": [
            {
                "source_type": "order",
                "order_item_id": order_item_id,
                "delivered_quantity": order_quantity,
            },
            {
                "source_type": "unordered_finished",
                "product_id": seed.priced_product_id,
                "delivered_quantity": unordered_quantity,
                "unit_price": "3.60",
                "allocations": [
                    {
                        "inventory_lot_id": seed.free_first_lot_id,
                        "quantity": unordered_quantity,
                    }
                ],
            },
        ],
    }


def _dispatch_unordered_delivery(client: TestClient, seed: UnorderedFinishedSeed) -> tuple[dict, dict]:
    delivery = _create_unordered_delivery(client, _unordered_payload(seed))
    dispatched = client.put(f"/api/deliveries/{delivery['id']}/dispatch")
    assert dispatched.status_code == 200, dispatched.text
    return delivery, _delivery_line(delivery)


def _short_receipt_payload(delivery_id: int, delivery_item_id: int, *, quantity: int) -> dict:
    return {
        "delivery_id": delivery_id,
        "actual_received_date": "2026-07-29",
        "signed_by": "客户仓管",
        "items": [
            {
                "delivery_item_id": delivery_item_id,
                "actual_received_quantity": quantity,
                "resolution_action": "accept_short",
                "difference_reason": "客户短收退回",
            }
        ],
    }


def _as_money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.00"))


def test_candidates_exclude_cross_customer_general_reserved_and_frozen(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    before = _stock_snapshot(factory, seed)

    with TestClient(app) as client:
        _login(client)
        response = client.get(
            UNORDERED_CANDIDATES_PATH,
            params={"customer_id": seed.customer_a_id},
        )

    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    returned_ids = {row["inventory_lot_id"] for row in rows}
    assert {seed.free_first_lot_id, seed.free_second_lot_id}.issubset(returned_ids)
    assert seed.reserved_lot_id not in returned_ids
    assert seed.general_lot_id not in returned_ids
    assert seed.other_customer_lot_id not in returned_ids
    assert seed.frozen_lot_id not in returned_ids
    assert _stock_snapshot(factory, seed) == before


def test_delivery_customer_candidates_are_exact_union_of_real_sources(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with factory() as db:
        order_customer = Customer(
            customer_number=15103,
            customer_code="P115B-C",
            name="P1-15B 仅订单待送客户",
            payment_term_days=0,
            credit_limit=0,
        )
        empty_customer = Customer(
            customer_number=15104,
            customer_code="P115B-D",
            name="P1-15B 无待送客户",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all([order_customer, empty_customer])
        db.flush()
        product = Product(
            customer_id=order_customer.id,
            product_code="P115B-ORDER-ONLY",
            customer_material_code="P115B-ORDER-ONLY-M",
            product_name="P1-15B 仅订单待送纸箱",
            box_category="normal",
            box_style="普通箱",
            sale_unit_price=Decimal("2.0000"),
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="P115B-ORDER-ONLY-001",
            customer_id=order_customer.id,
            customer_po="P115B-ORDER-ONLY",
            order_date=date(2026, 8, 4),
            delivery_date=date(2026, 8, 5),
            status="pending_delivery",
            payment_status="unpaid",
            total_amount=Decimal("20.00"),
        )
        db.add(order)
        db.flush()
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=10,
                delivered_quantity=0,
                unit_price=Decimal("2.0000"),
                subtotal=Decimal("20.00"),
                material_status="received",
                requisition_status="已入库",
                snapshot_product_name=product.product_name,
                snapshot_product_code=product.product_code,
            )
        )
        inactive_stock_customer = db.get(Customer, seed.customer_b_id)
        assert inactive_stock_customer is not None
        inactive_stock_customer.is_active = False
        order_customer_id = int(order_customer.id)
        empty_customer_id = int(empty_customer.id)
        db.commit()

    with TestClient(app) as client:
        _login(client)
        response = client.get("/api/deliveries/pending_items")
        lightweight = client.get("/api/deliveries/pending-customer-options")

    assert response.status_code == 200, response.text
    body = response.json()
    rows = {
        int(row["customer_id"]): row
        for row in body["customer_candidates"]
    }
    assert set(rows) == {seed.customer_a_id, order_customer_id}
    assert empty_customer_id not in rows
    assert seed.customer_b_id not in rows
    assert rows[seed.customer_a_id]["has_unordered_finished"] is True
    assert rows[seed.customer_a_id]["unordered_lot_count"] == 2
    assert rows[order_customer_id]["has_pending_orders"] is True
    assert rows[order_customer_id]["pending_item_count"] == 1
    assert len(body["customer_candidates"]) == 2
    assert lightweight.status_code == 200, lightweight.text
    assert {
        int(row["customer_id"]): row
        for row in lightweight.json()["items"]
    } == rows


@pytest.mark.parametrize("submitted_price", [None, 0, "0.0000"])
def test_unordered_finished_draft_can_keep_price_pending_without_stock_write(
    unordered_finished_delivery_app,
    submitted_price,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    lot_id, _version = _add_finished(
        factory,
        customer_id=seed.customer_a_id,
        product_id=seed.no_price_product_id,
        quantity=6,
        key="p115b-no-price-draft",
    )
    payload = {
        "customer_id": seed.customer_a_id,
        "delivery_date": "2026-08-04",
        "source_mode": "unordered_finished",
        "items": [
            {
                "source_type": "unordered_finished",
                "product_id": seed.no_price_product_id,
                "delivered_quantity": 4,
                "unit_price": submitted_price,
                "allocations": [{"inventory_lot_id": lot_id, "quantity": 4}],
            }
        ],
    }
    with factory() as db:
        before = db.get(InventoryLot, lot_id)
        assert before is not None
        before_fact = (
            int(before.quantity_available),
            int(before.quantity_reserved),
            int(before.quantity_consumed),
            int(before.version),
        )

    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/deliveries", json=payload)

    assert response.status_code == 201, response.text
    line = _delivery_line(response.json())
    with factory() as db:
        stored = db.get(DeliveryItem, line["id"])
        lot = db.get(InventoryLot, lot_id)
        assert stored is not None and lot is not None
        assert stored.unit_price_snapshot is None
        assert stored.price_source == "pending"
        assert (
            int(lot.quantity_available),
            int(lot.quantity_reserved),
            int(lot.quantity_consumed),
            int(lot.version),
        ) == before_fact


def test_unordered_finished_draft_can_push_mobile_pick_without_dispatch(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    before = _stock_snapshot(factory, seed)
    with TestClient(app) as client:
        _login(client)
        delivery = _create_unordered_delivery(client, _unordered_payload(seed))
        pushed = client.post(f"/api/deliveries/{delivery['id']}/pick-task")
        repeated = client.post(f"/api/deliveries/{delivery['id']}/pick-task")

    assert pushed.status_code == 201, pushed.text
    assert repeated.status_code == 201, repeated.text
    task = pushed.json()
    assert repeated.json()["id"] == task["id"]
    assert len(task["items"]) == 1
    assert task["items"][0]["order_item_id"] is None
    assert {
        row["lot_id"] for row in task["items"][0]["location_lines"]
    } == {seed.free_first_lot_id, seed.free_second_lot_id}
    with factory() as db:
        saved = db.get(Delivery, delivery["id"])
        assert saved is not None and saved.status == "pending"
    assert _stock_snapshot(factory, seed) == before


def test_unordered_finished_pick_task_can_later_dispatch(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        delivery = _create_unordered_delivery(client, _unordered_payload(seed))
        pushed = client.post(f"/api/deliveries/{delivery['id']}/pick-task")
        dispatched = client.put(f"/api/deliveries/{delivery['id']}/dispatch")

    assert pushed.status_code == 201, pushed.text
    assert dispatched.status_code == 200, dispatched.text
    with factory() as db:
        db.expire_all()
        saved = db.get(Delivery, delivery["id"])
        task = db.get(DeliveryPickTask, pushed.json()["id"])
        assert saved is not None and saved.status == "dispatched"
        assert task is not None and task.status == "dispatched"


def test_unordered_finished_partial_pick_only_resizes_pending_draft(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    before = _stock_snapshot(factory, seed)
    with TestClient(app) as client:
        _login(client)
        delivery = _create_unordered_delivery(client, _unordered_payload(seed))
        task = client.post(f"/api/deliveries/{delivery['id']}/pick-task").json()
        task_item = task["items"][0]
        partial = client.put(
            f"/api/delivery-picks/{task['id']}/items/{task_item['id']}",
            json={"pick_status": "partial", "picked_quantity": 6},
        )
        assert partial.status_code == 200, partial.text
        submitted = client.post(f"/api/delivery-picks/{task['id']}/submit")
        assert submitted.status_code == 200, submitted.text
        applied = client.post(f"/api/delivery-picks/{task['id']}/apply")
        assert applied.status_code == 200, applied.text

    with factory() as db:
        stored = db.get(Delivery, delivery["id"])
        assert stored is not None and stored.status == "pending"
        line = db.scalar(
            select(DeliveryItem).where(DeliveryItem.delivery_id == stored.id)
        )
        assert line is not None and int(line.delivered_quantity) == 6
        assert sum(
            int(row.planned_quantity)
            for row in db.scalars(
                select(UnorderedFinishedDeliveryAllocation).where(
                    UnorderedFinishedDeliveryAllocation.delivery_item_id
                    == line.id
                )
            ).all()
        ) == 6
    assert _stock_snapshot(factory, seed) == before


def test_draft_is_unordered_only_and_freezes_price_without_order_side_effects(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    before = _stock_snapshot(factory, seed)
    with TestClient(app) as client:
        _login(client)
        delivery = _create_unordered_delivery(client, _unordered_payload(seed))
        mixed = _unordered_payload(seed)
        mixed["lines"].append(
            {
                "source_type": "order",
                "order_item_id": seed.regular_order_item_id,
                "delivered_quantity": 1,
            }
        )
        mixed_response = client.post("/api/deliveries", json=mixed)

    assert mixed_response.status_code == 400, mixed_response.text
    line = _delivery_line(delivery)
    with factory() as db:
        stored = db.get(DeliveryItem, line["id"])
        assert stored is not None
        assert stored.order_item_id is None
        assert str(stored.unit_price_snapshot) == "3.6000"
        product = db.get(Product, seed.priced_product_id)
        assert product is not None
        product.sale_unit_price = Decimal("9.9900")
        db.commit()
        db.expire_all()
        assert str(db.get(DeliveryItem, line["id"]).unit_price_snapshot) == "3.6000"
    assert _stock_snapshot(factory, seed) == before


def test_draft_save_and_print_have_zero_inventory_side_effects(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    before = _stock_snapshot(factory, seed)
    with TestClient(app) as client:
        _login(client)
        delivery = _create_unordered_delivery(client, _unordered_payload(seed))
        printed = client.get(f"/api/deliveries/{delivery['id']}/print")

    assert printed.status_code == 200, printed.text
    payload = printed.json()
    assert payload["items"][0]["customer_po"] == "无订单库存"
    assert "source_mode" not in payload
    assert "source_type" not in payload["items"][0]
    assert _stock_snapshot(factory, seed) == before


def test_dispatch_consumes_exact_selected_lots_and_never_mutates_order(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        delivery = _create_unordered_delivery(client, _unordered_payload(seed))
        dispatched = client.put(f"/api/deliveries/{delivery['id']}/dispatch")

    assert dispatched.status_code == 200, dispatched.text
    with factory() as db:
        first = db.get(InventoryLot, seed.free_first_lot_id)
        second = db.get(InventoryLot, seed.free_second_lot_id)
        assert first is not None and second is not None
        assert int(first.quantity_available) == 5
        assert int(second.quantity_available) == 5
        moved_lot_ids = set(
            db.scalars(
                select(InventoryMovement.inventory_lot_id).where(
                    InventoryMovement.related_delivery_id == delivery["id"],
                    InventoryMovement.movement_type == "consume",
                )
            ).all()
        )
        assert moved_lot_ids == {seed.free_first_lot_id, seed.free_second_lot_id}
        order_item = db.get(OrderItem, seed.regular_order_item_id)
        assert order_item is not None
        assert int(order_item.delivered_quantity) == 0
        assert db.scalar(
            select(func.count()).select_from(DeliveryPickTask).where(
                DeliveryPickTask.delivery_id == delivery["id"]
            )
        ) == 0


def test_unordered_full_dispatch_releases_and_cancel_restores_pallet(
    unordered_finished_delivery_app,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryLocationMovement,
        InventoryPallet,
        InventoryPalletItem,
    )

    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with factory() as db:
        location = WarehouseLocation(
            location_code="P115B-PALLET-01",
            location_name="P1-15B 实体栈板位",
            warehouse_type="finished",
            is_active=True,
        )
        pallet = InventoryPallet(
            pallet_code="P115B-PALLET-FULL",
            location=location,
            status="active",
            is_current=True,
            needs_relocation=False,
            created_by=1,
            updated_by=1,
        )
        db.add_all([location, pallet])
        db.flush()
        for lot_id in (seed.free_first_lot_id, seed.free_second_lot_id):
            lot = db.get(InventoryLot, lot_id)
            assert lot is not None and lot.finished_detail is not None
            lot.warehouse_location_id = location.id
            db.add(
                InventoryPalletItem(
                    pallet_id=pallet.id,
                    inventory_lot_id=lot.id,
                    customer_id=seed.customer_a_id,
                    product_id=seed.priced_product_id,
                    inventory_code=lot.finished_detail.inventory_code_snapshot,
                    customer_name_snapshot="P1-15B 客户甲",
                    product_name=lot.finished_detail.product_name_snapshot,
                    item_type="finished",
                    quantity=lot.quantity_available,
                    unit="boxes",
                    match_status="matched",
                    created_by=1,
                )
            )
        db.add(
            InventoryLocationMovement(
                pallet_id=pallet.id,
                from_location_id=None,
                to_location_id=location.id,
                movement_type="create",
                operator_id=1,
            )
        )
        db.commit()
        pallet_id = int(pallet.id)
        location_id = int(location.id)

    payload = _unordered_payload(seed, quantity=20)
    payload["lines"][0]["allocations"] = [
        {"inventory_lot_id": seed.free_first_lot_id, "quantity": 12},
        {"inventory_lot_id": seed.free_second_lot_id, "quantity": 8},
    ]
    with TestClient(app) as client:
        _login(client)
        delivery = _create_unordered_delivery(client, payload)
        dispatched = client.put(f"/api/deliveries/{delivery['id']}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        with factory() as db:
            pallet = db.get(InventoryPallet, pallet_id)
            assert pallet is not None
            assert pallet.is_current is False
            assert pallet.location_id is None
        cancelled = client.put(f"/api/deliveries/{delivery['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text

    with factory() as db:
        pallet = db.get(InventoryPallet, pallet_id)
        first = db.get(InventoryLot, seed.free_first_lot_id)
        second = db.get(InventoryLot, seed.free_second_lot_id)
        assert pallet is not None
        assert pallet.is_current is True
        assert pallet.location_id == location_id
        assert first is not None and int(first.quantity_available) == 12
        assert second is not None and int(second.quantity_available) == 8


def test_dispatch_competition_or_insufficient_stock_rolls_back_completely(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    first_payload = _unordered_payload(seed, quantity=10)
    second_payload = _unordered_payload(seed, quantity=10)
    # Both drafts select the same physical batch; draft creation must not reserve it.
    for payload in (first_payload, second_payload):
        payload["lines"][0]["allocations"] = [
            {"inventory_lot_id": seed.free_first_lot_id, "quantity": 10}
        ]

    with TestClient(app) as client:
        _login(client)
        first = _create_unordered_delivery(client, first_payload)
        second = _create_unordered_delivery(client, second_payload)
        first_dispatch = client.put(f"/api/deliveries/{first['id']}/dispatch")
        second_dispatch = client.put(f"/api/deliveries/{second['id']}/dispatch")

    assert first_dispatch.status_code == 200, first_dispatch.text
    assert second_dispatch.status_code == 409, second_dispatch.text
    with factory() as db:
        lot = db.get(InventoryLot, seed.free_first_lot_id)
        failed = db.get(Delivery, second["id"])
        assert lot is not None and failed is not None
        assert int(lot.quantity_available) == 2
        assert failed.status == "pending"
        assert db.scalar(
            select(func.count()).select_from(InventoryMovement).where(
                InventoryMovement.related_delivery_id == second["id"]
            )
        ) == 0
        item = db.get(OrderItem, seed.regular_order_item_id)
        assert item is not None and int(item.delivered_quantity) == 0


def test_cancel_restores_only_original_lots_and_is_not_double_applied(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        delivery = _create_unordered_delivery(client, _unordered_payload(seed))
        assert client.put(f"/api/deliveries/{delivery['id']}/dispatch").status_code == 200
        cancelled = client.put(f"/api/deliveries/{delivery['id']}/cancel")
        cancelled_again = client.put(f"/api/deliveries/{delivery['id']}/cancel")
        edit_after_cancel = client.put(
            f"/api/deliveries/{delivery['id']}",
            json=_unordered_payload(seed),
        )

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "voided"
    assert cancelled_again.status_code == 409, cancelled_again.text
    assert edit_after_cancel.status_code == 409, edit_after_cancel.text
    assert "已作废" in edit_after_cancel.json()["detail"]
    with factory() as db:
        first = db.get(InventoryLot, seed.free_first_lot_id)
        second = db.get(InventoryLot, seed.free_second_lot_id)
        reserved = db.get(InventoryLot, seed.reserved_lot_id)
        archived = db.get(Delivery, delivery["id"])
        assert first is not None and second is not None and reserved is not None
        assert archived is not None and archived.status == "voided"
        assert int(first.quantity_available) == 12
        assert int(second.quantity_available) == 8
        assert int(reserved.quantity_available) == 5
        reversed_lot_ids = set(
            db.scalars(
                select(InventoryMovement.inventory_lot_id).where(
                    InventoryMovement.related_delivery_id == delivery["id"],
                    InventoryMovement.movement_type == "reverse_consume",
                )
            ).all()
        )
        assert reversed_lot_ids == {seed.free_first_lot_id, seed.free_second_lot_id}
        order_item = db.get(OrderItem, seed.regular_order_item_id)
        assert order_item is not None and int(order_item.delivered_quantity) == 0


def test_short_receipt_restores_only_original_allocation_and_is_traceable(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        delivery, line = _dispatch_unordered_delivery(client, seed)
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_short_receipt_payload(delivery["id"], line["id"], quantity=8),
        )

    assert receipt.status_code == 201, receipt.text
    receipt_body = receipt.json()
    receipt_item_id = receipt_body["items"][0]["id"]
    with factory() as db:
        first = db.get(InventoryLot, seed.free_first_lot_id)
        second = db.get(InventoryLot, seed.free_second_lot_id)
        reserved = db.get(InventoryLot, seed.reserved_lot_id)
        assert first is not None and second is not None and reserved is not None
        # Dispatch took 7 from the first lot and 3 from the second.  The
        # short 2 must return only to that original second allocation.
        assert int(first.quantity_available) == 5
        assert int(second.quantity_available) == 7
        assert int(reserved.quantity_available) == 5
        reversal = db.scalar(
            select(UnorderedFinishedDeliveryReversal).where(
                UnorderedFinishedDeliveryReversal.return_receipt_item_id == receipt_item_id
            )
        )
        assert reversal is not None
        assert reversal.reversal_kind == "receipt_short_return"
        assert int(reversal.reversal_quantity) == 2
        movement = db.get(InventoryMovement, reversal.inventory_movement_id)
        assert movement is not None
        assert movement.movement_type == "reverse_consume"
        assert movement.inventory_lot_id == seed.free_second_lot_id
        order_item = db.get(OrderItem, seed.regular_order_item_id)
        assert order_item is not None and int(order_item.delivered_quantity) == 0


def test_receipt_edit_reconsumes_previous_return_before_applying_new_shortage(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        delivery, line = _dispatch_unordered_delivery(client, seed)
        created = client.post(
            "/api/finance/return_receipts",
            json=_short_receipt_payload(delivery["id"], line["id"], quantity=8),
        )
        assert created.status_code == 201, created.text
        receipt_id = created.json()["id"]
        updated = client.put(
            f"/api/finance/return_receipts/{receipt_id}",
            json=_short_receipt_payload(delivery["id"], line["id"], quantity=9),
        )

    assert updated.status_code == 200, updated.text
    with factory() as db:
        first = db.get(InventoryLot, seed.free_first_lot_id)
        second = db.get(InventoryLot, seed.free_second_lot_id)
        assert first is not None and second is not None
        assert int(first.quantity_available) == 5
        # The old short-2 return is first re-consumed, then the new short-1
        # return is restored.  A direct overwrite would incorrectly leave 7.
        assert int(second.quantity_available) == 6
        reversals = db.scalars(
            select(UnorderedFinishedDeliveryReversal)
            .where(UnorderedFinishedDeliveryReversal.return_receipt_item_id.is_not(None))
            .order_by(UnorderedFinishedDeliveryReversal.id)
        ).all()
        assert [row.status for row in reversals] == ["reconsumed", "active"]
        assert [int(row.reversal_quantity) for row in reversals] == [2, 1]


def test_cancel_receipt_rejects_when_the_returned_original_lot_stock_was_used(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        delivery, line = _dispatch_unordered_delivery(client, seed)
        created = client.post(
            "/api/finance/return_receipts",
            json=_short_receipt_payload(delivery["id"], line["id"], quantity=8),
        )
        assert created.status_code == 201, created.text
        # Consume exactly the two boxes just returned.  The original receipt
        # must now be immutable even though the same lot still has older stock.
        follow_up = _unordered_payload(seed, quantity=2)
        follow_up["lines"][0]["allocations"] = [
            {"inventory_lot_id": seed.free_second_lot_id, "quantity": 2}
        ]
        later_delivery = _create_unordered_delivery(client, follow_up)
        assert client.put(f"/api/deliveries/{later_delivery['id']}/dispatch").status_code == 200
        cancelled = client.post(
            f"/api/finance/return_receipts/{created.json()['id']}/cancel"
        )

    assert cancelled.status_code == 409, cancelled.text
    with factory() as db:
        receipt_item = db.scalar(
            select(ReturnReceiptItem).where(
                ReturnReceiptItem.return_receipt_id == created.json()["id"]
            )
        )
        assert receipt_item is not None
        reversal = db.scalar(
            select(UnorderedFinishedDeliveryReversal).where(
                UnorderedFinishedDeliveryReversal.return_receipt_item_id == receipt_item.id
            )
        )
        assert reversal is not None and reversal.status == "active"


def test_cancelled_unordered_receipt_post_requires_editing_the_original_receipt(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        delivery, line = _dispatch_unordered_delivery(client, seed)
        created = client.post(
            "/api/finance/return_receipts",
            json=_short_receipt_payload(delivery["id"], line["id"], quantity=8),
        )
        assert created.status_code == 201, created.text
        receipt_id = created.json()["id"]
        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")
        retried = client.post(
            "/api/finance/return_receipts",
            json=_short_receipt_payload(delivery["id"], line["id"], quantity=9),
        )

    assert cancelled.status_code == 200, cancelled.text
    assert retried.status_code == 409, retried.text
    assert "编辑原回单" in retried.json()["detail"]
    assert str(receipt_id) in retried.json()["detail"]
    with factory() as db:
        receipt_items = db.scalars(
            select(ReturnReceiptItem).where(
                ReturnReceiptItem.return_receipt_id == receipt_id
            )
        ).all()
        assert len(receipt_items) == 1
        reversal = db.scalar(
            select(UnorderedFinishedDeliveryReversal).where(
                UnorderedFinishedDeliveryReversal.return_receipt_item_id
                == receipt_items[0].id
            )
        )
        assert reversal is not None and reversal.status == "reconsumed"


def test_reconfirming_the_same_shortage_creates_a_new_traceable_return_cycle(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        delivery, line = _dispatch_unordered_delivery(client, seed)
        created = client.post(
            "/api/finance/return_receipts",
            json=_short_receipt_payload(delivery["id"], line["id"], quantity=8),
        )
        assert created.status_code == 201, created.text
        receipt_id = created.json()["id"]
        cancelled = client.post(f"/api/finance/return_receipts/{receipt_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        reconfirmed = client.put(
            f"/api/finance/return_receipts/{receipt_id}",
            json=_short_receipt_payload(delivery["id"], line["id"], quantity=8),
        )

    assert reconfirmed.status_code == 200, reconfirmed.text
    with factory() as db:
        second = db.get(InventoryLot, seed.free_second_lot_id)
        assert second is not None
        assert int(second.quantity_available) == 7
        reversals = db.scalars(
            select(UnorderedFinishedDeliveryReversal)
            .where(UnorderedFinishedDeliveryReversal.return_receipt_item_id.is_not(None))
            .order_by(UnorderedFinishedDeliveryReversal.id)
        ).all()
        assert [row.status for row in reversals] == ["reconsumed", "active"]
        assert [int(row.reversal_quantity) for row in reversals] == [2, 2]
        assert len({row.idempotency_key for row in reversals}) == 2
        assert reversals[1].idempotency_key.endswith("-cycle-2")


def test_statement_uses_signed_quantity_and_frozen_price_without_order_item(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        delivery, line = _dispatch_unordered_delivery(client, seed)
        receipt = client.post(
            "/api/finance/return_receipts",
            json=_short_receipt_payload(delivery["id"], line["id"], quantity=8),
        )
        assert receipt.status_code == 201, receipt.text
        with factory() as db:
            product = db.get(Product, seed.priced_product_id)
            assert product is not None
            product.sale_unit_price = Decimal("99.9900")
            db.commit()
        pending = client.get(
            "/api/finance/pending_statements",
            # The fixture customer's existing settlement cut-off is the 20th,
            # so a 2026-07-29 delivery belongs to the August statement cycle.
            params={"customer_id": seed.customer_a_id, "statement_month": "2026-08"},
        )
        statement = client.post(
            "/api/finance/statements",
            json={
                "customer_id": seed.customer_a_id,
                "statement_month": "2026-08",
                "delivery_ids": [delivery["id"]],
            },
        )

    assert pending.status_code == 200, pending.text
    assert pending.json()["deliveries"], pending.text
    pending_line = pending.json()["deliveries"][0]["items"][0]
    assert pending_line["actual_received_quantity"] == 8
    assert _as_money(pending_line["unit_price"]) == Decimal("3.60")
    assert _as_money(pending_line["receivable_amount"]) == Decimal("28.80")
    assert statement.status_code == 201, statement.text
    with factory() as db:
        statement_item = db.scalar(select(StatementItem))
        delivery_item = db.get(DeliveryItem, line["id"])
        assert statement_item is not None and delivery_item is not None
        assert delivery_item.order_item_id is None
        assert statement_item.actual_received_quantity == 8
        assert str(statement_item.unit_price_snapshot) == "3.6000"
        assert _as_money(statement_item.receivable_amount) == Decimal("28.80")

    with TestClient(app) as client:
        _login(client)
        locked = client.put(
            f"/api/finance/return_receipts/{receipt.json()['id']}",
            json=_short_receipt_payload(delivery["id"], line["id"], quantity=9),
        )
    assert locked.status_code == 409, locked.text


def test_unordered_candidates_are_server_filtered_and_paginated(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        page_one = client.get(
            UNORDERED_CANDIDATES_PATH,
            params={
                "customer_id": seed.customer_a_id,
                "q": "P115B-BOX-A",
                "page": 1,
                "page_size": 1,
            },
        )
        page_two = client.get(
            UNORDERED_CANDIDATES_PATH,
            params={
                "customer_id": seed.customer_a_id,
                "q": "P115B-BOX-A",
                "page": 2,
                "page_size": 1,
            },
        )
        none = client.get(
            UNORDERED_CANDIDATES_PATH,
            params={
                "customer_id": seed.customer_a_id,
                "q": "不存在的规格",
                "page": 1,
                "page_size": 12,
            },
        )

    assert page_one.status_code == 200, page_one.text
    assert page_two.status_code == 200, page_two.text
    assert none.status_code == 200, none.text
    first = page_one.json()
    second = page_two.json()
    assert first["total"] == 2
    assert first["total_pages"] == 2
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["inventory_lot_id"] != second["items"][0]["inventory_lot_id"]
    assert none.json()["total"] == 0
    assert none.json()["items"] == []


def test_same_product_requires_all_order_quantity_before_unordered_supplement(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        _order_id, order_item_id, _order_lot_id = _mixed_delivery_fixture(
            client,
            factory,
            seed,
        )
        pending = client.get(
            "/api/deliveries/pending-items/search",
            params={
                "customer_id": seed.customer_a_id,
                "inventory_code": "P115B-BOX-A",
                "page": 1,
                "page_size": 12,
            },
        )
        assert pending.status_code == 200, pending.text
        candidate = next(
            item
            for item in pending.json()["items"]
            if int(item["order_item_id"]) == order_item_id
        )
        deliverable = int(candidate["deliverable_quantity"])
        blocked = client.post(
            "/api/deliveries",
            json=_mixed_payload(
                seed,
                order_item_id=order_item_id,
                order_quantity=deliverable - 1,
            ),
        )
        accepted = client.post(
            "/api/deliveries",
            json=_mixed_payload(
                seed,
                order_item_id=order_item_id,
                order_quantity=deliverable,
            ),
        )

    assert deliverable > 1
    assert blocked.status_code == 409, blocked.text
    assert "请先把订单待送数量全部加入" in blocked.json()["detail"]
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert body["source_mode"] == "mixed"
    assert {item["source_type"] for item in body["items"]} == {
        "order",
        "unordered_finished",
    }


def test_mixed_draft_dispatch_and_cancel_are_atomic_and_traceable(
    unordered_finished_delivery_app,
) -> None:
    app, factory = unordered_finished_delivery_app
    seed = _seed(app, factory)
    with TestClient(app) as client:
        _login(client)
        order_id, order_item_id, order_lot_id = _mixed_delivery_fixture(
            client,
            factory,
            seed,
        )
        pending = client.get(
            "/api/deliveries/pending-items/search",
            params={
                "customer_id": seed.customer_a_id,
                "inventory_code": "P115B-BOX-A",
                "page_size": 12,
            },
        ).json()["items"]
        deliverable = int(
            next(
                item
                for item in pending
                if int(item["order_item_id"]) == order_item_id
            )["deliverable_quantity"]
        )
        with factory() as db:
            order_lot_before = db.get(InventoryLot, order_lot_id)
            assert order_lot_before is not None
            order_lot_balance_before = (
                int(order_lot_before.quantity_available or 0),
                int(order_lot_before.quantity_reserved or 0),
                int(order_lot_before.quantity_consumed or 0),
            )
        before = _stock_snapshot(factory, seed)
        created = client.post(
            "/api/deliveries",
            json=_mixed_payload(
                seed,
                order_item_id=order_item_id,
                order_quantity=deliverable,
            ),
        )
        assert created.status_code == 201, created.text
        after_draft = _stock_snapshot(factory, seed)
        dispatched = client.put(f"/api/deliveries/{created.json()['id']}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        cancelled = client.put(f"/api/deliveries/{created.json()['id']}/cancel")

    assert before == after_draft
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "voided"
    with factory() as db:
        order = db.get(Order, order_id)
        order_item = db.get(OrderItem, order_item_id)
        order_lot = db.get(InventoryLot, order_lot_id)
        free_lot = db.get(InventoryLot, seed.free_first_lot_id)
        delivery = db.get(Delivery, created.json()["id"])
        assert order is not None and order_item is not None and delivery is not None
        assert order_lot is not None and free_lot is not None
        assert int(order_item.delivered_quantity or 0) == 0
        assert (
            int(order_lot.quantity_available or 0),
            int(order_lot.quantity_reserved or 0),
            int(order_lot.quantity_consumed or 0),
        ) == order_lot_balance_before
        assert int(free_lot.quantity_available or 0) == 12
        assert delivery.status == "voided"
        assert db.scalar(
            select(func.count())
            .select_from(InventoryMovement)
            .where(InventoryMovement.related_delivery_id == delivery.id)
        ) >= 4
