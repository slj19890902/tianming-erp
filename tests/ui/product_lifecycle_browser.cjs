const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_NODE_MODULE||'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
 const output=process.env.ERP_LIFECYCLE_BROWSER_EVIDENCE;assert(output);
 fs.mkdirSync(output,{recursive:true});
 const installedChrome=process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
   ||(process.platform==='darwin'?'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome':'C:/Program Files/Google/Chrome/Application/chrome.exe');
 const browser=await chromium.launch({executablePath:installedChrome,headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});const page=await context.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const base='http://127.0.0.1:18994';
 try{
  const login=await context.request.post(base+'/api/auth/login',{data:{username:'admin',password:'RolePass123!'}});assert.equal(login.status(),200);
  await page.goto(base+'/lifecycle-acceptance');
  await page.locator('[name="q"]').fill('80011965');await page.locator('form button[type="submit"]').click();
  await page.locator('[data-product="1"]').click();
  await page.getByText('自动成品入库',{exact:false}).waitFor();
  let text=await page.locator('.pw-detail').innerText();assert.match(text,/2,500 张/);assert.match(text,/10,000 只/);assert.doesNotMatch(text,/理论待产 10,000 只/);assert.match(text,/1865×830/);
  await page.screenshot({path:path.join(output,'desktop-lifecycle.png'),fullPage:true});
  await page.locator('[data-tab="inventory"]').first().click();
  text=await page.locator('.pw-detail').innerText();assert.match(text,/成品\s+10,000 只/);assert.match(text,/可用 10,000/);
  await page.screenshot({path:path.join(output,'desktop-inventory.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.locator('[data-tab="activity"]').click();
  assert(await page.locator('.pw-detail').isVisible());
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
  await page.screenshot({path:path.join(output,'mobile-lifecycle.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({status:'passed',browser:'installed Chrome',realApi:true,fictionalDatabase:true,productionTouched:false,material:2500,theoretical:10000,desktop:true,mobile:true,pageErrors:errors},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1});
