"""Full HTTP purchase/receipt/delivery loop in a disposable anonymous database."""
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login, _parent_payload, _component_payload
from tests.test_p1_81_receipt_purpose_flow import (
    _p181_published_map_identity, _seed_material_and_staging, FrozenSource,
    _freeze_receipt_fact, _receive,
)
from tests.test_p1_80_purchase_purpose_allocation import (
    _preview_supplier_order_draft, _save_draft,
)


def test_common_box_subkit_save_read_stale_and_disabled_access(composite_requisition_app):
    from app.api.products import router
    from app.models.product import Product
    app, factory = composite_requisition_app
    app.include_router(router, prefix="/api/products")
    with factory() as db:
        parent = db.get(Product, 1)
        parent.composite_fulfillment_mode = "parent_delivery"
        parent.is_virtual_composite_parent = False
        db.commit()
    with TestClient(app) as client:
        assert client.get("/api/products/1/bom").status_code in (401, 403)
        _login(client)
        current = client.get("/api/products/1/bom").json()
        payload = {"expected_version":current["version"], "change_reason":"isolated subkit test",
            "components":[{"component_product_id":2,"quantity_per_set":2},
                          {"component_product_id":3,"quantity_per_set":6}],
            "subkit":{"name":"000148内衬","kits_per_parent":1,"version":0,"enabled":True}}
        saved = client.put("/api/products/1/bom", json=payload)
        assert saved.status_code == 200, saved.text
        loaded = client.get("/api/products/1/bom").json()
        assert loaded["subkit"]["name"] == "000148内衬"
        assert loaded["subkit"]["version"] == 1
        assert client.put("/api/products/1/bom", json=payload).status_code == 409
        assert client.get("/api/products/1/bom").json() == loaded


