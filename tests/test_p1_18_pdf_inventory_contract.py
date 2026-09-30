from __future__ import annotations

from collections.abc import Generator
import json
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.fixed_shelf import ShelfLotState
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    InventoryReservation,
    OrderItemSemiRequirement,
    WarehouseLocation,
)


PASSWORD = "123456"


def _add_finished_lot(
    db: Session,
    *,
    lot_number: str,
    customer: Customer,
    product: Product,
    location: WarehouseLocation,
    quantity: int,
) -> InventoryLot:
    lot = InventoryLot(
        lot_number=lot_number,
        inventory_type="finished",
        warehouse_location_id=location.id,
        quantity_available=quantity,
        quantity_reserved=0,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        unit="boxes",
        status="active",
        source_type="stocktake",
        stock_date=date.today(),
        stock_date_accuracy="exact",
        last_movement_at=datetime.now(),
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
    return lot


@pytest.fixture()
def inventory_preview_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router

    engine = create_sqlite_engine(tmp_path / "p1-18-inventory-preview.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as db:
        allowed = User(
            username="preview-allowed",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="允许预览账号",
            customer_access_mode="selected",
            must_change_password=False,
        )
        no_warehouse = User(
            username="preview-no-warehouse",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="无库存权限账号",
            customer_access_mode="selected",
            must_change_password=False,
        )
        other_customer_user = User(
            username="preview-other-customer",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="其他客户账号",
            customer_access_mode="selected",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=91801,
            customer_code="P118-A",
            name="P1-18匿名客户A",
            payment_term_days=0,
            credit_limit=0,
        )
        other_customer = Customer(
            customer_number=91802,
            customer_code="P118-B",
            name="P1-18匿名客户B",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all(
            [
                allowed,
                no_warehouse,
                other_customer_user,
                customer,
                other_customer,
            ]
        )
        db.flush()
        db.add_all(
            [
                UserCustomerScope(user_id=allowed.id, customer_id=customer.id),
                UserCustomerScope(
                    user_id=no_warehouse.id,
                    customer_id=customer.id,
                ),
                UserCustomerScope(
                    user_id=other_customer_user.id,
                    customer_id=other_customer.id,
                ),
                UserPermissionOverride(
                    user_id=allowed.id,
                    permission_code="warehouse.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=other_customer_user.id,
                    permission_code="warehouse.view",
                    is_allowed=True,
                ),
            ]
        )

        def product(code: str, name: str, **overrides) -> Product:
            values = {
                "customer_id": customer.id,
                "product_code": code,
                "customer_material_code": code,
                "product_name": name,
                "box_category": "normal",
                "box_style": "A1",
                "legacy_material_text": "A416D",
                "default_material_code": "A416D",
                "flute_type": "B",
                "layer_count": 3,
                "report_length_mm": 800,
                "report_width_mm": 600,
                "splice_mode": "single",
                "pieces_per_box": 1,
            }
            values.update(overrides)
            return Product(**values)

        full_product = product("P118-FULL", "足额抵扣产品")
        partial_product = product("P118-PART", "部分抵扣产品")
        none_product = product("P118-NONE", "无成品库存产品")
        shared_product = product("P118-SHARED", "共享批次产品")
        cutting_product = product(
            "P118-CUT2",
            "一开二产品",
            box_style="模切内盒",
            default_cutting_mode="一开二",
        )
        location = WarehouseLocation(
            location_code="P118-FG",
            location_name="P1-18成品测试库位",
            warehouse_type="finished",
        )
        db.add_all(
            [
                full_product,
                partial_product,
                none_product,
                shared_product,
                cutting_product,
                location,
            ]
        )
        db.flush()
        full_lot = _add_finished_lot(
            db,
            lot_number="P118-FULL-LOT",
            customer=customer,
            product=full_product,
            location=location,
            quantity=100,
        )
        partial_lot = _add_finished_lot(
            db,
            lot_number="P118-PART-LOT",
            customer=customer,
            product=partial_product,
            location=location,
            quantity=60,
        )
        shared_lot = _add_finished_lot(
            db,
            lot_number="P118-SHARED-LOT",
            customer=customer,
            product=shared_product,
            location=location,
            quantity=5,
        )
        db.commit()
        ids = {
            "customer": customer.id,
            "other_customer": other_customer.id,
            "full_product": full_product.id,
            "partial_product": partial_product.id,
            "none_product": none_product.id,
            "shared_product": shared_product.id,
            "cutting_product": cutting_product.id,
            "full_lot": full_lot.id,
            "partial_lot": partial_lot.id,
            "shared_lot": shared_lot.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, factory, ids


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _finished_plan(lot_id: int, requested_qty: int) -> dict:
    return {
        "lot_id": lot_id,
        "expected_version": 1,
        "requested_qty": requested_qty,
        "recommendation_source": "dedicated",
        "confirmed": True,
    }


def _preview_item(
    *,
    line: str,
    product_id: int,
    quantity: int,
    finished: list[dict] | None = None,
) -> dict:
    return {
        "client_line_id": line,
        "product_id": product_id,
        "quantity": quantity,
        "reservation_plan": {
            "finished": finished or [],
            "semi": [],
        },
    }


def _database_snapshot(factory: sessionmaker[Session]) -> dict:
    with factory() as db:
        return {
            "orders": db.scalar(select(func.count()).select_from(Order)),
            "order_items": db.scalar(select(func.count()).select_from(OrderItem)),
            "reservations": db.scalar(
                select(func.count()).select_from(InventoryReservation)
            ),
            "movements": db.scalar(
                select(func.count()).select_from(InventoryMovement)
            ),
            "semi_requirements": db.scalar(
                select(func.count()).select_from(OrderItemSemiRequirement)
            ),
            "lots": [
                tuple(row)
                for row in db.execute(
                    select(
                        InventoryLot.id,
                        InventoryLot.quantity_available,
                        InventoryLot.quantity_reserved,
                        InventoryLot.quantity_consumed,
                        InventoryLot.version,
                    ).order_by(InventoryLot.id)
                ).all()
            ],
        }


def test_batch_preview_returns_three_states_one_to_two_and_is_read_only(
    inventory_preview_app,
) -> None:
    app, factory, ids = inventory_preview_app
    before = _database_snapshot(factory)

    with TestClient(app) as client:
        _login(client, "preview-allowed")
        response = client.post(
            "/api/orders/inventory-draft-preview",
            json={
                "customer_id": ids["customer"],
                "items": [
                    _preview_item(
                        line="FULL",
                        product_id=ids["full_product"],
                        quantity=100,
                        finished=[_finished_plan(ids["full_lot"], 100)],
                    ),
                    _preview_item(
                        line="PARTIAL",
                        product_id=ids["partial_product"],
                        quantity=100,
                        finished=[_finished_plan(ids["partial_lot"], 60)],
                    ),
                    _preview_item(
                        line="NONE",
                        product_id=ids["none_product"],
                        quantity=100,
                    ),
                    _preview_item(
                        line="CUT2",
                        product_id=ids["cutting_product"],
                        quantity=101,
                    ),
                ],
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["calculation_scope"] == "read_only_new_order_draft"
    rows = {row["client_line_id"]: row for row in payload["items"]}

    assert rows["FULL"] | {
        "interaction_state": "ready",
        "coverage_state": "full",
        "finished_planned_quantity": 100,
        "production_required_quantity": 0,
        "shortage_quantity": 0,
        "requisition_sheet_quantity": 0,
    } == rows["FULL"]
    assert rows["PARTIAL"] | {
        "interaction_state": "ready",
        "coverage_state": "partial",
        "finished_planned_quantity": 60,
        "production_required_quantity": 40,
        "shortage_quantity": 40,
        "requisition_sheet_quantity": 40,
    } == rows["PARTIAL"]
    assert rows["NONE"] | {
        "interaction_state": "ready",
        "coverage_state": "none",
        "finished_candidate_available_quantity": 0,
        "finished_planned_quantity": 0,
        "production_required_quantity": 100,
        "shortage_quantity": 100,
        "requisition_sheet_quantity": 100,
    } == rows["NONE"]
    assert rows["CUT2"] | {
        "interaction_state": "ready",
        "coverage_state": "none",
        "required_piece_quantity": 101,
        "remaining_required_piece_quantity": 101,
        "cutting_mode": "一开二",
        "cutting_factor": 2,
        "requisition_sheet_quantity": 51,
        "requisition_unit": "张",
    } == rows["CUT2"]

    assert _database_snapshot(factory) == before


def test_batch_preview_allocates_a_shared_lot_only_once(
    inventory_preview_app,
) -> None:
    app, _factory, ids = inventory_preview_app
    shared_plan = [_finished_plan(ids["shared_lot"], 4)]

    with TestClient(app) as client:
        _login(client, "preview-allowed")
        response = client.post(
            "/api/orders/inventory-draft-preview",
            json={
                "customer_id": ids["customer"],
                "items": [
                    _preview_item(
                        line="SHARED-1",
                        product_id=ids["shared_product"],
                        quantity=4,
                        finished=shared_plan,
                    ),
                    _preview_item(
                        line="SHARED-2",
                        product_id=ids["shared_product"],
                        quantity=4,
                        finished=shared_plan,
                    ),
                    _preview_item(
                        line="SHARED-3",
                        product_id=ids["shared_product"],
                        quantity=4,
                        finished=shared_plan,
                    ),
                ],
            },
        )

    assert response.status_code == 200, response.text
    first, second, third = response.json()["items"]
    assert first["coverage_state"] == "full"
    assert first["finished_planned_quantity"] == 4
    assert second["coverage_state"] == "partial"
    assert second["finished_planned_quantity"] == 1
    assert second["production_required_quantity"] == 3
    assert third["coverage_state"] == "none"
    assert third["interaction_state"] == "ready"
    assert third["finished_candidate_available_quantity"] == 0
    assert third["finished_planned_quantity"] == 0
    assert sum(row["finished_planned_quantity"] for row in (first, second, third)) == 5


def test_batch_preview_requires_warehouse_permission_and_customer_scope(
    inventory_preview_app,
) -> None:
    app, _factory, ids = inventory_preview_app
    request = {
        "customer_id": ids["customer"],
        "items": [
            _preview_item(
                line="SCOPE",
                product_id=ids["none_product"],
                quantity=1,
            )
        ],
    }

    with TestClient(app) as client:
        _login(client, "preview-no-warehouse")
        no_permission = client.post(
            "/api/orders/inventory-draft-preview",
            json=request,
        )
    assert no_permission.status_code == 403
    assert "仓库库存查看权限" in no_permission.json()["detail"]

    with TestClient(app) as client:
        _login(client, "preview-other-customer")
        outside_scope = client.post(
            "/api/orders/inventory-draft-preview",
            json=request,
        )
    assert outside_scope.status_code == 403
    assert "客户" in outside_scope.json()["detail"]


def test_batch_preview_rejects_duplicate_client_line_id(
    inventory_preview_app,
) -> None:
    app, _factory, ids = inventory_preview_app
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "preview-allowed")
        response = client.post(
            "/api/orders/inventory-draft-preview",
            json={
                "customer_id": ids["customer"],
                "items": [
                    _preview_item(
                        line="DUPLICATE",
                        product_id=ids["none_product"],
                        quantity=1,
                    ),
                    _preview_item(
                        line="DUPLICATE",
                        product_id=ids["partial_product"],
                        quantity=1,
                    ),
                ],
            },
        )

    assert response.status_code == 409, response.text
    assert "client_line_id 重复" in response.json()["detail"]


def test_batch_preview_stock_projection_is_physical_includes_formal_reservations_and_debits_current_line(
    inventory_preview_app,
) -> None:
    app, factory, ids = inventory_preview_app
    with factory() as db:
        customer = db.get(Customer, ids["customer"])
        product = db.get(Product, ids["shared_product"])
        location = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code == "P118-FG"))
        fully_reserved = _add_finished_lot(
            db,
            lot_number="P118-SHARED-FULLY-RESERVED",
            customer=customer,
            product=product,
            location=location,
            quantity=7,
        )
        fully_reserved.quantity_available = 0
        fully_reserved.quantity_reserved = 7
        db.commit()

    with TestClient(app) as client:
        _login(client, "preview-allowed")
        response = client.post(
            "/api/orders/inventory-draft-preview",
            json={
                "customer_id": ids["customer"],
                "items": [
                    _preview_item(
                        line="PHYSICAL-1",
                        product_id=ids["shared_product"],
                        quantity=4,
                        finished=[_finished_plan(ids["shared_lot"], 4)],
                    ),
                    _preview_item(
                        line="PHYSICAL-2-NOT-ADOPTED",
                        product_id=ids["shared_product"],
                        quantity=4,
                    ),
                ],
            },
        )

    assert response.status_code == 200, response.text
    first, second = response.json()["items"]
    # These fields are physical-ledger facts.  The fully reserved lot is part
    # of actual stock but cannot increase available stock for this draft.
    assert first["finished_stock_on_hand_quantity"] == 12
    assert first["finished_stock_reserved_quantity"] == 7
    assert first["finished_stock_available_quantity"] == 5
    assert first["finished_stock_unit"] == "只"
    assert first["finished_stock_line_allocated_quantity"] == 4
    assert first["finished_stock_allocated_quantity"] == 4
    assert first["finished_stock_remaining_quantity"] == 1
    # A later non-adopted row must retain the preceding same-product debit.
    assert second["finished_stock_line_allocated_quantity"] == 0
    assert second["finished_stock_allocated_quantity"] == 4
    assert second["finished_stock_remaining_quantity"] == 1


def test_batch_preview_stock_projection_keeps_physical_quantity_and_basis_unit(
    inventory_preview_app,
) -> None:
    app, factory, ids = inventory_preview_app
    with factory() as db:
        product = db.get(Product, ids["shared_product"])
        lot = db.get(InventoryLot, ids["shared_lot"])
        product.unit = "\u7bb1"
        product.supply_mode = "external_purchase"
        product.external_packaging_category_code = "carton"
        product.external_packaging_specification_json = "{}"
        product.external_packaging_specification_summary = "\u5916\u8d2d\u7eb8\u7bb1"
        product.external_packaging_purchase_unit = "\u4e2a"
        product.external_packaging_candidate_snapshot_json = "{}"
        product.external_packaging_default_order_quantity_basis = 1
        product.external_packaging_default_purchase_quantity_basis = 10
        lot.quantity_available = 20
        lot.finished_detail.physical_basis_json = json.dumps({
            "quantity_basis": {
                "schema": 1,
                "ledger": "physical",
                "customer_id": ids["customer"],
                "product_id": product.id,
                "physical_unit": "\u4e2a",
            }
        })
        db.commit()

    with TestClient(app) as client:
        _login(client, "preview-allowed")
        response = client.post(
            "/api/orders/inventory-draft-preview",
            json={
                "customer_id": ids["customer"],
                "items": [_preview_item(
                    line="PHYSICAL-RATIO",
                    product_id=ids["shared_product"],
                    quantity=1,
                    finished=[_finished_plan(ids["shared_lot"], 1)],
                )],
            },
        )

    assert response.status_code == 200, response.text
    row = response.json()["items"][0]
    # One customer carton consumes ten physical pieces.  The new display facts
    # must remain physical instead of applying available_customer_quantity.
    assert row["finished_stock_on_hand_quantity"] == 20
    assert row["finished_stock_available_quantity"] == 20
    assert row["finished_stock_line_allocated_quantity"] == 10
    assert row["finished_stock_allocated_quantity"] == 10
    assert row["finished_stock_remaining_quantity"] == 10
    assert row["finished_stock_unit"] == "\u4e2a"


def test_batch_preview_shows_staged_physical_stock_without_making_it_allocatable(
    inventory_preview_app,
) -> None:
    app, factory, ids = inventory_preview_app
    with factory() as db:
        customer = db.get(Customer, ids["customer"])
        product = db.get(Product, ids["shared_product"])
        location = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code == "P118-FG"))
        staged = _add_finished_lot(
            db, lot_number="P118-SHARED-STAGED", customer=customer, product=product,
            location=location, quantity=7,
        )
        delivery = Delivery(
            delivery_number="P118-STAGED-DELIVERY", customer_id=customer.id,
            delivery_date=date.today(), source_mode="unordered_finished",
            status="pending", total_quantity=7,
        )
        db.add(delivery)
        db.flush()
        line = DeliveryItem(
            delivery_id=delivery.id, source_type="unordered_finished",
            product_id=product.id, product_code_snapshot=product.product_code,
            product_name_snapshot=product.product_name, unit_snapshot=product.unit,
            price_source="manual", delivered_quantity=7,
        )
        db.add(line)
        db.flush()
        db.add(ShelfLotState(lot_id=staged.id, staged_delivery_item_id=line.id))
        db.commit()

    with TestClient(app) as client:
        _login(client, "preview-allowed")
        response = client.post(
            "/api/orders/inventory-draft-preview",
            json={
                "customer_id": ids["customer"],
                "items": [_preview_item(
                    line="STAGED", product_id=ids["shared_product"], quantity=7,
                    finished=[_finished_plan(ids["shared_lot"], 7)],
                )],
            },
        )

    assert response.status_code == 200, response.text
    row = response.json()["items"][0]
    assert row["finished_stock_on_hand_quantity"] == 12
    assert row["finished_stock_reserved_quantity"] == 0
    assert row["finished_stock_available_quantity"] == 5
    assert row["finished_stock_unavailable_quantity"] == 7
    # The 7 staged pieces remain visible but the original available-only
    # allocator may allocate only the five ordinary pieces.
    assert row["finished_stock_line_allocated_quantity"] == 5
    assert row["finished_stock_remaining_quantity"] == 0
