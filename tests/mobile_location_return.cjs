const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const stocktake = fs.readFileSync(path.join(root, 'static/mobile_stocktake.html'), 'utf8');
const mobile = fs.readFileSync(path.join(root, 'static/mobile_erp.html'), 'utf8');
const runtime = fs.readFileSync(path.join(root, 'static/mobile_initial_stocktake_runtime.js'), 'utf8');
const navigation = stocktake.slice(stocktake.indexOf('    function warehouseMapHref('), stocktake.indexOf('    function renderSelectedLocationIdentity('));

test('return links retain exact location; ordinary returns focus the map and batch links retain goods detail', () => {
  const redirects = [];
  const context = vm.createContext({URLSearchParams, embeddedStocktake:false, state:{selectedLocation:{id:42,area_code:'C'},submitting:false},
    inbound:{busy:false,attempt:null}, pick:(row,keys,fallback)=>keys.map(key=>row[key]).find(value=>value!=null)??fallback,
    window:{location:{search:'?location_id=42&return_floor=3F&return_area=C',assign:url=>redirects.push(url)}}});
  vm.runInContext(navigation, context);
  const url = new URL(vm.runInContext('warehouseMapHref()', context), 'http://localhost');
  assert.equal(url.searchParams.get('location_id'),'42');
  assert.equal(url.searchParams.get('focus_only'),'1');
  assert.equal(url.searchParams.get('floor_code'),'3F');
  assert.equal(url.searchParams.get('area_code'),'C');
  const lot = new URL(vm.runInContext('warehouseMapHref(99)', context), 'http://localhost');
  assert.equal(lot.searchParams.get('lot_id'),'99');
  assert.equal(lot.searchParams.has('focus_only'),false);
  context.inbound.attempt = {idempotency_key:'unknown'};
  vm.runInContext('openWarehouseMap()',context);
  assert.equal(redirects.length,0);
  context.inbound.attempt=null;context.state.selectedLocation={id:52};context.window.location.search='?location_id=52';
  const scanned=new URL(vm.runInContext('warehouseMapHref()',context),'http://localhost');
  assert.equal(scanned.searchParams.get('location_id'),'52');
  assert.equal(scanned.searchParams.has('floor_code'),false); // Resolve actual floor by stable ID, never guess 3F.
});

test('map return expands selected rack or highlights pallet region without opening goods/empty-entry loop', async () => {
  const start=mobile.indexOf('      async function loadWarehouseMapArea(');
  const source=mobile.slice(start,mobile.indexOf('      function renderWarehouseMap()',start));
  for (const rackId of ['rack-R014',null]) {
    const nodes=new Map();const node=id=>{if(!nodes.has(id))nodes.set(id,{hidden:false});return nodes.get(id)};
    let goods=0,renders=0,scrolls=0;
    const location={location_id:42,map_rack_id:rackId,goods:[]};
    const state={warehouseMapArea:'C',warehouseMapFloor:'3F',warehouseMapGeneration:0};
    const context=vm.createContext({state,warehouseActor:()=>101,AbortController,Number,encodeURIComponent,byId:node,
      showWarehouseArea(){node('warehouseAreaOverview').hidden=false;node('warehouseMapGoods').hidden=true},showStatus(){},
      apiGet:async()=>({locations:[location]}),renderWarehouseMap(){renders++},
      renderWarehouseLocationGoods(){goods++;node('warehouseAreaOverview').hidden=true},
      document:{querySelector(){return {scrollIntoView(){scrolls++}}}}});
    vm.runInContext(source,context);
    await vm.runInContext('loadWarehouseMapArea({preferredLocationId:42,focusOnly:true})',context);
    assert.equal(goods,0);assert.equal(node('warehouseAreaOverview').hidden,false);
    assert.equal(state.warehouseMapFocusLocationId,42);
    assert.equal(state.warehouseSelectedRack,rackId||undefined);
    assert.equal(renders,2);assert.equal(scrolls,1);
    await vm.runInContext('loadWarehouseMapArea({preferredLocationId:42})',context);
    assert.equal(goods,1); // Normal search/click behavior is retained.
  }
  assert.match(mobile,/focusOnly: launchParams\.get\("focus_only"\) === "1"/);
  assert.match(mobile,/focusOnly: resolvedPreferred\.focusOnly === true/);
});

function runtimeContext() {
  const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,{value:'',checked:false,replaceChildren(){},addEventListener(){},classList:{add(){},remove(){},toggle(){}}});return nodes.get(id)};
  const calls=[],returns=[],messages=[];
  const context=vm.createContext({URLSearchParams,Intl,Date,JSON,Number,$,
    state:{user:{id:1,role:'admin'},selectedLocation:{id:42,layout_version:1},lots:[],locked:false},
    document:{querySelectorAll:()=>[]},window:{location:{search:''}},
    sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},h:String,
    pick:(row,keys)=>keys.map(key=>row?.[key]).find(value=>value!==undefined),
    idempotencyKey:()=> 'stable-key',updateSubmitState(){},openLocation:async()=>{},
    returnToWarehouseContext(){returns.push(42);return true},showMessage:text=>messages.push(text),
    api:async(url,options)=>{calls.push(JSON.parse(options.body));if(calls.length===1)throw new Error('lost response');return {ok:true}}});
  vm.runInContext(runtime,context);
  $('inboundQuantity').value='7';$('inboundDate').value='2026-09-29';$('inboundCustomer').value='4';$('inboundProduct').value='8';
  vm.runInContext('inbound.context={can_add:true,snapshot:"s",existing_quantity:0}',context);
  return {context,calls,returns,messages,$};
}

test('save returns only after confirmed success; uncertain retry reuses payload; sheet goods use same return', async () => {
  const {context,calls,returns,$}=runtimeContext();
  await vm.runInContext('saveInitialInbound()',context);
  assert.equal(returns.length,0);assert.equal($('openCurrentMap').disabled,true);
  $('inboundQuantity').value='999';
  await vm.runInContext('saveInitialInbound()',context);
  assert.deepEqual(calls[0],calls[1]);assert.equal(calls[1].items[0].quantity,7);
  assert.equal(returns.length,1);assert.equal(vm.runInContext('inbound.attempt',context),null);
  await context.window.mobileGoodsSaved();assert.equal(returns.length,2);
});

test('definite save failure stays on form; navigation failure after acknowledged save is not an uncertain write', async () => {
  const {context,returns,messages}=runtimeContext();
  context.api=async()=>{throw Object.assign(new Error('conflict'),{status:409})};
  vm.runInContext('refreshInboundContext=async()=>{}',context);
  await vm.runInContext('saveInitialInbound()',context);
  assert.equal(returns.length,0);assert.equal(vm.runInContext('inbound.attempt',context),null);
  context.api=async()=>({ok:true});
  context.returnToWarehouseContext=()=>{throw new Error('navigation failed')};
  await vm.runInContext('saveInitialInbound()',context);
  assert.equal(vm.runInContext('inbound.attempt',context),null);
  assert.match(messages.at(-1),/货物已保存/);
});
