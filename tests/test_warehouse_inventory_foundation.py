from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.api import warehouse as warehouse_api
from app.api.deps import get_current_user, get_db
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLocationMovement,
    InventoryLot,
    InventoryMovement,
    InventoryPallet,
    WarehouseLocation,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    edit_finished_lot,
    finished_inventory_candidates_for_product,
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


def seed_product_for_customer(
    db: Session,
    customer: Customer,
    suffix: str,
) -> Product:
    product = Product(
        customer_id=customer.id,
        product_code=f"WH-P-{suffix}",
        customer_material_code=f"WH-M-{suffix}",
        product_name=f"测试成品箱-{suffix}",
        box_category="normal",
        box_style=f"STYLE-{suffix}",
        length_mm=600,
        width_mm=300,
        height_mm=200,
        default_material_code=f"K{suffix}",
        flute_type="B",
    )
    db.add(product)
    db.flush()
    return product


def seed_other_customer_product(
    db: Session,
    suffix: str,
) -> tuple[Customer, Product]:
    customer = Customer(
        customer_number=9100 + len(suffix),
        customer_code=f"WH-C-{suffix}",
        name=f"仓库测试客户-{suffix}",
        payment_term_days=0,
        credit_limit=0,
    )
    db.add(customer)
    db.flush()
    return customer, seed_product_for_customer(db, customer, suffix)


def seed_floor3_location(
    db: Session,
    code: str,
    *,
    warehouse_type: str = "finished",
) -> WarehouseLocation:
    row = WarehouseLocation(
        location_code=code,
        location_name=f"三楼货位-{code}",
        warehouse_type=warehouse_type,
        warehouse_floor=3,
        storage_type="ground",
        source_version="V11",
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
    assert lot.stock_date_accuracy == "exact"
    assert lot.stock_date_original_text == lot.stock_date.isoformat()
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


def test_unknown_technical_date_sorts_after_trustworthy_fifo_dates(
    db: Session,
) -> None:
    customer, product = seed_product(db)
    location = seed_location(db)
    unknown = manual_finished_in(
        db,
        customer_id=customer.id,
        product_id=product.id,
        location_id=location.id,
        quantity=5,
        stock_date=date(2020, 1, 1),
        stock_date_accuracy="unknown",
        stock_date_original_text=None,
        source_type="stocktake",
        remarks=None,
        operator_id=None,
        idempotency_key="unknown-fifo-lot",
    )
    exact = manual_finished_in(
        db,
        customer_id=customer.id,
        product_id=product.id,
        location_id=location.id,
        quantity=5,
        stock_date=date(2026, 7, 1),
        source_type="manual",
        remarks=None,
        operator_id=None,
        idempotency_key="exact-fifo-lot",
    )

    rows = finished_inventory_candidates_for_product(
        db,
        customer_id=customer.id,
        product_id=product.id,
    )

    assert [row.id for row in rows] == [exact.id, unknown.id]
    assert unknown.stock_date_original_text == "未提供"


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


def test_edit_finished_lot_can_rebind_general_inventory_to_customer(
    db: Session,
) -> None:
    lot = finished_lot(db)
    generalized = mutate_lot(
        db,
        lot_id=lot.id,
        operation="transfer_to_general",
        expected_version=1,
        operator_id=None,
        reason="管理员确认通用",
    )
    customer, product = seed_other_customer_product(db, "REBIND")

    edited = edit_finished_lot(
        db,
        lot_id=lot.id,
        expected_version=generalized.version,
        is_general=False,
        customer_id=customer.id,
        product_id=product.id,
        quantity_available=generalized.quantity_available,
        location_id=generalized.warehouse_location_id,
        stock_date=generalized.stock_date,
        operator_id=None,
        idempotency_key="edit-general-to-customer",
    )

    assert edited.version == 3
    assert edited.finished_detail.is_general is False
    assert edited.finished_detail.owner_customer_id == customer.id
    assert edited.finished_detail.owner_customer_name_snapshot == customer.name
    assert edited.finished_detail.product_id == product.id
    assert edited.finished_detail.product_name_snapshot == product.product_name
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == "edit-general-to-customer"
        )
    )
    assert movement is not None
    assert movement.movement_type == "adjust"
    assert movement.quantity == 0
    assert movement.reason == "编辑成品库存批次"
    audit = json.loads(movement.remarks)
    assert audit["action"] == "编辑成品库存批次"
    assert {"is_general", "owner_customer_id", "product_id"} <= set(
        audit["changed_fields"]
    )


