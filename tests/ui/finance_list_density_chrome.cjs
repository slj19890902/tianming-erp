// Synthetic localhost only; never forwards a request to formal ERP.
const fs=require('fs'),path=require('path'),http=require('http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../..'),out=process.env.ERP_UI_ARTIFACT_DIR;
if(!out)throw Error('Set an isolated ERP_UI_ARTIFACT_DIR');fs.mkdirSync(out,{recursive:true});
const html=fs.readFileSync(root+'/static/index.html','utf8').replace('        async mounted() {','        async fixtureDisabledMounted() {').replace('app.mount("#app");','window.erpFixture=app.mount("#app");');
const rows=Array.from({length:16},(_,i)=>({customer_id:i+1,customer_name:'隔离测试客户'+(i+1),statement_month:'2026-09',period_start:'2026-08-28',period_end:'2026-09-27',statement_count:1,statements:[],queue_status:'待收款',primary_action:'settle',pending_payment_amount:3500,pending_payment_action_amount:3500,reconciled_receivable_amount:3500,settled_amount:0,invoiced_amount:3500}));
let total=16,failNext=false;const requests=[],errors=[];
const server=http.createServer((req,res)=>{const url=new URL(req.url,'http://localhost');
 if(url.pathname.startsWith('/api/')){res.setHeader('Content-Type','application/json');
  if(req.method!=='GET'){res.statusCode=405;return res.end('{}');}
  if(url.pathname==='/api/finance/current-customer-months'){
   if(failNext){failNext=false;res.statusCode=503;return res.end(JSON.stringify({detail:'隔离模拟读取失败'}));}
   const p=Number(url.searchParams.get('page')),size=Number(url.searchParams.get('page_size'));requests.push({p,size,queue:url.searchParams.get('balance_type'),workspace:url.searchParams.get('workspace')});
   return res.end(JSON.stringify({items:rows.slice((p-1)*size,Math.min(p*size,total)).map(r=>url.searchParams.get('balance_type')==='pending_reconciliation'?{...r,queue_status:'待对账',primary_action:'reconcile'}:r),total,summary:{customer_count:total,pending_payment_action_amount:total*3500},queue_counts:{all_open:total,all:total,pending_payment:total,pending_reconciliation:total}}));
  }
  if(url.pathname==='/api/finance/invoices/1/void-preview') return res.end(JSON.stringify({invoice_id:1,invoice_number:'ISOLATED-ONLY',customer_name:'隔离测试客户1',invoice_status:'issued',invoice_amount:3500,payment_blockers:[],statements:[{id:1,statement_number:'TEST-ST',statement_month:'2026-09',amount:3500}],expected_task_version:3,expected_versions:{1:2},expected_ledger_versions:{1:2}}));
  if(url.pathname==='/api/finance/invoice-tasks') return res.end(JSON.stringify({items:rows.map(r=>({...r,id:r.customer_id,task_number:'TEST-'+r.customer_id,status:'exported',version:1})),invoice_records:rows.map(r=>({...r,id:r.customer_id,invoice_number:'SYNTHETIC-'+r.customer_id,invoice_date:'2026-09-22',invoice_amount:3500,statements:[{id:r.customer_id,statement_number:'ST-'+r.customer_id,customer_name:r.customer_name,statement_month:r.statement_month}]}))}));
  return res.end(JSON.stringify({items:[],total:0,permissions:[]}));
 }
 if(url.pathname==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');return res.end(html);}
 const file=path.resolve(root,'.'+decodeURIComponent(url.pathname));
 if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.statusCode=404;return res.end();}
 res.setHeader('Content-Type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'application/octet-stream');
 res.end(fs.readFileSync(file));
});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({channel:'chrome',headless:true});
 try{const page=await browser.newPage({viewport:{width:1920,height:920}});const origin='http://127.0.0.1:'+server.address().port;
  await page.route('**/*',route=>route.request().url().startsWith(origin)?route.continue():route.abort());
  page.on('pageerror',e=>errors.push(e.message));await page.goto(origin);await page.waitForFunction(()=>!!window.erpFixture);
  await page.evaluate(async()=>{const a=window.erpFixture;a.user={id:1,role:'admin',permissions:['*'],ui_mode:'standard'};a.hasPermission=()=>true;a.pageAllowed=()=>true;a.activePage='finance';a.financeView='current';a.authGeneration=1;a.financeFilters.balance_type='pending_reconciliation';await a.loadFinance();});
  await page.waitForTimeout(1200);
  total=6;await page.getByRole('button',{name:'刷新',exact:true}).last().click();await page.waitForTimeout(500);
  const metrics=await page.evaluate(()=>({size:erpFixture.pageSize,rows:erpFixture.financeCurrentRows.length,capacity:erpFixture.workspaceCapacities,tableTop:document.querySelector('.finance-current-table').getBoundingClientRect().top,main:document.querySelector('.main').getBoundingClientRect().toJSON()}));
  fs.writeFileSync(out+'/metrics.json',JSON.stringify({metrics,requests,errors},null,2));await page.screenshot({path:out+'/customer-list.png'});
  const initialSize=metrics.size;
  await page.getByRole('button',{name:'开票办理',exact:true}).click();await page.waitForTimeout(700);
  const invoiceSize=await page.evaluate(()=>erpFixture.pageSize);
  await page.getByRole('button',{name:'客户对账',exact:true}).click();await page.waitForTimeout(700);
  await page.getByRole('button',{name:'开票办理',exact:true}).click();await page.waitForTimeout(700);
  await page.getByRole('button',{name:'客户对账',exact:true}).click();await page.waitForTimeout(700);
  const returned=await page.evaluate(()=>({size:erpFixture.pageSize,rows:erpFixture.financeCurrentRows.length,capacity:erpFixture.workspaceCapacities}));
  fs.writeFileSync(out+'/navigation.json',JSON.stringify({initialSize,invoiceSize,returned,requests,errors},null,2));await page.screenshot({path:out+'/after-invoice-return.png'});
  assert.equal(returned.size,initialSize,'Returning from invoices must restore customer page capacity');assert.equal(returned.rows,6);
  assert.equal(metrics.rows,6,'Six customers must appear together on a full desktop, not one per page');assert(metrics.size>=6);assert(metrics.tableTop<310,'Top controls should give space back to customers');assert.deepEqual(errors,[]);
  assert.equal(await page.getByRole('button',{name:/^待收款/}).count(),0,'Reconciliation has no collection queue');
  await page.getByRole('button',{name:'收款办理',exact:true}).click();await page.waitForTimeout(500);
  assert.equal(await page.evaluate(()=>erpFixture.financeView),'collections');
  assert.equal(requests.at(-1).workspace,'collections');assert.equal(requests.at(-1).queue,'pending_payment');
  assert.equal(await page.getByRole('button',{name:/^待对账/}).count(),0);
  assert.equal(await page.evaluate(()=>erpFixture.financeCurrentRows.length),6);
  await page.screenshot({path:out+'/collections.png'});
  await page.getByRole('button',{name:'客户对账',exact:true}).click();await page.waitForTimeout(500);
  total=16;await page.getByRole('button',{name:'刷新',exact:true}).last().click();await page.waitForTimeout(400);
  const size=await page.evaluate(()=>erpFixture.pageSize);await page.getByRole('button',{name:'下一页',exact:true}).last().click();await page.waitForTimeout(400);assert.equal(await page.evaluate(()=>erpFixture.pages.financeCurrent),2);
  assert.equal(await page.evaluate(()=>erpFixture.pageSize),size,'Underfilled last page must keep page size');
  await page.getByRole('button',{name:'展开',exact:true}).first().click();await page.getByRole('dialog',{name:'客户账单',exact:true}).getByRole('button',{name:'关闭',exact:true}).click();await page.waitForTimeout(300);
  assert.equal(await page.evaluate(()=>erpFixture.pages.financeCurrent),2);assert.equal(await page.evaluate(()=>erpFixture.pageSize),size);
  await page.getByRole('button',{name:/^待对账/}).click();await page.waitForTimeout(300);assert.equal(await page.evaluate(()=>erpFixture.pages.financeCurrent),1);assert.equal(await page.evaluate(()=>erpFixture.pageSize),size);
  total=1;await page.evaluate(async()=>{erpFixture.pages.financeCurrent=2;await erpFixture.loadFinance();});await page.waitForTimeout(250);
  assert.equal(await page.evaluate(()=>erpFixture.pages.financeCurrent),1,'Empty last page must fall back');assert.equal(await page.evaluate(()=>erpFixture.financeCurrentRows.length),1);
  failNext=true;await page.getByRole('button',{name:'刷新',exact:true}).last().click();await page.waitForTimeout(250);
  assert(await page.evaluate(()=>!!erpFixture.financeCurrentState.error));assert.equal(await page.evaluate(()=>erpFixture.financeCurrentRows.length),0);
  total=16;await page.getByRole('button',{name:'刷新',exact:true}).last().click();await page.waitForTimeout(250);assert.equal(await page.evaluate(()=>erpFixture.financeCurrentRows.length),size);
  for(const viewport of [{width:1440,height:900},{width:1280,height:720},{width:390,height:844}]){
   await page.setViewportSize(viewport);await page.waitForTimeout(600);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
   await page.screenshot({path:out+`/customers-${viewport.width}.png`});
  }
  await page.setViewportSize({width:1920,height:920});await page.waitForTimeout(600);
  await page.evaluate(async()=>{erpFixture.user.ui_mode='large';await erpFixture.$nextTick();await erpFixture.loadFinance();});await page.waitForTimeout(700);
  assert(await page.evaluate(()=>erpFixture.financeCurrentRows.length>=5));await page.screenshot({path:out+'/customers-large.png'});
  await page.evaluate(async()=>{await erpFixture.openInvoiceVoid(1);});await page.waitForTimeout(200);
  assert.equal(await page.getByRole('button',{name:'登记并退回对账',exact:true}).isDisabled(),true);
  assert.equal(await page.getByLabel('我已核实以上事实；本次处理整张发票的全部关联金额，旧文件停止使用。').isChecked(),false);
  await page.screenshot({path:out+'/invoice-reversal-form.png'});
  await page.evaluate(()=>erpFixture.closeModal());
  assert.deepEqual(errors,[]);
  fs.writeFileSync(out+'/result.json',JSON.stringify({ok:true,requests,errors,metrics},null,2));console.log('PASS isolated Chrome: six rows, compact header, paging, expanded details, queue switch, last-page fallback, failed-read retry, large mode and responsive widths');
 }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
