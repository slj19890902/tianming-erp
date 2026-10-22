"""Execute extracted production editor methods with the shipped structure helper."""
import json

from tests.test_p1_46_common_box_final_layout import ROOT, INDEX, _method, _run_node
from tests.test_product_unknown_printing_frontend import _editor_runtime


def runtime():
    names = (
        "boxTypeLookupKey", "productBoxTypeRule", "normalizeBoxTypeDisplay",
        "usesProductSplice", "usesProductTongue", "productSupportedSpliceModes",
        "markProductStructureEdited", "resetProductStructure", "onProductSpliceChanged",
        "productCompatibleBoxStyleOptions", "onProductBoxStyleChange",
        "onProductProcessesChanged", "applyBoxTypeRecommendation", "autoApplyProductRecommendations",
    )
    bindings = []
    for name in names:
        params, body = _method(name)
        ctor = "AsyncFunction" if name == "autoApplyProductRecommendations" else "Function"
        bindings.append(f"vm.{name}=new {ctor}({json.dumps(params)},{json.dumps(body)}).bind(vm);")
    return _editor_runtime() + f"""
global.window={{ERPProductUnits:require({json.dumps(str(ROOT / 'static/js/product-unit-labels.js'))})}};
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
vm.productBoxTypeRules=[
 {{code:'a1_0201',display_name:'A1/0201 普通开槽箱',aliases:['WC 五层钉箱'],supports_splice:true,uses_flap:true,supported_splice_modes:['single','double']}},
 {{code:'half_slotted',display_name:'半开槽箱',aliases:[],supports_splice:true,uses_flap:true,supported_splice_modes:['single','double']}}
];
vm.productBoxStyleOptions=vm.productBoxTypeRules.map(x=>x.display_name);
vm.productRecommendationSequence=0;
vm.modal={{type:'product'}};
vm.loadProductBoxTypeRules=async()=>{{}};
vm.requestBoxTypeRecommendation=async()=>({{auto_calculated:false}});
vm.errorMessage=error=>error.message;
vm.syncProductSheetCuttingSettings=()=>{{}};
vm.applyPartnerProductDefaults=()=>{{}};
vm.productSupportedCreaseTypes=()=>['压线','净料','毛片'];
vm.showToast=()=>{{}};
vm.bomEditor={{}};
{chr(10).join(bindings)}
function legacy(overrides={{}}){{
 const detail={{id:2496,version:2,customer_id:7,product_code:'WC',
   box_style:'WC五层钉箱',classification:{{kind:'box',box_style:'A1/0201 普通开槽箱'}},
   splice_mode:null,pieces_per_box:null,flap_mm:null,production_process:null,unit_needs_review:true,
   print_content:'单色印刷',printing_colors:'黑色',printing_plate_mode:'no_plate',
   length_mm:400,width_mm:300,height_mm:200,report_length_mm:1430,report_width_mm:500,
   crease_type:'压线',crease_left_mm:150,crease_middle_mm:200,crease_right_mm:150,...overrides}};
 vm.productForm=vm.hydrateProductForm(detail);
 return vm.productForm;
}}
"""


def test_unrelated_purchase_and_printing_edit_keeps_exact_legacy_structure(tmp_path):
    _run_node(tmp_path, runtime() + """
const form=legacy();
assert.equal(form.box_style,'WC五层钉箱');
for(const key of ['splice_mode','pieces_per_box','flap_mm','production_process']) assert.equal(form[key],null,key);
form.material_id=88; form.report_length_mm=1420; form._printing_colors=['红色']; form._printing_colors_touched=true;
const payload=vm.buildProductWritePayload();
assert.deepEqual(payload.structure_edit_fields,[]);
assert.equal(payload.box_style,'WC五层钉箱');
for(const key of ['splice_mode','pieces_per_box','flap_mm','production_process']) assert.equal(payload[key],null,key);
assert.equal(payload.material_id,88); assert.equal(payload.report_length_mm,1420);
assert.equal(payload.printing_colors,'红色');
assert.ok(!Object.keys(payload).some(key=>key.startsWith('_structure_')));
""")


def test_known_structure_is_preserved_without_declaring_an_edit(tmp_path):
    _run_node(tmp_path, runtime() + """
const form=legacy({splice_mode:'double',pieces_per_box:2,flap_mm:40,production_process:'钉箱',unit_needs_review:false});
form.production_notes='核对采购';
const payload=vm.buildProductWritePayload();
assert.deepEqual(payload.structure_edit_fields,[]);
assert.equal(payload.splice_mode,'double'); assert.equal(payload.pieces_per_box,2);
assert.equal(payload.flap_mm,40); assert.equal(payload.production_process,'钉箱');
""")