def test_edit_finished_lot_rejects_customer_product_mismatch_without_changes(
    db: Session,
) -> None:
    lot = finished_lot(db)
    other_customer, _ = seed_other_customer_product(db, "MISMATCH")
    movement_count = db.scalar(select(func.count(InventoryMovement.id)))

    with pytest.raises(WarehouseInventoryError, match="不属于当前客户"):
        edit_finished_lot(
            db,
            lot_id=lot.id,
            expected_version=1,
            is_general=False,
            customer_id=other_customer.id,
            product_id=lot.finished_detail.product_id,
            quantity_available=5,
            location_id=lot.warehouse_location_id,
            stock_date=date(2026, 7, 1),
            operator_id=None,
            idempotency_key="edit-mismatched-product",
        )

    db.refresh(lot)
    assert lot.version == 1
    assert lot.quantity_available == 20
    assert lot.finished_detail.owner_customer_id != other_customer.id
    assert db.scalar(select(func.count(InventoryMovement.id))) == movement_count


@pytest.mark.parametrize("balance_field", ["quantity_reserved", "quantity_consumed"])
@pytest.mark.parametrize("identity_change", ["product", "customer", "general"])
def test_edit_finished_lot_blocks_identity_changes_after_reservation_or_consumption(
    db: Session,
    balance_field: str,
    identity_change: str,
) -> None:
    lot = finished_lot(db)
    detail = lot.finished_detail
    original_identity = (
        detail.product_id,
        detail.owner_customer_id,
        detail.is_general,
    )
    setattr(lot, balance_field, 3)
    target_is_general = False
    target_customer_id = detail.owner_customer_id
    target_product_id = detail.product_id
    if identity_change == "product":
        target_product_id = seed_product_for_customer(
            db, detail.customer, f"LOCK-{balance_field}"
        ).id
    elif identity_change == "customer":
        target_customer, target_product = seed_other_customer_product(
            db, f"LOCK-{balance_field}"
        )
        target_customer_id = target_customer.id
        target_product_id = target_product.id
    else:
        target_is_general = True
        target_customer_id = None
    db.flush()
    movement_count = db.scalar(select(func.count(InventoryMovement.id)))

    with pytest.raises(
        WarehouseInventoryError,
        match="已有预占或消耗，不能修改产品、客户或通用归属",
    ) as caught:
        edit_finished_lot(
            db,
            lot_id=lot.id,
            expected_version=1,
            is_general=target_is_general,
            customer_id=target_customer_id,
            product_id=target_product_id,
            quantity_available=5,
            location_id=lot.warehouse_location_id,
            stock_date=date(2026, 7, 6),
            operator_id=None,
            idempotency_key=f"edit-identity-{balance_field}-{identity_change}",
        )

    assert caught.value.status_code == 409
    db.refresh(lot)
    assert lot.version == 1
    assert lot.quantity_available == 20
    assert (
        lot.finished_detail.product_id,
        lot.finished_detail.owner_customer_id,
        lot.finished_detail.is_general,
    ) == original_identity
    assert db.scalar(select(func.count(InventoryMovement.id))) == movement_count


