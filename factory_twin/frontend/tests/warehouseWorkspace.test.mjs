import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import vm from 'node:vm';
import {buildMoveBatchPayload,upsertMoveDraft} from '../src/warehouseMoveDraft.mjs';
import {moveLocationState, areaSortKey} from '../src/warehouseWorkspace.mjs';
const source=fs.readFileSync(new URL('../src/WarehouseTwinApp.tsx',import.meta.url),'utf8');
test('single product can select occupied rack cells; whole pallets cannot',()=>{
  const cell={location_id:9,occupancy_status:'occupied',map_rack_id:'R',address_kind:'rack_slot'};
  assert.equal(moveLocationState(cell,{operation:'lot_transfer',source_location_id:1},[9],9).kind,'target');
  assert.equal(moveLocationState(cell,{operation:'lot_transfer',source_location_id:1},[9],null).kind,'occupied');
  assert.equal(moveLocationState(cell,{operation:'pallet_move',source_location_id:1},[9],9).kind,'blocked');
  assert.equal(moveLocationState({...cell,occupancy_status:'empty'},{operation:'pallet_move',source_location_id:1},[9],9).kind,'blocked');
});
test('source and ineligible targets cannot be submitted; selected eligible empty target remains distinct',()=>{
  const src={operation:'lot_transfer',source_location_id:1};
  assert.equal(moveLocationState({location_id:1},src,[1],1).kind,'source');
  assert.equal(moveLocationState({location_id:9},src,[],9).selectable,false);
  assert.equal(moveLocationState({location_id:9,occupancy_status:'empty'},src,[9],null).kind,'empty');
  assert.equal(moveLocationState({location_id:9,occupancy_status:'empty'},src,[9],9).kind,'target');
});
test('areas order by letter then natural number',()=>{
  assert.ok(areaSortKey('南A2').localeCompare(areaSortKey('北A10'),'zh-CN',{numeric:true})<0);
  assert.ok(areaSortKey('南A10').localeCompare(areaSortKey('北B1'),'zh-CN',{numeric:true})<0);
});
test('map and elevation have separate close behavior and guarded target selection',()=>{
  assert.match(source,/selectOperationalEntity\(\{ kind: "pallet", id: `erp-location-\$\{locationId\}` \}, "rack"\)/);
  const callback=source.slice(source.indexOf('  const selectRackLocation ='),source.indexOf('  const chooseRackEmptyLocation ='));
  assert.ok(!callback.includes('setRackFocusId(null)'));
  assert.match(source,/selectionOrigin === "map"\) setRackFocusId\(null\)/);
  assert.match(source,/setMoveDraftTargetLocationId\(""\);\s*setWarehouseOperationMessage\(targetState.reason\)/);
  assert.match(source,/className="twin-top-search"/);
  assert.match(source,/className="twin-floor-area-row"/);
  assert.match(source,/className="twin-move-target-list"/);
});

function moveHarness(mutate,refresh=async()=>{}) {
  const state={drafts:[],key:'initial',busy:false,message:'',target:'9',source:{operation:'lot_transfer'}};
  let serial=0;
  const deps={moveDrafts:state.drafts,moveBatchIdempotencyKey:state.key,moveBatchBusy:false,moveSubmitLock:{current:false},moveQuantity:'2',
    setMoveDrafts:v=>{state.drafts=v;deps.moveDrafts=v},
    setMoveBatchIdempotencyKey:v=>{state.key=v;deps.moveBatchIdempotencyKey=v},
    setMoveBatchBusy:v=>{state.busy=v;deps.moveBatchBusy=v},
    setMoveSource:v=>state.source=v,setMoveQuantity:()=>{},setMoveDraftTargetLocationId:v=>state.target=v,
    setWarehouseOperationMessage:v=>state.message=v,operationKey:prefix=>`${prefix}-${++serial}`,
    mutateJson:mutate,refreshDashboard:refresh,buildMoveBatchPayload,upsertMoveDraft,formatNumber:String,inventoryUnitLabel:String};
  const queue=source.slice(source.indexOf('  const queueMoveDraft ='),source.indexOf('  const addSelectedMoveDraft ='));
  const confirm=source.slice(source.indexOf('  const confirmMoveDrafts ='),source.indexOf('  const cancelLocationPointEditing ='));
  vm.createContext(deps);
  vm.runInContext(ts.transpile(queue+confirm+'\nglobalThis.actions={queueMoveDraft,confirmMoveDrafts};'),deps);
  return {...deps.actions,state};
}
const lotSource={operation:'lot_transfer',source_location_id:1,source_key:'lot:1',lot_id:1,source_version:3,expected_lot_version:3,quantity:2,max_quantity:8};
const occupiedTarget={location_id:9,occupancy_status:'occupied',map_rack_id:'R',address_kind:'rack_slot',map_position:{version:4},floor_code:'3F',area_code:'A1'};

