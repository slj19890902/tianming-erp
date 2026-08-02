from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.api.warehouse import router as warehouse_router
from app.models import Base
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryLotTransfer,
    InventoryMovement,
    InventoryReservation,
    WarehouseLocation,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    transfer_staging_finished_lot,
)


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "p1-25c2.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


def _case(db: Session, *, available: int, reserved: int, suffix: str):
    customer = Customer(
        customer_number=9800 + len(suffix),
        customer_code=f"C-{suffix}",
        name=f"待送区测试客户-{suffix}",
        payment_term_days=0,
        credit_limit=0,
    )
    db.add(customer)
    db.flush()
    product = Product(
        customer_id=customer.id,
        product_code=f"P-{suffix}",
        customer_material_code=f"M-{suffix}",
        product_name=f"待送成品-{suffix}",
        box_category="normal",
        box_style="A1",
        length_mm=500,
        width_mm=300,
        height_mm=200,
        default_material_code="K=A",
        flute_type="B",
    )
    db.add(product)
    staging = WarehouseLocation(
        location_code="F1-DISPATCH-01",
        location_name="一楼待送区",
        warehouse_type="finished",
        warehouse_floor=1,
        area_code="DISPATCH",
        storage_type="temporary_aisle",
        placement_status="placed",
        source_version="P1-25C",
    )
    target = WarehouseLocation(
        location_code=f"F2-A-{suffix}",
        location_name=f"二楼正式库位-{suffix}",
        warehouse_type="finished",
        warehouse_floor=2,
        area_code="A",
        storage_type="ground",
        placement_status="placed",
    )
    db.add_all([staging, target])
    db.flush()
    order = Order(
        order_number=f"SO-{suffix}",
        customer_id=customer.id,
        order_date=date.today(),
        delivery_date=date.today(),
        status="production",
        payment_status="unpaid",
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        item_order_number=f"SO-{suffix}-001",
        item_sequence=1,
        quantity=max(available + reserved, 1),
        delivered_quantity=0,
        unit_price=Decimal("1"),
        subtotal=Decimal(max(available + reserved, 1)),
        snapshot_product_name=product.product_name,
        snapshot_product_code=product.product_code,
        snapshot_spec="500×300×200",
        snapshot_material="K=A",
        requisition_status="已报料",
        material_status="received",
        special_process="无",
        flute_type="B",
    )
    db.add(item)
    db.flush()
    lot = InventoryLot(
        lot_number=f"FG-{suffix}",
        inventory_type="finished",
        warehouse_location_id=staging.id,
        quantity_available=available,
        quantity_reserved=reserved,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        unit="boxes",
        status="active",
        source_type="production_completion",
        source_ref_type="production_completion",
        source_ref_id=7000 + len(suffix),
        stock_date=date.today(),
        stock_date_accuracy="exact",
        stock_date_original_text=date.today().isoformat(),
        last_movement_at=datetime.now(timezone.utc).replace(tzinfo=None),
        version=1,
    )
    lot.finished_detail = FinishedGoodsInventoryDetail(
        owner_customer_id=customer.id,
        owner_customer_name_snapshot=customer.name,
        is_general=False,
        product_id=product.id,
        inventory_code_snapshot=product.product_code,
        product_name_snapshot=product.product_name,
        box_type_snapshot="A1",
        length_mm=500,
        width_mm=300,
        height_mm=200,
        material_code_snapshot="K=A",
        flute_type_snapshot="B",
    )
    db.add(lot)
    db.flush()
    reservation = None
    if reserved:
        reservation = InventoryReservation(
            reservation_number=f"RS-{suffix}",
            inventory_lot_id=lot.id,
            reservation_type="finished_order",
            order_id=order.id,
            order_item_id=item.id,
            reserved_stock_quantity=reserved,
            credited_requirement_quantity=reserved,
            yield_factor=1,
            status="active",
            reserved_at=datetime.now(timezone.utc).replace(tzinfo=None),
            reservation_group_key=f"GROUP-{suffix}",
            reservation_group_requested_quantity=reserved,
            idempotency_key=f"reserve-{suffix}",
        )
        db.add(reservation)
        db.flush()
    return lot, target, reservation


