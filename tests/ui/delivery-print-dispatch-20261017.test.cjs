const {test}=require('node:test'), assert=require('node:assert/strict');
const fs=require('fs'),vm=require('vm'),path=require('path');
const root=path.resolve(__dirname,'../..');
const fixture=JSON.parse(fs.readFileSync(path.join(root,'tests/fixtures/delivery-dispatch-recovery/normal.json')));
function storage(){const m=new Map();return {get length(){return m.size},key:i=>[...m.keys()][i]??null,getItem:k=>m.get(k)??null,setItem:(k,v)=>m.set(k,String(v)),removeItem:k=>m.delete(k)}}
function setup({lost=false,dispatched=false}={}) {
 const calls=[],values=storage();let first=true;
 const context={console,localStorage:values,TmOperationKey:{create:()=>fixture.originalBody.idempotency_key}};
 context.fetch=async(url,options)=>{
  calls.push({url,options});let result;
  if(url.endsWith('/dispatch-snapshot'))result=fixture.snapshot.body;
  else if(url.endsWith('/resolve'))result={...fixture.resolve.body,current:{...fixture.resolve.body.current,status:'dispatched',version:fixture.write.body.dispatch_receipt.completed_version}};
  else if(url===`/api/deliveries/${fixture.originalBody.snapshot.delivery.id}`) result={status:'dispatched'};
  else if(url.endsWith('/dispatch')){
   assert.deepEqual(JSON.parse(options.body),fixture.originalBody);
   if(lost&&first){first=false;throw new TypeError('response lost after commit')}
   result=fixture.write.body;
  }else throw new Error('unexpected '+url);
  return {ok:true,json:async()=>JSON.parse(JSON.stringify(result))};
 };
 vm.createContext(context);
 for(const f of ['delivery-dispatch-recovery.js','delivery-print-dispatch.js'])vm.runInContext(fs.readFileSync(path.join(root,'static/ui',f),'utf8'),context);
 return {context,calls,data:{id:fixture.originalBody.snapshot.delivery.id,current_actor_id:fixture.actorId,
   delivery_version:fixture.originalBody.expected_version,status:dispatched?'dispatched':'pending'}};
}
test('preview never sends a mutation; confirm coalesces clicks and reprint never dispatches',async()=>{
 const h=setup(),a=h.context.TmDeliveryPrintDispatch;
 await a.prepare(h.data);assert.equal(h.calls.length,1);assert.equal(h.calls[0].options.method,'GET');
 await Promise.all([a.confirm(h.data),a.confirm(h.data)]);
 await a.confirm(h.data);
 assert.equal(h.calls.filter(c=>c.url.endsWith('/dispatch')).length,1);
});
test('lost committed response resolves original key without a new inventory write',async()=>{
 const h=setup({lost:true}),a=h.context.TmDeliveryPrintDispatch;
 await a.prepare(h.data);await assert.rejects(a.confirm(h.data),/response lost/);
 await a.prepare({...h.data,status:'dispatched'});await a.confirm(h.data);
 assert.equal(h.calls.filter(c=>c.url.endsWith('/dispatch')).length,1);
 assert.equal(h.calls.filter(c=>c.url.endsWith('/resolve')).length,1);
});
test('an already dispatched delivery opens for reprint without any command',async()=>{
 const h=setup({dispatched:true});await h.context.TmDeliveryPrintDispatch.prepare(h.data);
 await h.context.TmDeliveryPrintDispatch.confirm(h.data);assert.equal(h.calls.length,1);assert.equal(h.calls[0].options.method,'GET');
});
test('Vue order import preserves decimal price; amount uses exact cents; stock edits conserve allocations',()=>{
 const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
 function method(name){const start=new RegExp('^          (?:async )?'+name+'\\([^\\n]*\\) \\{','m').exec(html);assert.ok(start,name);
 const end=/^          \},?$/m.exec(html.slice(start.index));return html.slice(start.index,start.index+end.index+end[0].length).trim().replace(/,$/,'')}
 const names=['createDeliveryLine','applyDeliveryCandidate','deliveryDefaultQuantity','deliveryLineAmount','deliveryFormAmount','onDeliveryQuantityChanged','unorderedFinishedPhysicalQuantity'];
 const c=vm.runInNewContext('({'+names.map(method).join(',')+'})');
 c.deliveryForm={lines:[]};c.deliveryKitSummary=()=>'';c.deliveryDefaultQuantity=()=>3;
 const line={key:'line',remarks:''};c.deliveryForm.lines=[line];c.applyDeliveryCandidate(line,{order_item_id:12,unit_price:'2.146420'});
 assert.equal(line.unit_price,'2.146420');line.delivered_quantity=3;assert.equal(c.deliveryLineAmount(line),'6.439');
 line.unit_price=null;assert.equal(c.deliveryLineAmount(line),'');
 const stock={source_type:'unordered_finished',delivered_quantity:9,quantity_contract:{physical_basis:2,customer_basis:1},
 allocations:[{inventory_lot_id:1,quantity:10},{inventory_lot_id:2,quantity:10}],stock_locations:[{inventory_lot_id:1,available_quantity:10},{inventory_lot_id:2,available_quantity:10}]};
 c.onDeliveryQuantityChanged(stock);assert.equal(stock.allocations.reduce((s,a)=>s+a.quantity,0),18);assert.equal(stock.allocation_quantity_error,false);
});
test('selected delivery template has drawing and amount columns without internal technical notes',()=>{
 const fixture=require('./helpers/delivery-editor-fixture.cjs');
 assert.match(fixture.template,/deliveryEditorPageLines/);
 assert.match(fixture.template,/<th>图号<\/th>/);
 assert.match(fixture.template,/<th v-if="canViewSalesAmounts">金额<\/th>/);
 assert.ok(!fixture.template.includes('先分配同款'));
 assert.ok(!fixture.template.includes('ERP TM'));
});

test('completed original request cannot print as a new dispatch after cancellation',async()=>{
 const h=setup({lost:true}),a=h.context.TmDeliveryPrintDispatch;
 await a.prepare(h.data);await assert.rejects(a.confirm(h.data));
 const original=h.context.fetch;
 h.context.fetch=async(url,options)=>{
  if(url.endsWith('/resolve'))return {ok:true,json:async()=>({...fixture.resolve.body,current:{...fixture.resolve.body.current,status:'pending'}})};
  return original(url,options);
 };
 await assert.rejects(a.confirm(h.data),/撤销/);
 assert.equal(h.calls.filter(c=>c.url.endsWith('/dispatch')).length,1);
});
