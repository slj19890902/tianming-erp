import pytest
from decimal import Decimal
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.api import products as api
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_snapshot import dump_graph
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_multilevel_bom_receipt_flow import seed_graph
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


@pytest.mark.parametrize("create", [False, True])
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("parent_unit", ["套", "只"])
def test_atomic_product_and_bom_save(composite_requisition_app, _p181_published_map_identity, monkeypatch, create, fail, parent_unit):
    app, factory = composite_requisition_app
    app.include_router(api.router, prefix="/api/master/products")
    seed_graph(factory)
    def state():
        with factory() as db:
            root = db.get(Product, 1)
            return (root.version, root.production_notes, root.unit, db.scalar(select(func.count()).select_from(Product)),
                [(row.parent_product_id, row.component_product_id, row.quantity_per_set)
                 for row in db.scalars(select(ProductBomComponent).order_by(ProductBomComponent.id))],
                dump_graph(read_compiled_order_bom(db, 1).graph))
    before = state()
    with factory() as db:
        root = db.get(Product, 1)
        fields = api._product_payload_snapshot(root)
        fields["production_notes"] = "本次原子保存备注"
        fields["unit"] = parent_unit
        if create:
            fields.update(product_code="ATOMIC-NEW", customer_material_code="ATOMIC-NEW")
        else:
            fields["expected_version"] = root.version
        body = jsonable_encoder({"product": fields, "bom": {
            "expected_version": 1 if create else root.version,
            "inventory_mode": "assembled", "components": [
                {"component_product_id": 2, "quantity_per_set": 5, "inventory_relation": "assembly"},
                {"component_product_id": 3, "quantity_per_set": 6, "inventory_relation": "assembly"}]}})
    if fail:
        original = api.replace_product_bom
        def failure(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("isolated failure after product and BOM writes")
        monkeypatch.setattr(api, "replace_product_bom", failure)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        response = (client.post("/api/master/products/with-bom", json=body) if create else
                    client.put("/api/master/products/1/with-bom", json=body))
        if response.status_code == 409 and not create:
            detail = response.json()["detail"]
            assert detail["code"] == "MASTER_CHANGE_CONFIRMATION_REQUIRED"
            assert state() == before
            body["product"]["confirmation_token"] = detail["confirmation_token"]
            response = client.put("/api/master/products/1/with-bom", json=body)
        assert response.status_code == (500 if fail else 201 if create else 200), response.text
        if fail:
            assert state() == before
            return
        saved = response.json()["product"]
        assert saved["production_notes"] == fields["production_notes"]
        expected_unit = "套" if create else parent_unit
        assert saved["unit"] == expected_unit
        bom = response.json()["bom"]
        assert bom["version"] == saved["version"]
        assert sorted(Decimal(row["quantity_per_set"]) for row in bom["components"]) == [5, 6]
        with factory() as db:
            assert db.get(Product, saved["id"]).production_notes == fields["production_notes"]
            assert db.get(Product, saved["id"]).unit == expected_unit
            assert dump_graph(read_compiled_order_bom(db, 1).graph) == before[-1]


@pytest.mark.parametrize("invalid", ["stale", "mismatch", "cycle"])
def test_combined_save_rejects_invalid_bom_without_product_write(
        composite_requisition_app, _p181_published_map_identity, invalid):
    app, factory = composite_requisition_app
    app.include_router(api.router, prefix="/api/master/products")
    seed_graph(factory)
    with factory() as db:
        root = db.get(Product, 1)
        before = (root.version, root.production_notes)
        fields = api._product_payload_snapshot(root)
        fields.update(expected_version=root.version, production_notes="不得部分保存")
        bom = {"expected_version":root.version, "inventory_mode":"assembled",
               "components":[{"component_product_id":1 if invalid == "cycle" else 2,
                              "quantity_per_set":5, "inventory_relation":"assembly"}]}
        if invalid == "stale":
            fields["expected_version"] -= 1
            bom["expected_version"] -= 1
        if invalid == "mismatch":
            bom["expected_version"] += 1
    with TestClient(app) as client:
        _login(client)
        body = jsonable_encoder({"product":fields, "bom":bom})
        response = client.put("/api/master/products/1/with-bom", json=body)
        detail = response.json().get("detail")
        if isinstance(detail, dict) and detail.get("code") == "MASTER_CHANGE_CONFIRMATION_REQUIRED":
            body["product"]["confirmation_token"] = detail["confirmation_token"]
            response = client.put("/api/master/products/1/with-bom", json=body)
        assert response.status_code in (400, 409), response.text
    with factory() as db:
        root = db.get(Product, 1)
        assert (root.version, root.production_notes) == before
