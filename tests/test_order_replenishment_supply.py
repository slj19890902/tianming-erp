from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from tests.test_stock_replenishment_flow import stock_replenishment_app
from tests.test_n039_composite_bom_requisition import composite_requisition_app
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


def seed(factory, *, quantities=(80, 80), sheets=100, product_id=1):
    from app.models.product import Product
    from app.models.order import Order, OrderItem
    from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
    from app.services.product_specification import product_dimension_specification
    from app.services.stock_replenishment import product_replenishment_defaults
    from app.services.stock_purchase_identity import capture
    import json
    now = datetime.utcnow() + timedelta(days=1)
    with factory() as db:
        product = db.get(Product, product_id)
        defaults = product_replenishment_defaults(product)
        source = StockReplenishmentOrder(order_number=f"PLANNED-{product_id}", request_hash="a" * 64,
            supplier_name=product.material.supplier_name, customer_id=product.customer_id,
            source_type="stock_warning", status="draft", created_by=1, created_at=now)
        item = StockReplenishmentOrderItem(target_inventory_type="semi_finished",
            procurement_route_snapshot="paperboard", reference_product_id=product.id,
            customer_id=product.customer_id, material_id=product.material_id,
            material_code_snapshot=product.material.code, normalized_material_code=product.material.code,
            product_name_snapshot=product.product_name, product_code_snapshot=product.product_code,
            layer_count=defaults["layer_count"], flute_type=defaults["flute_type"],
            report_length_mm=defaults["report_length_mm"], report_width_mm=defaults["report_width_mm"],
            crease_type=defaults["crease_type"], crease_left_mm=defaults["crease_left_mm"],
            crease_middle_mm=defaults["crease_middle_mm"], crease_right_mm=defaults["crease_right_mm"],
            sheet_type="creased_sheet" if defaults["crease_type"] == "压线" else "net_sheet",
            component_type="whole", pieces_per_box=defaults["pieces_per_box"],
            stock_yield_per_sheet=defaults["output_per_sheet"], quantity=sheets, stocked_quantity=0,
            sheet_cutting_snapshot=defaults.get("sheet_cutting_snapshot"), created_at=now)
        source.items = [item]
        item.production_snapshot_json = json.dumps(capture(product, item))
        db.add(source)
        orders = []
        for index, quantity in enumerate(quantities):
            order = Order(order_number=f"AFTER-{index}", customer_id=product.customer_id,
                order_date=date.today(), status="pending_production", created_at=now + timedelta(minutes=index + 1))
            order.items = [OrderItem(product_id=product.id, quantity=quantity, unit_price=Decimal("1"),
                subtotal=Decimal(quantity), snapshot_product_name=product.product_name,
                snapshot_product_code=product.product_code, snapshot_spec=product_dimension_specification(product),
                snapshot_material=product.material.code, snapshot_supplier_name=product.material.supplier_name,
                layer_count=defaults["layer_count"], flute_type=defaults["flute_type"], material_id=product.material_id,
                snapshot_report_length_mm=product.report_length_mm, snapshot_report_width_mm=product.report_width_mm,
                snapshot_crease_type=defaults["crease_type"], snapshot_crease_left_mm=defaults["crease_left_mm"],
                snapshot_crease_middle_mm=defaults["crease_middle_mm"], snapshot_crease_right_mm=defaults["crease_right_mm"],
                snapshot_pieces_per_box=defaults["pieces_per_box"], snapshot_splice_mode="single",
                special_process=defaults["cutting_mode"], sheet_cutting_settings_snapshot=product.sheet_cutting_settings,
                requisition_status="未报料", material_status="pending", created_at=order.created_at)]
            db.add(order)
            orders.append(order)
        db.commit()
        return item.id, [o.items[0].id for o in orders]


def test_pending_supply_covers_fifo_without_writing_stock(stock_replenishment_app):
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.api.requisition import _current_requisition_summary
    from app.services.order_replenishment_supply import allocations, summary
    app, factory = stock_replenishment_app
    source_id, ids = seed(factory)
    with factory() as db:
        before = [db.scalar(select(func.count()).select_from(model)) for model in (InventoryLot, InventoryReservation)]
        result = [_current_requisition_summary(db, db.get(OrderItem, iid)) for iid in ids]
        assert [r["replenishment_covered_piece_qty"] for r in result] == [80, 20]
        assert [r["requisition_qty"] for r in result] == [0, 60]
        assert sum(summary(db, iid)["covered_piece_quantity"] for iid in ids) == 100
        assert summary(db, ids[0])["pending_material_piece_quantity"] == 80
        assert [db.scalar(select(func.count()).select_from(model)) for model in (InventoryLot, InventoryReservation)] == before
        assert not db.new and not db.dirty
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"username": "admin", "password": "RolePass123!"})
        assert response.status_code == 200
        pending = client.get("/api/requisition/pending").json()
        order_rows = {r["item_id"]: r for r in pending["items"] if not r.get("stock_replenishment_item_id") and not r.get("is_merge_group")}
        assert ids[0] not in order_rows
        assert order_rows[ids[1]]["requisition_qty"] == 60


