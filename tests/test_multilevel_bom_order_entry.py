import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.models.order import OrderItem
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.order_estimated_cost_snapshot import SalesOrderItemEstimatedCostSnapshot
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import plan_bom
from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact, _receive
from tests.test_multilevel_bom_external_receipts import purchase_app, prepare as prepare_external, receive


def payload(factory, *, key="new-real-bom"):
    with factory() as db:
        product = db.get(Product, 1)
        return {"customer_id": 1, "customer_po": key, "order_date": "2026-09-10",
            "delivery_date": "2026-09-20", "items": [{"client_line_id": key,
            "product_id": product.id, "product_code": product.product_code,
            "product_name": product.product_name, "specification": "匿名组合品",
            "quantity": 10, "unit_price": "100"}]}


@pytest.mark.parametrize("liner", [False, True])
def test_edit_order_sets_preserves_recipe_and_refreshes_demands(composite_requisition_app, _p181_published_map_identity, liner):
    from app.models.multilevel_bom import OrderBomGraph
    from app.services.multilevel_bom_requirements import read_graph_requirements
    app, factory = composite_requisition_app
    seed_graph(factory, liner=liner)
    body = payload(factory, key="edit-real-bom")
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=body)
        assert created.status_code == 201, created.text
        iid = created.json()["items"][0]["id"]
        with factory() as db:
            before_hash = db.get(OrderBomGraph, iid).content_hash
            item = db.get(OrderItem, iid)
            edit = dict(quantity=12, unit_price="100", product_code=item.snapshot_product_code,
                        product_name=item.snapshot_product_name, material=item.snapshot_material,
                        quantity_adjustment_idempotency_key="quantity-edit-once")
        edited = client.put(f"/api/orders/items/{iid}", json=edit)
        assert edited.status_code == 200, edited.text
        replay = client.put(f"/api/orders/items/{iid}", json=edit)
        assert replay.status_code == 200, replay.text
        with factory() as db:
            assert db.get(OrderBomGraph, iid).content_hash == before_hash
            requirements = read_graph_requirements(db, iid)
            assert requirements.order_quantity == 12
            assert dict(requirements.plan.picking) == ({1: 12, 4: 12} if liner else {1: 12})
            assert all(row.order_set_quantity == 10 for row in requirements.compiled.snapshots)
            tasks = list(db.scalars(select(ProductionTask).where(ProductionTask.order_item_id == iid)))
            assert len(tasks) == 1 and tasks[0].ordered_quantity_snapshot == 12
            costs = list(db.scalars(select(SalesOrderItemEstimatedCostSnapshot).where(
                SalesOrderItemEstimatedCostSnapshot.sales_order_item_id == iid)))
            assert sorted(row.order_quantity_snapshot for row in costs) == [10, 12]
        changed = client.put(f"/api/orders/items/{iid}", json={**edit, "quantity": 14})
        assert changed.status_code == 409, changed.text
        with factory() as db:
            assert db.get(OrderItem, iid).quantity == 12
            assert read_graph_requirements(db, iid).order_quantity == 12
        next_edit = client.put(f"/api/orders/items/{iid}", json={**edit, "quantity": 14,
            "quantity_adjustment_idempotency_key": "quantity-edit-next"})
        assert next_edit.status_code == 200, next_edit.text
        with factory() as db:
            assert read_graph_requirements(db, iid).order_quantity == 14


