// Isolated synthetic label rendering only. Never connects to the ERP or printer.
const fs=require('fs'),path=require('path'),assert=require('assert');
const {chromium}=require(process.env.TM_PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),out=path.join(root,'data/audit/label411');fs.mkdirSync(out,{recursive:true});
 const raw=fs.readFileSync(path.join(root,'static/production-packaging-label.html'),'utf8');
 const html=raw.replace('      loadPackage();','      window.labelTest={applyTemplate,layoutDrivenLabelHtml,compactLabelHtml,validateRenderedLabels};');
 assert.notEqual(html,raw);
 const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const page=await browser.newPage({viewport:{width:1200,height:800}});
 await page.route('**/*',route=>route.abort());
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.setContent(html); await page.waitForFunction(()=>window.labelTest);
 for(const [version,width,height] of [['current_40x30_v2',40,30],['current_40x30_v1',40,30],['legacy_65x45_v1',65,45]]){
   const size=await page.evaluate(version=>{window.labelTest.applyTemplate(version);return document.getElementById('legacyPageSize').textContent},version);
   assert(size.includes(`size: ${width}mm ${height}mm`),version);
 }
 const layout=process.env.TM_LABEL_LAYOUT_FIXTURE
   ? JSON.parse(JSON.parse(fs.readFileSync(process.env.TM_LABEL_LAYOUT_FIXTURE,'utf8')).find(r=>r.stream==='release').payload_json)
   : {paper:{width_mm:40,height_mm:30},elements:['customer_short_name','product_code','product_name','specification','quantity'].map((id,index)=>({id,kind:'text',visible:true,x_mm:0.8,y_mm:0.6+index*5.8,width_mm:38.4,height_mm:5.6,font_size_mm:3.6,font_weight:900,text_align:'center'}))};
 const geometry=await page.evaluate(async layout=>{
   window.labelTest.applyTemplate('current_40x30_v2');
   const labels=[{customer_short_name:'驿力',product_code:'Z.001.000205',product_name:'30入装格挡',specification:'800×180×120mm',quantity:300,label_number:1,label_count:3},
     {customer_short_name:'天华',product_code:'LONG-CODE-22000015-ABCDEFGHIJKLMN',product_name:'模切白卡内盒完整两行文字测试样品',specification:'800×180×120mm',quantity:100,label_number:2,label_count:3},
     {customer_short_name:'通用',product_code:'SAMPLE-003',product_name:'衬板',specification:'500×300mm',quantity:80,label_number:3,label_count:3}];
   document.getElementById('labelList').innerHTML=labels.map(label=>window.labelTest.layoutDrivenLabelHtml(label,layout)).join('');
   await document.fonts.ready;
   window.labelTest.validateRenderedLabels();
   return [...document.querySelectorAll('.label-card')].map(el=>({width:el.getBoundingClientRect().width,height:el.getBoundingClientRect().height}));
 },layout);
 assert.equal(errors.length,0,errors.join(';'));
 for(const r of geometry){assert(Math.abs(r.width-40*96/25.4)<1);assert(Math.abs(r.height-30*96/25.4)<1);}
 await page.emulateMedia({media:'print'});
 await page.pdf({path:path.join(out,'labels.pdf'),preferCSSPageSize:true,printBackground:true,displayHeaderFooter:false,scale:1});
 await page.screenshot({path:path.join(out,'print-layout.png'),fullPage:true});
 fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({geometry,errors,paper:'40x30',labels:3},null,2));
 await browser.close();console.log('isolated Chrome labels rendered; PDF page sizes require independent verification');
})().catch(e=>{console.error(e);process.exit(1)});
