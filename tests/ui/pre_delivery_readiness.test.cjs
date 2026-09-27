const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('static/index.html','utf8');
const body=html.slice(html.indexOf('          scheduleTianhuaReadiness() {'),html.indexOf('          async loadTianhuaBatch() {'));
function setup(){
  let callback;
  const pending=[];
  const box={setTimeout:f=>{callback=f;return 1},clearTimeout(){},axios:{post:(url,data,options)=>new Promise((resolve,reject)=>pending.push({url,data,options,resolve,reject}))}};
  vm.createContext(box);vm.runInContext('method=({'+body+'}).scheduleTianhuaReadiness',box);
  const row={item_id:1,row_no:1,order_item_id:7,final_delivery_qty:100,
    source_payload:{shortage_diagnostic:{effective_inbound:999},candidates:[{order_item_id:7,_selected:true,_qty:100}]}};
  const state={user:{id:1},tianhuaPreimport:{batchId:3,items:[row]}};
  return {state,row,pending,start:()=>{box.method.call(state);return callback()},schedule:()=>box.method.call(state)};
}
function response(quantity){return {data:{items:[{item_id:1,source_payload:{shortage_diagnostic:{effective_inbound:quantity}},fulfillment_label:'checked',pick_locations:[]}]}}}
test('readiness preview uses current allocations without saving a draft',async()=>{
  const x=setup(), done=x.start();
  assert.equal(x.pending[0].url,'/api/deliveries/tianhua-preimport/3/readiness-preview');
  assert.equal(x.pending[0].data.items[0].allocations[0].quantity,100);
  assert.equal(x.pending[0].options.timeout,15000);
  x.pending[0].resolve(response(40));await done;
  assert.equal(x.row.source_payload.shortage_diagnostic.effective_inbound,40);
  assert.equal(x.row.source_payload.candidates[0]._qty,100);
  assert.equal(x.row._readinessLoading,false);
});
test('older response cannot replace a newer input result',async()=>{
  const x=setup(), first=x.start();x.row.final_delivery_qty=200;const second=x.start();
  x.pending[1].resolve(response(70));await second;x.pending[0].resolve(response(30));await first;
  assert.equal(x.row.source_payload.shortage_diagnostic.effective_inbound,70);
});
test('reloaded batch and changed account ignore pending responses',async()=>{
  for(const change of [x=>x.state.tianhuaPreimport.items=[],x=>x.state.user.id=2]){
    const x=setup(),done=x.start();change(x);x.pending[0].resolve(response(50));await done;
    assert.equal(x.row.source_payload.shortage_diagnostic.effective_inbound,999);
  }
});
test('timeout ends loading and visibly marks stale coverage',async()=>{
  const x=setup(),done=x.start();x.pending[0].reject(Error('timeout'));await done;
  assert.equal(x.row._readinessLoading,false);assert.match(x.row._readinessError,/刷新失败/);
});
test('image recognition rows do not use Excel readiness preview',()=>{
  const x=setup();x.row.source_payload={};x.schedule();assert.equal(x.pending.length,0);
});
test('selected alternative with no quantity cannot silently fall back to the old order',async()=>{
  const x=setup();x.row.source_payload.candidates[0]._qty=null;await x.start();
  assert.equal(x.pending.length,0);assert.match(x.row._readinessError,/分配数量/);assert.equal(x.row._readinessLoading,false);
});
