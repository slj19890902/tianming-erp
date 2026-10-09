import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const source = fs.readFileSync(new URL('../src/WarehouseTwinApp.tsx', import.meta.url), 'utf8');
const compile = code => ts.transpileModule(code, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
const rack = {id: 'rack-A', levels: 3, level_cell_counts: [2, 3, 4]};
const location = {location_id: 91, floor_code: '3F', map_rack_id: 'rack-A', address_kind: 'rack_slot',
  storage_type: 'rack', is_active: true, position_status: 'mapped', level_no: 2, slot_no: 3, items: [{lot_id: 15}]};
function resolver() {
  assert.ok(source.includes('function searchRackForLocation('), 'must resolve a formal rack binding, not guess by map coordinates');
  const context = {};
  vm.createContext(context);
  vm.runInContext(compile(source.slice(source.indexOf('function rackLevelCellCounts('), source.indexOf('function rackCellIdentityKey('))), context);
  return context.searchRackForLocation;
}

test('rack resolution requires an exact active published location, unique cell and valid level/slot', () => {
  const resolve = resolver();
  assert.equal(resolve(location, [location], [rack]), rack);
  for (const change of [{map_rack_id:null}, {map_rack_id:'missing'}, {is_active:false}, {storage_type:'ground'},
    {address_kind:'legacy'}, {position_status:'unmapped'}, {level_no:4}, {level_no:0}, {level_no:1.5}, {slot_no:0}, {slot_no:4}]) {
    const invalid = {...location, ...change};
    assert.equal(resolve(invalid, [invalid], [rack]), null);
  }
  assert.equal(resolve(location, [location, {...location, location_id:92}], [rack]), null);
  assert.equal(resolve(location, [location], [rack, {...rack}]), null);
  assert.equal(resolve(location, [location], [{...rack, mold_rack_code:'R01'}]), null);
});

test('every new search selection closes the previous rack and replaces pending location without writing', () => {
  const code = compile(source.slice(source.indexOf('  const focusSearchItem ='), source.indexOf('  const focusSearchProduct =')));
  const state = {};
  const setters = ['FocusedSearchItem','FocusedSearchProductKey','FocusedResource','AreaInventorySearch','CameraFocusTarget',
    'PendingAreaCode','PendingLocationId','Selected','FloorCode','RackFocusId','PendingRackSearchLocationId','PendingLotId',
    'PendingLocateResource','SearchPanelOpen','SearchError'];
  const context = {cameraFocusSequenceRef:{current:0}, searchProductKey:item=>`product-${item.product_id}`,
    isWarehouseOperationalFloorCode:floor=>['1F','3F','4F'].includes(floor),
    ...Object.fromEntries(setters.map(name=>['set'+name,value=>{state[name]=value;}]))};
  vm.createContext(context);
  vm.runInContext(code, context);
  for (const item of [
    {product_id:1, location_id:91, floor_code:'3F',position_status:'mapped'},
    {product_id:2, location_id:92, floor_code:'4F',position_status:'mapped'},
    {product_id:3, location_id:93, floor_code:'1F',position_status:'mapped'},
    {product_id:4, location_id:null, floor_code:'3F',area_code:'A',position_status:'unmapped'},
    {product_id:5, location_id:null, floor_code:'3F',position_status:'unmapped'},
  ]) {
    state.RackFocusId='old-rack';
    context.item=item;
    vm.runInContext('focusSearchItem(item)', context);
    assert.equal(state.RackFocusId,null);
    assert.equal(state.PendingRackSearchLocationId,item.position_status==='mapped'?item.location_id:null);
    assert.equal(state.PendingLocationId,item.position_status==='mapped'?item.location_id:null);
    assert.equal(state.FocusedSearchProductKey,`product-${item.product_id}`);
    assert.equal(state.SearchPanelOpen,true,'keep all location choices available after any result or pallet-detail click');
    assert.equal(state.PendingLocateResource,null);
  }
  assert.equal(state.PendingAreaCode,null,'a new unlocated result must clear a stale pending area');
});

test('one product can switch between every stock location without losing the search choices', () => {
  const state = {};
  const names = ['FocusedSearchItem','FocusedSearchProductKey','FocusedResource','AreaInventorySearch','CameraFocusTarget',
    'PendingAreaCode','PendingLocationId','Selected','FloorCode','RackFocusId','PendingRackSearchLocationId','PendingLotId',
    'PendingLocateResource','SearchPanelOpen','SearchError'];
  const items = [
    {lot_id:1163,product_id:3560,location_id:1978,floor_code:'3F',position_status:'mapped'},
    {lot_id:1211,product_id:3560,location_id:2164,floor_code:'3F',position_status:'mapped'},
    {lot_id:1212,product_id:3560,location_id:99,floor_code:'1F',position_status:'mapped'},
  ];
  const context = {items,cameraFocusSequenceRef:{current:0},searchProductKey:()=> 'same-product',
    isWarehouseOperationalFloorCode:floor=>['1F','3F','4F'].includes(floor),
    ...Object.fromEntries(names.map(name=>['set'+name,value=>{state[name]=value;}]))};
  vm.createContext(context);
  vm.runInContext(compile(source.slice(source.indexOf('  const focusSearchItem ='),source.indexOf('  const focusLocateResource ='))),context);
  for (let i=0;i<items.length;i++) {
    context.choice=i;
    vm.runInContext('focusSearchLocation({key:"same-product",items}, {location_id:items[choice].location_id})',context);
    assert.equal(state.PendingLocationId,items[i].location_id);
    assert.equal(state.FocusedSearchItem.lot_id,items[i].lot_id);
    assert.equal(state.FloorCode,items[i].floor_code);
    assert.equal(state.SearchPanelOpen,true);
    assert.equal(state.FocusedSearchProductKey,'same-product');
  }
});

test('pending rack search waits for the matching floor then opens once, without changing deep-link behavior', () => {
  const resolve = resolver();
  const body = source.indexOf('    if (pendingLocationId === null) return;');
  const begin = source.lastIndexOf('  useEffect(() => {',body);
  const end = source.indexOf('  useEffect(() => {',body);
  const code = compile(source.slice(begin,end));
  const state = {};
  const setters = ['Selected','CameraFocusTarget','PendingLocationId','PendingLotId','RackFocusId','PendingRackSearchLocationId',
    'TraceDeepLinkMessage','TraceFocusedLotId','LocationItemsExpanded','SearchError'];
  const context = {productionMapContext:false,pendingLocationId:91,pendingLotId:null,pendingRackSearchLocationId:91,floorCode:'3F',
    dashboard:{},layout:{floor_code:'4F',racks:[rack]},loading:false,visualLocations:[location],
    selected:{kind:'pallet',id:'erp-location-91'},selectedLocationItems:[],traceReadOnly:false,
    focusedSearchItem:{lot_id:15},cameraFocusSequenceRef:{current:0},
    useEffect:fn=>fn(),searchRackForLocation:resolve,rackLocationInventoryItems:row=>row.items,
    ...Object.fromEntries(setters.map(name=>['set'+name,value=>{state[name]=value;}]))};
  vm.runInNewContext(code, context);
  assert.deepEqual(state,{},'do not resolve against an old floor layout');
  context.layout.floor_code='3F';
  vm.runInNewContext(code,context);
  assert.equal(state.RackFocusId,'rack-A');
  assert.equal(state.PendingRackSearchLocationId,null);
  assert.equal(state.PendingLocationId,null);
  state.RackFocusId=null; // User closes it; a later dashboard update must not reopen it.
  context.pendingRackSearchLocationId=null;
  vm.runInNewContext(code,context);
  assert.equal(state.RackFocusId,null);
  context.pendingRackSearchLocationId=91;
  context.focusedSearchItem={lot_id:999};
  vm.runInNewContext(code,context);
  assert.equal(state.RackFocusId,null,'stale/moved stock must not claim to be on this rack');
  assert.ok(state.SearchError);
});

test('rack focus retains the search panel and highlights current location with text as well as color', () => {
  const css = fs.readFileSync(new URL('../src/warehouseTwin.css',import.meta.url),'utf8');
  assert.ok(css.includes('.twin-workspace.rack-focused.context-open > .twin-context-rail'));
  assert.ok(css.includes('.rack-search-hit'));
  assert.ok(css.includes('.rack-search-current'));
  assert.ok(source.includes('highlightedLotIds={rackSearchLotIds}'));
  assert.ok(source.includes('searchLocationId={focusedSearchItem?.location_id}'));
});

test('order location focuses the exact physical batch and rack, with a persistent yellow location highlight',()=>{
  const body=source.indexOf('    if (pendingLocationId === null) return;');
  const begin=source.lastIndexOf('  useEffect(() => {',body),end=source.indexOf('  useEffect(() => {',body);
  const state={},lot={lot_id:15,product_id:7,quantity:20,unit:'个',inventory_code:'P7'};
  const names=['Selected','CameraFocusTarget','PendingLocationId','PendingLotId','RackFocusId','PendingRackSearchLocationId','TraceDeepLinkMessage','TraceFocusedLotId','LocationItemsExpanded','SearchError','SidebarLabelLotId','FocusedSearchItem','FocusedSearchProductKey'];
  const context={productionMapContext:false,pendingLocationId:91,pendingLotId:15,pendingRackSearchLocationId:null,floorCode:'3F',dashboard:{},layout:{floor_code:'3F',racks:[rack]},loading:false,visualLocations:[location],selected:{kind:'pallet',id:'erp-location-91'},selectedLocationItems:[lot],traceReadOnly:true,focusedSearchItem:null,cameraFocusSequenceRef:{current:0},
    useEffect:fn=>fn(),searchRackForLocation:resolver(),rackLocationInventoryItems:r=>r.items,inventoryHasPhysicalQuantity:r=>r.quantity>0,employeeLocationName:()=> '三楼 A1 2层3格',inventoryLabelQuantity:r=>r.quantity,inventoryUnitLabel:s=>s,formatNumber:n=>String(n),searchProductKey:r=>`p-${r.product_id}`,
    ...Object.fromEntries(names.map(name=>['set'+name,value=>{state[name]=value;}]))};
  const code=compile(source.slice(begin,end));vm.runInNewContext(code,context);
  assert.equal(state.RackFocusId,'rack-A');assert.equal(state.SidebarLabelLotId,undefined);assert.equal(state.TraceFocusedLotId,15);assert.equal(state.FocusedSearchItem.location_id,91);assert.equal(state.FocusedSearchProductKey,'p-7');assert.match(state.TraceDeepLinkMessage,/黄色标记/);assert.equal(state.PendingLotId,null);
  for(const invalid of [[],[{...lot,quantity:0}]]){Object.keys(state).forEach(k=>delete state[k]);context.selectedLocationItems=invalid;vm.runInNewContext(code,context);assert.equal(state.SidebarLabelLotId,undefined);assert.equal(state.RackFocusId,undefined);assert.match(state.TraceDeepLinkMessage,/已移位、清零/);}
});