@pytest.mark.parametrize("production_edit", [False, True])
def test_edit_graph_quantity_cost_failure_rolls_back(composite_requisition_app, _p181_published_map_identity, monkeypatch, production_edit):
    from app.models.multilevel_bom import OrderBomProductionRevision
    from app.api import orders as api
    from app.models.product_bom import SalesOrderItemBomDemandAdjustment, SalesOrderItemBomComponent
    app, factory = composite_requisition_app
    seed_graph(factory, liner=True)
    body = payload(factory, key="edit-failure")
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        created = client.post("/api/orders", json=body)
        assert created.status_code == 201, created.text
        iid = created.json()["items"][0]["id"]
        original = api.freeze_order_item_estimated_cost
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("isolated cost failure")
        monkeypatch.setattr(api, "freeze_order_item_estimated_cost", fail)
        with factory() as db:
            item = db.get(OrderItem, iid)
            edit = dict(quantity=12, unit_price="100", product_code=item.snapshot_product_code,
                        product_name=item.snapshot_product_name, material=item.snapshot_material)
            if production_edit:
                edit.update(quantity=item.quantity, snapshot_report_width_mm=710, bom_production_expected_revision=0)
        response = client.put(f"/api/orders/items/{iid}", json=edit)
        assert response.status_code == 500
    with factory() as db:
        assert db.get(OrderItem, iid).quantity == 10
        assert db.scalar(select(func.count()).select_from(OrderBomProductionRevision).where(
            OrderBomProductionRevision.order_item_id == iid)) == 0
        assert db.scalar(select(func.count()).select_from(SalesOrderItemBomDemandAdjustment).join(
            SalesOrderItemBomComponent, SalesOrderItemBomComponent.id == SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id
        ).where(SalesOrderItemBomComponent.sales_order_item_id == iid)) == 0
        assert db.scalar(select(func.count()).select_from(SalesOrderItemEstimatedCostSnapshot).where(
            SalesOrderItemEstimatedCostSnapshot.sales_order_item_id == iid)) == 1
        assert db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == iid)).ordered_quantity_snapshot == 10


def test_edit_manufactured_root_updates_effective_material_dimensions(composite_requisition_app, _p181_published_map_identity):
    from app.models.multilevel_bom import OrderBomProductionRevision
    from app.models.product_bom import SalesOrderItemBomComponent, RequisitionItemBomSource
    from app.models.requisition import RequisitionItem
    from app.services.multilevel_bom_material_estimate import graph_material_estimate_inputs
    app, factory = composite_requisition_app
    material_id, _ = seed_graph(factory, liner=True)
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=payload(factory, key="edit-root-dimensions"))
        assert created.status_code == 201, created.text
        iid = created.json()["items"][0]["id"]
        with factory() as db:
            item = db.get(OrderItem, iid)
            original = read_compiled_order_bom(db, iid)
            root = next(row for row in original.snapshots if row.component_product_id == 1)
            width = int(root.snapshot_component_report_width_mm or 100) + 10
            root_id = root.id
            edit = dict(quantity=item.quantity, unit_price="100", product_code=item.snapshot_product_code,
                        product_name=item.snapshot_product_name, material=item.snapshot_material,
                        snapshot_report_width_mm=width, bom_production_expected_revision=0)
        response = client.put(f"/api/orders/items/{iid}", json=edit)
        assert response.status_code == 200, response.text
        assert response.json()["bom_production_revision"] == 1
        reopened = client.get(f"/api/orders/{created.json()['id']}")
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["items"][0]["bom_production_revision"] == 1
        assert client.put(f"/api/orders/items/{iid}", json=edit).status_code == 200
        stale = client.put(f"/api/orders/items/{iid}", json={**edit, "snapshot_report_width_mm": width + 10})
        assert stale.status_code == 409, stale.text
        with factory() as db:
            item = db.get(OrderItem, iid)
            assert item.snapshot_report_width_mm == width
            effective = read_compiled_order_bom(db, iid)
            root = next(row for row in effective.snapshots if row.component_product_id == 1)
            assert root.snapshot_component_report_width_mm == width
            assert db.get(SalesOrderItemBomComponent, root_id).snapshot_component_report_width_mm == width - 10
            revisions = list(db.scalars(select(OrderBomProductionRevision).where(OrderBomProductionRevision.order_item_id == iid)))
            assert len(revisions) == 1 and revisions[0].revision == 1
            sources, missing = graph_material_estimate_inputs(db, item)
            assert not missing
            assert next(row for row in sources if row["source_identity"]["product_id"] == 1)["width_mm"] == width
            snapshots = [(row.id, row.component_product_id) for row in effective.snapshots
                         if next(node for node in effective.graph.nodes if node.product_id == row.component_product_id).source == "manufactured"]
        pending = client.get("/api/requisition/pending")
        assert pending.status_code == 200, pending.text
        pending_item = next(row for row in pending.json()["items"] if row["item_id"] == iid)
        assert next(row for row in pending_item["component_requirements"] if row["snapshot_id"] == root_id)["report_width_mm"] == width
        sources = purchase_sources(client, factory, material_id, snapshots, order_item_id=iid)
        with factory() as db:
            requisition = db.scalar(select(RequisitionItem).join(RequisitionItemBomSource,
                RequisitionItemBomSource.requisition_item_id == RequisitionItem.id).where(
                    RequisitionItemBomSource.sales_order_item_bom_component_id == root_id))
            assert requisition.cardboard_width == width
        for index, source in enumerate(sources):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"revised-price-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            received = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
                                idempotency_key=f"revised-receipt-{index}")
            assert received.status_code == 200, received.text


