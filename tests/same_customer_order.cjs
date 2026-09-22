const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert/strict');
const root = process.argv[2];
const html = fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const source = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(x=>x[1]).filter(x=>x.trim());
assert.equal(source.length,1);
let posts = 0, post = async () => ({data:{id:9,order_number:'SYNTHETIC-9'}});
const box = {
  axios:{defaults:{},interceptors:{response:{use(){}}},post(...args){ posts++; return post(...args); }},
  Vue:{createApp(definition){box.definition=definition;return {component(){return this},mount(){return this}}}},
  window:{},document:{},localStorage:{getItem(){return ''},setItem(){},removeItem(){}},
  TMOrderReference:{component:{}},URLSearchParams,setTimeout,clearTimeout,console,
};
vm.createContext(box);
vm.runInContext(fs.readFileSync(path.join(root,'static/assets/time-utils.js'),'utf8'),box);
box.TmTime=box.window.TmTime || box.TmTime;
vm.runInContext(source[0],box);
const methods=box.definition.methods;
function context(){
  const c={...methods,user:{id:1},authGeneration:1,canCreateOrders:true,
    customerOptions:[{id:77,is_active:true}],hasPermission:()=>true,orderForm:{_create_key:'old-key'},
    orderCreateSaveState:{},orderNextStepGuide:{visible:false},modal:{type:''},
    loadCustomerOptions:async()=>true,rememberModalOpener(){},focusAccessibleModal(){},
    $nextTick(fn){fn()},resetOrderReminderState(){},resetOrderRequisitionPreview(){},
    newOrderInventoryState(){return {}},refreshOrderNumberPreview(){},searchOrderProducts:async()=>{},
    handleOrderCustomerChange:async function(){this.customerInitialized=this.orderForm.customer_id},
    prepareMoldRepairConfirmation:async()=>({confirmed:true}),
    closeModal(){this.modal={type:''}},loadOrders:async()=>{},loadKpi:async()=>{},
    showToast(){},errorMessage:e=>e.message,
  };
  Object.defineProperty(c,'canContinueSameCustomerOrder',{get(){return box.definition.computed.canContinueSameCustomerOrder.call(c)}});
  return c;
}
const payload=()=>({customer_id:77,customer_po:'OLD-PO',remark:'OLD',items:[{product_id:88,quantity:99,unit_price:7,client_line_id:'OLD-LINE'}]});
(async()=>{
  let c=context(); await c.saveNewOrder(payload());
  assert.equal(c.canContinueSameCustomerOrder,true,'successful manual save must allow same-customer continuation');
  await c.continueSameCustomerOrder();
  assert.equal(c.orderForm.customer_id,77);
  assert.equal(c.customerInitialized,77);
  assert.equal(c.orderForm.customer_po,''); assert.equal(c.orderForm.remark,'');
  assert.equal(c.orderForm._create_key,undefined);
  assert.equal(c.orderForm.items.length,1);
  const line=c.orderForm.items[0];
  assert.equal(line.quantity,null);assert.equal(line.unit_price,'');assert.equal(line.product_id,null);
  assert.equal(line.temp_drawing_token,null);assert.notEqual(line.client_line_id,'OLD-LINE');
  assert.equal(posts,1,'continuation must not submit');
  c.closeModal();await c.openOrder(); assert.equal(c.orderForm.customer_id,null,'generic new must remain blank');
  c.showOrderNextStepGuide({source:'pdf'});assert.equal(c.canContinueSameCustomerOrder,false);
  c=context();await c.saveNewOrder(payload());c.authGeneration++;assert.equal(c.canContinueSameCustomerOrder,false);
  const old=c.orderForm;await c.continueSameCustomerOrder();assert.equal(c.orderForm,old);
  c=context();await c.saveNewOrder(payload());c.canCreateOrders=false;assert.equal(c.canContinueSameCustomerOrder,false);
  c=context();await c.saveNewOrder(payload());c.loadCustomerOptions=async()=>{c.customerOptions=[];return true};
  await c.continueSameCustomerOrder();assert.notEqual(c.orderForm.customer_id,77,'unavailable customer cannot be carried');
  c=context();await c.saveNewOrder(payload());let release;
  c.loadCustomerOptions=()=>new Promise(r=>{release=r});
  const first=c.continueSameCustomerOrder();await c.continueSameCustomerOrder();
  c.authGeneration++;c.user={id:2};release(true);await first;
  assert.notEqual(c.orderForm.customer_id,77,'account change during lookup must abort');
  c=context();post=async()=>{throw new Error('lost response')};
  await assert.rejects(()=>c.saveNewOrder(payload()));
  assert.equal(c.orderCreateSaveState.outcomeUncertain,true);assert.equal(c.canContinueSameCustomerOrder,false);
  assert.equal(c.orderForm._create_key,'old-key','uncertain retry retains original key');
  c=context();post=async()=>{c.authGeneration++;c.user={id:2};return {data:{id:9}}};
  await c.saveNewOrder(payload());assert.equal(c.canContinueSameCustomerOrder,false,'late save response must not create next-user context');
  c=context();post=async()=>({data:{id:10}});
  await c.saveNewOrder(payload());c.customerOptions[0].is_active=false;
  await c.continueSameCustomerOrder();assert.notEqual(c.orderForm.customer_id,77,'inactive customer cannot be carried');
  c=context();await c.saveNewOrder(payload());
  c.loadCustomerOptions=async()=>{throw new Error('lookup unavailable')};
  await c.continueSameCustomerOrder();assert.notEqual(c.orderForm.customer_id,77,'lookup failure cannot use cached customer');
  // Real customer initializer and async helpers, with synthetic API responses only.
  c=context();c.handleOrderCustomerChange=methods.handleOrderCustomerChange;
  c.refreshOrderNumberPreview=methods.refreshOrderNumberPreview;
  c.searchOrderProducts=methods.searchOrderProducts;c.invalidateLineInventory=()=>{};
  c.loadOrderRemindersForCustomer=async id=>{c.reminderCustomer=id};
  box.axios.get=async url=>({data:url.includes('number-preview')?{order_number:'PREVIEW'}:{items:[]}});
  await c.saveNewOrder(payload());await c.continueSameCustomerOrder();
  assert.equal(c.reminderCustomer,77);assert.equal(c.orderNumberPreview,'PREVIEW');
  assert.equal(c.orderForm.items[0].quantity,null);
  for(const method of ['searchOrderProducts','refreshOrderNumberPreview','loadCustomerQuotePreferences']){
    c=context();c.orderForm={customer_id:77,items:[{}]};c.orderProductOptions={};
    c.customerQuotePreferences=[];c.orderNumberPreview='';let resolve;
    box.axios.get=()=>new Promise(r=>{resolve=r});
    const request=methods[method].call(c,method==='searchOrderProducts'?0:77,'');
    c.authGeneration++;c.orderForm={customer_id:88,items:[{}]};
    resolve({data:{items:[{id:999}],order_number:'OLD-PREVIEW'}});await request;
    assert.equal(Object.keys(c.orderProductOptions).length,0,method);
    assert.equal(c.customerQuotePreferences.length,0,method);assert.equal(c.orderNumberPreview,'',method);
  }
  const compileBox={console};vm.createContext(compileBox);
  vm.runInContext(fs.readFileSync(path.join(root,'static/vendor/vue-3.5.40.global.prod.js'),'utf8'),compileBox);
  const errors=[];
  const template=html.slice(html.indexOf('<section v-if="orderNextStepGuide.visible')).split('</section>')[0]+'</section>';
  compileBox.Vue.compile(template,{decodeEntities:value=>value,onError:e=>errors.push(String(e))});assert.deepEqual(errors,[]);
  assert.ok(html.includes('@click="continueSameCustomerOrder"'));
  console.log('PASS: same customer, fresh transaction, generic/PDF, permission, missing customer, double click, account race, uncertain save and stale response');
})().catch(e=>{console.error(e);process.exitCode=1});
