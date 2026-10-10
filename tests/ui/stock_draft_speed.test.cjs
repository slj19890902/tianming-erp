const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const nodeVm=require('node:vm');
const names=['go','openLowStockReplenishment','openStockReplenishment','addStockPolicyDraft','loadStockProducts','stockPolicyDraftQuantity','syncStockReplenishmentSupplier','stockReplenishmentMaterialSuppliers','validateStockReplenishmentForm'];
function methodSource(html=fs.readFileSync(path.join(__dirname,'../../static/index.html'),'utf8')) {
  return names.map(name=>{
    const start=html.search(new RegExp('^          (?:async )?'+name+'\\(', 'm'));
    assert(start>=0,name);
    const rest=html.slice(start),end=rest.slice(1).search(/^          (?:async )?\w+\(/m)+1;
    return rest.slice(0,end);
  }).join('\n');
}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(get=async()=>({data:{items:[]}})) {
  const controllers=new Map();let serial=0;
  const window={};
  nodeVm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/ui/product-workbench.js'),'utf8'),{window});
  const methods=new Function('axios','createIdempotencyKey','latestRequestControllers','window','return ({'+methodSource()+'});')({get},()=>`test-${++serial}`,controllers,window);
  const vm={...methods,user:{id:1},authGeneration:1,activePage:'dashboard',canRequisition:true,canSubmitBusinessRequest:false,
    stockPolicyDraftQuantities:{},customerOptions:[],allMaterials:[],stockPolicyWarnings:[],stockReplenishmentForm:{items:[]},stockReplenishmentProducts:[],modal:null,
    pages:[],toasts:[],loadPage:async p=>{vm.pages.push(p);},loadCustomerOptions:async()=>{},loadMaterials:async()=>{},pageAllowed:()=>true,
    resetSingleScreenExpansion(){},cancelLatestRequest(){},syncDesktopWorkspaceUrl(){},cancelSupplierRequisitionPreview(){},
    beginLatestRequest(key){controllers.get(key)?.abort();const c=new AbortController();controllers.set(key,c);return c;},
    finishLatestRequest(key,c){if(controllers.get(key)===c)controllers.delete(key);},isCancelledRequest:e=>e?.name==='AbortError',errorMessage:e=>e.message,
    showToast(...a){vm.toasts.push(a);}};
  return vm;
}
const policy={id:17,policy_id:17,suggested_new_requisition_sheet_quantity:20};
const draft={draft_ready:true,supplier_name:'虚构供应商',customer_id:1,items:[{stock_policy_id:17,product_id:3,customer_id:1,product_code:'FICTION-3',product_name:'虚构测试纸箱',quantity:20,material_supplier_name:'虚构供应商',draft_ready:true}]};

if(require.main===module) {
test('target details appear before slow options/workbench, without hidden all-policy computation',async()=>{
  const target=deferred(),options=deferred(),calls=[];
  const v=setup(async(url)=>{calls.push(url);return url.endsWith('/replenishment-draft')?target.promise:{data:{items:[]}};});
  v.loadMaterials=()=>options.promise;
  const opened=v.openLowStockReplenishment(policy);await tick();
  assert.deepEqual(calls,['/api/requisition/stock-policies/17/replenishment-draft']);assert.deepEqual(v.pages,[]);
  assert.match(v.validateStockReplenishmentForm(),/正在读取/);
  target.resolve({data:draft});await tick();
  assert.equal(v.stockReplenishmentForm.items[0].product_code,'FICTION-3');assert.equal(v.stockReplenishmentForm.items[0].reference_product_id,3);
  assert.equal(v.stockReplenishmentForm.items[0].quantity,20);assert.equal(v.stockReplenishmentForm._pendingDrafts,0);assert.deepEqual(v.pages,[]);
  options.resolve();await opened;assert.deepEqual(v.pages,['requisition']);assert(!calls.includes('/api/requisition/stock-policies'));
});
test('manual entry still fetches warnings and selector collections',async()=>{
  const calls=[];const v=setup(async u=>{calls.push(u);return {data:{items:[{id:9}]}};});let customers=0,materials=0;
  v.loadCustomerOptions=async()=>customers++;v.loadMaterials=async()=>materials++;
  await v.openStockReplenishment();assert.deepEqual(calls,['/api/requisition/stock-policies']);assert.equal(v.stockPolicyWarnings[0].id,9);assert.equal(customers,1);assert.equal(materials,1);
});
test('ordinary navigation still loads and permission rejection cannot open draft',async()=>{
  const v=setup();await v.go('requisition');assert.deepEqual(v.pages,['requisition']);v.pageAllowed=()=>false;
  await v.openLowStockReplenishment(policy);assert.equal(v.modal,null);assert.equal(v.toasts.length,1);
});
test('warehouse navigation cancellation does not open draft',async()=>{
  const v=setup();v.activePage='warehouse';v.checkWarehouseNavigation=async()=>false;await v.openLowStockReplenishment(policy);assert.equal(v.modal,null);
});
for(const change of ['close','replace','account']) test(`late target response ignored after ${change}`,async()=>{
  const request=deferred();const v=setup(()=>request.promise);const opened=v.openStockReplenishment({warningOnly:true,policy});await tick();const previous=v.stockReplenishmentForm;
  if(change==='close')v.modal=null;if(change==='replace')v.stockReplenishmentForm={items:[]};if(change==='account')v.authGeneration++;
  request.resolve({data:draft});await opened;assert.equal(previous.items.length,0);assert.equal(v.stockReplenishmentForm.items.length,0);assert.deepEqual(v.toasts,[]);
});
for(const change of ['close','replace','account']) test(`late product options ignored after ${change}`,async()=>{
  const request=deferred();const v=setup(()=>request.promise);v.modal={type:'stockReplenishment'};const loaded=v.loadStockProducts(1);
  if(change==='close')v.modal=null;if(change==='replace')v.stockReplenishmentForm={items:[]};if(change==='account')v.authGeneration++;
  request.resolve({data:{items:[{id:3,customer_id:1}]}});await loaded;assert.deepEqual(v.stockReplenishmentProducts,[]);
});
test('BOM manual quantity and frozen plan preserved; default sends no override',async()=>{
  const requests=[];const v=setup(async(u,o)=>{requests.push(o.params);return {data:{...draft,is_virtual_composite_parent:true,replenishment_plan:{policy_id:17,manual_quantity:true}}};});
  const bom={...policy,is_virtual_composite_parent:true};await v.openStockReplenishment({warningOnly:true,policy:bom});
  assert.equal(requests[0].finished_quantity,undefined);v.stockPolicyDraftQuantities['17']=6;requests.length=0;
  await v.openStockReplenishment({warningOnly:true,policy:bom});assert.equal(requests[0].finished_quantity,6);assert.equal(v.stockReplenishmentForm.replenishment_plans[0].policy_id,17);
});
test('failed target clears pending indicator and shows failure without inventing lines',async()=>{
  const v=setup(async()=>{throw new Error('isolated unavailable');});await v.openStockReplenishment({warningOnly:true,policy});
  assert.equal(v.stockReplenishmentForm._pendingDrafts,0);assert.equal(v.stockReplenishmentForm.items.length,0);assert.match(v.toasts[0][0],/isolated unavailable/);
});
test('old opening cannot refresh another account workbench',async()=>{
  const req=deferred();const v=setup(()=>req.promise);const opened=v.openLowStockReplenishment(policy);await tick();v.authGeneration++;
  req.resolve({data:draft});await opened;assert.deepEqual(v.pages,[]);
});
}
module.exports={methodSource,setup,draft,policy};
