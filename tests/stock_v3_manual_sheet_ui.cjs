const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),crypto=require('node:crypto');
const ui=require('../static/ui/manual-sheet-deduction.js');
const html=fs.readFileSync('static/index.html','utf8');
function methods(axios={}) {
  const start=html.indexOf('          manualSheetTargetsForItem(item) {');
  const end=html.indexOf('          async loadReceiptStorage()',start);
  const source=html.slice(start,end).trim().replace(/,$/,'');
  const result=vm.runInNewContext('({'+source+'})',{axios,TMManualSheetDeduction:ui});
  return Object.assign(result,{user:{id:1},authGeneration:1,modal:null,manualSheetOperations:{},
    supplierRequisitionDraft:{supplier_groups:[]},requisitionForm:{items:[]},requisitionItems:[],
    loadRequisition:async()=>true,loadReportedDocuments:async()=>{},loadOrders:async()=>{},showToast(){},
    errorMessage:error=>error.message,requisitionBatchLinePayload:line=>line,initializePurchasePurposeLine(){}});
}
test('saved ordinary reported order and independent BOM aliases expose actual IDs without status gate',()=>{
  const item={id:10592,requisition_status:'已报料',can_requisition:false,quantity:50,
    snapshot_report_length_mm:1980,snapshot_report_width_mm:1376,snapshot_pieces_per_box:2};
  assert.deepEqual(ui.orderTargets(item),[{key:'order:10592:whole',order_item_id:10592,component_type:'whole',label:'主件 · 整片'}]);
  for(const style of ['TDGD 天地盖底','独立底','TDGG 天地盖盖','tdgd 天地盖底','bottom_base'])assert.deepEqual(ui.physicalComponents({snapshot_component_box_style:style},true),['whole']);
  const bom=ui.orderTargets({id:10592,bom_components:[{id:31,component_product_id:3496,product_name:'下盖',snapshot_component_box_style:'独立底'},
    {id:32,component_product_id:3787,product_name:'上盖',sheet_cutting_settings_snapshot:{schema_version:2,whole:{}}}]});
  assert.deepEqual(bom.map(row=>row.bom_snapshot_id),[31,32]);
  assert.ok(bom.every(row=>row.component_type==='whole'));
});
test('typed requisition source selects one exact BOM use and ordinary frozen cover/base',()=>{
  assert.deepEqual(ui.reportedTargets({order_item_id:1,source_key:'component:18:base'})[0],
    {key:'bom:18:base',order_item_id:1,bom_snapshot_id:18,component_type:'base',label:'子件 · 底片'});
  assert.equal(ui.reportedTargets({order_item_id:1,source_key:'order_item:1:cover'})[0].component_type,'cover');
  assert.equal(ui.reportedTargets({order_item_id:1,product_name:'产品-底'})[0].component_type,'base');
  assert.deepEqual(ui.reportedTargets({order_item_id:1,source_key:'component:bad:base'}),[]);
  assert.deepEqual(ui.reportedTargets({order_item_id:1,source_key:'order_item:2:whole'}),[]);
});
test('BOM manual write refreshes child fingerprint and grouped parent totals without changing sibling or extras',async()=>{
  const requests=[];
  const root=methods({post:async(path,data)=>{requests.push({path,data});return {data:{order_purpose_sheet_qty:33,
    remaining_required_piece_qty:33,purpose_plan_version:2,purpose_plan_fingerprint:'new-A'}};}});
  root.modal={type:'requisition'};
  const a={bom_snapshot_id:31,is_composite_component:true,component_type:'whole',order_purpose_sheet_qty:40,
    _minimum_requisition_qty:40,remaining_required_piece_qty:40,purpose_plan_fingerprint:'old-A',remark:'A备注'};
  const b={bom_snapshot_id:32,is_composite_component:true,_minimum_requisition_qty:30,order_purpose_sheet_qty:30,
    remaining_required_piece_qty:30,purpose_plan_fingerprint:'keep-B'};
  const group={is_composite_component_group:true,_merged_component_lines:[a,b],stock_purpose_sheet_qty:5,remark:'保留合并备注'};
  root.requisitionForm.items=[group];
  await root.refreshManualBomSheetDeduction(a,{message:'已预占7张',remaining_required_piece_qty:33,semi_finished_reserved_piece_qty:7},group);
  assert.equal(requests[0].path,'/api/requisition/bom-sheet-cutting/preview');
  assert.equal(requests[0].data._manual_sheet_refresh_error,false);
  assert.equal(a.purpose_plan_fingerprint,'new-A');assert.equal(a.purpose_plan_version,2);
  assert.equal(b.purpose_plan_fingerprint,'keep-B');assert.equal(a.remark,'A备注');
  assert.equal(group.order_purpose_sheet_qty,63);assert.equal(group.purchase_total_sheet_qty,68);
  assert.equal(group.stock_purpose_sheet_qty,5);assert.equal(group.remark,'保留合并备注');
  assert.equal(Object.keys(root.manualSheetOperations).length,0);
});
test('zero order demand removes empty purchase row while explicit reserve purchase survives',async()=>{
  for(const extra of [0,7]) {
    const root=methods({post:async()=>({data:{order_purpose_sheet_qty:0,remaining_required_piece_qty:0,purpose_plan_fingerprint:'zero'}})});
    root.modal={type:'requisition'};
    const line={bom_snapshot_id:31,is_composite_component:true,stock_purpose_sheet_qty:extra,remark:'保留'};
    root.requisitionForm.items=[line];
    await root.refreshManualBomSheetDeduction(line,{message:'成功',remaining_required_piece_qty:0});
    assert.equal(root.requisitionForm.items.length,extra?1:0);
    assert.equal(line.purchase_total_sheet_qty,extra);assert.equal(line.remark,'保留');
    assert.equal(Boolean(root.modal),Boolean(extra));
  }
});
test('failed draft refresh marks supplier document stale and blocks old BOM payload',async()=>{
  const root=methods({post:async()=>{throw Error('preview unavailable')}});
  root.modal={type:'supplierRequisitionDraft'};
  const line={bom_snapshot_id:31,is_composite_component:true,stock_purpose_sheet_qty:5};
  await root.refreshManualBomSheetDeduction(line,{message:'成功',remaining_required_piece_qty:33});
  assert.equal(line._manual_sheet_refresh_error,true);
  assert.equal(root.supplierRequisitionDraft._manual_sheet_refresh_error,true);
  assert.equal(Object.keys(root.manualSheetOperations).length,0);
  assert.match(html,/validateSupplierRequisitionDraft\(\) \{\s*if\(this.supplierRequisitionDraft\?\._manual_sheet_refresh_error\)/);
  assert.match(html,/requisitionBatchLinePayloads\(line\) \{\s*if\(line\?\._manual_sheet_refresh_error\)/);
});
test('ordinary supplier draft runs existing authority refresh after manual allocation',async()=>{
  const root=methods();root.modal={type:'supplierRequisitionDraft'};let refreshes=0;
  root.refreshSupplierRequisitionDraftAfterInventoryReservation=async()=>{refreshes++;root.supplierRequisitionDraft={supplier_groups:[{lines:[
    {order_purpose_sheet_qty:40,stock_purpose_sheet_qty:7,remark:'原备注',purpose_plan_fingerprint:'new'}]}]};};
  await root.refreshManualBomSheetDeduction({order_item_id:10592},{message:'预占60',remaining_required_piece_qty:40});
  assert.equal(refreshes,1);assert.equal(root.supplierRequisitionDraft.supplier_groups[0].lines[0].stock_purpose_sheet_qty,7);
});
test('order read refresh cannot overwrite a later drawer or another signed-in actor',async()=>{
  let complete;const root=methods({get:()=>new Promise(resolve=>{complete=resolve})});
  root.modal={type:'orderDetail'};root.orderDetail={id:1};
  const done=root.refreshManualSheetOrder({id:10592},{message:'成功'},1);
  root.orderDetail={id:2};complete({data:{id:1,items:[]}});await done;
  assert.equal(root.orderDetail.id,2);assert.equal(Object.keys(root.manualSheetOperations).length,0);
});
test('reported drawer obtains its exact source from original document, refresh is read-only',async()=>{
  const root=methods({get:async(path,args)=>({data:{items:[{id:56,source_type:'legacy_material_requisition',line_items:[
    {id:206,order_item_id:10592,source_key:null,product_name:'000204'}]}]}})});
  root.canWarehouseReserve=true;
  const row={stable_id:'legacy_requisition:56:206',source_type:'legacy_material_requisition',document_id:56,item_id:206,
    document_number:'BL-20261014-001',order_item_id:10592};root.reportedItemDetail=row;
  await root.loadManualReportedSheetTargets(row);
  assert.equal(row._manual_sheet_targets[0].order_item_id,10592);
  assert.equal(row._manual_sheet_targets[0].component_type,'whole');assert.equal(row._manual_sheet_loading,false);
});
test('uncertain stock recovery remains clickable while parent save and closing are guarded',()=>{
  assert.ok(!/<fieldset[^>]*requisitionInventoryActionBlocked\(\)/.test(html));
  assert.match(html,/async saveSupplierRequisitionDraft\(\) \{\s*if\(this.requisitionInventoryActionBlocked\(\)\)/);
  assert.match(html,/async saveCompositeRequisitionDraft\(\) \{\s*if\(this.requisitionInventoryActionBlocked\(\)\)/);
  assert.match(html,/closeReportedItemDetail\(\) \{\s*if\(this.manualSheetParentControlsBlocked\(\)/);
  const root=methods();root.manualSheetOperations={'user:1:whole':true};
  assert.equal(root.manualSheetDeductionDisabled(),true);assert.equal(root.requisitionInventoryBaseActionBlocked(),false);
});
test('all inline scripts parse; affected order, reported and procurement templates compile',()=>{
  for(const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi))if(match[1].trim())new vm.Script(match[1]);
  const sandbox={console};vm.runInNewContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),sandbox);
  for(const [start,end] of [["<aside v-if=\"reportedItemDetail\"",'</aside>'],["<div v-else-if=\"modal.type === \'orderDetail\' && orderDetail\">","<div v-else-if=\"modal.type === \'orderBomCutover\'"]]) {
    const from=html.indexOf(start);assert.ok(from>=0);
    let to=html.indexOf(end,from);if(to<0)to=html.indexOf('<div v-else-if=',from+start.length);
    assert.ok(to>from);sandbox.Vue.compile((html.slice(from,to)+(end==='</aside>'?end:'')).replace('v-else-if=', 'v-if='),{decodeEntities:v=>v});
  }
  for(const match of html.matchAll(/<fieldset :disabled="requisitionInventoryBaseActionBlocked[^>]*>/g)) {
    const end=html.indexOf('</fieldset>',match.index);
    sandbox.Vue.compile(html.slice(match.index,end+11),{decodeEntities:v=>v});
  }
});
test('cache fingerprints match manual and dispatch source and product index tracks root approved search hash',()=>{
  for(const path of ['manual-sheet-deduction','delivery-dispatch-recovery']) {
    const digest=crypto.createHash('sha256').update(fs.readFileSync('static/ui/'+path+'.js')).digest('hex').slice(0,12);
    assert.ok(html.includes('/static/ui/'+path+'.js?v='+digest),path+' stale cache');
  }
  assert.ok(html.includes('product-workbench.js?v=20261019origins01'));
});

test('a covered child contributes no zero supplier payload; uncovered sibling retains its own fingerprint',()=>{
  const start=html.indexOf('          requisitionBatchLinePayloads(line) {'),end=html.indexOf('          async openRequisition()',start);
  const root=vm.runInNewContext('({'+html.slice(start,end).trim().replace(/,$/,'')+'})');
  Object.assign(root,{normalizeCuttingMode:value=>value,cuttingModeFactor:()=>1,requisitionBatchLinePayload:line=>line});
  const group={is_composite_component_group:true,special_process:'一开一',cardboard_len:1000,cardboard_width:700,
    purchase_total_sheet_qty:30,order_purpose_sheet_qty:30,stock_purpose_sheet_qty:0,remark:'合并备注',
    _merged_component_lines:[{bom_snapshot_id:31,remaining_required_piece_qty:0,purpose_plan_fingerprint:'A-zero'},
      {bom_snapshot_id:32,remaining_required_piece_qty:30,purpose_plan_fingerprint:'B-thirty'}]};
  const rows=root.requisitionBatchLinePayloads(group);
  assert.equal(rows.length,1);assert.equal(rows[0].bom_snapshot_id,32);
  assert.equal(rows[0].requisition_qty,30);assert.equal(rows[0].purpose_plan_fingerprint,'B-thirty');
});