def test_reset_structure_retains_confirmed_procurement_and_printing(tmp_path):
    _run_node(tmp_path, runtime() + """
const form=legacy({box_style:'A1/0201 普通开槽箱',splice_mode:'single',pieces_per_box:1,flap_mm:30,production_process:'无需结合',unit_needs_review:false});
vm.resetProductStructure('flap_mm');
form.splice_mode=null; vm.onProductSpliceChanged();
vm.resetProductStructure('production_process');
form.box_style='WC 五层钉箱'; vm.onProductBoxStyleChange();
vm.normalizeProductDimensions(form);
const payload=vm.buildProductWritePayload();
assert.deepEqual(payload.structure_edit_fields,ERPProductStructure.fields);
assert.equal(payload.box_style,'WC 五层钉箱');
for(const key of ['splice_mode','pieces_per_box','flap_mm','production_process']) assert.equal(payload[key],null,key);
assert.equal(payload.report_length_mm,1430); assert.equal(payload.report_width_mm,500);
assert.equal(payload.printing_colors,'黑色'); assert.equal(payload.printing_plate_mode,'no_plate');
assert.equal(payload.crease_middle_mm,200);
""")


def test_explicit_splice_and_flap_edits_are_the_only_declared_fields(tmp_path):
    _run_node(tmp_path, runtime() + """
const form=legacy();
form.splice_mode='double'; vm.onProductSpliceChanged();
form.flap_mm=45; vm.markProductStructureEdited(['flap_mm']);
const payload=vm.buildProductWritePayload();
assert.deepEqual(payload.structure_edit_fields,['splice_mode','pieces_per_box','flap_mm']);
assert.equal(payload.pieces_per_box,2); assert.equal(payload.flap_mm,45);
assert.equal(payload.production_process,null);
""")


def test_box_name_can_be_reset_without_clearing_other_confirmed_structure(tmp_path):
    _run_node(tmp_path, runtime() + """
const form=legacy({splice_mode:'double',pieces_per_box:2,flap_mm:40,production_process:'打钉',unit_needs_review:false});
form.box_style=null; vm.onProductBoxStyleChange();
const payload=vm.buildProductWritePayload();
assert.deepEqual(payload.structure_edit_fields,['box_style']);
assert.equal(payload.box_style,null); assert.equal(payload.flap_mm,40);
assert.equal(payload.splice_mode,'double'); assert.equal(payload.pieces_per_box,2);
assert.equal(payload.production_process,'打钉'); assert.equal(payload.report_length_mm,1430);
""")


def test_compatible_name_only_marks_name_but_new_box_type_marks_all_links(tmp_path):
    _run_node(tmp_path, runtime() + """
const form=legacy();
assert.ok(vm.productCompatibleBoxStyleOptions().includes('WC 五层钉箱'));
form.box_style='A1/0201 普通开槽箱'; vm.onProductBoxStyleChange();
assert.deepEqual(form._structure_edit_fields,['box_style']);
assert.equal(form.flap_mm,null); assert.equal(form.splice_mode,null);
form.box_style='半开槽箱'; vm.onProductBoxStyleChange();
assert.deepEqual(form._structure_edit_fields,ERPProductStructure.fields);
assert.equal(form.splice_mode,'single'); assert.equal(form.pieces_per_box,1); assert.equal(form.flap_mm,30);
assert.equal(vm.buildProductWritePayload().production_process,null);
""")


