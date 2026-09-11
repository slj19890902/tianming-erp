from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.multilevel_bom import BomAssembly
from app.models.warehouse_inventory import InventoryLot
from app.services.bom_subkits import SubkitError
from app.services.multilevel_bom_inventory import assemble_order_inventory, reverse_order_assembly
from tests.test_multilevel_bom_inventory import db, setup_graph, raw


def run(db, actor, item, kid, lots, key="test:graph:batch", **overrides):
    args = dict(order_item_id=item.id, source_lot_versions={l.id: l.version for l in lots},
        target_locations={kid: 1890, 3765: 1890}, operation_key=key,
        operator_id=actor.id, available_lot_ids=[l.id for l in lots])
    args.update(overrides)
    return assemble_order_inventory(db, **args)


def test_bottom_up_receipt_batch_cost_replay_and_reverse(db):
    actor, item, kid = setup_graph(db, root_assembly=True)
    lots = [raw(db, actor, 3788, 220), raw(db, actor, 3789, 660)]
    versions = {l.id: l.version for l in lots}
    rows = run(db, actor, item, kid, lots)
    db.commit()
    assert [(r.output_product_id, r.quantity) for r in rows] == [(kid, 100), (3765, 100)]
    assert rows[-1].total_cost == Decimal("100")
    assert [l.quantity_available for l in lots] == [20, 60]
    replay = run(db, actor, item, kid, lots, source_lot_versions=versions)
    assert [r.id for r in replay] == [r.id for r in rows]
    assert db.scalar(select(func.count()).select_from(BomAssembly)) == 2
    with pytest.raises(SubkitError, match="载荷"):
        run(db, actor, item, kid, lots)
    reverse_order_assembly(db, order_item_id=item.id, operation_key="test:graph:batch", operator_id=actor.id)
    db.commit()
    assert [l.quantity_available for l in lots] == [220, 660]
    assert all(r.status == "reversed" for r in rows)
    with pytest.raises(SubkitError, match="已撤销"):
        run(db, actor, item, kid, lots, source_lot_versions=versions)


def test_later_layer_failure_rolls_back_entire_batch(db, monkeypatch):
    actor, item, kid = setup_graph(db, root_assembly=True)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    from app.services import multilevel_bom_inventory as service
    original = service.assemble_subkit_inventory
    def fail(db, **kwargs):
        if kwargs["graph_product_id"] == 3765:
            raise RuntimeError("outer layer failed")
        return original(db, **kwargs)
    monkeypatch.setattr(service, "assemble_subkit_inventory", fail)
    with pytest.raises(RuntimeError, match="outer layer"):
        run(db, actor, item, kid, lots)
    assert db.scalar(select(func.count()).select_from(BomAssembly)) == 0
    assert [db.get(InventoryLot, l.id).quantity_available for l in lots] == [200, 600]


def test_zero_output_batch_is_idempotent_and_later_receipt_uses_new_key(db):
    actor, item, kid = setup_graph(db, root_assembly=True)
    long = raw(db, actor, 3788, 200)
    versions = {long.id: long.version}
    rows = run(db, actor, item, kid, [long])
    db.commit()
    assert [r.quantity for r in rows] == [0, 0]
    assert [r.id for r in run(db, actor, item, kid, [long], source_lot_versions=versions)] == [r.id for r in rows]
    short = raw(db, actor, 3789, 600)
    result = run(db, actor, item, kid, [long, short], key="next-receipt")
    assert [r.quantity for r in result] == [100, 100]


def test_partial_two_level_receipts_do_not_double_credit_consumed_inner(db):
    actor, item, kid = setup_graph(db, root_assembly=True)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    # Only half is explicitly available; the remaining half is unavailable to
    # this operation, modelled as another order's reservation balance.
    for lot, qty in zip(lots, (100, 300)):
        lot.quantity_available -= qty
        lot.quantity_reserved += qty
        lot.version += 1
    db.commit()
    first = run(db, actor, item, kid, lots, key="partial:1")
    db.commit()
    assert [r.quantity for r in first] == [50, 50]
    # Release fixture-only held balances; actual reservation release is tested
    # by the existing stock service. No conversion or receipt fact is changed.
    for lot in lots:
        lot.quantity_available += lot.quantity_reserved
        lot.quantity_reserved = 0
        lot.version += 1
    db.commit()
    second = run(db, actor, item, kid, lots, key="partial:2")
    db.commit()
    assert [r.quantity for r in second] == [50, 50]
    assert sum(r.quantity for r in (first[-1], second[-1])) == 100
    third = run(db, actor, item, kid, lots, key="partial:3")
    assert [r.quantity for r in third] == [0, 0]


def test_unapproved_free_stock_and_invalid_unused_lots_are_not_consumed(db):
    actor, item, kid = setup_graph(db, root_assembly=True)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    result = run(db, actor, item, kid, lots, available_lot_ids=[])
    assert [r.quantity for r in result] == [0, 0]
    assert [l.quantity_available for l in lots] == [200, 600]
    with pytest.raises(SubkitError, match="版本"):
        run(db, actor, item, kid, lots, key="bad-version",
            source_lot_versions={lots[0].id: 999, lots[1].id: lots[1].version})


def test_accompany_only_assembles_liner_not_carton(db):
    actor, item, kid = setup_graph(db)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    result = run(db, actor, item, kid, lots, target_locations={kid: 1890})
    assert [(r.output_product_id, r.quantity) for r in result] == [(kid, 100)]
    db.rollback()
    assert db.scalar(select(func.count()).select_from(BomAssembly)) == 0


def test_partial_reversed_batch_cannot_be_replayed(db):
    actor, item, kid = setup_graph(db, root_assembly=True)
    lots = [raw(db, actor, 3788, 200), raw(db, actor, 3789, 600)]
    versions = {l.id: l.version for l in lots}
    rows = run(db, actor, item, kid, lots)
    db.commit()
    from app.services.bom_subkit_inventory import reverse_subkit_conversion
    reverse_subkit_conversion(db, conversion_id=rows[-1].id, operator_id=actor.id, graph_assembly=True)
    db.commit()
    with pytest.raises(SubkitError, match="已撤销"):
        run(db, actor, item, kid, lots, source_lot_versions=versions)


@pytest.mark.parametrize("status", ["dead", "completed", "archived", "delivered"])
def test_finished_orders_cannot_start_new_batch(db, status):
    actor, item, kid = setup_graph(db, root_assembly=True)
    from app.models.order import Order
    db.get(Order, item.order_id).status = status
    db.commit()
    with pytest.raises(SubkitError, match="已结束"):
        run(db, actor, item, kid, [])
    assert db.scalar(select(func.count()).select_from(BomAssembly)) == 0
