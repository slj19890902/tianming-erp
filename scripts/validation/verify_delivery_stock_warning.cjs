const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
function method(name, next, env) {
  const start = html.indexOf('          async ' + name + '(');
  const end = html.indexOf('          async ' + next + '(', start);
  assert(start > 0 && end > start);
  return vm.runInNewContext('({' + html.slice(start, end) + '})', env)[name];
}
(async () => {
  let accepted = false, posted = [], alerts = [], toasts = [];
  let rows = [{id:7, product_code:'EXT', available_quantity:0, warning_quantity:10,
    suggested_physical_quantity:1, physical_unit:'片'},
    {id:8, product_code:'BOX', available_quantity:0, warning_quantity:10,
      plan_hash:'a'.repeat(64), proposed_items:[{quantity:50, target_inventory_type:'semi_finished'}]}];
  const env = {confirm: () => accepted, alert: x => alerts.push(x), axios:{
    get:async () => ({data:{items:rows}}),
    post:async (url, body) => { posted.push({url,body}); return {data:{orders:[{order_number:'SW-1'}]}}; },
  }};
  const ctx = {hasPermission:()=>true, errorMessage:e=>e.message,
    showToast:(...args)=>toasts.push(args)};
  const warn = method('showDeliveryStockWarnings', 'dispatchDelivery', env);
  await warn.call(ctx, 11);
  assert.equal(posted.length, 0, 'Cancel must not create anything');
  accepted = true;
  await warn.call(ctx, 11);
  assert.deepEqual(JSON.parse(JSON.stringify(posted[0].body)), {items:[
    {policy_id:7,physical_quantity:1}, {policy_id:8,plan_hash:'a'.repeat(64)}]});
  rows = [{id:7,available_quantity:0,warning_quantity:10,suggested_physical_quantity:0}];
  await warn.call(ctx, 11);
  assert.equal(posted.length, 1);
  assert.equal(alerts.length, 1);
  env.axios.get = async () => {throw Error('offline')};
  await warn.call(ctx, 11);
  assert(toasts.at(-1)[0].includes('发货已完成'));

  for (const failure of ['printed', 'dispatch']) {
    let reminded = 0;
    const dispatch = method('dispatchDelivery', 'printDelivery', {confirm:()=>true, axios:{
      post:async()=>({}), put:async url=>{if(url.endsWith('/'+failure)) throw Error(failure);},
    }});
    const state = {deliveryOperationState:{action:''}, receiptOperationState:{action:''},
      normalizeDeliveryPickTask:()=>null,
      openDeliveryPrintTab:async()=>({activate(){},abort(){}}), invalidateDeliveryListDetail(){},
      loadDeliveries:async()=>{},loadOrders:async()=>{},loadKpi:async()=>{},
      showToast(){},errorMessage:e=>e.message,showDeliveryStockWarnings:async()=>{reminded++;}};
    await dispatch.call(state,{id:11,delivery_number:'D-11'});
    assert.equal(reminded, failure === 'printed' ? 1 : 0);
    assert.equal(state.deliveryOperationState.action,'');
  }
  let buyAccepted = false, buys = 0, previews = 0, reloads = 0;
  const buy = method('confirmStockExternalDraft', 'showDeliveryStockWarnings', {
    confirm:()=>buyAccepted, axios:{
      get:async()=>{previews++;return {data:{expected_request_hash:'r',expected_quote_hash:'q',
        product_name:'外购品',supplier_name:'供应商',quantity:1,unit:'片',unit_price:'8.5',currency:'CNY',tax_rate:'0.13'}};},
      post:async(url,body)=>{assert.equal(body.expected_request_hash,'r');assert.equal(body.expected_quote_hash,'q');buys++;},
    }});
  const buyer = {canAdmin:true,canViewCosts:true,canRequisition:true,
    loadRequisition:async()=>{reloads++;},showToast(){},errorMessage:e=>e.message};
  const row = {source_id:3};
  await buy.call(buyer,row);
  assert.equal(buys,0);assert.equal(row._externalBuying,false);
  buyAccepted = true;
  await buy.call(buyer,row);
  assert.equal(buys,1);assert.equal(reloads,1);assert.equal(row._externalBuying,false);
  buyer.canAdmin = false;
  await buy.call(buyer,row);
  assert.equal(previews,2);assert.equal(buys,1);
  console.log('PASS: cancel, mixed demand, covered demand, reminder error, dispatch success with print failure, dispatch failure');
  console.log('PASS: purchase preview cancellation, reviewed hashes, one purchase, refresh, permission gate');
})().catch(error=>{console.error(error);process.exitCode=1;});
