"""Product-identity destinations on an anonymous published map, never factory data."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_multilevel_bom_receipt_flow import (
    composite_requisition_app, _p181_published_map_identity, seed_graph,
    purchase_sources, _freeze_receipt_fact, _receive, _login,
)


def bind_graph_products(factory, monkeypatch):
    from app.models.warehouse_inventory import WarehouseArea, WarehouseAreaStoragePolicy, WarehouseFloor
    from app.services import location_candidates
    from app.services.warehouse_rack_cells import sync_published_rack_cells
    from tests.test_p1_123_warehouse_region_rack_labels import _layout
    from tests.test_fixed_shelf import configure
    original = location_candidates.load_warehouse_twin_published_floor_identity
    def identity(floor):
        result = original(floor)
        if int(floor) == 3:
            result = {**result, "zones_by_id": {**result["zones_by_id"], "zone-fin-001": "FIN-001"},
                "zone_ids_by_area": {**result["zone_ids_by_area"], "FIN-001": ("zone-fin-001",)}}
        return result
    monkeypatch.setattr(location_candidates, "load_warehouse_twin_published_floor_identity", identity)
    with factory() as db:
        floor = db.scalar(select(WarehouseFloor).where(WarehouseFloor.floor_number == 3))
        area = WarehouseArea(floor_id=floor.id, area_code="FIN-001", area_name="匿名组套货架",
            construction_status="enabled")
        area.storage_policy = WarehouseAreaStoragePolicy(map_feature_id="zone-fin-001",
            allowed_inventory_types_json='["finished"]', storage_layout="rack", status="published",
            published_map_revision="p181-anonymous-map-v1", draft_map_revision="p181-anonymous-map-v1", version=1)
        db.add(area)
        db.flush()
        layout = _layout()
        layout["floor_code"] = floor.floor_code
        layout["revision"] = "p181-anonymous-map-v1"
        ids = list(sync_published_rack_cells(db, floor_layout=layout, operator_id=1).created_location_ids)
        bindings = {1: ids[0], 4: ids[1]}
        for pid, lid in bindings.items():
            configure(db, pid, [lid], capacity=100)
        db.commit()
        return bindings


def test_real_receipts_place_carton_and_liner_in_their_own_fixed_cells(
        composite_requisition_app, _p181_published_map_identity, monkeypatch):
    from app.models.warehouse_inventory import InventoryLot
    from app.models.fixed_shelf import ShelfLotState
    from tests.test_fixed_shelf import incoming
    app, factory = composite_requisition_app
    material, snapshots = seed_graph(factory, liner=True)
    bindings = bind_graph_products(factory, monkeypatch)
    with factory() as db:
        # Existing same-product stock must stay in place with its own cost lineage.
        for pid, lid in bindings.items():
            incoming(db, pid, 1, lid, qty=2, key=f"old-stock-{pid}")
        db.commit()
    with TestClient(app) as client:
        _login(client)
        receipt_ids = []
        for index, source in enumerate(purchase_sources(client, factory, material, snapshots)):
            priced = _freeze_receipt_fact(client, source, idempotency_key=f"fixed-price-{index}", unit_price="0.1234")
            assert priced.status_code == 200, priced.text
            received = _receive(client, source, priced.json(), quantity=source.order_purpose_sheet_qty,
                                idempotency_key=f"fixed-receipt-{index}")
            assert received.status_code == 200, received.text
            receipt_ids.append(received.json()["receipt_item_id"])
        with factory() as db:
            for pid, lid in bindings.items():
                lots = [lot for lot in db.scalars(select(InventoryLot))
                        if lot.finished_detail and lot.finished_detail.product_id == pid]
                assert {lot.warehouse_location_id for lot in lots} == {lid}
                assert sum(lot.quantity_reserved for lot in lots) == 10
                assert sum(lot.quantity_available for lot in lots) == 2
                assert all(lot.pallet_item is None for lot in lots)
                assert all(db.get(ShelfLotState, lot.id).target_location_id is None for lot in lots)
            from app.services.production_workflow import list_production_completions
            completions = list_production_completions(db, allowed_customer_ids={1})
            carton = next(row for row in completions if row["quantity"] == 10)
            assert carton["current_inventory_status"] == "located", carton
            assert carton["current_location_issue"] is None
            assert carton["system_pallet_id"] is None
        for rid in reversed(receipt_ids):
            reverted = client.put(f"/api/incoming/receipt-items/{rid}/revert", json={})
            assert reverted.status_code == 200, reverted.text
        with factory() as db:
            for pid, lid in bindings.items():
                lots = [lot for lot in db.scalars(select(InventoryLot)) if lot.warehouse_location_id == lid]
                assert sum(lot.quantity_available for lot in lots) == 2
                assert sum(lot.quantity_reserved for lot in lots) == 0


def test_fixed_target_rejects_stale_map_and_wrong_customer_and_honors_preferred_area(
        composite_requisition_app, _p181_published_map_identity, monkeypatch):
    from app.services.production_workflow import _receipt_auto_finished_ground_target, ProductionWorkflowError
    from app.models.warehouse_inventory import WarehouseLocation
    _, factory = composite_requisition_app
    seed_graph(factory, liner=True)
    bindings = bind_graph_products(factory, monkeypatch)
    with factory() as db:
        with pytest.raises(ProductionWorkflowError, match="客户不一致"):
            _receipt_auto_finished_ground_target(db, claim=True, customer_id=999, product_id=4)
        db.rollback()
        target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=4)
        assert target.location.id == bindings[4]
        assert target.target_kind == "fixed_shelf"
        db.get(WarehouseLocation, bindings[4]).is_active = False
        db.commit()
        with pytest.raises(ProductionWorkflowError, match="固定货位不可用"):
            _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=4)
        db.rollback()
        # A configured customer area owns precedence, even when the product's
        # fixed slot is invalid. Never silently choose a different area.
        from app.models.customer_finished_storage_preference import CustomerFinishedStoragePreference
        from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor
        area = db.scalar(select(WarehouseArea).join(WarehouseFloor).where(
            WarehouseFloor.floor_number == 1, WarehouseArea.area_code == "FIN-001"))
        db.add(CustomerFinishedStoragePreference(customer_id=1, warehouse_area_id=area.id, priority=1))
        db.commit()
        preferred = _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=4)
        assert preferred.target_kind == "preferred_location"
        assert preferred.area.id == area.id
        assert preferred.location.warehouse_floor == 1
        area.storage_policy.status = "draft"
        db.commit()
        with pytest.raises(ProductionWorkflowError, match="系统不会改放一楼"):
            _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=4)


@pytest.mark.parametrize("full_product", [1, 4])
def test_fixed_cell_capacity_failure_rolls_back_entire_receipt_then_allows_retry(
        composite_requisition_app, _p181_published_map_identity, monkeypatch, full_product):
    from app.models.fixed_shelf import ShelfBinding
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.production import ProductionCompletion, ProductionCompletionBatch
    from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation, InventoryMovement
    app, factory = composite_requisition_app
    material, snapshots = seed_graph(factory, liner=True)
    bindings = bind_graph_products(factory, monkeypatch)
    with factory() as db:
        db.get(ShelfBinding, bindings[full_product]).capacity = 1
        db.commit()
    models = [IncomingReceipt, IncomingReceiptItem, ProductionCompletion, ProductionCompletionBatch,
              BomAssembly, BomAssemblyInput, InventoryLot, InventoryReservation, InventoryMovement]
    def facts():
        with factory() as db:
            return {model.__tablename__: list(db.execute(select(model.__table__).order_by(
                *model.__table__.primary_key.columns))) for model in models}
    failed_once = False
    with TestClient(app) as client:
        _login(client)
        for index, source in enumerate(purchase_sources(client, factory, material, snapshots)):
            priced = _freeze_receipt_fact(client, source, idempotency_key=f"cap-price-{index}", unit_price="0.1234")
            assert priced.status_code == 200, priced.text
            before = facts()
            received = _receive(client, source, priced.json(), quantity=source.order_purpose_sheet_qty,
                                idempotency_key=f"cap-receipt-{index}")
            if received.status_code != 200:
                assert received.status_code == 409 and "放不下" in received.text, received.text
                assert not failed_once
                failed_once = True
                assert facts() == before
                with factory() as db:
                    db.get(ShelfBinding, bindings[full_product]).capacity = 100
                    db.commit()
                received = _receive(client, source, priced.json(), quantity=source.order_purpose_sheet_qty,
                                    idempotency_key=f"cap-receipt-{index}")
                assert received.status_code == 200, received.text
    assert failed_once
