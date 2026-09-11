"""Original material FIFO survives a reviewed structural source handoff."""
import json
from decimal import Decimal
import pytest

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact, _receive
from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources


@pytest.mark.parametrize("splice", [False, True])
def test_partial_material_receipt_preserves_original_consumption(composite_requisition_app, _p181_published_map_identity, splice):
    from app.models.order import OrderItem
    from app.models.user import User
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import InventoryLot
    from app.models.multilevel_bom import OrderBomSourceHandoff
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.multilevel_bom_rule_impact import review_current_rule_requirements
    from app.services.multilevel_bom_rule_cutover import persist_reviewed_rule
    from app.services.multilevel_bom_execution_boundary import _source_identity
    from app.services.multilevel_bom_output_history import completion_source_id, completion_material_source_id
    app, factory = composite_requisition_app
    material_id, snapshots = seed_graph(factory, splice=splice)
    output_per_sheet = 2 if splice else 1
    pieces_per_sheet = 4 if splice else 1
    with TestClient(app) as client:
        _login(client)
        source = purchase_sources(client, factory, material_id, snapshots, splice=splice)[0]
        fact = _freeze_receipt_fact(client, source, idempotency_key="handoff-paper-price", unit_price="0.1234")
        assert fact.status_code == 200, fact.text
        half = source.order_purpose_sheet_qty // 2
        next_quantity = (source.order_purpose_sheet_qty - half) // 2
        first = _receive(client, source, fact.json(), quantity=half, idempotency_key="paper-before")
        assert first.status_code == 200, first.text
        with factory() as db:
            original = read_compiled_order_bom(db, 1)
            before = db.scalar(select(ProductionCompletion).where(ProductionCompletion.order_item_id == 1))
            assert before.actual_output_quantity == half * output_per_sheet
            before_id = before.id
            original_source = completion_source_id(db, before)
            original_lot = db.get(InventoryLot, before.inventory_lot_id)
            original_cost = original_lot.cost_snapshot_detail_json
            review = review_current_rule_requirements(db, order_item_id=1, customer_id=1)
            # Isolated fixture: the public in-process material writer is still
            # pending. Receipt posting below uses the real HTTP transaction.
            revision = persist_reviewed_rule(db, review=review, item=db.get(OrderItem, 1), previous=None,
                expected_revision=0, reviewed_hash=review.checksum, request_hash="d" * 64,
                operation_key="isolated-material-handoff", actor=db.get(User, 1))
            revision_id = revision.id
            mapping = [(old, next(row for row in review.proposed.snapshots
                if row.component_product_id == old.component_product_id)) for old in original.snapshots
                if next(node for node in original.graph.nodes if node.product_id == old.component_product_id).source == "manufactured"]
            links = [dict(revision_id=revision_id, source_snapshot_id=old.id, target_snapshot_id=new.id,
                order_item_id=1, product_id=old.component_product_id, source_kind="manufactured",
                source_basis_hash=_source_identity(old)["hash"], target_basis_hash=_source_identity(new)["hash"])
                for old, new in mapping]
            db.commit()
        blocked = _receive(client, source, fact.json(), quantity=next_quantity,
            idempotency_key="paper-after")
        assert blocked.status_code == 409, blocked.text
        with factory() as db:
            db.add_all([OrderBomSourceHandoff(**values) for values in links])
            db.commit()
        def assert_material_credit():
            from app.services.multilevel_bom_carried_material import carried_material_pieces
            from app.api.requisition import _bom_snapshot_requirements
            with factory() as db:
                current = read_compiled_order_bom(db, 1)
                target = next(row for row in current.snapshots
                    if row.id == next(link["target_snapshot_id"] for link in links
                        if link["source_snapshot_id"] == original_source))
                credit = carried_material_pieces(db, current)[target.component_product_id, "whole"]
                assert credit == (source.order_purpose_sheet_qty-half) * pieces_per_sheet
                requirement = _bom_snapshot_requirements(db, target)
                assert requirement["carried_material_piece_qty"] == credit
                # The old finished lot has not been allocated by this fixture.
                # It cannot be counted again as free stock or inherited paper.
                assert requirement["inventory_covered_piece_qty"] == 0
                assert requirement["requisition_qty"] == half
        assert_material_credit()
        second = _receive(client, source, fact.json(), quantity=next_quantity,
            idempotency_key="paper-after")
        assert second.status_code == 200, second.text
        assert _receive(client, source, fact.json(), quantity=next_quantity,
            idempotency_key="paper-after").status_code == 200
        with factory() as db:
            completions = list(db.scalars(select(ProductionCompletion).where(ProductionCompletion.order_item_id == 1)
                .order_by(ProductionCompletion.id)))
            assert len(completions) == 2
            assert sum(row.actual_output_quantity for row in completions) == (half + next_quantity) * output_per_sheet
            assert completions[0].id == before_id
            assert db.get(InventoryLot, completions[0].inventory_lot_id).cost_snapshot_detail_json == original_cost
            assert completion_source_id(db, completions[1]) != original_source
            assert completion_material_source_id(db, completions[1]) == original_source
            detail = json.loads(db.get(InventoryLot, completions[1].inventory_lot_id).cost_snapshot_detail_json)
            assert detail["bom_material_source_snapshot_id"] == original_source
            assert sum(row["quantity"] for row in detail["bom_material_inputs"]) == next_quantity * pieces_per_sheet
            from app.services.multilevel_bom_cost_lineage import graph_material_sources
            from app.services.bom_subkits import SubkitError
            lot = db.get(InventoryLot, completions[1].inventory_lot_id)
            portions = graph_material_sources(db, lot)
            assert sum(row["amount"] for row in portions) == Decimal(detail["capitalized_material_cost"])
            lot.cost_snapshot_detail_json = json.dumps({**detail, "bom_material_source_snapshot_id": -1})
            with pytest.raises(SubkitError, match="材料来源身份无效"):
                graph_material_sources(db, lot)
            db.rollback()
        last = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty-half-next_quantity,
            idempotency_key="paper-last")
        assert last.status_code == 200, last.text
        assert_material_credit()
        with factory() as db:
            posted = list(db.scalars(select(ProductionCompletion).where(ProductionCompletion.order_item_id == 1,
                ProductionCompletion.status == "posted")))
            assert len(posted) == 3
            assert sum(row.actual_output_quantity for row in posted) == source.order_purpose_sheet_qty * output_per_sheet
        reverted_last = client.put(f"/api/incoming/receipt-items/{last.json()['receipt_item_id']}/revert", json={})
        assert reverted_last.status_code == 200, reverted_last.text
        reverted = client.put(f"/api/incoming/receipt-items/{second.json()['receipt_item_id']}/revert", json={})
        assert reverted.status_code == 200, reverted.text
        assert_material_credit()
        with factory() as db:
            original = db.get(ProductionCompletion, before_id)
            assert original.status == "posted" and original.actual_output_quantity == half * output_per_sheet
            assert db.get(InventoryLot, original.inventory_lot_id).cost_snapshot_detail_json == original_cost
            posted = list(db.scalars(select(ProductionCompletion).where(ProductionCompletion.order_item_id == 1,
                ProductionCompletion.status == "posted")))
            assert [row.id for row in posted] == [before_id]
        from tests.test_n039_composite_bom_requisition import _component_payload
        target_id = next(link["target_snapshot_id"] for link in links
            if link["source_snapshot_id"] == original_source)
        additional = client.post("/api/requisition/batches", json={"request_key": "carried-paper-difference",
            "supplier_name": "苏州纸板供应商", "items": [{**_component_payload(target_id),
                "order_item_id": 1, "component_type": "whole",
                "special_process": "一开四" if splice else "一开一"}]})
        assert additional.status_code == 201, additional.text
        from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
        from app.models.product_bom import RequisitionItemBomSource
        with factory() as db:
            purposes = list(db.scalars(select(PurchasePurposeSourceSnapshot).join(RequisitionItemBomSource,
                RequisitionItemBomSource.id == PurchasePurposeSourceSnapshot.source_bom_requisition_source_id).where(
                    RequisitionItemBomSource.sales_order_item_bom_component_id == target_id)))
            assert len(purposes) == 1 and purposes[0].order_purpose_sheet_qty == half
