from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.bom_subkit import ProductSubkit
from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot
from app.services.bom_subkit_inventory import assemble_subkit_inventory, reverse_subkit_conversion
from app.services.bom_subkits import save_subkit, SubkitError
from app.services.multilevel_bom_orders import freeze_master_order_bom
from tests.test_bom_subkit_inventory import db, setup_order, raw
from tests.test_multilevel_bom_master import save


def setup_graph(db, *, root_assembly=False, quantity=100):
    actor, old_item, definition = setup_order(db)
    kid = definition["kit_product_id"]
    save_subkit(db, parent_product_id=3765, name="000148内衬", kits_per_parent=1,
        members=[{"product_id": 3788, "pieces_per_kit": 2}, {"product_id": 3789, "pieces_per_kit": 6}],
        expected_version=definition["version"], actor=actor, enabled=False)
    save(db, actor, kid, "assembled", [(3788, 2, "assembly"), (3789, 6, "assembly")])
    save(db, actor, 3765, "assembled" if root_assembly else "manufactured",
         [(kid, 1, "assembly" if root_assembly else "accompany")])
    order = Order(order_number="TEST-REAL-GRAPH", customer_id=136, order_date=date.today())
    db.add(order)
    db.flush()
    item = OrderItem(order_id=order.id, product_id=3765, quantity=quantity,
        unit_price=Decimal("5"), subtotal=Decimal(5 * quantity), snapshot_product_name="测试真实父件",
        composite_fulfillment_mode_snapshot="parent_delivery")
    db.add(item)
    db.flush()
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    return actor, item, kid


def assemble(db, actor, item, pid, lots, key="graph:assembly", **kwargs):
    return assemble_subkit_inventory(db, order_item_id=item.id, graph_product_id=pid,
        source_lot_versions={lot.id: lot.version for lot in lots}, target_location_id=1890,
        operation_key=key, operator_id=actor.id, **kwargs)


def test_real_node_debits_components_credits_set_and_retains_extra(db):
    actor, item, kid = setup_graph(db)
    lots = [raw(db, actor, 3788, 220), raw(db, actor, 3789, 660)]
    versions = {lot.id: lot.version for lot in lots}
    result = assemble(db, actor, item, kid, lots)
    db.commit()
    assert isinstance(result, BomAssembly)
    assert result.output_product_id == kid
    assert result.quantity == 100
    assert result.total_cost == Decimal("100")
    output = db.get(InventoryLot, result.output_lot_id)
    assert output.finished_detail.product_id == kid
    assert output.quantity_available == 100
    assert [lot.quantity_available for lot in lots] == [20, 60]
    again = assemble_subkit_inventory(db, order_item_id=item.id, graph_product_id=kid,
        source_lot_versions=versions, target_location_id=1890, operation_key="graph:assembly", operator_id=actor.id)
    assert again.id == result.id
    assert db.scalar(select(func.count()).select_from(BomAssembly)) == 1
    assert db.scalar(select(func.count()).select_from(BomAssemblyInput)) == 2


def test_assembled_parent_and_intermediate_use_same_inventory_and_exact_cost(db):
    actor, item, kid = setup_graph(db, root_assembly=True)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    inner = assemble(db, actor, item, kid, lots, key="graph:inner")
    output = db.get(InventoryLot, inner.output_lot_id)
    outer = assemble(db, actor, item, 3765, [output], key="graph:outer")
    db.commit()
    assert outer.quantity == 100
    assert outer.total_cost == inner.total_cost == Decimal("100")
    assert output.quantity_available == 0
    assert db.get(InventoryLot, outer.output_lot_id).finished_detail.product_id == 3765


def test_accompany_does_not_consume_real_carton_and_bad_node_rejected(db):
    actor, item, kid = setup_graph(db)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    with pytest.raises(SubkitError, match="只能组装"):
        assemble(db, actor, item, 3765, lots)
    assert [lot.quantity_available for lot in lots] == [200, 600]


def test_generic_failure_after_debits_rolls_back_and_outer_cancel_also_rolls_back(db, monkeypatch):
    actor, item, kid = setup_graph(db)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    from app.services import bom_subkit_inventory as service
    original = service.manual_finished_in
    def fail(*args, **kwargs):
        raise RuntimeError("injected generic output failure")
    monkeypatch.setattr(service, "manual_finished_in", fail)
    with pytest.raises(RuntimeError, match="injected"):
        assemble(db, actor, item, kid, lots)
    assert [db.get(InventoryLot, lot.id).quantity_available for lot in lots] == [200, 600]
    assert db.scalar(select(func.count()).select_from(BomAssembly)) == 0
    monkeypatch.setattr(service, "manual_finished_in", original)
    assemble(db, actor, item, kid, lots)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(BomAssembly)) == 0
    assert [db.get(InventoryLot, lot.id).quantity_available for lot in lots] == [200, 600]


