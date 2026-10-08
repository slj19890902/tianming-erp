"""Exercise the real page methods without calling a formal ERP service."""
from __future__ import annotations

import json

from test_p1_09c_58_product_editor_race_contract import _method_body, _run_node


def test_option_failure_is_specific_and_retry_preserves_other_results(tmp_path):
    body = _method_body("async ensureProductEditorOptions({force=false,refreshMolds=false,only=null} = {}) {", "moldToolSelectOptions() {")
    _run_node(f"""
const assert=require('node:assert/strict');
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();
let calls={{materials:0,molds:0,printingPlates:0}};
const vm={{isWorkshop:false,allMaterials:[],moldTools:[],printingPlates:[],productEditorOptionErrors:{{}},
 beginLatestRequest(key){{const c=new AbortController();latestRequestControllers.set(key,c);return c;}},
 finishLatestRequest(key,c){{if(latestRequestControllers.get(key)===c)latestRequestControllers.delete(key);}},
 async loadMaterials(){{calls.materials++;throw new Error('500');}},
 async loadMoldTools(){{calls.molds++;this.moldTools=[{{id:9}}];return true;}},
 async loadPrintingPlates(){{calls.printingPlates++;throw new Error('403');}}
}};
vm.ensureProductEditorOptions=new AsyncFunction('{{force=false,refreshMolds=false,only=null}}={{}}',{json.dumps(body, ensure_ascii=False)}).bind(vm);
(async()=>{{
 assert.equal(await vm.ensureProductEditorOptions(),false);
 assert.deepEqual(Object.keys(vm.productEditorOptionErrors),['materials','printingPlates']);
 assert.match(vm.productEditorOptionErrors.materials,/材质/);
 assert.match(vm.productEditorOptionErrors.printingPlates,/挂板/);
 assert.deepEqual(vm.moldTools,[{{id:9}}]);
 vm.loadMaterials=async()=>{{calls.materials++;vm.allMaterials=[{{id:5}}];return true;}};
 assert.equal(await vm.ensureProductEditorOptions({{force:true,only:'materials'}}),true);
 assert.deepEqual(calls,{{materials:2,molds:1,printingPlates:1}});
 assert.deepEqual(Object.keys(vm.productEditorOptionErrors),['printingPlates']);
 assert.equal(vm.productEditorOptionsLoading,false);
}})().catch(e=>{{console.error(e);process.exit(1);}});
""", tmp_path, "resource-isolation.cjs")


def test_drawing_collapse_retains_draft_and_new_product_loads_separately(tmp_path):
    body = _method_body("async toggleProductDrawingPanel(event) {", "async loadDrawingV2(")
    _run_node(f"""
const assert=require('node:assert/strict');
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let calls=0;
const vm={{productForm:{{id:4}},drawingV2:{{}},productDrawingPanelExpanded:false,
 async loadDrawingV2(){{calls++;this.drawingV2.loaded=true;}}
}};
vm.toggleProductDrawingPanel=new AsyncFunction('event',{json.dumps(body, ensure_ascii=False)}).bind(vm);
(async()=>{{
 await vm.toggleProductDrawingPanel({{target:{{open:false}}}});assert.equal(calls,0);
 await vm.toggleProductDrawingPanel({{target:{{open:true}}}});assert.equal(calls,1);
 vm.drawingV2.parameters={{length:340}};
 await vm.toggleProductDrawingPanel({{target:{{open:false}}}});
 await vm.toggleProductDrawingPanel({{target:{{open:true}}}});
 assert.equal(calls,1);assert.deepEqual(vm.drawingV2.parameters,{{length:340}});
 vm.productForm={{id:5}};vm.drawingV2={{}};
 await vm.toggleProductDrawingPanel({{target:{{open:true}}}});assert.equal(calls,2);
 vm.drawingV2={{reading:true}};
 await vm.toggleProductDrawingPanel({{target:{{open:true}}}});assert.equal(calls,2);
}})().catch(e=>{{console.error(e);process.exit(1);}});
""", tmp_path, "drawing-disclosure.cjs")
