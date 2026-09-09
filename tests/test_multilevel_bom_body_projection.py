from sqlalchemy import select

from tests.test_multilevel_bom_body_assembly_ledger import seed, composite_requisition_app, _p181_published_map_identity
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionCompletion
from app.models.order import OrderItem
from app.services.multilevel_bom_receipt_projection import project_graph_receipts
from app.services.multilevel_bom_receipts import refresh_graph_main_task
from decimal import Decimal
import pytest
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.production_workflow import post_automatic_receipt_completion


def test_automatic_body_completion_does_not_reserve_or_mark_finished(composite_requisition_app, _p181_published_map_identity):
    _, factory = composite_requisition_app
    seed(factory, child_quantity=0)
    with factory() as db:
        snapshot = db.scalar(select(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.component_product_id == 1))
        args = dict(order_item_id=1, previous_theoretical_quantity=5, new_theoretical_quantity=8,
            material_input_delta=3, material_input_cumulative=8, operator_id=1,
            idempotency_key="real-body-automatic-completion", capitalized_material_cost=Decimal("0.60"),
            cost_detail={"bom_snapshot_id":snapshot.id, "bom_material_product_id":1,
                "bom_material_inputs":[], "actual":True, "currency":"CNY"}, bom_snapshot_id=snapshot.id)
        completion = post_automatic_receipt_completion(db, **args)
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        assert lot.inventory_type == "assembly_body" and lot.quantity_available == 3
        assert lot.finished_detail is None and lot.quantity_reserved == 0
        assert completion.order_reserved_quantity == 0
        assert completion.direct_delivery_quantity == 0 and completion.stock_quantity == 3
        assert list(db.scalars(select(InventoryReservation))) == []
        assert post_automatic_receipt_completion(db, **args).id == completion.id
        task = refresh_graph_main_task(db, db.get(OrderItem,1), create_if_missing=False)
        assert task.finished_coverage_snapshot == 0 and task.status != "completed"
        db.commit()


