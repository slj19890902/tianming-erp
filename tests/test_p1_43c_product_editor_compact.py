from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PRODUCT_MODAL = INDEX.split(
    '<div v-else-if="modal.type === \'product\'">', 1
)[1].split(
    '<div v-else-if="modal.type === \'finishedStockPolicy\'">', 1
)[0]


def _method(name: str) -> tuple[str, str]:
    match = re.search(
        rf"^\s{{10}}(?:async )?{name}\(([^)]*)\) \{{(.*?)^\s{{10}}\}},",
        INDEX,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"missing JavaScript method: {name}"
    return match.group(1), match.group(2)


def _run_node(tmp_path: Path, source: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for P1-43C behavior tests")
    target = tmp_path / "p1-43c-product-editor.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_product_editor_uses_compact_rows_and_keeps_mold_next_to_process() -> None:
    assert 'class="product-edit-grid product-compact-editor"' in PRODUCT_MODAL
    assert 'class="field product-supply-compact"' in PRODUCT_MODAL
    assert "材质候选、报料轨迹与价格对比" in PRODUCT_MODAL
    assert '<details class="product-material-details">' in PRODUCT_MODAL

    process = PRODUCT_MODAL.index("生产工艺（可多选）")
    mold = PRODUCT_MODAL.index("生产模具 / 货架位置（必选）")
    printing = PRODUCT_MODAL.index("印刷情况")
    price = PRODUCT_MODAL.index("默认单价")
    assert process < mold < printing < price
    assert PRODUCT_MODAL.count("生产模具 / 货架位置（必选）") == 1

    css = INDEX.split("<style>", 1)[1].split("</style>", 1)[0]
    assert ".product-core-row" in css
    assert ".product-material-controls" in css
    assert ".bom-component-compact-row" in css
    assert "@media (max-width: 680px)" in css
    assert ".product-drawing-independent-section { grid-template-columns: 1fr; }" in css
    assert ".product-dimension-field .input { width: 5ch; min-width: 5ch; max-width: 100%; }" in css
    assert ".product-cutting-mode-field .inline-input .input { width: 2ch; min-width: 2ch; flex: 0 0 2ch; }" in css
    assert ".product-splice-field .select { width: 6em; min-width: 6em; max-width: 100%; }" in css
    assert ".product-flap-field .input { width: 2ch; min-width: 2ch; max-width: 100%; }" in css
    assert ".product-crease-field { grid-column: span 3; min-width: 0; }" in css
    assert "grid-template-columns: minmax(6em, .8fr) 86px 100px minmax(150px, .85fr);" in css
    assert ".product-material-picker .search-select { width: 150px; min-width: 0; max-width: 100%; }" in css
    assert ".product-production-label-config {\n        grid-column: span 1;" in css
    assert ".product-drawing-independent-section {\n        grid-column: span 3;" in css
    assert "minmax(0, 1fr)" in css


def test_label_drawing_and_remarks_are_reduced_without_deleting_saved_fields() -> None:
    label_panel = PRODUCT_MODAL.split(
        'class="product-production-label-config"', 1
    )[1].split("</div>", 3)[0]
    assert "随生产任务打印包装标签" in label_panel
    assert "每张标签代表只数" in label_panel
    assert "product-production-label-note" not in PRODUCT_MODAL
    assert "只保存以后新生产任务的包装标签规则" not in PRODUCT_MODAL

    assert "<label>图纸</label>" in PRODUCT_MODAL
    assert "<label>图纸记录</label>" in PRODUCT_MODAL
    assert "图片/图纸与印刷情况相互独立" not in PRODUCT_MODAL

    for removed_input in (
        'v-model.trim="productForm.report_notes"',
        'v-model.trim="productForm.base_report_notes"',
        'v-model="productForm.remark"',
        'v-model.trim="component.remark"',
        'v-model.trim="component.remarks"',
    ):
        assert removed_input not in PRODUCT_MODAL

    # The write payload still carries the loaded values, so opening and saving
    # an old record does not blank historical notes or create a false change.
    for preserved_payload in (
        "report_notes: f.report_notes",
        "base_report_notes: f.base_report_notes",
        "remark: f.remark",
        "remark: component.remark || \"\"",
        "remarks:String(component.remarks || \"\").trim() || null",
    ):
        assert preserved_payload in INDEX


def test_internal_bom_component_is_one_compact_business_row() -> None:
    component_card = PRODUCT_MODAL.split(
        'v-for="(component,index) in bomEditor.components"', 1
    )[1].split("</div>\n                    </div>\n                  </div>", 1)[0]
    for marker in (
        "bom-component-compact-row",
        "同客户常用箱",
        "每套数量",
        "必需",
        "送货单显示",
        "removeBomComponent(index)",
    ):
        assert marker in component_card
    for removed_control in (
        "模切组件",
        "模具最大产出",
        "备用纸张",
        "单据显示方式",
        "组件备注",
    ):
        assert removed_control not in component_card


def test_hidden_bom_facts_and_delivery_visibility_survive_round_trip(
    tmp_path: Path,
) -> None:
    normalize_params, normalize_body = _method("normalizeBomComponent")
    save_params, save_body = _method("_productBomSaveFields")
    source = f"""
globalThis.blankBomComponent=()=>({{
  component_product_id:null,quantity_per_set:1,is_die_cut:false,mold_tool_id:null,
  mold_max_yield_per_sheet:null,spare_sheet_quantity:0,display_mode:"internal_only",
  show_on_delivery:true,is_required:true,remark:""
}});
const vm={{bomEditor:{{enabled:true,expected_version:7,components:[]}}}};
vm.normalizeBomComponent=new Function(
  {json.dumps(normalize_params)},
  {json.dumps(normalize_body, ensure_ascii=False)}
).bind(vm);
vm._productBomSaveFields=new Function(
  {json.dumps(save_params)},
  {json.dumps(save_body, ensure_ascii=False)}
).bind(vm);
const loaded=vm.normalizeBomComponent({{
  component_product_id:9,quantity_per_set:2,is_die_cut:true,mold_tool_id:21,
  mold_max_yield_per_sheet:4,spare_sheet_quantity:6,
  display_mode:"show_on_all_docs",show_on_delivery:false,is_required:false,
  remark:"历史生产备注"
}});
vm.bomEditor.components=[loaded];
const saved=vm._productBomSaveFields().components[0];
if (saved.show_on_delivery !== false || saved.is_required !== false) throw new Error("visible flags changed");
if (!saved.is_die_cut || saved.mold_tool_id !== 21 || saved.mold_max_yield_per_sheet !== 4) throw new Error("hidden mold facts were cleared");
if (saved.spare_sheet_quantity !== 6 || saved.display_mode !== "show_on_all_docs") throw new Error("hidden component facts were cleared");
if (saved.remark !== "历史生产备注") throw new Error("hidden remark was cleared");
"""
    _run_node(tmp_path, source)


def test_open_save_with_hidden_product_notes_is_not_a_false_version_change(
    tmp_path: Path,
) -> None:
    fields_params, fields_body = _method("_productFormSaveFields")
    dirty_params, dirty_body = _method("_productFormDirty")
    source = f"""
const vm={{
  modal:{{type:"product"}},drawingFile:null,productFormSnapshot:null,
  productForm:{{
    id:3,customer_id:1,product_code:"A-01",product_name:"A1纸箱",
    report_notes:"历史报料要求",base_report_notes:"历史底料要求",remark:"历史内部备注",
    _production_processes:[],_external_specification:{{}},_external_selected_ids:[]
  }}
}};
vm._productFormSaveFields=new Function(
  {json.dumps(fields_params)},
  {json.dumps(fields_body, ensure_ascii=False)}
).bind(vm);
vm._productFormDirty=new Function(
  {json.dumps(dirty_params)},
  {json.dumps(dirty_body, ensure_ascii=False)}
).bind(vm);
const saved=vm._productFormSaveFields();
if (saved.report_notes!=="历史报料要求" || saved.base_report_notes!=="历史底料要求" || saved.remark!=="历史内部备注") throw new Error("hidden notes were not retained");
vm.productFormSnapshot=JSON.stringify(saved);
if (vm._productFormDirty()) throw new Error("unchanged hidden notes caused a false product version change");
vm.productForm.product_name="A1纸箱新版";
if (!vm._productFormDirty()) throw new Error("a real visible edit was not detected");
"""
    _run_node(tmp_path, source)


def test_mold_toggle_and_full_inline_javascript_remain_valid(tmp_path: Path) -> None:
    params, body = _method("onProductProcessesChanged")
    source = f"""
const vm={{
  productForm:{{_production_processes:[],mold_tool_id:19}},
  productUsesMold(form){{return (form._production_processes||[]).includes("模切");}}
}};
vm.onProductProcessesChanged=new Function(
  {json.dumps(params)},
  {json.dumps(body, ensure_ascii=False)}
).bind(vm);
vm.onProductProcessesChanged();
if (vm.productForm.mold_tool_id !== null) throw new Error("turning off die-cut kept a stale mold");
"""
    _run_node(tmp_path, source)

    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for P1-43C syntax validation")
    target = tmp_path / "p1-43c-index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