def test_new_order_root_uses_order_material_and_notes_not_child_master(composite_requisition_app, _p181_published_map_identity):
    from app.models.material import Material
    app, factory = composite_requisition_app
    original_material_id, _ = seed_graph(factory, liner=True)
    with factory() as db:
        original = db.get(Material, original_material_id)
        alternate = Material(code="ORDER-ONLY-BOARD", supplier_name=original.supplier_name,
            layer_count=original.layer_count, flute_type=original.flute_type,
            quote_price=original.quote_price, price_unit=original.price_unit,
            purchase_currency=original.purchase_currency,
            purchase_tax_included=original.purchase_tax_included,
            purchase_tax_rate=original.purchase_tax_rate)
        db.add(alternate)
        db.flush()
        alternate_id = alternate.id
        db.commit()
    body = payload(factory, key="order-specific-root")
    body["items"][0].update(material_id=alternate_id, production_notes="本单标签朝外")
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=body)
        assert response.status_code == 201, response.text
    item_id = response.json()["items"][0]["id"]
    with factory() as db:
        item = db.get(OrderItem, item_id)
        assert item.material_id == alternate_id
        compiled = read_compiled_order_bom(db, item_id)
        rows = {row.component_product_id: row for row in compiled.snapshots}
        assert rows[1].snapshot_component_material_id == alternate_id
        assert rows[1].snapshot_component_production_notes == "本单标签朝外"
        assert rows[2].snapshot_component_material_id == original_material_id
        assert rows[3].snapshot_component_material_id == original_material_id
        assert db.get(Product, 1).material_id == original_material_id
        from app.services.multilevel_bom_material_estimate import graph_material_estimate_inputs
        sources, missing = graph_material_estimate_inputs(db, item)
        assert not missing
        assert next(row for row in sources if row["source_identity"]["product_id"] == 1)["material_id"] == alternate_id
        # Existing frozen orders are never rewritten to the new order's input.
        old = read_compiled_order_bom(db, 1)
        assert next(row for row in old.snapshots if row.component_product_id == 1).snapshot_component_material_id == original_material_id
        # Replay reads the frozen contract, never reinterprets even changed
        # order/master display data as permission to replace it.
        from app.models.user import User
        from app.services.multilevel_bom_external_freeze import freeze_order_procurement
        item.snapshot_production_notes = "后续输入"
        item.material_id = original_material_id
        replay = freeze_order_procurement(db, order_item_id=item_id, actor=db.get(User, 1), root_order_snapshot=True)
        root = next(row for row in replay.snapshots if row.component_product_id == 1)
        assert root.snapshot_component_material_id == alternate_id
        assert root.snapshot_component_production_notes == "本单标签朝外"