def test_reverse_outer_then_inner_restores_original_raw_balances(db):
    actor, item, kid = setup_graph(db, root_assembly=True)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    inner = assemble(db, actor, item, kid, lots, key="graph:inner")
    output = db.get(InventoryLot, inner.output_lot_id)
    outer = assemble(db, actor, item, 3765, [output], key="graph:outer")
    db.commit()
    with pytest.raises(SubkitError, match="不能撤销"):
        reverse_subkit_conversion(db, conversion_id=inner.id, operator_id=actor.id, graph_assembly=True)
    reverse_subkit_conversion(db, conversion_id=outer.id, operator_id=actor.id, graph_assembly=True)
    reverse_subkit_conversion(db, conversion_id=inner.id, operator_id=actor.id, graph_assembly=True)
    db.commit()
    assert [lot.quantity_available for lot in lots] == [200, 600]
    assert inner.status == outer.status == "reversed"


def test_own_component_reservations_consumed_and_restored_but_free_remainder_kept(db):
    actor, item, kid = setup_graph(db)
    lots = [raw(db, actor, 3788, 220), raw(db, actor, 3789, 660)]
    from app.services.warehouse_inventory import reserve_finished_inventory_for_bom_component
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    snapshots = {r.component_product_id: r.id for r in read_compiled_order_bom(db, item.id).snapshots}
    reservations = []
    for lot, qty in zip(lots, (200, 600)):
        reservations.append(reserve_finished_inventory_for_bom_component(db, order_item_id=item.id,
            bom_snapshot_id=snapshots[lot.finished_detail.product_id], inventory_lot_id=lot.id,
            quantity=qty, expected_version=lot.version, operator_id=actor.id,
            idempotency_key=f"graph:reserve:{lot.id}", warning_acknowledged_codes=[]))
    db.commit()
    result = assemble(db, actor, item, kid, lots, available_lot_ids=[])
    assert [r.consumed_stock_quantity for r in reservations] == [200, 600]
    assert [lot.quantity_available for lot in lots] == [20, 60]
    reverse_subkit_conversion(db, conversion_id=result.id, operator_id=actor.id, graph_assembly=True)
    db.commit()
    assert [r.consumed_stock_quantity for r in reservations] == [0, 0]
    assert [lot.quantity_reserved for lot in lots] == [200, 600]
    assert [lot.quantity_available for lot in lots] == [20, 60]


def test_output_owner_guard_and_zero_quantity_after_demand_filled(db):
    actor, item, kid = setup_graph(db)
    lots = [raw(db, actor, 3788, 220), raw(db, actor, 3789, 660)]
    result = assemble(db, actor, item, kid, lots)
    db.commit()
    from app.services.bom_subkits import active_subkit_order, require_free_subkit_stock
    output = db.get(InventoryLot, result.output_lot_id)
    assert active_subkit_order(db, output) == item.id
    with pytest.raises(SubkitError, match="保留给原订单"):
        require_free_subkit_stock(db, output)
    again = assemble(db, actor, item, kid, lots, key="graph:next")
    assert again.quantity == 0
    assert [lot.quantity_available for lot in lots] == [20, 60]


def test_equal_output_balance_after_untracked_change_does_not_allow_reverse(db):
    actor, item, kid = setup_graph(db)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    result = assemble(db, actor, item, kid, lots)
    output = db.get(InventoryLot, result.output_lot_id)
    output.version += 1
    db.commit()
    with pytest.raises(SubkitError, match="不能撤销"):
        reverse_subkit_conversion(db, conversion_id=result.id, operator_id=actor.id, graph_assembly=True)
    assert [lot.quantity_available for lot in lots] == [0, 0]


def test_another_order_cannot_consume_reserved_assembly_output(db):
    actor, item, kid = setup_graph(db, root_assembly=True)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    result = assemble(db, actor, item, kid, lots)
    db.commit()
    output = db.get(InventoryLot, result.output_lot_id)
    order = Order(order_number="TEST-OTHER-GRAPH", customer_id=136, order_date=date.today())
    db.add(order)
    db.flush()
    other = OrderItem(order_id=order.id, product_id=3765, quantity=100,
        unit_price=Decimal("5"), subtotal=Decimal("500"), snapshot_product_name="其他测试单")
    db.add(other)
    db.flush()
    freeze_master_order_bom(db, order_item_id=other.id, actor=actor)
    db.commit()
    with pytest.raises(SubkitError, match="其他未完成订单"):
        assemble(db, actor, other, 3765, [output], key="graph:other")
    assert output.quantity_available == 100


def test_changed_quantity_limit_cannot_replay_same_operation_key(db):
    actor, item, kid = setup_graph(db)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    result = assemble(db, actor, item, kid, lots, quantity_limit=20)
    db.commit()
    assert result.quantity == 20
    with pytest.raises(SubkitError, match="标识"):
        assemble(db, actor, item, kid, lots, quantity_limit=21)
