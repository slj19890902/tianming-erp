"""A priced physical subassembly retains both commercial parent and its own BOM."""
from fastapi.testclient import TestClient
import pytest

from app.models.product import Product
from app.models.order import OrderItem
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import plan_bom
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity
from tests.test_multilevel_bom_receipt_flow import seed_graph


@pytest.mark.parametrize("short_received", [0, 5])
def test_assembled_child_keeps_upstream_price_and_internal_recipe(
    composite_requisition_app, _p181_published_map_identity, short_received,
):
    app, factory = composite_requisition_app
    material_id, _ = seed_graph(factory, liner=True)
    from app.api.deliveries import router as delivery_router
    from app.api.finance import router as finance_router
    app.include_router(delivery_router, prefix="/api/deliveries")
    app.include_router(finance_router, prefix="/api/finance")
    with factory() as db:
        parent = db.get(Product, 1)
        parent.combination_mode = "component_priced"
        parent_name = parent.product_name
        db.commit()
    with TestClient(app) as client:
        _login(client)
        payload = {
            "customer_id": 1, "order_date": "2026-09-11", "items": [{
                "product_id": 4, "quantity": 100, "unit_price": "8.50",
                "combination_mode_snapshot": "component_priced",
                "combination_role": "priced_component",
                "combination_group_key": "nested-commercial-100",
                "combination_parent_product_id": 1,
                "combination_parent_name_snapshot": parent_name,
                "combination_set_quantity_snapshot": 100,
                "combination_quantity_per_set_snapshot": 1,
            }],
        }
        # Being a physical composite does not bypass upstream membership/ratio.
        payload["items"][0]["combination_quantity_per_set_snapshot"] = 2
        rejected = client.post("/api/orders", json=payload)
        assert rejected.status_code == 400, rejected.text
        assert "每套数量" in rejected.json()["detail"]
        payload["items"][0]["combination_quantity_per_set_snapshot"] = 1
        created = client.post("/api/orders", json=payload)
        assert created.status_code == 201, created.text
        iid = created.json()["items"][0]["id"]
    with factory() as db:
        item = db.get(OrderItem, iid)
        assert item.combination_role == "priced_component"
        assert item.combination_parent_product_id == 1
        compiled = read_compiled_order_bom(db, iid)
        assert compiled.graph.root_id == 4
        assert dict(plan_bom(compiled.graph, 100).picking) == {4: 100}
        assert {r.component_product_id: r.required_piece_quantity for r in compiled.snapshots} == {
            4: 100, 2: 200, 3: 600,
        }
        sources = [(row.id, row.component_product_id) for row in compiled.snapshots
                   if row.component_product_id in {2, 3}]
        from app.models.product_bom import ProductBomComponent
        from sqlalchemy import select
        relation = db.scalar(select(ProductBomComponent).where(
            ProductBomComponent.parent_product_id == 1,
            ProductBomComponent.component_product_id == 4))
        relation.quantity_per_set = 7
        db.get(Product, 1).product_name = "后改上层名称"
        db.commit()
        assert item.combination_quantity_per_set_snapshot == 1
        assert item.combination_parent_name_snapshot == parent_name
    from tests.test_multilevel_bom_receipt_flow import purchase_sources
    from tests.test_p1_81_receipt_purpose_flow import _freeze_receipt_fact, _receive
    from tests.test_bom_commercial_settlement import _dispatch, _confirm_receipt
    from datetime import date
    from decimal import Decimal
    from app.models.finance import Statement
    with TestClient(app) as client:
        _login(client)
        for index, source in enumerate(purchase_sources(client, factory, material_id, sources, order_item_id=iid)):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"nested-price-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            for batch in range(2):
                received = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty // 2,
                                    idempotency_key=f"nested-receipt-{index}-{batch}")
                assert received.status_code == 200, received.text
        delivery = _dispatch(client, 1, [(iid, 25)])
        cancelled = client.put(f"/api/deliveries/{delivery['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        with factory() as db:
            assert db.get(OrderItem, iid).delivered_quantity == 0
        delivery = _dispatch(client, 1, [(iid, 25)])
        target, versions = None, {}
        if short_received:
            from app.services.ordered_finished_receipt_return import _return_location_candidates
            with factory() as db:
                from app.models.multilevel_bom import BomAssemblyInput
                from app.services.multilevel_bom_cutover_review import _row
                from app.services.multilevel_bom_requirements import read_graph_requirements
                original_materials = read_graph_requirements(db, iid).plan.materials
                original_inputs = {row.id: _row(row) for row in db.scalars(select(BomAssemblyInput))}
                location = next(iter(_return_location_candidates(db).values()))
                target = location.id
                versions = {iid: location.floor3_layout.version}
        _confirm_receipt(client, delivery, short_received=short_received,
                         return_location_id=target, return_layout_versions=versions)
        statement = client.post("/api/finance/statements", json={
            "customer_id": 1, "statement_month": date.today().strftime("%Y-%m"),
            "delivery_ids": [delivery["id"]],
        })
        assert statement.status_code == 201, statement.text
        with factory() as db:
            assert db.get(Statement, statement.json()["id"]).total_receivable == Decimal("212.50")-short_received*Decimal("8.50")
            if short_received:
                from app.models.warehouse_inventory import InventoryLot
                from app.services.multilevel_bom_cost_lineage import graph_material_sources
                returned = list(db.scalars(select(InventoryLot).where(
                    InventoryLot.warehouse_location_id == target,
                    InventoryLot.source_ref_type == "return_receipt_item")))
                assert sum(lot.quantity_reserved for lot in returned) == short_received
                assert all(lot.finished_detail.product_id == 4 and graph_material_sources(db, lot) for lot in returned)
                assert read_graph_requirements(db, iid).plan.materials == original_materials
                returned_ids = [lot.id for lot in returned]
                assert {row.id: _row(row) for row in db.scalars(select(BomAssemblyInput))} == original_inputs
        if short_received:
            remaining_original = _dispatch(client, 1, [(iid, 75)])
            _confirm_receipt(client, remaining_original)
            replacement = _dispatch(client, 1, [(iid, short_received)])
            _confirm_receipt(client, replacement)
            with factory() as db:
                assert sum(db.get(InventoryLot, lid).quantity_consumed for lid in returned_ids) == short_received
                assert db.get(OrderItem, iid).delivered_quantity == 100
                assert {row.id: _row(row) for row in db.scalars(select(BomAssemblyInput))} == original_inputs
            final_statement = client.post("/api/finance/statements", json={"customer_id": 1,
                "statement_month": date.today().strftime("%Y-%m"),
                "delivery_ids": [remaining_original["id"], replacement["id"]]})
            assert final_statement.status_code == 201, final_statement.text
            with factory() as db:
                assert db.get(Statement, final_statement.json()["id"]).total_receivable == Decimal("680.00")