@pytest.mark.parametrize("liner", [False, True])
@pytest.mark.parametrize("legacy_root", [False, True])
@pytest.mark.parametrize("fulfillment_mode", ["parent_delivery", "component_delivery"])
def test_real_order_api_freezes_graph_and_single_main_task(composite_requisition_app, _p181_published_map_identity, liner, legacy_root, fulfillment_mode):
    app, factory = composite_requisition_app
    from app.api.deliveries import router
    app.include_router(router, prefix="/api/deliveries")
    material_id, _ = seed_graph(factory, liner=liner)
    with factory() as db:
        db.get(Product, 1).composite_fulfillment_mode = fulfillment_mode
        db.commit()
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload(factory))
        assert response.status_code == 201, response.text
    item_id = response.json()["items"][0]["id"]
    with factory() as db:
        compiled = read_compiled_order_bom(db, item_id)
        assert plan_bom(compiled.graph, 10).picking == (((1, 10), (4, 10)) if liner else ((1, 10),))
        tasks = list(db.scalars(select(ProductionTask).where(ProductionTask.order_item_id == item_id)))
        assert len(tasks) == 1 and tasks[0].sales_order_item_bom_component_id is None
        cost = db.scalar(select(SalesOrderItemEstimatedCostSnapshot).where(SalesOrderItemEstimatedCostSnapshot.sales_order_item_id == item_id))
        assert cost is not None and cost.rule_version == "multilevel-bom-estimated-v1"
        assert db.get(OrderItem, 1).quantity == 10
        # Existing order-edit callers still use this legacy helper. A no-op
        # refresh must not create a task for every physical graph node.
        from app.services.composite_bom_workflow import ensure_component_production_tasks
        ensure_component_production_tasks(db, item_id)
        db.flush()
        assert db.scalar(select(func.count()).select_from(ProductionTask).where(
            ProductionTask.order_item_id == item_id)) == 1
        source_ids = {node.product_id for node in compiled.graph.nodes if node.source == "manufactured"}
        expected_units = {node.product_id: node.unit for node in compiled.graph.nodes}
        picking_products = {pid for pid, _ in plan_bom(compiled.graph, 10).picking}
        picking_snapshots = {row.id for row in compiled.snapshots if row.component_product_id in picking_products}
        snapshots = [(row.id, row.component_product_id) for row in compiled.snapshots if row.component_product_id in source_ids]
    with TestClient(app) as client:
        _login(client)
        for index, source in enumerate(purchase_sources(client, factory, material_id, snapshots, order_item_id=item_id)):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"new-order-price-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            received = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
                                idempotency_key=f"new-order-receipt-{index}")
            assert received.status_code == 200, received.text
        created = client.post("/api/deliveries", json={"customer_id": 1, "delivery_date": "2026-09-10",
            "items": [{"order_item_id": item_id, "delivered_quantity": 4}]})
        assert created.status_code == 201, created.text
        did = created.json()["id"]
        if legacy_root:
            from app.models.warehouse_inventory import InventoryReservation
            with factory() as db:
                root_sid = next(row.id for row in read_compiled_order_bom(db, item_id).snapshots if row.component_product_id == 1)
                for reservation in db.scalars(select(InventoryReservation).where(
                    InventoryReservation.order_item_id == item_id,
                    InventoryReservation.sales_order_item_bom_component_id == root_sid)):
                    reservation.sales_order_item_bom_component_id = None
                db.commit()
        from app.api.deliveries import _delivery_list_page_context, _delivery_response
        def compare_page():
            with factory() as db:
                page = _delivery_list_page_context(db, [did])
                listed = _delivery_response(db, did, list_context=page)
                detailed = _delivery_response(db, did)
                for field in ("component_lines", "actual_goods_lines", "inventory_sources", "available_sets"):
                    assert listed["items"][0][field] == detailed["items"][0][field], field
                summaries = client.get("/api/deliveries", params={"view": "summary"})
                assert summaries.status_code == 200, summaries.text
                summary = next(row for row in summaries.json()["items"] if row["id"] == did)
                assert summary["total_actual_goods_quantity"] == detailed["total_actual_goods_quantity"]
        compare_page()
        dispatched = client.put(f"/api/deliveries/{did}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        compare_page()
        with factory() as db:
            for pid in picking_products:
                product = db.get(Product, pid)
                product.unit = "后续新单位"
                product.version += 1
            db.commit()
        detail = client.get(f"/api/deliveries/{did}")
        assert detail.status_code == 200, detail.text
        line = detail.json()["items"][0]
        full_page = client.get("/api/deliveries", params={"view": "full"})
        assert full_page.status_code == 200, full_page.text
        public_listed = next(row for row in full_page.json()["items"] if row["id"] == did)["items"][0]
        assert public_listed["component_lines"] == line["component_lines"]
        assert public_listed["inventory_sources"] == line["inventory_sources"]
        assert {row["component_product_id"] for row in line["component_lines"]} == picking_products
        assert all(row["unit"] == expected_units[row["component_product_id"]] for row in line["component_lines"])
        assert len(line["actual_goods_lines"]) == (1 if fulfillment_mode == "parent_delivery" else len(picking_products))
        goods = line["actual_goods_lines"][0]
        assert goods["line_type"] == ("parent" if fulfillment_mode == "parent_delivery" else "component")
        assert goods["quantity"] == 4
        assert goods["unit"] == expected_units[1]
        from app.api.deliveries import _delivery_list_page_context, _delivery_response
        with factory() as db:
            context = _delivery_list_page_context(db, [did])
            listed = _delivery_response(db, did, list_context=context)
            detailed = _delivery_response(db, did)
            for field in ("component_lines", "actual_goods_lines", "inventory_sources", "available_sets"):
                if field == "inventory_sources":
                    for a, b in zip(listed["items"][0][field], detailed["items"][0][field]):
                        assert a == b, {key: (a.get(key), b.get(key)) for key in a.keys() | b.keys() if a.get(key) != b.get(key)}
                assert listed["items"][0][field] == detailed["items"][0][field], field
        assert {row["component_snapshot_id"] for row in line["inventory_sources"]} == picking_snapshots
        assert all(row["location_id"] and row["quantity_to_pick_stock"] == 4 for row in line["inventory_sources"])
        printed = client.get(f"/api/deliveries/{did}/print")
        assert printed.status_code == 200, printed.text
        printed_line = printed.json()["items"][0]
        assert printed_line["actual_goods_lines"][0]["quantity"] == 4
        assert printed_line["actual_goods_lines"][0]["unit"] == expected_units[1]
        assert printed_line["unit"] == expected_units[1]
        with factory() as db:
            assert db.get(OrderItem, item_id).delivered_quantity == 4
            assert db.get(OrderItem, 1).delivered_quantity == 0
        cancelled = client.put(f"/api/deliveries/{did}/cancel", json={"reason": "隔离验收撤销"})
        assert cancelled.status_code == 200, cancelled.text
        compare_page()
        with factory() as db:
            assert db.get(OrderItem, item_id).delivered_quantity == 0


@pytest.mark.parametrize("stage", ["procurement", "cost"])
def test_new_order_failure_rolls_back_graph_tasks_cost_and_order(composite_requisition_app, _p181_published_map_identity, monkeypatch, stage):
    from app.api import orders as api
    from app.services import multilevel_bom_external_freeze as procurement
    from app.services.multilevel_bom_plan import BomPlanError
    from app.models.order import Order
    from app.models.multilevel_bom import OrderBomGraph
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.order_material_cost_snapshot import SalesOrderItemMaterialCostSnapshot
    app, factory = composite_requisition_app
    seed_graph(factory)
    models = (Order, OrderItem, OrderBomGraph, SalesOrderItemBomComponent, ProductionTask,
              SalesOrderItemMaterialCostSnapshot, SalesOrderItemEstimatedCostSnapshot)
    def counts():
        with factory() as db:
            return [db.scalar(select(func.count()).select_from(model)) for model in models]
    before = counts()
    target, name = ((procurement, "freeze_order_procurement") if stage == "procurement" else (api, "freeze_order_item_estimated_cost"))
    original = getattr(target, name)
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise BomPlanError("隔离故障：写入后失败")
    monkeypatch.setattr(target, name, fail)
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload(factory))
        assert response.status_code == 409, response.text
    assert counts() == before


