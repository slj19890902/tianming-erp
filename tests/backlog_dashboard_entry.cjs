// Real entry and candidate methods; fake transport only, no ERP or browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
function method(start, next) {
  const code = source.slice(source.indexOf(start), source.indexOf(next, source.indexOf(start))).trim().replace(/,$/, '');
  return eval('(' + code.replace(/^async (\w+)\(/, 'async function $1(').replace(/^(\w+)\(/, 'function $1(') + ')');
}
global.today = () => '2026-09-30';
let scenario = 'success';
global.axios = {get: async url => {
  assert.equal(url, '/api/deliveries/pending-customer-options');
  if(scenario==='failure')throw new Error('delivery candidates unavailable');
  return {data: {items: [{customer_id:7,customer_name:'Synthetic',customer_code:'SYN',has_pending_orders:true,pending_item_count:1}]}};
}};
const candidateOptions = method('deliveryCustomerOptions() {', 'pendingDeliveryCustomerGroups() {');
function state() {
  const vm = {
    customerOptions: [], deliveryCustomerCandidates: [], deliveryBacklogs:{loading:false,preview:null},
    deliveryForm:{}, user:{id:1}, loadedBatch:0,
    resetDeliveryReminderState(){}, deliveryFormSignature(){return JSON.stringify(this.deliveryForm.lines);},
    loadDeliveryRemindersForCustomer:async()=>{}, async loadDeliveryBatchItems(){this.loadedBatch++;},
    async loadCustomerOptions(){
      throw new Error('delivery-only user has no customer master permission');
    },
    errorMessage:error=>error.message,
    createDeliveryLine:row=>({...row,key:'line-'+row.order_item_id}),
    showToast(message){this.toast=message;},
  };
  Object.defineProperty(vm,'deliveryCustomerOptions',{get(){return candidateOptions.call(this);}});
  vm.loadDeliveryCustomerOptions=method('async loadDeliveryCustomerOptions() {','resetDeliveryListFilters() {').bind(vm);
  vm.openDelivery=method('async openDelivery(preferredCustomerId = null) {','async openDeliveryRoutePlan() {').bind(vm);
  vm.applyDeliveryBacklogs=method('async applyDeliveryBacklogs() {','openTianhuaPreimport() {').bind(vm);
  return vm;
}
(async()=>{
  const vm=state();
  vm.modal={type:'deliveryBacklogs'};
  vm.deliveryBacklogs.preview={customer_id:7,requested_quantity:1000,items:[{
    order_item_id:31,ready_quantity:1000,customer_quantity_step:2,take:400,
    stock_code:'TEST',candidate:{order_item_id:31,product_id:4},
  }]};
  await vm.applyDeliveryBacklogs();
  assert.equal(vm.deliveryForm.customer_id,7,'cold dashboard entry must load authorized customer options');
  assert.equal(vm.customerOptions.length,0,'delivery-only permission must not load the customer master');
  assert.match(vm.deliveryCustomerOptions[0]._delivery_label,/SYN/);
  assert.equal(vm.loadedBatch,1);
  assert.equal(vm.deliveryForm.lines.length,1);
  assert.equal(vm.deliveryForm.lines[0].delivered_quantity,400);
  assert.equal(vm.deliveryBacklogs.preview,null);
  scenario='failure';
  const failed=state();await failed.openDelivery(7);
  assert.equal(failed.deliveryForm.customer_id,null);
  assert.deepEqual(failed.deliveryForm.lines,[]);
  assert.match(failed.deliveryCandidateError,/delivery candidates unavailable/);
  console.log('cold dashboard backlog entry and option-load failure verified');
})().catch(error=>{console.error(error);process.exitCode=1;});
