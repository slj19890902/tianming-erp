const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),vm=require('node:vm');
const {chromium}=require(process.env.PLAYWRIGHT_NODE_MODULE||'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root=path.resolve(__dirname,'../..');
for(const file of ['static/index.html','static/warehouse.html']) {
 for(const match of fs.readFileSync(path.join(root,file),'utf8').matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
  if(match[1].trim())new vm.Script(match[1]);
 }
}
(async()=>{
 assert.equal(process.env.ERP_ENVIRONMENT,'test');
 const out=process.env.ERP_MULTI_MOLD_BROWSER_EVIDENCE;assert(out);fs.mkdirSync(out,{recursive:true});
 const port=Number(process.env.ERP_PORT);assert(port>18000&&port<20000);
 const base=`http://127.0.0.1:${port}`;
 const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 page.on('dialog',dialog=>dialog.accept());
 try {
  // Fictional test account created by mold_app; never a production account.
  const login=await context.request.post(base+'/api/auth/login',{data:{username:'admin',password:'RolePass123!'}});assert.equal(login.status(),200);
  await page.goto(base+'/multi-mold-acceptance');await page.waitForFunction(()=>!!window.erpAcceptance);
  await page.evaluate(async()=>{
   const a=window.erpAcceptance,me=(await axios.get('/api/auth/me')).data;
   a.user=me.user||me;a.user.permissions=me.permissions||a.user.permissions;a.activePage='products';
   a.allCustomers=a.customerOptions=[{id:1,name:'模具联动测试客户'}];a.selectedProductCustomer=a.customerOptions[0];
   if(!await a.openProduct({id:1}))throw Error('parent editor did not open');
   a.bomEditor.components[0].quantity_per_set=7;a.productForm.production_notes='父件未保存备注';
  });
  const panel=page.locator('fieldset.bom-editor-panel');await panel.getByRole('button',{name:'新建子件',exact:true}).waitFor();
  assert.match(await panel.innerText(),/每张出 2 片/);assert.match(await panel.innerText(),/每张出 4 片/);
  assert.match(await panel.innerText(),/用途：A模/);assert.match(await panel.innerText(),/用途：B模/);
  assert.equal(await panel.getByRole('link',{name:'查看模具位置'}).count(),2);
  await panel.screenshot({path:path.join(out,'bom-two-molds.png')});
  await panel.getByRole('button',{name:'新建子件',exact:true}).click();
  await page.waitForFunction(()=>window.erpAcceptance.modal.title.startsWith('新建子件'));
  await page.evaluate(()=>window.erpAcceptance.closeModal());
  await page.waitForFunction(()=>window.erpAcceptance.productForm.id===1);
  assert.equal(await page.evaluate(()=>window.erpAcceptance.bomEditor.components[0].quantity_per_set),7);
  assert.equal(await page.evaluate(()=>window.erpAcceptance.productForm.production_notes),'父件未保存备注');
  await panel.getByRole('button',{name:'新建子件',exact:true}).click();
  await page.waitForFunction(()=>window.erpAcceptance.modal.title.startsWith('新建子件'));
  const saved=await page.evaluate(async()=>{
   const a=window.erpAcceptance,child=(await axios.get('/api/master/products/2')).data;
   a.productForm=a.hydrateProductForm({...child,id:null,version:undefined,product_code:'UAT-NEW-CHILD',
    customer_material_code:'UAT-NEW-CHILD',product_name:'虚构新增子件'});
   a.productForm.sheet_cutting_settings=child.sheet_cutting_settings;
   const ok=await a.saveModal();
   return {ok,id:a.productForm.id,rows:a.bomEditor.components.map(row=>({id:row.component_product_id,mold:row.mold_tool_id})),
     toast:a.toast?.message||a.toastMessage||'',error:a.productEditorOptionsError};
  });
  assert.equal(saved.id,1,JSON.stringify(saved));assert.equal(saved.rows.length,3,JSON.stringify(saved));
  assert.equal(saved.rows[2].mold,2);
  const childRead=await context.request.get(base+`/api/master/products/${saved.rows[2].id}`);assert.equal(childRead.status(),200);
  assert.equal((await childRead.json()).product_name,'虚构新增子件');
  // Saving the child must not silently write the parent's draft recipe.
  const bom=await context.request.get(base+'/api/master/products/1/bom');assert.equal(bom.status(),200);
  assert.equal(new Set((await bom.json()).components.map(r=>r.mold_tool_id)).size,2);
  for(const id of [2,3]) {
   const preview=await context.request.get(base+`/api/warehouse/molds/${id}/label-preview`);assert.equal(preview.status(),200);
   const data=await preview.json();assert.equal(data.label_display_product_name,id===2?'A模':'B模');
  }
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({status:'passed',browser:'installed Chrome',actualEditor:true,realApis:true,fictionalDatabase:true,productionTouched:false,twoMolds:true,newChildSaveReadback:true,parentRecipeNotSilentlySaved:true,newChildCancelPreservesDraft:true,syntax:'passed',pageErrors:errors},null,2));
 } finally {await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1});