@pytest.mark.parametrize("changed", [False, True])
def test_graph_demand_cannot_override_recipe_or_create_child_tasks(composite_requisition_app, _p181_published_map_identity, changed):
    from app.models.product_bom import ProductBomComponent
    app, factory = composite_requisition_app
    seed_graph(factory)
    body = payload(factory)
    with factory() as db:
        relation = db.scalar(select(ProductBomComponent).where(ProductBomComponent.parent_product_id == 1,
                                                              ProductBomComponent.component_product_id == 2))
        body["items"][0]["bom_component_demands"] = [{"product_bom_component_id": relation.id,
            "required_piece_quantity": 31 if changed else 30, "idempotency_key": "recipe-check"}]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=body)
        assert response.status_code == (409 if changed else 201), response.text
        if not changed:
            with factory() as db:
                item_id = response.json()["items"][0]["id"]
                tasks = list(db.scalars(select(ProductionTask).where(ProductionTask.order_item_id == item_id)))
                assert len(tasks) == 1 and tasks[0].sales_order_item_bom_component_id is None
                compiled = read_compiled_order_bom(db, item_id)
                sid = next(row.id for row in compiled.snapshots if row.component_product_id == 2)
            altered = client.put(f"/api/orders/items/{item_id}/bom-components/{sid}/demand", json={
                "required_piece_quantity": 31, "expected_required_piece_quantity": 30,
                "idempotency_key": "old-api-recipe-check"})
            assert altered.status_code == 409, altered.text


