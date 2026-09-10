import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.models.multilevel_bom import BomAssembly
from app.models.warehouse_inventory import InventoryLot
from app.services.composite_bom_workflow import delivery_component_demands
from app.services.multilevel_bom_requirements import read_graph_requirements
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact, _receive
from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources


@pytest.mark.parametrize("separate", [True, False])
def test_loose_children_are_reserved_individually_before_sheet_conversion(
    composite_requisition_app, _p181_published_map_identity, separate, corrupt_restore=False, short_yield=2,
    other_order=False
):
    from app.core.time_contract import beijing_today
    from decimal import Decimal
    from app.models.warehouse_inventory import InventoryReservation
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from tests.test_n039_composite_bom_requisition import _component_payload

    app, factory = composite_requisition_app
    short_mode = "一开二" if short_yield == 2 else "一开三"
    short_sheets = (350 + short_yield - 1) // short_yield
    extra_short = short_sheets * short_yield - 350
    material_id, snapshots = seed_graph(factory, separate=separate, quantity=100,
                              cutting_modes={2: "一开四", 3: short_mode}, finished_slot_count=16)
    lots = {}
    others = {2:10, 3:30} if other_order else {2:0, 3:0}
    with factory() as db:
        for sid, pid in snapshots:
            target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=pid)
            lot = manual_finished_in(db, customer_id=1, product_id=pid,
                location_id=target.location.id, quantity=(20 if pid == 2 else 50)+others[pid],
                stock_date=beijing_today(), source_type="manual", remarks="隔离余料夹具",
                operator_id=1, idempotency_key=f"loose-stock-{pid}",
                expected_layout_version=target.layout_version)
            # Explicit historical estimate in this anonymous fixture. Assembly
            # must retain its estimated provenance, never promote it to actual.
            lot.estimated_unit_cost_snapshot = Decimal("0.1250")
            lots[pid] = (lot.id, lot.version)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        protected = {}
        if other_order:
            from tests.test_multilevel_bom_order_entry import payload as order_payload
            from app.services.multilevel_bom_orders import read_compiled_order_bom
            from app.services.multilevel_bom_cutover_review import _row
            created_other = client.post("/api/orders",json=order_payload(factory,key="other-reserved-order"))
            assert created_other.status_code == 201, created_other.text
            other_iid = created_other.json()["items"][0]["id"]
            with factory() as db:
                other_sources = {row.component_product_id:row.id for row in read_compiled_order_bom(db,other_iid).snapshots}
            for pid,(lid,version) in lots.items():
                reserved = client.post("/api/warehouse/finished/bom-components/reservations",json=dict(
                    order_item_id=other_iid,bom_snapshot_id=other_sources[pid],inventory_lot_id=lid,
                    quantity=others[pid],expected_version=version,idempotency_key=f"other-order-{pid}",
                    warning_acknowledged_codes=[]))
                assert reserved.status_code == 200, reserved.text
            with factory() as db:
                protected = {row.id:_row(row) for row in db.scalars(select(InventoryReservation)
                    .where(InventoryReservation.order_item_id==other_iid))}
                lots = {pid:(lid,db.get(InventoryLot,lid).version) for pid,(lid,_) in lots.items()}
        for sid, pid in snapshots:
            lid, version = lots[pid]
            payload = dict(order_item_id=1, bom_snapshot_id=sid, inventory_lot_id=lid,
                quantity=20 if pid == 2 else 50, expected_version=version,
                idempotency_key=f"loose-reserve-{pid}", warning_acknowledged_codes=[])
            if other_order:
                rejected = client.post("/api/warehouse/finished/bom-components/reservations",
                    json={**payload,"quantity":payload["quantity"]+1,"idempotency_key":f"over-available-{pid}"})
                assert rejected.status_code == 409, rejected.text
            response = client.post("/api/warehouse/finished/bom-components/reservations", json=payload)
            assert response.status_code == 200, response.text
            replay = client.post("/api/warehouse/finished/bom-components/reservations", json=payload)
            assert replay.status_code == 200, replay.text
        with factory() as db:
            requirements = read_graph_requirements(db, 1)
            assert {r.product_id: r.purchase_sheets for r in requirements.plan.materials} == {2: 70, 3: short_sheets}
            rows = list(db.scalars(select(InventoryReservation)))
            assert len(rows) == (4 if other_order else 2)
            assert {r.inventory_lot_id for r in rows} == {v[0] for v in lots.values()}
        saved = client.post("/api/requisition/batches", json={"request_key": "loose-sheet-conversion",
            "supplier_name": "苏州纸板供应商", "items": [
                {**_component_payload(sid), "special_process": "一开四" if pid == 2 else short_mode}
                for sid, pid in snapshots]})
        assert saved.status_code == 201, saved.text
        from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
        with factory() as db:
            sources = list(db.scalars(select(PurchasePurposeSourceSnapshot)))
            assert sorted(s.order_purpose_sheet_qty for s in sources) == [70, short_sheets]
            assert db.scalar(select(func.count()).select_from(BomAssembly)) == 0
        from tests.test_multilevel_bom_receipt_flow import read_purchase_sources
        receipt_ids = []
        for index, source in enumerate(read_purchase_sources(factory, material_id)):
            fact = _freeze_receipt_fact(client, source,
                idempotency_key=f"loose-price-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            for batch_index, quantity in enumerate((10, source.order_purpose_sheet_qty - 10)):
                key = f"loose-receipt-{index}-{batch_index}"
                response = _receive(client, source, fact.json(), quantity=quantity, idempotency_key=key)
                assert response.status_code == 200, response.text
                receipt_ids.append(response.json()["receipt_item_id"])
                replay = _receive(client, source, fact.json(), quantity=quantity, idempotency_key=key)
                assert replay.status_code == 200, replay.text
                assert replay.json()["receipt_item_id"] == response.json()["receipt_item_id"]
        with factory() as db:
            totals = {}
            for lot in db.scalars(select(InventoryLot).where(InventoryLot.inventory_type == "finished")):
                pid = lot.finished_detail.product_id
                totals[pid] = totals.get(pid, 0) + lot.quantity_reserved + lot.quantity_available
            if separate:
                assert totals == {2: 300+others[2], 3: 400 + extra_short+others[3]}
                assert db.scalar(select(func.count()).select_from(BomAssembly)) == 0
            else:
                import json
                assert totals == {1: 100, 2: others[2], 3: extra_short+others[3]}
                assemblies = list(db.scalars(select(BomAssembly)))
                assert sum(row.quantity for row in assemblies) == 100
                assert sum(row.total_cost for row in assemblies) == Decimal("38.9830" if short_yield == 2 else "31.7847")
                assert any(json.loads(row.cost_detail_json)["actual"] is False for row in assemblies)
                assert {pid: sum(lot.quantity_consumed for lot in db.scalars(select(InventoryLot))
                    if lot.finished_detail and lot.finished_detail.product_id == pid) for pid in (2, 3)} == {2: 300, 3: 400}
            requirements = read_graph_requirements(db, 1)
            assert {r.product_id: r.purchase_sheets for r in requirements.plan.materials} == {2: 70, 3: short_sheets}
        from app.api.deliveries import router
        from app.models.order import OrderItem
        app.include_router(router, prefix="/api/deliveries")
        created = client.post("/api/deliveries", json={"customer_id": 1,
            "delivery_date": "2026-09-10", "items": [{"order_item_id": 1, "delivered_quantity": 100}]})
        assert created.status_code == 201, created.text
        did = created.json()["id"]
        dispatched = client.put(f"/api/deliveries/{did}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        with factory() as db:
            assert db.get(OrderItem, 1).delivered_quantity == 100
            if separate:
                from app.services.multilevel_bom_fulfillment import read_order_component_fulfillment
                assert read_order_component_fulfillment(db,1).complete
                # Preserve the real dispatch, inject a missing child allocation
                # in a rollback-only transaction to exercise the close guard.
                from app.models.warehouse_inventory import DeliveryInventoryAllocation
                from app.api.deliveries import _refresh_order_status
                from fastapi import HTTPException
                allocation = db.scalar(select(DeliveryInventoryAllocation)
                    .join(InventoryReservation,InventoryReservation.id==DeliveryInventoryAllocation.reservation_id)
                    .where(InventoryReservation.order_item_id==1,
                        InventoryReservation.sales_order_item_bom_component_id==dict((pid,sid) for sid,pid in snapshots)[3]))
                assert allocation is not None
                allocation.reversed_requirement_quantity += 1
                db.flush()
                fulfillment = read_order_component_fulfillment(db,1)
                assert not fulfillment.complete and fulfillment.components[-1].remaining == 1
                with pytest.raises(HTTPException,match="子件实发尚未完成"):
                    _refresh_order_status(db,db.get(OrderItem,1).order_id)
                db.rollback()
        cancelled = client.put(f"/api/deliveries/{did}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        with factory() as db:
            assert db.get(OrderItem, 1).delivered_quantity == 0
            for pid, expected in ({2: 300, 3: 400} if separate else {1: 100}).items():
                selected = [lot for lot in db.scalars(select(InventoryLot))
                    if lot.finished_detail and lot.finished_detail.product_id == pid]
                assert sum(lot.quantity_reserved for lot in selected) == expected+others.get(pid,0)
                assert sum(lot.quantity_consumed for lot in selected) == 0
        if corrupt_restore:
            from app.models.warehouse_inventory import InventoryLocationMovement
            from app.models.production import ProductionCompletion
            with factory() as db:
                restore = db.scalar(select(InventoryLocationMovement).where(
                    InventoryLocationMovement.movement_type == "move").order_by(InventoryLocationMovement.id.desc()))
                assert restore is not None
                restore.pallet_version_before += 10  # Same label, broken exact inverse proof.
                db.commit()
                before = [(lot.id, lot.version, lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed)
                          for lot in db.scalars(select(InventoryLot).order_by(InventoryLot.id))]
            rejected = client.put(f"/api/incoming/receipt-items/{receipt_ids[-1]}/revert", json={
                "reason": "不能信任损坏的恢复链", "idempotency_key": "broken-restore-revert"})
            assert rejected.status_code == 409 and "移过库位" in rejected.text
            with factory() as db:
                assert [(lot.id, lot.version, lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed)
                        for lot in db.scalars(select(InventoryLot).order_by(InventoryLot.id))] == before
                assert all(row.status == "posted" for row in db.scalars(select(ProductionCompletion)))
            return
        for rid in reversed(receipt_ids):
            undone = client.put(f"/api/incoming/receipt-items/{rid}/revert", json={
                "reason": "隔离验收撤销本次分批收料", "idempotency_key": f"loose-undo-{rid}"})
            assert undone.status_code == 200, undone.text
            replay = client.put(f"/api/incoming/receipt-items/{rid}/revert", json={
                "reason": "隔离验收撤销本次分批收料", "idempotency_key": f"loose-undo-{rid}"})
            assert replay.status_code == 200 and replay.json() == undone.json()
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(BomAssembly).where(BomAssembly.status == "posted")) == 0
            for pid, (lid, _) in lots.items():
                lot = db.get(InventoryLot, lid)
                assert lot.quantity_reserved == (20 if pid == 2 else 50)+others[pid]
                assert lot.quantity_consumed == 0
            if other_order:
                assert {rid:_row(db.get(InventoryReservation,rid)) for rid in protected} == protected


@pytest.mark.parametrize("separate", [True, False])
def test_other_orders_reservations_survive_credits_receipts_delivery_and_reversal(
    composite_requisition_app, _p181_published_map_identity, separate
):
    test_loose_children_are_reserved_individually_before_sheet_conversion(
        composite_requisition_app, _p181_published_map_identity, separate, other_order=True)


def test_receipt_reversal_rejects_a_broken_pallet_restore_proof(composite_requisition_app, _p181_published_map_identity):
    test_loose_children_are_reserved_individually_before_sheet_conversion(
        composite_requisition_app, _p181_published_map_identity, True, corrupt_restore=True)


@pytest.mark.parametrize("separate", [True, False])
def test_rounding_surplus_remains_real_stock(composite_requisition_app, _p181_published_map_identity, separate):
    test_loose_children_are_reserved_individually_before_sheet_conversion(
        composite_requisition_app, _p181_published_map_identity, separate, short_yield=3)


def test_public_order_entry_freezes_separate_mode_and_real_child_demands(
    composite_requisition_app, _p181_published_map_identity
):
    from app.models.order import OrderItem
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from tests.test_multilevel_bom_order_entry import payload
    app, factory = composite_requisition_app
    seed_graph(factory, separate=True)
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload(factory, key="separate-new-order"))
        assert response.status_code == 201, response.text
        iid = response.json()["items"][0]["id"]
        with factory() as db:
            compiled = read_compiled_order_bom(db, iid)
            assert compiled.graph.modes.inventory == "separate"
            assert compiled.graph.modes.delivery == "components"
            assert db.get(OrderItem, iid).composite_fulfillment_mode_snapshot == "component_delivery"
            assert {d.component_product_id: d.required_piece_quantity for d in delivery_component_demands(db, iid)} == {2: 30, 3: 40}


@pytest.mark.parametrize("partial", [False, True])
def test_separate_receipts_preserve_each_real_child_without_parent_stock(
    composite_requisition_app, _p181_published_map_identity, partial
):
    app, factory = composite_requisition_app
    material, snapshots = seed_graph(factory, separate=True)
    with TestClient(app) as client:
        _login(client)
        sources = purchase_sources(client, factory, material, snapshots)
        for index, source in enumerate(sources):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"separate-price-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            batches = (10, source.order_purpose_sheet_qty - 10) if partial else (source.order_purpose_sheet_qty,)
            for batch_index, quantity in enumerate(batches):
                key = f"separate-receipt-{index}-{batch_index}"
                response = _receive(client, source, fact.json(), quantity=quantity, idempotency_key=key)
                assert response.status_code == 200, response.text
                replay = _receive(client, source, fact.json(), quantity=quantity, idempotency_key=key)
                assert replay.status_code == 200, replay.text
                assert replay.json()["receipt_item_id"] == response.json()["receipt_item_id"]
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(BomAssembly)) == 0
            lots = list(db.scalars(select(InventoryLot).where(InventoryLot.source_ref_type == "production_completion")))
            quantities = {}
            for lot in lots:
                pid = lot.finished_detail.product_id
                quantities[pid] = quantities.get(pid, 0) + lot.quantity_available + lot.quantity_reserved
            assert quantities == {2: 30, 3: 40}
            assert {d.component_product_id for d in delivery_component_demands(db, 1) if d.show_on_delivery} == {2, 3}
            requirements = read_graph_requirements(db, 1)
            assert requirements.finished_units[1] == 0
            assert {r.product_id: r.purchase_sheets for r in requirements.plan.materials} == {2: 30, 3: 40}
