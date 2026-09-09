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
def test_real_order_api_freezes_graph_and_single_main_task(composite_requisition_app, _p181_published_map_identity, liner):
    app, factory = composite_requisition_app
    from app.api.deliveries import router
    app.include_router(router, prefix="/api/deliveries")
    material_id, _ = seed_graph(factory, liner=liner)
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
        dispatched = client.put(f"/api/deliveries/{did}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        with factory() as db:
            assert db.get(OrderItem, item_id).delivered_quantity == 4
            assert db.get(OrderItem, 1).delivered_quantity == 0
        cancelled = client.put(f"/api/deliveries/{did}/cancel", json={"reason": "隔离验收撤销"})
        assert cancelled.status_code == 200, cancelled.text
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