@pytest.mark.parametrize("direct", [False, True])
def test_public_external_order_freezes_links_then_receives_stock(purchase_app, _p181_published_map_identity, direct):
    from app.api.orders import router
    from app.models.order import Order
    from app.models.multilevel_bom import OrderBomExternalComponent
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
    from app.models.warehouse_inventory import InventoryLot
    from tests.test_p1_33c5_external_packaging_receiving import _login as login_external, _confirm
    from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging
    purchase_app.include_router(router, prefix="/api/orders")
    _seed_material_and_staging(purchase_app.state.session_factory)
    old_order_id, old_item_id, child_id = prepare_external(purchase_app, direct=direct)
    with purchase_app.state.session_factory() as db:
        root = db.get(Product, db.get(OrderItem, old_item_id).product_id)
        root_id = root.id
        body = {"customer_id": db.get(Order, old_order_id).customer_id,
            "customer_po": "public-external-graph", "order_date": "2026-09-10", "delivery_date": "2026-09-20",
            "items": [{"product_id": root.id, "product_code": root.product_code,
                       "product_name": root.product_name, "quantity": 10, "unit_price": "100"}]}
    with TestClient(purchase_app) as client:
        login_external(client, "purchase-admin")
        response = client.post("/api/orders", json=body)
        assert response.status_code == 201, response.text
        order_id, item_id = response.json()["id"], response.json()["items"][0]["id"]
        with purchase_app.state.session_factory() as db:
            links = list(db.scalars(select(OrderBomExternalComponent).where(OrderBomExternalComponent.order_item_id == item_id)))
            assert len(links) == 1 and links[0].product_id == child_id
        _confirm(client, order_id)
        with purchase_app.state.session_factory() as db:
            line = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == item_id))
            purchase_id, line_id, quantity = line.purchase_order_id, line.id, line.purchase_quantity
        received = receive(client, purchase_id, line_id, "public-external-receive", quantity)
        assert received.status_code == 200, received.text
        with purchase_app.state.session_factory() as db:
            lots = list(db.scalars(select(InventoryLot).where(InventoryLot.finished_detail.has(product_id=root_id))))
            assert sum(lot.quantity_reserved for lot in lots) == 10
