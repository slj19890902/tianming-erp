const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const ts=require(process.env.ERP_UI_TYPESCRIPT_LIBRARY);
const source=fs.readFileSync(path.resolve(__dirname,'../src/utils/orderRequisitionHold.ts'),'utf8');
const api={};vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:api});
const row=(id,status,candidates=[],selected=null)=>({client_line_id:id,status,candidates:candidates.map(order_item_id=>({order_item_id,order_number:'UAT-'+order_item_id})),warnings:[],selected_previous_order_item_id:selected});
const plain=value=>JSON.parse(JSON.stringify(value));
test('identity matching survives response reorder and no previous batch',()=>{
  const out=api.resolveHoldPreview(['a','b'],[row('b','hold',[9],9),row('a','normal')],{});
  assert.deepEqual(plain(api.previousBatchSelections(out,['a','b'])),[{client_line_id:'b',previous_order_item_id:9}]);
});
test('ambiguous batches require an explicit choice even with an automatic recommendation',()=>{
  const out=api.resolveHoldPreview(['a'],[row('a','hold',[1,2],2)],{});
  assert.throws(()=>api.previousBatchSelections(out,['a']),/请选择/);
});
test('zero explicitly chooses normal requisition; valid manual previous batch survives recheck',()=>{
  for(const selected of [0,7]){
    const out=api.resolveHoldPreview(['a'],[row('a','select_required',[7,8])],{a:selected});
    assert.deepEqual(plain(api.previousBatchSelections(out,['a'])),[{client_line_id:'a',previous_order_item_id:selected}]);
  }
});
test('a removed candidate cannot retain a stale manual decision',()=>{
  const out=api.resolveHoldPreview(['a'],[row('a','select_required',[7,8])],{a:99});
  assert.throws(()=>api.previousBatchSelections(out,['a']),/请选择/);
});
test('missing code can be explicitly normal but cannot invent a batch',()=>{
  const blocked=row('a','blocked');
  assert.throws(()=>api.previousBatchSelections(api.resolveHoldPreview(['a'],[blocked],{}),['a']));
  assert.deepEqual(plain(api.previousBatchSelections(api.resolveHoldPreview(['a'],[blocked],{a:0}),['a'])),[{client_line_id:'a',previous_order_item_id:0}]);
});
test('partial duplicate foreign and malformed responses fail closed',()=>{
  for(const rows of [[],[row('foreign','normal')],[row('a','unknown')],[row('a','hold',[7],8)],[row('a','hold',[7])],[row('a','normal',[7])],[row('a','select_required',[7,7])]])assert.throws(()=>api.resolveHoldPreview(['a'],rows,{}));
  assert.throws(()=>api.resolveHoldPreview(['a','b'],[row('a','normal'),row('a','normal')],{}));
});