@pytest.mark.parametrize("balance_field", ["quantity_reserved", "quantity_consumed"])
def test_edit_finished_lot_keeps_identity_balances_while_editing_quantity_location_date(
    db: Session,
    balance_field: str,
) -> None:
    lot = finished_lot(db)
    customer = lot.finished_detail.customer
    product = lot.finished_detail.product
    target = WarehouseLocation(
        location_code="FI-EDIT-TARGET",
        location_name="成品编辑目标库位",
        warehouse_type="finished",
    )
    db.add(target)
    db.flush()
    lot.quantity_available = 13
    setattr(lot, balance_field, 7)
    db.flush()
    before_last_movement = lot.last_movement_at

    edited = edit_finished_lot(
        db,
        lot_id=lot.id,
        expected_version=1,
        is_general=False,
        customer_id=customer.id,
        product_id=product.id,
        quantity_available=0,
        location_id=target.id,
        stock_date=date(2026, 6, 30),
        operator_id=None,
        idempotency_key=f"edit-quantity-location-date-{balance_field}",
    )

    assert edited.quantity_available == 0
    assert getattr(edited, balance_field) == 7
    assert edited.warehouse_location_id == target.id
    assert edited.stock_date == date(2026, 6, 30)
    assert edited.stock_date_accuracy == "exact"
    assert edited.stock_date_original_text == "2026-06-30"
    assert edited.last_movement_at > before_last_movement
    assert inventory_age_warning(edited, today=date(2026, 7, 16)).days == 16
    assert edited.version == 2
    assert edited.finished_detail.product_id == product.id
    assert edited.finished_detail.inventory_code_snapshot == product.product_code
    assert edited.finished_detail.product_name_snapshot == product.product_name
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key
            == f"edit-quantity-location-date-{balance_field}"
        )
    )
    assert movement is not None
    assert movement.quantity == 13
    assert (movement.before_available, movement.after_available) == (13, 0)
    if balance_field == "quantity_reserved":
        assert (movement.before_reserved, movement.after_reserved) == (7, 7)
    else:
        assert (movement.before_consumed, movement.after_consumed) == (7, 7)


def test_edit_finished_lot_preserves_unknown_accuracy_when_date_is_unchanged(
    db: Session,
) -> None:
    lot = finished_lot(db)
    lot.stock_date_accuracy = "unknown"
    lot.stock_date_original_text = None
    db.flush()

    edited = edit_finished_lot(
        db,
        lot_id=lot.id,
        expected_version=lot.version,
        is_general=False,
        customer_id=lot.finished_detail.owner_customer_id,
        product_id=lot.finished_detail.product_id,
        quantity_available=19,
        location_id=lot.warehouse_location_id,
        stock_date=lot.stock_date,
        operator_id=None,
        idempotency_key="edit-preserve-unknown-date",
    )

    assert edited.stock_date_accuracy == "unknown"
    assert edited.stock_date_original_text is None
    assert inventory_age_warning(edited).days is None


def test_edit_finished_lot_can_explicitly_confirm_an_unchanged_technical_date(
    db: Session,
) -> None:
    lot = finished_lot(db)
    lot.stock_date_accuracy = "unknown"
    lot.stock_date_original_text = None
    db.flush()

    edited = edit_finished_lot(
        db,
        lot_id=lot.id,
        expected_version=lot.version,
        is_general=False,
        customer_id=lot.finished_detail.owner_customer_id,
        product_id=lot.finished_detail.product_id,
        quantity_available=lot.quantity_available,
        location_id=lot.warehouse_location_id,
        stock_date=lot.stock_date,
        operator_id=None,
        idempotency_key="edit-confirm-technical-date",
        confirm_stock_date_exact=True,
    )

    assert edited.stock_date_accuracy == "exact"
    assert edited.stock_date_original_text == edited.stock_date.isoformat()
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == "edit-confirm-technical-date"
        )
    )
    assert movement is not None
    assert "stock_date_accuracy" in movement.remarks


def test_edit_finished_lot_moves_bound_floor3_pallet_and_projection(
    db: Session,
) -> None:
    customer, product = seed_product(db)
    source = seed_floor3_location(db, "F3-EDIT-A")
    target = seed_floor3_location(db, "F3-EDIT-B")
    lot = manual_finished_in(
        db,
        customer_id=customer.id,
        product_id=product.id,
        location_id=source.id,
        quantity=20,
        stock_date=date(2026, 7, 1),
        source_type="manual",
        remarks=None,
        operator_id=None,
        idempotency_key="floor3-edit-in",
    )
    pallet_id = lot.pallet_item.pallet_id
    pallet_version = lot.pallet_item.pallet.version

    edited = edit_finished_lot(
        db,
        lot_id=lot.id,
        expected_version=1,
        is_general=False,
        customer_id=customer.id,
        product_id=product.id,
        quantity_available=15,
        location_id=target.id,
        stock_date=date(2026, 7, 2),
        operator_id=None,
        idempotency_key="floor3-edit-move",
    )

    pallet = db.get(InventoryPallet, pallet_id)
    assert edited.warehouse_location_id == target.id
    assert edited.version == 2
    assert pallet.location_id == target.id
    assert pallet.version == pallet_version + 1
    assert lot.pallet_item.quantity == 15
    location_movement = db.scalar(
        select(InventoryLocationMovement).where(
            InventoryLocationMovement.pallet_id == pallet_id,
            InventoryLocationMovement.movement_type == "move",
        )
    )
    assert location_movement is not None
    assert location_movement.to_location_id == target.id
    assert location_movement.remarks == "编辑成品库存批次"
    from app.services.asset_time_archive import build_inventory_lot_detail_timeline

    timeline = build_inventory_lot_detail_timeline(db, edited)
    matching_location_events = [
        event
        for event in timeline
        if event.get("from_location") == source.location_code
        and event.get("to_location") == target.location_code
    ]
    assert [event["event_type"] for event in matching_location_events] == [
        "pallet_location_move"
    ]
    adjust_event = next(
        event for event in timeline if event["event_type"] == "inventory_adjust"
    )
    assert (adjust_event["before_available"], adjust_event["after_available"]) == (
        20,
        15,
    )