def test_partial_transfer_moves_available_first_and_splits_reservation(db: Session) -> None:
    source, target_location, source_reservation = _case(
        db, available=6, reserved=50, suffix="PART"
    )
    result = transfer_staging_finished_lot(
        db,
        lot_id=source.id,
        expected_version=1,
        quantity=10,
        location_id=target_location.id,
        operator_id=None,
        idempotency_key="p1-25c2-partial",
    )
    db.flush()

    assert result.replayed is False
    assert result.source_lot.id != result.target_lot.id
    assert (result.source_lot.quantity_available, result.source_lot.quantity_reserved) == (0, 46)
    assert (result.target_lot.quantity_available, result.target_lot.quantity_reserved) == (6, 4)
    assert result.target_lot.source_type == "transfer"
    assert result.target_lot.source_ref_type == "production_completion"
    assert result.target_lot.finished_detail.owner_customer_id == source.finished_detail.owner_customer_id
    assert source_reservation is not None
    assert source_reservation.released_stock_quantity == 4
    moved_reservation = db.scalar(
        select(InventoryReservation).where(
            InventoryReservation.inventory_lot_id == result.target_lot.id
        )
    )
    assert moved_reservation is not None
    assert moved_reservation.reserved_stock_quantity == 4
    assert moved_reservation.order_item_id == source_reservation.order_item_id
    assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 1
    assert db.scalar(
        select(func.count(InventoryMovement.id)).where(
            InventoryMovement.movement_type == "location_transfer"
        )
    ) == 2
    assert sum(
        int(value)
        for value in (
            result.source_lot.quantity_available,
            result.source_lot.quantity_reserved,
            result.target_lot.quantity_available,
            result.target_lot.quantity_reserved,
        )
    ) == 56

    replay = transfer_staging_finished_lot(
        db,
        lot_id=source.id,
        expected_version=1,
        quantity=10,
        location_id=target_location.id,
        operator_id=None,
        idempotency_key="p1-25c2-partial",
    )
    assert replay.replayed is True
    assert replay.transfer.id == result.transfer.id
    assert db.scalar(select(func.count(InventoryLotTransfer.id))) == 1


def test_whole_transfer_keeps_same_lot_and_reservations(db: Session) -> None:
    source, target_location, reservation = _case(
        db, available=6, reserved=50, suffix="WHOLE"
    )
    result = transfer_staging_finished_lot(
        db,
        lot_id=source.id,
        expected_version=1,
        quantity=56,
        location_id=target_location.id,
        operator_id=None,
        idempotency_key="p1-25c2-whole",
    )
    db.flush()

    assert result.source_lot.id == result.target_lot.id == source.id
    assert result.target_lot.warehouse_location_id == target_location.id
    assert (result.target_lot.quantity_available, result.target_lot.quantity_reserved) == (6, 50)
    assert reservation is not None
    assert reservation.inventory_lot_id == source.id
    assert reservation.released_stock_quantity == 0
    assert result.transfer.available_quantity == 6
    assert result.transfer.reserved_quantity == 50


def test_transfer_rejects_stale_version_and_non_staging_lot(db: Session) -> None:
    source, target_location, _ = _case(db, available=5, reserved=0, suffix="GUARD")
    with pytest.raises(WarehouseInventoryError, match="刷新"):
        transfer_staging_finished_lot(
            db,
            lot_id=source.id,
            expected_version=2,
            quantity=5,
            location_id=target_location.id,
            operator_id=None,
            idempotency_key="p1-25c2-stale",
        )
    source.warehouse_location_id = target_location.id
    db.flush()
    with pytest.raises(WarehouseInventoryError, match="一楼待送区"):
        transfer_staging_finished_lot(
            db,
            lot_id=source.id,
            expected_version=1,
            quantity=5,
            location_id=target_location.id,
            operator_id=None,
            idempotency_key="p1-25c2-not-staging",
        )


def test_warehouse_page_exposes_compact_staging_transfer_flow() -> None:
    html = Path("static/warehouse.html").read_text(encoding="utf-8")
    assert "转入库位" in html
    assert "stagingTransferQuantity" in html
    assert "stagingTransferFloor" in html
    assert "stagingTransferArea" in html
    assert "stagingTransferLocation" in html
    assert "/location-transfers" in html
    assert "移动位置不会增加或减少库存" in html
    assert "stagingTransferReason" not in html
    assert any(
        route.path == "/lots/{lot_id}/location-transfers"
        and "POST" in (route.methods or set())
        for route in warehouse_router.routes
    )
