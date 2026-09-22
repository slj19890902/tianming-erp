const fs=require('node:fs'),assert=require('node:assert/strict');
const html=fs.readFileSync('static/index.html','utf8');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
function body(start,end){return html.split(start)[1].split(end)[0].replace(/},\s*$/, '');}
const search=new AsyncFunction(body('async onFinanceCustomerChange() {','async onFinanceMonthChange() {'));
const load=new AsyncFunction(body('async loadFinance() {','async loadFinanceOverview(sessionContext = null) {'));
const detail=new AsyncFunction('row',body('async openStatementDetail(row) {','nextStatementMonth(value) {'));
(async()=>{
 let reloaded=0;
 const ctx={financeFilters:{customer_id:3,statement_month:'2026-09',balance_type:'pending_payment'},async reloadFinanceCurrent(){reloaded++;}};
 await search.call(ctx);
 assert.equal(reloaded,1);assert.equal(ctx.financeFilters.balance_type,'all');assert.equal(ctx.financeFilters.statement_month,'');
 const pending=[];
 global.axios={get:(url,config)=>new Promise(resolve=>pending.push({url,config,resolve}))};
 Object.assign(ctx,{authGeneration:1,user:{id:1},financeView:'current',financeCurrentState:{},financeCurrentRequestId:0,pages:{financeCurrent:1},pageSize:10,syncDesktopListPageSize(){this.pageSize=10;},errorMessage:String});
 const first=load.call(ctx);
 ctx.financeFilters={customer_id:'',statement_month:'2026-08',balance_type:'pending_payment'};
 const second=load.call(ctx);
 assert.equal(pending[0].config.params.balance_type,'all');assert.equal(pending[0].config.params.customer_id,3);
 assert.equal(pending[1].config.params.through_month,true);
 pending[1].resolve({data:{total:1,items:[{id:'aug'}],queue_counts:{pending_payment:1}}});await second;
 pending[0].resolve({data:{total:5,items:[{id:'stale'}]}});await first;
 assert.deepEqual(ctx.financeCurrentRows,[{id:'aug'}]);assert.equal(ctx.financeCurrentTotal,1);
 const d1=detail.call(ctx,{id:1}),d2=detail.call(ctx,{id:2});
 pending[3].resolve({data:{id:2,statement_number:'B',invoices:[{invoice_number:'REAL'}],settlements:[]}});await d2;
 pending[2].resolve({data:{id:1,statement_number:'A'}});await d1;
 assert.equal(ctx.statementDetail.id,2);
 console.log('PASS search resets hidden filters, cutoff request, stale queue and detail responses ignored');
})().catch(error=>{console.error(error);process.exitCode=1;});
