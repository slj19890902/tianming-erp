from __future__ import annotations

import shutil
import subprocess
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def product_api_app(tmp_path: Path):
    """A disposable product API; it must never resolve the configured ERP DB."""
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.products import router as products_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent
    from app.models.product_drawing import ProductDrawing
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-50b-product-api.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="P1-50B管理员",
            display_name="P1-50B管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=15050,
            customer_code="P150B",
            name="苏州思迈尔包装有限公司",
        )
        material = Material(
            code="K=A-BC-P150B",
            paper_composition="K=A",
            layer_count=5,
            flute_type="AB",
        )
        db.add_all([user, customer, material])
        db.flush()
        current = Product(
            customer_id=customer.id,
            product_code="P150B-CURRENT",
            customer_material_code="KH-P150B-CURRENT",
            product_name="五层加强纸箱",
            material_id=material.id,
            legacy_material_text="K=A",
            length_mm=Decimal("520"),
            width_mm=Decimal("350"),
            height_mm=Decimal("300"),
            box_category="normal",
            box_style="A1/0201 普通开槽箱",
            supply_mode="corrugated_production",
            print_content="红黑双色印刷",
            production_process="印刷,粘贴",
            sale_unit_price=Decimal("7.3500"),
            report_length_mm=1770,
            report_width_mm=656,
            crease_type="压线",
            crease_left_mm=150,
            crease_middle_mm=356,
            crease_right_mm=150,
            report_notes="保护报料备注",
            layer_count=5,
            flute_type="AB",
            splice_mode="single",
            pieces_per_box=1,
            production_label_enabled=False,
            production_label_units_per_label=None,
        )
        sibling = Product(
            customer_id=customer.id,
            product_code="P150B-SIBLING",
            customer_material_code="KH-P150B-SIBLING",
            product_name="物流周转箱",
            material_id=material.id,
            length_mm=Decimal("380"),
            width_mm=Decimal("260"),
            height_mm=Decimal("220"),
            box_category="normal",
            box_style="A1",
            production_label_enabled=True,
            production_label_units_per_label=9,
        )
        component = Product(
            customer_id=customer.id,
            product_code="P150B-COMPONENT",
            customer_material_code="KH-P150B-COMPONENT",
            product_name="组合套装内盒",
            box_category="die_cut",
            box_style="模切内盒",
            production_label_enabled=True,
            production_label_units_per_label=50,
        )
        db.add_all([current, sibling, component])
        db.flush()
        current.is_composite = True
        db.add_all(
            [
                ProductBomComponent(
                    parent_product_id=current.id,
                    component_product_id=component.id,
                    quantity_per_set=Decimal("2"),
                    display_order=1,
                    internal_component_code="P150B-BOM-1",
                    is_die_cut=False,
                    spare_sheet_quantity=3,
                    display_mode="internal_only",
                    show_on_delivery=False,
                ),
                ProductDrawing(
                    product_id=current.id,
                    image_path="drawings/p150b-current.pdf",
                    thumbnail_path="drawings/p150b-current.png",
                    uploaded_by=user.id,
                ),
            ]
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(products_router, prefix="/api/master/products")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory
    finally:
        engine.dispose()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def _update_payload(product: dict, **changes: object) -> dict:
    from app.api.products import ProductPayload

    payload = {
        key: product[key]
        for key in ProductPayload.model_fields
        if key != "external_supply" and key in product
    }
    payload.update(
        expected_version=product["version"],
        change_reason="P1-50B包装标签策略保存回归",
    )
    payload.update(changes)
    return payload


def test_label_policy_only_edits_are_real_master_changes() -> None:
    """A label-only edit must reach the versioned product save path."""
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the P1-50B frontend behavior test"

    harness = r"""
const fs = require("fs");
const vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map(match => match[1])
  .filter(source => source.trim());
if (scripts.length !== 1) throw new Error(`Expected one inline script, found ${scripts.length}`);

const sandbox = {
  axios: {
    defaults: {},
    interceptors: { response: { use() {} } },
  },
  Vue: {
    createApp(definition) {
      sandbox.definition = definition;
      return { component() { return this; }, mount() { return this; } };
    },
  },
  localStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  window: {},
  console,
  URLSearchParams,
  setTimeout,
  clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(scripts[0], sandbox);
const methods = sandbox.definition.methods;
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};

const changesFor = (original, current) => methods.masterLocalChanges.call({
  ...methods,
  masterEditBaseline: { product: original },
  productForm: current,
  drawingFile: null,
  customers: [],
  allMaterials: [],
  materials: [],
  moldTools: [],
  printingPlates: [],
}, "product");

const toggleChanges = changesFor(
  { production_label_enabled: false, production_label_units_per_label: null },
  { production_label_enabled: true, production_label_units_per_label: 100 },
);
assert(
  toggleChanges.some(change => change.field === "production_label_enabled"),
  "production label toggle was misclassified as no master-data change",
);
assert(
  toggleChanges.some(change => change.field === "production_label_units_per_label"),
  "initial units-per-label was omitted from the master-data diff",
);

const unitsChanges = changesFor(
  { production_label_enabled: true, production_label_units_per_label: 100 },
  { production_label_enabled: true, production_label_units_per_label: 125 },
);
assert(
  unitsChanges.length === 1 && unitsChanges[0].field === "production_label_units_per_label",
  "units-per-label-only edit was misclassified as no master-data change",
);
const disableChanges = changesFor(
  { production_label_enabled: true, production_label_units_per_label: 100 },
  { production_label_enabled: false, production_label_units_per_label: null },
);
assert(
  disableChanges.some(change => change.field === "production_label_enabled") &&
    disableChanges.some(change => change.field === "production_label_units_per_label"),
  "disabled label strategy was misclassified as no master-data change",
);
const equivalentUnits = changesFor(
  { production_label_enabled: true, production_label_units_per_label: 100 },
  { production_label_enabled: true, production_label_units_per_label: "100" },
);
assert(equivalentUnits.length === 0, "numeric input string created a false master-data change");
process.stdout.write(JSON.stringify({ toggleChanges, unitsChanges, disableChanges }));
"""

    result = subprocess.run(
        [node, "-", str(ROOT / "static" / "index.html")],
        input=harness,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert '"production_label_enabled"' in result.stdout
    assert '"production_label_units_per_label"' in result.stdout


def test_save_modal_hydrates_authoritative_label_response_before_close() -> None:
    """The response, open form, baseline and dirty snapshot must become one state."""
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the P1-50B saveModal behavior test"

    harness = r"""
const fs = require("fs");
const vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map(match => match[1])
  .filter(source => source.trim());
if (scripts.length !== 1) throw new Error(`Expected one inline script, found ${scripts.length}`);

const saved = {
  id: 7, version: 4, customer_id: 1,
  product_code: "P150B-SAVE", customer_material_code: "P150B-SAVE",
  product_name: "P1-50B保存回填纸箱", material_id: null,
  length_mm: 520, width_mm: 350, height_mm: 300,
  box_category: "normal", box_style: "A1/0201 普通开槽箱",
  supply_mode: "corrugated_production", production_process: "粘贴",
  production_label_enabled: true, production_label_units_per_label: 125,
  splice_mode: "single", pieces_per_box: 1, default_cutting_mode: "一开一",
  printing_plate_mode: "no_plate", print_content: "无印刷",
  combination_mode: "parent_priced_set", drawings: [],
};
const calls = [];
const sandbox = {
  axios: {
    defaults: {}, interceptors: { response: { use() {} } },
    async post(url, payload) {
      calls.push({ method: "post", url, payload });
      if (url.endsWith("/update-preview")) {
        return { data: { can_update: true, current_version: 3, confirmation_token: null } };
      }
      throw new Error(`Unexpected POST ${url}`);
    },
    async put(url, payload) {
      calls.push({ method: "put", url, payload });
      return { data: saved };
    },
  },
  Vue: {
    createApp(definition) {
      sandbox.definition = definition;
      return { component() { return this; }, mount() { return this; } };
    },
  },
  localStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  window: {}, console, URLSearchParams, setTimeout, clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(scripts[0], sandbox);
const methods = sandbox.definition.methods;
const assert = (condition, message) => { if (!condition) throw new Error(message); };

const baseline = { ...saved, version: 3, production_label_enabled: false, production_label_units_per_label: null };
const current = { ...baseline, production_label_enabled: true, production_label_units_per_label: "125" };
const context = {
  ...methods,
  modal: { type: "product", title: "编辑产品" },
  productForm: current,
  masterEditBaseline: { product: JSON.parse(JSON.stringify(baseline)) },
  productFormSnapshot: null,
  productBomSnapshot: "stable-bom",
  drawingFile: null,
  bomEditor: { enabled: false, expected_version: null, components: [] },
  masterSavePending: false,
  masterPendingSaveOptions: null,
  masterVersionConflict: null,
  masterChangeConfirm: { entity: null },
  productEditReturnContext: null,
  loading: false,
  productsError: "",
  allMaterials: [], materials: [], customers: [], moldTools: [], printingPlates: [],
  productBoxTypeRules: [{ code: "a1_0201", label: "A1/0201 普通开槽箱" }],
  closeSnapshots: [],
  closeModal() {
    this.closeSnapshots.push({
      form: JSON.parse(JSON.stringify(this.productForm)),
      baseline: JSON.parse(JSON.stringify(this.masterEditBaseline.product)),
      formSnapshot: this.productFormSnapshot,
    });
    this.modal = null;
  },
  showToast() {},
  async loadProducts() { return true; },
  handleMaster409() { return false; },
  errorMessage(error) { return error?.message || String(error); },
};
context.productFormSnapshot = JSON.stringify(methods._productFormSaveFields.call({ ...context, productForm: baseline }));
context.productBomSnapshot = JSON.stringify(methods._productBomSaveFields.call(context));

(async () => {
  const result = await methods.saveModal.call(context);
  assert(result === true, "saveModal did not complete");
  assert(calls.filter(call => call.method === "post").length === 1, "preview was not sent exactly once");
  assert(calls.filter(call => call.method === "put").length === 1, "PUT was not sent exactly once");
  assert(calls.find(call => call.method === "put").payload.production_label_units_per_label === 125, "PUT did not normalize units to integer");
  assert(context.closeSnapshots.length === 1, "modal did not close exactly once");
  const atClose = context.closeSnapshots[0];
  assert(atClose.form.production_label_units_per_label === 125, "current form was not hydrated from authoritative response");
  assert(atClose.form.version === 4, "current form kept stale version");
  assert(atClose.baseline.production_label_units_per_label === 125, "master baseline was not synchronized");
  assert(atClose.baseline.version === 4, "master baseline kept stale version");
  assert(atClose.formSnapshot === null, "successful close kept an unsaved-change snapshot");
  process.stdout.write(JSON.stringify({ calls, atClose }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""

    result = subprocess.run(
        [node, "-", str(ROOT / "static" / "index.html")],
        input=harness,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert '"version":4' in result.stdout


def test_label_units_input_keeps_string_until_payload_validation() -> None:
    source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'v-model="productForm.production_label_units_per_label"' in source
    assert 'v-model.number="productForm.production_label_units_per_label"' not in source
    payload_start = source.index("buildProductWritePayload(options = null)")
    payload_end = source.index("async prepareProductOneClickSave()", payload_start)
    payload = source[payload_start:payload_end]
    assert "const unitsPerLabel = Number(payload.production_label_units_per_label);" in payload
    assert "!Number.isInteger(unitsPerLabel) || unitsPerLabel <= 0" in payload
    assert "payload.production_label_units_per_label = null;" in payload


def test_successful_label_only_save_rehydrates_current_form_from_authoritative_response() -> None:
    source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    save_start = source.index('if (this.modal.type === "product") {')
    save_end = source.index('if (this.modal.type === "material") {', save_start)
    save = source[save_start:save_end]

    put = 'saved=(await axios.put(`/api/master/products/${this.productForm.id}`, payload)).data;'
    hydrate = "this.productForm = this.hydrateProductForm(saved);"
    baseline = 'this.beginMasterEdit("product",this.productForm);'
    snapshot = "this.productFormSnapshot = JSON.stringify(this._productFormSaveFields());"

    assert put in save
    assert hydrate in save
    assert baseline in save
    assert snapshot in save
    assert save.index(put) < save.index(hydrate) < save.index(baseline) < save.index(snapshot)


@pytest.mark.parametrize("boolean_units", [True, False])
def test_enabled_label_strategy_rejects_boolean_units(boolean_units: bool) -> None:
    from pydantic import ValidationError

    from app.api.products import ProductPayload

    with pytest.raises(ValidationError, match="每张标签数量必须是正整数"):
        ProductPayload(
            customer_id=1,
            product_code="P150B-BOOL",
            customer_material_code="P150B-BOOL",
            product_name="布尔数量非法纸箱",
            box_category="normal",
            box_style="A1",
            production_label_enabled=True,
            production_label_units_per_label=boolean_units,
        )


def test_only_virtual_component_parent_forces_label_clear_in_fields_set() -> None:
    from app.api.products import ProductPayload

    external = ProductPayload(
        customer_id=1,
        product_code="P150B-EXTERNAL",
        customer_material_code="P150B-EXTERNAL",
        product_name="外购包材",
        box_category="normal",
        supply_mode="external_purchase",
        external_packaging_category_code="paper_corner_guard",
        external_packaging_specification_summary="L形护角",
        external_packaging_purchase_unit="根",
        external_supply={
            "candidates": [{"external_product_id": 1, "is_default": True}],
            "customer_specification": {
                "shape": "L",
                "length_mm": 100,
                "side_a_mm": 40,
                "side_b_mm": 40,
                "thickness_mm": 4,
            },
        },
    )
    virtual_parent = ProductPayload(
        customer_id=1,
        product_code="P150B-VIRTUAL",
        customer_material_code="P150B-VIRTUAL",
        product_name="虚拟组合套装",
        box_category="normal",
        is_virtual_composite_parent=True,
    )

    assert "production_label_enabled" not in external.model_fields_set
    assert "production_label_units_per_label" not in external.model_fields_set
    for payload in (virtual_parent,):
        assert payload.production_label_enabled is False
        assert payload.production_label_units_per_label is None
        assert "production_label_enabled" in payload.model_fields_set
        assert "production_label_units_per_label" in payload.model_fields_set


def test_legacy_full_update_with_explicit_supply_mode_preserves_label_policy(
    product_api_app,
) -> None:
    """Old full-update clients sent supply_mode but knew neither label field."""
    from app.api.products import ProductPayload, _validated_product_versioned_updates
    from app.models.product import Product
    from app.models.user import User

    _app, factory = product_api_app
    with factory() as db:
        product = db.scalar(select(Product).where(Product.product_code == "P150B-SIBLING"))
        assert product is not None
        payload = ProductPayload(
            customer_id=product.customer_id,
            product_code=product.product_code,
            customer_material_code=product.customer_material_code,
            product_name=product.product_name,
            material_id=product.material_id,
            length_mm=product.length_mm,
            width_mm=product.width_mm,
            height_mm=product.height_mm,
            box_category=product.box_category,
            box_style=product.box_style,
            supply_mode="corrugated_production",
        )
        assert "supply_mode" in payload.model_fields_set
        assert "production_label_enabled" not in payload.model_fields_set
        updates = _validated_product_versioned_updates(
            db,
            product=product,
            payload=payload,
            user=User(role="admin", username="p1-50b-contract-admin"),
        )

    assert "production_label_enabled" not in updates
    assert "production_label_units_per_label" not in updates


def test_label_policy_api_round_trip_is_scoped_versioned_and_lossless(
    product_api_app,
) -> None:
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent
    from app.models.product_drawing import ProductDrawing

    app, factory = product_api_app
    with TestClient(app) as client:
        _login(client)
        rows_before = client.get("/api/master/products?page_size=200").json()["items"]
        target_before = next(row for row in rows_before if row["product_code"] == "P150B-CURRENT")
        sibling_before = next(row for row in rows_before if row["product_code"] == "P150B-SIBLING")
        protected = {
            field: target_before[field]
            for field in (
                "material_id", "legacy_material_text", "length_mm", "width_mm", "height_mm",
                "box_category", "box_style", "print_content", "production_process",
                "sale_unit_price", "report_length_mm", "report_width_mm", "crease_type",
                "crease_left_mm", "crease_middle_mm", "crease_right_mm", "report_notes",
                "layer_count", "flute_type", "splice_mode", "pieces_per_box",
            )
        }

        enabled = client.put(
            f"/api/master/products/{target_before['id']}",
            json=_update_payload(
                target_before,
                production_label_enabled=True,
                production_label_units_per_label="125",
            ),
        )
        assert enabled.status_code == 200, enabled.text
        enabled_body = enabled.json()
        assert enabled_body["production_label_enabled"] is True
        assert enabled_body["production_label_units_per_label"] == 125
        assert enabled_body["version"] == target_before["version"] + 1

        reopened = client.get(f"/api/master/products/{target_before['id']}")
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["production_label_enabled"] is True
        assert reopened.json()["production_label_units_per_label"] == 125
        assert {field: reopened.json()[field] for field in protected} == protected

        repeated = client.put(
            f"/api/master/products/{target_before['id']}",
            json=_update_payload(enabled_body),
        )
        assert repeated.status_code == 200, repeated.text
        repeated_body = repeated.json()
        assert repeated_body["version"] == enabled_body["version"]

        stale = client.put(
            f"/api/master/products/{target_before['id']}",
            json=_update_payload(
                target_before,
                production_label_enabled=False,
                production_label_units_per_label=None,
            ),
        )
        assert stale.status_code == 409, stale.text

        disable_payload = _update_payload(
            repeated_body,
            production_label_enabled=False,
            production_label_units_per_label=999,
        )
        disable_preview = client.post(
            f"/api/master/products/{target_before['id']}/update-preview",
            json=disable_payload,
        )
        assert disable_preview.status_code == 200, disable_preview.text
        preview_body = disable_preview.json()
        assert preview_body["can_update"] is True
        assert preview_body["warnings"]
        assert preview_body["confirmation_token"]
        disable_payload["confirmation_token"] = preview_body["confirmation_token"]
        disabled = client.put(
            f"/api/master/products/{target_before['id']}",
            json=disable_payload,
        )
        assert disabled.status_code == 200, disabled.text
        disabled_body = disabled.json()
        assert disabled_body["production_label_enabled"] is False
        assert disabled_body["production_label_units_per_label"] is None
        reopened_disabled = client.get(f"/api/master/products/{target_before['id']}").json()
        assert reopened_disabled["production_label_enabled"] is False
        assert reopened_disabled["production_label_units_per_label"] is None

        rows_after = client.get("/api/master/products?page_size=200").json()["items"]
        sibling_after = next(row for row in rows_after if row["product_code"] == "P150B-SIBLING")
        assert len(rows_after) == len(rows_before)
        assert sibling_after == sibling_before

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Product)) == 3
        assert db.scalar(select(func.count()).select_from(ProductBomComponent)) == 1
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 1
