"""Focused transactional tests, only against copies of an explicitly supplied UAT source."""
from datetime import date
from decimal import Decimal
import os
from pathlib import Path
import shutil
import hashlib
import sqlite3

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.bom_subkit import OrderSubkit, ProductSubkit, SubkitConversion, SubkitConversionInput
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.services.bom_subkits import save_subkit, freeze_order_subkit, SubkitError
from app.services.bom_subkit_inventory import assemble_subkit_inventory, reverse_subkit_conversion
from app.services.composite_bom import create_order_item_bom_snapshots
from app.services.warehouse_inventory import manual_finished_in


@pytest.fixture
def db(tmp_path, monkeypatch):
    source = os.environ.get("ERP_SUBKIT_UAT_SOURCE")
    if not source:
        pytest.skip("requires an explicitly prepared isolated factory database copy")
    path = Path(source).resolve()
    if path.name != "source-isolated.sqlite3" or "tm-uat" not in path.parts:
        pytest.fail("not the prepared isolated database")
    target = tmp_path / "subkit-test.sqlite3"
    before = hashlib.sha256(path.read_bytes()).digest()
    shutil.copy2(path, target)
    backup = tmp_path / "subkit-before.sqlite3"
    shutil.copy2(target, backup)
    assert hashlib.sha256(backup.read_bytes()).digest() == before
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    from alembic import command
    from tests.test_p1_131_material_cost_lineage_migration import _config
    command.upgrade(_config(monkeypatch, target), "head")
    engine = create_sqlite_engine(target)
    with Session(engine) as session:
        yield session
        session.rollback()
    engine.dispose()
    assert hashlib.sha256(path.read_bytes()).digest() == before


def setup_order(db):
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    result = save_subkit(db, parent_product_id=3765, name="000148内衬",
        kits_per_parent=1, members=[{"product_id": 3788, "pieces_per_kit": 2},
                                  {"product_id": 3789, "pieces_per_kit": 6}],
        expected_version=0, actor=actor)
    order = Order(order_number="TEST-SUBKIT-ONLY", customer_id=136, order_date=date.today())
    db.add(order)
    db.flush()
    item = OrderItem(order_id=order.id, product_id=3765, quantity=100,
        unit_price=Decimal("5"), subtotal=Decimal("500"), snapshot_product_name="000148内盒",
        composite_fulfillment_mode_snapshot="parent_delivery")
    db.add(item)
    db.flush()
    from app.models.product import Product
    create_order_item_bom_snapshots(db, order_item=item, parent_product=db.get(Product, 3765))
    freeze_order_subkit(db, order_item_id=item.id, actor_id=actor.id)
    db.commit()
    return actor, item, result


def raw(db, actor, pid, quantity):
    lot = manual_finished_in(db, customer_id=136, product_id=pid,
        location_id=1890, quantity=quantity, stock_date=date.today(), source_type="production_completion",
        remarks="isolated fixture", operator_id=actor.id, idempotency_key=f"test-raw:{pid}")
    lot.estimated_unit_cost_snapshot = Decimal("0.1250")
    db.commit()
    return lot


def convert(db, actor, item, lots, key="test-assembly"):
    return assemble_subkit_inventory(db, order_item_id=item.id,
        source_lot_versions={lot.id: lot.version for lot in lots}, target_location_id=1890,
        operation_key=key, operator_id=actor.id)


def test_recipe_save_reload_and_snapshot_are_independent(db):
    actor, item, result = setup_order(db)
    frozen = db.get(OrderSubkit, item.id)
    assert frozen.kit_name_snapshot == "000148内衬"
    save_subkit(db, parent_product_id=3765, name="新版内衬",
        kits_per_parent=1, members=[{"product_id": 3788, "pieces_per_kit": 2},
                                  {"product_id": 3789, "pieces_per_kit": 6}],
        expected_version=result["version"], actor=actor)
    assert freeze_order_subkit(db, order_item_id=item.id).kit_name_snapshot == "000148内衬"
    with pytest.raises(SubkitError, match="已变化"):
        save_subkit(db, parent_product_id=3765, name="旧窗口", kits_per_parent=1,
            members=[], expected_version=0, actor=actor)


