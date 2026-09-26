const fs=require('fs'),vm=require('vm'),path=require('path'),assert=require('assert/strict');
const root=process.argv[2],html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const source=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(x=>x[1]).filter(x=>x.trim());
let pending=[];
const box={axios:{defaults:{},interceptors:{response:{use(){}}},get(url){return new Promise((resolve,reject)=>pending.push({url,resolve,reject}))},post(){throw Error('must not write')}},
 Vue:{createApp(d){box.d=d;return {component(){return this},mount(){return this}}}},window:{},document:{},
 localStorage:{getItem(){return ''},setItem(){},removeItem(){}},TMOrderReference:{component:{}},URLSearchParams,setTimeout,clearTimeout,console};
vm.createContext(box);vm.runInContext(fs.readFileSync(path.join(root,'static/assets/time-utils.js'),'utf8'),box);
box.TmTime=box.window.TmTime||box.TmTime;vm.runInContext(source[0],box);
function context(){return {...box.d.methods,user:{id:1},authGeneration:1,canCreateOrders:true,orderHistoryOpening:null,
 modal:{type:'orderDetail'},orderForm:{customer_po:'OLD',_create_key:'OLD'},customerOptions:[{id:9,is_active:true}],
 hasPermission:()=>true,loadCustomerOptions:async()=>true,rememberModalOpener(){},focusAccessibleModal(){},
 $nextTick(fn){fn()},resetOrderReminderState(){},resetOrderRequisitionPreview(){},newOrderInventoryState(){return {}},
 refreshOrderNumberPreview(){},handleOrderCustomerChange:async()=>{},
 closeModal(){this.modal=null},showToast(){},errorMessage:e=>e.message,
 loadOrderCommonBoxes:async function(page){this.loaded={page,source:this.orderCommonBoxPicker.source_order_id,customer:this.orderForm.customer_id}},
}}
function resolve(request){request.resolve({data:{id:12,customer_id:9,order_number:'HISTORY',customer_po:'OLD-PO',delivery_date:'2000-01-01',
 remark:'OLD NOTE',items:[{id:1,product_id:10,quantity:99,unit_price:88},{id:2,product_id:10,quantity:100,unit_price:77}]}})}
(async()=>{
 let c=context();const first=c.openOrderFromHistory(12);assert.equal(await c.openOrderFromHistory(12),false);
 assert.equal(pending.length,1);resolve(pending.shift());assert.equal(await first,true);
 assert.equal(c.orderForm.customer_id,9);assert.equal(c.orderForm.customer_po,'');assert.equal(c.orderForm.remark,'');
 assert.equal(c.orderForm._create_key,undefined);assert.notEqual(c.orderForm.delivery_date,'2000-01-01');
 assert.equal(c.orderForm.items.length,1);assert.equal(c.orderForm.items[0].product_id,null);
 assert.equal(c.orderForm.items[0].quantity,null);assert.equal(c.orderForm.items[0].unit_price,'');
 assert.equal(c.orderCommonBoxPicker.source_product_total,1);assert.equal(c.loaded.source,12);
 assert.equal(Object.keys(c.orderCommonBoxPicker.selected).length,0,'history must not auto-select old quantities');
 await c.showAllOrderCommonBoxes();assert.equal(c.loaded.source,null);
 c=context();const stale=c.openOrderFromHistory(12);const old=pending.shift();c.authGeneration++;c.user={id:2};resolve(old);
 assert.equal(await stale,false);assert.equal(c.orderForm.customer_po,'OLD');
 c=context();const cancelled=c.openOrderFromHistory(12);const closed=pending.shift();c.modal=null;resolve(closed);
 assert.equal(await cancelled,false);
 c=context();const failed=c.openOrderFromHistory(12);pending.shift().reject(Error('403'));
 assert.equal(await failed,false);assert.equal(c.modal.type,'orderDetail');assert.equal(c.orderHistoryOpening,null);
 c=context();c.canCreateOrders=false;assert.equal(await c.openOrderFromHistory(12),false);assert.equal(pending.length,0);
 assert.ok(html.includes('@click="openOrderFromHistory(orderDetail.id)"'));
 console.log('PASS history read-only source, blank transaction, no auto-selection, customer, duplicate, stale session, close, failure and permission');
})().catch(e=>{console.error(e);process.exitCode=1});
