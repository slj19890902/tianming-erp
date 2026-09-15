"""Real HTTP receipt posting against a disposable anonymous map/database."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, delete

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login, _component_payload
from tests.test_p1_81_receipt_purpose_flow import (
    _p181_published_map_identity, _seed_material_and_staging, FrozenSource, _freeze_receipt_fact, _receive as _receive_cut_parts,
)
from tests.test_multilevel_bom_master import save


def _receive(client, *args, **kwargs):
    """Legacy end-to-end scenarios now explicitly confirm physical assembly.

    Receipt-only assertions live in test_bom_confirmation396, which calls the
    original receipt helper and verifies no assembly before this separate POST.
    """
    result = _receive_cut_parts(client, *args, **kwargs)
    if result.status_code == 200:
        import hashlib, json
        plans = client.get('/api/production/pending-assemblies')
        assert plans.status_code == 200, plans.text
        for row in plans.json()['items']:
            if row.get('source_kind') == 'stock' or not any(row.get('expected_outputs', {}).values()):
                continue
            assert not row.get('error'), row
            payload = dict(source_lot_versions=row['source_lot_versions'], available_lot_ids=row['available_lot_ids'],
                expected_outputs=row['expected_outputs'], target_locations={o['product_id']:o['location_id'] for o in row['outputs']},
                physical_assembly_confirmed=True)
            payload['operation_key']='fixture-physical-'+hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:40]
            confirmed=client.post(f"/api/production/assemblies/{row['order_item_id']}/confirm",json=payload)
            assert confirmed.status_code==200,confirmed.text
    return result


def _reverse_physical_assemblies(client):
    rows=client.get('/api/production/completions',params={'include_stock':True,'page':1,'page_size':200}).json()['items']
    for row in rows:
        if row.get('origin')=='bom_assembly' and row['status']=='posted':
            result=client.post(f"/api/production/assemblies/{row['bom_assembly_id']}/reverse",json={'confirm_reverse':True})
            assert result.status_code==200,result.text


@pytest.mark.parametrize("liner", [False, True])
def test_graph_receipt_delivery_api_dispatch_and_cancel(composite_requisition_app, _p181_published_map_identity, liner, monkeypatch):
    from app.core.time_contract import beijing_today
    from app.api.deliveries import router
    from app.models.warehouse_inventory import InventoryLot
    from app.models.order import OrderItem
    app, factory = composite_requisition_app
    app.include_router(router, prefix="/api/deliveries")
    material_id, snapshots = seed_graph(factory, liner=liner)
    with TestClient(app) as client:
        _login(client)
        for index, source in enumerate(purchase_sources(client, factory, material_id, snapshots)):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"api-price-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            receipt = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
                               idempotency_key=f"api-receipt-{index}")
            assert receipt.status_code == 200, receipt.text
        pending = client.get("/api/deliveries/pending_items")
        from app.services.multilevel_bom_cost_lineage import graph_material_sources, graph_material_cost_slice
        with factory() as db:
            for lot in db.scalars(select(InventoryLot).where(InventoryLot.quantity_reserved > 0)):
                evidence = graph_material_sources(db, lot)
                assert evidence is not None
                pid = lot.finished_detail.product_id
                expected = Decimal("9.8720") if pid == 4 else Decimal("1.2340") if liner else Decimal("8.6380")
                assert sum(row["amount"] for row in evidence) == expected
                assert len({row["purchase_receipt_fact_id"] for row in evidence}) == (1 if liner and pid == 1 else 2)
                first = graph_material_cost_slice(db, lot, used=0, take=4)
                second = graph_material_cost_slice(db, lot, used=4, take=6)
                assert sum(r["amount"] for r in first + second) == expected
                assert [a["amount"]+b["amount"] for a, b in zip(first, second)] == [r["amount"] for r in evidence]
                from app.services.bom_subkits import SubkitError
                with pytest.raises(SubkitError):
                    graph_material_cost_slice(db, lot, used=4, take=7)
            from app.models.product_bom import SalesOrderItemBomComponent
            bad_snapshot = db.scalar(select(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.component_product_id == 2))
            bad_snapshot.component_product_id = 1
            with pytest.raises(SubkitError, match="产品或客户不一致"):
                assembled = next(l for l in db.scalars(select(InventoryLot)) if l.source_ref_type == "bom_assembly")
                graph_material_sources(db, assembled)
            db.rollback()
        assert pending.status_code == 200, pending.text
        pending_line = next(row for row in pending.json()["items"] if row["order_item_id"] == 1)
        assert pending_line["remaining_quantity"] == 10
        expected_products = {1, 4} if liner else {1}
        assert {row["component_product_id"] for row in pending_line["component_lines"]} == expected_products
        assert len(pending_line["inventory_sources"]) == len(expected_products)
        assert all(row["location_id"] and row["quantity_to_pick_stock"] == 10 for row in pending_line["inventory_sources"])
        created = client.post("/api/deliveries", json={"customer_id": 1, "delivery_date": beijing_today().isoformat(),
            "items": [{"order_item_id": 1, "delivered_quantity": 4}]})
        assert created.status_code == 201, created.text
        did = created.json()["id"]
        from app.services import graph_delivery_cost
        from app.models.graph_material_cost import FinanceDeliveryGraphCostFact, FinanceDeliveryGraphCostPortion
        from app.models.delivery import Delivery
        with monkeypatch.context() as patch:
            def fail_portion(**kwargs):
                raise SubkitError("模拟成本明细写入失败")
            patch.setattr(graph_delivery_cost, "Portion", fail_portion)
            failed = client.put(f"/api/deliveries/{did}/dispatch")
            assert failed.status_code == 409, failed.text
        with factory() as db:
            assert db.get(Delivery, did).status == "pending"
            assert db.get(OrderItem, 1).delivered_quantity == 0
            assert list(db.scalars(select(FinanceDeliveryGraphCostFact))) == []
        dispatched = client.put(f"/api/deliveries/{did}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        detail = client.get(f"/api/deliveries/{did}")
        assert detail.status_code == 200, detail.text
        detail_line = detail.json()["items"][0]
        assert {row["component_product_id"] for row in detail_line["component_lines"]} == expected_products
        assert len(detail_line["inventory_sources"]) == len(expected_products)
        assert all(row["location_id"] and row["quantity_to_pick_stock"] == 4 for row in detail_line["inventory_sources"])
        def check(expected):
            with factory() as db:
                assert db.get(OrderItem, 1).delivered_quantity == expected
                for pid in ([1, 4] if liner else [1]):
                    lots = [lot for lot in db.scalars(select(InventoryLot)) if lot.finished_detail and lot.finished_detail.product_id == pid]
                    assert sum(lot.quantity_consumed for lot in lots) == expected
                    assert sum(lot.quantity_reserved for lot in lots) == 10-expected
        check(4)
        from app.services.material_cost_lineage import material_cost_coverage_report
        with factory() as db:
            report = material_cost_coverage_report(db, month=beijing_today().strftime("%Y-%m"))
            assert report["covered_delivery_lines"] == 1, report
            assert report["actual_material_cost"] == Decimal("4.44" if liner else "3.46")
            from app.services.customer_delivery_margin import build_customer_delivery_margin
            margin = build_customer_delivery_margin(
                db, date_from=beijing_today(), date_to=beijing_today(),
                customer_id=1, visible_customer_ids=None,
            )
            assert margin["summary"]["delivery_line_count"] == 1
            assert margin["summary"]["unknown_unit_quantity"] == 0
            cost_facts = list(db.scalars(select(FinanceDeliveryGraphCostFact)))
            assert len(cost_facts) == len(expected_products)
            frozen_ids = [f.id for f in cost_facts]
            assert len(list(db.scalars(select(FinanceDeliveryGraphCostPortion)))) == (3 if liner else 2)
            from app.models.warehouse_inventory import DeliveryInventoryAllocation
            for f in cost_facts:
                allocation = db.get(DeliveryInventoryAllocation, f.delivery_inventory_allocation_id)
                replay = graph_delivery_cost.freeze_graph_delivery_cost(db, allocation=allocation,
                    lot=db.get(InventoryLot, f.inventory_lot_id), operator_id=1)
                assert replay.id == f.id
        cancelled = client.put(f"/api/deliveries/{did}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        check(0)
        with factory() as db:
            assert material_cost_coverage_report(db, month=beijing_today().strftime("%Y-%m"))["actual_material_cost"] == 0
            assert [f.id for f in db.scalars(select(FinanceDeliveryGraphCostFact))] == frozen_ids


@pytest.mark.parametrize("liner", [False, True])
def test_delivery_requires_only_real_pick_products(composite_requisition_app, _p181_published_map_identity, liner):
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.services.composite_bom_workflow import delivery_component_required_quantities
    _, factory = composite_requisition_app
    seed_graph(factory, liner=liner)
    with factory() as db:
        requested = delivery_component_required_quantities(db, order_item_id=1, delivery_sets=4)
        by_product = {db.get(SalesOrderItemBomComponent, sid).component_product_id: qty for sid, qty in requested.items()}
        assert by_product == ({1: 4, 4: 4} if liner else {1: 4})
        from app.services.composite_bom_workflow import kit_availability, kit_available_sets_by_order_item_ids, delivery_component_demands
        from tests.test_multilevel_bom_requisition import reserve
        from app.models.order import OrderItem
        for sid in requested:
            reserve(db, db.get(OrderItem, 1), db.get(SalesOrderItemBomComponent, sid), 10)
        available = kit_availability(db, 1)
        assert available["available_sets"] == 10
        assert len(available["components"]) == (2 if liner else 1)
        assert kit_available_sets_by_order_item_ids(db, [1]) == {1: 10}
        assert [d.component_product_id for d in delivery_component_demands(db, 1) if d.show_on_delivery] == [1]


@pytest.mark.parametrize("move_before_assembly", [True, False])
def test_split_movement_keeps_receipt_cost_and_finished_coverage(
    composite_requisition_app, _p181_published_map_identity, move_before_assembly
):
    from app.models.multilevel_bom import BomAssembly
    from app.models.order import OrderItem
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
    from app.services.warehouse_inventory import transfer_finished_lot_between_locations
    from app.services.multilevel_bom_receipts import refresh_graph_main_task
    app, factory = composite_requisition_app
    material_id, snapshots = seed_graph(factory)
    with TestClient(app) as client:
        _login(client)
        sources = purchase_sources(client, factory, material_id, snapshots)
        for index, source in enumerate(sources):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"move-price-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            response = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
                                idempotency_key=f"move-receipt-{index}")
            assert response.status_code == 200, response.text
            if (move_before_assembly and index == 0) or (not move_before_assembly and index == 1):
                with factory() as db:
                    if move_before_assembly:
                        lot_id = db.scalar(select(ProductionCompletion.inventory_lot_id))
                    else:
                        lot_id = db.scalar(select(BomAssembly.output_lot_id).where(BomAssembly.quantity > 0))
                    lot = db.get(InventoryLot, lot_id)
                    target = db.scalar(select(WarehouseLocation).where(WarehouseLocation.area_code == "FIN-001",
                        WarehouseLocation.source_version == "TWIN_V1",
                        WarehouseLocation.id != lot.warehouse_location_id).order_by(WarehouseLocation.id))
                    try:
                        moved = transfer_finished_lot_between_locations(db, lot_id=lot.id, expected_version=lot.version,
                            quantity=15 if move_before_assembly else 4, location_id=target.id,
                            expected_target_layout_version=target.floor3_layout.version,
                            operator_id=1, idempotency_key=f"graph-move-{index}")
                    except ValueError as error:
                        origin = db.get(WarehouseLocation, lot.warehouse_location_id)
                        pytest.fail(f"{error}: {origin.location_code}/{origin.placement_status} -> {target.location_code}/{target.placement_status}")
                    if move_before_assembly:
                        from app.services.bom_subkit_costs import source_cost
                        from app.services.bom_subkits import SubkitError
                        target_lot = moved.target_lot
                        original_cost = target_lot.cost_snapshot_detail_json
                        assert source_cost(db, target_lot, 15)[0] == Decimal("1.8510")
                        target_lot.cost_snapshot_detail_json = "{}"
                        with pytest.raises(SubkitError, match="成本身份"):
                            source_cost(db, target_lot, 15)
                        target_lot.cost_snapshot_detail_json = original_cost
                    db.commit()
        with factory() as db:
            assemblies = list(db.scalars(select(BomAssembly).where(BomAssembly.status == "posted")))
            assert sum(a.quantity for a in assemblies) == 10
            assert sum(a.total_cost for a in assemblies) == Decimal("8.6380")
            task = refresh_graph_main_task(db, db.get(OrderItem, 1), create_if_missing=False)
            assert task.finished_coverage_snapshot == 10
            assert task.status == "completed"
        if not move_before_assembly:
            blocked = client.put(f"/api/incoming/receipt-items/{response.json()['receipt_item_id']}/revert", json={})
            assert blocked.status_code == 409, blocked.text
            with factory() as db:
                active = list(db.scalars(select(BomAssembly).where(BomAssembly.status == "posted")))
                assert sum(a.quantity for a in active) == 10


def seed_graph(factory, *, liner=False, a3=False, splice=False, body=False, separate=False, quantity=None, cutting_modes=None, finished_slot_count=8, accompany=False):
    from app.models.warehouse_inventory import WarehouseLocation
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.supplier import Supplier
    from app.models.order import OrderItem
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity
    from app.services.multilevel_bom_orders import freeze_master_order_bom
    with factory() as db:
        db.get(WarehouseLocation, 1).location_code = "GRAPH-OLD-FIXTURE"
        db.commit()
    material_id = _seed_material_and_staging(factory, finished_slot_count=finished_slot_count)
    with factory() as db:
        db.execute(delete(SalesOrderItemBomComponent))
        db.add(Supplier(standard_name="苏州纸板供应商", normalized_name=normalize_supplier_identity("苏州纸板供应商"),
            display_name="苏州纸板供应商", sort_order=20, is_active=True, version=1))
        actor = db.get(User, 1)
        item = db.get(OrderItem, 1)
        if quantity is not None:
            item.quantity = quantity
        item.composite_fulfillment_mode_snapshot = "parent_delivery"
        for pid in (1, 2, 3):
            p = db.get(Product, pid)
            p.material_id = material_id
            p.box_style = "隔板"
            p.report_length_mm = 1000
            p.report_width_mm = 700
            p.pieces_per_box = 1
            p.default_cutting_mode = "一开一"
            if accompany:
                p.length_mm, p.width_mm, p.height_mm = 1000, 700, 20
            if cutting_modes and pid in cutting_modes:
                p.default_cutting_mode = cutting_modes[pid]
        if separate:
            from app.services.composite_bom import replace_product_bom
            replace_product_bom(db, parent_product_id=1, expected_version=db.get(Product, 1).version,
                user=actor, inventory_mode="separate", material_mode="expand_children", delivery_mode="components",
                components=[dict(component_product_id=2, quantity_per_set=3, inventory_relation="accompany"),
                            dict(component_product_id=3, quantity_per_set=4, inventory_relation="accompany")])
            item.composite_fulfillment_mode_snapshot = "component_delivery"
        elif accompany:
            save(db, actor, 1, "manufactured", [(2, 4, "accompany")])
        elif liner:
            kit = Product(customer_id=1, product_code="LINER", customer_material_code="LINER",
                          product_name="真实内衬", unit="套")
            db.add(kit)
            db.flush()
            save(db, actor, kit.id, "assembled", [(2, 2, "assembly"), (3, 6, "assembly")])
            save(db, actor, 1, "manufactured", [(kit.id, 1, "assembly" if body else "accompany")])
        else:
            save(db, actor, 1, "assembled", [(2, 3, "assembly"), (3, 4, "assembly")])
        if a3:
            p = db.get(Product, 2)
            p.box_style = "A3 天地盖"
            p.base_report_length_mm = 900
            p.base_report_width_mm = 600
        if splice:
            p = db.get(Product, 2)
            p.pieces_per_box = 2
            p.default_cutting_mode = "一开四"
        compiled = freeze_master_order_bom(db, order_item_id=1, actor=actor)
        db.commit()
        snapshots = [(s.id, s.component_product_id) for s in compiled.snapshots
                     if next(n.source for n in compiled.graph.nodes if n.product_id == s.component_product_id) == "manufactured"]
    return material_id, snapshots


def purchase_sources(client, factory, material_id, snapshots, *, a3=False, splice=False, order_item_id=1):
    items = []
    for sid, pid in snapshots:
        for route in (["cover", "base"] if a3 and pid == 2 else ["whole"]):
            items.append({**_component_payload(sid), "component_type": route,
                          "order_item_id": order_item_id,
                          "special_process": "一开四" if splice and pid == 2 else "一开一"})
    saved = client.post("/api/requisition/batches", json={"request_key": "graph-requisition",
        "supplier_name": "苏州纸板供应商", "items": items})
    assert saved.status_code == 201, saved.text
    return read_purchase_sources(factory, material_id)


def read_purchase_sources(factory, material_id):
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.requisition import RequisitionItem
    with factory() as db:
        result = []
        for snapshot in db.scalars(select(PurchasePurposeSourceSnapshot).order_by(PurchasePurposeSourceSnapshot.id)):
            bom = db.get(RequisitionItemBomSource, snapshot.source_bom_requisition_source_id)
            source = db.get(RequisitionItem, bom.requisition_item_id)
            result.append(FrozenSource(source_key=snapshot.source_key, route_key=f"r{source.id}",
                supplier_item_id=source.id, source_version=getattr(source, "version", 1),
                purpose_snapshot_id=snapshot.id, purpose_snapshot_version=snapshot.snapshot_version,
                receipt_plan_fingerprint=snapshot.preview_fingerprint, component_type=snapshot.component_type,
                material_id=material_id, order_purpose_sheet_qty=snapshot.order_purpose_sheet_qty,
                reserve_purpose_sheet_qty=snapshot.reserve_purpose_sheet_qty))
        return result


@pytest.mark.parametrize("liner", [False, True])
def test_real_receipts_create_nodes_then_sets_not_flat_children(composite_requisition_app, _p181_published_map_identity, liner):
    from app.models.multilevel_bom import BomAssembly
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import InventoryLot
    app, factory = composite_requisition_app
    material_id, snapshots = seed_graph(factory, liner=liner)
    with TestClient(app) as client:
        _login(client)
        sources = purchase_sources(client, factory, material_id, snapshots)
        receipt_ids = []
        for i, source in enumerate(sources):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"graph-price-{i}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            received = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
                                idempotency_key=f"graph-in-{i}")
            assert received.status_code == 200, received.text
            receipt_ids.append(received.json()["receipt_item_id"])
            replay = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
                              idempotency_key=f"graph-in-{i}")
            assert replay.status_code == 200, replay.text
        with factory() as db:
            assemblies = list(db.scalars(select(BomAssembly).where(BomAssembly.status == "posted")))
            assert sum(r.quantity for r in assemblies) == 10
            kit = db.get(InventoryLot, next(r.output_lot_id for r in assemblies if r.quantity))
            assert kit.finished_detail.product_id == (4 if liner else 1)
            assert kit.quantity_available == 0
            assert kit.quantity_reserved == 10
            from app.services.composite_bom_workflow import kit_availability
            assert kit_availability(db, 1)["available_sets"] == 10
            completed = list(db.scalars(select(ProductionCompletion)))
            products = {db.get(InventoryLot, c.inventory_lot_id).finished_detail.product_id: c.quantity for c in completed}
            assert products == ({1: 10, 2: 20, 3: 60} if liner else {2: 30, 3: 40})
            assert sum(r.total_cost for r in assemblies) > Decimal("0")
            from app.models.production import ProductionTask
            from app.models.order import OrderItem
            main = db.scalar(select(ProductionTask).where(ProductionTask.sales_order_item_bom_component_id.is_(None)))
            assert main.status == "completed"
            assert main.finished_coverage_snapshot == 10
            assert len(list(db.scalars(select(ProductionTask)))) == 3
            assert db.get(OrderItem, 1).material_status == "received"
            from app.services.multilevel_bom_requirements import read_graph_requirements
            # New output is already paid for by this order's requisitions;
            # do not count it again as prior-stock procurement credit.
            assert not any(read_graph_requirements(db, 1).finished_units.values())
            from app.services.receipt_managed_production import receipt_purpose_summaries_by_order_item_ids
            summary = receipt_purpose_summaries_by_order_item_ids(db, [1])[1]
            assert summary["automatic_finished_output_qty"] == 10
            assert summary["automatic_order_reserved_quantity"] == 10
            assert summary["automatic_surplus_finished_quantity"] == 0
            assert summary["current_theoretical_finished_capacity_qty"] == 10
            assert summary["projection_inconsistent"] is False
            assert summary["product_output_quantities"] == ({1: 10, 2: 20, 3: 60, 4: 10} if liner else {1: 10, 2: 30, 3: 40})
        with factory() as db:
            from app.services.bom_transactions import atomic_bom
            from tests.test_composite_component_delivery_quantities import _delivery
            from app.services.composite_bom_workflow import (
                execute_delivery_component_consumption, reverse_delivery_component_allocations,
                delivery_item_component_quantities, kit_availability,
            )
            from app.models.product_bom import SalesOrderItemBomComponent
            with atomic_bom(db):
                _, line = _delivery(db, customer_id=1, order_item_id=1, number="GRAPH-DISPATCH", quantity=4)
                execute_delivery_component_consumption(db, delivery_item_id=line.id, delivery_sets=4,
                    operator_id=1, operation_key="graph-dispatch")
                actual = delivery_item_component_quantities(db, line.id)
                assert {db.get(SalesOrderItemBomComponent, sid).component_product_id: qty for sid, qty in actual.items()} == ({1: 4, 4: 4} if liner else {1: 4})
                assert execute_delivery_component_consumption(db, delivery_item_id=line.id, delivery_sets=4,
                    operator_id=1, operation_key="graph-dispatch") == []
                reverse_delivery_component_allocations(db, delivery_item_id=line.id, operator_id=1,
                    operation_key="graph-dispatch-reverse")
                assert all(q == 0 for q in delivery_item_component_quantities(db, line.id).values())
                assert kit_availability(db, 1)["available_sets"] == 10
            # This tests the inventory transaction, not the delivery API status
            # transitions. Leave the separate receipt-reversal scenario intact.
            db.rollback()
        _reverse_physical_assemblies(client)
        for rid in reversed(receipt_ids):
            reverted = client.put(f"/api/incoming/receipt-items/{rid}/revert", json={})
            assert reverted.status_code == 200, reverted.text
        with factory() as db:
            assert all(r.status == "reversed" for r in db.scalars(select(BomAssembly)))
            assert all(r.status == "reversed" for r in db.scalars(select(ProductionCompletion)))


def test_a3_partial_route_cost_remains_with_unused_material(composite_requisition_app, _p181_published_map_identity):
    import json
    from app.models.production import ProductionCompletion, ProductionTask
    from app.models.warehouse_inventory import InventoryLot
    from app.models.multilevel_bom import BomAssembly
    app, factory = composite_requisition_app
    material_id, snapshots = seed_graph(factory, a3=True)
    with TestClient(app) as client:
        _login(client)
        sources = purchase_sources(client, factory, material_id, snapshots, a3=True)
        assert len(sources) == 3
        facts = [_freeze_receipt_fact(client, s, idempotency_key=f"a3-price-{i}", unit_price="0.1234") for i, s in enumerate(sources)]
        assert all(f.status_code == 200 for f in facts)
        # Thirty lids, half the bases, then the other component, then bases.
        for sequence, (index, qty) in enumerate(((0, 30), (1, 15), (2, 40), (1, 15))):
            result = _receive(client, sources[index], facts[index].json(), quantity=qty,
                              idempotency_key=f"a3-in-{sequence}")
            assert result.status_code == 200, result.text
            with factory() as db:
                rows = db.scalars(select(ProductionCompletion).join(ProductionTask).where(
                    ProductionTask.sales_order_item_bom_component_id == snapshots[0][0])).all()
                if index == 0:
                    assert not rows  # A lid is not one complete A3 product.
                for row in rows:
                    lot = db.get(InventoryLot, row.inventory_lot_id)
                    assert row.quantity == 15
                    detail = json.loads(lot.cost_snapshot_detail_json)
                    assert Decimal(detail["capitalized_material_cost"]) == Decimal("3.7020")
        with factory() as db:
            assert sum(r.quantity for r in db.scalars(select(BomAssembly))) == 10


def test_component_semi_stock_used_once_and_restored_on_receipt_reversal(composite_requisition_app, _p181_published_map_identity):
    from tests.test_p1_81_receipt_purpose_flow import _seed_order_semi_reservation
    from app.models.warehouse_inventory import InventoryReservation, InventoryLot, OrderItemSemiRequirement, SemiFinishedLotAllowedProduct
    from app.models.production import ProductionCompletion
    app, factory = composite_requisition_app
    material_id, snapshots = seed_graph(factory)
    _seed_order_semi_reservation(factory, credited_piece_quantity=6, pieces_per_box=1)
    with factory() as db:
        reservation = db.scalar(select(InventoryReservation))
        reservation.sales_order_item_bom_component_id = snapshots[0][0]
        req = db.get(OrderItemSemiRequirement, reservation.semi_requirement_id)
        req.sales_order_item_bom_component_id = snapshots[0][0]
        req.required_piece_quantity = 30
        req.board_length_mm, req.board_width_mm = 1000, 700
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        lot.estimated_unit_cost_snapshot = Decimal("0.5")
        lot.semi_finished_detail.board_length_mm, lot.semi_finished_detail.board_width_mm = 1000, 700
        allowed = db.scalar(select(SemiFinishedLotAllowedProduct))
        allowed.product_id = 2
        lot_id, reservation_id = lot.id, reservation.id
        db.commit()
    with TestClient(app) as client:
        _login(client)
        sources = purchase_sources(client, factory, material_id, snapshots)
        source = sources[0]
        assert source.order_purpose_sheet_qty == 24
        fact = _freeze_receipt_fact(client, source, idempotency_key="semi-graph-price", unit_price="0.1234")
        assert fact.status_code == 200, fact.text
        receipt_ids = []
        for i in range(2):
            result = _receive(client, source, fact.json(), quantity=12, idempotency_key=f"semi-graph-in-{i}")
            assert result.status_code == 200, result.text
            receipt_ids.append(result.json()["receipt_item_id"])
        with factory() as db:
            assert [c.quantity for c in db.scalars(select(ProductionCompletion).order_by(ProductionCompletion.id))] == [18, 12]
            assert db.get(InventoryLot, lot_id).quantity_consumed == 6
            assert db.get(InventoryReservation, reservation_id).consumed_stock_quantity == 6
        _reverse_physical_assemblies(client)
        for rid in reversed(receipt_ids):
            result = client.put(f"/api/incoming/receipt-items/{rid}/revert", json={})
            assert result.status_code == 200, result.text
        with factory() as db:
            assert db.get(InventoryLot, lot_id).quantity_reserved == 6
            assert db.get(InventoryLot, lot_id).quantity_consumed == 0


def test_failure_after_component_posting_rolls_back_entire_receipt(composite_requisition_app, _p181_published_map_identity, monkeypatch):
    from app.services import multilevel_bom_receipts as service
    from app.services.bom_subkits import SubkitError
    from app.models.production import ProductionCompletion
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    from app.models.warehouse_inventory import InventoryLot
    app, factory = composite_requisition_app
    material_id, snapshots = seed_graph(factory)
    def fail(*args, **kwargs):
        raise SubkitError("注入组件入库后组套失败")
    with TestClient(app) as client:
        _login(client)
        source = purchase_sources(client, factory, material_id, snapshots)[0]
        fact = _freeze_receipt_fact(client, source, idempotency_key="fault-graph-price", unit_price="0.1234")
        assert fact.status_code == 200, fact.text
        monkeypatch.setattr(service, "assemble_graph_receipt", fail)
        result = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty, idempotency_key="fault-graph-in")
        assert result.status_code == 409, result.text
        with factory() as db:
            for model in (ProductionCompletion, IncomingReceiptItem, IncomingReceiptPurposeAllocation, InventoryLot):
                assert db.scalar(select(model)) is None


def test_cut_yield_and_splice_each_applied_once_on_real_receipt(composite_requisition_app, _p181_published_map_identity):
    from app.models.production import ProductionCompletion
    from app.models.multilevel_bom import BomAssembly
    app, factory = composite_requisition_app
    material_id, snapshots = seed_graph(factory, splice=True)
    with TestClient(app) as client:
        _login(client)
        sources = purchase_sources(client, factory, material_id, snapshots, splice=True)
        assert sources[0].order_purpose_sheet_qty == 15  # 30 products x2 pieces /4 per sheet.
        fact = _freeze_receipt_fact(client, sources[0], idempotency_key="splice-price", unit_price="0.1234")
        assert fact.status_code == 200, fact.text
        for i, qty in enumerate((1, 14)):
            received = _receive(client, sources[0], fact.json(), quantity=qty, idempotency_key=f"splice-in-{i}")
            assert received.status_code == 200, received.text
        with factory() as db:
            assert [c.quantity for c in db.scalars(select(ProductionCompletion).order_by(ProductionCompletion.id))] == [2, 28]
            assert sum(a.quantity for a in db.scalars(select(BomAssembly))) == 0


def test_a3_reserved_cover_consumption_can_unwind_partial_batches(composite_requisition_app, _p181_published_map_identity):
    from tests.test_p1_81_receipt_purpose_flow import _seed_order_semi_reservation
    from app.models.warehouse_inventory import InventoryReservation, InventoryLot, OrderItemSemiRequirement, SemiFinishedLotAllowedProduct
    app, factory = composite_requisition_app
    material_id, snapshots = seed_graph(factory, a3=True)
    _seed_order_semi_reservation(factory, credited_piece_quantity=20, pieces_per_box=1)
    with factory() as db:
        reservation = db.scalar(select(InventoryReservation))
        reservation.sales_order_item_bom_component_id = snapshots[0][0]
        req = db.get(OrderItemSemiRequirement, reservation.semi_requirement_id)
        req.sales_order_item_bom_component_id = snapshots[0][0]
        req.component_type = "cover"
        req.required_piece_quantity = 30
        req.board_length_mm, req.board_width_mm = 1000, 700
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        lot.estimated_unit_cost_snapshot = Decimal("0.5")
        lot.semi_finished_detail.board_length_mm, lot.semi_finished_detail.board_width_mm = 1000, 700
        lot.semi_finished_detail.component_type = "cover"
        db.scalar(select(SemiFinishedLotAllowedProduct)).product_id = 2
        reservation_id = reservation.id
        db.commit()
    with TestClient(app) as client:
        _login(client)
        sources = purchase_sources(client, factory, material_id, snapshots, a3=True)
        assert sources[0].order_purpose_sheet_qty == 10
        source = sources[1]  # Bases complete only the matching number of covers.
        fact = _freeze_receipt_fact(client, source, idempotency_key="a3-semi-price", unit_price="0.1234")
        assert fact.status_code == 200, fact.text
        ids = []
        for i in range(2):
            result = _receive(client, source, fact.json(), quantity=5, idempotency_key=f"a3-semi-in-{i}")
            assert result.status_code == 200, result.text
            ids.append(result.json()["receipt_item_id"])
        _reverse_physical_assemblies(client)
        for rid, remaining in zip(reversed(ids), (5, 0)):
            result = client.put(f"/api/incoming/receipt-items/{rid}/revert", json={})
            assert result.status_code == 200, result.text
            assert result.json()["purpose_reversal"]["theoretical_finished_cumulative"] == remaining
            with factory() as db:
                assert db.get(InventoryReservation, reservation_id).consumed_stock_quantity == remaining
                from app.services.receipt_managed_production import receipt_purpose_summaries_by_order_item_ids
                projection = receipt_purpose_summaries_by_order_item_ids(db, [1])[1]
                assert projection["automatic_finished_output_qty"] == 0
                assert projection["product_output_quantities"][2] == remaining
                assert projection["projection_inconsistent"] is False