@pytest.mark.parametrize("change", ["size", "supplier", "customer", "void", "force_close", "before"])
def test_frozen_scope_and_cancel_recompute(stock_replenishment_app, change):
    from app.models.order import OrderItem, Order
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.services.order_replenishment_supply import summary
    _, factory = stock_replenishment_app
    source_id, ids = seed(factory)
    with factory() as db:
        item = db.get(OrderItem, ids[0])
        source = db.get(StockReplenishmentOrderItem, source_id)
        if change == "size": item.snapshot_report_width_mm += 1
        if change == "supplier": item.snapshot_supplier_name = "其他供应商"
        if change == "customer": source.customer_id = None
        if change == "void": source.order.status = "voided"
        if change == "force_close": item.is_force_closed = True
        if change == "before": item.order.created_at = source.order.created_at - timedelta(minutes=1)
        db.flush()
        assert summary(db, ids[0])["covered_piece_quantity"] == 0
        assert summary(db, ids[1])["covered_piece_quantity"] == (0 if change in {"customer", "void"} else 80)


def test_direct_client_cannot_create_duplicate_purchase_for_covered_order(stock_replenishment_app):
    from tests.test_phase11_requisition import _preview_supplier_order_draft, _save_supplier_order_draft
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    app, factory = stock_replenishment_app
    _, ids = seed(factory)
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": "RolePass123!"}).status_code == 200
        response = client.post("/api/requisition/supplier-orders/preview-from-pending-selection",
            json={"selections": [{"type": "order_item", "order_item_id": ids[0]}]})
        assert response.status_code in (409, 400), response.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SupplierRequisitionOrder)) == 0


