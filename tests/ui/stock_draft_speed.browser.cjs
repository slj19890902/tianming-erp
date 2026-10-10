// Real installed Chrome, actual ERP methods and warning summary; fictional fixture only.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {methodSource,draft,policy}=require('./stock_draft_speed.test.cjs');
const root=path.resolve(__dirname,'../..'),output=process.argv[2];
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const loading=html.match(/<div v-if="stockReplenishmentForm\._pendingDrafts"[^\n]+/)[0];
const summary=html.slice(html.indexOf('<div v-else-if="stockReplenishmentForm.source_type===\'stock_warning\'" class="stock-warning-summary">'),html.indexOf('<details v-if="line.procurement_mode!==\'external_purchase\'"')).replace('v-else-if','v-if');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1280,height:800}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.setContent('<main id="app"></main>');
  await page.addScriptTag({path:path.join(root,'static/vendor/vue-3.5.40.global.prod.js')});
  await page.evaluate(({source,draft,policy,loading,summary})=>{
   const controllers=new Map();let serial=0;
   const methods=new Function('axios','createIdempotencyKey','latestRequestControllers','return ({'+source+'});')({get:async url=>{
    testVm.requests.push(url);
    if(url.endsWith('/replenishment-draft'))return await new Promise(resolve=>window.completeDraft=()=>resolve({data:draft}));
    if(url==='/api/requisition/stock-policies')throw Error('Unneeded all-policy request');
    return {data:{items:[]}};
   }},()=>`fixture-${++serial}`,controllers);
   window.testVm=Vue.createApp({data:()=>({user:{id:1},authGeneration:1,activePage:'dashboard',canRequisition:true,canSubmitBusinessRequest:false,stockPolicyDraftQuantities:{},customerOptions:[],allMaterials:[],stockReplenishmentForm:{items:[]},stockReplenishmentProducts:[],stockPolicyWarnings:[],stockReplenishmentCompanions:[],modal:null,requests:[],workbenchLoads:0,materialDone:false}),methods:{...methods,
    loadPage:async()=>{testVm.workbenchLoads++;},loadCustomerOptions:async()=>{},loadMaterials:()=>new Promise(resolve=>window.completeMaterials=()=>{testVm.materialDone=true;resolve();}),pageAllowed:()=>true,
    resetSingleScreenExpansion(){},cancelLatestRequest(){},syncDesktopWorkspaceUrl(){},cancelSupplierRequisitionPreview(){},
    beginLatestRequest(key){controllers.get(key)?.abort();const c=new AbortController();controllers.set(key,c);return c;},finishLatestRequest(){},isCancelledRequest:e=>e?.name==='AbortError',errorMessage:e=>e.message,showToast(){},
    fluteDisplay:x=>x||'B',stockWarningTheoreticalSheets:()=>20,stockWarningExtraSheets:()=>0,start(){this.openLowStockReplenishment(policy);}
   },template:'<button id="generate" @click="start">生成报料草稿</button><section v-if="modal" style="margin:30px;padding:30px;border:1px solid #ccc"><h2>{{modal.title}}</h2>'+loading+'<div v-for="line in stockReplenishmentForm.items" :key="line._key">'+summary+'</div></section>'}).mount('#app');
  },{source:methodSource(html),draft,policy,loading,summary});
  await page.locator('#generate').click();
  await page.getByRole('status').waitFor();assert.deepEqual(await page.evaluate(()=>testVm.requests),['/api/requisition/stock-policies/17/replenishment-draft']);
  assert.equal(await page.evaluate(()=>testVm.workbenchLoads),0);
  await page.evaluate(()=>completeDraft());
  await page.getByText('FICTION-3｜虚构测试纸箱').waitFor();
  assert.equal(await page.evaluate(()=>testVm.materialDone),false);assert.equal(await page.evaluate(()=>testVm.workbenchLoads),0);
  await page.locator('input[type=number]').fill('25');assert.equal(await page.evaluate(()=>testVm.stockReplenishmentForm.items[0].quantity),25);
  await page.evaluate(()=>completeMaterials());
  await page.waitForFunction(()=>testVm.workbenchLoads===1);
  assert.equal(await page.evaluate(()=>testVm.stockReplenishmentForm.items[0].quantity),25);assert.deepEqual(errors,[]);
  fs.mkdirSync(output,{recursive:true});await page.screenshot({path:path.join(output,'draft-first.png')});
  fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({status:'passed',browser:'installed Chrome',fictional_data:true,details_before_options_and_workbench:true,editing_preserved:true},null,2));
  console.log('Chrome: loading feedback, requested details before auxiliary loads, quantity edit preserved.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
