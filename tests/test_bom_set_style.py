import pytest
import shutil
import subprocess
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.api import products as api
from app.models.product import Product
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_multilevel_bom_receipt_flow import seed_graph
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


@pytest.mark.parametrize("invalid", [False, True])
@pytest.mark.parametrize("parent_unit", [None, "只", "套"])
def test_set_style_atomic_create(composite_requisition_app, _p181_published_map_identity, invalid, parent_unit):
    app, factory = composite_requisition_app
    app.include_router(api.router, prefix="/api/master/products")
    seed_graph(factory)
    with factory() as db:
        count = db.scalar(select(func.count()).select_from(Product))
        customer_id = db.get(Product, 1).customer_id
    fields = dict(customer_id=customer_id, product_code="SET-TEST", product_name="组套测试",
        box_style="BOM组合", box_category="normal", customer_material_code="SET-TEST", sale_unit_price="12", production_label_enabled=True,
        production_label_units_per_label=5)
    if parent_unit is not None:
        fields["unit"] = parent_unit
    expected_unit = "套"  # New assembled products follow the confirmed unit rule.
    bom = dict(expected_version=1, inventory_mode="assembled", material_mode="expand_children",
        delivery_mode="parent", components=[] if invalid else [
        dict(component_product_id=2, quantity_per_set=3, inventory_relation="assembly"),
        dict(component_product_id=3, quantity_per_set=4, inventory_relation="assembly")])
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/master/products/with-bom", json=dict(product=fields, bom=bom))
        if invalid:
            assert response.status_code in (400, 422), response.text
            with factory() as db:
                assert db.scalar(select(func.count()).select_from(Product)) == count
            return
        assert response.status_code == 201, response.text
        product = response.json()["product"]
        assert product["unit"] == expected_unit
        assert product["readiness"]["ready"], product["readiness"]
        assert product["material_id"] is None and product["report_length_mm"] is None
        assert product["is_virtual_composite_parent"] is False
        assert product["composite_fulfillment_mode"] == "parent_delivery"
        assert product["production_label_units_per_label"] == 5
        assert float(product["sale_unit_price"]) == 12
        from app.models.order import OrderItem
        from app.services.multilevel_bom_compile import compile_master_order_bom
        from app.services.production_label_strategy import build_new_task_production_label_snapshot
        with factory() as db:
            master = db.get(Product, product["id"])
            compiled = compile_master_order_bom(db, OrderItem(id=999, order_id=1,
                product_id=master.id, quantity=10, unit_price=12, subtotal=120))
            root = next(n for n in compiled.graph.nodes if n.product_id == master.id)
            assert root.source == "assembled"
            assert root.unit == expected_unit
            label = build_new_task_production_label_snapshot(master, total_quantity=12)
            assert label["production_label_count_snapshot"] == 3
            assert label["production_label_total_quantity_snapshot"] == 12
        bad = {**bom, "expected_version":product["version"], "inventory_mode":"manufactured"}
        rejected = client.put(f'/api/master/products/{product["id"]}/bom', json=bad)
        assert rejected.status_code in (400, 422), rejected.text
        direct = client.post("/api/master/products", json={**fields, "product_code":"NO-BOM"})
        assert direct.status_code == 422, direct.text
        changed = {**bom, "expected_version":product["version"], "components":[
            {**bom["components"][0], "quantity_per_set":4}, bom["components"][1]]}
        updated = client.put(f'/api/master/products/{product["id"]}/bom', json=changed)
        assert updated.status_code == 200, updated.text
        reopened = client.get(f'/api/master/products/{product["id"]}')
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["unit"] == expected_unit


def test_set_preset_frontend_defaults_and_child_ratios():
    from tests.test_multilevel_bom_frontend import HTML, method
    constants = HTML[HTML.index("      let bomComponentKeyCounter"):HTML.index("      const blankMaterial =")]
    names = ["onProductBoxStyleChange", "onBomInventoryModeChange", "addBomComponent", "validateProductBom"]
    script = "const assert=require('node:assert/strict');\n" + constants
    script += "\nconst methods={" + "\n".join(method(name) for name in names) + "};\n"
    script += """
const c={...methods,productForm:{id:null,customer_id:1,box_style:'BOM组合',unit:'只',supply_mode:'external_purchase'},
  bomEditor:blankBomEditor(),normalizeBoxTypeDisplay:x=>x,applyPartnerProductDefaults:()=>{},searchBomProducts:()=>{},showToast:()=>{throw Error('unexpected')}};
c.onProductBoxStyleChange();
assert.equal(c.productForm.unit,'套');assert.equal(c.productForm.composite_fulfillment_mode,'parent_delivery');
assert.equal(c.productForm.is_virtual_composite_parent,false);assert.equal(c.bomEditor.inventory_mode,'assembled');
assert.match(c.validateProductBom(),/至少/);
c.addBomComponent(); c.addBomComponent();
c.bomEditor.components.forEach((x,i)=>Object.assign(x,{component_product_id:i+2,quantity_per_set:i+3}));
assert.equal(c.validateProductBom(),'');
assert.deepEqual(c.bomEditor.components.map(x=>x.quantity_per_set),[3,4]);
assert(c.bomEditor.components.every(x=>x.inventory_relation==='assembly'));
c.productForm.unit='只';
assert.equal(c.validateProductBom(),'');
c.productForm.unit='千克';
assert.match(c.validateProductBom(),/只或套/);
"""
    result = subprocess.run([shutil.which("node")], input=script, text=True, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr
