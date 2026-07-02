from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryMovement,
    InventoryReservation,
    WarehouseLocation,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
    finished_inventory_candidates,
    manual_finished_in,
    release_finished_reservation,
    reserve_finished_inventory,
)


@pytest.fixture()
def reservation_db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "finished-reservation.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="inventory-admin",
            password_hash="test",
            role="admin",
            real_name="库存管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=7001,
            customer_code="FG-A",
            name="成品库存客户A",
            payment_term_days=0,
            credit_limit=0,
        )
        other_customer = Customer(
            customer_number=7002,
            customer_code="FG-B",
            name="成品库存客户B",
            payment_term_days=0,
            credit_limit=0,
        )
        db.add_all([admin, customer, other_customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="FG-P001",
            customer_material_code="FG-M001",
            product_name="成品测试箱",
            box_category="normal",
            length_mm=800,
            width_mm=200,
            height_mm=100,
            default_material_code="A416D",
            flute_type="BE",
        )
        other_product = Product(
            customer_id=customer.id,
            product_code="FG-P002",
            customer_material_code="FG-M002",
            product_name="其他产品",
            box_category="normal",
        )
        location = WarehouseLocation(
            location_code="FG-01",
            location_name="成品一号库位",
            warehouse_type="finished",
        )
        db.add_all([product, other_product, location])
        db.flush()
        order = Order(
            order_number="TM-FG-001",
            customer_id=customer.id,
            order_date=date.today(),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("100"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=100,
            unit_price=Decimal("1"),
            subtotal=Decimal("100"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=product.product_code,
            snapshot_product_name=product.product_name,
            snapshot_spec="800×200×100mm",
            snapshot_material="A416D",
            snapshot_pieces_per_box=1,
            snapshot_report_length_mm=800,
            snapshot_report_width_mm=200,
        )
        db.add(item)
        db.flush()
        db.commit()
        yield db, {
            "admin": admin,
            "customer": customer,
            "other_customer": other_customer,
            "product": product,
            "other_product": other_product,
            "location": location,
            "order": order,
            "item": item,
        }


def add_lot(db: Session, data: dict, *, quantity: int = 50, key: str):
    return manual_finished_in(
        db,
        customer_id=data["customer"].id,
        product_id=data["product"].id,
        location_id=data["location"].id,
        quantity=quantity,
        stock_date=date.today(),
        source_type="manual",
        remarks=None,
        operator_id=data["admin"].id,
        idempotency_key=key,
    )


def reserve(db: Session, data: dict, lot, quantity: int, key: str):
    return reserve_finished_inventory(
        db,
        order_item_id=data["item"].id,
        inventory_lot_id=lot.id,
        quantity=quantity,
        expected_version=lot.version,
        operator_id=data["admin"].id,
        idempotency_key=key,
        warning_acknowledged_codes=(
            ["GENERAL_FINISHED_STOCK"] if lot.finished_detail.is_general else []
        ),
    )


def test_candidates_use_exact_product_customer_and_active_available_rules(
    reservation_db,
) -> None:
    db, data = reservation_db
    dedicated = add_lot(db, data, key="candidate-dedicated")
    general = add_lot(db, data, key="candidate-general")
    general.finished_detail.is_general = True
    general.finished_detail.owner_customer_id = None
    other_customer = add_lot(db, data, key="candidate-other-customer")
    other_customer.finished_detail.owner_customer_id = data["other_customer"].id
    frozen = add_lot(db, data, key="candidate-frozen")
    frozen.status = "frozen"
    zero = add_lot(db, data, key="candidate-zero")
    zero.quantity_available = 0
    other_product = add_lot(db, data, key="candidate-other-product")
    other_product.finished_detail.product_id = data["other_product"].id
    db.flush()

    rows = finished_inventory_candidates(db, data["item"].id)
    ids = {row.id for row in rows}
    assert ids == {dedicated.id, general.id}
    assert general.finished_detail.is_general is True


def test_general_inventory_requires_explicit_acknowledgement(reservation_db) -> None:
    db, data = reservation_db
    lot = add_lot(db, data, key="general-ack-lot")
    lot.finished_detail.is_general = True
    lot.finished_detail.owner_customer_id = None
    db.flush()
    with pytest.raises(WarehouseInventoryError, match="通用库存必须人工确认"):
        reserve_finished_inventory(
            db,
            order_item_id=data["item"].id,
            inventory_lot_id=lot.id,
            quantity=10,
            expected_version=lot.version,
            operator_id=data["admin"].id,
            idempotency_key="general-no-ack",
            warning_acknowledged_codes=[],
        )


def test_reserve_updates_balances_writes_reservation_and_movement(reservation_db) -> None:
    db, data = reservation_db
    lot = add_lot(db, data, quantity=60, key="reserve-lot")
    row = reserve(db, data, lot, 30, "reserve-30")
    db.flush()
    db.refresh(lot)
    assert data["item"].quantity == 100
    assert (lot.quantity_available, lot.quantity_reserved) == (30, 30)
    assert row.status == "active"
    assert row.credited_requirement_quantity == 30
    movement = db.scalar(
        select(InventoryMovement).where(InventoryMovement.reservation_id == row.id)
    )
    assert movement.movement_type == "reserve"
    assert (movement.before_available, movement.after_available) == (60, 30)
    assert (movement.before_reserved, movement.after_reserved) == (0, 30)


def test_reserve_guards_quantity_version_and_idempotency(reservation_db) -> None:
    db, data = reservation_db
    lot = add_lot(db, data, quantity=120, key="guard-lot")
    first = reserve(db, data, lot, 30, "same-reserve")
    repeated = reserve_finished_inventory(
        db,
        order_item_id=data["item"].id,
        inventory_lot_id=lot.id,
        quantity=30,
        expected_version=1,
        operator_id=data["admin"].id,
        idempotency_key="same-reserve",
        warning_acknowledged_codes=[],
    )
    assert repeated.id == first.id
    db.refresh(lot)
    assert lot.quantity_reserved == 30
    with pytest.raises(WarehouseInventoryError, match="刷新候选"):
        reserve_finished_inventory(
            db,
            order_item_id=data["item"].id,
            inventory_lot_id=lot.id,
            quantity=1,
            expected_version=1,
            operator_id=data["admin"].id,
            idempotency_key="wrong-version",
            warning_acknowledged_codes=[],
        )
    with pytest.raises(WarehouseInventoryError, match="剩余可抵扣"):
        reserve_finished_inventory(
            db,
            order_item_id=data["item"].id,
            inventory_lot_id=lot.id,
            quantity=71,
            expected_version=lot.version,
            operator_id=data["admin"].id,
            idempotency_key="over-order",
            warning_acknowledged_codes=[],
        )


def test_release_restores_balances_and_cannot_repeat(reservation_db) -> None:
    db, data = reservation_db
    lot = add_lot(db, data, quantity=50, key="release-lot")
    reservation = reserve(db, data, lot, 20, "reserve-release")
    released = release_finished_reservation(
        db,
        reservation_id=reservation.id,
        operator_id=data["admin"].id,
        release_reason="订单调整",
        idempotency_key="release-once",
    )
    db.flush()
    db.refresh(lot)
    assert released.status == "released"
    assert (lot.quantity_available, lot.quantity_reserved) == (50, 0)
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.reservation_id == reservation.id,
            InventoryMovement.movement_type == "release_reserve",
        )
    )
    assert movement is not None
    with pytest.raises(WarehouseInventoryError, match="不能重复释放"):
        release_finished_reservation(
            db,
            reservation_id=reservation.id,
            operator_id=data["admin"].id,
            release_reason="再次释放",
            idempotency_key="release-twice",
        )


def test_requisition_pending_uses_remaining_production_quantity(reservation_db) -> None:
    from app.api.requisition import pending_requisitions

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="req-lot")
    reserve(db, data, lot, 30, "req-reserve")
    result = pending_requisitions(db=db, _user=data["admin"])
    row = next(item for item in result["items"] if item["item_id"] == data["item"].id)
    assert row["quantity"] == 100
    assert row["finished_inventory_reserved_qty"] == 30
    assert row["production_required_qty"] == 70
    assert row["required_piece_qty"] == 70
    assert row["requisition_qty"] == 70
    data["item"].snapshot_pieces_per_box = 2
    result = pending_requisitions(db=db, _user=data["admin"])
    row = next(item for item in result["items"] if item["item_id"] == data["item"].id)
    assert row["required_piece_qty"] == 140


