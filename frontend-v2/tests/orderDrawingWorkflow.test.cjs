const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict'),{webcrypto}=require('node:crypto');
const ts=require(process.env.ERP_UI_TYPESCRIPT_LIBRARY),root=path.resolve(__dirname,'../src');
const compile=source=>ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
function harness({failRead=false,cancel=false,allPermissions=false}={}){
 const calls=[],stubInventory={states:{value:{}},checked:{value:false},busy:{value:false},changed:{value:false},error:{value:''},refresh:async()=>true},tabs={markDirty(){}};
 const helpers={};vm.runInNewContext(compile(fs.readFileSync(path.join(root,'utils/orderDrawings.ts'),'utf8')),{exports:helpers,crypto:webcrypto,Uint8Array,Error});
 let reads=0;const api={createOrder:async payload=>{calls.push('create');return {id:41,order_number:'ISOLATED-UAT'}},getOrder:async()=>{calls.push('read');if(failRead&&reads++===0)throw Error('read interrupted');return {id:41,order_number:'ISOLATED-UAT',items:[{id:1,client_line_id:'line-1'}]}},createAttempt:async()=>{calls.push('attempt');return {status:'not_found'}},drawingContent:async()=>{calls.push('drawing');return new Blob(['unit-only'])}};
 const messages=[],exported={},source=fs.readFileSync(path.join(root,'views/OrderView.vue'),'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1];
 const context={exports:exported,crypto:webcrypto,Error,Date,Promise,Blob,console,require(id){
  if(id==='vue')return {ref:value=>({value}),computed:fn=>({get value(){return fn()}}),nextTick:async()=>{},watch(){},onMounted(){}};
  if(id==='element-plus')return {ElMessage:{warning:m=>messages.push(m),success:m=>messages.push(m),error:m=>messages.push(m),info:m=>messages.push(m)},ElMessageBox:{confirm:async()=>{calls.push('confirm');if(cancel)throw Error('cancel')}}};
  if(id==='../api/client')return {orderApi:api,warehouseApi:{},masterApi:{},pricingApi:{},ApiError:class ApiError extends Error{}};
  if(id==='../utils/businessDate')return {businessDate:()=> '2026-10-06'};
  if(id==='../utils/orderCreateReadback')return {buildReadbackExpectations:()=>({}),verifyOrderCreateReadback:()=>calls.push('verify')};
  if(id==='../stores/tabs')return {useTabsStore:()=>tabs};
  if(id==='vue-router')return {useRouter:()=>({push:target=>calls.push(['navigate',target.path])})};
  if(id==='../stores/auth')return {useAuthStore:()=>({hasPermission:p=>allPermissions||p==='orders.create'})};
  if(id==='../composables/useOrderInventory')return {useOrderInventory:()=>stubInventory};
  if(id==='../utils/orderInventory')return {emptyPlan:()=>({finished:[],semi:[]})};
  if(id==='../utils/orderRequisitionHold')return {};
  if(id==='../utils/orderDrawings')return helpers;
  if(id.endsWith('.vue'))return {};
  throw Error('Unexpected dependency '+id)
 }};
 vm.runInNewContext(compile(source)+'\nexports.harness={submitOrder,orderForm,orderLines,chosenProducts,committedAttempt,entryLocked,addOrderLine};',context);
 const h=exported.harness;h.orderForm.value.customer_id=1;h.chosenProducts.value[1]={id:1,customer_id:1,version:1,production_notes:null,supply_mode:'self_produced'};h.addOrderLine();Object.assign(h.orderLines.value[0],{client_line_id:'line-1',product_id:1,quantity:'2',unit_price:'3.60'});
 return {h,calls,messages};
}
test('server-created order with failed readback locks draft; retry reads without another create',async()=>{const {h,calls}=harness({failRead:true});await h.submitOrder();assert.equal(calls.filter(x=>x==='create').length,1);assert.notEqual(h.committedAttempt.value,null);assert.equal(h.entryLocked.value,true);await h.submitOrder();assert.equal(calls.filter(x=>x==='create').length,1);assert.equal(calls.filter(x=>x==='read').length,2);assert.equal(h.committedAttempt.value,null);assert.equal(h.entryLocked.value,false);assert.equal(calls.some(x=>Array.isArray(x)&&x[0]==='navigate'),true)});
test('overwriting product drawing cancel performs no create or inventory mutation',async()=>{const {h,calls}=harness({cancel:true,allPermissions:true});Object.assign(h.orderLines.value[0].drawing,{token:'a'.repeat(32),filename:'drawing.png',digest:'test-only',saveOption:'overwrite_product'});await h.submitOrder();assert.equal(calls.filter(x=>x==='confirm').length,1);assert.equal(calls.filter(x=>x==='create').length,0);assert.equal(h.orderLines.value[0].product_id,1)});
test('normal order never asks overwrite confirmation',async()=>{const {h,calls}=harness();await h.submitOrder();assert.equal(calls.filter(x=>x==='confirm').length,0);assert.equal(calls.filter(x=>x==='create').length,1)});
test('client upload preserves multipart boundary and protected blob transport',async()=>{
 let fetches=[];const exported={},source=fs.readFileSync(path.join(root,'api/client.ts'),'utf8').replace("const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\\/$/, '')","const API_BASE = ''");
 const helpers={};vm.runInNewContext(compile(fs.readFileSync(path.join(root,'utils/orderDrawings.ts'),'utf8')),{exports:helpers,crypto:webcrypto,Uint8Array,Error});
 vm.runInNewContext(compile(source),{exports:exported,Error,DOMException,AbortController,FormData,URLSearchParams,window:{setTimeout,clearTimeout},fetch:async(url,init)=>{fetches.push({url,init});return {ok:true,status:200,json:async()=>({token:'a'.repeat(32)}),blob:async()=>new Blob(['protected'],{type:'image/png'})}},require:id=>id==='../utils/orderDrawings'?helpers:{}});
 await exported.orderApi.uploadDraftDrawing(Object.assign(new Blob(['file']),{name:'drawing.png'}));assert.equal(fetches[0].init.body instanceof FormData,true);assert.equal(fetches[0].init.headers['Content-Type'],undefined);assert.equal(fetches[0].init.credentials,'include');
 const blob=await exported.orderApi.drawingContent('/api/orders/items/2/drawing/content/file.png');assert.equal(await blob.text(),'protected');await assert.rejects(async()=>exported.orderApi.drawingContent('https://outside.invalid/file.png'),/受保护/);assert.equal(fetches.length,2)
});
