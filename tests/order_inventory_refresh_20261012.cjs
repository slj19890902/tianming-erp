const fs = require('fs');
const vm = require('vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[2] || 'static/index.html', 'utf8');
const script = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]).find(s => s.trim());
const sandbox = {axios:{defaults:{},interceptors:{request:{use(){}},response:{use(){}}}},
  Vue:{createApp(def){sandbox.methods=def.methods;return {mixin(){return this;},component(){return this;},mount(){return this;}};}},
  localStorage:{getItem(){return '';},setItem(){},removeItem(){}},window:{},TMOrderReference:{component:{}},
  TMDemandStock:{mixin:{}},console,URLSearchParams,setTimeout,clearTimeout};
vm.createContext(sandbox); vm.runInContext(script.slice(0, script.indexOf('app.mixin(TMDemandStock.mixin)')), sandbox);
const methods = sandbox.methods;
const candidate = {lot_id:44,quantity_available:100,version:2};
function setup(manual=false) {
  const line = {matched_product_id:1,quantity:34,unit_price:1};
  const draft = {matched_customer_id:1,items:[line]};
  const ctx = {...methods,orderForm:{items:[]},orderImportDrafts:[draft],orderImportBatch:{},authGeneration:1,
    invalidatePdfDraftForItem(){},schedulePdfDraftInventoryAuthority(){},showToast(){},errorMessage(e){return e.message;}};
  line._inventory = ctx.newOrderInventoryState();
  line._inventory.context = {customer_id:1,product_id:1,quantity:34};
  const selected = {...candidate,...(manual ? {is_general:true,warning_codes:['GENERAL_FINISHED_STOCK']} : {})};
  line._inventory.finished = {...line._inventory.finished,candidates:[selected],selected,selected_candidates:[selected]};
  sandbox.axios.get = async url => ({data:url.startsWith('/api/master') ? {is_composite:true} : {items:[selected]}});
  return {ctx,line,draft,selected};
}
(async () => {
  let {ctx,line} = setup();
  line.quantity = 50;
  ctx.invalidateLineInventory(line);
  await ctx.loadOrderLineInventory(line,1);
  assert.equal(line._inventory.stale,false,'automatic recommendation stays stale after editing quantity');
  assert.equal(line._inventory.finished.allocations[0]?.requested_qty,50);
  assert.equal(ctx.inventoryDecisionRequired(line),'');
  // An explicit refresh with unchanged inputs also recovers after invalidation.
  ctx.invalidateLineInventory(line); await ctx.loadOrderLineInventory(line,1);
  assert.equal(line._inventory.stale,false);
  assert.equal(line._inventory.finished.allocations[0]?.requested_qty,50);
  ({ctx,line}=setup(true));
  line.quantity=50; ctx.invalidateLineInventory(line); await ctx.loadOrderLineInventory(line,1);
  assert.equal(line._inventory.loading,false);
  assert.equal(line._inventory.stale,false,'fresh candidates must not masquerade as a pending query');
  assert.ok(ctx.inventoryDecisionRequired(line),'manual confirmation must not survive changed quantity automatically');
  ctx.confirmOrderLineInventory(line,'finished',line._inventory.finished.candidates[0]);
  assert.equal(ctx.inventoryDecisionRequired(line),'');
  assert.equal(line._inventory.finished.allocations[0]?.requested_qty,50);
  // Explicit refresh preserves an unchanged manual decision using fresh versions.
  ctx.invalidateLineInventory(line); await ctx.loadOrderLineInventory(line,1);
  assert.equal(ctx.inventoryDecisionRequired(line),'');
  assert.equal(line._inventory.finished.selected.version,2);
  // Shared stock never gets allocated twice after edited rows finish querying.
  ({ctx,line}=setup());
  const second={matched_product_id:1,quantity:80,_inventory:ctx.newOrderInventoryState(),_inventory_product:{is_composite:true}};
  second._inventory.context={customer_id:1,product_id:1,quantity:80};
  second._inventory.finished={...second._inventory.finished,candidates:[{...candidate}],selected:{...candidate},selected_candidates:[{...candidate}]};
  ctx.orderImportDrafts[0].items.push(second);
  line.quantity=50; ctx.invalidateLineInventory(line); await ctx.loadOrderLineInventory(line,1);
  assert.equal(second._inventory.finished.allocations[0]?.requested_qty,50);
  // A deliberate refusal must not silently turn into a reservation on edit.
  ({ctx,line}=setup()); ctx.skipOrderLineInventory(line,'finished');
  line.quantity=45; ctx.invalidateLineInventory(line); await ctx.loadOrderLineInventory(line,1);
  assert.equal(line._inventory.finished.skipped,true);
  assert.equal(line._inventory.finished.allocations.length,0);
  // A rejected older request cannot overwrite the latest successful query.
  ({ctx,line}=setup());
  let rejectOld; const oldProduct=new Promise((resolve,reject)=>{rejectOld=reject;});
  sandbox.axios.get=()=>oldProduct;
  const old=ctx.loadOrderLineInventory(line,1);
  line.quantity=40;ctx.invalidateLineInventory(line);
  sandbox.axios.get=async url=>({data:url.startsWith('/api/master')?{is_composite:true}:{items:[candidate]}});
  await ctx.loadOrderLineInventory(line,1);
  rejectOld(Error('obsolete network failure'));await old;
  assert.equal(line._inventory.api_error,false);
  assert.equal(line._inventory.context.quantity,40);
  assert.equal(line._inventory.finished.allocations[0].requested_qty,40);
  console.log('automatic/manual edit, explicit refresh, shared batch stock passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
