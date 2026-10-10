const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mixin,template}=require('../../static/ui/home-workbench.js');

function view(tasks, options={}) {
  const vm={homeData:{tasks,today:'2026-10-10'},homeCustomer:'',homeQuery:'',homeWorkspace:'finance',homeTaskMode:'all',homeAttentionTab:'active',homeWorkCustomer:null,homeTaskPage:1,homePageSize:4,...options};
  for(const [key,fn] of Object.entries(mixin.computed)) {
    if(!(key in vm))Object.defineProperty(vm,key,{get(){return fn.call(vm);},configurable:true});
  }
  return vm;
}
function task(id,cid,key='pending_payment',extra={}) {
  return {id,customer_id:cid,customer_ids:[cid],customer_label:`客户${cid}`,key,action_text:'查看收款',...extra};
}

test('attention badges use the selected finance customer instead of all homepage work',()=>{
  const rows=[task('pay1',1),task('pay2',2),...Array.from({length:63},(_,i)=>task(`deliver${i}`,3,'pending_delivery'))];
  const vm=view(rows);
  assert.equal(vm.homeSelectedCustomer,1);
  assert.equal(vm.homeTasksPage.total,1);
  assert.equal(vm.homeAttentionCounts.active,1);
  assert.equal(vm.homeAttentionCounts.all,1);
  vm.homeWorkCustomer=2;assert.equal(vm.homeAttentionCounts.active,1);
  vm.homeCustomer=3;assert.equal(vm.homeTasksPage.total,0);assert.equal(vm.homeAttentionCounts.all,0);
});

test('business stage narrows the customer queue and count before customer selection',()=>{
  const vm=view([task('pay1',1),task('pay2',1),task('invoice',2,'pending_invoice')],{homeTaskMode:'pending_invoice',homeWorkCustomer:1});
  assert.deepEqual(vm.homeQueueGroups.map(g=>g.id),[2]);
  assert.equal(vm.homeSelectedCustomer,2);assert.equal(vm.homeAttentionCounts.active,1);
  assert.deepEqual(vm.homeTaskRows.map(r=>r.id),['invoice']);
});

test('badge units match rendered groups and preserve all member documents',()=>{
  const rows=Array.from({length:56},(_,i)=>task(`delivery${i}`,1,'pending_delivery',{order_id:10,action_text:'安排送货'}));
  const vm=view(rows,{homeWorkspace:'delivery'});
  assert.equal(vm.homeAttentionCounts.active,1);assert.equal(vm.homeTasksPage.total,1);
  assert.equal(vm.homeTasksPage.rows[0].rows.length,56);
  assert.match(template,/homeAttentionCounts\[key\]}}组/);
  assert.match(template,/g.rows.length}}项/);
});

test('hidden and deferred counts remain available while active is selected',()=>{
  const vm=view([task('active',1),task('hidden',1,'pending_invoice',{attention_state:'hidden'}),task('later',1,'pending_reconciliation',{attention_state:'snoozed'}),task('other',2)]);
  assert.deepEqual(vm.homeAttentionCounts,{active:1,customer:0,deferred:1,review:0,hidden:1,all:3});
  for(const [tab,id] of [['hidden','hidden'],['deferred','later'],['active','active']]) {
    vm.homeAttentionTab=tab;assert.equal(vm.homeTasksPage.total,1);assert.equal(vm.homeTaskRows[0].id,id);
    assert.equal(vm.homeAttentionCounts.hidden,1);assert.equal(vm.homeAttentionCounts.deferred,1);
  }
  vm.homeData.tasks[0].attention_state='hidden';
  vm.homeAttentionTab='active';assert.equal(vm.homeSelectedCustomer,1);
  assert.equal(vm.homeTasksPage.total,0);assert.equal(vm.homeAttentionCounts.active,0);
  assert.equal(vm.homeAttentionCounts.hidden,2);
});

test('workspace changes and global customer search keep list and counts in the same scope',()=>{
  const vm=view([task('pay1',1),task('pay2',2),task('prod1',2,'pending_production',{order_id:10,product_code:'80012273'}),task('prod2',2,'pending_production',{order_id:10,product_code:'80012274'})],{homeWorkCustomer:1});
  vm.homeWorkspace='production';vm.homeTaskMode='all';assert.equal(vm.homeSelectedCustomer,2);
  assert.equal(vm.homeAttentionCounts.active,1);assert.equal(vm.homeTasksPage.rows[0].rows.length,2);
  vm.homeQuery='80012273';assert.equal(vm.homeAttentionCounts.active,1);assert.equal(vm.homeTaskRows.length,1);
  vm.homeWorkspace='finance';assert.equal(vm.homeAttentionCounts.all,0);
  vm.homeQuery='';assert.equal(vm.homeSelectedCustomer,1);assert.equal(vm.homeAttentionCounts.active,1);
  vm.homeCustomer=2;assert.equal(vm.homeSelectedCustomer,2);assert.equal(vm.homeAttentionCounts.active,1);
  vm.homeCustomer='';assert.equal(vm.homeSelectedCustomer,1);assert.equal(vm.homeAttentionCounts.active,1);
});

test('all accounting months remain as group members and clicking keeps exact finance target',async()=>{
  const rows=['2026-09','2026-10'].map((month,i)=>task(`pay${i}`,1,'pending_payment',{settlement_identity:'entity:1',target:'finance',product_code:month,target_filter:{customer_id:1,statement_month:month,balance_type:'pending_payment'},amount:i?'200':'100'}));
  const vm=view(rows);
  assert.equal(vm.homeAttentionCounts.active,1);assert.equal(vm.homeTasksPage.rows[0].rows.length,2);
  assert.equal(vm.homeTaskRows.reduce((n,r)=>n+Number(r.amount),0),300);
  const opened=[];vm.homeActionBusy=false;vm.overviewError='';vm.openDashboardTarget=async r=>opened.push(r);
  await mixin.methods.homeOpenTask.call(vm,rows[0]);
  assert.equal(opened[0],rows[0]);assert.equal(vm.homeEntry.target_filter.statement_month,'2026-09');
});