@pytest.mark.parametrize("mode", ["normal", "two_piece", "partial_receipt", "short_close", "own_purchase", "concurrent_purchase", "fault_replay", "cancel_first", "cancel_first_own"])
def test_confirm_receipt_processing_and_short_output_convert_without_double_count(stock_replenishment_app, mode, monkeypatch):
    from tests.test_stock_replenishment_flow import _confirm_replenishment_purchase
    from app.api.stock_preparation import router
    from app.models.order import OrderItem
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.stock_preparation import StockPreparationJob
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.api.requisition import _current_requisition_summary
    from app.services.order_replenishment_supply import summary, reconcile_outputs
    from app.services.warehouse_inventory import active_finished_reserved_qty
    from app.services.semi_finished_inventory import active_semi_reserved_piece_qty
    app, factory = stock_replenishment_app
    app.include_router(router, prefix="/api/production")
    if mode == "two_piece":
        from app.models.product import Product
        with factory() as db:
            db.get(Product, 1).pieces_per_box = 2
            db.commit()
    source_id, ids = seed(factory, quantities=(40, 40) if mode == "two_piece" else (80, 80))
    received_quantity = 80 if mode == "partial_receipt" else 90 if mode == "short_close" else 100
    actual_output = 50 if mode == "two_piece" else 80 if mode == "partial_receipt" else 100 if mode.startswith("cancel_first") else 90
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": "RolePass123!"}).status_code == 200
        _confirm_replenishment_purchase(client, {"items": [{"id": source_id}]})
        with factory() as db:
            assert [summary(db, iid)["incoming_piece_quantity"] for iid in ids] == [80, 20]
        if mode in {"own_purchase", "concurrent_purchase", "cancel_first_own"}:
            from tests.test_phase11_requisition import _preview_supplier_order_draft, _save_supplier_order_draft
            draft = _preview_supplier_order_draft(client, [{"type": "order_item", "order_item_id": ids[1]}])
            if mode == "concurrent_purchase":
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: _save_supplier_order_draft(client, draft), range(2)))
                assert all(response.status_code == 201 for response in results), [response.text for response in results]
            else:
                purchased = _save_supplier_order_draft(client, draft)
                assert purchased.status_code == 201, purchased.text
            assert _save_supplier_order_draft(client, draft).status_code == 201
        received = client.put(f"/api/incoming/receive/sr{source_id}",
            json={"received_quantity": received_quantity, "idempotency_key": "supply-receipt-100",
                **({"resolution_action": "accept_short" if mode == "short_close" else "await_supplier"} if received_quantity < 100 else {})})
        assert received.status_code == 200, received.text
        rid = received.json()["receipt_item_id"]
        with factory() as db:
            assert [summary(db, iid)["processing_piece_quantity"] for iid in ids] == ([80, 0] if mode == "partial_receipt" else [80, 10] if mode == "short_close" else [80, 20])
            assert [active_finished_reserved_qty(db, iid) for iid in ids] == [0, 0]
            job = db.scalar(select(StockPreparationJob).where(StockPreparationJob.receipt_item_id == rid))
            from app.models.incoming_receipt import IncomingReceiptItem
            lot = db.get(InventoryLot, db.get(IncomingReceiptItem, rid).received_inventory_lot_id)
            assert lot.quantity_available == 0 and lot.quantity_reserved == received_quantity
            body = dict(action="complete", operation_key="supply-actual-90", lot_version=lot.version,
                job_id=job.id, job_version=job.version, actual_output=actual_output,
                actual_input_quantity=received_quantity, location_id=7, layout_version=1)
        if mode == "fault_replay":
            from app.services import audit_log
            original = audit_log.append_audit_event
            def fail(*args, **kwargs):
                if kwargs.get("action_code") == "replenishment_supply.output_reserved":
                    raise RuntimeError("synthetic allocation audit failure")
                return original(*args, **kwargs)
            with monkeypatch.context() as patch:
                patch.setattr(audit_log, "append_audit_event", fail)
                failed = client.post(f"/api/production/stock-preparation/{rid}/actions", json=body)
                assert failed.status_code == 500, failed.text
            with factory() as db:
                assert db.get(StockPreparationJob, job.id).status == "pending"
                assert db.get(InventoryLot, lot.id).quantity_reserved == 100
                assert db.scalar(select(func.count()).select_from(InventoryLot)) == 1
        complete = client.post(f"/api/production/stock-preparation/{rid}/actions", json=body)
        assert complete.status_code == 200, complete.text
        replay = client.post(f"/api/production/stock-preparation/{rid}/actions", json=body)
        assert replay.status_code == 200, replay.text
        with factory() as db:
            assert [active_finished_reserved_qty(db, iid) for iid in ids] == [0, 0]
            assert [active_semi_reserved_piece_qty(db, order_item_id=iid, component_type="whole") for iid in ids] == ([80, 0] if mode == "partial_receipt" else [80, 20] if mode == "two_piece" or mode.startswith("cancel_first") else [80, 10])
            assert [summary(db, iid)["covered_piece_quantity"] for iid in ids] == ([0, 20] if mode == "partial_receipt" else [0, 0])
            from app.api.requisition import _remaining_supplier_requisition_summary
            assert [_remaining_supplier_requisition_summary(db, db.get(OrderItem, iid))["requisition_qty"] for iid in ids] == ([0, 0] if mode == "cancel_first_own" else [0, 60] if mode in {"partial_receipt", "two_piece", "cancel_first"} else [0, 10] if mode in {"own_purchase", "concurrent_purchase"} else [0, 70])
            assert reconcile_outputs(db, operator_id=1) == []
            job = db.scalar(select(StockPreparationJob).where(StockPreparationJob.receipt_item_id == rid))
            output = db.get(InventoryLot, job.output_lot_id)
            assert (output.quantity_available, output.quantity_reserved) == (0, actual_output)
            assert db.scalar(select(func.count()).select_from(InventoryReservation).where(
                InventoryReservation.inventory_lot_id == output.id)) == (1 if mode == "partial_receipt" else 2)
        if mode.startswith("cancel_first"):
            from app.api.orders import router as orders_router
            app.include_router(orders_router, prefix="/api/orders")
            with factory() as db:
                order_id = db.get(OrderItem, ids[0]).order_id
            cancelled = client.put(f"/api/orders/{order_id}/status", json={"status": "cancelled"})
            assert cancelled.status_code == 200, cancelled.text
            with factory() as db:
                assert [active_semi_reserved_piece_qty(db, order_item_id=iid, component_type="whole") for iid in ids] == ([0, 20] if mode == "cancel_first_own" else [0, 80])
                output = db.get(InventoryLot, output.id)
                assert (output.quantity_available, output.quantity_reserved) == ((80, 20) if mode == "cancel_first_own" else (20, 80))
        elif mode in {"normal", "two_piece"}:
            from app.services.semi_finished_inventory import consume_semi_finished_reservation
            from app.services.warehouse_inventory import WarehouseInventoryError
            with factory() as db:
                reservation = db.scalar(select(InventoryReservation).where(InventoryReservation.order_item_id == ids[0],
                    InventoryReservation.inventory_lot_id == output.id))
                output = db.get(InventoryLot, output.id)
                mutation = consume_semi_finished_reservation(db, reservation_id=reservation.id,
                    stock_quantity=reservation.reserved_stock_quantity, expected_version=output.version,
                    operator_id=1, idempotency_key="actual-source-consume", reason="合成后续成品确认消耗")
                assert mutation.reservation.status == "consumed"
                assert mutation.reservation.consumed_requirement_quantity == 80
                db.rollback()