def test_conversion_debits_components_and_credits_kits_once_with_cost(db):
    actor, item, definition = setup_order(db)
    lots = [raw(db, actor, 3788, 220), raw(db, actor, 3789, 660)]
    versions = {lot.id: lot.version for lot in lots}
    conversion = convert(db, actor, item, lots)
    db.commit()
    assert conversion.quantity == 100
    output = db.get(InventoryLot, conversion.output_lot_id)
    assert output.quantity_available == 100
    assert output.finished_detail.product_id == definition["kit_product_id"]
    assert [lot.quantity_available for lot in lots] == [20, 60]
    assert conversion.total_cost == Decimal("100.0000")
    assert output.estimated_unit_cost_snapshot == Decimal("1.0000")
    assert db.scalar(select(func.count()).select_from(SubkitConversionInput)) == 2
    replay = assemble_subkit_inventory(db, order_item_id=item.id, source_lot_versions=versions,
        target_location_id=1890, operation_key="test-assembly", operator_id=actor.id)
    assert replay.id == conversion.id
    assert db.scalar(select(func.count()).select_from(SubkitConversion)) == 1
    with pytest.raises(SubkitError, match="标识"):
        convert(db, actor, item, lots)


def test_failure_after_component_debit_rolls_back_everything(db, monkeypatch):
    actor, item, _ = setup_order(db)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    from app.services import bom_subkit_inventory as service
    def fail(*a, **k):
        raise RuntimeError("injected output failure")
    monkeypatch.setattr(service, "manual_finished_in", fail)
    with pytest.raises(RuntimeError, match="injected"):
        convert(db, actor, item, lots)
    assert [db.get(InventoryLot, lot.id).quantity_available for lot in lots] == [200, 600]
    assert db.scalar(select(func.count()).select_from(SubkitConversion)) == 0
    assert db.scalar(select(func.count()).select_from(InventoryMovement).where(
        InventoryMovement.idempotency_key.like("test-assembly%"))) == 0


def test_reversal_restores_components_but_does_not_touch_parent_stock(db):
    actor, item, _ = setup_order(db)
    parent_before = db.get(InventoryLot, 600).quantity_available
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    conversion = convert(db, actor, item, lots)
    db.commit()
    reverse_subkit_conversion(db, conversion_id=conversion.id, operator_id=actor.id)
    db.commit()
    assert [lot.quantity_available for lot in lots] == [200, 600]
    assert db.get(InventoryLot, conversion.output_lot_id).quantity_available == 0
    assert db.get(InventoryLot, 600).quantity_available == parent_before
    reverse_subkit_conversion(db, conversion_id=conversion.id, operator_id=actor.id)
    assert [lot.quantity_available for lot in lots] == [200, 600]


def test_used_kit_blocks_reversal_without_partial_restore(db):
    actor, item, _ = setup_order(db)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    conversion = convert(db, actor, item, lots)
    db.commit()
    output = db.get(InventoryLot, conversion.output_lot_id)
    output.quantity_reserved = 1
    output.quantity_available -= 1
    db.commit()
    with pytest.raises(SubkitError, match="不能撤销"):
        reverse_subkit_conversion(db, conversion_id=conversion.id, operator_id=actor.id)
    assert [lot.quantity_available for lot in lots] == [0, 0]


def test_previous_order_components_reserved_first_are_consumed_and_restored(db):
    actor, item, _ = setup_order(db)
    lots = [raw(db, actor, 3788, 220), raw(db, actor, 3789, 660)]
    from app.services.warehouse_inventory import reserve_finished_inventory_for_bom_component
    from app.services.bom_subkits import recipe_rows
    recipe = recipe_rows(db.get(OrderSubkit, item.id))
    reservations = []
    for lot in lots:
        member = next(r for r in recipe if r["product_id"] == lot.finished_detail.product_id)
        reservations.append(reserve_finished_inventory_for_bom_component(db, order_item_id=item.id,
            bom_snapshot_id=member["bom_snapshot_id"], inventory_lot_id=lot.id,
            quantity=member["pieces_per_kit"] * 100, expected_version=lot.version,
            operator_id=actor.id, idempotency_key=f"prior-reserve:{lot.id}", warning_acknowledged_codes=[]))
    db.commit()
    conversion = assemble_subkit_inventory(db, order_item_id=item.id,
        source_lot_versions={lot.id: lot.version for lot in lots}, target_location_id=1890,
        operation_key="reserved-assembly", operator_id=actor.id, available_lot_ids=[])
    db.commit()
    assert conversion.quantity == 100
    assert [r.consumed_stock_quantity for r in reservations] == [200, 600]
    assert [lot.quantity_available for lot in lots] == [20, 60]
    reverse_subkit_conversion(db, conversion_id=conversion.id, operator_id=actor.id)
    db.commit()
    assert [r.consumed_stock_quantity for r in reservations] == [0, 0]
    assert [lot.quantity_reserved for lot in lots] == [200, 600]
    assert [lot.quantity_available for lot in lots] == [20, 60]


