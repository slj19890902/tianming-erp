const {test}=require('node:test');
const assert=require('node:assert/strict');
const {matches,pageRows,stockState,stageCards,mixin}=require('../../static/ui/home-workbench.js');
test('filter matches exact customer including merged group and searches business identifiers',()=>{
 const row={customer_id:null,customer_ids:[2,3],product_code:'80011946',customer_po:'PO-009'};
 assert(matches(row,2,'11946'));assert(matches(row,3,'po-009'));assert(!matches(row,4,''));
});
test('pending approval, preparation, incoming and missing data are different stock actions',()=>{
 assert.equal(stockState({pending_request_ids:[1],suggested_new_requisition_sheet_quantity:20,draft_ready:true}),'approval');
 assert.equal(stockState({replenishment_state:'board_preparation_ready'}),'prepare');
 assert.equal(stockState({replenishment_state:'already_ordered'}),'incoming');
 assert.equal(stockState({suggested_new_requisition_sheet_quantity:30,draft_ready:false}),'missing');
 assert.equal(stockState({procurement_mode:'external_purchase',suggested_new_requisition_finished_quantity:20,draft_ready:true}),'new');
});
test('pagination stays bounded after filtering; quantities and customer counts do not duplicate',()=>{
 assert.deepEqual(pageRows([1,2],9,4),{rows:[1,2],page:1,pages:1,total:2});
 const cards=[{key:'pending_delivery',count:4,count_unit:'客户'}];
 assert.equal(stageCards(cards,[{key:'pending_delivery',id:1,customer_id:3},{key:'pending_delivery',id:2,customer_id:3}],true)[0].count,1);
 assert.equal(stageCards(cards,[],false)[0].count,4);
});
test('rapid task clicks run one navigation while the first is pending',async()=>{
 let done,calls=0;const hold=new Promise(r=>done=r);
 const vm={homeActionBusy:false,overviewError:'',pages:{},invalidatePageCache(){},async go(){calls++;await hold;}};
 const action=mixin.methods.homeOpenTask.bind(vm),row={key:'pending_material',target:'requisition',source_item_id:'mg3'};
 const first=action(row);await action(row);assert.equal(calls,1);assert.equal(vm.homeActionBusy,true);
 done();await first;assert.equal(vm.homeActionBusy,false);assert.equal(vm.homeEntry.source_item_id,'mg3');
});
test('clearing homepage position also clears production search and reloads',async()=>{
 const vm={homeEntry:{target:'production'},activePage:'production',productionQuery:'PO-9',stockPrepQuery:'8001',invalidatePageCache(){},async loadPage(p){assert.equal(p,'production');}};
 await mixin.methods.homeClearEntry.call(vm);assert.equal(vm.homeEntry,null);assert.equal(vm.productionQuery,'');assert.equal(vm.stockPrepQuery,'');
});

const {customerGroups,fairTasks,attentionCategory}=require('../../static/ui/home-workbench.js');
test('one hundred products occupy one customer row; a small customer stays visible',()=>{
 const rows=Array.from({length:100},(_,i)=>({customer_id:1,customer_label:'A',policy_id:i,suggested_new_requisition_sheet_quantity:1,draft_ready:true}));
 rows.push({customer_id:2,customer_label:'B',suggested_new_requisition_sheet_quantity:1,draft_ready:true});
 const groups=customerGroups(rows);assert.equal(groups.length,2);assert.equal(groups[0].action,100);assert.equal(groups[1].action,1);
});
test('customer rotation does not put normal work ahead of an urgent order',()=>{
 const rows=[{id:1,customer_id:1,urgency:'overdue'},{id:2,customer_id:1,urgency:'overdue'},{id:3,customer_id:2,urgency:'overdue'},{id:4,customer_id:3,urgency:'normal'}];
 assert.deepEqual(fairTasks(rows).map(r=>r.id),[1,3,2,4]);assert.equal(attentionCategory({attention_state:'cancel_review'}),'review');
});
test('hidden payment contact does not reduce totals; shared settlement count is unique',()=>{
 const tasks=[{key:'pending_payment',id:1,customer_id:1,settlement_identity:'entity:1',amount:'100',attention_state:'hidden'},{key:'pending_payment',id:2,customer_id:2,settlement_identity:'entity:1',amount:'50'}];
 const cards=stageCards([{key:'pending_payment',count_unit:'客户',amount:'150'}],tasks,true);assert.equal(cards[0].count,1);assert.equal(cards[0].amount,150);
});
test('reminder retry keeps idempotency key; new payload creates a new one',async()=>{
 const payloads=[];global.axios={async post(url,payload){payloads.push(payload);throw {response:{data:{detail:'failed'}}};}};
 const vm={homePreferenceBusy:false,overviewError:'',homePreferenceForm:{reason:'later',scope:'personal',remind_on:'2026-10-12'},homeData:{attention_reasons:[{code:'later',state:'snoozed'}]},user:{id:1},authGeneration:1};
 const row={id:'task:1',source_hash:'a'.repeat(64),attention_versions:{personal:0}};
 await mixin.methods.homeSaveReminder.call(vm,row);await mixin.methods.homeSaveReminder.call(vm,row);
 assert.equal(payloads[0].idempotency_key,payloads[1].idempotency_key);assert.equal(vm.homePreferenceError,'failed');
 vm.homePreferenceForm.remind_on='2026-10-13';await mixin.methods.homeSaveReminder.call(vm,row);assert.notEqual(payloads[1].idempotency_key,payloads[2].idempotency_key);
 delete global.axios;
});