def test_bom_component_fifo_uses_frozen_recipe(composite_requisition_app, _p181_published_map_identity):
    from tests.test_multilevel_bom_receipt_flow import seed_graph
    from app.models.order import OrderItem, Order
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.api.requisition import _bom_pending_component_requirements
    from app.services.order_replenishment_supply import summary
    _, factory = composite_requisition_app
    seed_graph(factory, quantity=20)
    source2, _ = seed(factory, quantities=(), product_id=2)
    source3, _ = seed(factory, quantities=(), product_id=3)
    with factory() as db:
        item = db.get(OrderItem, 1)
        source = db.get(StockReplenishmentOrderItem, source3)
        item.order.created_at = source.order.created_at + timedelta(minutes=1)
        item.created_at = item.order.created_at
        db.commit()
        rows = _bom_pending_component_requirements(db, item)
        assert [row["replenishment_covered_piece_qty"] for row in rows] == [60, 80]
        assert [row["requisition_qty"] for row in rows] == [0, 0]
        assert summary(db, 1)["covered_piece_quantity"] == 140
        # The graph's real inventory coverage and physical assembly do not change.
        from app.services.multilevel_bom_requirements import read_graph_requirements
        graph = read_graph_requirements(db, 1)
        assert all(value == 0 for value in graph.finished_units.values())
        assert {p.product_id: p.make_units for p in graph.plan.products}[1] == 20
        assert summary(db, 1)["covered_finished_quantity"] == 20
        assert summary(db, 1)["remaining_purchase_finished_quantity"] == 0


def test_two_piece_plan_rounds_each_batch_and_keeps_actual_output_physical(stock_replenishment_app):
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.api.requisition import _current_requisition_summary
    from app.services.order_replenishment_supply import summary
    _, factory = stock_replenishment_app
    with factory() as db:
        db.get(Product, 1).pieces_per_box = 2
        db.commit()
    _, ids = seed(factory, quantities=(51,), sheets=101)
    with factory() as db:
        req = _current_requisition_summary(db, db.get(OrderItem, ids[0]))
        assert req["required_piece_qty"] == 102
        assert req["replenishment_covered_piece_qty"] == 100
        assert req["requisition_qty"] == 2
        result = summary(db, ids[0])
        assert result["covered_finished_quantity"] == result["total_coverable_finished_quantity"] == 50
        assert result["remaining_purchase_finished_quantity"] == 1


