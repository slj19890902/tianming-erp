const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const ui=require('../static/ui/manual-sheet-deduction.js');
const row=()=>({lot_id:7,version:1,selectable:true,max_stock_quantity:20,available_stock_quantity:204,
  stock_yield_per_sheet:1,review_hash:'a'.repeat(64),warning_codes:['GENERAL_SEMI_FINISHED_STOCK'],inputQuantity:'7'});
const data=()=>({expected_order_item_revision:'b'.repeat(64),remaining_required_piece_qty:40,items:[row()]});
function ctx() {
  ui.pending.clear();
  const result={snapshotId:1,componentType:'whole',...ui.component.data(),...ui.component.methods,
    emitted:[],$emit(name,value){this.emitted.push([name,value]);}};
  Object.defineProperty(result,'identity',{get(){return ui.component.computed.identity.call(this);}});
  return result;
}
test('operator quantity is explicit, integer and capped, never auto-filled',async()=>{
  assert.equal(ui.chosenQuantity({...row(),inputQuantity:''}),0);
  for(const input of [0,-1,1.2,21,'bad'])assert.equal(ui.chosenQuantity({...row(),inputQuantity:input}),0);
  assert.equal(ui.chosenQuantity({...row(),inputQuantity:12}),12);
  global.axios={get:async()=>({data:data()})};const state=ctx();await state.open();
  assert.equal(state.rows[0].inputQuantity,'');assert.equal(ui.pending.size,0);
});
test('uncertain retry and reopen retain exact batch, quantity and idempotency',async()=>{
  const calls=[];global.axios={get:async()=>({data:data()}),post:async(url,payload)=>{
    calls.push(structuredClone(payload));if(calls.length===1)throw Error('network lost');
    return {data:{message:'成功',stock_quantity:7,remaining_required_piece_qty:33}};
  }};
  const state=ctx();await state.open();state.rows[0].inputQuantity=7;await state.reserve(state.rows[0]);
  assert.equal(state.uncertain,true);assert.equal(ui.pending.size,1);
  const reopened={snapshotId:1,componentType:'whole',...ui.component.data(),...ui.component.methods,emitted:[],
    $emit(name,value){this.emitted.push([name,value]);}};
  Object.defineProperty(reopened,'identity',{get(){return ui.component.computed.identity.call(this);}});
  await reopened.open();assert.equal(reopened.uncertain,true);
  reopened.rows[0].inputQuantity=19;await reopened.reserve(null);
  assert.deepEqual(calls[0],calls[1]);assert.equal(calls[1].stock_quantity,7);assert.equal(ui.pending.size,0);
});
test('refresh failure after success does not retain or replay the write',async()=>{
  let reads=0,writes=0;global.axios={get:async()=>{if(++reads>1)throw Error('refresh lost');return {data:data()};},
    post:async()=>{writes++;return {data:{message:'已成功预占',stock_quantity:3}};}};
  const state=ctx();await state.open();state.rows[0].inputQuantity=3;await state.reserve(state.rows[0]);
  assert.equal(writes,1);assert.equal(ui.pending.size,0);assert.equal(state.uncertain,false);
  assert.match(state.error,/抵扣已成功/);assert.equal(state.rows.length,0);
});
test('explicit stale rejection requires new preview rather than retrying changed payload',async()=>{
  global.axios={get:async()=>({data:data()}),post:async()=>{const e=Error('stale');e.response={status:409,data:{detail:'库存已变'}};throw e;}};
  const state=ctx();await state.open();state.rows[0].inputQuantity=3;await state.reserve(state.rows[0]);
  assert.equal(ui.pending.size,0);assert.equal(state.uncertain,false);assert.equal(state.rows.length,0);
  assert.match(state.error,/重新查看/);
});
test('Vue component template compiles and ordinary and BOM procurement entry points are connected',()=>{
  const sandbox={console};vm.runInNewContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),sandbox);
  assert.equal(typeof sandbox.Vue.compile(ui.component.template,{decodeEntities:value=>value}),'function');
  const html=fs.readFileSync('static/index.html','utf8');
  assert.ok((html.match(/<manual-sheet-deduction /g)||[]).length>=8);
  assert.match(html,/order-manual-sheet-entry/);
  assert.match(html,/reported-manual-sheet-entry/);
  assert.match(html,/TMManualSheetDeduction.component/);
});

