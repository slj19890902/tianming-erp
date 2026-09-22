// Isolated Chrome only: routes all requests to local files/fixtures, never ERP.
// NODE_PATH must contain Playwright. Args: payload.json output-dir [broken.html] [original.html].
const fs=require('fs'),path=require('path'),assert=require('assert');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../..'),[payloadFile,out,oldFile,baselineFile]=process.argv.slice(2);
const data=JSON.parse(fs.readFileSync(payloadFile,'utf8'));
fs.mkdirSync(out,{recursive:true});
const html=fs.readFileSync(path.join(root,'static/delivery-print.html'),'utf8');
const designer=fs.readFileSync(path.join(root,'static/delivery-print-designer.html'),'utf8');
async function mount(browser,source){
 const page=await browser.newPage({viewport:{width:1440,height:900}});
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url());
  assert.equal(url.hostname,'legacy-print.test');
  if(url.pathname==='/delivery-print.html')return route.fulfill({contentType:'text/html',body:source});
  if(url.pathname==='/delivery-print-designer.html')return route.fulfill({contentType:'text/html',body:designer});
  if(url.pathname.startsWith('/api/')){
   assert.equal(route.request().method(),'GET');
   const body=url.pathname.includes('delivery-print-templates')?{published:{version:0,layout:data.print_template.layout}}:{items:[],total_pages:1};
   return route.fulfill({json:body});
  }
  const relative=url.pathname.startsWith('/static/')?url.pathname.slice(1):'static'+url.pathname;
  const asset=path.resolve(root,relative);assert(asset.startsWith(root+path.sep));
  if(!fs.existsSync(asset))return route.fulfill({status:404,body:''});
  return route.fulfill({contentType:asset.endsWith('.css')?'text/css':'text/javascript',body:fs.readFileSync(asset)});
 });
 await page.goto('http://legacy-print.test/delivery-print.html?defer=1');
 await page.evaluate(()=>document.fonts.ready);
 return page;
}
async function inspect(page,payload){
 return page.evaluate(d=>{
  renderDelivery(d);showReady();
  const sheet=document.querySelector('.sheet'),font=s=>parseFloat(getComputedStyle(sheet.querySelector(s)).fontSize);
  return {pages:document.querySelectorAll('.sheet').length,rows:document.querySelectorAll('[data-field="itemRows"] tr').length,
   fonts:{company:font('.company'),title:font('.document-title'),address:font('.factory-address'),meta:font('.meta'),table:font('table'),total:font('.total'),footer:font('.signatures'),detail:font('.footer-meta')},
   positions:['.company','.document-title','.factory-address'].map(s=>sheet.querySelector(s).getBoundingClientRect().top),
   allFit:[...document.querySelectorAll('.print-safe-area')].every(n=>n.scrollHeight<=n.clientHeight+1),
   quantity:[...document.querySelectorAll('.item-quantity')].reduce((s,n)=>s+Number(n.textContent),0),
   unitLineHeights:[...document.querySelectorAll('tbody tr')].map(row=>{const n=row.children[5];const r=document.createRange();r.selectNodeContents(n);return r.getBoundingClientRect().height;})};
 },payload);
}
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const report={};
  if(oldFile){const old=await mount(browser,fs.readFileSync(oldFile,'utf8'));report.before=await inspect(old,data);await old.screenshot({path:path.join(out,'before.png')});await old.close();}
  const page=await mount(browser,html);report.fixed=await inspect(page,data);
  if(baselineFile){const baseline=await mount(browser,fs.readFileSync(baselineFile,'utf8'));report.original=await inspect(baseline,data);assert.deepStrictEqual(report.fixed,report.original);await baseline.close();}
  assert.deepStrictEqual(report.fixed.fonts,{company:21,title:18,address:11,meta:12,table:13,total:13,footer:11,detail:10});
  assert(report.fixed.positions[0]<report.fixed.positions[1]&&report.fixed.positions[1]<report.fixed.positions[2]);
  assert(report.fixed.allFit);assert.equal(report.fixed.rows,data.items.length);
  assert.equal(report.fixed.quantity,data.items.reduce((s,i)=>s+Number(i.quantity),0));
  assert(report.fixed.unitLineHeights.every(h=>h<20),'PCS must fit one line at default size');
  if(report.before)assert(report.fixed.pages<report.before.pages);
  await page.screenshot({path:path.join(out,'fixed.png')});
  const custom=structuredClone(data);custom.print_template.layout.elements.forEach(e=>{e.font_size_pt*=1.1;e.x_mm=1.5;});
  report.custom=await inspect(page,custom);assert(report.custom.allFit);
  for(const [key,size] of Object.entries(report.fixed.fonts))assert(Math.abs(report.custom.fonts[key]-size*1.1)<.01,key);
  const editor=await mount(browser,html);await editor.goto('http://legacy-print.test/delivery-print-designer.html');
  await editor.waitForFunction(()=>document.querySelectorAll('#elementControls input').length===18);
  report.editor=await editor.evaluate(()=>({company:parseFloat(getComputedStyle(document.querySelector('.company')).fontSize),address:parseFloat(getComputedStyle(document.querySelector('.address')).fontSize),table:parseFloat(getComputedStyle(document.querySelector('.table')).fontSize)}));
  for(const key of Object.keys(report.editor))assert.equal(report.editor[key],report.fixed.fonts[key]);
  await editor.evaluate(d=>{layout=d.print_template.layout;render();},custom);
  const customEditor=await editor.evaluate(()=>parseFloat(getComputedStyle(document.querySelector('.company')).fontSize));
  assert(Math.abs(customEditor-report.custom.fonts.company)<.01);
  // Render twice after a custom size to catch cumulative scaling or PO fitting.
  const again=await inspect(page,data);assert.deepStrictEqual(again,report.fixed);
  fs.writeFileSync(path.join(out,'typography-verification.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
