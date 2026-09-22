const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('static/index.html','utf8');
const script=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]).find(s=>s.trim());
const sandbox={axios:{defaults:{},interceptors:{response:{use(){}}}},Vue:{createApp(d){sandbox.definition=d;return{component(){return this},mount(){return this}}}},TMOrderReference:{component:{}},localStorage:{getItem(){return ''},setItem(){},removeItem(){}},window:{},console,URLSearchParams,setTimeout,clearTimeout,crypto:require('crypto').webcrypto};
vm.createContext(sandbox);vm.runInContext(script,sandbox);
const candidate=(id,qty)=>({lot_id:id,version:1,available_stock_quantity:qty,stock_yield_per_sheet:1,deductible_requirement_quantity:qty,safe_group_eligible:true,match_kind:'exact',signature_differences:['customer']});
const a=candidate(11,334),b=candidate(12,100),yellow={...candidate(13,999),safe_group_eligible:false,match_kind:'other_dimension_cut',requires_requisition_cut_plan:true};
const options=[224,112].map((qty,i)=>({order_item_id:7+i,component_type:'whole',board_length_mm:800,board_width_mm:600,remaining_requirement_quantity:qty,recommended_candidates:[a,b,yellow]}));
const line={line_key:'physical',report_length_mm:800,report_width_mm:600,source_items:options.map(o=>({order_item_id:o.order_item_id,late_semi_inventory_options:[o]}))};
const ctx={...sandbox.definition.methods,supplierRequisitionDraft:{supplier_groups:[{lines:[line]}]},requisitionInventoryActionBlocked:()=>false,showToast(){}};
(async()=>{
  const pool=ctx.draftLineInventoryPool(line);
  assert.equal(pool.demand,336);assert.equal(pool.stock,434);assert.equal(pool.allocated,336);assert.equal(pool.lots.length,2);
  const requests=[];sandbox.axios.post=async(url,data)=>{requests.push(JSON.parse(JSON.stringify(data)));return {data:{}}};
  ctx.executeRequisitionInventoryAction=async config=>{await config.submit();return true};
  await ctx.reserveDraftExactSemiInventory(line);
  assert.equal(requests.length,1);assert.equal(requests[0].allocation_mode,'pooled');
  assert.deepEqual(requests[0].items.map(i=>i.requested_requirement_quantity),[224,112]);
  assert.deepEqual(requests[0].items.map(i=>i.lots.map(l=>l.lot_id)),[[11,12],[11,12]]);
  // UI must pass the shared pool, not discard a whole pallet after the first order.
  a.available_stock_quantity=100;b.available_stock_quantity=200;
  assert.equal(ctx.draftLineInventoryPool(line).shortfall,36);
  line.report_length_mm=801;assert.equal(ctx.draftLineInventoryPool(line).allocated,0);
  line.report_length_mm=800;
  const fresh={supplier_groups:[{lines:[{line_key:'physical',source_items:[{order_item_id:8}],order_purpose_sheet_qty:36}]}]};
  line.remark='保留备注';line.stock_purpose_sheet_qty=5;
  ctx.restoreSupplierDraftEdits(ctx.supplierRequisitionDraft,fresh);
  assert.equal(fresh.supplier_groups[0].lines[0].remark,'保留备注');
  assert.equal(fresh.supplier_groups[0].lines[0].purchase_total_sheet_qty,41);
  const amount={authoritative_order_sheet_qty:700,requisition_qty:700};
  ctx.initializePurchasePurposeLine(amount);
  amount.purchase_total_sheet_qty=750;ctx.onPurchasePurposeTotalChanged(amount);
  assert.equal(amount.order_purpose_sheet_qty,700);assert.equal(amount.stock_purpose_sheet_qty,50);
  assert.equal(ctx.draftPurchaseQuantityHint(amount),'多备 50 张材料');
  amount.purchase_total_sheet_qty=650;ctx.onPurchasePurposeTotalChanged(amount);
  assert.equal(amount.order_purpose_sheet_qty,650);assert.equal(amount.stock_purpose_sheet_qty,0);
  assert.equal(ctx.draftPurchaseQuantityHint(amount),'尚有 50 张待报料');
  const cutting={remaining_required_piece_qty:100,cutting_mode:'1',requisition_qty:100};
  ctx.initializePurchasePurposeLine(cutting);cutting.purchase_total_sheet_qty=110;
  ctx.onPurchasePurposeTotalChanged(cutting);
  ctx.changeSupplierDraftCuttingMode(cutting,'2');
  assert.equal(cutting.purchase_total_sheet_qty,60);assert.equal(cutting.stock_purpose_sheet_qty,10);
  // Full coverage removes the orders from pending, but must retain explicit extra material and notes.
  ctx.supplierRequisitionSelections=[{type:'order_item',order_item_id:7},{type:'order_item',order_item_id:8}];
  ctx.requisitionPending=[];ctx.modal={type:'supplierRequisitionDraft'};
  sandbox.axios.post=async(url,request)=>{
    assert.equal(request.selections.length,2);
    assert.ok(request.selections.every(row=>row.retain_stock_purchase));
    return {data:{supplier_groups:[{lines:[{line_key:'physical',source_items:line.source_items,
      authoritative_order_sheet_qty:0,order_purpose_sheet_qty:0,stock_purpose_sheet_qty:0,
      purchase_total_sheet_qty:0,retain_stock_purchase:true}]}]}};
  };
  await ctx.refreshSupplierRequisitionDraftAfterInventoryReservation();
  const retained=ctx.supplierRequisitionDraft.supplier_groups[0].lines[0];
  assert.equal(retained.purchase_total_sheet_qty,5);assert.equal(retained.order_purpose_sheet_qty,0);
  assert.equal(retained.remark,'保留备注');assert.ok(ctx.modal);
  console.log('PASS pooled total, shared pallets, shortage, exact-only, single request and surviving draft edits');
})().catch(e=>{console.error(e);process.exit(1)});
