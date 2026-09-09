from app.models.warehouse_inventory import InventoryLot
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_inventory import reverse_order_assembly
from tests.test_bom_subkit_inventory import db, raw
from tests.test_multilevel_bom_inventory import setup_graph, assemble
from tests.test_multilevel_bom_orchestration import run
from tests.test_bom_cutover_source_read import seed_read_boundary


def test_single_layer_only_assembles_execution_balance(db):
    actor, item, kid = setup_graph(db, quantity=80)
    compiled = read_compiled_order_bom(db, item.id)
    seed_read_boundary(db, actor, item, compiled)
    lots = [raw(db, actor, 3788, 220), raw(db, actor, 3789, 660)]
    result = assemble(db, actor, item, kid, lots)
    db.commit()
    assert result.quantity == 80
    assert [lot.quantity_available for lot in lots] == [60,180]
    assert item.quantity == 100 and item.delivered_quantity == 20


def test_bottom_up_only_assembles_execution_balance_and_does_not_repeat_it(db):
    actor, item, kid = setup_graph(db, root_assembly=True, quantity=80)
    compiled = read_compiled_order_bom(db, item.id)
    seed_read_boundary(db, actor, item, compiled)
    lots = [raw(db, actor, 3788, 220), raw(db, actor, 3789, 660)]
    results = run(db, actor, item, kid, lots)
    db.commit()
    assert [row.quantity for row in results] == [80,80]
    assert db.get(InventoryLot, results[-1].output_lot_id).quantity_available == 80
    assert [lot.quantity_available for lot in lots] == [60,180]
    again = run(db, actor, item, kid, lots, key="new-key-same-execution")
    db.commit()
    assert [row.quantity for row in again] == [0,0]
    assert [lot.quantity_available for lot in lots] == [60,180]
    reverse_order_assembly(db, order_item_id=item.id, operation_key="test:graph:batch", operator_id=actor.id)
    db.commit()
    assert [lot.quantity_available for lot in lots] == [220,660]
    assert item.quantity == 100 and item.delivered_quantity == 20
