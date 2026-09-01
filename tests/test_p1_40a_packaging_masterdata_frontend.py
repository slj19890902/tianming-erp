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
    assert "supplierHasCorrugated(row)" in supplier_panel
    assert "supplierHasPackaging(row)" in supplier_panel
    assert "纸板材质维护" in supplier_panel
    assert "包材产品与报价" in supplier_panel
    assert "包材供应商只维护包材产品、规格和正式报价" in supplier_panel

    supplier_rows = supplier_panel.split(
        '<tbody><tr v-for="row in pagedSuppliers"', 1
    )[1].split("</tbody>", 1)[0]
    assert 'supplierGroupFilter===\'corrugated\'' in supplier_rows
    assert 'supplierGroupFilter===\'packaging\'' in supplier_rows
    assert "材质维护</button><button" not in supplier_rows

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
    assert "客户要求的蜂窝板资料（将打印到供应商采购单）" in product_modal
    assert "客户要求材质" in product_modal
    assert "客户要求孔径(mm)" in product_modal
    assert "客户要求规格长(mm)" in product_modal
    assert "客户要求规格宽(mm)" in product_modal
    assert "客户要求规格厚(mm)" in product_modal
    assert "客户要求及报给供应商的规格" in product_modal
    assert "按客户样本填写材质、孔径、长×宽×厚及其他采购要求" in product_modal
    assert "默认采购数量比例" in product_modal
    assert "订单1 → 采购1" in product_modal
    assert "订单1 → 采购2" in product_modal
    assert "新订单只读取并冻结本常用箱比例" in product_modal
    assert '<details v-if="productForm.supply_mode!==\'external_purchase\'"' in product_modal
    assert 'supplierPackagingForm.category_code===\'hollow_board\'' in INDEX
    assert "维护正式报价" in INDEX
    assert "不进入材质字典、组合材质或材质规则" in INDEX
    assert "去维护正式报价" in INDEX
    assert "externalPurchaseCandidateNeedsPrice(row)" in INDEX
    assert "openExternalPurchasePriceMaintenance" in INDEX
    assert "supplierPriceProduct?.category_code==='paper_corner_guard'" in INDEX

    assert "v-if=\"productForm.supply_mode!=='external_purchase'\"" in product_modal
    assert product_modal.count("productForm.supply_mode!=='external_purchase'") >= 7
    assert "<label>图纸</label>" in product_modal
    candidate_table = product_modal.split(
        'v-else-if="productExternalSupplyOptions.length"',
        1,
    )[1].split("</table>", 1)[0]
    assert "采购价" not in candidate_table
    assert "最低价" not in candidate_table


def test_external_purchase_ratio_defaults_in_common_box_and_allows_order_exception() -> None:
    assert "常用箱采购数量换算" in INDEX
    assert "onProductExternalRatioModeChange" in INDEX
    assert "external_packaging_default_order_quantity_basis" in INDEX
    assert "external_packaging_default_purchase_quantity_basis" in INDEX
    assert "external_packaging_order_quantity_basis" in INDEX
    assert "external_packaging_purchase_quantity_basis" in INDEX
    assert "订单录入页不能另行修改" in INDEX
    assert 'v-model.number="item.external_packaging_order_quantity_basis"' not in INDEX
    assert 'v-model.number="item.external_packaging_purchase_quantity_basis"' not in INDEX
    assert "采购单价读取供应商有效报价" in INDEX
    assert "每个客户销售单位需采购数量" not in INDEX


def test_common_box_ratio_selector_sets_standard_and_custom_values(tmp_path: Path) -> None:
    args, body = _method("onProductExternalRatioModeChange")
    source = f"""
const vm={{productForm:{{_external_ratio_mode:"one_to_two",external_packaging_default_order_quantity_basis:null,external_packaging_default_purchase_quantity_basis:null}}}};
vm.change=new Function({json.dumps(args)},{json.dumps(body, ensure_ascii=False)}).bind(vm);
vm.change();
if(vm.productForm.external_packaging_default_order_quantity_basis!==1||vm.productForm.external_packaging_default_purchase_quantity_basis!==2) throw new Error("1 to 2 preset failed");
vm.productForm._external_ratio_mode="custom";
vm.productForm.external_packaging_default_order_quantity_basis=3;
vm.productForm.external_packaging_default_purchase_quantity_basis=5;
vm.change();
if(vm.productForm.external_packaging_default_order_quantity_basis!==3||vm.productForm.external_packaging_default_purchase_quantity_basis!==5) throw new Error("custom ratio was overwritten");
"""
    _run_node(tmp_path, source)