def test_requisition_batch_uses_remaining_quantity_and_keeps_order_quantity(
    reservation_db,
) -> None:
    from app.api.requisition import RequisitionBatchCreate, create_batch

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="batch-lot")
    reserve(db, data, lot, 30, "batch-reserve")
    result = create_batch(
        payload=RequisitionBatchCreate(
            supplier_name="测试供应商",
            items=[
                {
                    "order_item_id": data["item"].id,
                    "inventory_deducted_qty": 0,
                    "requisition_qty": 999,
                    "cardboard_len": 800,
                    "cardboard_width": 200,
                    "special_process": "一开一",
                }
            ],
        ),
        db=db,
        user=data["admin"],
    )
    assert result["items"][0]["requisition_qty"] == 70
    assert data["item"].quantity == 100


def test_full_reservation_has_zero_requisition_and_is_delivery_eligible(
    reservation_db,
) -> None:
    from app.api.deliveries import _pending_query
    from app.api.requisition import pending_requisitions

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="full-lot")
    reserve(db, data, lot, 100, "full-reserve")
    result = pending_requisitions(db=db, _user=data["admin"])
    row = next(item for item in result["items"] if item["item_id"] == data["item"].id)
    assert row["fully_covered_by_finished_inventory"] is True
    assert row["production_required_qty"] == 0
    assert row["required_piece_qty"] == 0
    assert row["requisition_qty"] == 0
    pending_delivery_ids = {
        item.item_id for item in db.execute(_pending_query()).all()
    }
    assert data["item"].id in pending_delivery_ids


