from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.product import Product
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryMovement,
    WarehouseLocation,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    inventory_age_warning,
    manual_finished_in,
    manual_semi_finished_in,
    mutate_lot,
)


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "warehouse.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


def seed_product(db: Session) -> tuple[Customer, Product]:
    customer = Customer(
        customer_number=9001,
        customer_code="WH-TEST",
        name="仓库测试客户",
        payment_term_days=0,
        credit_limit=0,
    )
    db.add(customer)
    db.flush()
    product = Product(
        customer_id=customer.id,
        product_code="WH-P001",
        customer_material_code="WH-M001",
        product_name="测试成品箱",
        box_category="normal",
        box_style="A1",
        length_mm=800,
        width_mm=200,
        height_mm=100,
        default_material_code="A416D",
        flute_type="BE",
    )
    db.add(product)
    db.flush()
    return customer, product


def seed_location(db: Session, warehouse_type: str = "finished") -> WarehouseLocation:
    row = WarehouseLocation(
        location_code=f"{warehouse_type[:2].upper()}-01",
        location_name="测试库位",
        warehouse_type=warehouse_type,
    )
    db.add(row)
    db.flush()
    return row


def finished_lot(db: Session, key: str = "fg-in-1") -> InventoryLot:
    customer, product = seed_product(db)
    location = seed_location(db)
    return manual_finished_in(
        db,
        customer_id=customer.id,
        product_id=product.id,
        location_id=location.id,
        quantity=20,
        stock_date=date.today(),
        source_type="manual",
        remarks="test",
        operator_id=None,
        idempotency_key=key,
    )


def test_finished_manual_in_snapshots_product_and_writes_movement(db: Session) -> None:
    lot = finished_lot(db)
    db.flush()
    assert lot.quantity_available == 20
    assert lot.unit == "boxes"
    assert lot.finished_detail.inventory_code_snapshot == "WH-P001"
    assert lot.finished_detail.material_code_snapshot == "A416D"
    assert lot.finished_detail.flute_type_snapshot == "BE"
    movement = db.scalar(select(InventoryMovement))
    assert movement is not None
    assert movement.movement_type == "manual_in"
    assert (movement.before_available, movement.after_available) == (0, 20)
    lot.finished_detail.product.product_name = "后来修改的名称"
    db.flush()
    assert lot.finished_detail.product_name_snapshot == "测试成品箱"


def test_manual_in_is_idempotent(db: Session) -> None:
    first = finished_lot(db, "same-key")
    second = manual_finished_in(
        db,
        customer_id=first.finished_detail.owner_customer_id,
        product_id=first.finished_detail.product_id,
        location_id=first.warehouse_location_id,
        quantity=99,
        stock_date=date.today(),
        source_type="manual",
        remarks=None,
        operator_id=None,
        idempotency_key="same-key",
    )
    assert first.id == second.id
    assert db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == "same-key")).after_available == 20


def test_freeze_blocks_quantity_changes_then_unfreeze_allows_adjustment(db: Session) -> None:
    lot = finished_lot(db)
    frozen = mutate_lot(
        db,
        lot_id=lot.id,
        operation="freeze",
        expected_version=1,
        operator_id=None,
        idempotency_key="freeze-1",
    )
    assert frozen.status == "frozen"
    with pytest.raises(WarehouseInventoryError, match="冻结库存"):
        mutate_lot(
            db,
            lot_id=lot.id,
            operation="adjust",
            expected_version=2,
            operator_id=None,
            quantity=1,
        )
    active = mutate_lot(
        db,
        lot_id=lot.id,
        operation="unfreeze",
        expected_version=2,
        operator_id=None,
        idempotency_key="unfreeze-1",
    )
    adjusted = mutate_lot(
        db,
        lot_id=lot.id,
        operation="adjust",
        expected_version=active.version,
        operator_id=None,
        quantity=-3,
        reason="盘点减少",
        idempotency_key="adjust-1",
    )
    assert adjusted.quantity_available == 17


def test_damage_scrap_and_version_conflict_are_guarded(db: Session) -> None:
    lot = finished_lot(db)
    damaged = mutate_lot(
        db,
        lot_id=lot.id,
        operation="damage",
        expected_version=1,
        operator_id=None,
        quantity=4,
        reason="受潮",
    )
    assert (damaged.quantity_available, damaged.quantity_damaged) == (16, 4)
    scrapped = mutate_lot(
        db,
        lot_id=lot.id,
        operation="scrap",
        expected_version=2,
        operator_id=None,
        quantity=3,
        reason="无法使用",
    )
    assert (scrapped.quantity_available, scrapped.quantity_scrapped) == (13, 3)
    with pytest.raises(WarehouseInventoryError, match="刷新"):
        mutate_lot(
            db,
            lot_id=lot.id,
            operation="adjust",
            expected_version=1,
            operator_id=None,
            quantity=1,
        )


