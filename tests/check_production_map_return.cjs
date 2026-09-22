const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let mixin, response, writes = 0, resolveRead;
const win = {location:{origin:'http://fixture'}, addEventListener(){}, removeEventListener(){}};
vm.runInNewContext(fs.readFileSync('static/ui/production-map.js','utf8'), {
  window:win, URLSearchParams, document:{activeElement:null,querySelectorAll:()=>[]},
  axios:{get:async()=>response instanceof Promise ? response : {data:response}},
});
win.ERPProductionMap.install({mixin:value=>mixin=value});
function context() {
  return Object.assign({productionMap:null, productionBusy:false, authGeneration:1, productionTab:"history", pages:{productionHistory:3,productionPlacement:1},
    pageAllowed:()=>true, canWarehouseExecute:true, canProductionExecute:true, stockOperationKey:()=> 'nonce',
    errorMessage:e=>e.message, $refs:{productionMapFrame:{contentWindow:{postMessage(){}}}},
    $nextTick:async()=>{}, loadProductionHistory:async()=>{},loadProductionPlacement:async()=>{},loadKpi:async()=>{},
    ensureProductionLocations:async()=>true, productionLocation:()=>({id:93,layout_version:2}),
    transferProductionCompletionToStock:async()=>{writes++;return true;},
  }, mixin.methods);
}
const row={inventory_lot_id:18,current_inventory_lot_id:19,warehouse_location_id:92,can_place:true};
const live={id:19,quantity_available:29,quantity_reserved:0,quantity_damaged:0,location:{id:95,warehouse_floor:4}};
(async()=>{
  const c=context();response=live;await c.showProductionMap(row);
  const url=new URL(c.productionMap.url,win.location.origin);
  assert.equal(url.searchParams.get('lot_id'),'19');assert.equal(url.searchParams.get('location_id'),'95');
  assert.equal(url.searchParams.get('floor'),'4F');assert.equal(url.searchParams.get('tab'),'map');
  assert.equal(url.searchParams.get('readonly'),null);
  c.productionTab='history';c.pages={productionHistory:3};c.productionHistoryFilters={product_code:'P007'};
  await c.finishProductionMapReturn();assert.equal(c.productionTab,'history');assert.equal(c.pages.productionHistory,3);
  assert.equal(c.productionHistoryFilters.product_code,'P007');
  response=new Promise(r=>resolveRead=r);const pending=c.showProductionMap(row);await c.finishProductionMapReturn();
  resolveRead({data:live});await pending;assert.equal(c.productionMap,null,'cancelled reads cannot reopen the map');
  response=live;await c.showProductionMap(row,true);const session=c.productionMap;
  const event={origin:win.location.origin,source:c.$refs.productionMapFrame.contentWindow,data:{type:'erp-production-location',token:session.token,location_id:93,layout_version:2}};
  for(const forged of [{...event,origin:'https://evil.invalid'},{...event,source:{}},{...event,data:{...event.data,token:'wrong'}}]) await c.acceptProductionMapMessage(forged);
  assert.equal(session.target,null);
  await c.acceptProductionMapMessage({...event,data:{...event.data,layout_version:1}});assert.equal(session.target,null);assert.match(session.error,/布局已变化/);
  await c.acceptProductionMapMessage(event);assert.equal(session.target.id,93);assert.equal(writes,0,'selection never submits');
  c.canProductionExecute=false;await c.confirmProductionMapPlacement();assert.equal(writes,0);
  c.canProductionExecute=true;await c.confirmProductionMapPlacement();assert.equal(writes,1);assert.equal(c.productionMap,null);
  await c.showProductionMap(row,true);c.authGeneration++;await c.acceptProductionMapMessage(event);assert.equal(c.productionMap.target,null);
  mixin.watch.authGeneration.call(c);assert.equal(c.productionMap,null);
  c.canWarehouseExecute=false;await c.showProductionMap(row,true);assert.equal(c.productionMap,null);
  c.canWarehouseExecute=true;response={...live,quantity_available:0};await c.showProductionMap(row);assert.equal(c.productionMap.url,'');assert.match(c.productionMap.error,/实物库存/);
  console.log('PASS actual production map methods: current lot/floor, preserved context, cancel races, message origin/source/token/auth, stale layout, permissions, selection and single commit');
})().catch(e=>{console.error(e);process.exitCode=1;});
