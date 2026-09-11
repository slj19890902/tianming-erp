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
        pytest.skip("Node.js is required for P1-46 behavior tests")
    target = tmp_path / "p1-46-product-printing-compat.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_selected_layout_keeps_one_source_of_truth_for_visible_fields() -> None:
    bindings = {
        "长": 'v-model.number="productForm.length_mm"',
        "宽": 'v-model.number="productForm.width_mm"',
        "高": 'v-model.number="productForm.height_mm"',
        "拼箱方式": 'v-model="productForm.splice_mode"',
        "舌头": 'v-model.number="productForm.flap_mm"',
        "报料长": 'v-model.number="productForm.report_length_mm"',
        "报料宽": 'v-model.number="productForm.report_width_mm"',
        "压线类型": 'v-model="productForm.crease_type"',
        "压线左": 'v-model.number="productForm.crease_left_mm"',
        "压线中": 'v-model.number="productForm.crease_middle_mm"',
        "压线右": 'v-model.number="productForm.crease_right_mm"',
        "材质": 'v-model="productForm.material_id"',
        "层数": 'v-model.number="productForm.layer_count"',
        "楞型": 'v-model="productForm.flute_type"',
        "模具": 'v-model="productForm.mold_tool_id"',
        "印刷情况": 'v-model="productForm._printing_situation"',
        "打印标签": 'v-model="productForm.production_label_enabled"',
        "标签数量": 'v-model="productForm.production_label_units_per_label"',
        "默认单价": 'v-model="productForm.sale_unit_price"',
    }
    for label, binding in bindings.items():
        assert binding in PRODUCT_MODAL, f"{label}没有继续绑定后端写入字段"

    # _printing_situation / _printing_colors are editor-only projections. The
    # existing API fields remain the sole persisted facts and colors serialize
    # to the shared full-width-plus canonical form.
    assert "const printing = this.productPrintingWriteFields(this.productForm);" in INDEX
    assert "Object.assign(payload,printing);" in INDEX
    assert "print_content: printing.print_content" in INDEX
    assert "printing_plate_mode: printing.printing_plate_mode" in INDEX
    assert "printing_colors:" in INDEX
    assert "join('＋')" in INDEX
    assert "delete payload._printing_situation" in INDEX
    assert "delete payload._printing_colors" in INDEX
    assert "delete payload._printing_original" in INDEX

    # 支持自定义开料数的箱型仍使用已有规范化链路；A1 等固定箱型显示“一开一”。
    assert ':value="cuttingModeFactor(productForm.default_cutting_mode)"' in PRODUCT_MODAL
    assert 'productForm.default_cutting_mode=normalizeCuttingMode($event.target.value)' in PRODUCT_MODAL
    assert 'v-else class="inline-input"><span>一开一</span>' in PRODUCT_MODAL

    # 保存按钮必须复用原有 saveModal，不另造请求或局部保存逻辑。
    assert 'class="btn primary product-inline-save"' in PRODUCT_MODAL
    assert '@click="saveModal()"' in PRODUCT_MODAL
    assert '<div v-if="modal?.type !== \'product\'" class="modal-foot">' in INDEX


def test_layout_matches_selected_compact_information_architecture() -> None:
    size_row = PRODUCT_MODAL.split('class="product-form-row product-size-report-row"', 1)[1].split(
        'class="product-material-workbench"', 1
    )[0]
    regular_row = size_row.split("<template v-else>", 1)[1]
    assert regular_row.index("报料长宽") < regular_row.index("开料方式")
    assert regular_row.index("开料方式") < regular_row.index("压线类型")
    assert regular_row.index("压线类型") < regular_row.index("压线尺寸")

    final_row = PRODUCT_MODAL.split('class="product-form-row product-final-row"', 1)[1].split(
        "</fieldset>", 1
    )[0]
    assert final_row.index('v-model="productForm.sale_unit_price"') < final_row.index("图纸")
    assert final_row.index("图纸") < final_row.index("图纸记录")
    assert "product-inline-save" not in final_row
    assert 'class="modal-foot product-editor-actions"' in INDEX

    assert "productMaterialActiveTab==='candidates'" in PRODUCT_MODAL
    assert "productMaterialActiveTab==='history'" in PRODUCT_MODAL
    assert "<summary>组合 BOM</summary>" in PRODUCT_MODAL
    assert 'class="product-secondary-disclosure"' in PRODUCT_MODAL


def test_digit_capacity_is_content_width_plus_control_chrome() -> None:
    css = INDEX.split("<style>", 1)[1].split("</style>", 1)[0]
    # 原来的 5ch/2ch 把 padding 和数字步进控件也算入总宽，导致一位数都看不见。
    assert "width: 5ch; min-width: 5ch" not in css
    assert "width: 2ch; min-width: 2ch" not in css
    assert ".product-dimension-field .input { width: 84px;" in css
    assert ".product-report-field .report-size-line .input { width: 104px;" in css
    assert ".product-production-label-quantity .input { width:68px;" in css
    assert 'max="999"' in PRODUCT_MODAL


def test_async_search_select_options_repaint_existing_backend_bindings() -> None:
    component = INDEX.split('app.component("search-select", {', 1)[1].split(
        'app.mount("#app")', 1
    )[0]
    assert "modelValue() { if (!this.open) this.query=this.selectedLabel; }," in component
    assert (
        "options() { if (!this.open && this.query !== this.selectedLabel) "
        "this.query=this.selectedLabel; },"
    ) in component


