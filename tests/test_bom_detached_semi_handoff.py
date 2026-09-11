"""Public cutover of an imported cancelled report with an unused reservation."""
from decimal import Decimal
import pytest

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login, _component_payload
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _seed_order_semi_reservation, _freeze_receipt_fact, _receive
from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources, read_purchase_sources


@pytest.mark.parametrize("uncredited_sheets,credited_pieces", [(0, 6), (2, 6), (0, 30)])
def test_cancelled_original_material_moves_only_unused_semi(composite_requisition_app, _p181_published_map_identity, monkeypatch, uncredited_sheets, credited_pieces, plan_only=False):
    from app.api.bom_cutover import router
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement, SemiFinishedLotAllowedProduct
    from app.models.requisition import RequisitionItem
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.order import OrderItem
    from app.api.requisition import _bom_pending_component_requirements
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    app, factory = composite_requisition_app
    app.include_router(router, prefix="/api/orders")
    material_id, snapshots = seed_graph(factory)
    _seed_order_semi_reservation(factory, credited_piece_quantity=credited_pieces+uncredited_sheets, pieces_per_box=1)
    with factory() as db:
        old = db.scalar(select(InventoryReservation))
        old.credited_requirement_quantity = 0
        old.sales_order_item_bom_component_id = snapshots[0][0]
        requirement = db.get(OrderItemSemiRequirement, old.semi_requirement_id)
        requirement.sales_order_item_bom_component_id = snapshots[0][0]
        requirement.required_piece_quantity = 30
        requirement.board_length_mm, requirement.board_width_mm = 1000, 700
        lot = db.get(InventoryLot, old.inventory_lot_id)
        lot.estimated_unit_cost_snapshot = Decimal("0.5")
        lot.semi_finished_detail.board_length_mm, lot.semi_finished_detail.board_width_mm = 1000, 700
        db.scalar(select(SemiFinishedLotAllowedProduct)).product_id = 2
        old_id, lot_id = old.id, lot.id
        db.commit()
    with TestClient(app) as client:
        _login(client)
        purchase_sources(client, factory, material_id, snapshots)
        # Imported historical state: original reports cancelled but reservation
        # retained. This fixture does not claim the ordinary cancel UI does so.
        with factory() as db:
            db.get(InventoryReservation, old_id).credited_requirement_quantity = credited_pieces
            for line in db.scalars(select(RequisitionItem)):
                line.status = "已取消"
            for source in db.scalars(select(RequisitionItemBomSource)):
                source.active_guard = None
            db.commit()
        url = "/api/orders/items/1/material-bom-cutover"
        preview = client.post(url+"/preview", json={})
        assert preview.status_code == 200, preview.text
        review = preview.json()
        assert review["retained_semi"][0]["action"] == "transfer_to_new_source"
        assert review["retained_semi"][0]["transfer"] == dict(reserve_sheets=credited_pieces, credited_pieces=credited_pieces, released_to_available_sheets=uncredited_sheets)
        payload = {key: review[key] for key in ("reviewed_hash", "preview_hash", "rule_revision", "source_lot_versions", "target_locations")}
        payload["operation_key"] = "detached-semi-public"
        from pathlib import Path
        import sqlite3
        from tests.test_multilevel_bom_modes_migration import original_facts
        from app.services import audit_log
        from app.services.multilevel_bom_plan import BomPlanError
        with factory() as db:
            path = Path(db.get_bind().url.database)
        def facts():
            with sqlite3.connect(path.as_uri()+"?mode=ro", uri=True) as check:
                columns = {table: [row[1] for row in check.execute(f'PRAGMA table_info("{table}")')]
                    for table, in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                return original_facts(check, columns)
        before = facts()
        def fail_audit(*args, **kwargs):
            raise BomPlanError("isolated semi transfer audit failure")
        with monkeypatch.context() as patch:
            patch.setattr(audit_log, "append_audit_event", fail_audit)
            failed = client.post(url+"/execute", json=payload)
            assert failed.status_code == 409 and "audit failure" in failed.text
        assert facts() == before
        response = client.post(url+"/execute", json=payload)
        assert response.status_code == 200, response.text
        assert client.post(url+"/execute", json=payload).json() == response.json()
        if plan_only:
            from app.services.multilevel_bom_receipts import plan_semi_only_production
            before_plan = facts()
            with factory() as db:
                context, plan = plan_semi_only_production(db, order_item_id=1, product_id=2)
                assert (plan["before"], plan["after"]) == (0, 30)
                assert plan["total_cost"] == Decimal("15.0000")
                assert plan["detail"]["actual"] is False
                assert all(entry["kind"] == "reservation" for entry in plan["detail"]["bom_material_inputs"])
                assert sum(entry["quantity"] for entry in plan["detail"]["bom_material_inputs"]) == 30
            assert facts() == before_plan
            return
        with factory() as db:
            old = db.get(InventoryReservation, old_id)
            assert old.released_stock_quantity == credited_pieces+uncredited_sheets and old.consumed_stock_quantity == 0
            current = read_compiled_order_bom(db, 1)
            new = db.scalar(select(InventoryReservation).where(InventoryReservation.id != old_id))
            assert new.sales_order_item_bom_component_id in {row.id for row in current.snapshots}
            assert (new.reserved_stock_quantity, new.credited_requirement_quantity) == (credited_pieces, credited_pieces)
            assert db.get(InventoryLot, lot_id).quantity_reserved == credited_pieces
            assert db.get(InventoryLot, lot_id).quantity_available == uncredited_sheets
            pending = _bom_pending_component_requirements(db, db.get(OrderItem, 1))
            assert sorted(row["requisition_qty"] for row in pending) == [30-credited_pieces, 40]
            items = [{**_component_payload(row.id), "order_item_id": 1, "special_process": "一开一"}
                for row in current.snapshots if row.component_product_id in ({3} if credited_pieces == 30 else {2, 3})]
            source_ids = {row.id for row in current.snapshots}
        batch = client.post("/api/requisition/batches", json={"request_key": "detached-new-paper", "supplier_name": "苏州纸板供应商", "items": items})
        assert batch.status_code == 201, batch.text
        with factory() as db:
            current_lines = set(db.scalars(select(RequisitionItemBomSource.requisition_item_id).where(
                RequisitionItemBomSource.sales_order_item_bom_component_id.in_(source_ids))))
        receipt_ids = []
        for index, source in enumerate(read_purchase_sources(factory, material_id)):
            if source.supplier_item_id not in current_lines:
                continue
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"detached-price-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            received = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty, idempotency_key=f"detached-receipt-{index}")
            assert received.status_code == 200, received.text
            receipt_ids.append(received.json()["receipt_item_id"])
        semi_completion_id = None
        if credited_pieces == 30:
            semi_url = "/api/orders/items/1/semi-production"
            semi_preview = client.post(semi_url+"/preview", json={"product_id": 2})
            assert semi_preview.status_code == 200, semi_preview.text
            semi_payload = dict(product_id=2, reviewed_hash=semi_preview.json()["reviewed_hash"], operation_key="complete-from-semi")
            before_semi = facts()
            assert client.post(semi_url+"/execute", json={**semi_payload, "reviewed_hash": "0"*64}).status_code == 409
            assert facts() == before_semi
            with monkeypatch.context() as patch:
                patch.setattr(audit_log, "append_audit_event", fail_audit)
                rejected = client.post(semi_url+"/execute", json=semi_payload)
                assert rejected.status_code == 409 and "audit failure" in rejected.text
            assert facts() == before_semi
            completed = client.post(semi_url+"/execute", json=semi_payload)
            assert completed.status_code == 200, completed.text
            assert client.post(semi_url+"/execute", json=semi_payload).json() == completed.json()
            semi_completion_id = completed.json()["completion_id"]
            listed = client.get(semi_url).json()
            assert any(row["id"] == semi_completion_id and row["status"] == "posted" for row in listed["completions"])
            after_semi = facts()
            from app.models.purchase_receipt import IncomingReceiptPurposeAllocation, PurchaseReceiptFact
            from app.models.incoming_receipt import IncomingReceiptItem
            for model in (IncomingReceiptPurposeAllocation, PurchaseReceiptFact, IncomingReceiptItem):
                assert after_semi[model.__tablename__] == before_semi[model.__tablename__]
            from app.models.production import ProductionCompletion, ProductionTask
            from app.models.multilevel_bom import BomAssembly
            with factory() as db:
                completion = db.get(ProductionCompletion, semi_completion_id)
                assert completion.origin == "manual"
                assert db.get(ProductionTask, completion.task_id).material_received_quantity == 0
                main_task = db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == 1,
                    ProductionTask.sales_order_item_bom_component_id.is_(None)))
                assert main_task.finished_coverage_snapshot == 10
                assert main_task.material_received_quantity == 40 and main_task.material_input_quantity == 70
                produced_lot = db.get(InventoryLot, completion.inventory_lot_id)
                assert produced_lot.cost_snapshot_source == "semi_finished_estimate"
                assert sum(db.get(InventoryLot, assembly.output_lot_id).quantity_reserved
                    for assembly in db.scalars(select(BomAssembly).where(BomAssembly.output_product_id == 1,
                        BomAssembly.output_lot_id.is_not(None), BomAssembly.status == "posted"))) == 10
        with factory() as db:
            assert db.get(InventoryReservation, old_id).consumed_stock_quantity == 0
            assert db.get(InventoryReservation, new.id).consumed_stock_quantity == credited_pieces
            assert db.get(InventoryLot, lot_id).quantity_consumed == credited_pieces
        if semi_completion_id:
            before_reverse = facts()
            with monkeypatch.context() as patch:
                patch.setattr(audit_log, "append_audit_event", fail_audit)
                failed_reverse = client.post(f"{semi_url}/{semi_completion_id}/revert")
                assert failed_reverse.status_code == 409 and "audit failure" in failed_reverse.text
            assert facts() == before_reverse
            undone_semi = client.post(f"{semi_url}/{semi_completion_id}/revert")
            assert undone_semi.status_code == 200, undone_semi.text
            assert client.post(f"{semi_url}/{semi_completion_id}/revert").json() == undone_semi.json()
        for receipt_id in reversed(receipt_ids):
            undone = client.put(f"/api/incoming/receipt-items/{receipt_id}/revert", json={})
            assert undone.status_code == 200, undone.text
        with factory() as db:
            assert db.get(InventoryReservation, old_id).consumed_stock_quantity == 0
            assert db.get(InventoryReservation, old_id).released_stock_quantity == credited_pieces+uncredited_sheets
            assert db.get(InventoryReservation, new.id).consumed_stock_quantity == 0
            assert db.get(InventoryLot, lot_id).quantity_reserved == credited_pieces
            assert db.get(InventoryLot, lot_id).quantity_available == uncredited_sheets


def test_semi_only_plan_has_no_synthetic_receipt(composite_requisition_app, _p181_published_map_identity, monkeypatch):
    test_cancelled_original_material_moves_only_unused_semi(composite_requisition_app,
        _p181_published_map_identity, monkeypatch, 0, 30, plan_only=True)