def test_bom_real_processed_children_use_product_units_and_still_require_assembly(
    composite_requisition_app, _p181_published_map_identity
):
    from tests.test_multilevel_bom_receipt_flow import seed_graph
    from tests.test_stock_replenishment_flow import _confirm_replenishment_purchase
    from tests.test_n039_composite_bom_requisition import _login
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.user import User
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.stock_preparation import StockPreparationJob
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
    from app.services.multilevel_bom_orders import freeze_master_order_bom
    from app.services.multilevel_bom_requirements import read_graph_requirements
    from app.services.warehouse_inventory import active_finished_reserved_qty
    from app.api.stock_preparation import router
    app, factory = composite_requisition_app
    app.include_router(router, prefix="/api/production")
    seed_graph(factory, two_piece=True, quantity=40)
    with factory() as db:
        from app.models.material import Material
        from app.models.order import Order
        db.get(Material, db.get(Product, 2).material_id).price_unit = "元/㎡"
        db.get(Product, 2).pieces_per_box = 2
        db.get(OrderItem, 1).is_force_closed = True
        later = Order(order_number="BOM-PPB2-AFTER", customer_id=1, order_date=date.today(), status="pending_production")
        later.items = [OrderItem(product_id=1, quantity=40, unit_price=Decimal("1"), subtotal=Decimal("40"),
            snapshot_product_name="双拼组件组套", snapshot_product_code="KIT-001",
            sales_unit_snapshot="只", composite_fulfillment_mode_snapshot="parent_delivery",
            material_status="pending", requisition_status="未报料")]
        db.add(later)
        db.flush()
        iid = later.items[0].id
        freeze_master_order_bom(db, order_item_id=iid, actor=db.get(User, 1))
        db.commit()
    sources = [seed(factory, quantities=(), product_id=pid)[0] for pid in (2, 3)]
    with factory() as db:
        item = db.get(OrderItem, iid)
        item.order.created_at = db.get(StockReplenishmentOrderItem, sources[-1]).order.created_at + timedelta(minutes=1)
        item.created_at = item.order.created_at
        raw = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code == "P181-RAW-STAGE"))
        for source_id in sources:
            db.get(StockReplenishmentOrderItem, source_id).location_id = raw.id
        db.commit()
        raw_id, layout_version = raw.id, raw.floor3_layout.version
    with TestClient(app) as client:
        _login(client)
        _confirm_replenishment_purchase(client, {"items": [{"id": sid} for sid in sources]})
        for source_id, actual in zip(sources, (50, 100)):
            received = client.put(f"/api/incoming/receive/sr{source_id}", json={
                "received_quantity": 100, "idempotency_key": f"bom-supply-in-{source_id}"})
            assert received.status_code == 200, received.text
            rid = received.json()["receipt_item_id"]
            with factory() as db:
                job = db.scalar(select(StockPreparationJob).where(StockPreparationJob.receipt_item_id == rid))
                lot = db.get(InventoryLot, db.get(IncomingReceiptItem, rid).received_inventory_lot_id)
                body = dict(action="complete", operation_key=f"bom-supply-out-{source_id}",
                    lot_version=lot.version, job_id=job.id, job_version=job.version,
                    actual_output=actual, actual_input_quantity=100, location_id=raw_id, layout_version=layout_version)
            completed = client.post(f"/api/production/stock-preparation/{rid}/actions", json=body)
            assert completed.status_code == 200, completed.text
            with factory() as db:
                graph = read_graph_requirements(db, iid)
                assert graph.finished_units[2] == 40
                assert graph.finished_units.get(3, 0) == (0 if actual == 50 else 40)
                assert active_finished_reserved_qty(db, iid) == 0
                assert next(p.make_units for p in graph.plan.products if p.product_id == 2) == 0
        with factory() as db:
            graph = read_graph_requirements(db, iid)
            assert graph.finished_units[2] == graph.finished_units[3] == 40
            assert next(p.make_units for p in graph.plan.products if p.product_id == 1) == 40


def test_mixed_semi_finished_semi_preserves_frozen_requirement(stock_replenishment_app):
    from tests.test_stock_replenishment_flow import _confirm_replenishment_purchase
    from app.api.stock_preparation import router
    from app.models.stock_preparation import StockPreparationJob
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import InventoryLot, OrderItemSemiRequirement
    from app.models.order import OrderItem
    from app.services.warehouse_inventory import active_finished_reserved_qty
    from app.services.semi_finished_inventory import active_semi_reserved_piece_qty
    from app.api.requisition import _remaining_supplier_requisition_summary
    app, factory = stock_replenishment_app
    app.include_router(router, prefix="/api/production")
    source_id, ids = seed(factory, quantities=(80,), sheets=80)
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": "RolePass123!"}).status_code == 200
        _confirm_replenishment_purchase(client, {"items": [{"id": source_id}]})
        received = client.put(f"/api/incoming/receive/sr{source_id}", json={
            "received_quantity": 80, "idempotency_key": "mixed-supply-in"})
        assert received.status_code == 200, received.text
        rid = received.json()["receipt_item_id"]
        for sequence, (quantity, output_kind, location_id) in enumerate(((20, "semi", 7), (30, "finished", 8), (30, "semi", 7))):
            with factory() as db:
                job = db.scalar(select(StockPreparationJob).where(StockPreparationJob.receipt_item_id == rid,
                    StockPreparationJob.status == "pending"))
                raw = db.get(InventoryLot, db.get(IncomingReceiptItem, rid).received_inventory_lot_id)
                body = dict(action="complete", operation_key=f"mixed-supply-out-{sequence}",
                    lot_version=raw.version, job_id=job.id, job_version=job.version,
                    actual_input_quantity=quantity, actual_output=quantity, output_kind=output_kind,
                    location_id=location_id, layout_version=1)
            completed = client.post(f"/api/production/stock-preparation/{rid}/actions", json=body)
            assert completed.status_code == 200, completed.text
        with factory() as db:
            requirement = db.scalar(select(OrderItemSemiRequirement).where(OrderItemSemiRequirement.order_item_id == ids[0]))
            assert requirement.required_piece_quantity == 80
            assert active_finished_reserved_qty(db, ids[0]) == 30
            assert active_semi_reserved_piece_qty(db, order_item_id=ids[0], component_type="whole") == 50
            assert _remaining_supplier_requisition_summary(db, db.get(OrderItem, ids[0]))["requisition_qty"] == 0


