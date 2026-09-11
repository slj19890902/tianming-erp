"""One commercial order: loose stock, material conversion, receipts and settlement."""
from decimal import Decimal
import pytest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.product import Product
from app.models.user import User
from app.models.order import OrderItem
from app.models.warehouse_inventory import InventoryLot
from app.services.multilevel_bom_orders import read_compiled_order_bom
from tests.test_multilevel_bom_receipt_flow import seed_graph, read_purchase_sources
from tests.test_multilevel_bom_master import save
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login, _component_payload
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact, _receive
from tests.test_bom_commercial_settlement import _dispatch, _confirm_receipt


@pytest.mark.parametrize("reverse_receipts,short_received", [(False, 0), (True, 0), (False, 20), (False, 1)])
def test_component_order_loose_stock_to_receipts_delivery_and_statement(
    composite_requisition_app, _p181_published_map_identity, reverse_receipts, short_received, monkeypatch,
):
    from app.api.deliveries import router as delivery_router
    from app.api.finance import router as finance_router
    from app.api import products as products_api
    from fastapi.encoders import jsonable_encoder
    from app.core.time_contract import beijing_today
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from app.models.finance import Statement
    from app.models.multilevel_bom import BomAssembly
    from app.services.multilevel_bom_requirements import read_graph_requirements

    app, factory = composite_requisition_app
    app.include_router(delivery_router, prefix="/api/deliveries")
    app.include_router(finance_router, prefix="/api/finance")
    app.include_router(products_api.router, prefix="/api/master/products")
    from app.api.bom_cutover import router as cutover_router
    app.include_router(cutover_router, prefix="/api/orders")
    material_id, _ = seed_graph(factory, separate=True, quantity=100,
                               cutting_modes={2: "一开四", 3: "一开二"}, finished_slot_count=16)
    lots = {}
    with factory() as db:
        parent = db.get(Product, 1)
        fields = products_api._product_payload_snapshot(parent)
        fields.update(product_code="ADMIN-LOOSE-100", customer_material_code="ADMIN-LOOSE-100",
                      combination_mode="component_priced", composite_fulfillment_mode="component_delivery")
        actor = db.get(User, 1)
        for pid, qty in [(2, 20), (3, 50)]:
            product = db.get(Product, pid)
            product.flute_type = product.material.flute_type
            product.splice_mode = "single"
            save(db, actor, pid, "manufactured", [])
            target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=pid)
            lot = manual_finished_in(db, customer_id=1, product_id=pid,
                location_id=target.location.id, quantity=qty+(10 if pid == 2 else 30), stock_date=beijing_today(),
                source_type="manual", remarks="隔离零散库存", operator_id=1,
                idempotency_key=f"priced-loose-{pid}", expected_layout_version=target.layout_version)
            lots[pid] = (lot.id, lot.version, qty)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        configured = client.post("/api/master/products/with-bom", json=jsonable_encoder({
            "product": fields, "bom": {"expected_version": 1, "inventory_mode": "separate",
                "material_mode": "expand_children", "delivery_mode": "components", "components": [
                    {"component_product_id": pid, "quantity_per_set": qty, "inventory_relation": "accompany"}
                    for pid, qty in [(2, 3), (3, 4)]]}}))
        assert configured.status_code == 201, configured.text
        parent = configured.json()["product"]
        assert parent["combination_mode"] == "component_priced"
        parent_id, parent_name = parent["id"], parent["product_name"]
        reopened = client.get(f"/api/master/products/{parent_id}/bom")
        assert reopened.status_code == 200, reopened.text
        assert {row["component_product_id"]: Decimal(str(row["quantity_per_set"]))
                for row in reopened.json()["components"]} == {2: 3, 3: 4}
        other = client.post("/api/orders", json={"customer_id": 1,
            "order_date": beijing_today().isoformat(), "items": [
                {"product_id": pid, "quantity": qty, "unit_price": "1"}
                for pid, qty in [(2, 10), (3, 30)]]})
        assert other.status_code == 201, other.text
        for row in other.json()["items"]:
            pid = row["product_id"]
            lid, version, qty = lots[pid]
            reserved = client.post("/api/warehouse/finished/reservations", json={
                "order_item_id": row["id"], "inventory_lot_id": lid, "quantity": row["quantity"],
                "expected_version": version, "idempotency_key": f"protected-{pid}",
                "warning_acknowledged_codes": [],
            })
            assert reserved.status_code == 200, reserved.text
            with factory() as db:
                lots[pid] = (lid, db.get(InventoryLot, lid).version, qty)
        from app.models.warehouse_inventory import InventoryReservation
        from app.services.multilevel_bom_cutover_review import _row
        protected_ids = [row["id"] for row in other.json()["items"]]
        with factory() as db:
            protected = {row.id: _row(row) for row in db.scalars(select(InventoryReservation)
                         .where(InventoryReservation.order_item_id.in_(protected_ids)))}
        created = client.post("/api/orders", json={"customer_id": 1,
            "order_date": beijing_today().isoformat(), "items": [dict(
                product_id=pid, quantity=qty, unit_price=price,
                combination_mode_snapshot="component_priced", combination_role="priced_component",
                combination_group_key="loose-100", combination_parent_product_id=parent_id,
                combination_parent_name_snapshot=parent_name, combination_set_quantity_snapshot=100,
                combination_quantity_per_set_snapshot=ratio,
            ) for pid, qty, price, ratio in [(2, 300, "1.25", 3), (3, 400, "0.80", 4)]]})
        assert created.status_code == 201, created.text
        items = {row["product_id"]: row["id"] for row in created.json()["items"]}
        requisitions = []
        for pid, iid in items.items():
            lid, version, qty = lots[pid]
            with factory() as db:
                import json
                from app.services.finished_stock_identity import order_product_basis
                actual = json.loads(db.get(InventoryLot, lid).finished_detail.physical_basis_json)
                expected = json.loads(order_product_basis(db, iid, pid))
                assert actual == expected, {key: (actual.get(key), expected.get(key))
                                            for key in actual if actual.get(key) != expected.get(key)}
            reservation_payload = {
                "order_item_id": iid, "inventory_lot_id": lid, "quantity": qty,
                "expected_version": version, "idempotency_key": f"priced-reserve-{pid}",
                "warning_acknowledged_codes": [],
            }
            rejected = client.post("/api/warehouse/finished/reservations", json={
                **reservation_payload, "quantity": qty+1, "idempotency_key": f"excess-{pid}"})
            assert rejected.status_code == 409, rejected.text
            reserved = client.post("/api/warehouse/finished/reservations", json=reservation_payload)
            assert reserved.status_code == 200, reserved.text
            replay = client.post("/api/warehouse/finished/reservations", json=reservation_payload)
            assert replay.status_code == 200, replay.text
            with factory() as db:
                requirements = read_graph_requirements(db, iid)
                assert requirements.plan.materials[0].purchase_sheets == (70 if pid == 2 else 175)
                source = next(row for row in read_compiled_order_bom(db, iid).snapshots
                              if row.component_product_id == pid)
                requisitions.append({**_component_payload(source.id), "order_item_id": iid,
                    "special_process": "一开四" if pid == 2 else "一开二"})
        saved = client.post("/api/requisition/batches", json={"request_key": "priced-loose-material",
            "supplier_name": "苏州纸板供应商", "items": requisitions})
        assert saved.status_code == 201, saved.text
        procurement_hashes = {}
        for pid, iid in items.items():
            preview = client.get(f"/api/orders/items/{iid}/bom-procurement-impact")
            assert preview.status_code == 200, preview.text
            data = preview.json()
            assert data["executable"] is False
            assert len(data["paper"]) == 1
            line = data["paper"][0]
            assert line["unit"] == "张"
            assert line["receipt_summary"]["remaining_quantity"] == (70 if pid == 2 else 175)
            assert line["mappings"][0]["product_id"] == pid
            procurement_hashes[pid] = data["evidence_hash"]
        receipt_ids = []
        for index, source in enumerate(read_purchase_sources(factory, material_id)):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"priced-fact-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            first = source.order_purpose_sheet_qty // 2
            for batch, qty in enumerate([first, source.order_purpose_sheet_qty - first]):
                received = _receive(client, source, fact.json(), quantity=qty,
                                    idempotency_key=f"priced-receipt-{index}-{batch}")
                assert received.status_code == 200, received.text
                receipt_ids.append(received.json()["receipt_item_id"])
                replay = _receive(client, source, fact.json(), quantity=qty,
                                  idempotency_key=f"priced-receipt-{index}-{batch}")
                assert replay.status_code == 200, replay.text
        with factory() as db:
            assert list(db.scalars(select(BomAssembly))) == []
            for pid, iid in items.items():
                from app.models.warehouse_inventory import InventoryReservation
                rows = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == iid)))
                assert sum(row.reserved_stock_quantity-row.consumed_stock_quantity-row.released_stock_quantity
                           for row in rows) == (300 if pid == 2 else 400)
        for pid, iid in items.items():
            preview = client.get(f"/api/orders/items/{iid}/bom-procurement-impact")
            assert preview.status_code == 200, preview.text
            data = preview.json()
            assert data["evidence_hash"] != procurement_hashes[pid]
            assert data["paper"][0]["receipt_summary"]["remaining_quantity"] == 0
            assert len(data["paper"][0]["receipts"]) == 2
            assert data["paper"][0]["frozen_costs"]
        first_delivery = _dispatch(client, 1, [(items[2], 120), (items[3], 100)])
        second = _dispatch(client, 1, [(items[2], 180), (items[3], 300)])
        def actual_dispatch_cost(deliveries):
            from app.services.graph_delivery_cost import graph_cost_report_sources, active_graph_cost
            from app.models.warehouse_inventory import DeliveryInventoryAllocation
            with factory() as db:
                sources = graph_cost_report_sources(db, [line["id"] for delivery in deliveries for line in delivery["items"]])
                return sum((active_graph_cost(fact, portions,
                    db.get(DeliveryInventoryAllocation, allocation_id).consumed_stock_quantity
                    - db.get(DeliveryInventoryAllocation, allocation_id).reversed_stock_quantity)
                    for (_, allocation_id), (fact, portions) in sources.items()), Decimal(0))
        cost_before_returns = actual_dispatch_cost([first_delivery, second])
        assert cost_before_returns > 0
        cancelled = client.put(f"/api/deliveries/{second['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        with factory() as db:
            assert db.get(OrderItem, items[2]).delivered_quantity == 120
            assert db.get(OrderItem, items[3]).delivered_quantity == 100
        cancelled = client.put(f"/api/deliveries/{first_delivery['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        replay = client.put(f"/api/deliveries/{first_delivery['id']}/cancel")
        assert replay.status_code == 409, replay.text
        assert "无需取消" in replay.json()["detail"]
        with factory() as db:
            for pid, iid in items.items():
                assert db.get(OrderItem, iid).delivered_quantity == 0
                rows = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == iid)))
                assert sum(row.reserved_stock_quantity-row.consumed_stock_quantity-row.released_stock_quantity
                           for row in rows) == (300 if pid == 2 else 400)
        if reverse_receipts:
            # Fail after the real audit write: all business facts and the audit
            # must roll back, and the same operation key must remain retryable.
            from app.services import incoming_receipts
            from sqlalchemy import inspect, text

            def database_facts():
                with factory() as db:
                    names = inspect(db.get_bind()).get_table_names()
                    return {name: db.execute(text(f'SELECT * FROM "{name}" ORDER BY 1')).all()
                            for name in names if name != "sqlite_sequence"}

            before_failure = database_facts()
            original_purchase_costs = before_failure["purchase_receipt_facts"]
            assert original_purchase_costs, "Receipt reversal must preserve real frozen purchase cost evidence"
            real_audit = incoming_receipts.append_audit_event

            def fail_after_audit(*args, **kwargs):
                real_audit(*args, **kwargs)
                raise RuntimeError("isolated reversal audit failure")

            with monkeypatch.context() as patch:
                patch.setattr(incoming_receipts, "append_audit_event", fail_after_audit)
                with pytest.raises(RuntimeError, match="isolated reversal audit failure"):
                    client.put(f"/api/incoming/receipt-items/{receipt_ids[-1]}/revert", json={
                        "reason": "隔离验收恢复原余料",
                        "idempotency_key": f"priced-undo-{receipt_ids[-1]}"})
            assert database_facts() == before_failure
            for rid in reversed(receipt_ids):
                body = {"reason": "隔离验收恢复原余料", "idempotency_key": f"priced-undo-{rid}"}
                undone = client.put(f"/api/incoming/receipt-items/{rid}/revert", json=body)
                assert undone.status_code == 200, undone.text
                replay = client.put(f"/api/incoming/receipt-items/{rid}/revert", json=body)
                assert replay.status_code == 200 and replay.json() == undone.json(), replay.text
            after_reversal = database_facts()
            assert after_reversal["purchase_receipt_facts"] == original_purchase_costs
            for name in ("finance_delivery_graph_cost_facts", "finance_delivery_graph_cost_portions"):
                assert after_reversal[name] == before_failure[name]
            with factory() as db:
                for pid, (lid, _, qty) in lots.items():
                    lot = db.get(InventoryLot, lid)
                    assert lot.quantity_reserved == qty + (10 if pid == 2 else 30)
                    assert lot.quantity_consumed == 0
                    requirements = read_graph_requirements(db, items[pid])
                    assert requirements.plan.materials[0].purchase_sheets == (70 if pid == 2 else 175)
                assert {row.id: _row(row) for row in db.scalars(select(InventoryReservation)
                        .where(InventoryReservation.order_item_id.in_(protected_ids)))} == protected
            return
        first_delivery = _dispatch(client, 1, [(items[2], 120), (items[3], 100)])
        return_locations = None
        return_versions = {}
        if short_received:
            from app.services.ordered_finished_receipt_return import _return_location_candidates
            with factory() as db:
                candidates = _return_location_candidates(db)
                free = list(candidates)
                assert len(free) >= 2
                return_locations = {items[2]: free[0], items[3]: free[1]}
                return_versions = {iid: candidates[lid].floor3_layout.version for iid, lid in return_locations.items()}
        if short_received:
            from app.services import bom_return_cost
            from app.services.bom_subkits import SubkitError
            from sqlalchemy import inspect, text
            def return_facts():
                with factory() as db:
                    return {name: db.execute(text(f'SELECT * FROM "{name}" ORDER BY 1')).all()
                            for name in inspect(db.get_bind()).get_table_names() if name != "sqlite_sequence"}
            original_facts = return_facts()
            freeze = bom_return_cost.freeze_return_graph_cost
            def fail_after_cost(*args, **kwargs):
                freeze(*args, **kwargs)
                raise SubkitError("模拟退回成本写入失败")
            with monkeypatch.context() as patch:
                patch.setattr(bom_return_cost, "freeze_return_graph_cost", fail_after_cost)
                with pytest.raises(AssertionError, match="模拟退回成本写入失败"):
                    _confirm_receipt(client, first_delivery, short_received=short_received,
                        return_location_id=return_locations, return_layout_versions=return_versions)
            assert return_facts() == original_facts
        receipt = _confirm_receipt(client, first_delivery, short_received=short_received,
                         return_location_id=return_locations, return_layout_versions=return_versions)
        if short_received:
            cancelled = client.post(f"/api/finance/return_receipts/{receipt['id']}/cancel")
            assert cancelled.status_code == 200, cancelled.text
            reopened = client.put(f"/api/finance/return_receipts/{receipt['id']}", json={
                "actual_received_date": beijing_today().isoformat(), "items": [dict(
                    delivery_item_id=line["id"], actual_received_quantity=line["delivered_quantity"]-short_received,
                    resolution_action="continue_delivery", difference_reason="隔离验收重新确认短收",
                    return_location_id=return_locations[line["order_item_id"]],
                    expected_return_layout_version=return_versions[line["order_item_id"]])
                    for line in first_delivery["items"]]})
            assert reopened.status_code == 200, reopened.text
        second = _dispatch(client, 1, [(items[2], 180), (items[3], 300)])
        _confirm_receipt(client, second)
        statement = client.post("/api/finance/statements", json={"customer_id": 1,
            "statement_month": beijing_today().strftime("%Y-%m"),
            "delivery_ids": [first_delivery["id"], second["id"]]})
        assert statement.status_code == 201, statement.text
        with factory() as db:
            assert db.get(Statement, statement.json()["id"]).total_receivable == Decimal("695.00")-short_received*Decimal("2.05")
            assert {row.id: _row(row) for row in db.scalars(select(InventoryReservation)
                    .where(InventoryReservation.order_item_id.in_(protected_ids)))} == protected
        if short_received:
            from app.services.finished_stock_identity import order_product_basis
            from app.services.multilevel_bom_cost_lineage import graph_material_sources
            with factory() as db:
                for pid, iid in items.items():
                    returned = list(db.scalars(select(InventoryLot).where(
                        InventoryLot.warehouse_location_id == return_locations[iid],
                        InventoryLot.source_ref_type == "return_receipt_item")))
                    assert sum(lot.quantity_reserved for lot in returned) == short_received
                    for lot in returned:
                        assert lot.finished_detail.physical_basis_json == order_product_basis(db, iid, pid)
                        assert graph_material_sources(db, lot), "Actual receipt cost lineage must survive customer return"
                        import json
                        original_detail = lot.cost_snapshot_detail_json
                        forged = json.loads(original_detail)
                        forged["bom_return_cost"]["offset"] += 1
                        lot.cost_snapshot_detail_json = json.dumps(forged)
                        with pytest.raises(SubkitError, match="成本身份不一致"):
                            graph_material_sources(db, lot)
                        lot.cost_snapshot_detail_json = original_detail
            replacement = _dispatch(client, 1, [(items[2], short_received), (items[3], short_received)])
            _confirm_receipt(client, replacement)
            assert actual_dispatch_cost([first_delivery, second, replacement]) == cost_before_returns
            replacement_statement = client.post("/api/finance/statements", json={"customer_id": 1,
                "statement_month": beijing_today().strftime("%Y-%m"), "delivery_ids": [replacement["id"]]})
            assert replacement_statement.status_code == 201, replacement_statement.text
            with factory() as db:
                assert db.get(Statement, replacement_statement.json()["id"]).total_receivable == short_received*Decimal("2.05")
                assert db.get(OrderItem, items[2]).delivered_quantity == 300
                assert db.get(OrderItem, items[3]).delivered_quantity == 400
