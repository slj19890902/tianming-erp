const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('static/index.html','utf8');
const script=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]).find(s=>s.trim());
const box={console, setTimeout,clearTimeout,AbortController,URLSearchParams,localStorage:{getItem(){return ''},setItem(){},removeItem(){}},window:{},TMOrderReference:{component:{}},axios:{defaults:{},interceptors:{response:{use(){}}},get:async()=>({data:{items:[]}}),post:async()=>({data:{items:[]}})}};
vm.createContext(box);vm.runInContext(fs.readFileSync('static/vendor/vue-3.5.40.global.prod.js','utf8'),box);
const reactive=box.Vue.reactive;
box.Vue.createApp=d=>{box.definition=d;return{component(){return this},mount(){return this}}};vm.runInContext(script,box);
const ctx={...box.definition.methods,orderForm:{customer_id:1,items:[]},orderImportDrafts:[],inventoryComponents:()=>['whole'],inventoryCandidatePayload:()=>({}),inventoryPayloadUnavailableReason:()=>'',invalidatePdfDraftForItem(){},reallocateAllDraftInventory(){},confirmSafeOrderLineInventoryRecommendations(){},errorMessage:e=>String(e)};

const makeLine=(id=1)=>reactive({product_id:id,quantity:10,product_name:'录入名称',remark:'不能丢'});
const empty=async()=>({data:{items:[]}});
const attach=line=>{ctx.orderForm.items=[line];ctx.orderForm.customer_id=1;return line};
(async()=>{
 let line=attach(makeLine());const observed=[];const stop=box.Vue.watch(()=>line._inventory?.loading,value=>observed.push(value),{flush:"sync"});await ctx.loadOrderLineInventory(line,1);
 assert(observed.includes(true)&&observed.at(-1)===false,"loading must update reactive observers, not only raw state");stop();
 assert.equal(line._inventory.loading,false,'real Vue reactive line must finish successful query');
 assert.equal(ctx.inventoryDecisionRequired(line),'');
 for(const reason of ['HTTP 500','timeout','canceled']) {
   box.axios.post=async(url,body,config)=>{assert.equal(config.timeout,10000);throw Error(reason)};
   await ctx.loadOrderLineInventory(line,1);
   assert(!line._inventory.loading);assert(line._inventory.api_error);assert.equal(ctx.inventoryDecisionRequired(line),'');
   assert.equal(line.remark,'不能丢');assert.equal(line.product_name,'录入名称');
 }
 const lines=[makeLine(1),makeLine(2)];ctx.orderForm.items=lines;
 box.axios.post=async url=>{if(url.includes('/1/'))throw Error('one failed');return {data:{items:[]}}};
 await Promise.all(lines.map(l=>ctx.loadOrderLineInventory(l,1)));
 assert(lines.every(l=>!l._inventory.loading));assert(lines[0]._inventory.api_error);assert(!lines[1]._inventory.api_error);
 let release;box.axios.get=()=>new Promise(resolve=>release=resolve);line=attach(makeLine());
 const old=ctx.loadOrderLineInventory(line,1);
 line.product_id=2;ctx.orderForm.customer_id=2;ctx.invalidateLineInventory(line);
 box.axios.get=async()=>({data:{id:2,items:[]}});box.axios.post=empty;
 await ctx.loadOrderLineInventory(line,2);release({data:{id:1}});await old;
 assert.equal(line._inventory_product.id,2);assert.equal(line._inventory.context.customer_id,2);assert(!line._inventory.loading);
 // Invalidate during debounce, before another request exists.
 box.axios.get=()=>new Promise(resolve=>release=resolve);const pending=ctx.loadOrderLineInventory(line,2);
 line.quantity=25;ctx.invalidateLineInventory(line);release({data:{id:999}});await pending;
 assert.equal(line._inventory_product.id,2);assert(!line._inventory.loading);
 // Selected deductions must survive refresh errors and context changes and block saving.
 line=attach(makeLine());line._inventory=ctx.newOrderInventoryState();line._inventory.context={product_id:1,customer_id:1,quantity:10};
 const selected={lot_id:8,version:1,quantity_available:10};line._inventory.finished.selected_candidates=[selected];line._inventory.finished.selected=selected;line._inventory.finished.allocations=[{candidate:selected,requested_qty:10}];
 box.axios.get=async()=>{throw Error('failure')};await ctx.loadOrderLineInventory(line,1);
 assert.equal(line._inventory.finished.selected_candidates[0].lot_id,8);assert(ctx.inventoryDecisionRequired(line));
 ctx.orderForm.customer_id=2;ctx.invalidateLineInventory(line);box.axios.get=empty;await ctx.loadOrderLineInventory(line,2);
 assert(line._inventory.stale);assert(ctx.inventoryDecisionRequired(line));assert.equal(line._inventory.finished.selected_candidates[0].lot_id,8);
 // No customer means no broad discovery, including late responses after clearing customer.
 line=attach(makeLine());ctx.orderForm.customer_id=null;let calls=0;box.axios.get=async()=>{calls++;return {data:{}}};await ctx.loadOrderLineInventory(line,null);assert.equal(calls,0);
 console.log('PASS: real Vue success/empty, failure/timeout/cancel, multi-line, stale product/customer/quantity, retained input and selected deductions, no-customer discovery');
})().catch(e=>{console.error(e);process.exit(1)});
