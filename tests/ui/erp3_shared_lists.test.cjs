const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('static/index.html','utf8');
const browser={};if(fs.existsSync('static/js/list-read-state.js'))vm.runInNewContext(fs.readFileSync('static/js/list-read-state.js','utf8'),{window:browser});
const vue={console};vm.runInNewContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),vue);
function fixture(kind){
 const pending=[],controllers=new Map(),name=kind==='customers'?'loadCustomers':'loadOrders';
 const scope=kind==='customers'?'customers':'orders:queue:7';
 const names=[name,'beginLatestRequest','finishLatestRequest','isCancelledRequest'];
 const methods=names.map(n=>{const start=html.search(new RegExp(`          (?:async )?${n}\\(`));return html.slice(start,html.indexOf('\n          },',start)+12)}).join(',');
 const methodsObject=vm.runInNewContext(`({${methods}})`,{window:browser,AbortController,latestRequestControllers:controllers,axios:{get:(url,config)=>new Promise((resolve,reject)=>pending.push({url,config,resolve,reject}))}});
 const state=vue.Vue.reactive({user:{id:1},authGeneration:1,activePage:kind,orderWorkspace:'queue',pageSize:10,pages:{customers:1,orders:1},filters:{orderCustomer:7},
  customers:[{id:11}],customersTotal:1,orders:[{id:11}],ordersTotal:1,ordersUnfinishedTotal:1,
  listReadStates:{[kind+':list']:{identity:'1:1:'+scope,loaded:true,loading:false,error:''}},hasPermission:()=>true,desktopListPageSize:()=>10,orderListPageSize:()=>10,
  errorMessage:e=>e.message,cancelOrderGroupDetailRequests(){},orderListGeneration:0,orderBomDemandSaveState:{},...methodsObject});
 return {state,pending,read:()=>state[name]()};
}
for(const kind of ['customers','orders']){
 test(kind+': refresh failure retains the snapshot and exposes a local retry state',async()=>{
  const f=fixture(kind),read=f.read();assert.equal(f.state[kind][0].id,11);assert.equal(f.state.listReadStates[kind+':list'].loading,true);
  f.pending[0].reject(new Error('offline'));assert.equal(await read,false);assert.equal(f.state[kind][0].id,11);assert.equal(f.state.listReadStates[kind+':list'].error,'offline');assert.equal(f.state.listReadStates[kind+':list'].loading,false);
 });
 test(kind+': late success after navigation/account change cannot replace the current snapshot',async()=>{
  for(const change of [s=>s.activePage='dashboard',s=>{s.user={id:2};s.authGeneration++;}]){
   const f=fixture(kind),read=f.read();change(f.state);f.pending[0].resolve({data:{items:[{id:99}],total:1}});assert.equal(await read,false);assert.notEqual(f.state[kind][0]?.id,99);
  }
 });
 test(kind+': latest query wins and the previous completion cannot stop its indicator',async()=>{
  const f=fixture(kind),old=f.read(),current=f.read();assert.equal(f.pending[0].config.signal.aborted,true);
  f.pending[0].resolve({data:{items:[{id:88}],total:1}});await old;assert.equal(f.state.listReadStates[kind+':list'].loading,true);
  f.pending[1].resolve({data:{items:[{id:22}],total:1}});assert.equal(await current,true);assert.equal(f.state[kind][0].id,22);assert.equal(f.state.listReadStates[kind+':list'].loaded,true);
 });
}
test('orders: changing the customer clears the previous customer before the next reply',async()=>{
 const f=fixture('orders');f.state.filters.orderCustomer=8;const read=f.read();assert.equal(f.state.orders.length,0);f.pending[0].resolve({data:{items:[],total:0}});await read;assert.equal(f.state.listReadStates['orders:list'].loaded,true);
});
for(const kind of ['customers','orders']){
 test(kind+': first read publishes reactive loading/error state, and cancellation releases it',async()=>{
  const f=fixture(kind);f.state.listReadStates={};const observed=[];const stop=vue.Vue.watchEffect(()=>observed.push([f.state.listReadStates[kind+':list']?.loading,f.state.listReadStates[kind+':list']?.error]));
  const read=f.read();await vue.Vue.nextTick();assert.equal(observed.at(-1)[0],true);f.pending[0].reject(new Error('offline'));await read;await vue.Vue.nextTick();assert.deepEqual(observed.at(-1),[false,'offline']);
  const retry=f.read();f.pending[1].reject(Object.assign(new Error('cancelled'),{code:'ERR_CANCELED'}));await retry;assert.equal(f.state.listReadStates[kind+':list'].loading,false);assert.equal(f.state.listReadStates[kind+':list'].error,'');stop();
 });
 test(kind+': returning to the same page cannot accept an older navigation generation',async()=>{
  const f=fixture(kind);f.state.pageLoadSequence=3;const read=f.read();f.state.activePage='dashboard';f.state.pageLoadSequence++;f.state.activePage=kind;f.state.pageLoadSequence++;
  f.pending[0].resolve({data:{items:[{id:999}],total:1}});assert.equal(await read,false);assert.notEqual(f.state[kind][0]?.id,999);
 });
}
test('orders: an out-of-range last page is corrected once without letting old finally release the replacement',async()=>{
 const f=fixture('orders');f.state.pages.orders=4;const load=f.read();f.pending[0].resolve({data:{items:[],total:21,page_size:10}});await new Promise(setImmediate);
 assert.equal(f.state.pages.orders,3);assert.equal(f.pending.length,2);assert.equal(f.state.listReadStates['orders:list'].loading,true);assert.equal(f.pending[1].config.params.page,3);
 f.pending[1].resolve({data:{items:[{id:21}],total:21,page_size:10}});assert.equal(await load,true);assert.equal(f.state.orders[0].id,21);assert.equal(f.state.listReadStates['orders:list'].loading,false);
});