@pytest.mark.parametrize("body_last", [False, True])
@pytest.mark.parametrize("dispatch", [False, True])
def test_real_http_body_receipts_assemble_cost_and_reverse(composite_requisition_app, _p181_published_map_identity, monkeypatch, body_last, dispatch):
    from fastapi.testclient import TestClient
    from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources, _login, _freeze_receipt_fact, _receive
    from app.models.multilevel_bom import BomAssembly
    from app.services.multilevel_bom_cost_lineage import graph_material_sources
    from app.services.receipt_managed_production import receipt_purpose_summaries_by_order_item_ids
    app, factory = composite_requisition_app
    from app.api.deliveries import router
    app.include_router(router, prefix="/api/deliveries")
    material_id, snapshots = seed_graph(factory, liner=True, body=True)
    with TestClient(app) as client:
        _login(client)
        receipt_ids = []
        sources = purchase_sources(client, factory, material_id, snapshots)
        if body_last:
            sources = sources[1:] + sources[:1]
        for index, source in enumerate(sources):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"body-price-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            if index == len(sources)-1:
                from app.services import multilevel_bom_inventory as inventory
                from app.services.bom_subkits import SubkitError
                original = inventory.assemble_subkit_inventory
                def fail_after_outer(db, **kwargs):
                    output = original(db, **kwargs)
                    if kwargs["graph_product_id"] == 1 and output.quantity:
                        raise SubkitError("injected after outer body assembly")
                    return output
                with factory() as db:
                    before = ([r.id for r in db.scalars(select(ProductionCompletion))],
                              [r.id for r in db.scalars(select(BomAssembly))])
                with monkeypatch.context() as patch:
                    patch.setattr(inventory, "assemble_subkit_inventory", fail_after_outer)
                    failed = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
                        idempotency_key=f"body-receipt-{index}")
                    assert failed.status_code == 409, failed.text
                with factory() as db:
                    assert ([r.id for r in db.scalars(select(ProductionCompletion))],
                            [r.id for r in db.scalars(select(BomAssembly))]) == before
            result = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
                idempotency_key=f"body-receipt-{index}")
            assert result.status_code == 200, result.text
            receipt_ids.append(result.json()["receipt_item_id"])
        with factory() as db:
            body = db.scalar(select(InventoryLot).where(InventoryLot.inventory_type == "assembly_body"))
            assert body.quantity_available == 0 and body.quantity_consumed == 10 and body.quantity_reserved == 0
            outer = db.scalar(select(BomAssembly).where(BomAssembly.output_product_id==1, BomAssembly.quantity>0))
            output = db.get(InventoryLot, outer.output_lot_id)
            assert output.quantity_reserved == 10
            evidence = graph_material_sources(db, output)
            assert len({r["purchase_receipt_fact_id"] for r in evidence}) == 3
            assert sum(r["amount"] for r in evidence) == Decimal("11.1060")
            summary = receipt_purpose_summaries_by_order_item_ids(db,[1])[1]
            assert summary["automatic_finished_output_qty"] == 10
            assert summary["current_theoretical_finished_capacity_qty"] == 10
            assert summary["projection_inconsistent"] is False
        pending = client.get("/api/deliveries/pending_items")
        assert pending.status_code == 200, pending.text
        line = next(r for r in pending.json()["items"] if r["order_item_id"] == 1)
        assert {r["component_product_id"] for r in line["component_lines"]} == {1}
        if dispatch:
            created = client.post("/api/deliveries", json={"customer_id":1,"delivery_date":"2026-09-10",
                "items":[{"order_item_id":1,"delivered_quantity":4}]})
            assert created.status_code == 201, created.text
            did = created.json()["id"]
            sent = client.put(f"/api/deliveries/{did}/dispatch")
            assert sent.status_code == 200, sent.text
            with factory() as db:
                assert db.get(OrderItem,1).delivered_quantity == 4
                body = db.scalar(select(InventoryLot).where(InventoryLot.inventory_type == "assembly_body"))
                assert body.quantity_consumed == 10 and body.quantity_reserved == 0
            cancelled = client.put(f"/api/deliveries/{did}/cancel")
            assert cancelled.status_code == 200, cancelled.text
            # Existing downstream-history gate stays fail-closed. A cancelled
            # dispatch is not proof that every later effect can be unwound.
            blocked = client.put(f"/api/incoming/receipt-items/{receipt_ids[-1]}/revert", json={})
            assert blocked.status_code == 409, blocked.text
            with factory() as db:
                assert db.get(OrderItem,1).delivered_quantity == 0
                assert all(r.status == "posted" for r in db.scalars(select(BomAssembly)))
            return
        for rid in reversed(receipt_ids):
            result = client.put(f"/api/incoming/receipt-items/{rid}/revert", json={})
            assert result.status_code == 200, result.text
        with factory() as db:
            assert all(r.status == "reversed" for r in db.scalars(select(BomAssembly)))
            assert all(r.status == "reversed" for r in db.scalars(select(ProductionCompletion)))


def test_body_receipt_is_not_finished_output_or_task_coverage(composite_requisition_app, _p181_published_map_identity):
    _, factory = composite_requisition_app
    seed(factory, child_quantity=0)
    with factory() as db:
        completion = db.scalar(select(ProductionCompletion))
        completion.actual_output_quantity = 5
        snapshot = db.scalar(select(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.component_product_id == 1))
        summary = {"projection_inconsistent": False}
        project_graph_receipts(db, 1, summary, [{"component_key":f"bom:{snapshot.id}:whole",
            "received_capacity":5,"planned_capacity":5,
            "received_order_sheet_qty":5,"planned_order_sheet_qty":5}], {})
        assert summary["automatic_finished_output_qty"] == 0
        assert summary["current_theoretical_finished_capacity_qty"] == 0
        assert summary["product_output_quantities"][1] == 0
        assert summary["projection_inconsistent"] is False
        task = refresh_graph_main_task(db, db.get(OrderItem,1), create_if_missing=False)
        assert task.finished_coverage_snapshot == 0 and task.status != "completed"