test('confirm move submits one guarded batch immediately and prevents a double click',async()=>{
  const calls=[];let finish;
  const h=moveHarness((...args)=>{calls.push(args);return new Promise(resolve=>finish=resolve)});
  assert.equal(h.queueMoveDraft(lotSource,occupiedTarget,2,true),true);
  assert.equal(h.queueMoveDraft(lotSource,occupiedTarget,2,true),false);
  assert.equal(calls.length,1);
  assert.equal(calls[0][0],'/api/warehouse/twin-operations/move-batches');
  assert.equal(calls[0][2].items[0].target_location_id,9);
  assert.equal(calls[0][2].items[0].expected_target_layout_version,4);
  finish({});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.state.drafts.length,0);assert.equal(h.state.busy,false);assert.equal(h.state.source,null);
});

test('failed immediate move keeps the same draft and idempotency key for retry',async()=>{
  const calls=[];const h=moveHarness(async(...args)=>{calls.push(args);if(calls.length===1)throw new Error('network');});
  h.queueMoveDraft(lotSource,occupiedTarget,2,true);
  await new Promise(resolve=>setImmediate(resolve));
  const key=h.state.key;
  assert.equal(h.state.drafts.length,1);assert.match(h.state.message,/已保留/);
  await h.confirmMoveDrafts();
  assert.equal(calls[0][2].idempotency_key,key);assert.equal(calls[1][2].idempotency_key,key);
  assert.equal(h.state.drafts.length,0);
});

test('acknowledged move is not left available to retry when refresh fails',async()=>{
  let calls=0;const h=moveHarness(async()=>{calls++},async()=>{throw new Error('refresh')});
  h.queueMoveDraft(lotSource,occupiedTarget,2,true);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.state.drafts.length,0);assert.match(h.state.message,/不要重复提交/);
  await h.confirmMoveDrafts();assert.equal(calls,1);
});

test('direct submit still rejects whole pallets into rack cells, same source and invalid quantity',()=>{
  let calls=0;const h=moveHarness(async()=>{calls++});
  assert.equal(h.queueMoveDraft({...lotSource,operation:'pallet_move'},occupiedTarget,2,true),false);
  assert.equal(h.queueMoveDraft(lotSource,{...occupiedTarget,location_id:1},2,true),false);
  assert.equal(h.queueMoveDraft(lotSource,occupiedTarget,9,true),false);
  assert.equal(calls,0);assert.equal(h.state.drafts.length,0);
});

test('warehouse parent header compiles using the shipped Vue compiler',()=>{
  const html=fs.readFileSync(new URL('../../../static/index.html',import.meta.url),'utf8');
  const header=html.slice(html.indexOf('<header class="topbar">'),html.indexOf('<div class="layout">'));
  const context={console};vm.createContext(context);
  vm.runInContext(fs.readFileSync(new URL('../../../static/vendor/vue-3.5.40.global.prod.js',import.meta.url),'utf8'),context);
  const render=context.Vue.compile(header);
  assert.equal(typeof render,'function');
  assert.match(header,/warehouse-account-menu/);assert.ok(!header.includes('warehouse-top-shortcuts'));
});
