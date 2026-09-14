from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from pydantic import ValidationError

from test_phase192_layer_flute_production_notes import v192_app, _login
from test_product_readiness import _product
from app.api.materials import MaterialComposePreviewPayload
from app.models.material import Material
from app.services.flute_mapping import validate_flute_for_write
from app.services.product_readiness import product_readiness


def test_cardstock_compose_save_price_and_identity(v192_app):
    app, factory, _ = v192_app
    from app.api.products import router as product_router
    app.include_router(product_router, prefix="/api/products")
    with TestClient(app) as client:
        _login(client)
        base = dict(supplier_name="鸣朋", code_char="H", paper_name="灰底白板",
                    gram_weight=400, color="white", paper_role="卡纸")
        r = client.post("/api/master/materials/paper-codes", json=base)
        assert r.status_code == 201, r.text
        paper_id = r.json()["id"]
        # Existing supplier H cannot be silently overwritten by the preset.
        assert client.post("/api/master/materials/paper-codes", json=base).status_code == 409
        form = dict(supplier_name="鸣朋", layer_count=1, material_code="H", usage_flute_type="NONE")
        r = client.post("/api/master/materials/compose/preview", json=form)
        assert r.status_code == 200, r.text
        assert r.json()["total_gram_weight"] == 400
        assert r.json()["layers"][0]["paper_name"] == "灰底白板"
        updated = client.put(f"/api/master/materials/paper-codes/{paper_id}", json={**base, "gram_weight":450})
        assert updated.status_code == 200 and updated.json()["gram_weight"] == 450
        assert client.post("/api/master/materials/compose/preview", json=form).json()["total_gram_weight"] == 450
        form.update(parsed_supplier_name="鸣朋", parsed_layer_count=1,
                    parsed_material_code="H", price_source="manual", quote_price="1.8")
        r = client.post("/api/master/materials/compose/save", json=form)
        assert r.status_code == 200, r.text
        with factory() as db:
            material = db.scalar(select(Material).where(Material.code == "H"))
            assert material.layer_count == 1 and material.flute_type == "NONE"
            assert material.is_white_face and material.basis_weight_description == "450g"
            assert material.quote_price == Decimal("1.8")
        r = client.get("/api/master/materials", params=dict(layer_count=1))
        assert r.status_code == 200 and "H" in r.text
        material_id = material.id
        update = dict(code="H", layer_count=1, flute_type="NONE", supplier_name="鸣朋",
            basis_weight_description="450g", paper_composition="灰底白板", quote_price="2.10",
            expected_version=material.version)
        r = client.put(f"/api/master/materials/{material_id}", json=update)
        if r.status_code == 409 and isinstance(r.json().get("detail"), dict):
            token = r.json()["detail"].get("confirmation_token")
            assert token, r.text
            r = client.put(f"/api/master/materials/{material_id}", json={**update, "confirmation_token":token})
        assert r.status_code == 200, r.text
        with factory() as db:
            assert db.get(Material, material_id).quote_price == Decimal("2.10")
        product = dict(customer_id=1, product_code="CARD408", customer_material_code="CARD408", product_name="卡纸外箱",
            box_category="normal", box_style="A1", material_id=material_id, layer_count=1,
            flute_type="NONE", length_mm=200, width_mm=100, height_mm=100,
            report_length_mm=620, report_width_mm=300, supply_mode="corrugated_production")
        r = client.post("/api/products", json=product)
        assert r.status_code == 201, r.text
        saved_product = r.json()
        assert saved_product["layer_count"] == 1 and saved_product["flute_type"] == "NONE"
        assert saved_product["material_id"] == material_id
        _login(client, "workshop")
        assert client.post("/api/master/materials/compose/save", json=form).status_code == 403


@pytest.mark.parametrize("layer,flute,valid", [(1,"NONE",True),(1,"B",False),
    (1,None,False),(3,"NONE",False),(5,"NONE",False),(3,"B",True),(7,"AAA",True)])
def test_cardstock_flute_contract(layer, flute, valid):
    assert (validate_flute_for_write(flute, layer) is None) is valid


def test_cardstock_readiness_and_wrong_layer():
    material = SimpleNamespace(code="H", supplier_name="鸣朋", layer_count=1, is_active=True)
    assert product_readiness(_product(material=material, layer_count=1, flute_type="NONE"))["ready"]
    with pytest.raises(ValidationError):
        MaterialComposePreviewPayload(supplier_name="鸣朋", material_code="H", layer_count=1, usage_flute_type="B")


def test_cardstock_ui_options_and_preset():
    html = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
    assert 'code_char:"H", paper_name:"灰底白板", gram_weight:400' in html
    assert '<option :value="1">卡纸</option>' in html
    assert 'categories.some(code => ["corrugated_board","coated_board"].includes(code))' in html
    assert '.some(code=>!["corrugated_board","coated_board"].includes(code))' in html


from tests.test_phase11_requisition import requisition_app
from tests.test_p0_19_receipt_auto_fin_ground import (
    _seed_material_and_staging, _seed_published_fin_ground_plan, _mock_fin_runtime_identity,
    _create_frozen_sources, _freeze_receipt_fact, _receive,
)


def test_cardstock_requisition_receipt_finished_and_frozen_price(requisition_app, monkeypatch):
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import InventoryLot
    app, factory = requisition_app
    _seed_material_and_staging(factory)
    location_id = _seed_published_fin_ground_plan(factory)
    _mock_fin_runtime_identity(factory, monkeypatch)
    with factory() as db:
        item = db.get(OrderItem, 1)
        material = db.get(Material, item.material_id)
        material.code = "H"
        material.layer_count, material.flute_type = 1, "NONE"
        material.basis_weight_description = "400g"
        material.is_white_face = True
        item.snapshot_material, item.layer_count, item.flute_type = "H", 1, "NONE"
        product = db.get(Product, item.product_id)
        product.layer_count, product.flute_type = 1, "NONE"
        db.commit()
    with TestClient(app) as client:
        from tests.test_phase11_requisition import _login as login
        login(client, "admin")
        source = _create_frozen_sources(client, factory, order_quantity=10,
            purchase_total=10, order_purpose=10, stock_purpose=0)[0]
        with factory() as db:
            from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
            frozen_line = db.get(SupplierRequisitionOrderItem, source.supplier_item_id)
            assert frozen_line.layer_count_snapshot == 1 and frozen_line.flute_type_snapshot == "NONE"
        fact = _freeze_receipt_fact(client, source, idempotency_key="cardstock408-price")
        assert fact.status_code == 200, fact.text
        received = _receive(client, source, fact.json(), quantity=10, idempotency_key="cardstock408-receipt")
        assert received.status_code == 200, received.text
        with factory() as db:
            completion = db.scalar(select(ProductionCompletion).where(ProductionCompletion.origin == "receipt_auto"))
            assert completion is not None
            lot = db.get(InventoryLot, completion.inventory_lot_id)
            assert lot.quantity_available + lot.quantity_reserved == 10
            assert lot.warehouse_location_id == location_id
            previous_cost = lot.cost_snapshot_detail_json
            db.get(Material, source.material_id).quote_price = Decimal("222")
            db.commit()
        replay = _receive(client, source, fact.json(), quantity=10, idempotency_key="cardstock408-receipt")
        assert replay.status_code == 200, replay.text
        with factory() as db:
            lot = db.scalar(select(InventoryLot).where(InventoryLot.id == completion.inventory_lot_id))
            assert lot.cost_snapshot_detail_json == previous_cost