def test_finished_inventory_can_be_manually_transferred_to_general(db: Session) -> None:
    lot = finished_lot(db)
    result = mutate_lot(
        db,
        lot_id=lot.id,
        operation="transfer_to_general",
        expected_version=1,
        operator_id=None,
        reason="管理员确认通用",
    )
    assert result.finished_detail.is_general is True
    assert result.finished_detail.owner_customer_id is None
    assert result.finished_detail.owner_customer_name_snapshot == "仓库测试客户"


def test_semi_finished_records_physical_sheets_without_rotation_or_yield(db: Session) -> None:
    location = seed_location(db, "semi_finished")
    lot = manual_semi_finished_in(
        db,
        location_id=location.id,
        quantity=2,
        stock_date=date.today(),
        source_type="purchase_surplus",
        material_code="A416D",
        layer_count=5,
        flute_type="AB",
        board_length_mm=800,
        board_width_mm=600,
        sheet_type="raw_board",
        supplier_name="测试供应商",
        customer_id=None,
        crease_type=None,
        crease_left_mm=None,
        crease_middle_mm=None,
        crease_right_mm=None,
        cutting_note="宽向一开三，仅记录",
        remarks=None,
        operator_id=None,
        idempotency_key="semi-1",
    )
    assert lot.quantity_available == 2
    assert lot.unit == "sheets"
    assert lot.semi_finished_detail.board_length_mm == 800
    assert lot.semi_finished_detail.board_width_mm == 600
    assert lot.semi_finished_detail.cutting_note == "宽向一开三，仅记录"
    with pytest.raises(WarehouseInventoryError, match="三层仅支持"):
        manual_semi_finished_in(
            db,
            location_id=location.id,
            quantity=1,
            stock_date=date.today(),
            source_type="manual",
            material_code="C6C",
            layer_count=3,
            flute_type="AB",
            board_length_mm=600,
            board_width_mm=300,
            sheet_type="net_sheet",
            supplier_name=None,
            customer_id=None,
            crease_type=None,
            crease_left_mm=None,
            crease_middle_mm=None,
            crease_right_mm=None,
            cutting_note=None,
            remarks=None,
            operator_id=None,
            idempotency_key=None,
        )


def test_disabled_or_wrong_type_location_cannot_receive(db: Session) -> None:
    customer, product = seed_product(db)
    location = seed_location(db, "semi_finished")
    with pytest.raises(WarehouseInventoryError, match="类型"):
        manual_finished_in(
            db,
            customer_id=customer.id,
            product_id=product.id,
            location_id=location.id,
            quantity=1,
            stock_date=date.today(),
            source_type="manual",
            remarks=None,
            operator_id=None,
            idempotency_key=None,
        )
    location.warehouse_type = "finished"
    location.is_active = False
    with pytest.raises(WarehouseInventoryError, match="停用"):
        manual_finished_in(
            db,
            customer_id=customer.id,
            product_id=product.id,
            location_id=location.id,
            quantity=1,
            stock_date=date.today(),
            source_type="manual",
            remarks=None,
            operator_id=None,
            idempotency_key=None,
        )


@pytest.mark.parametrize(
    ("days", "level"),
    [(100, None), (365, "attention"), (548, "handling"), (730, "cleanup")],
)
def test_inventory_age_warning_thresholds(db: Session, days: int, level: str | None) -> None:
    lot = finished_lot(db)
    lot.last_movement_at = datetime.now() - timedelta(days=days)
    assert inventory_age_warning(lot).level == level


def test_schema_and_frontend_expose_foundation_without_deduction_endpoints(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "schema.sqlite3")
    Base.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())
    assert {
        "warehouse_locations",
        "inventory_lots",
        "finished_goods_inventory_details",
        "semi_finished_inventory_details",
        "inventory_reservations",
        "inventory_movements",
    } <= tables
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "warehouse.html").read_text(encoding="utf-8")
    api_source = (root / "app" / "api" / "warehouse.py").read_text(encoding="utf-8")
    index_html = (root / "static" / "index.html").read_text(encoding="utf-8")
    assert "按实际物理张/片记录" in html
    assert "本阶段不做一开几换算" in html
    assert "转为半成品库存" not in api_source
    assert '@router.post("/finished/reservations")' in api_source
    assert '@router.post("/semi-finished/reservations")' not in api_source
    assert "/semi-finished/candidates" not in api_source
    assert "仓库库存管理" in index_html
