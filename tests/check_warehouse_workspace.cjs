const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function fixture() {
  let mixin; const sent = [], storage = new Map(), timers = new Map();
  const win = {location:{origin:'http://fixture',href:'http://fixture/?page=warehouse',search:'?page=warehouse'},
    history:{state:null,replaceState(state,title,url){win.location.href='http://fixture'+url;win.location.search=new URL(win.location.href).search;}},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    addEventListener(){},removeEventListener(){},setTimeout(fn){const id=timers.size+1;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);}};
  vm.runInNewContext(fs.readFileSync('static/ui/warehouse-workspace.js','utf8'),{window:win,URL,URLSearchParams,Promise});
  win.ERPWarehouseWorkspace.install({mixin:value=>mixin=value});
  const map = {postMessage:data=>sent.push({view:'map',data})},ledger={postMessage:data=>sent.push({view:'ledger',data})};
  const ctx=Object.assign({$parent:null,user:{id:1},authGeneration:2,activePage:'warehouse',uiMode:'standard',warehouseFrameUrl:'/warehouse.html?embedded=1',
    pageAllowed:()=>true,hasPermission:()=>true,isWarehouseTwinFloorCode:f=>['1F','3F','4F'].includes(f),$refs:{warehouseFrame:{contentWindow:map},warehouseLedgerFrame:{contentWindow:ledger}},$nextTick:fn=>fn()},mixin.data.call({}),mixin.methods);
  const message=(data,source=map,origin=win.location.origin)=>ctx.acceptWarehouseWorkspaceMessage({data:{source:'tianming-warehouse',...data},source,origin});
  return {ctx,message,sent,map,ledger,timers,mixin,win,storage,route:win.ERPWarehouseWorkspace.route};
}
test('only same-origin supported warehouse routes can enter shell',()=>{
  const {route}=fixture();
  for(const value of ['https://other/warehouse.html','javascript:alert(1)','/api/admin','//other/warehouse.html']) assert.equal(route(value,'http://fixture'),null);
  assert.equal(route('/warehouse-ledger.html?tab=finished&lot_id=7','http://fixture').view,'ledger');
});
test('warehouse appears for permitted users with legacy menus without restoring hidden entries',()=>{
  const {ctx}=fixture();ctx.uiLayoutEffective={layout:{menus:[{id:'dashboard'}]}};
  ctx.applyEffectiveLayout=()=>[{key:'dashboard'}];
  const eligible=[{key:'dashboard'},{key:'warehouse'},{key:'finance'}];
  assert.deepEqual(Array.from(ctx.warehouseNavigationMenus(eligible),x=>x.key),['dashboard','warehouse']);
  assert.deepEqual(Array.from(ctx.warehouseNavigationMenus(eligible.filter(x=>x.key!=='warehouse')),x=>x.key),['dashboard']);
  ctx.uiLayoutEffective.layout.menus.push({id:'warehouse',visible:false});
  assert.deepEqual(Array.from(ctx.warehouseNavigationMenus(eligible),x=>x.key),['dashboard']);
});
test('map/list retain the original iframe documents and independent state',()=>{
  const {ctx}=fixture();ctx.activateWarehouseRoute('/warehouse.html?floor=3F'); const mapUrl=ctx.warehouseFrameUrl;
  ctx.activateWarehouseRoute('/warehouse-ledger.html?tab=finished&q=APS4'); const ledgerUrl=ctx.warehouseLedgerUrl;
  ctx.activateWarehouseRoute('/warehouse.html?floor=4F&location_id=20&lot_id=7');
  assert.equal(ctx.warehouseFrameUrl,mapUrl);assert.equal(ctx.warehouseLedgerUrl,ledgerUrl);
  assert.match(ctx._warehouseActivation.map,/location_id=20/);assert.equal(ctx.warehouseView,'map');
});
test('navigation waits for matching trusted view guard, ignores stale and spoofed replies',async()=>{
  const {ctx,message,sent,map}=fixture(); const target='/warehouse-ledger.html?tab=finished';
  const pending=ctx.checkWarehouseNavigation(target), req=sent.at(-1).data;
  message({type:'warehouse-workspace-navigate',request_id:req.request_id,url:target},{},'http://fixture');
  message({type:'warehouse-workspace-navigate',request_id:req.request_id,url:target},map,'http://evil');
  message({type:'warehouse-workspace-navigate',request_id:req.request_id,url:'/warehouse.html'});
  assert.equal(ctx.warehouseNavigating,true);
  message({type:'warehouse-workspace-navigate',request_id:req.request_id,url:target});
  assert.equal(await pending,true);assert.equal(ctx.warehouseView,'map');
});
test('uncertain writes block navigation and timeout never permits leaving',async()=>{
  const {ctx,message,sent,timers}=fixture(); let pending=ctx.checkWarehouseNavigation();
  message({type:'warehouse-workspace-blocked',request_id:sent.at(-1).data.request_id,message:'结果未确认'});
  assert.equal(await pending,false);assert.equal(ctx.warehouseNavigationError,'结果未确认');
  pending=ctx.checkWarehouseNavigation();[...timers.values()][0]();assert.equal(await pending,false);
});
test('auth change cancels pending navigation and removes previous user views',async()=>{
  const {ctx,mixin}=fixture();const pending=ctx.checkWarehouseNavigation();ctx.authGeneration++;
  mixin.watch.authGeneration.call(ctx);assert.equal(await pending,false);assert.equal(ctx.warehouseLedgerUrl,'');assert.equal(ctx.warehouseFrameUrl,'');
});
test('only active inventory query can update shared search and ready triggers activation once',()=>{
  const {ctx,message,sent,ledger}=fixture();ctx.activateWarehouseRoute('/warehouse.html?q=APS4');
  message({type:'warehouse-workspace-context',ready:true,q:'APS4'});const count=sent.length;
  message({type:'warehouse-workspace-context',ready:true,q:'APS4'});assert.equal(sent.length,count);
  message({type:'warehouse-workspace-context',ready:true,q:'wrong',tab:'finished'},ledger);assert.equal(ctx.warehouseContext.q,'APS4');
  ctx.activateWarehouseRoute('/warehouse-ledger.html?tab=molds');message({type:'warehouse-workspace-context',ready:true,q:'mold-only',tab:'molds'},ledger);
  assert.equal(ctx.warehouseContext.q,'APS4');
});
test('inactive iframe cannot navigate and refresh retains frame identity',async()=>{
  const {ctx,message,ledger,sent}=fixture();message({type:'warehouse-workspace-navigate',url:'/warehouse-ledger.html'},ledger);assert.equal(ctx.warehouseView,'map');
  const original=ctx.warehouseFrameUrl; const pending=ctx.refreshWarehouseWorkspace();const req=sent.at(-1).data;
  message({type:'warehouse-workspace-navigate',request_id:req.request_id,url:req.url});await pending;
  assert.equal(sent.at(-1).data.command,'refresh');assert.equal(ctx.warehouseFrameUrl,original);
});
test('view floor and search scope remain separate and unsupported ledger scope cannot erase map choice',()=>{
  const {ctx,message,ledger}=fixture();message({type:'warehouse-workspace-context',ready:true,q:'APS4',search_floor:'UNLOCATED'});
  ctx.activateWarehouseRoute('/warehouse-ledger.html?tab=finished&search_floor=UNLOCATED');
  message({type:'warehouse-workspace-context',ready:true,tab:'finished',q:'APS4',search_floor:'ALL'},ledger);
  assert.equal(ctx.warehouseContext.search_floor,'UNLOCATED');
  message({type:'warehouse-workspace-context',tab:'finished',q:'APS4',search_floor:'3F',scope_changed:true},ledger);
  assert.equal(ctx.warehouseContext.search_floor,'3F');
});
test('legacy stocktake map intent overrides a saved list preference',()=>{
  const {ctx,win,storage}=fixture();ctx.warehouseFrameUrl='';storage.set('tm-warehouse-view:1','list');
  win.location.search='?page=warehouse&warehouse_mode=move&warehouse_action=stocktake&warehouse_view=2d';
  ctx.initializeWarehouseWorkspace();assert.equal(ctx.warehouseView,'map');
  assert.match(ctx.warehouseFrameUrl,/mode=move/);assert.match(ctx.warehouseFrameUrl,/action=stocktake/);
});
test('warehouse view permission alone cannot expose stocktake records',()=>{
  const {ctx}=fixture();ctx.hasPermission=()=>false;
  assert.equal(ctx.warehouseStocktakeVisible(),false);
  assert.equal(ctx.activateWarehouseRoute('/warehouse-ledger.html?tab=stocktake_review'),false);
  assert.match(ctx.warehouseNavigationError,/没有盘点记录/);
});
test('leaving warehouse updates refresh route while retaining child documents',()=>{
  const {ctx,win}=fixture();ctx.activateWarehouseRoute('/warehouse-ledger.html?tab=finished');const frame=ctx.warehouseLedgerUrl;
  ctx.leaveWarehouseWorkspaceUrl('orders');const url=new URL(win.location.href);
  assert.equal(url.searchParams.get('page'),'orders');assert.equal(url.searchParams.has('warehouse_target'),false);assert.equal(ctx.warehouseLedgerUrl,frame);
});


test('changing text size does not replay stale query or location activation',()=>{
  const {ctx,mixin,message,sent}=fixture();ctx.activateWarehouseRoute('/warehouse.html?q=old&location_id=92');
  message({type:'warehouse-workspace-context',ready:true,q:'new',search_floor:'3F'});
  ctx.uiMode='large';mixin.watch.uiMode.call(ctx);
  const command=sent.at(-1).data;assert.equal(command.ui_mode,'large');
  const url=new URL(command.url,'http://fixture');assert.equal(url.searchParams.has('q'),false);assert.equal(url.searchParams.has('location_id'),false);
  assert.equal(ctx.warehouseContext.q,'new');
});
