from decimal import Decimal
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import products as api
from app.models.product import Product
from app.models.order import OrderItem
from app.models.production import ProductionTask
from app.models.warehouse_inventory import InventoryLot
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import plan_bom
from tests.test_multilevel_bom_order_entry import payload
from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources, _login, _freeze_receipt_fact, _receive
from tests.test_n039_composite_bom_requisition import composite_requisition_app
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


def test_public_body_edit_new_order_and_receipt(composite_requisition_app, _p181_published_map_identity):
    app, factory = composite_requisition_app
    app.include_router(api.router, prefix="/api/master/products")
    material_id, _ = seed_graph(factory, liner=True)
    with factory() as db:
        root = db.get(Product,1)
        fields = api._product_payload_snapshot(root)
        fields.update(expected_version=root.version, production_notes="自制本体组装")
        body = jsonable_encoder({"product":fields,"bom":{"expected_version":root.version,
            "inventory_mode":"manufactured", "components":[{"component_product_id":4,
                "quantity_per_set":1,"inventory_relation":"assembly"}]}})
    with TestClient(app) as client:
        _login(client)
        saved = client.put("/api/master/products/1/with-bom", json=body)
        if saved.status_code == 409:
            detail = saved.json()["detail"]
            assert detail["code"] == "MASTER_CHANGE_CONFIRMATION_REQUIRED", saved.text
            body["product"]["confirmation_token"] = detail["confirmation_token"]
            saved = client.put("/api/master/products/1/with-bom", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["bom"]["components"][0]["inventory_relation"] == "assembly"
        created = client.post("/api/orders", json=payload(factory,key="public-body-order"))
        assert created.status_code == 201, created.text
        iid = created.json()["items"][0]["id"]
        with factory() as db:
            compiled = read_compiled_order_bom(db,iid)
            assert dict(plan_bom(compiled.graph,10).picking) == {1:10}
            assert dict(plan_bom(read_compiled_order_bom(db,1).graph,10).picking) == {1:10,4:10}
            snapshots = [(s.id,s.component_product_id) for s in compiled.snapshots if s.component_product_id != 4]
        for index, source in enumerate(purchase_sources(client,factory,material_id,snapshots,order_item_id=iid)):
            fact = _freeze_receipt_fact(client,source,idempotency_key=f"public-body-price-{index}",unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            received = _receive(client,source,fact.json(),quantity=source.order_purpose_sheet_qty,
                idempotency_key=f"public-body-receive-{index}")
            assert received.status_code == 200, received.text
        with factory() as db:
            from app.services.multilevel_bom_receipts import own_output_lots
            lots = own_output_lots(db,iid)
            bodies = [l for l in lots if l.inventory_type == "assembly_body"]
            assert len(bodies)==1 and bodies[0].quantity_consumed==10 and bodies[0].quantity_reserved==0
            finished = [l for l in lots if l.finished_detail and l.finished_detail.product_id==1]
            assert sum(l.quantity_reserved for l in finished)==10
            task = db.scalar(select(ProductionTask).where(ProductionTask.order_item_id==iid,
                ProductionTask.sales_order_item_bom_component_id.is_(None)))
            assert task.finished_coverage_snapshot==10 and task.status=="completed"