def test_edit_finished_lot_rejects_occupied_floor3_target_and_rolls_back(
    db: Session,
) -> None:
    customer, product = seed_product(db)
    source = seed_floor3_location(db, "F3-OCCUPIED-A")
    target = seed_floor3_location(db, "F3-OCCUPIED-B")
    first = manual_finished_in(
        db,
        customer_id=customer.id,
        product_id=product.id,
        location_id=source.id,
        quantity=20,
        stock_date=date(2026, 7, 1),
        source_type="manual",
        remarks=None,
        operator_id=None,
        idempotency_key="floor3-first-in",
    )
    other_customer, other_product = seed_other_customer_product(db, "OCCUPIER")
    manual_finished_in(
        db,
        customer_id=other_customer.id,
        product_id=other_product.id,
        location_id=target.id,
        quantity=8,
        stock_date=date(2026, 7, 1),
        source_type="manual",
        remarks=None,
        operator_id=None,
        idempotency_key="floor3-occupier-in",
    )
    source_pallet_id = first.pallet_item.pallet_id

    with pytest.raises(WarehouseInventoryError, match="其它真实栈板占用"):
        edit_finished_lot(
            db,
            lot_id=first.id,
            expected_version=1,
            is_general=False,
            customer_id=customer.id,
            product_id=product.id,
            quantity_available=3,
            location_id=target.id,
            stock_date=date(2026, 7, 3),
            operator_id=None,
            idempotency_key="floor3-edit-occupied",
        )

    db.refresh(first)
    assert first.warehouse_location_id == source.id
    assert first.quantity_available == 20
    assert first.version == 1
    assert db.get(InventoryPallet, source_pallet_id).location_id == source.id
    assert db.scalar(
        select(func.count(InventoryMovement.id)).where(
            InventoryMovement.idempotency_key == "floor3-edit-occupied"
        )
    ) == 0


def test_edit_finished_lot_is_idempotent_and_rejects_version_conflicts(
    db: Session,
) -> None:
    lot = finished_lot(db)
    detail = lot.finished_detail
    payload = {
        "lot_id": lot.id,
        "expected_version": 1,
        "is_general": False,
        "customer_id": detail.owner_customer_id,
        "product_id": detail.product_id,
        "quantity_available": 12,
        "location_id": lot.warehouse_location_id,
        "stock_date": date(2026, 7, 4),
        "operator_id": None,
        "idempotency_key": "edit-idempotent",
    }

    first = edit_finished_lot(db, **payload)
    replay = edit_finished_lot(db, **payload)
    assert replay.id == first.id
    assert replay.version == 2
    assert replay.quantity_available == 12
    assert db.scalar(
        select(func.count(InventoryMovement.id)).where(
            InventoryMovement.idempotency_key == "edit-idempotent"
        )
    ) == 1

    with pytest.raises(WarehouseInventoryError, match="幂等键"):
        edit_finished_lot(db, **{**payload, "quantity_available": 11})
    with pytest.raises(WarehouseInventoryError, match="刷新"):
        edit_finished_lot(
            db,
            **{
                **payload,
                "idempotency_key": "edit-stale-version",
                "quantity_available": 10,
            },
        )
    db.refresh(lot)
    assert lot.quantity_available == 12
    assert lot.version == 2


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
    assert lot.stock_date_accuracy == "exact"
    assert lot.stock_date_original_text == lot.stock_date.isoformat()
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


