const assert=require('node:assert/strict'), {test}=require('node:test');
const api=require('../../static/ui/manual-replenishment-entry.js');
function harness(overrides={}) {
  const calls=[],messages=[],history={state:{},replaceState(...args){calls.push(['history',args[2]]);}};
  const vm={user:{id:1},authGeneration:1,pageLoadSequence:1,activePage:'requisition',canRequisition:true,modal:null,
    showToast(...args){messages.push(args);},errorMessage:e=>e.message,
    async openStockReplenishment({productAction}) {
      this.stockReplenishmentForm={source_type:'customer_request',stock_now:false,items:[]};
      this.modal={type:'stockReplenishment'};productAction.bind(this.stockReplenishmentForm,this.modal);
    },addBlankStockReplenishmentLine(){this.stockReplenishmentForm.items.push({quantity:null});},
    applyStockProduct(line){Object.assign(line,this.stockReplenishmentProducts.find(p=>p.id===line.reference_product_id));},...overrides};
  const http={async get(url){calls.push(['get',url]);return {data:{items:[{id:1,customer_id:7,product_code:'SAME'},{id:2,customer_id:7,product_code:'SAME'}]}};}};
  const location={href:'http://localhost'+api.link(7,[1,2,1])};
  return {vm,calls,messages,http,history,location,run(){return api.openFromLocation(vm,location,history,http);}};
}
test('real product identity deduplicates rows, retains different products with identical codes, excludes unmatched',()=>{
 const lines=[{product_id:1,quantity:999},{matched_product_id:1},{product_id:2,product_code:'SAME'},
 {matched_product_id:3,is_new_product:true},{product_id:4,manual_size_entry:true},{}];
 const before=JSON.stringify(lines);assert.deepEqual(api.products(lines).map(r=>r.id),[1,2]);assert.equal(JSON.stringify(lines),before);
 assert.equal(api.link(7,Array.from({length:101},(_,i)=>i+1)),'');assert.equal(api.link(0,[1]),'');
});
test('non-warning multiple products open existing manual draft with blank quantities and no POST',async()=>{
 const h=harness();await h.run();assert.deepEqual(h.vm.stockReplenishmentForm.items.map(x=>x.reference_product_id),[1,2]);
 assert.deepEqual(h.vm.stockReplenishmentForm.items.map(x=>x.quantity),[null,null]);
 assert.equal(h.vm.stockReplenishmentForm.source_type,'customer_request');assert.equal(h.vm.stockReplenishmentForm.stock_now,false);
 assert.equal(h.calls.filter(x=>x[0]==='get').length,1);assert.ok(!h.calls[0][1].includes('manual_replenishment_products'));
});
test('missing or wrong-customer product rejects the whole selection',async()=>{
 for(const items of [[{id:1,customer_id:7}],[{id:1,customer_id:7},{id:2,customer_id:8}]]) {
  const h=harness();h.http.get=async()=>({data:{items}});await h.run();assert.equal(h.vm.modal,null);assert.equal(h.messages.at(-1)[1],true);
 }
});
test('permissions, malformed IDs and forced password never load or write',async()=>{
 for(const override of [{canRequisition:false},{activePage:'orders'},{user:{id:1,must_change_password:true}}]) {
  const h=harness(override);await h.run();assert.equal(h.calls.filter(x=>x[0]==='get').length,0);
 }
 const h=harness();h.location.href+='x';await h.run();assert.equal(h.vm.modal,null);assert.equal(h.calls.filter(x=>x[0]==='get').length,0);
});
test('account, navigation and modal races cannot replace another form',async()=>{
 for(const mutate of [vm=>vm.authGeneration++,vm=>vm.user={id:2},vm=>vm.activePage='orders',vm=>vm.pageLoadSequence++,vm=>vm.modal={type:'order'}]) {
  const h=harness();const original=h.http.get;h.http.get=async url=>{const result=await original(url);mutate(h.vm);return result;};
  await h.run();assert.equal(h.vm.stockReplenishmentForm,undefined);
 }
});
test('closing during auxiliary loading does not append rows or toast late errors',async()=>{
 const h=harness({async openStockReplenishment({productAction}) {
  this.stockReplenishmentForm={items:[]};this.modal={type:'stockReplenishment'};productAction.bind(this.stockReplenishmentForm,this.modal);
  this.modal=null;throw Error('late');
 }});await h.run();assert.deepEqual(h.vm.stockReplenishmentForm.items,[]);assert.deepEqual(h.messages,[]);
});
test('startup reads once and bootstrap failure cannot leave a partial editable batch',async()=>{
 const h=harness({async openStockReplenishment({productAction}) {
  this.stockReplenishmentForm={items:[]};this.modal={type:'stockReplenishment'};productAction.bind(this.stockReplenishmentForm,this.modal);throw Error('offline');
 }});await h.run();assert.equal(h.vm.modal,null);assert.match(h.messages[0][0],/offline/);
});
module.exports={harness};