@pytest.mark.parametrize("parent_last,split_short", [(False, False), (True, False), (False, True)])
def test_parent_and_liner_receipt_dispatch_cancel_are_separate(composite_requisition_app, _p181_published_map_identity, parent_last, split_short):
    from app.api.deliveries import router
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
    from app.models.user import User
    from app.models.bom_subkit import SubkitConversion, SubkitDeliveryAllocation
    from app.models.warehouse_inventory import InventoryLot
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot, SupplierRequisitionOrderItem
    from app.services.bom_subkits import save_subkit, freeze_order_subkit
    app, factory = composite_requisition_app
    app.include_router(router, prefix="/api/deliveries")
    with factory() as db:
        from app.models.warehouse_inventory import WarehouseLocation
        db.get(WarehouseLocation, 1).location_code = "SUBKIT-OLD-FIXTURE"
        db.commit()
    material_id = _seed_material_and_staging(factory)
    with factory() as db:
        parent = db.get(Product, 1)
        from app.models.supplier import Supplier
        from app.services.supplier_master import normalize_supplier_identity
        db.add(Supplier(standard_name="苏州纸板供应商", normalized_name=normalize_supplier_identity("苏州纸板供应商"),
            display_name="苏州纸板供应商", sort_order=20, is_active=True, version=1))
        parent.is_virtual_composite_parent = False
        parent.composite_fulfillment_mode = "parent_delivery"
        parent.box_style = "模切内盒"
        item = db.get(OrderItem, 1)
        item.composite_fulfillment_mode_snapshot = "parent_delivery"
        item.snapshot_supplier_name = "苏州纸板供应商"
        item.snapshot_pieces_per_box = 1
        for component in db.scalars(select(SalesOrderItemBomComponent)):
            per = 2 if component.component_product_id == 2 else 6
            component.quantity_per_set = per
            component.required_piece_quantity = 10 * per
            component.snapshot_component_pieces_per_box = 1
            component.snapshot_component_supplier_name = "苏州纸板供应商"
            component.snapshot_component_material = "KAKAK"
            component.snapshot_component_material_id = material_id
            product = db.get(Product, component.component_product_id)
            product.material_id = material_id
            relation = db.scalar(select(ProductBomComponent).where(ProductBomComponent.component_product_id == product.id))
            relation.quantity_per_set = per
        save_subkit(db, parent_product_id=1, name="000148内衬", kits_per_parent=1,
            members=[{"product_id": 2, "pieces_per_kit": 2}, {"product_id": 3, "pieces_per_kit": 6}],
            expected_version=0, actor=db.get(User, 1))
        freeze_order_subkit(db, order_item_id=1, actor_id=1)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        saved = client.post("/api/requisition/batches", json={"request_key":"subkit-requisition",
            "supplier_name":"苏州纸板供应商", "items":[_parent_payload(), _component_payload(1), _component_payload(2)]})
        assert saved.status_code == 201, saved.text
        with factory() as db:
            sources = []
            for snapshot in db.scalars(select(PurchasePurposeSourceSnapshot).order_by(PurchasePurposeSourceSnapshot.id)):
                from app.models.requisition import RequisitionItem
                from app.models.product_bom import RequisitionItemBomSource
                rid = snapshot.source_requisition_item_id
                if snapshot.source_bom_requisition_source_id:
                    rid = db.get(RequisitionItemBomSource, snapshot.source_bom_requisition_source_id).requisition_item_id
                elif not rid:
                    rid = db.scalar(select(RequisitionItem.id).where(RequisitionItem.order_item_id == 1,
                        ~RequisitionItem.id.in_(select(RequisitionItemBomSource.requisition_item_id))))
                source = db.get(RequisitionItem, rid) if rid else db.get(OrderItem, snapshot.source_order_item_id)
                sources.append(FrozenSource(source_key=snapshot.source_key, route_key=f"r{source.id}" if rid else str(source.id),
                    supplier_item_id=source.id, source_version=getattr(source, "version", 1),
                    purpose_snapshot_id=snapshot.id, purpose_snapshot_version=snapshot.snapshot_version,
                    receipt_plan_fingerprint=snapshot.preview_fingerprint, component_type=snapshot.component_type,
                    material_id=material_id, order_purpose_sheet_qty=snapshot.order_purpose_sheet_qty,
                    reserve_purpose_sheet_qty=snapshot.reserve_purpose_sheet_qty))
        assert len(sources) == 3
        if parent_last:
            sources = sources[1:] + sources[:1]
        for index, source in enumerate(sources):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"subkit-price-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            if split_short and source.order_purpose_sheet_qty == 60:
                first = _receive(client, source, fact.json(), quantity=30,
                    idempotency_key=f"subkit-in-first-{index}")
                assert first.status_code == 200, first.text
                with factory() as db:
                    assert sum(row.quantity for row in db.scalars(select(SubkitConversion))) == 5
            received_quantity = 30 if split_short and source.order_purpose_sheet_qty == 60 else source.order_purpose_sheet_qty
            received = _receive(client, source, fact.json(), quantity=received_quantity,
                idempotency_key=f"subkit-in-{index}")
            assert received.status_code == 200, (received.text, source, client.get("/api/incoming/pending").json())
            repeated = _receive(client, source, fact.json(), quantity=received_quantity,
                idempotency_key=f"subkit-in-{index}")
            assert repeated.status_code == 200, repeated.text
        with factory() as db:
            conversions = list(db.scalars(select(SubkitConversion)))
            assert sum(row.quantity for row in conversions) == 10
            kit = db.get(InventoryLot, next(row.output_lot_id for row in conversions if row.quantity))
            assert sum(db.get(InventoryLot, row.output_lot_id).quantity_available for row in conversions if row.quantity) == 10
            assert kit.finished_detail.product_name_snapshot == "000148内衬"
            assert sum(row.total_cost for row in conversions) == Decimal("9.8720")
        delivery = client.post("/api/deliveries", json={"customer_id":1,"items":[{"order_item_id":1,"delivered_quantity":10}]})
        assert delivery.status_code == 201, delivery.text
        delivery_id = delivery.json()["id"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch", json={})
        assert dispatched.status_code == 200, dispatched.text
        with factory() as db:
            allocations = list(db.scalars(select(SubkitDeliveryAllocation).where(SubkitDeliveryAllocation.reversed.is_(False))))
            allocation = allocations[0]
            assert sum(row.quantity for row in allocations) == 10
            assert sum(row.total_cost for row in allocations) == Decimal("9.8720")
            assert db.get(InventoryLot, allocation.lot_id).quantity_available == 0
            from app.services.material_cost_lineage import material_cost_coverage_report
            from app.models.delivery import Delivery
            month = db.get(Delivery, delivery_id).delivery_date.strftime("%Y-%m")
            report = material_cost_coverage_report(db, month=month)
            assert report["actual_material_cost"] == Decimal("11.11"), report
            assert report["covered_delivery_lines"] == 1, report
        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel", json={})
        assert cancelled.status_code == 200, cancelled.text
        with factory() as db:
            assert sum(db.get(InventoryLot, row.lot_id).quantity_available for row in allocations) == 10