def test_external_only_common_box_edits_are_not_misclassified_as_unchanged(
    tmp_path: Path,
) -> None:
    method_names = (
        "masterEntityConfig",
        "masterComparableValue",
        "masterDisplayValue",
        "masterLocalChanges",
    )
    methods = {name: _method(name) for name in method_names}
    source = f"""
const vm={{
  masterEditBaseline:{{product:{{
    supply_mode:"external_purchase",
    external_packaging_category_code:"other_packaging",
    external_packaging_specification_summary:"旧摘要",
    external_packaging_purchase_unit:"片",
    external_packaging_default_order_quantity_basis:null,
    external_packaging_default_purchase_quantity_basis:null,
    _external_specification:{{summary:"旧摘要"}},
    _external_selected_ids:[2],
    _external_default_product_id:2,
  }}}},
  productForm:{{
    supply_mode:"external_purchase",
    external_packaging_category_code:"honeycomb_board",
    external_packaging_specification_summary:"材质170*110*170，孔径15mm，800×180×60mm",
    external_packaging_purchase_unit:"片",
    external_packaging_default_order_quantity_basis:1,
    external_packaging_default_purchase_quantity_basis:2,
    _external_specification:{{material:"170*110*170",aperture_mm:15,length_mm:800,width_mm:180,thickness_mm:60}},
    _external_selected_ids:[2],
    _external_default_product_id:2,
  }},
  drawingFile:null,
  customers:[],allMaterials:[],materials:[],moldTools:[],printingPlates:[],
  masterCurrentForm(entity){{return entity==="product" ? this.productForm : null;}},
  customerName(value){{return String(value);}},
}};
for (const name of {json.dumps(method_names)}) {{
  const [params,body]={json.dumps(methods, ensure_ascii=False)}[name];
  vm[name]=new Function(params,body).bind(vm);
}}
const changes=vm.masterLocalChanges("product");
const fields=new Set(changes.map(change=>change.field));
for (const required of [
  "external_packaging_category_code",
  "external_packaging_specification_summary",
  "external_packaging_default_order_quantity_basis",
  "external_packaging_default_purchase_quantity_basis",
  "_external_specification",
]) {{
  if(!fields.has(required)) throw new Error(`external-only edit was ignored: ${{required}}`);
}}
vm.masterEditBaseline.product={{...vm.productForm,_external_specification:{{...vm.productForm._external_specification,thickness_mm:40}}}};
const structureOnly=vm.masterLocalChanges("product");
if(structureOnly.length!==1||structureOnly[0].field!=="_external_specification") throw new Error("structure-only edit was misclassified as unchanged");
"""
    _run_node(tmp_path, source)


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
    external_packaging_default_order_quantity_basis:1,
    external_packaging_default_purchase_quantity_basis:2,
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
  productPrintingWriteFields(){{return {{printing_plate_mode:"no_plate",printing_plate_product_id:null,printing_content_description:""}};}},
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


def test_honeycomb_customer_specification_is_built_without_copying_supplier_spec(
    tmp_path: Path,
) -> None:
    method_names = ("syncProductExternalProfile", "buildProductWritePayload")
    methods = {name: _method(name) for name in method_names}
    source = f"""
const vm={{
  productForm:{{
    supply_mode:"external_purchase", box_style:"其他",
    external_packaging_category_code:"honeycomb_board",
    external_packaging_specification_summary:"", external_packaging_purchase_unit:"片",
    external_packaging_default_order_quantity_basis:1,
    external_packaging_default_purchase_quantity_basis:2,
    _external_specification:{{material:"170*110*170",aperture_mm:15,length_mm:800,width_mm:180,thickness_mm:60}},
    _external_selected_ids:[31], _external_default_product_id:31,
    customer_material_code:"HC-1", product_code:"HC-1",
  }},
  productExternalSupplyOptions:[{{external_product_id:31,purchase_unit:"片",specification_summary:"供应商标准规格1000×500×40mm"}}],
  productMoldError:"", productPrintingPlateError:"", productProductionLabelError:"",
  productPrintingWriteFields(){{return {{printing_plate_mode:"no_plate",printing_plate_product_id:null,printing_content_description:""}};}},
  validateProductCreaseAndReport(){{return null;}},
  serializeProductionProcesses(rows){{return (rows||[]).join(",");}},
  normalizeMmInteger(value){{return Number(value);}}, productHasPrinting(){{return false;}},
  attachMasterUpdateMetadata(_entity,payload){{return payload;}},
}};
for (const name of {json.dumps(method_names)}) {{
  const [params,body]={json.dumps(methods, ensure_ascii=False)}[name];
  vm[name]=new Function(params,body).bind(vm);
}}
vm.syncProductExternalProfile();
if(vm.productForm.external_packaging_specification_summary!=="材质170*110*170，孔径15mm，800×180×60mm") throw new Error("customer honeycomb summary was replaced by supplier specification");
const payload=vm.buildProductWritePayload();
const spec=payload.external_supply.customer_specification;
if(spec.material!=="170*110*170"||spec.aperture_mm!==15||spec.length_mm!==800||spec.width_mm!==180||spec.thickness_mm!==60) throw new Error("customer honeycomb specification was not saved");
if(payload.external_supply.candidates.length!==1||payload.external_supply.candidates[0].external_product_id!==31) throw new Error("supplier candidate was not retained");
"""
    _run_node(tmp_path, source)


