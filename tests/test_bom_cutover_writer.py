import json

import pytest
from sqlalchemy import select, text

from app.models.order import OrderItem
from app.models.user import User
from app.models.multilevel_bom import OrderBomExecutionCutover, BomAssembly
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.multilevel_bom_cutover import convert_reserved_legacy_order
from app.services.multilevel_bom_cutover_review import review_legacy_cutover
from app.services.multilevel_bom_plan import BomPlanError
from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_bom_cutover_review import prepare


def request(db):
    prepare(db)
    review = review_legacy_cutover(db, order_item_id=10050, customer_id=136)
    return dict(order_item_id=10050, customer_id=136, reviewed_hash=review.checksum,
        source_lot_versions={lid: db.get(InventoryLot, lid).version for lid in [365, 366]},
        # Disposable copy only: a known valid map slot, NOT a statement of
        # 00205's factory location. Formal conversion needs the user's target.
        target_locations={3799: 1890}, operation_key="isolated-00205-conversion",
        actor=db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True))))


def test_actual_00205_conversion_and_replay_preserve_history(factory_copy):
    db = factory_copy
    payload = request(db)
    supplements = db.execute(text("SELECT * FROM finance_material_cost_supplements ORDER BY id")).all()
    old_sources = db.execute(text("SELECT * FROM sales_order_item_bom_components WHERE sales_order_item_id=10050 ORDER BY id")).all()
    old_tasks = db.execute(text("SELECT * FROM production_tasks WHERE order_item_id=10050 ORDER BY id")).all()
    old_completions = db.execute(text("SELECT * FROM production_completions WHERE order_item_id=10050 ORDER BY id")).all()
    result = convert_reserved_legacy_order(db, **payload)
    db.commit()
    assert result["execution_quantity"] == 300
    assert len(result["output_lot_ids"]) == 1
    output = db.get(InventoryLot, result["output_lot_ids"][0])
    assert output.finished_detail.product_id == 3799
    assert (output.quantity_available, output.quantity_reserved) == (0, 300)
    assert output.warehouse_location_id == 1890
    assembly = db.get(BomAssembly, result["assembly_ids"][0])
    assert json.loads(assembly.cost_detail_json)["actual"] is False
    assert db.execute(text("SELECT * FROM sales_order_item_bom_components WHERE id IN (2,3) ORDER BY id")).all() == old_sources
    assert db.execute(text("SELECT * FROM production_tasks WHERE order_item_id=10050 ORDER BY id")).all() == old_tasks
    assert db.execute(text("SELECT * FROM production_completions WHERE order_item_id=10050 ORDER BY id")).all() == old_completions
    item = db.get(OrderItem, 10050)
    assert (item.quantity, item.delivered_quantity) == (1800, 1500)
    assert [(db.get(InventoryReservation, rid).consumed_stock_quantity, db.get(InventoryReservation, rid).released_stock_quantity)
            for rid in [334, 335]] == [(4500, 900), (6000, 1200)]
    from app.services.multilevel_bom_requirements import read_graph_requirements
    from app.api.requisition import _bom_pending_component_requirements
    from app.services.multilevel_bom_receipt_projection import project_graph_receipts
    requirements = read_graph_requirements(db, item.id)
    assert requirements.order_quantity == 300 and requirements.finished_units[3799] == 300
    assert sum(row["requisition_qty"] for row in _bom_pending_component_requirements(db, item)) == 0
    summary = {"projection_inconsistent": False}
    project_graph_receipts(db, item.id, summary, [], {})
    assert summary["automatic_finished_output_qty"] == 0  # Conversion is stock, not fictitious new receipt.
    assert summary["projection_inconsistent"] is False
    before = db.execute(text("SELECT * FROM inventory_movements ORDER BY id")).all()
    assert convert_reserved_legacy_order(db, **payload) == result
    db.commit()
    assert db.execute(text("SELECT * FROM inventory_movements ORDER BY id")).all() == before
    assert db.execute(text("SELECT * FROM finance_material_cost_supplements ORDER BY id")).all() == supplements


