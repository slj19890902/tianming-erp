import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def test_supplier_master_is_compact_dynamic_and_keeps_history_readable() -> None:
    supplier_panel = INDEX.split(
        '<data-panel v-else-if="productTab === \'suppliers\'"',
        1,
    )[1].split(
        '<data-panel v-else-if="productTab === \'materials\'"',
        1,
    )[0]
    supplier_modal = INDEX.split(
        '<div v-else-if="modal.type === \'supplier\'">',
        1,
    )[1].split(
        '<div v-else-if="modal.type === \'quotationConvert\'"',
        1,
    )[0]
    supplier_methods = INDEX.split(
        "async loadSuppliers(",
        1,
    )[1].split(
        "async loadMaterials()",
        1,
    )[0]
    supplier_save = INDEX.split(
        'if (this.modal.type === "supplier") {',
        1,
    )[1].split(
        'if (this.modal.type === "product") {',
        1,
    )[0]

    assert "供应商" in supplier_panel
    assert "新增供应商" in supplier_panel
    assert "下载材质模板" in supplier_panel
    assert "导入材质 Excel" in supplier_panel
    assert "@click=\"openSupplierMaterials(row)\"" in supplier_panel

    assert supplier_modal.count("*") == 1
    assert "供应商全称 *" in supplier_modal
    assert "显示简称 *</label>" not in supplier_modal
    assert "业务代码 *</label>" not in supplier_modal
    assert "<details" in supplier_modal
    assert "选填：联系人、电话和备注" in supplier_modal

    assert '"/api/master/suppliers/candidates"' in supplier_methods
    assert '"/api/master/suppliers"' in supplier_methods
    assert "include_inactive:true" in supplier_methods
    assert 'return ["全部供应商", ...this.activeSupplierNames]' in INDEX
    assert '["苏州嘉林亿", "昆山鸣朋", "苏州佳丰"]' not in INDEX
    assert "苏州佳丰" not in INDEX

    assert 'axios.post("/api/master/suppliers",payload)' in supplier_save
    assert "expected_version:Number(this.supplierForm.version)" in supplier_save
    assert "`/api/master/suppliers/${saved.id}/status`" in supplier_save
    assert 'display_name:String(form.display_name || "").trim() || null' in INDEX
    assert 'business_code:String(form.business_code || "").trim() || null' in INDEX
    assert "confirm(" not in supplier_save
    assert "reason" not in supplier_save

    assert 'if (name.includes("嘉林亿")) return "嘉林亿"' in INDEX
    assert 'if (name.includes("鸣朋")) return "鸣朋"' in INDEX


def test_new_business_material_candidates_only_use_active_supplier_master() -> None:
    material_gate = INDEX.split(
        "isActiveMaterialSupplier(name) {",
        1,
    )[1].split(
        "fluteMatches(fluteType, tab)",
        1,
    )[0]
    material_filter = INDEX.split(
        "filteredMaterialOptions(supplierName, layerCount, fluteType, keyword) {",
        1,
    )[1].split(
        "materialSelectOptions(supplierName, layerCount, fluteType)",
        1,
    )[0]

    assert "this.activeSuppliers.some(row =>" in material_gate
    assert 'String(row.standard_name || "").trim() === target' in material_gate
    assert "(row.aliases || []).some(" in material_gate
    assert "if (!this.isActiveMaterialSupplier(m.supplier_name)) return false;" in material_filter

    # 新建订单、常用箱和报料都复用同一候选 helper，避免各自遗漏停用供应商门禁。
    assert ':options="orderItemMaterialSelectOptions(item)"' in INDEX
    assert (
        ':options="materialSelectOptions(productForm._material_supplier, '
        'productForm.layer_count, productForm.flute_type)"'
    ) in INDEX
    assert (
        ':options="materialSelectOptions(requisitionMaterialForm.supplier_name,'
        'requisitionMaterialForm.layer_count,requisitionMaterialForm.flute_type)"'
    ) in INDEX

    # 供应商可用性完全来自动态主档，不再硬编码佳丰或其它固定供应商名单。
    assert '["苏州嘉林亿", "昆山鸣朋", "苏州佳丰"]' not in INDEX
    assert "苏州佳丰" not in INDEX
    assert 'return ["全部供应商", ...this.activeSupplierNames]' in INDEX

    # 历史只读文本仍按单据快照展示，不依赖新业务候选过滤。
    history_display = INDEX.split(
        "orderItemMaterialText(item, includeWeight = true) {",
        1,
    )[1].split(
        "displayMaterialText(value)",
        1,
    )[0]
    assert "filteredMaterialOptions" not in history_display


def test_material_candidate_gate_excludes_inactive_and_unknown_suppliers(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for supplier material candidate gate test")

    def method_body(name: str) -> str:
        match = re.search(
            rf"^\s{{10}}{name}\([^)]*\) \{{(.*?)^\s{{10}}\}},",
            INDEX,
            re.MULTILINE | re.DOTALL,
        )
        assert match, f"missing JavaScript method: {name}"
        return match.group(1)

    active_suppliers = [
        {"standard_name": "苏州嘉林亿", "aliases": ["嘉林亿"], "is_active": True},
        {"standard_name": "昆山鸣朋", "aliases": ["鸣朋"], "is_active": True},
        {"standard_name": "胜源", "aliases": [], "is_active": True},
        {"standard_name": "森林阳光", "aliases": [], "is_active": True},
    ]
    materials = [
        {"id": 1, "code": "JLY", "supplier_name": "苏州嘉林亿", "layer_count": 3},
        {"id": 2, "code": "MP", "supplier_name": "昆山鸣朋", "layer_count": 3},
        {"id": 3, "code": "SY", "supplier_name": "胜源", "layer_count": 3},
        {"id": 4, "code": "SLYG", "supplier_name": "森林阳光", "layer_count": 3},
        {"id": 5, "code": "JF", "supplier_name": "苏州佳丰", "layer_count": 3},
        {"id": 6, "code": "UNKNOWN", "supplier_name": "未登记供应商", "layer_count": 3},
    ]
    script = f"""
const vm = {{
  activeSuppliers: {json.dumps(active_suppliers, ensure_ascii=False)},
  activeSupplierNames: {json.dumps([row["standard_name"] for row in active_suppliers], ensure_ascii=False)},
  allMaterials: {json.dumps(materials, ensure_ascii=False)},
  materials: [],
  sortMaterialRows(rows) {{ return rows; }},
  materialWeightStructure() {{ return ""; }}
}};
vm.isActiveMaterialSupplier = new Function("name", {json.dumps(method_body("isActiveMaterialSupplier"))}).bind(vm);
vm.filteredMaterialOptions = new Function(
  "supplierName", "layerCount", "fluteType", "keyword",
  {json.dumps(method_body("filteredMaterialOptions"))}
).bind(vm);
const codes = vm.filteredMaterialOptions("", 3, null, "").map(row => row.code);
if (JSON.stringify(codes) !== JSON.stringify(["JLY", "MP", "SY", "SLYG"])) {{
  throw new Error(`unexpected new-business candidates: ${{JSON.stringify(codes)}}`);
}}
"""
    target = tmp_path / "supplier-material-gate.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
