"""Real body ledger + frozen material identities; no master gate bypass in app."""
import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, delete

from tests.test_multilevel_bom_body_inventory import setup, composite_requisition_app, _p181_published_map_identity
from app.models.order import OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
from app.models.warehouse_inventory import InventoryLot
from app.services.multilevel_bom_body_inventory import receive_body_inventory
from app.services.bom_subkit_inventory import assemble_subkit_inventory, reverse_subkit_conversion
from app.services.warehouse_inventory import manual_finished_in
from app.services.bom_subkits import SubkitError


def seed(factory, *, child_quantity):
    cid, lid = setup(factory)
    with factory() as db:
        item = db.get(OrderItem, 1)
        # Replace anonymous legacy fixture rows only, before any procurement.
        db.execute(delete(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.sales_order_item_id == 1))
        for pid, multiplier in ((1, 1), (2, 2)):
            p = db.get(Product, pid)
            db.add(SalesOrderItemBomComponent(sales_order_item_id=1,
                component_product_id=pid, parent_product_version=db.get(Product, 1).version,
                component_product_version=p.version, snapshot_schema_version=5,
                order_set_quantity=item.quantity, quantity_per_set=multiplier,
                required_piece_quantity=item.quantity * multiplier, display_order=pid,
                internal_component_code=f"BODY-{pid}", is_die_cut=False, spare_sheet_quantity=0,
                display_mode="internal_only", is_required=True,
                snapshot_component_product_code=p.product_code,
                snapshot_component_product_name=p.product_name,
                snapshot_component_box_category="normal", snapshot_component_default_cutting_mode="一开一"))
        body = receive_body_inventory(db, completion_id=cid, location_id=lid, operator_id=1,
            idempotency_key="body-assembly-in", expected_layout_version=2)
        body.cost_snapshot_detail_json = json.dumps({"bom_material_product_id":1,
            "capitalized_material_cost":"1.2345", "currency":"CNY", "actual":True})
        lots = [body]
        if child_quantity:
            child = manual_finished_in(db, customer_id=1, product_id=2, location_id=lid,
                quantity=child_quantity, stock_date=date(2026,9,10), operator_id=1,
                source_type="manual", remarks="isolated body assembly test",
                idempotency_key="body-child-in", expected_layout_version=2)
            child.estimated_unit_cost_snapshot = Decimal("0.10")
            lots.append(child)
        db.commit()
        return lid, {lot.id:lot.version for lot in lots}


@pytest.mark.parametrize("child_quantity,expected", [(0,0),(6,3),(20,5)])
def test_body_and_children_consumed_once_and_reverse_restores_each_stage(
    composite_requisition_app, _p181_published_map_identity, child_quantity, expected
):
    _, factory = composite_requisition_app
    lid, versions = seed(factory, child_quantity=child_quantity)
    args = dict(order_item_id=1, graph_product_id=1, source_lot_versions=versions,
        target_location_id=lid, operation_key="body-assemble", operator_id=1,
        available_lot_ids=list(versions))
    with factory() as db:
        result = assemble_subkit_inventory(db, **args)
        assert result.quantity == expected
        assert assemble_subkit_inventory(db, **args).id == result.id
        with pytest.raises(SubkitError, match="标识"):
            assemble_subkit_inventory(db, **{**args, "available_lot_ids":[]})
        body = db.get(InventoryLot, next(iter(versions)))
        assert body.inventory_type == "assembly_body" and body.finished_detail is None
        assert body.quantity_available == 5-expected and body.quantity_consumed == expected
        assert body.quantity_reserved == 0
        if expected:
            output = db.get(InventoryLot, result.output_lot_id)
            assert output.inventory_type == "finished" and output.finished_detail.product_id == 1
            assert output.quantity_available == expected
            assert result.total_cost == (Decimal("1.2345") * expected / 5).quantize(Decimal("0.0001")) + Decimal("0.2")*expected
            inputs = list(db.scalars(select(BomAssemblyInput).where(BomAssemblyInput.conversion_id==result.id)))
            assert {r.product_id:r.quantity for r in inputs} == {1:expected, 2:expected*2}
        reverse_subkit_conversion(db, conversion_id=result.id, operator_id=1, graph_assembly=True)
        assert body.quantity_available == 5 and body.quantity_consumed == 0
        if child_quantity:
            child = db.get(InventoryLot, list(versions)[1])
            assert child.quantity_available == child_quantity and child.quantity_consumed == 0
        db.commit()


def test_finished_output_cannot_substitute_for_body(composite_requisition_app, _p181_published_map_identity):
    _, factory = composite_requisition_app
    lid, versions = seed(factory, child_quantity=20)
    with factory() as db:
        finished = manual_finished_in(db, customer_id=1, product_id=1, location_id=lid,
            quantity=5, stock_date=date(2026,9,10), operator_id=1, source_type="manual",
            remarks="not a body", idempotency_key="not-body", expected_layout_version=2)
        inputs = {list(versions)[1]:versions[list(versions)[1]], finished.id:finished.version}
        with pytest.raises(SubkitError, match="产品"):
            assemble_subkit_inventory(db, order_item_id=1, graph_product_id=1,
                source_lot_versions=inputs, target_location_id=lid, operation_key="invalid-body",
                operator_id=1, available_lot_ids=list(inputs))
        assert finished.quantity_available == 5 and finished.quantity_consumed == 0
        assert list(db.scalars(select(BomAssembly))) == []


def test_body_assembly_output_failure_rolls_back_all_inputs(composite_requisition_app, _p181_published_map_identity, monkeypatch):
    _, factory = composite_requisition_app
    lid, versions = seed(factory, child_quantity=20)
    from app.services import bom_subkit_inventory as service
    def fail(*args, **kwargs):
        raise RuntimeError("after body and child debits")
    monkeypatch.setattr(service, "manual_finished_in", fail)
    with factory() as db:
        with pytest.raises(RuntimeError, match="after body"):
            assemble_subkit_inventory(db, order_item_id=1, graph_product_id=1,
                source_lot_versions=versions, target_location_id=lid, operation_key="body-fail",
                operator_id=1, available_lot_ids=list(versions))
        assert [db.get(InventoryLot,lid).quantity_available for lid in versions] == [5,20]
        assert list(db.scalars(select(BomAssembly))) == []
        assert list(db.scalars(select(BomAssemblyInput))) == []
        db.commit()


def test_graph_orchestrator_keeps_finished_stock_separate_from_body(composite_requisition_app, _p181_published_map_identity):
    from app.services.multilevel_bom_inventory import assemble_order_inventory, reverse_order_assembly
    _, factory = composite_requisition_app
    lid, versions = seed(factory, child_quantity=20)
    with factory() as db:
        finished = manual_finished_in(db, customer_id=1, product_id=1, location_id=lid,
            quantity=2, stock_date=date(2026,9,10), operator_id=1, source_type="manual",
            remarks="already finished, not body", idempotency_key="finished-not-body", expected_layout_version=2)
        versions[finished.id] = finished.version
        args = dict(order_item_id=1, source_lot_versions=versions, target_locations={1:lid},
            operation_key="body-full-graph", operator_id=1, available_lot_ids=list(versions))
        result, = assemble_order_inventory(db, **args)
        assert result.quantity == 5
        assert finished.quantity_available == 2 and finished.quantity_consumed == 0
        assert assemble_order_inventory(db, **args)[0].id == result.id
        reverse_order_assembly(db, order_item_id=1, operation_key="body-full-graph", operator_id=1)
        assert [db.get(InventoryLot,lid).quantity_available for lid in versions] == [5,20,2]
        db.commit()