def test_printing_raw_fields_survive_sibling_edits_until_configuration_changes(
    tmp_path: Path,
) -> None:
    method_names = (
        "productHasPrinting",
        "productDirectPrintingColorCount",
        "productPrintingColorsForWrite",
        "productPrintingColorSummary",
        "productPrintingOriginalSnapshot",
        "productPrintingConfigurationChanged",
        "productPrintingWriteFields",
        "serializeProductionProcesses",
        "_productFormSaveFields",
        "_productFormDirty",
        "buildProductWritePayload",
    )
    bindings = []
    for name in method_names:
        params, body = _method(name)
        bindings.append(
            f"vm.{name}=new Function({json.dumps(params)},"
            f"{json.dumps(body, ensure_ascii=False)}).bind(vm);"
        )
    source = f"""
const vm={{
  modal:{{type:"product"}},drawingFile:null,productFormSnapshot:null,
  validateProductCreaseAndReport(){{return null;}},
  normalizeMmInteger(value){{return value;}},
  attachMasterUpdateMetadata(_entity,payload){{return payload;}},
}};
{chr(10).join(bindings)}
const base={{
  id:7,customer_id:1,product_code:"A-01",customer_material_code:"A-01",
  product_name:"旧挂板纸箱",supply_mode:"corrugated_production",remark:"原备注",
  print_content:"多色印刷",printing_colors:"黑色,红色/蓝色",
  printing_plate_mode:"plate",printing_plate_1_id:11,printing_plate_2_id:12,printing_plate_3_id:13,
  plate_alignment_value_mm:1,plate_mount_value_mm:2,machine_set_length_mm:3,machine_set_width_mm:4,machine_set_height_mm:5,
  production_process:"",_production_processes:[],_production_process_legacy:"",
  _printing_situation:"挂板印刷",_printing_colors:["黑色","红色","蓝色"],
  _printing_colors_touched:false,
  _printing_original:{{
    print_content:"多色印刷",printing_colors:"黑色,红色/蓝色",printing_plate_mode:"plate",
    printing_plate_1_id:11,printing_plate_2_id:12,printing_plate_3_id:13
  }},
  _external_selected_ids:[],_external_default_product_id:null,_external_specification:{{}},
  production_label_enabled:false
}};
vm.productForm=JSON.parse(JSON.stringify(base));
const platePayload=vm.buildProductWritePayload();
if (platePayload.print_content!=="多色印刷" || platePayload.printing_colors!=="黑色,红色/蓝色") throw new Error("legacy plate raw text changed");
if (platePayload.printing_plate_mode!=="plate" || platePayload.printing_plate_1_id!==11 || platePayload.printing_plate_2_id!==12 || platePayload.printing_plate_3_id!==13) throw new Error("legacy plate links changed");
const before=vm._productFormSaveFields();
vm.productFormSnapshot=JSON.stringify(before);
vm.productForm.remark="只改备注";
if (!vm._productFormDirty()) throw new Error("sibling edit was not detected");
const after=vm._productFormSaveFields();
if (after.print_content!=="多色印刷" || after.printing_colors!=="黑色,红色/蓝色" || after.printing_plate_mode!=="plate") throw new Error("dirty snapshot normalized untouched printing");

const unknown=JSON.parse(JSON.stringify(base));
unknown.print_content="旧版专色说明";
unknown.printing_colors="PANTONE 186 C,金色";
unknown.printing_plate_mode="no_plate";
unknown.printing_plate_1_id=null; unknown.printing_plate_2_id=null; unknown.printing_plate_3_id=null;
unknown._printing_situation="无印刷";
unknown._printing_original={{print_content:"旧版专色说明",printing_colors:"PANTONE 186 C,金色",printing_plate_mode:"no_plate",printing_plate_1_id:null,printing_plate_2_id:null,printing_plate_3_id:null}};
unknown.remark="只改未知记录备注";
vm.productForm=unknown;
const unknownPayload=vm.buildProductWritePayload();
if (unknownPayload.print_content!=="旧版专色说明" || unknownPayload.printing_colors!=="PANTONE 186 C,金色" || unknownPayload.printing_plate_mode!=="no_plate") throw new Error("unknown legacy printing was rewritten");

const direct=JSON.parse(JSON.stringify(base));
direct._printing_situation="双色印刷"; direct.print_content="双色印刷";
direct.printing_plate_mode="no_plate"; direct.printing_plate_1_id=null; direct.printing_plate_2_id=null; direct.printing_plate_3_id=null;
direct._printing_colors=[" 黑色 ","红色"]; direct._printing_colors_touched=true;
vm.productForm=direct;
const directPayload=vm.buildProductWritePayload();
if (directPayload.print_content!=="双色印刷" || directPayload.printing_colors!=="黑色＋红色") throw new Error("explicit direct printing was not canonicalized");
if (directPayload.printing_plate_mode!=="no_plate" || directPayload.printing_plate_1_id!==null || directPayload.plate_alignment_value_mm!==null) throw new Error("explicit direct printing kept plate data");

const external=JSON.parse(JSON.stringify(base));
external.supply_mode="external_purchase"; external.external_packaging_category_code="carton";
external._external_selected_ids=[88]; external._external_default_product_id=88;
external.external_packaging_default_order_quantity_basis=1;
external.external_packaging_default_purchase_quantity_basis=2;
vm.productForm=external;
const externalPayload=vm.buildProductWritePayload();
if (externalPayload.print_content!=="无印刷" || externalPayload.printing_colors!==null || externalPayload.printing_plate_mode!=="no_plate" || externalPayload.printing_plate_1_id!==null) throw new Error("external switch kept plate data");
"""
    _run_node(tmp_path, source)
