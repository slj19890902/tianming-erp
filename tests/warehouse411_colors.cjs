// Isolated CSS and script checks; no ERP requests or browser interaction.
const fs=require('fs'),path=require('path'),assert=require('assert'),vm=require('vm');
const {chromium}=require(process.env.TM_PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),read=p=>fs.readFileSync(path.join(root,p),'utf8');
 const mobile=read('static/mobile_erp.html');
 for(const block of mobile.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) if(block[1].trim()) new vm.Script(block[1]);
 const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const page=await browser.newPage();await page.route('**/*',r=>r.abort());
 const css=read('factory_twin/frontend/src/warehouseTwin.css')+'\n'+read('factory_twin/frontend/src/warehouseWorkspace.css');
 await page.setContent(`<style>${css}</style><div class="warehouse-twin-shell"><div id="cell" class="mold-rack-cell"></div></div>`);
 for(const [classes,color] of [['empty','rgb(255, 255, 255)'],['selected','rgb(245, 158, 11)'],['selected rack-search-hit move-state-target','rgb(255, 235, 0)']]){
   const actual=await page.evaluate(classes=>{const e=document.getElementById('cell');e.className='mold-rack-cell '+classes;return getComputedStyle(e).backgroundColor},classes);
   assert.equal(actual,color,classes);
 }
 const styles=[...mobile.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/gi)].map(m=>m[1]).join('\n');
 await page.setContent(`<style>${styles}</style><button id="cell" class="warehouse-map-location"></button>`);
 for(const [classes,color] of [['','rgb(255, 255, 255)'],['target','rgb(245, 158, 11)'],['target has-goods deep-link','rgb(255, 235, 0)']]){
   const actual=await page.evaluate(classes=>{const e=document.getElementById('cell');e.className='warehouse-map-location '+classes;return getComputedStyle(e).backgroundColor},classes);
   assert.equal(actual,color,classes);
 }
 const conflict=await page.evaluate(()=>{const e=document.getElementById('cell');e.className='warehouse-map-location deep-link unmatched';return getComputedStyle(e).borderColor});
 assert.equal(conflict,'rgb(185, 28, 28)');
 await browser.close();console.log('Desktop/mobile computed colors, search precedence, conflict border and mobile syntax passed');
})().catch(e=>{console.error(e);process.exit(1)});
