const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('static/index.html','utf8');
const script=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]).find(s=>s.trim());
const axios={defaults:{},interceptors:{request:{use(){}},response:{use(){}}}};
const box={axios,Vue:{createApp(d){box.methods=d.methods;return {}}},window:{},localStorage:{getItem(){return ''}},console,URLSearchParams,setTimeout,clearTimeout};vm.createContext(box);vm.runInContext(script.split('app.mixin(TMDemandStock.mixin)')[0],box);
const methods=box.methods;
(async()=>{
 const c={...methods,orderForm:{customer_id:1,items:[]},orderImportDrafts:[],authGeneration:1,showToast(){},reallocateAllDraftInventory(){},errorMessage(e){return e.message}};
 const line={product_id:1,quantity:600,_inventory_product:{id:1,is_composite:true,order_parent_inventory_eligible:false},_inventory:c.newOrderInventoryState()};c.orderForm.items=[line];line._inventory.finished.allocations=[{candidate:{lot_id:1,version:1},requested_qty:600}];
 assert.equal(c.inventoryPlanApplies(line),false);assert.equal(c.buildReservationPlan(line).finished.length,0);assert.equal(c.inventoryDecisionRequired(line),'');
 c.refreshOrderLineInventory(line,1);assert.equal(line._inventory,null);assert.equal(line._inventory_product.id,1);assert.equal(c.inventoryPlanApplies(line),false);
 const incoming={product_id:2,quantity:600};c.orderForm.items=[incoming];const calls=[];axios.get=async path=>{calls.push(path);return {data:{id:2,is_composite:true,order_parent_inventory_eligible:false}}};await c.loadOrderLineInventory(incoming,1);assert.equal(calls.length,1);assert.equal(incoming._inventory,null);assert.equal(c.inventoryPlanApplies(incoming),false);
 const physical={product_id:3,quantity:600,_inventory_product:{id:3,is_composite:true,order_parent_inventory_eligible:true}};assert.equal(c.inventoryPlanApplies(physical),true);assert.equal(c.inventoryComponents(physical).length,0);
 assert.equal(c.inventoryPlanApplies({product_id:4,_inventory_product:{order_parent_inventory_eligible:true}}),true);
 // Execute the real UI methods: preserve one immutable request after a
 // lost response and suppress responses from a previous account or item.
 c.user={id:1};c.canWarehouseReserve=true;c.canAdmin=true;c.hasPermission=()=>true;
 c.pageCacheUpdatedAt={orders:123};c.loadOrders=async()=>true;
 c.modal={type:'orderBomStockReview'};
 const state={itemId:1,actorId:1,authGeneration:1,rows:[],loading:false,busy:false,pending:{url:'/test-adopt',payload:{idempotency_key:'same-key',lots:[{lot_id:4,expected_version:1}]}},error:'',sequence:0};
 c.orderBomStockReview=state;const posted=[];
 axios.post=async (url,payload)=>{posted.push(JSON.stringify(payload));throw new Error('network lost');};
 await c.submitOrderBomStockReview();assert.ok(state.pending);assert.equal(state.busy,false);
 axios.post=async (url,payload)=>{posted.push(JSON.stringify(payload));return {data:{message:'saved'}}};
 axios.get=async ()=>({data:{rows:[]}});
 await c.submitOrderBomStockReview();assert.equal(posted[0],posted[1]);assert.equal(state.pending,null);
 let done;axios.get=()=>new Promise(resolve=>done=resolve);const loading=c.loadOrderBomStockReview();
 c.authGeneration=2;c.user={id:2};done({data:{rows:[{snapshot_id:99}]}});await loading;assert.equal(state.rows.length,0);
 c.authGeneration=1;c.user={id:1};
 state.pending={url:'/explicit',payload:{idempotency_key:'explicit'}};
 axios.post=async ()=>{throw {response:{status:409},message:'version changed'}};
 await c.submitOrderBomStockReview();assert.equal(state.pending,null);assert.match(state.error,/已选内容保留/);
 console.log('PASS: components-only parents discard ghost plans and keep product facts; refreshed role prevents parent query; physical parent still allowed');
})().catch(e=>{console.error(e);process.exitCode=1});
