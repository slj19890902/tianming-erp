"""Frozen-purpose receiving with fixed V2 drawings in disposable fixture DBs.

The anonymous published warehouse-map identity is an existing warehouse test
fixture. Real map availability and a factory schema upgrade are not asserted.
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_phase11_requisition import requisition_app, _login
from tests.test_p1_80_purchase_purpose_allocation import (
    _created_order_id, _preview_supplier_order_draft, _save_draft,
    _selection, _set_purpose_plan,
)
from tests.test_p1_81_receipt_purpose_flow import (
    FrozenSource, _business_counts, _freeze_receipt_fact, _receive,
    _seed_material_and_staging, _use_p181_published_map_identity,
)
from tests.test_n039_composite_bom_requisition import composite_requisition_app


@pytest.mark.parametrize("with_print", [False, True])
def test_frozen_supplier_receipt_keeps_new_order_drawing_and_completion_gate(
    requisition_app, monkeypatch, tmp_path, with_print,
):
    from app.api.drawing_design import router as drawing_router
    from app.api.mobile_erp import router as mobile_router
    from app.api.orders import router as orders_router
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    from app.models.drawing_design import DrawingRelease, ProductionTaskDrawing
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionCompletion, ProductionTask
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot, SupplierRequisitionOrderItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    app, factory = requisition_app
    factory.class_ = ReceiptPriceGuardSession
    app.include_router(drawing_router, prefix="/api/master/products")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(mobile_router, prefix="/api/mobile")
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(tmp_path / "v2_files"))
    monkeypatch.setenv("ERP_DRAWING_V2_ENABLED", "1")
    _use_p181_published_map_identity(monkeypatch)
    material_id = _seed_material_and_staging(factory)
    with factory() as db:
        product = db.get(Product, 1)
        product.box_style = "A1/0201 普通开槽箱"
        product.splice_mode, product.pieces_per_box = "single", 1
        product.flute_type, product.layer_count = "AB", 5
        product.report_length_mm, product.report_width_mm = 1770, 650
        product.crease_type = "压线"
        product.crease_left_mm, product.crease_middle_mm, product.crease_right_mm = 175, 300, 175
        product.default_cutting_mode = "一开一"
        product.print_content = "单色印刷" if with_print else "无印刷"
        product.printing_colors = "黑色" if with_print else None
        db.commit()
        product_version = product.version

    with TestClient(app) as client:
        _login(client, "admin")
        write = {
            "expected_product_version": product_version, "template_key": "slotted_v1",
            "parameters": {"panel_1_mm": 520, "panel_2_mm": 350, "panel_3_mm": 520, "panel_4_mm": 350,
                "body_height_mm": 300, "top_flap_mm": 175, "bottom_flap_mm": 175,
                "glue_flap_mm": 30, "slot_width_mm": 5},
            "thickness_mm": "7", "thickness_source": "匿名隔离模板测试值，非生产测量",
            "idempotency_key": "purpose-drawing-initial-save",
            "print_objects": [{"kind": "text", "text": "UAT印刷", "panel_id": "panel_1",
                "x_mm": 10, "y_mm": 10, "width_mm": 80, "height_mm": 20}] if with_print else [],
        }
        saved = client.put("/api/master/products/1/managed-drawing", json=write)
        assert saved.status_code == 200, saved.text
        published = client.post("/api/master/products/1/managed-drawing/releases", json={
            "expected_product_version": product_version, "expected_design_version": saved.json()["draft"]["version"],
            "idempotency_key": "purpose-drawing-initial-publish"})
        assert published.status_code == 200, published.text
        initial_release = published.json()["id"]

        created = client.post("/api/orders", json={
            "customer_id": 1, "order_date": "2026-09-22", "status": "pending_production",
            "remark": "隔离冻结用途图纸集成测试", "items": [{
                "product_id": 1, "quantity": 2, "unit_price": "3.60", "specification": "520×350×300mm",
                "material_id": material_id, "material": "KAKAK", "layer_count": 5, "flute_type": "AB",
            }]})
        assert created.status_code == 201, created.text
        with factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == created.json()["id"]))
            assert item is not None and item.snapshot_spec == "520×350×300mm"
            item_id = item.id
            task = db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == item_id))
            assert task is not None and task.status == "waiting_material"
            task_id = task.id
            assert db.get(ProductionTaskDrawing, task_id).release_id == initial_release

        draft = _preview_supplier_order_draft(client, [_selection(item_id, report_length_mm=1770, report_width_mm=650)])
        lines = draft["supplier_groups"][0]["lines"]
        assert len(lines) == 1
        _set_purpose_plan(lines[0], purchase_total=2, order_purpose=2, stock_purpose=0)
        ordered = _save_draft(client, draft)
        assert ordered.status_code == 201, ordered.text
        supplier_order_id = _created_order_id(ordered)
        with factory() as db:
            line = db.scalar(select(SupplierRequisitionOrderItem).where(
                SupplierRequisitionOrderItem.supplier_order_id == supplier_order_id))
            assert line.purpose_contract_status == "frozen"
            snapshot = db.scalar(select(PurchasePurposeSourceSnapshot).where(
                PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id == line.id))
            source = FrozenSource(source_key=snapshot.source_key, route_key=f"so{line.id}",
                supplier_item_id=line.id, source_version=line.version, purpose_snapshot_id=snapshot.id,
                purpose_snapshot_version=snapshot.snapshot_version, receipt_plan_fingerprint=snapshot.preview_fingerprint,
                component_type=snapshot.component_type, material_id=line.material_id,
                order_purpose_sheet_qty=2, reserve_purpose_sheet_qty=0)

        before = client.get(f"/api/requisition/supplier-orders/{supplier_order_id}/production-print-package")
        assert before.status_code == 200, before.text
        drawings = [d for card in before.json()["cards"] for d in card["managed_drawings"]]
        assert len(drawings) == 1 and drawings[0]["release_id"] == initial_release
        assert bool(drawings[0]["print_objects"]) == with_print

        # The already-created order must remain on its release even if another
        # draft/release is published before the deferred frozen receipt arrives.
        write.update(expected_design_version=saved.json()["draft"]["version"], idempotency_key="purpose-drawing-newer-save")
        write["parameters"]["top_flap_mm"] = 180
        newer_saved = client.put("/api/master/products/1/managed-drawing", json=write)
        assert newer_saved.status_code == 200, newer_saved.text
        newer = client.post("/api/master/products/1/managed-drawing/releases", json={
            "expected_product_version": product_version, "expected_design_version": newer_saved.json()["draft"]["version"],
            "idempotency_key": "purpose-drawing-newer-publish"})
        assert newer.status_code == 200 and newer.json()["id"] != initial_release

        price = _freeze_receipt_fact(client, source, idempotency_key="purpose-drawing-freeze-price")
        assert price.status_code == 200, price.text
        contract = price.json()
        baseline = _business_counts(factory)
        blocked = client.put(f"/api/incoming/receive/{source.route_key}", json={
            "received_quantity": 2, "idempotency_key": "purpose-drawing-missing-tokens"})
        assert blocked.status_code == 409, blocked.text
        assert _business_counts(factory) == baseline
        received = _receive(client, source, contract, quantity=2, idempotency_key="purpose-drawing-receive")
        assert received.status_code == 200, received.text
        allocation = received.json()["purpose_allocation"]
        assert allocation["order_sheet_delta"] == allocation["theoretical_finished_delta"] == 2
        assert allocation["reserve_sheet_delta"] == 0
        assert Decimal(str(allocation["order_cost"])) == Decimal(str(allocation["receipt_total_cost"]))
        posted_counts = _business_counts(factory)
        retry = _receive(client, source, contract, quantity=2, idempotency_key="purpose-drawing-receive")
        assert retry.status_code == 200, retry.text
        assert _business_counts(factory) == posted_counts

        with factory() as db:
            receipt = db.scalar(select(IncomingReceiptItem).where(IncomingReceiptItem.supplier_order_item_id == source.supplier_item_id))
            assert receipt is not None and receipt.received_quantity == 2
            assert db.scalar(select(func.count()).select_from(IncomingReceiptPurposeAllocation).where(
                IncomingReceiptPurposeAllocation.incoming_receipt_item_id == receipt.id)) == 1
            price_fact = db.scalar(select(SupplierReceiptSettlementPriceFact).where(
                SupplierReceiptSettlementPriceFact.incoming_receipt_item_id == receipt.id))
            assert price_fact is not None and price_fact.unit_price == Decimal("2.5")
            assert db.scalar(select(func.count()).select_from(ProductionCompletion).where(
                ProductionCompletion.order_item_id == item_id)) == 1
            assert db.get(ProductionTask, task_id).status == "completed"
            assert db.get(ProductionTaskDrawing, task_id).release_id == initial_release
            receipt_id = receipt.id
            release_hash = db.get(DrawingRelease, initial_release).pdf_sha256

        actual = client.get(f"/api/incoming/receipt-items/{receipt_id}/production-card")
        assert actual.status_code == 200, actual.text
        assert [d for card in actual.json()["cards"] for d in card["managed_drawings"]] == drawings
        # Frozen-purpose receiving posts completion. The existing mobile
        # pending-task gate must continue hiding an already-completed task.
        assert client.get(f"/api/mobile/production/tasks/{task_id}/drawing").status_code == 404
        download = client.get(f"/api/master/products/1/managed-drawing/releases/{initial_release}/file")
        assert download.status_code == 200, download.text
        import hashlib
        assert hashlib.sha256(download.content).hexdigest() == release_hash


def test_frozen_bom_two_sources_keep_component_drawings_and_minimum_capacity(
    composite_requisition_app, monkeypatch, tmp_path,
):
    """Two physical components must retain distinct releases and form two sets.

    Uses the established N039 2/3-piece BOM and P1-81 warehouse prerequisites.
    All material, price and quantity values are explicit anonymous test data.
    """
    from tests.test_n039_composite_bom_requisition import _login as login_composite
    from tests.test_n039_composite_bom_requisition import _component_payload
    from app.api.drawing_design import router as drawing_router
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    from app.models.drawing_design import ProductionTaskDrawing
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.material import Material
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent
    from app.models.production import ProductionCompletion, ProductionTask
    from app.models.requisition import RequisitionItem
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.models.warehouse_inventory import WarehouseLocation
    from app.services.purchase_purpose_allocation import canonical_purchase_purpose_hash

    app, factory = composite_requisition_app
    factory.class_ = ReceiptPriceGuardSession
    app.include_router(drawing_router, prefix="/api/master/products")
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(tmp_path / "bom_v2_files"))
    monkeypatch.setenv("ERP_DRAWING_V2_ENABLED", "1")
    _use_p181_published_map_identity(monkeypatch)
    with factory() as db:
        # Both established fixtures seed the same anonymous legacy location;
        # retain the N039 row under a distinct inactive test identity.
        legacy = db.scalar(select(WarehouseLocation).where(
            WarehouseLocation.location_code == "F1-DISPATCH-01"))
        legacy.location_code = "N039-LEGACY-DISPATCH"
        legacy.is_active = False
        db.commit()
    material_id = _seed_material_and_staging(factory)
    product_versions = {}
    with factory() as db:
        db.get(Material, material_id).supplier_name = "N039 供应商"
        parent = db.get(Product, 1)
        # The set identity is not a third physical sheet.
        parent.is_virtual_composite_parent = True
        parent.composite_fulfillment_mode = "parent_delivery"
        for product_id, length in ((2, 1000), (3, 1100)):
            product = db.get(Product, product_id)
            product.box_style = "衬板"
            product.length_mm, product.width_mm = length, 700
            product.report_length_mm, product.report_width_mm = length, 700
            product.material_id = material_id
            product.flute_type, product.layer_count = "AB", 5
            product.splice_mode, product.pieces_per_box = "single", 1
            product.default_cutting_mode = "一开一"
            product.print_content = "无印刷"
            product_versions[product_id] = product.version
        db.commit()

    with TestClient(app) as client:
        login_composite(client)
        releases = {}
        for product_id, length in ((2, 1000), (3, 1100)):
            saved = client.put(f"/api/master/products/{product_id}/managed-drawing", json={
                "expected_product_version": product_versions[product_id],
                "template_key": "liner_v1", "parameters": {"length_mm": length, "width_mm": 700},
                "thickness_mm": "7", "thickness_source": "匿名隔离 BOM 测试值，非生产测量",
                "print_objects": [], "idempotency_key": f"bom-component-{product_id}-save",
            })
            assert saved.status_code == 200, saved.text
            published = client.post(f"/api/master/products/{product_id}/managed-drawing/releases", json={
                "expected_product_version": product_versions[product_id],
                "expected_design_version": saved.json()["draft"]["version"],
                "idempotency_key": f"bom-component-{product_id}-publish",
            })
            assert published.status_code == 200, published.text
            releases[product_id] = published.json()["id"]
        assert releases[2] != releases[3]

        created = client.post("/api/orders", json={
            "customer_id": 1, "order_date": "2026-09-22", "status": "pending_production",
            "remark": "隔离 BOM 双组件冻结用途图纸测试", "items": [{
                "product_id": 1, "quantity": 2, "unit_price": "100", "specification": "组合成品",
            }],
        })
        assert created.status_code == 201, created.text
        with factory() as db:
            item = db.scalar(select(OrderItem).where(OrderItem.order_id == created.json()["id"]))
            item_id = item.id
            snapshots = db.scalars(select(SalesOrderItemBomComponent).where(
                SalesOrderItemBomComponent.sales_order_item_id == item_id
            ).order_by(SalesOrderItemBomComponent.display_order)).all()
            assert len(snapshots) == 2
            lines = []
            expected_sources = {}
            task_ids = {}
            for component in snapshots:
                product_id = component.component_product_id
                required = 4 if product_id == 2 else 6
                assert component.required_piece_quantity == required
                assert component.component_product_version == product_versions[product_id]
                task = db.scalar(select(ProductionTask).where(
                    ProductionTask.sales_order_item_bom_component_id == component.id))
                assert task is not None
                assert db.get(ProductionTaskDrawing, task.id).release_id == releases[product_id]
                task_ids[product_id] = task.id
                fingerprint = canonical_purchase_purpose_hash({
                    "version": 1, "source_key": f"bom_component:{component.id}:whole", "customer_id": 1,
                    "effective_piece_qty": required, "yield_per_sheet": 1,
                    "authoritative_order_sheet_qty": required,
                })
                lines.append({**_component_payload(component.id, requisition_qty=required),
                    "order_item_id": item_id, "purchase_total_sheet_qty": required,
                    "order_purpose_sheet_qty": required, "stock_purpose_sheet_qty": 0,
                    "purpose_plan_version": 1, "purpose_plan_fingerprint": fingerprint})
                expected_sources[component.id] = (product_id, required)

        ordered = client.post("/api/requisition/batches", json={
            "request_key": "drawing-bom-two-sources", "supplier_name": "N039 供应商", "items": lines,
        })
        assert ordered.status_code == 201, ordered.text
        batch_id = ordered.json()["id"]
        frozen_sources = []
        expected_release_by_material_item = {}
        with factory() as db:
            rows = db.execute(select(RequisitionItem, RequisitionItemBomSource, PurchasePurposeSourceSnapshot)
                .join(RequisitionItemBomSource, RequisitionItemBomSource.requisition_item_id == RequisitionItem.id)
                .join(PurchasePurposeSourceSnapshot,
                    PurchasePurposeSourceSnapshot.material_requisition_item_id == RequisitionItem.id)
                .where(RequisitionItem.requisition_id == batch_id)
                .order_by(RequisitionItem.id)).all()
            assert len(rows) == 2
            for line, bom_source, snapshot in rows:
                product_id, required = expected_sources[bom_source.sales_order_item_bom_component_id]
                assert line.purpose_contract_status == "frozen"
                assert snapshot.pieces_per_finished_snapshot == (2 if product_id == 2 else 3)
                frozen_sources.append(FrozenSource(
                    source_key=snapshot.source_key, route_key=f"r{line.id}", supplier_item_id=line.id,
                    source_version=line.version, purpose_snapshot_id=snapshot.id,
                    purpose_snapshot_version=snapshot.snapshot_version, receipt_plan_fingerprint=snapshot.preview_fingerprint,
                    component_type=snapshot.component_type, material_id=material_id,
                    order_purpose_sheet_qty=required, reserve_purpose_sheet_qty=0,
                ))
                expected_release_by_material_item[line.id] = releases[product_id]

        paper_url = f"/api/requisition/batches/{batch_id}/production-print-package"
        paper_params = {"item_ids": ",".join(str(key) for key in expected_release_by_material_item)}

        def assert_component_cards(package):
            assert len(package["cards"]) == 2
            actual = {}
            for card in package["cards"]:
                assert len(card["components"]) == len(card["managed_drawings"]) == 1
                component = card["components"][0]
                drawing = card["managed_drawings"][0]
                assert component["managed_drawing"] == drawing
                assert drawing["print_objects"] == []
                actual[component["material_requisition_item_id"]] = drawing["release_id"]
            assert actual == expected_release_by_material_item

        planned = client.get(paper_url, params=paper_params)
        assert planned.status_code == 200, planned.text
        assert_component_cards(planned.json())
        for index, source in enumerate(frozen_sources):
            price = _freeze_receipt_fact(client, source, idempotency_key=f"drawing-bom-price-{index}")
            assert price.status_code == 200, price.text
            received = _receive(client, source, price.json(), quantity=source.order_purpose_sheet_qty,
                idempotency_key=f"drawing-bom-receive-{index}")
            assert received.status_code == 200, received.text
            allocation = received.json()["purpose_allocation"]
            assert allocation["order_sheet_delta"] == source.order_purpose_sheet_qty
            assert allocation["reserve_sheet_delta"] == 0
            # The established gate is min(4 / 2, 6 / 3), not sum(4, 6).
            assert allocation["theoretical_finished_delta"] == (0 if index == 0 else 2)
            assert allocation["theoretical_finished_cumulative"] == (0 if index == 0 else 2)
            with factory() as db:
                assert db.scalar(select(func.count()).select_from(ProductionCompletion).where(
                    ProductionCompletion.order_item_id == item_id)) == index
                receipt = db.scalar(select(IncomingReceiptItem).where(
                    IncomingReceiptItem.requisition_item_id == source.supplier_item_id))
                assert receipt is not None
                receipt_id = receipt.id
            actual = client.get(f"/api/incoming/receipt-items/{receipt_id}/production-card")
            assert actual.status_code == 200, actual.text
            receipt_drawings = [drawing for card in actual.json()["cards"] for drawing in card["managed_drawings"]]
            assert len(receipt_drawings) == 1
            assert receipt_drawings[0]["release_id"] == expected_release_by_material_item[source.supplier_item_id]

        after = client.get(paper_url, params=paper_params)
        assert after.status_code == 200, after.text
        assert_component_cards(after.json())
        with factory() as db:
            for product_id, task_id in task_ids.items():
                assert db.get(ProductionTaskDrawing, task_id).release_id == releases[product_id]


def test_bom_freeze_counts_two_physical_pieces_per_component_once(composite_requisition_app):
    """Real batch freeze retains 2 components × 2 pieces = 4 sheets per set."""
    from tests.test_n039_composite_bom_requisition import _login as login_composite
    from tests.test_n039_composite_bom_requisition import _component_payload, _parent_payload
    from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent
    from app.models.requisition import RequisitionItem
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot

    app, factory = composite_requisition_app
    with factory() as db:
        component = db.get(SalesOrderItemBomComponent, 1)
        component.snapshot_component_splice_mode = "double"
        component.snapshot_component_pieces_per_box = 2
        db.commit()
    with TestClient(app) as client:
        login_composite(client)
        created = client.post("/api/requisition/batches", json={
            "request_key": "drawing-bom-double-physical-factor", "supplier_name": "N039 供应商",
            "items": [_parent_payload(), _component_payload(1)],
        })
        assert created.status_code == 201, created.text
    with factory() as db:
        rows = db.execute(select(RequisitionItem, PurchasePurposeSourceSnapshot)
            .join(PurchasePurposeSourceSnapshot,
                PurchasePurposeSourceSnapshot.material_requisition_item_id == RequisitionItem.id)
            .where(RequisitionItem.requisition_id == created.json()["id"])).all()
        assert len(rows) == 2
        for line, purpose in rows:
            if purpose.source_bom_requisition_source_id is None:
                assert line.requisition_qty == 10
                assert purpose.pieces_per_finished_snapshot == line.pieces_per_box == 1
            else:
                source = db.get(RequisitionItemBomSource, purpose.source_bom_requisition_source_id)
                assert source.quantity_per_set == 4
                assert line.pieces_per_box == 2
                assert line.requisition_qty == purpose.order_purpose_sheet_qty == 40
                assert purpose.source_finished_qty_snapshot == 10
                assert purpose.pieces_per_finished_snapshot == 4
