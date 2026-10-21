const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const html=fs.readFileSync(process.argv[2]||path.join(__dirname,'../static/index.html'),'utf8');
const script=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(x=>x[1]).find(x=>x.trim());
const names=['loadOrderLineInventory','newOrderInventoryState','importDraftInventoryDecisionSnapshot','restoreImportDraftInventoryDecision','inventoryStateMatchesLine','loadPdfDraftInventoryAuthority','pdfInventoryDraftSequence','refreshImportDraftInventoryForSave','finishPdfSave','schedulePdfDraftInventoryAuthority'];
const source=names.map(name=>{
 const start=new RegExp('^          (?:async )?'+name+'\\(','m').exec(script);assert.ok(start,name);
 const tail=script.slice(start.index),next=/^          (?:async )?[A-Za-z_]+\(/m.exec(tail.slice(1));
 return next?tail.slice(0,next.index+1):tail;
}).join('\n');
const box={axios:{},setTimeout,clearTimeout};vm.createContext(box);
const methods=vm.runInContext('({'+source+'})',box);
const make=(name,status='idle',customer=5)=>({name,matched_customer_id:customer,_save_status:status,confirmed:true,items:[{matched_product_id:10,quantity:10,_inventory:{version:1,stale:false,context:{product_id:10,customer_id:customer,quantity:10}}}]});
function context(ds){return {...methods,authGeneration:1,orderImportDrafts:ds,orderImportBatch:{},inventoryPlanApplies:()=>true,pdfImportItemMaterialCode:()=>'',pdfFinishedInventoryExplicitlySkipped:()=>false,errorMessage:e=>e.response?.data?.detail||e.message,
 buildReservationPlan:line=>({finished:[{lot_id:101,expected_version:line._inventory.version,requested_qty:10,confirmed:true}],semi:[]}),reallocateAllDraftInventory(){},loadOrderLineInventory:async(line,customer,preserve)=>{assert.equal(preserve,true);line._inventory={version:2,stale:false,context:{product_id:10,customer_id:customer,quantity:line.quantity}}}};}
let passed=0;
async function test(name,fn){await fn();passed++;console.log('PASS '+name);}
(async()=>{
 await test('real candidate reload preserves manual adoption, refusal and document confirmation',async()=>{
  for(const skipped of [false,true]){
   const d=make('manual'),line=d.items[0],ctx=context([d]);line._inventory=methods.newOrderInventoryState();line._inventory.context={product_id:10,customer_id:5,quantity:10};
   const part=line._inventory.semi.whole;Object.assign(part,{skipped,selected:skipped?null:{lot_id:101,version:1},selected_candidates:skipped?[]:[{lot_id:101,version:1}],manual_override:!skipped,general_confirmation:!skipped});line._inventory.finished.skipped=true;
   Object.assign(ctx,{inventoryCustomerForLine:()=>5,inventoryComponents:()=>['whole'],isSafeSystemInventoryCandidate:()=>false,isDirectSemiDeductionCandidate:()=>false,inventoryCandidatePayload:()=>({}),inventoryPayloadUnavailableReason:()=>'',componentLabel:()=>'',invalidatePdfDraftForItem(){d.confirmed=false}});
   box.axios.get=async url=>({data:url.includes('/master/')?{id:10}:{items:[]}});box.axios.post=async()=>({data:{items:[{lot_id:101,version:2}]}});
   await methods.loadOrderLineInventory.call(ctx,line,5,true);
   assert.equal(d.confirmed,true);assert.equal(line._inventory.api_error,false);assert.equal(line._inventory.finished.skipped,true);assert.equal(line._inventory.semi.whole.skipped,skipped);
   if(!skipped){assert.equal(line._inventory.semi.whole.selected.version,2);assert.equal(line._inventory.semi.whole.manual_override,true);assert.equal(line._inventory.semi.whole.general_confirmation,true)}
  }
 });
 await test('same-customer cohort refreshed; saved and other customer excluded',async()=>{
  const a=make('saved','success'),b=make('current'),c=make('later'),other=make('other','idle',6),ctx=context([a,b,c,other]);
  await ctx.refreshImportDraftInventoryForSave(b);
  let payload;box.axios.post=async(url,p)=>{payload=p;assert.ok(p.items.every(i=>i.reservation_plan.finished[0].expected_version===2));return {data:{items:p.items.map(i=>({client_line_id:i.client_line_id,ok:true}))}}};
  await ctx.loadPdfDraftInventoryAuthority(b);
  assert.equal(payload.items.length,2);assert.equal(b.items[0]._inventory.authoritative_error,'');assert.equal(a.items[0]._inventory.version,1);assert.equal(other.items[0]._inventory.version,1);assert.equal(c.confirmed,true);
 });
 await test('partial refresh cannot issue preview and invalidates late response',async()=>{
  const b=make('b'),c=make('c'),ctx=context([b,c]);let release,resolvePreview,calls=0;
  box.axios.post=()=>{calls++;return new Promise(r=>resolvePreview=r)};
  const old=ctx.loadPdfDraftInventoryAuthority(c);
  ctx.loadOrderLineInventory=async(line,customer,preserve)=>{if(line===b.items[0])await new Promise(r=>release=r);line._inventory={version:2,context:{product_id:10,customer_id:customer,quantity:10}}};
  const refresh=ctx.refreshImportDraftInventoryForSave(b);
  await ctx.loadPdfDraftInventoryAuthority(c);assert.equal(calls,1);
  resolvePreview({data:{items:[{client_line_id:'preview-1',ok:'old'}]}});await old;assert.equal(c.items[0]._inventory.authoritative,null);
  release();await refresh;assert.equal(b._inventory_refreshing,false);assert.equal(c._inventory_refreshing,false);
 });
 await test('refresh failure keeps unrefreshed candidates stale and clears barrier',async()=>{
  const b=make('b'),c=make('c'),ctx=context([b,c]);ctx.loadOrderLineInventory=async()=>{throw Error('offline')};
  await assert.rejects(()=>ctx.refreshImportDraftInventoryForSave(b),/offline/);
  assert.equal(b._inventory_refreshing,false);assert.equal(c._inventory_refreshing,false);assert.equal(c.items[0]._inventory.stale,true);
 });
 await test('auth change stops remaining refresh and does not reallocate',async()=>{
  const b=make('b'),c=make('c'),ctx=context([b,c]);let calls=0;ctx.loadOrderLineInventory=async()=>{calls++;ctx.authGeneration++};ctx.reallocateAllDraftInventory=()=>{throw Error('wrong actor')};
  await ctx.refreshImportDraftInventoryForSave(b);assert.equal(calls,1);assert.equal(c._inventory_refreshing,false);
 });
 await test('successful save refreshes remaining drafts; refresh failure preserves saved receipt',async()=>{
  for(const failed of [false,true]){
   const a=make('a'),b=make('b'),ctx=context([a,b]),record={};let refreshed=false;
   Object.assign(ctx,{pdfSaveStore:()=>({current(){},complete(){return {retained:false}}}),reloadPdfSaveRecovery(){},showToast(){},showOrderNextStepGuide(){},loadOrders:async()=>{},loadKpi:async()=>{}});
   const original=ctx.refreshImportDraftInventoryForSave.bind(ctx);ctx.refreshImportDraftInventoryForSave=async(d)=>{refreshed=true;assert.equal(d._save_status,'success');if(failed)throw Error('offline');await original(d)};
   await ctx.finishPdfSave(record,{id:42,orderNumber:'TM42'},a,()=>true);
   assert.equal(refreshed,true);assert.equal(a._save_status,'success');assert.equal(record.phase,'confirmed');assert.equal(a._saved_order_id,42);assert.equal(b.items[0]._inventory.version,failed?1:2);if(failed)assert.match(a._save_message,/已保存.*刷新失败/);
  }
 });
 await test('saved documents never request a new preview',async()=>{
  const d=make('saved','success'),ctx=context([d]);box.axios.post=()=>{throw Error('saved preview')};await ctx.loadPdfDraftInventoryAuthority(d);ctx.schedulePdfDraftInventoryAuthority(d);assert.equal(d._inventory_authority_timer,undefined);
 });
 console.log(`${passed} stock refresh checks passed`);
})().catch(e=>{console.error(e);process.exitCode=1});
