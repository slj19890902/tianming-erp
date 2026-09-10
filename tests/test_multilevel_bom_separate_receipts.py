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
