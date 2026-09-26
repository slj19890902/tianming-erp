// All requests are fulfilled locally; never navigate to a formal ERP surface.
const fs=require('node:fs'), path=require('node:path'), assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'../..');
const [fixture,output]=process.argv.slice(2);
assert(fixture && output, 'usage: node verify_delivery_print_http.cjs print-fixture.json output-directory');
(async()=>{
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1250,height:850}});
    const posts=[]; let printed=0;
    await page.exposeFunction('recordBrowserPrint',()=>{printed++;});
    await page.addInitScript(()=>{window.print=()=>window.recordBrowserPrint();});
    await page.route('**/*',async route=>{
      const request=route.request(), url=new URL(request.url());
      assert.equal(url.hostname,'erp-isolated.test');
      if(request.method()!=='GET') {
        assert.match(url.pathname,/^\/api\/deliveries\/\d+\/customer-print-events$/);
        posts.push(JSON.parse(request.postData()));
        return route.fulfill({json:{ok:true}});
      }
      if(url.pathname.startsWith('/api/')) return route.fulfill({json:{}});
      const file=path.resolve(root,'static',url.pathname.replace(/^\/(static\/)?/,''));
      assert(file.startsWith(path.join(root,'static')+path.sep));
      if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
      return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.html')?'text/html':file.endsWith('.css')?'text/css':'text/javascript'});
    });
    await page.goto('http://erp-isolated.test/delivery-print.html?defer=1');
    const data=JSON.parse(fs.readFileSync(fixture,'utf8'));
    await page.evaluate(d=>{CustomerDeliveryPrint.controls(d,()=>{});renderDelivery(d);showReady();},data);
    const environment=await page.evaluate(()=>({secure:isSecureContext,uuid:typeof crypto.randomUUID,random:typeof crypto.getRandomValues}));
    assert.equal(environment.secure,false); assert.equal(environment.uuid,'undefined');
    await page.locator('#printButton').click();
    await page.waitForFunction(()=>!document.getElementById('printButton').disabled);
    assert.equal(posts.length,1); assert.equal(printed,1);
    assert.equal(await page.locator('#errorBox').isVisible(),false);
    await page.screenshot({path:path.join(output,'print-http-fixed.png'),fullPage:true});
    fs.writeFileSync(path.join(output,'print-http-evidence.json'),JSON.stringify({environment,printCalls:printed,posts,productionAccess:false},null,2));
    console.log(JSON.stringify({environment,printCalls:printed,eventPosts:posts.length}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
