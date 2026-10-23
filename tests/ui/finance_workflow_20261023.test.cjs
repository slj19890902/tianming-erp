const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vmModule = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname,'../../static/index.html'),'utf8');
const methodStarts = [...html.matchAll(/^          (?:async )?(\w+)\([^\n]*\) \{/gm)];
function harness(names) {
  const calls=[], pending=[], messages=[], routes=[], opens=[];
  const context={console,Blob,URL, document:{createElement(){throw Error('unexpected stale download');}},window:{setTimeout(){}},confirm:()=>true,today:()=> '2026-10-23',createIdempotencyKey:()=> 'frozen-key',
    axios:{post:(url,payload)=>new Promise((resolve,reject)=>pending.push({url,payload,resolve,reject})),get:(url)=>new Promise((resolve,reject)=>pending.push({url,resolve,reject}))}};
  const state={action:'',statementId:null}, operation={confirmingTaskId:null,downloadingTaskId:null,voidingTaskId:null,resultSaving:false,detailTaskId:null};
  const vm={authGeneration:1,user:{id:7},financeView:'current',canGenerateInvoiceTask:true,canViewInvoiceTasks:true,canConfirmStatement:true,
    financeStatementOperationState:state,invoiceTaskOperationState:operation,financeCurrentExpanded:{},financeCurrentRows:[],modal:null,
    errorMessage:e=>e.message,showToast:(...m)=>messages.push(m),
    loadFinance:async()=>{calls.push('finance');return true;},loadInvoiceTasks:async()=>{calls.push('tasks');return true;},
    setFinanceView:async(v,f)=>{routes.push([v,f]);vm.financeView=v;return true;},closeModal:()=>{vm.modal=null;},
    financeGroupKey:r=>r.customer_id+':'+r.statement_month,toggleFinanceCurrentGroup:r=>opens.push(['expand',r]),
    openInvoiceTask:async t=>{opens.push(['invoice',t]);return true;},openStatementDetail:async s=>{opens.push(['statement',s]);return true;},
    exportStatement:async()=>true};
  for (const name of names) {
    const n=methodStarts.findIndex(m=>m[1]===name);assert.ok(n>=0,name);
    const source=html.slice(methodStarts[n].index,methodStarts[n+1].index).trim();
    vm[name]=vmModule.runInNewContext('({'+source+'})',context)[name].bind(vm);
  }
  return {vm,context,pending,messages,routes,opens,calls};
}
const issuedForm=()=>({task_id:20,customer_id:9,status:'issued',invoice_number:'INV-1',invoice_date:'2026-10-23',expected_version:3,expected_ledger_version:2,expected_ledger_versions:{11:2,12:4},authGeneration:1,actorId:7});

test('next step reuses inline task; fresh preparation stays in reconciliation',async()=>{
  const {vm,opens,pending,routes}=harness(['runFinancePrimaryAction','openFinanceInvoice','generateInvoiceTask']);
  await vm.runFinancePrimaryAction({primary_action:'invoice',statements:[{id:11,pending_invoice_amount:113,active_invoice_tasks:[{id:41,status:'exported'}]}]});
  assert.equal(opens[0][1].id,41);assert.equal(pending.length,0);assert.equal(vm.financeView,'current');
  await vm.runFinancePrimaryAction({primary_action:'invoice',statements:[11,12].map(id=>({id,active_invoice_tasks:[{id:41}]}))});
  assert.equal(opens[1][1].id,41);
  const row={id:12,version:2,active_invoice_tasks:[]};
  const creating=vm.openFinanceInvoice(row);assert.equal(await vm.openFinanceInvoice(row),false);assert.equal(pending.length,1);
  row.version=99;assert.equal(pending[0].payload.expected_version,2);
  pending[0].resolve({data:{id:42}});assert.equal(await creating,true);
  assert.equal(opens[2][1].id,42);assert.equal(vm.financeView,'current');assert.equal(routes.length,0);
  vm.canViewInvoiceTasks=false;assert.equal(await vm.openFinanceInvoice(row),false);assert.equal(pending.length,1);
});

test('export-confirm updates open detail and next-step version, rejects stale session before POST',async()=>{
  const {vm,pending}=harness(['exportAndConfirmStatement']);
  const row={id:11,version:2,confirmation_status:'draft'};
  vm.statementDetail={...row};vm.financeCurrentRows=[{statements:[{...row}]}];
  const confirming=vm.exportAndConfirmStatement(row);await new Promise(setImmediate);
  pending[0].resolve({data:{id:11,version:3,confirmation_status:'confirmed'}});
  assert.equal(await confirming,true);
  assert.equal(vm.statementDetail.confirmation_status,'confirmed');assert.equal(row.version,3);assert.equal(vm.financeCurrentRows[0].statements[0].version,3);
  let finishDownload;vm.exportStatement=()=>new Promise(resolve=>finishDownload=resolve);
  const old=vm.exportAndConfirmStatement(row);vm.authGeneration++;finishDownload(true);
  assert.equal(await old,false);assert.equal(pending.length,1);
});

test('issuing requires actual number; freezes merged ledgers; routes once to collection after commit',async()=>{
  const {vm,pending,routes,messages}=harness(['saveInvoiceTaskResult','openInvoiceTaskResult']);
  assert.equal(vm.openInvoiceTaskResult({id:20,status:'ready'}),false);
  assert.equal(vm.openInvoiceTaskResult({id:20,status:'ready',last_export_hash:'previous-failed-attempt'}),false);
  vm.invoiceTaskResult={...issuedForm(),invoice_number:''};assert.equal(await vm.saveInvoiceTaskResult(),false);assert.equal(pending.length,0);
  vm.invoiceTaskResult=issuedForm();vm.modal={type:'invoiceTaskResult'};
  const saving=vm.saveInvoiceTaskResult();assert.equal(await vm.saveInvoiceTaskResult(),false);
  vm.invoiceTaskResult.expected_ledger_versions[11]=100;
  assert.equal(pending[0].payload.expected_ledger_versions[11],2);assert.equal(routes.length,0);
  vm.loadInvoiceTasks=async()=>{throw Error('refresh offline');};pending[0].resolve({data:{}});
  assert.equal(await saving,true);assert.equal(routes.length,1);assert.equal(routes[0][0],'collections');assert.equal(routes[0][1].customer_id,9);
  assert.equal(vm.modal,null);assert.ok(messages.some(m=>m[0].includes('不要重复登记')));
});

test('failed issue stays pending; stale save neither navigates nor closes another session form',async()=>{
  const {vm,pending,routes}=harness(['saveInvoiceTaskResult']);
  vm.invoiceTaskResult={...issuedForm(),status:'failed',failure_reason:'tax declined'};vm.modal={type:'invoiceTaskResult'};
  const failed=vm.saveInvoiceTaskResult();pending[0].resolve({data:{}});assert.equal(await failed,true);assert.equal(routes.length,0);
  vm.invoiceTaskResult=issuedForm();vm.modal={type:'invoiceTaskResult'};
  const old=vm.saveInvoiceTaskResult();const replacement={...vm.invoiceTaskOperationState,resultSaving:true};vm.invoiceTaskOperationState=replacement;vm.authGeneration=2;
  pending[1].resolve({data:{}});assert.equal(await old,false);assert.equal(routes.length,0);assert.equal(vm.modal.type,'invoiceTaskResult');assert.equal(replacement.resultSaving,true);
  assert.equal(await vm.saveInvoiceTaskResult(),false);assert.equal(pending.length,2);
});

test('unissued reversal is single flight, retains error form, returns to reconciliation after commit',async()=>{
  const {vm,pending,routes}=harness(['voidInvoiceTask']);
  vm.modal={type:'invoiceTask'};vm.invoiceTaskOperationState.detailTaskId=20;
  const task={id:20,version:3,customer_id:9};const reverting=vm.voidInvoiceTask(task);
  assert.equal(await vm.voidInvoiceTask(task),false);pending[0].reject(Error('version conflict'));
  assert.equal(await reverting,false);assert.equal(routes.length,0);assert.equal(vm.modal.type,'invoiceTask');
  const retry=vm.voidInvoiceTask(task);pending[1].resolve({data:{reopened_statement:true}});assert.equal(await retry,true);
  assert.equal(routes[0][0],'current');assert.equal(vm.modal,null);assert.equal(vm.invoiceTaskOperationState.voidingTaskId,null);
});

test('confirm/download responses cannot cross logout into a new session',async()=>{
  const {vm,pending,messages}=harness(['prepareFinanceInvoiceTask','confirmInvoiceTask','downloadInvoiceTaxTemplate']);
  const preparing=vm.prepareFinanceInvoiceTask({id:20,version:3,status:'draft'});
  vm.authGeneration++;pending[0].resolve({data:{id:20,version:4,status:'ready'}});
  assert.equal(await preparing,false);assert.equal(pending.length,1);assert.equal(messages.length,0);
  const fetching=vm.downloadInvoiceTaxTemplate({id:20,status:'ready'});vm.authGeneration++;
  pending[1].resolve({data:new Blob(['secret workbook'])});assert.equal(await fetching,false);assert.equal(messages.length,0);
});

test('real statement export drops stale blob before download and confirmation',async()=>{
  const {vm,pending,messages,context}=harness(['exportStatement','exportAndConfirmStatement']);
  let downloads=0;context.document.createElement=()=>({style:{},click(){downloads++;},remove(){}});context.document.body={appendChild(){}};
  const old=vm.exportAndConfirmStatement({id:11,version:2});
  vm.authGeneration++;vm.user={id:8};pending[0].resolve({data:new Blob(['old customer statement'])});
  assert.equal(await old,false);assert.equal(downloads,0);assert.equal(pending.length,1);assert.equal(messages.length,0);
  const current=vm.exportStatement({id:11});pending[1].resolve({data:new Blob(['current customer statement'])});
  assert.equal(await current,true);assert.equal(downloads,1);
});
