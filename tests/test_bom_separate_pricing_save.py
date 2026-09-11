from fastapi.testclient import TestClient
from fastapi.encoders import jsonable_encoder

from app.api import products as api
from app.models.product import Product
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity
from tests.test_multilevel_bom_receipt_flow import seed_graph


def test_admin_separate_component_pricing_survives_save_reopen_resave(
    composite_requisition_app, _p181_published_map_identity,
):
    app, factory = composite_requisition_app
    app.include_router(api.router, prefix="/api/master/products")
    seed_graph(factory)
    with factory() as db:
        fields = api._product_payload_snapshot(db.get(Product, 1))
    fields.update(product_code="SEPARATE-PRICED-NEW", customer_material_code="SEPARATE-PRICED-NEW",
                  combination_mode="component_priced", composite_fulfillment_mode="component_delivery",
                  is_virtual_composite_parent=False)
    bom = {"expected_version": 1, "inventory_mode": "separate", "material_mode": "expand_children",
           "delivery_mode": "components", "components": [
               {"component_product_id": pid, "quantity_per_set": qty, "inventory_relation": "accompany"}
               for pid, qty in [(2, 3), (3, 4)]]}
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/master/products/with-bom", json=jsonable_encoder({"product": fields, "bom": bom}))
        assert response.status_code == 201, response.text
        product = response.json()["product"]
        assert product["combination_mode"] == "component_priced"
        reopened = client.get(f"/api/master/products/{product['id']}")
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["combination_mode"] == "component_priced"
        with factory() as db:
            fields = api._product_payload_snapshot(db.get(Product, product["id"]))
        fields["expected_version"] = product["version"]
        bom["expected_version"] = product["version"]
        body = jsonable_encoder({"product": fields, "bom": bom})
        saved = client.put(f"/api/master/products/{product['id']}/with-bom", json=body)
        if saved.status_code == 409:
            detail = saved.json()["detail"]
            assert detail["code"] == "MASTER_CHANGE_CONFIRMATION_REQUIRED"
            # The first mode transition leaves old unused parent fields visible.
            # Confirm their clearing through the normal administrator protocol.
            body["product"]["confirmation_token"] = detail["confirmation_token"]
            saved = client.put(f"/api/master/products/{product['id']}/with-bom", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["product"]["combination_mode"] == "component_priced"
