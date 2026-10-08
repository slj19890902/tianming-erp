'use strict';
// Actual composable, with explicit API promises and Vue lifecycle doubles for races.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const test=require('node:test'),assert=require('node:assert/strict'),ts=require(process.env.ERP_UI_TYPESCRIPT_LIBRARY);
const compile=name=>ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src',name),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const utility={};vm.runInNewContext(compile('utils/orderInventory.ts'),{exports:utility,Error});
const product={id:1,customer_id:1,version:2,product_code:'UNIT-ONLY',is_active:true,report_length_mm:800,report_width_mm:600,material_code:'KA-AB',flute_type:'AB',pieces_per_box:1,default_cutting_mode:'一开一'};
const candidate=()=>({lot_id:1,version:1,quantity_available:4,quantity_contract:{customer_id:1,product_id:1,customer_basis:1,physical_basis:1,physical_unit:'只'}});
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}}
function harness(){
 const env={customer:1,permitted:true,inputs:[{id:'stable-a',product:{...product},quantity:6}]},watchers=[],calls=[];
 const impl={finished:async()=>({items:[candidate()]}),semi:async()=>({items:[]}),preview:async(customer,items)=>({items:items.map(i=>({client_line_id:i.client_line_id,product_id:i.product_id,order_quantity:i.quantity,coverage_state:'partial',interaction_state:'ready',finished_planned_quantity:4,production_required_quantity:2,requisition_components:[{component_type:'whole',required_piece_quantity:2,remaining_required_piece_quantity:2,requisition_sheet_quantity:2}]}))})};
 const api={finishedCandidates(...a){calls.push(['finished',...a]);return impl.finished(...a)},semiCandidates(...a){calls.push(['semi',...a]);return impl.semi(...a)},previewDraft(...a){calls.push(['preview',...a]);return impl.preview(...a)}};
 const exported={},context={exports:exported,Error,require(id){if(id==='vue')return {ref:value=>({value}),computed:fn=>({get value(){return fn()}}),watch:(source,fn)=>watchers.push(fn)};if(id==='../api/client')return {warehouseApi:api};if(id==='../utils/orderInventory')return utility;throw Error('unexpected '+id)}};
 vm.runInNewContext(compile('composables/useOrderInventory.ts'),context);
 return {env,watchers,calls,impl,api:exported.useOrderInventory(()=>env.customer,()=>env.inputs,()=>env.permitted)};
}
test('real API workflow uses whole draft identities and two authoritative passes',async()=>{const h=harness();assert.equal(await h.api.refresh(),true);assert.equal(h.api.checked.value,true);assert.deepEqual(h.calls.map(c=>c[0]),['finished','semi','preview','preview']);assert.equal(h.calls[2][2][0].client_line_id,'stable-a');assert.equal(h.api.states.value['stable-a'].plan.finished[0].expected_version,1)});
test('missing warehouse authorization performs no candidate or preview calls',async()=>{const h=harness();h.env.permitted=false;assert.equal(await h.api.refresh(),false);assert.equal(h.calls.length,0);assert.equal(h.api.checked.value,false)});
test('customer/quantity changes invalidate in-flight response immediately',async()=>{const h=harness(),d=deferred();h.impl.finished=()=>d.promise;const pending=h.api.refresh();h.env.inputs[0].quantity=8;h.watchers[0]();d.resolve({items:[candidate()]});assert.equal(await pending,false);assert.equal(h.api.checked.value,false);assert.equal(Object.keys(h.api.states.value).length,0);assert.equal(h.api.busy.value,false)});
test('older request cannot clear busy or replace a later successful draft',async()=>{const h=harness(),d=deferred();let n=0;h.impl.finished=()=>++n===1?d.promise:Promise.resolve({items:[]});const old=h.api.refresh();h.env.inputs[0].quantity=7;h.watchers[0]();assert.equal(await h.api.refresh(),true);d.resolve({items:[candidate()]});assert.equal(await old,false);assert.equal(h.api.states.value['stable-a'].quantity,7);assert.equal(h.api.checked.value,true);assert.equal(h.api.busy.value,false)});
test('adopted batch version change at save requires explicit recheck, retaining draft',async()=>{const h=harness();assert.equal(await h.api.refresh(),true);h.impl.finished=async()=>({items:[{...candidate(),version:2}]});assert.equal(await h.api.refresh(true),false);assert.match(h.api.error.value,/版本已变化/);assert.equal(h.env.inputs[0].quantity,6);assert.equal(h.api.checked.value,false);assert.equal(await h.api.refresh(),true);assert.equal(h.api.states.value['stable-a'].plan.finished[0].expected_version,2)});
test('explicit no finished stock survives refresh and does not invent selections',async()=>{const h=harness();await h.api.refresh();await h.api.choose('stable-a','finished',null);assert.equal(h.api.states.value['stable-a'].plan.finished.length,0);assert.equal(await h.api.refresh(true),true);assert.equal(h.api.states.value['stable-a'].parts.finished.mode,'skipped');assert.equal(h.api.states.value['stable-a'].plan.finished.length,0)});
test('failed authority clears verified state and retains independent input values',async()=>{const h=harness();let n=0;h.impl.preview=async()=>++n===1?{items:[{client_line_id:'stable-a',product_id:1,order_quantity:6,coverage_state:'partial',requisition_components:[]}]}:{items:[]};assert.equal(await h.api.refresh(),false);assert.equal(h.api.checked.value,false);assert.equal(h.api.states.value['stable-a'].authority,null);assert.equal(h.env.inputs[0].quantity,6);assert.match(h.api.error.value,/缺少/)});

test('manual selection survives browsing another page and refreshes its original page before save',async()=>{
 const h=harness();h.impl.finished=async()=>({items:[]});
 h.impl.semi=async(id,payload,page)=>({items:page?[{lot_id:page===1?21:22,version:3,source:'signature',customer_bound:true,automatic_recommendation:true,direct_deduction_eligible:true,available_stock_quantity:10,stock_yield_per_sheet:1}]:[],total:40});
 await h.api.refresh();await h.api.browse('stable-a','whole',1);await h.api.choose('stable-a','whole',21);await h.api.browse('stable-a','whole',2);
 assert.equal(h.api.states.value['stable-a'].parts.whole.manual.some(c=>c.lot_id===21),true);
 const before=h.calls.length;assert.equal(await h.api.refresh(true),true);
 assert.deepEqual(h.calls.slice(before).filter(c=>c[0]==='semi'&&c[3]).map(c=>c[3]),[2,1]);
 assert.equal(h.api.states.value['stable-a'].plan.semi[0].lot_id,21);
});
