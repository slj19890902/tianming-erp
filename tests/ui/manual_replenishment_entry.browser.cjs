const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const port=Number(process.argv[2]);assert.ok(port>1024&&port<65536);
const base=`http://127.0.0.1:${port}`,out=process.env.ERP_MANUAL_REPLENISHMENT_BROWSER;
const getVm=()=>document.querySelector('#app')._vnode.component.proxy;
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try {
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const login=await context.request.post(base+'/api/auth/login',{data:{username:'admin',password:'RolePass123!'}});
  assert.equal(login.status(),200);
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/frontend-v2/formal-workspace?page=orders');
  await page.waitForFunction(()=>document.querySelector('#app')?._vnode?.component?.proxy?.user?.id);
  await page.waitForFunction(()=>!document.querySelector('#app')._vnode.component.proxy.sessionRestoring);
  await page.evaluate(async()=>{
   const vm=document.querySelector('#app')._vnode.component.proxy;
   await vm.openOrder();vm.orderForm.customer_id=1;vm.orderForm.customer_po='FICTION-UNSAVED';
   vm.orderForm.items=[{product_id:1,product_code:'21301010',product_name:'虚构纸箱',quantity:999},
    {product_id:2,product_code:'LINER-001',product_name:'虚构衬板',quantity:888},
    {product_id:1,product_code:'21301010',product_name:'重复行',quantity:777}];
  });
  const before=await page.evaluate(()=>JSON.stringify(document.querySelector('#app')._vnode.component.proxy.orderForm));
  const picker=page.locator('[data-manual-replenishment-picker]:visible');
  await picker.locator('summary').click();assert.equal(await picker.locator('input[type=checkbox]').count(),2);
  await picker.getByRole('button',{name:'选择全部',exact:true}).click();
  const popupPromise=context.waitForEvent('page');await picker.getByRole('link',{name:'进入补库录入（2款）'}).click();const popup=await popupPromise;
  popup.on('pageerror',e=>errors.push(e.message));
  await popup.waitForFunction(()=>document.querySelector('#app')?._vnode?.component?.proxy?.stockReplenishmentForm?.items?.length===2);
  const state=await popup.evaluate(()=>{
   const v=document.querySelector('#app')._vnode.component.proxy;
   return {source:v.stockReplenishmentForm.source_type,qty:v.stockReplenishmentForm.items.map(i=>i.quantity),ids:v.stockReplenishmentForm.items.map(i=>i.reference_product_id)};
  });assert.deepEqual(state,{source:'customer_request',qty:[null,null],ids:[1,2]});
  assert.equal(await page.evaluate(()=>JSON.stringify(document.querySelector('#app')._vnode.component.proxy.orderForm)),before);
  fs.mkdirSync(out,{recursive:true});await page.screenshot({path:path.join(out,'order-multi-select.png')});
  await popup.screenshot({path:path.join(out,'manual-draft.png')});
  // Use the real rendered quantity controls, then save through the existing guarded path.
  const quantities=popup.locator('input[placeholder="请人工填写"]');assert.equal(await quantities.count(),2);
  await quantities.nth(0).fill('12');await quantities.nth(1).fill('25');
  const responsePromise=popup.waitForResponse(r=>r.url().endsWith('/api/requisition/stock-replenishment/orders')&&r.request().method()==='POST');
  await popup.getByRole('button',{name:'保存补库报料草稿',exact:true}).click();const saved=await responsePromise;
  assert.equal(saved.status(),201);const savedData=await saved.json();assert.equal(savedData.status,'draft');
  const read=await context.request.get(base+`/api/requisition/stock-replenishment/orders/${savedData.id}`);assert.equal(read.status(),200);
  assert.deepEqual((await read.json()).items.map(i=>Number(i.quantity)),[12,25]);
  assert.equal(await page.evaluate(()=>JSON.stringify(document.querySelector('#app')._vnode.component.proxy.orderForm)),before);
  // Actual import modal shares the picker and its original draft stays intact when opening it.
  await page.evaluate(()=>{
   const v=document.querySelector('#app')._vnode.component.proxy;
   v.orderImportDrafts=[{source_name:'fiction.pdf',matched_customer_id:1,items:[{matched_product_id:1,raw_product_code:'21301010',quantity:50},{matched_product_id:2,raw_product_code:'LINER-001',quantity:80}]}];
   v.emailQueueMode=false;v.modal={type:'orderPdfImport',title:'虚构订单导入核对'};
  });
  await page.locator('[data-manual-replenishment-picker]:visible summary').click();
  await page.locator('[data-manual-replenishment-picker]:visible').getByRole('button',{name:'选择全部',exact:true}).click();
  assert.ok(await page.locator('[data-manual-replenishment-picker]:visible a').getAttribute('href'));
  await page.screenshot({path:path.join(out,'import-multi-select.png')});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({passed:true,browser:'installed Chrome',full_erp_html:true,real_api:true,fictional_database:true,multi_select:2,unwarned:true,blank_quantities:true,order_preserved:true,saved_readback:[12,25],import_entry:true},null,2));
  console.log('Chrome full-page multi-select, new window, manual save/readback and original order preserved: passed');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
