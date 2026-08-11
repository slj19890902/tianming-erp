from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


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
        pytest.skip("Node.js is required for P1-40A frontend behavior tests")
    target = tmp_path / "p1-40a-frontend.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_supplier_groups_and_external_product_editor_are_explicit() -> None:
    supplier_panel = INDEX.split(
        '<data-panel v-else-if="productTab === \'suppliers\'"',
        1,
    )[1].split(
        '<data-panel v-else-if="productTab === \'materials\'"',
        1,
    )[0]
    product_modal = INDEX.split(
        '<div v-else-if="modal.type === \'product\'">',
        1,
    )[1].split(
        '<div v-else-if="modal.type === \'productStockPolicy\'">',
        1,
    )[0]

    assert "纸板供应商" in supplier_panel
    assert "包材供应商" in supplier_panel
    assert "供应商大类" in supplier_panel
    assert "supplierBusinessGroup(row)" in supplier_panel

    for marker in (
        "外购包材资料",
        "不走纸板材质与生产工艺",
        "单一候选自动设为默认",
        "多候选必须人工指定一个默认",
        "不会按最低价自动选择",
        "当前客户和类别没有可用供应商产品",
        "/api/master/products/external-supply-candidates",
        "productExternalCandidateSelected",
        "setProductExternalCandidate",
    ):
        assert marker in INDEX
    assert "hollow_board" in INDEX
    assert "中空板" in INDEX
    assert 'supplierPackagingForm.category_code===\'hollow_board\'' in INDEX

    assert "v-if=\"productForm.supply_mode!=='external_purchase'\"" in product_modal
    assert product_modal.count("productForm.supply_mode!=='external_purchase'") >= 8
    assert "上传产品图片/图纸" in product_modal
    candidate_table = product_modal.split(
        'v-else-if="productExternalSupplyOptions.length"',
        1,
    )[1].split("</table>", 1)[0]
    assert "采购价" not in candidate_table
    assert "最低价" not in candidate_table


def test_external_candidate_selection_and_payload_clear_paper_fields(
    tmp_path: Path,
) -> None:
    method_names = (
        "syncProductExternalProfile",
        "setProductExternalCandidate",
        "buildProductWritePayload",
    )
    methods = {name: _method(name) for name in method_names}
    source = f"""
const vm = {{
  productForm: {{
    supply_mode:"external_purchase",
    external_packaging_category_code:"paper_corner_guard",
    external_packaging_specification_summary:"",
    external_packaging_purchase_unit:"",
    _external_specification:{{shape:"L",length_mm:780,side_a_mm:50,side_b_mm:50,thickness_mm:5}},
    _external_selected_ids:[],
    _external_default_product_id:null,
    material_id:99, layer_count:5, flute_type:"AB",
    length_mm:500, width_mm:400, height_mm:300, die_cut_path:"legacy-paper-path",
    report_length_mm:999, report_width_mm:888,
    production_process:"印刷,打钉", _production_processes:["印刷","打钉"],
    print_content:"双色印刷",
    production_label_enabled:true, production_label_units_per_label:50,
    customer_material_code:"EXT-1", product_code:"EXT-1",
  }},
  productExternalSupplyOptions:[
    {{external_product_id:7,specification_summary:"L型50×50×5mm，长870mm",purchase_unit:"根"}},
    {{external_product_id:8,specification_summary:"L型50×50×5mm，长870mm",purchase_unit:"根"}},
  ],
  productMoldError:"", productPrintingPlateError:"", productProductionLabelError:"",
  validateProductCreaseAndReport(){{return null;}},
  serializeProductionProcesses(rows){{return (rows||[]).join(",");}},
  normalizeMmInteger(value){{return Number(value);}},
  productHasPrinting(){{return false;}},
  attachMasterUpdateMetadata(_entity,payload){{return payload;}},
}};
vm.syncProductExternalProfile = new Function(
  {json.dumps(methods["syncProductExternalProfile"][0])},
  {json.dumps(methods["syncProductExternalProfile"][1], ensure_ascii=False)}
).bind(vm);
vm.setProductExternalCandidate = new Function(
  {json.dumps(methods["setProductExternalCandidate"][0])},
  {json.dumps(methods["setProductExternalCandidate"][1], ensure_ascii=False)}
).bind(vm);
vm.buildProductWritePayload = new Function(
  {json.dumps(methods["buildProductWritePayload"][0])},
  {json.dumps(methods["buildProductWritePayload"][1], ensure_ascii=False)}
).bind(vm);

vm.setProductExternalCandidate(vm.productExternalSupplyOptions[0], true);
if (vm.productForm._external_default_product_id !== 7) throw new Error("single candidate was not auto-defaulted");
vm.setProductExternalCandidate(vm.productExternalSupplyOptions[1], true);
if (vm.productForm._external_default_product_id !== 7) throw new Error("adding a second candidate changed the explicit default");
vm.setProductExternalCandidate(vm.productExternalSupplyOptions[0], false);
if (vm.productForm._external_default_product_id !== 8) throw new Error("remaining single candidate was not defaulted");

const payload = vm.buildProductWritePayload();
if (payload.material_id !== null || payload.layer_count !== null || payload.flute_type !== null) throw new Error("paper material fields were retained");
if (payload.length_mm !== null || payload.width_mm !== null || payload.height_mm !== null || payload.die_cut_path !== null) throw new Error("paper dimensions were retained");
if (payload.report_length_mm !== null || payload.report_width_mm !== null) throw new Error("paper report dimensions were retained");
if (payload.production_process !== "" || payload.print_content !== "无印刷") throw new Error("paper production fields were retained");
if (payload.production_label_enabled !== false) throw new Error("production label policy was retained");
if (payload.external_supply.candidates.length !== 1 || payload.external_supply.candidates[0].is_default !== true) throw new Error("external supply snapshot is invalid");
if (payload.external_supply.customer_specification.length_mm !== 780) throw new Error("customer length was not saved separately");
if (payload.unit !== "根") throw new Error("corner guard customer unit must be roots");
"""
    _run_node(tmp_path, source)


