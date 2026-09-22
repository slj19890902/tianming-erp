const fs=require('node:fs'),assert=require('node:assert/strict'),vm=require('node:vm');
const html=fs.readFileSync('static/index.html','utf8');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
function method(start,end,...args){return new AsyncFunction(...args,html.split(start)[1].split(end)[0].replace(/},\s*$/,''));}
const save=method('async saveInvoiceVoid() {','openStatementInvalidate(row) {');
const open=method('async openInvoiceVoid(invoiceId) {','async saveInvoiceVoid() {','invoiceId');
const load=method('async loadFinance() {','async loadFinanceOverview(sessionContext = null) {');
(async()=>{
 const messages=[],calls=[];
 const ctx={invoiceVoidState:{saving:false,loading:false,token:0},authGeneration:1,user:{id:1},showToast:m=>messages.push(m),errorMessage:String,loadInvoiceTasks:async()=>false,loadFinance:async()=>true};
 global.today=()=> '2026-09-22';global.createIdempotencyKey=()=> 'stable-test-key';
 global.axios={get:()=>new Promise(resolve=>calls.push({resolve})),post:(url,payload)=>new Promise((resolve,reject)=>calls.push({url,payload,resolve,reject}))};
 let pending=open.call(ctx,1);ctx.authGeneration=2;calls[0].resolve({data:{invoice_id:1,invoice_status:'issued'}});await pending;assert.equal(ctx.modal,undefined);
 pending=open.call(ctx,2);calls[1].resolve({data:{invoice_id:2,invoice_status:'issued'}});await pending;assert.equal(ctx.invoiceVoidForm.invoice_id,2);
 Object.assign(ctx.invoiceVoidForm,{treatment:'tax_void',reference:'ref',reason:'reason',confirmed:true});
 pending=save.call(ctx);assert.equal(await save.call(ctx),false);assert.equal(calls.length,3);
 calls[2].reject(new Error('network failure'));await pending;
 assert.equal(ctx.modal.type,'invoiceVoid');const key=calls[2].payload.idempotency_key;
 pending=save.call(ctx);assert.equal(calls[3].payload.idempotency_key,key);
 calls[3].resolve({data:{reopened_statement_ids:[1],retained_statement_ids:[]}});assert.equal(await pending,true);
 assert.equal(ctx.modal,null);assert(messages.some(m=>m.includes('已保存')&&m.includes('刷新失败')));
 // A response from reconciliation must never overwrite a newly selected collection workspace.
 Object.assign(ctx,{financeFilters:{},financeView:'current',financeCurrentState:{},financeCurrentRequestId:0,pages:{financeCurrent:1},pageSize:10,syncDesktopListPageSize(){},financeCurrentRows:[]});
 const old=load.call(ctx);ctx.financeView='collections';ctx.financeFilters.balance_type='pending_payment';const current=load.call(ctx);
 calls[5].resolve({data:{items:[{id:'collection'}],total:1}});await current;
 calls[4].resolve({data:{items:[{id:'old-reconciliation'}],total:1}});await old;
 assert.deepEqual(ctx.financeCurrentRows,[{id:'collection'}]);
 for(const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) if(!/\bsrc\s*=/.test(match[1])&&match[2].trim())new vm.Script(match[2]);
 console.log('PASS reversal double submit, same-key network retry, saved-refresh failure, auth and workspace race, complete inline JavaScript');
})().catch(e=>{console.error(e);process.exitCode=1;});
