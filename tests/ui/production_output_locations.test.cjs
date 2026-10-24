const {test}=require('node:test');
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const html=fs.readFileSync(require.resolve('../../static/index.html'),'utf8');
const desk=fs.readFileSync(require.resolve('../../static/ui/production-desk.js'),'utf8');

function harness(post){
  const requests=[],toasts=[];
  const box={document:{activeElement:null},axios:{post:async(url,body)=>{requests.push({url,body:JSON.parse(JSON.stringify(body))});if(post)return post(url,body);return {data:{}};}},createIdempotencyKey:()=>`test-${requests.length}`};
  box.window=box;vm.createContext(box);
  function actual(name){
    const m=html.match(new RegExp('^          (?:async )?'+name+'\\([^\\n]*\\) \\{','m'));
    assert.ok(m,name);const end=html.indexOf('\n          },',m.index);
    return vm.runInContext('({'+html.slice(m.index,end+13).trim().replace(/,$/,'')+'})',box)[name];
  }
  const c={authGeneration:1,canWarehouseExecute:true,canProductionExecute:true,productionSelected:{},productionPending:[],productionCompletionAttempts:{},productionTab:'pending',productionBusy:false,
    productionLocations:[{id:7,area_id:3,area_code:'DEFAULT',warehouse_floor:3,location_name:'三楼货架2层1格',is_empty:false,storage_type:'rack',layout_version:5},
      {id:8,area_id:4,area_code:'CUSTOMER',warehouse_floor:1,location_name:'一楼客户区域',is_empty:true,storage_type:'ground',layout_version:2},
      {id:9,area_id:3,area_code:'DEFAULT',warehouse_floor:3,location_name:'三楼地面2位',is_empty:false,storage_type:'ground',layout_version:5}],
    inventoryLocation:loc=>loc?.location_name||'',ensureProductionLocations:async()=>true,
    refreshProductionPreview:async()=>{},focusProductionDialog(){},autoSelectProductionRow(row){this.productionSelected[row.id]=this.canConfirmProductionRow(row);},
    loadProduction:async()=>{},loadOrders:async()=>{},loadDeliveries:async()=>{},loadKpi:async()=>{},hasPermission:()=>false,
    showProductionNextStepGuide(){},showToast:(message,error)=>toasts.push({message,error}),errorMessage:e=>e.message};
  for(const name of ['productionDefaultStorage','productionHasProductStorage','productionPreferredAreaIds','productionPreferredAreas','productionPreferredAreaHint','productionLocationsForRow','productionLocationFloorNumber','productionLocationAreaCode','productionLocation','productionNeedsLocation','productionUsesBatchConfirmation','ensureProductionMode','canConfirmProductionRow','productionTheoreticalOutput','productionDefectiveQuantity','productionCompletionRequestItems','batchConfirmProduction'])c[name]=actual(name);
  const m=html.match(/^          productionOutputLocations\(\) \{/m),end=html.indexOf('\n          },',m.index);
  const available=vm.runInContext('({'+html.slice(m.index,end+13).trim().replace(/,$/,'')+'})',box).productionOutputLocations;
  Object.defineProperty(c,'productionOutputLocations',{get:()=>available.call(c)});
  Object.defineProperty(c,'productionLocationFloors',{get:()=>[{number:3},{number:1}]});
  vm.runInContext(desk,box);let mixin;box.ERPProductionDesk.install({mixin:value=>mixin=value});c.openProductionEntry=mixin.methods.openProductionEntry;c.registerProductionOutput=mixin.methods.registerProductionOutput;
  return {c,requests,toasts};
}
const row=()=>({id:11,version:4,customer_id:2,status:'pending',completion_mode:'',actual_input_quantity:5,actual_output_quantity:5,output_factor:1,pieces_per_box:1,
  preferred_finished_storage_area_ids:[4],production_default_storage:{configured:true,source:'product_location',product_id:100,preference_version:3,area_id:3,location_id:7,layout_version:5,location:{name:'三楼货架2层1格'},issue:null}});

test('opening output uses the saved common-box location even outside customer area, with no automatic save',async()=>{
  const {c,requests}=harness(),r=row();c.productionPending=[r];await c.openProductionEntry(r);
  assert.equal(r.completion_mode,'stock');assert.equal(r.location_id,7);assert.equal(r.location_floor_number,3);assert.equal(r.location_area_code,'DEFAULT');
  assert.equal(c.canConfirmProductionRow(r),true);assert.match(c.productionPreferredAreaHint(r),/常用箱默认位置.*三楼货架/);assert.equal(requests.length,0);
  const item=c.productionCompletionRequestItems([r])[0];assert.equal(item.location_id,7);assert.equal(item.expected_layout_version,5);assert.equal(item.actual_output_quantity,5);
});

test('valid occupied rack and ground locations are selectable, and a manual choice survives quantity/default updates',async()=>{
  const {c}=harness(),r=row();r.completion_mode='stock';r.location_id=9;
  await c.ensureProductionMode(r);assert.equal(r.location_id,9);assert.ok(c.productionLocationsForRow(r).some(l=>l.id===9));
  assert.equal(c.canConfirmProductionRow(r),true);
  c.productionLocations[2].can_store=false;assert.ok(!c.productionOutputLocations.some(l=>l.id===9));
});

test('an invalid saved default is not silently replaced with a customer-area location',async()=>{
  const {c}=harness(),r=row();r.production_default_storage.issue='货位已停用';r.production_default_storage.location_id=null;
  await c.openProductionEntry(r);assert.equal(r.completion_mode,'');assert.equal(r.location_id,undefined);
  r.completion_mode='stock';await c.ensureProductionMode(r);assert.equal(r.location_id,null);assert.equal(c.canConfirmProductionRow(r),false);
  r.location_id=9;assert.equal(c.canConfirmProductionRow(r),false,'an invalid explicit preference must be corrected before manual output');
});

test('missing product preference follows customer routing without guessing a common-box location',async()=>{
  const {c}=harness(),r=row();delete r.production_default_storage;await c.openProductionEntry(r);assert.equal(r.completion_mode,'');
  r.completion_mode='stock';await c.ensureProductionMode(r);assert.equal(r.location_id,8);
});

test('closing the dialog during a location read does not change its old draft or submit it',async()=>{
  const {c,requests}=harness(),r=row();let resolve;c.ensureProductionLocations=()=>new Promise(r=>resolve=r);
  const opening=c.openProductionEntry(r);c.productionEntry=null;resolve(true);await opening;
  assert.equal(r.completion_mode,'');assert.equal(r.location_id,undefined);assert.equal(requests.length,0);
});

test('multiple production rows can submit the same valid occupied location, retaining individual task versions',async()=>{
  const {c,requests}=harness(),a=row(),b={...row(),id:12,version:6};for(const r of [a,b]){r.completion_mode='stock';r.location_id=7;c.productionSelected[r.id]=true;}c.productionPending=[a,b];
  await c.batchConfirmProduction();assert.equal(requests.length,1);assert.deepEqual(requests[0].body.items.map(i=>[i.task_id,i.expected_version,i.location_id]),[[11,4,7],[12,6,7]]);assert.equal(c.productionTab,'history');
});

test('an unconfirmed save retains the selected rows and exact retry key on the pending page',async()=>{
  const {c,requests,toasts}=harness(()=>{throw Error('暂时断线');}),r=row();r.completion_mode='stock';r.location_id=7;c.productionPending=[r];c.productionSelected[r.id]=true;
  await c.batchConfirmProduction();await c.batchConfirmProduction();assert.equal(c.productionTab,'pending');assert.equal(c.productionSelected[r.id],true);assert.equal(r.location_id,7);
  assert.equal(requests.length,2);assert.deepEqual(requests[0].body,requests[1].body);assert.match(toasts[0].message,/失败项已保留/);
});

test('after a failed save and real list-row replacement, the dialog edits the same row that retry submits',async()=>{
  const {c,requests}=harness(()=>{throw Error('暂时断线');}),r=row();r.completion_mode='stock';r.location_id=7;c.productionPending=[r];c.productionSelected[r.id]=true;c.productionEntry=r;
  c.loadProduction=async()=>{c.productionPending=c.productionPending.map(item=>({...item}));};
  c.refreshProductionPreview=async item=>{item.output_preview={allowed:true};item.preview_error='';};
  await c.batchConfirmProduction();assert.notEqual(c.productionEntry,r);assert.equal(c.productionEntry,c.productionPending[0]);
  c.productionEntry.actual_output_quantity=3;await c.registerProductionOutput(c.productionEntry);
  assert.equal(requests[0].body.items[0].actual_output_quantity,5);assert.equal(requests[1].body.items[0].actual_output_quantity,3);
  assert.notEqual(requests[1].body.idempotency_key,requests[0].body.idempotency_key);
});

test('a missing or changed task after a failed save closes the old dialog rather than retrying stale inputs',async()=>{
  for(const version of [null,99]){
    const {c,requests}=harness(()=>{throw Error('暂时断线');}),r=row();r.completion_mode='stock';r.location_id=7;c.productionPending=[r];c.productionSelected[r.id]=true;c.productionEntry=r;
    c.loadProduction=async()=>{c.productionPending=version===null?[]:[{...r,version}];};await c.batchConfirmProduction();assert.equal(c.productionEntry,null);assert.equal(requests.length,1);
  }
});