def test_candidate_loader_is_single_request_and_auto_defaults_one_candidate(
    tmp_path: Path,
) -> None:
    sync_params, sync_body = _method("syncProductExternalProfile")
    load_params, load_body = _method("loadProductExternalSupplyCandidates")
    load_factory = f"return async function({load_params}) {{{load_body}}}"
    source = f"""
let calls=0;
globalThis.axios={{
  async get(url,options){{
    calls += 1;
    if (url !== "/api/master/products/external-supply-candidates") throw new Error("wrong endpoint");
    if (options.params.customer_id !== 5 || options.params.category_code !== "paper_corner_guard") throw new Error("wrong scope");
    return {{data:{{items:[{{external_product_id:9,specification_summary:"L型50×50×5mm，长870mm",purchase_unit:"根"}}]}}}};
  }}
}};
const vm={{
  productForm:{{
    customer_id:5,supply_mode:"external_purchase",
    external_packaging_category_code:"paper_corner_guard",
    external_packaging_specification_summary:"",
    external_packaging_purchase_unit:"",
    external_supply:{{candidates:[]}},
    _external_selected_ids:[],_external_default_product_id:null,
  }},
  productExternalSupplyOptions:[],productExternalSupplySequence:0,
  productExternalSupplyLoading:false,productExternalSupplyError:"",
  errorMessage(error){{return String(error);}},
}};
vm.syncProductExternalProfile=new Function(
  {json.dumps(sync_params)},
  {json.dumps(sync_body, ensure_ascii=False)}
).bind(vm);
vm.loadProductExternalSupplyCandidates=new Function(
  {json.dumps(load_factory, ensure_ascii=False)}
)().bind(vm);
(async()=>{{
  const ok=await vm.loadProductExternalSupplyCandidates();
  if (!ok || calls !== 1) throw new Error("candidate request was not single-flight");
  if (vm.productForm._external_default_product_id !== 9) throw new Error("single live candidate was not auto-defaulted");
  if (vm.productForm.external_packaging_purchase_unit !== "根") throw new Error("purchase unit was not synchronized");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(tmp_path, source)


def test_main_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for P1-40A syntax validation")
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            INDEX,
            re.DOTALL,
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-40a-index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
