const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const port=Number(process.argv[2]);assert.ok(port>1024&&port<65536);
const component=process.argv[3]||'whole';
const base=`http://127.0.0.1:${port}`,out=path.join(process.env.ERP_SUPPLIER_SHEET_BROWSER,component);
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1500,height:1050}});
  // This account exists only in the fictional fixture created by this test.
  const login=await context.request.post(base+'/api/auth/login',{data:{username:'admin',password:'RolePass123!'}});assert.equal(login.status(),200);
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/frontend-v2/formal-workspace?page=products');
  await page.waitForFunction(()=>document.querySelector('#app')?._vnode?.component?.proxy?.user?.id);
  await page.evaluate(async()=>await document.querySelector('#app')._vnode.component.proxy.openProduct({id:1}));
  const row=page.locator(`[data-supplier-sheet-default="${component}"]`);
  await row.waitFor();assert.equal(await row.getByLabel('供应商报料长',{exact:true}).inputValue(),'750');
  assert.equal(await row.getByLabel('供应商报料宽',{exact:true}).inputValue(),'678');
  await row.getByLabel('供应商报料宽',{exact:true}).fill('700');await row.getByLabel('供应商纸板要求',{exact:true}).selectOption('毛片');
  fs.mkdirSync(out,{recursive:true});await row.scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'default-edited.png')});
  const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/master/products/1')&&r.request().method()==='PUT');
  await page.getByRole('button',{name:'保存',exact:true}).last().click();
  const saved=await responsePromise;assert.equal(saved.status(),200,await saved.text());
  const record=await saved.json();assert.equal(record.sheet_cutting_settings.schema_version,3);
  assert.equal(record.sheet_cutting_settings[component].actual_supplier_width_mm,'700');assert.equal(Number(record.report_width_mm),226);assert.equal(record[component==='base'?'base_crease_type':'crease_type'],'毛片');
  if(component==='base'){
   assert.equal(record.crease_type,'净料');assert.equal(record.base_crease_left_mm,null);assert.equal(record.base_crease_middle_mm,null);assert.equal(record.base_crease_right_mm,null);
   assert.equal(record.sheet_cutting_settings.cover.actual_supplier_width_mm,undefined);
  }
  await page.reload();await page.waitForFunction(()=>document.querySelector('#app')?._vnode?.component?.proxy?.user?.id);
  await page.evaluate(async()=>await document.querySelector('#app')._vnode.component.proxy.openProduct({id:1}));
  await row.waitFor();assert.equal(await row.getByLabel('供应商报料宽',{exact:true}).inputValue(),'700');
  assert.equal(await row.getByLabel('供应商纸板要求',{exact:true}).inputValue(),'毛片');
  await row.scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'default-reloaded.png')});
  const defaults=await context.request.get(base+'/api/requisition/stock-replenishment/products?customer_id=1&product_ids=1');assert.equal(defaults.status(),200);
  const product=(await defaults.json()).items[0];assert.equal(product.report_width_mm,component==='base'?678:700);assert.equal(product.output_per_sheet,6);
  // Clearing the explicit override is a form edit until the user saves it.
  await row.getByRole('button',{name:'恢复按开料计算',exact:true}).click();
  assert.equal(await row.getByLabel('供应商报料宽',{exact:true}).inputValue(),'678');
  const unchanged=await context.request.get(base+'/api/master/products/1');assert.equal((await unchanged.json()).sheet_cutting_settings[component].actual_supplier_width_mm,'700');
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({passed:true,component,browser:'installed Chrome',real_page:true,fictional_db:true,save_reload:true,theoretical:[375,226],supplier:[750,700],type:'毛片',yield:6,unsaved_reset_preserves_database:true},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