def test_finished_receipt_closed_original_follows_moved_family(stock_replenishment_app):
    from tests.test_stock_replenishment_flow import _confirm_replenishment_purchase
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot
    from app.services.warehouse_inventory import transfer_finished_lot_between_locations, active_finished_reserved_qty
    from app.services.order_replenishment_supply import summary, reconcile_outputs
    app, factory = stock_replenishment_app
    source_id, ids = seed(factory, quantities=(20,), sheets=30, product_id=2)
    with factory() as db:
        source = db.get(StockReplenishmentOrderItem, source_id)
        source.target_inventory_type, source.product_id = "finished", 2
        db.get(OrderItem, ids[0]).is_force_closed = True
        db.commit()
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": "RolePass123!"}).status_code == 200
        _confirm_replenishment_purchase(client, {"items": [{"id": source_id}]})
        received = client.put(f"/api/incoming/receive/sr{source_id}", json={
            "received_quantity": 30, "idempotency_key": "finished-family-in"})
        assert received.status_code == 200, received.text
        with factory() as db:
            original = db.get(InventoryLot, db.get(IncomingReceiptItem, received.json()["receipt_item_id"]).received_inventory_lot_id)
            transfer_finished_lot_between_locations(db, lot_id=original.id, expected_version=original.version,
                quantity=30, location_id=7, expected_target_layout_version=1, operator_id=1,
                idempotency_key="finished-family-move")
            db.get(OrderItem, ids[0]).is_force_closed = False
            db.commit()
            assert original.status == "closed"
            assert summary(db, ids[0])["physical_output_piece_quantity"] == 20
            assert reconcile_outputs(db, operator_id=1, order_item_ids=ids)
            assert active_finished_reserved_qty(db, ids[0]) == 20
            assert sum(l.quantity_available for l in db.scalars(select(InventoryLot))) == 10


def test_consumed_history_does_not_resolve_every_old_source(stock_replenishment_app, monkeypatch):
    from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
    from app.services import stock_purchase_identity
    from app.services.order_replenishment_supply import allocations, lock_supply
    _, factory = stock_replenishment_app
    source_id, ids = seed(factory)
    with factory() as db:
        source = db.get(StockReplenishmentOrderItem, source_id)
        values = {col.name: getattr(source, col.name) for col in source.__table__.columns
            if col.name not in {"id", "replenishment_order_id", "version"}}
        for index in range(300):
            history = StockReplenishmentOrder(order_number=f"CONSUMED-HISTORY-{index}", request_hash="b" * 64,
                supplier_name=source.order.supplier_name, customer_id=1, source_type="stock_warning",
                status="stocked", created_by=1, created_at=source.order.created_at)
            history.items = [StockReplenishmentOrderItem(**dict(values, stocked_quantity=100))]
            db.add(history)
        db.commit()
    calls = []
    original = stock_purchase_identity.resolve
    def counted(db, source, *args, **kwargs):
        if isinstance(source, StockReplenishmentOrderItem):
            calls.append(source.id)
        return original(db, source, *args, **kwargs)
    monkeypatch.setattr(stock_purchase_identity, "resolve", counted)
    with factory() as db:
        timestamp = db.get(StockReplenishmentOrderItem, source_id).order.created_at
        assert sum(row["covered_piece_quantity"] for rows in allocations(db, order_item_ids=ids).values() for row in rows) == 100
        assert calls == [source_id]
        lock_supply(db, order_item_ids=ids)
        assert db.get(StockReplenishmentOrderItem, source_id).order.created_at == timestamp
