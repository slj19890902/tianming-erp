import json

import pytest

from tests.test_p1_46_common_box_final_layout import INDEX, _method, _run_node


def _editor_runtime() -> str:
    names = (
        "hydrateProductForm", "hydrateProductPrintingPlateRows", "normalizeProductDimensions",
        "parseProductionProcesses", "unmanagedProductionProcesses", "serializeProductionProcesses", "productUsesMold",
        "productHasPrinting", "parseProductPrintingColors", "productPrintingColorsForWrite",
        "productPrintingColorSummary", "productPrintingOriginalSnapshot",
        "productPrintingConfigurationChanged", "productPrintingWriteFields",
        "productDirectPrintingColorCount", "productPrintingPlateCount",
        "onProductPrintingSituationChanged", "buildProductWritePayload", "_productFormSaveFields",
    )
    bindings = []
    for name in names:
        params, body = _method(name)
        bindings.append(f"vm.{name}=new Function({json.dumps(params)},{json.dumps(body)}).bind(vm);")
    return rf"""
const assert=require('node:assert/strict');
const html={json.dumps(INDEX)};
const blank=html.match(/const blankProduct = \(\) => \((\{{[\s\S]*?\n      \}})\);/);
assert.ok(blank,'use the actual product defaults');
global.blankProduct=new Function('return ('+blank[1]+')');
const vm={{
  mergePrintingPlateOptions(){{}}, normalizeBoxTypeDisplay:value=>value,
  productBoxTypeRule:()=>({{}}), usesProductSplice:()=>true, usesProductTongue:()=>true,
  usesProductDefaultCuttingMode:()=>false, normalizeCuttingMode:value=>value,
  isTelescopingLidBoxStyle:()=>false, productSheetCuttingParts:()=>[],
  normalizeMmInteger:value=>value==null||value===''?null:Number(value),
  validateProductCreaseAndReport:()=>null,
  attachMasterUpdateMetadata:(_entity,payload)=>payload,
}};
{chr(10).join(bindings)}
function openExisting(printContent){{
  const detail={{id:2968,version:2,customer_id:126,product_code:'HALF',customer_material_code:'HALF',
    product_name:'半截纸箱',box_style:'半开槽箱',supply_mode:'corrugated_production',
    print_content:printContent,printing_colors:null,printing_plate_mode:'no_plate',
    printing_plate_1_id:null,printing_plate_2_id:null,printing_plate_3_id:null,
    length_mm:1217,width_mm:966,height_mm:500,splice_mode:'double',pieces_per_box:2,
    production_process:'打钉',report_length_mm:2213,report_width_mm:984,
    crease_type:'压线',crease_left_mm:484,crease_middle_mm:500,crease_right_mm:0}};
  vm.productForm=vm.hydrateProductForm(detail);
  assert.equal(detail.print_content,printContent,'opening must not mutate source detail');
  return vm.productForm;
}}
"""


@pytest.mark.parametrize("initial", [None, "", "无印刷"])
def test_existing_printing_value_survives_open_dimension_edit_and_full_save(tmp_path, initial):
    _run_node(tmp_path, _editor_runtime() + f"""
const initial={json.dumps(initial)};
const form=openExisting(initial);
assert.equal(form._printing_situation,initial?'无印刷':'','unknown must have its own displayed value');
assert.ok(html.includes('<option v-if="productForm.id" value="">未确认</option>'));
assert.equal(vm.productPrintingConfigurationChanged(form),false,'opening inferred a printing change');
form.length_mm=1218;
assert.equal(vm._productFormSaveFields().print_content,initial,'dirty snapshot changed printing');
const payload=vm.buildProductWritePayload();
assert.equal(payload.length_mm,1218,'edited dimension was lost');
assert.equal(payload.print_content,initial,'unrelated save replaced the stored printing fact');
assert.equal(payload.printing_colors,null);
assert.equal(payload.printing_plate_mode,'no_plate');
assert.equal(payload.crease_right_mm,0);
assert.equal(payload.pieces_per_box,2);
""")


@pytest.mark.parametrize("choice", ["无印刷", "单色印刷", "双色印刷", "挂板印刷"])
def test_explicit_printing_choice_updates_unknown_record(tmp_path, choice):
    _run_node(tmp_path, _editor_runtime() + f"""
const choice={json.dumps(choice)};
const form=openExisting(null);
form._printing_situation=choice;
vm.onProductPrintingSituationChanged();
if(choice==='双色印刷') form._printing_colors=[' 黑色 ','红色'];
if(choice==='挂板印刷') form.printing_plate_1_id=77;
assert.equal(vm.productPrintingConfigurationChanged(form),true,'explicit choice was ignored');
const payload=vm.buildProductWritePayload();
assert.equal(payload.print_content,choice==='挂板印刷'?'单色印刷':choice);
assert.equal(payload.printing_colors,choice==='单色印刷'?'黑色':choice==='双色印刷'?'黑色＋红色':null);
assert.equal(payload.printing_plate_mode,choice==='挂板印刷'?'plate':'no_plate');
assert.equal(payload.printing_plate_1_id,choice==='挂板印刷'?77:null);
""")


def test_new_product_keeps_existing_no_print_default(tmp_path):
    _run_node(tmp_path, _editor_runtime() + """
vm.productForm=vm.hydrateProductForm({print_content:null});
assert.equal(vm.productForm.print_content,'无印刷');
assert.equal(vm.buildProductWritePayload().print_content,'无印刷');
""")


def test_explicit_unknown_option_clears_an_existing_printing_fact(tmp_path):
    _run_node(tmp_path, _editor_runtime() + """
const form=openExisting('无印刷');
form._printing_situation='';
vm.onProductPrintingSituationChanged();
assert.equal(vm.productPrintingConfigurationChanged(form),true);
const payload=vm.buildProductWritePayload();
assert.equal(payload.print_content,null);
assert.equal(payload.printing_colors,null);
assert.equal(payload.printing_plate_mode,'no_plate');
""")
