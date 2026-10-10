import json
import subprocess
from pathlib import Path

from app.services.sheet_cutting_contract import SheetCuttingContract
from tests.test_p1_09c_58_product_editor_race_contract import _method_body, _run_node

ROOT = Path(__file__).resolve().parents[1]


def test_browser_contract_matches_decimal_backend_and_rejects_bad_inputs(tmp_path):
    expected = SheetCuttingContract("298.5", "444.25", 2, 3, True, 4).to_snapshot()
    _run_node(f"""
const assert=require('node:assert/strict');
const cutting=require({json.dumps(str(ROOT / 'static/js/sheet-cutting.js'))});
const actual=cutting.contract('298.5','444.25',{{length_parts:2,width_parts:3,mold_count:4,is_die_cut:true}});
assert.deepEqual(actual,{json.dumps(expected)});
assert.throws(()=>cutting.contract(340,200,{{length_parts:0,width_parts:1,mold_count:1,is_die_cut:false}}));
assert.throws(()=>cutting.contract('1.001',200,{{length_parts:1,width_parts:1,mold_count:1,is_die_cut:false}}));
assert.throws(()=>cutting.contract(340,200,{{length_parts:1,width_parts:1,mold_count:2,is_die_cut:false}}));
assert.equal(cutting.contract(200,100,{{length_parts:1,width_parts:1,mold_count:1,is_die_cut:false}}).supplier_length_mm,'200');
assert.match(cutting.summary(actual),/4模/);
""", tmp_path, "sheet-cutting-ui.cjs")


def test_supplier_actual_size_browser_matches_backend_and_keeps_defaults(tmp_path):
    expected=SheetCuttingContract(375,226,2,3,True,4,750,700).to_snapshot()
    dimension=_method_body("productSupplierDimension(key,axis) {", "setProductSupplierDimension(")
    change=_method_body("setProductSupplierDimension(key,axis,value) {", "resetProductSupplierDimensions(")
    sync=_method_body("syncProductSheetCuttingSettings() {", "hydrateProductForm(")
    _run_node(f"""
const assert=require('node:assert/strict');
global.window={{ERPSheetCutting:require({json.dumps(str(ROOT/'static/js/sheet-cutting.js'))})}};
const part={{length_parts:2,width_parts:3,mold_count:4,is_die_cut:true}};
const vm={{productForm:{{report_length_mm:375,report_width_mm:226,sheet_cutting_settings:{{schema_version:2,whole:part}}}},productUsesMold:()=>true,isTelescopingLidBoxStyle:()=>false}};
vm.productSupplierDimension=new Function('key','axis',{json.dumps(dimension)}).bind(vm);
vm.setProductSupplierDimension=new Function('key','axis','value',{json.dumps(change)}).bind(vm);
vm.syncProductSheetCuttingSettings=new Function({json.dumps(sync)}).bind(vm);
vm.setProductSupplierDimension('whole','width','700');vm.syncProductSheetCuttingSettings();
assert.equal(vm.productForm.sheet_cutting_settings.schema_version,3);
assert.equal(vm.productForm.report_width_mm,226);
const snap=window.ERPSheetCutting.contract(375,226,vm.productForm.sheet_cutting_settings.whole);
assert.deepEqual(snap,{json.dumps(expected)});assert.match(window.ERPSheetCutting.summary(snap),/先修边至750×678/);
assert.throws(()=>window.ERPSheetCutting.contract(375,226,{{...part,actual_supplier_length_mm:749,actual_supplier_width_mm:700}}));
""",tmp_path,'supplier-actual-default.cjs')


def test_v2_draft_refresh_replaces_geometry_and_quantity_without_losing_remark(tmp_path):
    body = _method_body("restoreSupplierDraftEdits(previousDraft, nextDraft) {", "async refreshSupplierRequisitionDraftAfterInventoryReservation(")
    key = _method_body("supplierDraftEditKey(line) {", "restoreSupplierDraftEdits(")
    _run_node(f"""
const assert=require('node:assert/strict');
const vm={{draftGroupLines:g=>g.lines,recalculateSupplierDraftLine:()=>{{throw new Error('must not use legacy geometry');}}}};
vm.supplierDraftEditKey=new Function('line',{json.dumps(key)}).bind(vm);
vm.restoreSupplierDraftEdits=new Function('previousDraft','nextDraft',{json.dumps(body)}).bind(vm);
const source=[{{order_item_id:8,component_type:'whole'}}];
const old={{supplier_groups:[{{supplier_name:'supplier',lines:[{{source_items:source,sheet_cutting_snapshot:{{length_parts:1}},cutting_mode:'一开二',report_length_mm:340,report_width_mm:200,order_purpose_sheet_qty:200,stock_purpose_sheet_qty:5,remark:'保留备注'}}]}}]}};
const fresh={{supplier_groups:[{{supplier_name:'supplier',lines:[{{source_items:source,sheet_cutting_snapshot:{{length_parts:2}},cutting_mode:'一开四',report_length_mm:680,report_width_mm:200,order_purpose_sheet_qty:100}}]}}]}};
const line=vm.restoreSupplierDraftEdits(old,fresh).supplier_groups[0].lines[0];
assert.equal(line.report_length_mm,680);assert.equal(line.report_width_mm,200);
assert.equal(line.cutting_mode,'一开四');assert.equal(line.purchase_total_sheet_qty,100);
assert.equal(line.stock_purpose_sheet_qty,0);assert.equal(line.remark,'保留备注');
""", tmp_path, "sheet-cutting-draft-refresh.cjs")
