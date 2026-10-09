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
