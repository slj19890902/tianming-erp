const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = fs.readFileSync('static/index.html', 'utf8');
function fixture() {
  const controllers = new Map();
  const requests = [];
  const axios = {get(url, config) {return new Promise((resolve,reject)=>requests.push({url,config,resolve,reject}));}};
  const names = ['loadOverview','loadWarehouseCapacitySummary','loadKpi'];
  const methods = names.map(name => {
    const start = html.indexOf(`          async ${name}()`);
    const end = html.indexOf('\n          },',start)+12;
    return html.slice(start,end);
  }).join(',');
  const vm = Object.assign({authGeneration:1,user:{id:1},kpi:{},
    hasPermission:()=>true,loadDeliveryBacklogs(){},syncDesktopDeliveryMargin(){},$nextTick:fn=>fn(),
    errorMessage:e=>e.message,isCancelledRequest:e=>e.code==='ERR_CANCELED',
    beginLatestRequest(key){controllers.get(key)?.abort();const c=new AbortController();controllers.set(key,c);return c;},
    finishLatestRequest(key,c){if(controllers.get(key)===c)controllers.delete(key);}
  },Function('axios','latestRequestControllers',`return ({${methods}})`)(axios,controllers));
  return {vm,requests};
}
test('main dashboard completes even while optional capacity is pending', async()=>{
 const {vm,requests}=fixture(); const p=vm.loadOverview();
 assert.equal(requests[0].config.timeout,20000);
 requests[0].resolve({data:{cards:[{count:7}],kpi:{orders:7}}});
 assert.equal(await p,true);assert.equal(vm.overviewLoading,false);assert.equal(vm.kpi.orders,7);
 assert.equal(requests[1].url,'/api/warehouse/capacity/summary');assert.equal(requests[1].config.timeout,10000);
 requests[1].reject(new Error('capacity down'));await new Promise(setImmediate);
 assert.equal(vm.overview.cards[0].count,7);assert.equal(vm.overviewError,'');
});
test('timeout exits loading with an actionable error',async()=>{
 const {vm,requests}=fixture();const p=vm.loadOverview();requests[0].reject({code:'ECONNABORTED'});
 assert.equal(await p,false);assert.equal(vm.overviewLoading,false);assert.match(vm.overviewError,/重新加载/);
});
test('older response cannot overwrite newer dashboard or clear its loading state',async()=>{
 const {vm,requests}=fixture();const old=vm.loadOverview();const current=vm.loadOverview();
 assert.equal(requests[0].config.signal.aborted,true);
 requests[0].resolve({data:{kpi:{orders:1}}});assert.equal(await old,false);assert.equal(vm.overviewLoading,true);
 requests[1].resolve({data:{kpi:{orders:2}}});assert.equal(await current,true);assert.equal(vm.kpi.orders,2);
 requests[2].resolve({data:{lots:3}});await new Promise(setImmediate);
});
test('session change rejects overview and optional capacity results',async()=>{
 const {vm,requests}=fixture();const old=vm.loadOverview();vm.authGeneration++;
 requests[0].resolve({data:{kpi:{orders:99}}});assert.equal(await old,false);assert.deepEqual(vm.kpi,{});
 const capacity=vm.loadWarehouseCapacitySummary();vm.user={id:2};requests[1].resolve({data:{secret:1}});await capacity;
 assert.equal(vm.warehouseCapacitySummary,null);
});