def test_exact_cost_slices_conserve_rounded_total():
    from app.services.bom_subkit_costs import cost_slice
    assert sum(cost_slice(Decimal("1"), 6, n, 1) for n in range(6)) == Decimal("1")
    with pytest.raises(SubkitError):
        cost_slice(Decimal("1"), 6, 5, 2)


def test_liner_is_retained_for_original_unfinished_order(db):
    from app.services.bom_subkits import active_subkit_order, require_free_subkit_stock
    actor, item, _ = setup_order(db)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    conversion = convert(db, actor, item, lots)
    db.commit()
    output = db.get(InventoryLot, conversion.output_lot_id)
    assert active_subkit_order(db, output) == item.id
    with pytest.raises(SubkitError, match="保留给原订单"):
        require_free_subkit_stock(db, output)
    assert active_subkit_order(db, lots[0]) is None
    item.is_force_closed = True
    db.flush()
    assert active_subkit_order(db, output) is None


def test_damaged_liner_can_be_replaced_without_exceeding_order(db):
    actor, item, _ = setup_order(db)
    lots = [raw(db, actor, 3788, 220), raw(db, actor, 3789, 660)]
    conversion = convert(db, actor, item, lots)
    db.commit()
    output = db.get(InventoryLot, conversion.output_lot_id)
    from app.services.warehouse_inventory import mutate_lot
    mutate_lot(db, lot_id=output.id, expected_version=output.version, quantity=1,
        operation="damage", operator_id=actor.id, reason="isolated damaged liner", idempotency_key="test-damage")
    db.commit()
    replacement = convert(db, actor, item, lots, key="replacement")
    assert replacement.quantity == 1
    assert [lot.quantity_available for lot in lots] == [18, 54]


def test_split_move_preserves_liner_order_and_available_sets(db):
    from app.models.warehouse_inventory import WarehouseLocation
    from app.services.warehouse_inventory import transfer_finished_lot_between_locations
    from app.services.bom_subkit_delivery import limit_by_subkit_stock
    from app.services.bom_subkits import active_subkit_order
    actor, item, _ = setup_order(db)
    conversion = convert(db, actor, item, [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)])
    db.commit()
    output = db.get(InventoryLot, conversion.output_lot_id)
    source, target = db.get(WarehouseLocation, 1890), db.get(WarehouseLocation, 1891)
    transfer_finished_lot_between_locations(db, lot_id=output.id, expected_version=output.version,
        quantity=40, location_id=target.id, operator_id=actor.id, idempotency_key="subkit-split-move",
        expected_source_layout_version=source.floor3_layout.version if source.floor3_layout else None,
        expected_target_layout_version=target.floor3_layout.version if target.floor3_layout else None)
    db.commit()
    moved = db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type == "subkit_conversion",
        InventoryLot.source_ref_id == conversion.id, InventoryLot.warehouse_location_id == target.id))
    assert moved.quantity_available == 40
    assert active_subkit_order(db, moved) == item.id
    assert limit_by_subkit_stock(db, {item.id:100}) == {item.id:100}


def test_outer_rollback_undoes_conversion_after_read_only_entry(db):
    actor, item, _ = setup_order(db)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    lot_ids = [lot.id for lot in lots]
    result = convert(db, actor, item, lots, key="outer-rollback-assembly")
    conversion_id, output_id = result.id, result.output_lot_id
    db.rollback()
    assert db.get(SubkitConversion, conversion_id) is None
    assert db.get(InventoryLot, output_id) is None
    assert [db.get(InventoryLot, lid).quantity_available for lid in lot_ids] == [200, 600]


def test_outer_rollback_undoes_reversal_after_read_only_entry(db):
    actor, item, _ = setup_order(db)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    result = convert(db, actor, item, lots, key="outer-rollback-reversal")
    db.commit()
    conversion_id, output_id, actor_id = result.id, result.output_lot_id, actor.id
    reverse_subkit_conversion(db, conversion_id=conversion_id, operator_id=actor_id)
    db.rollback()
    assert db.get(SubkitConversion, conversion_id).status == "posted"
    assert db.get(InventoryLot, output_id).quantity_available == 100