def test_explicitly_unplaced_location_cannot_receive_inventory(db: Session) -> None:
    customer, product = seed_product(db)
    location = seed_location(db, "finished")
    location.placement_status = "unplaced"
    db.flush()

    with pytest.raises(WarehouseInventoryError) as error:
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
            idempotency_key="n081-unplaced-manual-in",
        )

    assert error.value.status_code == 409
    assert "尚未完成空间放置" in str(error.value)
    assert db.scalar(
        select(func.count(InventoryLot.id)).where(
            InventoryLot.warehouse_location_id == location.id
        )
    ) == 0


@pytest.mark.parametrize(
    ("days", "level"),
    [(100, None), (365, "attention"), (548, "handling"), (730, "cleanup")],
)
def test_inventory_age_warning_thresholds(db: Session, days: int, level: str | None) -> None:
    lot = finished_lot(db)
    lot.stock_date = date.today() - timedelta(days=days)
    lot.last_movement_at = datetime.now()
    warning = inventory_age_warning(lot)
    assert warning.days == days
    assert warning.level == level


def test_edit_finished_lot_api_is_admin_only(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "warehouse-edit-api.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer, product = seed_product(db)
        location = seed_location(db)
        admin = User(
            username="warehouse-edit-admin",
            password_hash="test",
            role="admin",
            real_name="库存管理员",
            must_change_password=False,
        )
        workshop = User(
            username="warehouse-edit-workshop",
            password_hash="test",
            role="workshop",
            real_name="车间用户",
            must_change_password=False,
        )
        db.add_all([admin, workshop])
        db.flush()
        lot = manual_finished_in(
            db,
            customer_id=customer.id,
            product_id=product.id,
            location_id=location.id,
            quantity=20,
            stock_date=date(2026, 7, 1),
            source_type="manual",
            remarks=None,
            operator_id=admin.id,
            idempotency_key="edit-api-manual-in",
        )
        lot.stock_date = date.today() - timedelta(days=1000)
        lot.stock_date_accuracy = "unknown"
        lot.stock_date_original_text = None
        db.commit()
        ids = {
            "admin": admin.id,
            "workshop": workshop.id,
            "customer": customer.id,
            "product": product.id,
            "location": location.id,
            "lot": lot.id,
        }

    app = FastAPI()
    app.include_router(warehouse_api.router, prefix="/api/warehouse")
    current_user_id = {"value": ids["workshop"]}

    def override_get_db():
        with factory() as db:
            yield db

    def override_get_current_user():
        with factory() as db:
            yield db.get(User, current_user_id["value"])

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    edited_stock_date = date.today() - timedelta(days=731)
    payload = {
        "expected_version": 1,
        "is_general": False,
        "customer_id": ids["customer"],
        "product_id": ids["product"],
        "quantity_available": 9,
        "location_id": ids["location"],
        "stock_date": edited_stock_date.isoformat(),
        "idempotency_key": "edit-api-admin-only",
    }

    with TestClient(app) as client:
        unknown_before = client.get(
            "/api/warehouse/lots",
            params={"stale_level": "unknown"},
        )
        cleanup_before = client.get(
            "/api/warehouse/lots",
            params={"stale_level": "cleanup"},
        )
        forbidden = client.post(
            f"/api/warehouse/lots/{ids['lot']}/edit-finished",
            json=payload,
        )
        assert forbidden.status_code == 403
        current_user_id["value"] = ids["admin"]
        allowed = client.post(
            f"/api/warehouse/lots/{ids['lot']}/edit-finished",
            json=payload,
        )
        cleanup = client.get(
            "/api/warehouse/lots",
            params={"stale_level": "cleanup"},
        )

    assert allowed.status_code == 200, allowed.text
    assert unknown_before.status_code == 200, unknown_before.text
    assert ids["lot"] in {row["id"] for row in unknown_before.json()["items"]}
    assert cleanup_before.status_code == 200, cleanup_before.text
    assert ids["lot"] not in {
        row["id"] for row in cleanup_before.json()["items"]
    }
    assert allowed.json()["quantity_available"] == 9
    assert allowed.json()["version"] == 2
    assert allowed.json()["age_days"] == 731
    assert allowed.json()["stock_date_accuracy"] == "exact"
    assert allowed.json()["stock_date_original_text"] == edited_stock_date.isoformat()
    assert cleanup.status_code == 200, cleanup.text
    assert ids["lot"] in {row["id"] for row in cleanup.json()["items"]}


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