def test_conversion_stale_review_leaves_no_graph_or_stock_change(factory_copy):
    db = factory_copy
    payload = request(db)
    payload["reviewed_hash"] = "0"*64
    before = db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all()
    with pytest.raises(BomPlanError, match="已变化"):
        convert_reserved_legacy_order(db, **payload)
    db.commit()
    assert db.get(OrderBomExecutionCutover, 10050) is None
    assert db.execute(text("SELECT * FROM inventory_lots ORDER BY id")).all() == before


def facts(db):
    names = ["sales_orders", "sales_order_items", "sales_order_item_bom_components", "inventory_lots",
        "inventory_reservations", "inventory_movements", "production_tasks", "production_completions",
        "order_bom_graphs", "order_bom_graph_products", "order_bom_execution_cutovers", "order_bom_cutover_sources",
        "bom_assemblies", "bom_assembly_inputs", "operation_logs"]
    return {name: db.execute(text(f"SELECT * FROM {name} ORDER BY 1")).all() for name in names}


@pytest.mark.parametrize("failure", ["invalid_target", "final_audit", "outer_rollback"])
def test_conversion_late_failure_restores_all_old_facts(factory_copy, monkeypatch, failure):
    from app.services import multilevel_bom_cutover as service
    from app.services.warehouse_inventory import WarehouseInventoryError
    db = factory_copy
    payload = request(db)
    before = facts(db)
    if failure == "invalid_target":
        payload["target_locations"] = {3799: 656}  # actual unplaced legacy target must stay prohibited
        with pytest.raises(WarehouseInventoryError):
            convert_reserved_legacy_order(db, **payload)
        db.commit()
    elif failure == "final_audit":
        def fail(*args, **kwargs):
            raise RuntimeError("isolated final audit failure")
        with monkeypatch.context() as patch:
            patch.setattr(service, "append_audit_event", fail)
            with pytest.raises(RuntimeError, match="final audit"):
                convert_reserved_legacy_order(db, **payload)
        db.commit()
    else:
        convert_reserved_legacy_order(db, **payload)
        db.rollback()
    assert facts(db) == before
    payload["target_locations"] = {3799: 1890}
    assert convert_reserved_legacy_order(db, **payload)["execution_quantity"] == 300
    db.commit()


def test_nonadmin_and_changed_replay_do_not_mutate_stock(factory_copy):
    db = factory_copy
    payload = request(db)
    operator = User(username="isolated-cutover-employee", password_hash="unusable-test-only", role="sales", is_active=True,
        real_name="隔离测试员工", display_name="隔离测试员工")
    db.add(operator)
    db.commit()
    before = facts(db)
    with pytest.raises(BomPlanError, match="管理员"):
        convert_reserved_legacy_order(db, **{**payload, "actor": operator})
    db.commit()
    assert facts(db) == before
    convert_reserved_legacy_order(db, **payload)
    db.commit()
    after = facts(db)
    with pytest.raises(BomPlanError, match="载荷"):
        convert_reserved_legacy_order(db, **{**payload, "target_locations": {3799: 656}})
    db.commit()
    assert facts(db) == after


def test_converted_parent_dispatch_and_cancel_use_one_set(factory_copy):
    from app.api.deliveries import _dispatch_delivery, _cancel_delivery
    from tests.test_composite_component_delivery_quantities import _delivery
    db = factory_copy
    payload = request(db)
    result = convert_reserved_legacy_order(db, **payload)
    db.commit()
    delivery, _ = _delivery(db, customer_id=136, order_item_id=10050, number="ISOLATED-CUTOVER-DISPATCH", quantity=1)
    db.commit()
    _dispatch_delivery(delivery.id, db=db, user=payload["actor"])
    db.expire_all()
    item = db.get(OrderItem, 10050)
    output = db.get(InventoryLot, result["output_lot_ids"][0])
    assert item.delivered_quantity == 1501
    assert (output.quantity_reserved, output.quantity_consumed) == (299, 1)
    from app.services.multilevel_bom_requirements import read_graph_requirements
    assert read_graph_requirements(db, item.id).finished_units[3799] == 300
    _cancel_delivery(delivery.id, db=db, user=payload["actor"])
    db.expire_all()
    assert item.delivered_quantity == 1500
    assert (output.quantity_reserved, output.quantity_consumed) == (300, 0)
