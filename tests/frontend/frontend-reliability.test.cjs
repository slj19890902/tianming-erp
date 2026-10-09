const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const ts = require('../../frontend-v2/node_modules/typescript');
const root = path.resolve(__dirname,'../..');
const html = fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const read = file => fs.readFileSync(path.join(root,file),'utf8');
const AF = Object.getPrototypeOf(async function(){}).constructor;
function method(signature,next,args=[],sync=false) {
  const body=html.split(signature)[1].split(next)[0].replace(/}\s*,\s*$/,'');
  return new (sync?Function:AF)(...args,body);
}
function requests() {
  const pending=[], controllers=new Map();
  global.latestRequestControllers=controllers;
  return {
    pending, axios:{get:(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}))},
    methods:{
      user:{id:1},authGeneration:1,pageLoadSequence:1,activePage:'contracts',
      beginLatestRequest(key){controllers.get(key)?.abort();const c=new AbortController();controllers.set(key,c);return c},
      finishLatestRequest(key,c){if(controllers.get(key)===c)controllers.delete(key)},
      isCancelledRequest:e=>e.name==='AbortError',errorMessage:e=>e.message,
    }
  };
}
function contracts(r) {
  return {...r.methods,contractCustomer:{id:1},contractDraft:{items:[]},contractProducts:[],contractHistory:[],
    contractReadState:{},hydrateContract:d=>d,contractActionBusy:()=>false,
    contractReadContext:method('contractReadContext(kind) {','async readContractRows(',['kind'],true),
    readContractRows:method('async readContractRows(kind, keyword="") {','async searchContractProducts(',['kind','keyword']),
    loadContractHistory:method('async loadContractHistory() {','contractPayload() {'),
    editContract:method('async editContract(row) {','contractIdempotencyKey() {',['row']),
  };
}
test('materials: every refresh updates selector including rename/inactive/new and unfiltered entries',async()=>{
  const r=requests();let queries=[];
  const context={...r.methods,allMaterials:[{id:1,code:'OLD',is_active:true}],materialSort:'common',
    materialLayerFilter:3,materialSupplierFilter:'',activeSupplierNames:['S'],
    fetchAllMaterials:async params=>{queries.push(params);return params.layer_count?[{id:2,code:'NEW'}]:[{id:1,code:'UPDATED',is_active:false},{id:2,code:'NEW'}]}};
  const load=method('async loadMaterials() {','async openMaterialCandidateMaintenance() {');
  assert.equal(await load.call(context),true);
  assert.equal(context.materials.length,1);assert.equal(context.allMaterials.length,2);
  assert.equal(context.allMaterials[0].is_active,false);assert.equal(context.allMaterials[0].code,'UPDATED');
  assert.equal(queries.length,2);
  context.materialLayerFilter='';queries=[];await load.call(context);assert.equal(queries.length,1);
});
test('materials: old actor cannot populate selector after logout',async()=>{
  const r=requests();let resolve;
  const context={...r.methods,allMaterials:[],materialSort:'common',fetchAllMaterials:()=>new Promise(r=>resolve=r)};
  const task=method('async loadMaterials() {','async openMaterialCandidateMaintenance() {').call(context);
  context.authGeneration++;resolve([{id:9}]);assert.equal(await task,false);assert.equal(context.allMaterials.length,0);
});
test('contracts: latest customer wins even if server ignores cancellation; old failure remains silent',async()=>{
  const r=requests();global.axios=r.axios;const c=contracts(r);
  const a=c.loadContractHistory();c.contractCustomer={id:2};const b=c.loadContractHistory();
  r.pending[1].resolve({data:{items:[{id:20,customer_id:2}]}});assert.equal(await b,true);
  r.pending[0].reject(new Error('old failure'));assert.equal(await a,false);
  assert.equal(c.contractHistory[0].customer_id,2);assert.equal(c.contractReadState.history.error,'');
  const d=c.loadContractHistory();c.authGeneration++;r.pending[2].resolve({data:{items:[{id:99}]}});
  assert.equal(await d,false);assert.equal(c.contractHistory[0].id,20);
});
test('contracts: product search, page leave and current read failure preserve truthful state',async()=>{
  const r=requests();global.axios=r.axios;const c=contracts(r);
  const a=c.readContractRows('products','a'),b=c.readContractRows('products','b');
  r.pending[1].resolve({data:{items:[{id:2}]}});await b;
  r.pending[0].resolve({data:{items:[{id:1}]}});await a;assert.equal(c.contractProducts[0].id,2);
  const leave=c.loadContractHistory();c.activePage='customers';r.pending[2].resolve({data:{items:[{id:3}]}});await leave;
  assert.equal(c.contractHistory.length,0);
  c.activePage='contracts';const failed=c.loadContractHistory();r.pending[3].reject(new Error('network'));
  assert.equal(await failed,false);assert.equal(c.contractReadState.history.error,'network');assert.equal(c.contractReadState.history.loading,false);
});
test('contract editor: last selected row wins and an in-flight detail never overwrites typed edits',async()=>{
  const r=requests();global.axios=r.axios;global.window={scrollTo(){}};
  const c=contracts(r),a=c.editContract({id:1}),b=c.editContract({id:2});
  r.pending[1].resolve({data:{id:2,customer_id:1}});await b;
  r.pending[0].resolve({data:{id:1,customer_id:1}});await a;assert.equal(c.contractDraft.id,2);
  const later=c.editContract({id:3});c.contractDraft.remarks='new typed value';
  r.pending[2].resolve({data:{id:3,customer_id:1}});assert.equal(await later,false);assert.equal(c.contractDraft.remarks,'new typed value');
});
test('product: known successful create keeps id/version after drawing failure; retry uploads only',async()=>{
  let creates=0,uploads=0,fail=true;
  global.axios={post:async(url)=>{if(url==='/api/master/products'){creates++;return {data:{id:701,version:2,product_code:'TEST'}}}
    if(url==='/api/master/products/701/drawings'){uploads++;if(fail)throw new Error('upload disconnected');return {data:{id:1}}}throw new Error(url)}};
  const p={modal:{type:'product'},masterSavePending:false,masterPendingSaveOptions:null,masterChangeConfirm:{},
    productForm:{id:null},productFormSnapshot:null,productEditReturnContext:null,drawingFile:new Blob(['test']),
    masterCurrentForm(){return this.productForm},_productFormDirty:()=>true,_productBomDirty:()=>false,
    _productFormSaveFields(){return {...this.productForm}},hydrateProductForm:d=>({...d}),beginMasterEdit(){},
    buildProductWritePayload:()=>({product_code:'TEST'}),errorMessage:e=>e.message,showToast(){},loadProducts:async()=>true,
    closeModal(){this.modal=null},user:{id:1},authGeneration:1};
  const save=method('async saveModal() {','async confirmStockExternalDraft(row) {');
  assert.equal(await save.call(p),false);assert.equal(p.productForm.id,701);assert.equal(p.productForm.version,2);
  assert.equal(p.productSaveRecovery.id,701);assert.equal(p.masterSavePending,false);
  fail=false;assert.equal(await method('async retryProductDrawing() {','async saveModal() {').call(p),true);
  assert.equal(creates,1);assert.equal(uploads,2);assert.equal(p.drawingFile,null);assert.equal(p.productSaveRecovery,null);
});
function tsModule(file,dependencies={}) {
  const exports={};vm.runInNewContext(ts.transpileModule(read(file),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,
    {exports,require:name=>dependencies[name],setTimeout,clearTimeout,AbortController,DOMException});
  return exports;
}
test('auth: 503 is recoverable, 401 is login-required; late restore cannot replace a newer login',async()=>{
  let mode=503,resolve;
  const api={me:()=>mode===0?new Promise(r=>resolve=r):Promise.reject(Object.assign(new Error('fail'),{status:mode})),
    login:async()=>({user:{id:9},permissions:['orders.view']})};
  const auth=tsModule('frontend-v2/src/stores/auth.ts',{'pinia':{defineStore:(_,fn)=>fn},'vue':{ref:value=>({value})},'../api/client':{authApi:api}}).useAuthStore();
  await auth.restore();assert.ok(auth.restoreError.value);assert.equal(auth.user.value,null);assert.equal(auth.initialized.value,true);
  mode=401;await auth.restore();assert.equal(auth.restoreError.value,'');
  mode=0;const old=auth.restore();await auth.login('u','p',false);resolve({user:{id:1},permissions:[]});await old;
  assert.equal(auth.user.value.id,9);assert.deepEqual(Array.from(auth.permissions.value),['orders.view']);
});
test('request deadline covers stalled response body and honors external cancellation; no retry',async()=>{
  const {withRequestDeadline}=tsModule('frontend-v2/src/utils/requestLifecycle.ts');
  let calls=0,signal;
  await assert.rejects(withRequestDeadline(async s=>{calls++;signal=s;await Promise.resolve();return new Promise(()=>{})},()=>new Error('body timeout'),null,10),/body timeout/);
  assert.equal(calls,1);assert.equal(signal.aborted,true);
  const external=new AbortController();
  const task=withRequestDeadline(async()=>new Promise(()=>{}),()=>new Error('wrong timeout'),external.signal,100);
  external.abort();await assert.rejects(task,{name:'AbortError'});
  const already=new AbortController();already.abort();
  await assert.rejects(withRequestDeadline(async()=>{calls++;return 1},()=>new Error('wrong'),already.signal,10),{name:'AbortError'});
  assert.equal(calls,1);
  assert.equal(await withRequestDeadline(async()=>42,()=>new Error('wrong'),null,10),42);
});
test('draft protection: pristine, modified, returned-to-original, known saved, saving and uncertain',()=>{
  const events={},watchers=[],window={addEventListener:(k,v)=>events[k]=v,removeEventListener(){}};
  const document={createElement:()=>({}),head:{appendChild(){}}};
  vm.runInNewContext(read('static/js/frontend-reliability.js'),{window,document});
  const page={user:{id:1},authGeneration:1,modal:{type:'customer'},customerForm:{name:'A'},$watch:(_,cb)=>{watchers.push(cb);return()=>{};}};
  window.ERPFrontendReliability.install(page);
  const protectedNow=()=>{let prevented=false;events.beforeunload({preventDefault(){prevented=true}});return prevented};
  assert.equal(protectedNow(),false);page.customerForm.name='B';assert.equal(protectedNow(),true);
  page.customerForm.name='A';assert.equal(protectedNow(),false);
  page.customerForm.name='C';window.ERPFrontendReliability.acceptDraft('modal');assert.equal(protectedNow(),false);
  page.masterSavePending=true;assert.equal(protectedNow(),true);page.masterSavePending=false;
  page.deliverySaveState={outcomeUncertain:true};assert.equal(protectedNow(),true);
  page.deliverySaveState={};page.modal=null;assert.equal(protectedNow(),false);
  page.contractCustomer={id:1};page.contractDraft={items:[],remarks:''};watchers.forEach(fn=>fn());
  page.contractDraft.remarks='pending';page.activePage='warehouse';assert.equal(protectedNow(),true);
  page.user=null;page.authGeneration++;assert.equal(protectedNow(),false);
});
test('hidden workspaces send no periodic navigation; unchanged active snapshot is suppressed',()=>{
  const listeners={},messages=[],classes={add(){},remove(){},toggle(){}};
  const parent={postMessage:data=>messages.push(data)};
  const window={parent,addEventListener:(k,v)=>listeners[k]=v,setInterval:cb=>(listeners.tick=cb,1)};
  const document={documentElement:{classList:classes},head:{appendChild(){}},createElement:()=>({})};
  const location={origin:'http://test',search:'?frontend_shell=1&unified_navigation=1'};
  vm.runInNewContext(read('static/js/frontend-shell-bridge.js'),{window,document,location,URLSearchParams,clearInterval(){}});
  const page={user:{id:1},menus:[],activePage:'dashboard',isMenuActive:()=>false,$watch:()=>()=>{}};
  window.ERPFrontendShell.install(page);
  listeners.message({source:parent,origin:location.origin,data:{type:'tianming-formal-shell-v1',active:false}});
  messages.length=0;for(let n=0;n<20;n++)listeners.tick();assert.equal(messages.length,0);
  listeners.message({source:parent,origin:location.origin,data:{type:'tianming-formal-shell-v1',active:true}});
  const count=messages.length;for(let n=0;n<20;n++)listeners.tick();assert.equal(messages.length,count);
  page.activePage='orders';listeners.tick();assert.equal(messages.length,count+1);
});

test('legacy session 503 does not publish logged-out; retry only rechecks session',async()=>{
  const events={},messages=[],parent={postMessage:m=>messages.push(m)};
  const window={parent,erpCheckingSession:false,erpSessionUnavailable:true,addEventListener:(k,v)=>events[k]=v,setInterval:fn=>(events.tick=fn,1)};
  const document={documentElement:{classList:{add(){},remove(){},toggle(){}}},head:{appendChild(){}},createElement:()=>({})};
  const location={origin:'http://test',search:'?frontend_shell=1'};let retries=0;
  const page={user:null,$watch:()=>()=>{},checkSession:async()=>{retries++;window.erpSessionUnavailable=false;page.user={id:1}},menus:[],isMenuActive:()=>false};
  vm.runInNewContext(read('static/js/frontend-shell-bridge.js'),{window,document,location,URLSearchParams,clearInterval(){}});
  window.ERPFrontendShell.install(page);
  const connect=retrySession=>events.message({source:parent,origin:location.origin,data:{type:'tianming-formal-shell-v1',active:true,retrySession}});
  connect(false);assert.equal(messages.at(-1).sessionUnavailable,true);assert.ok(!messages.some(m=>m.authenticated===false));
  connect(true);await new Promise(r=>setImmediate(r));assert.equal(retries,1);assert.equal(messages.at(-1).ready,true);
});

test('legacy read deadline does not add write timeout or retry an operation',()=>{
  let hook;const window={axios:{interceptors:{request:{use:cb=>hook=cb}}},addEventListener(){},removeEventListener(){}};
  const document={createElement:()=>({}),head:{appendChild(){}}};
  vm.runInNewContext(read('static/js/frontend-reliability.js'),{window,document});
  window.ERPFrontendReliability.install({$watch:()=>()=>{}});
  assert.equal(hook({method:'get'}).timeout,30000);
  assert.equal(hook({method:'get',timeout:12000}).timeout,12000);
  assert.equal(hook({method:'post'}).timeout,undefined);
});

test('standalone session failure shows connection recovery instead of password entry',async()=>{
  global.window={};let status=503;
  global.axios={get:async()=>{throw {response:{status}}}};
  const page={authGeneration:1,errorMessage:()=>String(status)};
  const check=method('async checkSession() {','async login() {');
  await check.call(page);assert.equal(page.sessionUnavailable,true);assert.equal(page.sessionRestoring,false);
  status=401;await check.call(page);assert.equal(page.sessionUnavailable,false);assert.equal(page.sessionRestoring,false);
});

test('workspace startup failure exposes retry; reloading never discards a known dirty or saving frame',()=>{
  const timers=new Map();let count=0,mount;
  const frameWindow={postMessage(){},ERPFrontendReliability:{state:()=>({dirty:true,saving:false,uncertain:false})}};
  const tabs={dirtyPaths:{},markDirty(path,dirty){this.dirtyPaths[path]=dirty}};
  const route={path:'/orders',query:{}};
  const context={exports:{},defineProps:()=>({workspacePage:'orders',workspacePath:'/orders'}),
    require:name=>({
      'vue':{ref:value=>({value}),computed:fn=>({get value(){return fn()}}),watch(){},onMounted:cb=>mount=cb,onBeforeUnmount(){}},
      'vue-router':{useRoute:()=>route,useRouter:()=>({replace(){}})},
      '../stores/tabs':{useTabsStore:()=>tabs},'../stores/auth':{useAuthStore:()=>({user:{id:1}})},
      '../utils/formalOrderEntry':{parseFormalOrderEntry:()=>null},
    })[name],
    location:{origin:'http://test'},window:{addEventListener(){},removeEventListener(){}},
    setTimeout:fn=>{timers.set(++count,fn);return count},clearTimeout:key=>timers.delete(key)};
  let source=read('frontend-v2/src/views/FormalWorkspaceView.vue').split('<script setup lang="ts">')[1].split('</script>')[0];
  source+='\nexports.test={frame,connectionError,frameRevision,reloadWorkspace,retryConnection,receive};';
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,context);
  const t=context.exports.test;t.frame.value={contentWindow:frameWindow};mount();
  [...timers.values()][0]();assert.match(t.connectionError.value,/连接尚未完成/);
  t.reloadWorkspace();assert.equal(t.frameRevision.value,0);assert.match(t.connectionError.value,/未保存/);
  frameWindow.ERPFrontendReliability.state=()=>({dirty:false,saving:true,uncertain:false});t.reloadWorkspace();assert.equal(t.frameRevision.value,0);
  frameWindow.ERPFrontendReliability.state=()=>({dirty:false,saving:false,uncertain:false});t.reloadWorkspace();assert.equal(t.frameRevision.value,1);
  t.receive({origin:'http://test',source:{},data:{type:'tianming-formal-navigation-v1',ready:true}});assert.ok(timers.size>0);
  t.receive({origin:'http://test',source:frameWindow,data:{type:'tianming-formal-navigation-v1',ready:true,draftOpen:false}});assert.equal(timers.size,0);
});