def test_mixed_supplier_appears_in_both_business_groups(tmp_path: Path) -> None:
    method_names = (
        "supplierHasCorrugated",
        "supplierHasPackaging",
        "filteredSuppliers",
    )
    methods = {name: _method(name) for name in method_names}
    source = f"""
const vm = {{
  supplierGroupFilter:"corrugated",
  supplierSearch:"",
  suppliers:[
    {{id:1,standard_name:"纸板兼包材",sort_order:1,supply_categories:["corrugated_board","paper_corner_guard"]}},
    {{id:2,standard_name:"仅纸板",sort_order:2,supply_categories:["corrugated_board"]}},
    {{id:3,standard_name:"仅包材",sort_order:3,supply_categories:["epe"]}},
    {{id:4,standard_name:"历史纸板",sort_order:4,supply_categories:[]}},
  ],
}};
vm.supplierHasCorrugated = new Function(
  {json.dumps(methods["supplierHasCorrugated"][0])},
  {json.dumps(methods["supplierHasCorrugated"][1], ensure_ascii=False)}
).bind(vm);
vm.supplierHasPackaging = new Function(
  {json.dumps(methods["supplierHasPackaging"][0])},
  {json.dumps(methods["supplierHasPackaging"][1], ensure_ascii=False)}
).bind(vm);
vm.filteredSuppliers = new Function(
  {json.dumps(methods["filteredSuppliers"][0])},
  {json.dumps(methods["filteredSuppliers"][1], ensure_ascii=False)}
).bind(vm);

const paperboardIds = vm.filteredSuppliers().map(row=>row.id).join(",");
if (paperboardIds !== "1,2,4") throw new Error(`paperboard group mismatch: ${{paperboardIds}}`);
vm.supplierGroupFilter = "packaging";
const packagingIds = vm.filteredSuppliers().map(row=>row.id).join(",");
if (packagingIds !== "1,3") throw new Error(`packaging group mismatch: ${{packagingIds}}`);
"""
    _run_node(tmp_path, source)


def test_purchase_price_shortcut_is_only_shown_for_missing_price(
    tmp_path: Path,
) -> None:
    params, body = _method("externalPurchaseCandidateNeedsPrice")
    source = f"""
const vm = {{
  candidate:null,
  selectedExternalPurchaseCandidate(){{return this.candidate;}},
}};
vm.externalPurchaseCandidateNeedsPrice = new Function(
  {json.dumps(params)},
  {json.dumps(body, ensure_ascii=False)}
).bind(vm);

vm.candidate={{external_product_id:9,blocked_reason:"当前没有有效价格：请维护正式报价"}};
if (!vm.externalPurchaseCandidateNeedsPrice({{}})) throw new Error("missing-price shortcut was hidden");
vm.candidate={{external_product_id:9,blocked_reason:"供应商或外购产品已停用"}};
if (vm.externalPurchaseCandidateNeedsPrice({{}})) throw new Error("inactive product was routed to price maintenance");
vm.candidate={{external_product_id:null,blocked_reason:"当前没有有效价格"}};
if (vm.externalPurchaseCandidateNeedsPrice({{}})) throw new Error("missing product was routed to price maintenance");
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
    external_packaging_default_order_quantity_basis:1,
    external_packaging_default_purchase_quantity_basis:2,
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