test('manual write blocks parent saving while outcome is uncertain and clears after reconciliation',async()=>{
  global.axios={get:async()=>({data:data()}),post:async()=>{throw Error('network lost');}};
  const state=ctx();await state.open();state.rows[0].inputQuantity=3;await state.reserve(state.rows[0]);
  assert.equal(state.emitted.filter(item=>item[0]==='operation-state').at(-1)[1].blocked,true);
  global.axios.post=async()=>({data:{message:'成功',stock_quantity:3}});await state.reserve(null);
  assert.equal(state.emitted.filter(item=>item[0]==='operation-state').at(-1)[1].blocked,false);
});

test('successful BOM deduction refreshes save fingerprint and preserves remark and separate stock purchase',async()=>{
  const html=fs.readFileSync('static/index.html','utf8');
  const start=html.indexOf('          async refreshManualBomSheetDeduction(source, data, parentLine=null) {');
  const end=html.indexOf('          async loadReceiptStorage()',start);
  const code=html.slice(start,end).trim().replace(/,$/,'');
  const root=vm.runInNewContext('({'+code+'})',{axios:{post:async()=>({data:{
    order_purpose_sheet_qty:33,purpose_plan_fingerprint:'fresh-fingerprint',remaining_required_piece_qty:33}})}});
  const states=[];Object.assign(root,{showToast(){},setManualSheetOperation(value){states.push(value)},
    requisitionBatchLinePayload(){return {}},initializePurchasePurposeLine(){},loadRequisition:async()=>{}});
  const line={bom_snapshot_id:1,is_composite_component:true,stock_purpose_sheet_qty:5,remark:'保留备注'};
  await root.refreshManualBomSheetDeduction(line,{message:'成功',remaining_required_piece_qty:33,semi_finished_reserved_piece_qty:7});
  assert.equal(line.order_purpose_sheet_qty,33);assert.equal(line.purchase_total_sheet_qty,38);
  assert.equal(line.stock_purpose_sheet_qty,5);assert.equal(line.remark,'保留备注');
  assert.equal(line.purpose_plan_fingerprint,'fresh-fingerprint');assert.equal(states.at(-1).blocked,false);
});
test('pending operation is isolated by signed-in operator',()=>{
  const a=ctx();a.actorId=1;const b=ctx();b.actorId=2;
  assert.notEqual(a.identity,b.identity);
});

test('late read and write responses never populate a different signed-in actor scope',async()=>{
  let resolveRead;
  global.axios={get:()=>new Promise(resolve=>{resolveRead=resolve})};
  const state=ctx();state.actorId=1;
  const read=state.open();state.actorId=2;ui.component.watch.identity.call(state);
  resolveRead({data:data()});await read;
  assert.equal(state.preview,null);assert.equal(state.rows.length,0);assert.equal(state.loading,false);
  let resolveWrite;
  global.axios={get:async()=>({data:data()}),post:()=>new Promise(resolve=>{resolveWrite=resolve})};
  state.actorId=1;await state.open();state.rows[0].inputQuantity=3;
  const write=state.reserve(state.rows[0]);state.actorId=2;ui.component.watch.identity.call(state);
  resolveWrite({data:{message:'原操作者成功',stock_quantity:3}});await write;
  assert.equal(state.preview,null);assert.equal(state.message,'');assert.equal(ui.pending.size,0);
  assert.equal(state.emitted.filter(item=>item[0]==='reserved').length,0);
});

test('re-mounted pending component can reconcile while parent remains disabled',async()=>{
  global.axios={get:async()=>({data:data()}),post:async()=>{throw Error('lost response')}};
  const original=ctx();await original.open();original.rows[0].inputQuantity=3;await original.reserve(original.rows[0]);
  const reopened={snapshotId:1,componentType:'whole',disabled:true,...ui.component.data(),...ui.component.methods,$emit(){}};
  Object.defineProperty(reopened,'identity',{get(){return ui.component.computed.identity.call(this)}});
  ui.component.created.call(reopened);
  assert.equal(reopened.uncertain,true);await reopened.open();assert.equal(reopened.opened,true);
  let posted;global.axios.post=async(path,payload)=>{posted=payload;return {data:{message:'原回执',stock_quantity:3}}};
  await reopened.reserve(null);assert.equal(posted.stock_quantity,3);assert.equal(ui.pending.size,0);
});