def test_full_reservation_cannot_generate_purchase_requisition(
    reservation_db,
) -> None:
    from app.api.requisition import RequisitionBatchCreate, create_batch

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="full-block-lot")
    reserve(db, data, lot, 100, "full-block-reserve")
    payload = RequisitionBatchCreate(
        supplier_name="测试供应商",
        items=[
            {
                "order_item_id": data["item"].id,
                "inventory_deducted_qty": 0,
                "cardboard_len": 800,
                "cardboard_width": 200,
                "special_process": "一开一",
            }
        ],
    )
    with pytest.raises(HTTPException, match="无需报料"):
        create_batch(payload=payload, db=db, user=data["admin"])


def test_delete_order_releases_active_finished_reservation(reservation_db) -> None:
    from app.api.orders import _delete_orders_in_transaction

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="delete-lot")
    reservation = reserve(db, data, lot, 40, "delete-reserve")
    _delete_orders_in_transaction(
        db,
        orders=[data["order"]],
        user=data["admin"],
    )
    assert db.get(Order, data["order"].id) is None
    released = db.get(InventoryReservation, reservation.id)
    db.refresh(lot)
    assert released.status == "released"
    assert (lot.quantity_available, lot.quantity_reserved) == (100, 0)
    assert active_finished_reserved_qty(db, data["item"].id) == 0


def test_cancel_order_releases_active_finished_reservation(reservation_db) -> None:
    from app.api.orders import OrderStatusRequest, update_order_status

    db, data = reservation_db
    lot = add_lot(db, data, quantity=100, key="cancel-lot")
    reservation = reserve(db, data, lot, 25, "cancel-reserve")
    result = update_order_status(
        order_id=data["order"].id,
        payload=OrderStatusRequest(status="cancelled", remark="客户取消订单"),
        db=db,
        user=data["admin"],
    )
    db.refresh(lot)
    assert result["status"] == "cancelled"
    assert db.get(InventoryReservation, reservation.id).status == "released"
    assert (lot.quantity_available, lot.quantity_reserved) == (100, 0)


def test_ui_contains_real_reservation_controls_and_hides_free_deduction() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")
    warehouse = (
        Path(__file__).resolve().parents[1] / "static" / "warehouse.html"
    ).read_text(encoding="utf-8")
    for marker in (
        "已预占成品库存",
        "需生产数量",
        "查看可用成品库存",
        "人工确认抵扣",
        "取消抵扣",
        "/api/warehouse/finished/reservations",
        "/api/warehouse/reservations/",
        "成品库存已全额抵扣",
    ):
        assert marker in source
    assert 'v-model.number="line.inventory_deducted_qty"' not in source
    assert "reserve" in warehouse
    assert "release_reserve" in warehouse
    assert "已预占" in warehouse
    assert "已消耗" in warehouse
