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


def test_loose_children_are_reserved_individually_before_sheet_conversion(
    composite_requisition_app, _p181_published_map_identity
):
    from app.core.time_contract import beijing_today
    from app.models.warehouse_inventory import InventoryReservation
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from tests.test_n039_composite_bom_requisition import _component_payload

    app, factory = composite_requisition_app
    material_id, snapshots = seed_graph(factory, separate=True, quantity=100,
                              cutting_modes={2: "一开四", 3: "一开二"})
    lots = {}
    with factory() as db:
        for sid, pid in snapshots:
            target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=pid)
            lot = manual_finished_in(db, customer_id=1, product_id=pid,
                location_id=target.location.id, quantity=20 if pid == 2 else 50,
                stock_date=beijing_today(), source_type="manual", remarks="隔离余料夹具",
                operator_id=1, idempotency_key=f"loose-stock-{pid}",
                expected_layout_version=target.layout_version)
            lots[pid] = (lot.id, lot.version)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        for sid, pid in snapshots:
            lid, version = lots[pid]
            payload = dict(order_item_id=1, bom_snapshot_id=sid, inventory_lot_id=lid,
                quantity=20 if pid == 2 else 50, expected_version=version,
                idempotency_key=f"loose-reserve-{pid}", warning_acknowledged_codes=[])
            response = client.post("/api/warehouse/finished/bom-components/reservations", json=payload)
            assert response.status_code == 200, response.text
            replay = client.post("/api/warehouse/finished/bom-components/reservations", json=payload)
            assert replay.status_code == 200, replay.text
        with factory() as db:
            requirements = read_graph_requirements(db, 1)
            assert {r.product_id: r.purchase_sheets for r in requirements.plan.materials} == {2: 70, 3: 175}
            rows = list(db.scalars(select(InventoryReservation)))
            assert len(rows) == 2
            assert {r.inventory_lot_id for r in rows} == {v[0] for v in lots.values()}
        saved = client.post("/api/requisition/batches", json={"request_key": "loose-sheet-conversion",
            "supplier_name": "苏州纸板供应商", "items": [
                {**_component_payload(sid), "special_process": "一开四" if pid == 2 else "一开二"}
                for sid, pid in snapshots]})
        assert saved.status_code == 201, saved.text
        from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
        with factory() as db:
            sources = list(db.scalars(select(PurchasePurposeSourceSnapshot)))
            assert sorted(s.order_purpose_sheet_qty for s in sources) == [70, 175]
            assert db.scalar(select(func.count()).select_from(BomAssembly)) == 0
        from tests.test_multilevel_bom_receipt_flow import read_purchase_sources
        for index, source in enumerate(read_purchase_sources(factory, material_id)):
            fact = _freeze_receipt_fact(client, source,
                idempotency_key=f"loose-price-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            for batch_index, quantity in enumerate((10, source.order_purpose_sheet_qty - 10)):
                key = f"loose-receipt-{index}-{batch_index}"
                response = _receive(client, source, fact.json(), quantity=quantity, idempotency_key=key)
                assert response.status_code == 200, response.text
                replay = _receive(client, source, fact.json(), quantity=quantity, idempotency_key=key)
                assert replay.status_code == 200, replay.text
                assert replay.json()["receipt_item_id"] == response.json()["receipt_item_id"]
        with factory() as db:
            totals = {}
            for lot in db.scalars(select(InventoryLot).where(InventoryLot.inventory_type == "finished")):
                pid = lot.finished_detail.product_id
                totals[pid] = totals.get(pid, 0) + lot.quantity_reserved + lot.quantity_available
            assert totals == {2: 300, 3: 400}
            assert db.scalar(select(func.count()).select_from(BomAssembly)) == 0
            requirements = read_graph_requirements(db, 1)
            assert {r.product_id: r.purchase_sheets for r in requirements.plan.materials} == {2: 70, 3: 175}


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