def test_unknown_structure_blocks_automatic_and_forced_recommendations(tmp_path):
    _run_node(tmp_path, runtime() + """
(async()=>{
 const form=legacy({report_length_mm:null,report_width_mm:null,crease_type:null,crease_left_mm:null,crease_middle_mm:null,crease_right_mm:null});
 let calls=0; vm.requestBoxTypeRecommendation=async()=>{calls++;throw Error('must not request default30');};
 await vm.autoApplyProductRecommendations();
 await vm.autoApplyProductRecommendations({force:true});
 assert.equal(calls,0); assert.equal(form.flap_mm,null); assert.equal(form.report_length_mm,null);
 assert.match(form._recommendation_message,/未确认/);
 form.report_length_mm=1430; form.report_width_mm=500;
 assert.equal(vm.buildProductWritePayload().report_length_mm,1430);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_recommendation_never_changes_existing_structure_and_keeps_manual_dimensions(tmp_path):
    _run_node(tmp_path, runtime() + """
(async()=>{
 const form=legacy({splice_mode:'double',pieces_per_box:2,flap_mm:45});
 vm.requestBoxTypeRecommendation=async()=>({auto_calculated:true,splice_mode:'single',pieces_per_box:1,flap_mm:30,report_length_mm:999,report_width_mm:999,crease_left_mm:9,crease_middle_mm:9,crease_right_mm:9});
 await vm.autoApplyProductRecommendations();
 assert.equal(form.splice_mode,'double'); assert.equal(form.pieces_per_box,2); assert.equal(form.flap_mm,45);
 assert.equal(form.report_length_mm,1430); assert.equal(form.crease_middle_mm,200);
 assert.deepEqual(vm.buildProductWritePayload().structure_edit_fields,[]);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_new_product_defaults_and_payload_remain_unchanged(tmp_path):
    _run_node(tmp_path, runtime() + """
vm.productForm=vm.hydrateProductForm({box_style:'A1/0201 普通开槽箱'});
assert.equal(vm.productForm.splice_mode,'single'); assert.equal(vm.productForm.pieces_per_box,1); assert.equal(vm.productForm.flap_mm,30);
assert.ok(!('structure_edit_fields' in vm.buildProductWritePayload()));
""")


def test_existing_external_completion_keeps_its_original_editor_classification(tmp_path):
    _run_node(tmp_path, runtime() + """
const form=legacy({box_style:'外购护角',classification:{needs_supply_completion:true,category_code:'paper_corner_guard'}});
assert.equal(form.box_style,'其他'); assert.equal(form.supply_mode,'external_purchase');
assert.equal(form._structure_preserve,false);
const payload={box_style:'其他',production_process:''};
ERPProductStructure.applyPayload(form,payload);
assert.equal(payload.box_style,'其他'); assert.equal(payload.production_process,'');
""")


def test_explicit_joining_and_mold_actions_survive_without_guessing_other_structure(tmp_path):
    _run_node(tmp_path, runtime() + """
const form=legacy();
form._production_processes=['打钉'];
vm.onProductProcessesChanged({target:{value:'打钉',checked:true}});
let payload=vm.buildProductWritePayload();
assert.deepEqual(payload.structure_edit_fields,['production_process']);
assert.equal(payload.production_process,'打钉'); assert.equal(payload.flap_mm,null);
vm.resetProductStructure('production_process');
form._production_processes=['模切']; form.mold_tool_id=77;
vm.onProductProcessesChanged({target:{value:'模切',checked:true}});
payload=vm.buildProductWritePayload();
assert.equal(payload.production_process,'模切'); assert.equal(payload.mold_tool_id,77);
assert.ok(!payload.production_process.includes('无需结合'));
vm.resetProductStructure('production_process');
payload=vm.buildProductWritePayload();
assert.equal(payload.production_process,null); assert.equal(payload.mold_tool_id,null);
""")


def test_late_recommendation_cannot_fill_dimensions_after_unknown_reset(tmp_path):
    _run_node(tmp_path, runtime() + """
(async()=>{
 const form=legacy({splice_mode:'single',pieces_per_box:1,flap_mm:30,report_length_mm:null,report_width_mm:null});
 let finish;
 vm.requestBoxTypeRecommendation=()=>new Promise(resolve=>{finish=resolve;});
 const pending=vm.autoApplyProductRecommendations();
 await Promise.resolve();
 assert.ok(finish,'the real recommendation method started');
 vm.resetProductStructure('flap_mm');
 finish({auto_calculated:true,report_length_mm:1430,report_width_mm:500,flap_mm:30});
 await pending;
 assert.equal(form.flap_mm,null); assert.equal(form.report_length_mm,null);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_real_product_template_compiles_with_unknown_controls(tmp_path):
    _run_node(tmp_path, f"""
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync({json.dumps(str(ROOT / 'static/index.html'))},'utf8');
const sandbox=vm.createContext({{console}});
vm.runInContext(fs.readFileSync({json.dumps(str(ROOT / 'static/vendor/vue-3.5.40.global.prod.js'))},'utf8'),sandbox);
const modal='<div v-if="modal.type === \\'product\\'">'+html.split('<div v-else-if="modal.type === \\'product\\'">')[1].split('<div v-else-if="modal.type === \\'finishedStockPolicy\\'">')[0];
sandbox.Vue.compile(modal,{{decodeEntities:value=>value,onError:error=>{{throw error;}}}});
for(const match of html.matchAll(/<script(?:\\s[^>]*)?>([\\s\\S]*?)<\\/script>/g)) new vm.Script(match[1]);
""")


def test_real_template_wires_explicit_actions_and_preserves_normal_save():
    assert 'src="/static/js/product-structure-edit.js?' in INDEX
    assert '@change="onProductSpliceChanged"' in INDEX
    assert '@input="markProductStructureEdited([\'flap_mm\'])"' in INDEX
    assert '@click="resetProductStructure(\'production_process\')"' in INDEX
    assert '@click="saveModal()"' in INDEX
