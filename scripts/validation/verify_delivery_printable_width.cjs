// Synthetic documents, all network requests fulfilled locally; no formal ERP access.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = process.env.DELIVERY_SOURCE_ROOT || path.resolve(__dirname, '../..'), output = process.argv[2];
assert(output, 'output directory required; generate layouts.json from preset_layout/default_layout first');
const layouts = JSON.parse(fs.readFileSync(path.join(output,'layouts.json'),'utf8'));
const rows = Array.from({length:7},(_,i)=>({sequence:i+1,customer_po:'POORD041674',
  customer_material_code:`Z.001.00020${i}`,customer_product_name:['30入装格挡','30入装衬板6片','自制风机包装箱1178×906×814'][i%3],
  product_code:`TEST${i}`,product_name:'测试包装产品',customer_drawing_number:`063209${i}`,customer_model:'TRD-N',customer_category:'A',
  specification:'1139×778×102',quantity:[600,1000,60,300,100,50,190][i],unit:'只',pricing_included:true,
  unit_price:'12.345',amount:'7407.00',remarks:''}));
function fixture(preset, prices=false, items=rows) {
  return {id:999999,document_hash:'isolated-synthetic',delivery_number:'TEST-20260928-001',delivery_date:'2026-09-28',
    sender:{company_name:'苏州天明包装有限公司',address:'苏州',phone:''},
    customer:{name:'隔离打印测试客户',address:'苏州市测试路99号2号楼',contact_person:'测试联系人',phone:'00000000000'},
    vehicle_number:'测试车牌',print_template:{layout:structuredClone(layouts[preset]),profile_key:'test',version:1},
    price_display:{shown:prices,allowed:prices},order_context:'',customer_document_rows:structuredClone(items),
    items:structuredClone(items),commercial_quantities:{'只':2300},total_quantity:2300,total_amount:'12345.67'};
}
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const evidence=[]; let posts=0,prints=0;
  try {
    const page=await browser.newPage({viewport:{width:1280,height:1000}});
    await page.exposeFunction('mockPrint',()=>{prints++;});
    await page.addInitScript(()=>{window.print=()=>window.mockPrint();});
    await page.route('**/*',async route=>{
      const req=route.request(), url=new URL(req.url());
      assert.equal(url.hostname,'erp-isolated.test');
      if(req.method()!=='GET') {assert.match(url.pathname,/customer-print-events$/);posts++;return route.fulfill({json:{ok:true}});}
      if(url.pathname.startsWith('/api/'))return route.fulfill({json:{}});
      const file=path.resolve(root,'static',url.pathname.replace(/^\/(static\/)?/,''));
      assert(file.startsWith(path.join(root,'static')+path.sep));
      return route.fulfill(fs.existsSync(file)?{body:fs.readFileSync(file),contentType:file.endsWith('.html')?'text/html':file.endsWith('.css')?'text/css':'text/javascript'}:{status:404,body:''});
    });
    await page.goto('http://erp-isolated.test/delivery-print.html?defer=1');
    await page.evaluate(()=>{
      const original=CustomerDeliveryPrint.checkWidth;
      CustomerDeliveryPrint.checkWidth=s=>{try{return original(s);}catch(e){
        const b=s.querySelector('.cd-body,.print-safe-area').getBoundingClientRect();
        e.message+=JSON.stringify([...s.querySelectorAll('section,table,th,td,footer')].filter(x=>x.scrollWidth>x.clientWidth+2||x.getBoundingClientRect().right>b.right+1).map(x=>({tag:x.tagName,cls:x.className,w:x.clientWidth,scroll:x.scrollWidth,text:x.textContent.slice(0,40)})));
        throw e;
      }};
    });
    async function render(data,profile={paper_width_mm:241,paper_height_mm:139.5}) {
      await page.emulateMedia({media:'screen'});
      await page.evaluate(async({data,profile})=>{
        applyPrintProfile(profile); await document.fonts.ready;
        CustomerDeliveryPrint.controls(data,()=>{});renderDelivery(data);await document.fonts.ready;
        renderDelivery(data);showReady();
      },{data,profile});
    }
    async function measure() {return page.evaluate(()=>Array.from(document.querySelectorAll('.cd-sheet,.sheet')).map(s=>{
      const b=s.querySelector('.cd-body,.print-safe-area'),r=b.getBoundingClientRect(),p=s.getBoundingClientRect();
      return {paperMM:p.width*25.4/96,widthMM:r.width*25.4/96,leftMM:r.left*25.4/96,rightMM:r.right*25.4/96,centerError:Math.abs((r.left+r.right-p.left-p.right)/2),
        rows:s.querySelectorAll('tbody tr').length,headers:Array.from(s.querySelectorAll('th')).map(t=>t.textContent),height:s.scrollHeight};
    }));}
    if (process.argv.includes('--baseline')) {
      await render(fixture('yl'),{paper_width_mm:241,paper_height_mm:139.5,content_width_mm:215});
      await page.emulateMedia({media:'print'});
      const baseline=await measure();
      assert(Math.abs(baseline[0].widthMM-215)<.2);assert(baseline[0].rightMM>227);
      fs.writeFileSync(path.join(output,'baseline-reproduction.json'),JSON.stringify({baseline,printableWidth:200,clippedRightMM:baseline[0].rightMM-200},null,2));
      console.log(JSON.stringify({baseline}));return;
    }
    const long=Array.from({length:35},(_,i)=>({...rows[i%7],sequence:i+1,customer_po:'CUSTOMER-ORDER-LONG-0000000000000000',customer_product_name:'长名称测试'.repeat(6)}));
    const frozen=fixture('yl');frozen.print_template.layout.font_size_pt=10;
    for(const [name,data] of [['yl-no-price',fixture('yl')],['yl-frozen',frozen],['yl-priced',fixture('yl',true)],['yke',fixture('yke',true)],
      ['kew',fixture('kew')],['legacy',fixture('legacy')],['yl-long',fixture('yl',false,long)]]) {
      const original=JSON.stringify(data.print_template);
      await render(data);
      assert.deepEqual(await page.evaluate(()=>customerPrintData.print_template),data.print_template);
      if(name==='yl-frozen')assert(Math.abs(await page.locator('.cd-table').first().evaluate(el=>parseFloat(getComputedStyle(el).fontSize))-10*96/72)<.01);
      const screen=await measure();
      assert(screen.every(x=>Math.abs(x.widthMM-188)<.2 && x.centerError<1));
      assert.equal(screen.reduce((n,x)=>n+x.rows,0),name==='yl-long'?35:7);
      if(name.startsWith('yl'))assert(screen.every(x=>x.headers.includes('单价')&&x.headers.includes('金额')));
      if(name==='yl-no-price') {
        const amounts=await page.locator('.cd-table tbody tr').evaluateAll(trs=>trs.map(tr=>Array.from(tr.cells).slice(-2).map(c=>c.textContent)));
        assert(amounts.every(c=>c.every(v=>v==='')));
      }
      await page.emulateMedia({media:'print'});
      await page.evaluate(()=>CustomerDeliveryPrint.validate(document.getElementById('sheets')));
      const printed=await measure();
      assert(printed.every(x=>x.leftMM>=5.8 && x.rightMM<=194.2), "rightmost content must fit the 200mm printable span");
      assert.deepEqual(printed.map(x=>[x.widthMM,x.rows]),screen.map(x=>[x.widthMM,x.rows]));
      await page.pdf({path:path.join(output,name+'.pdf'),width:'241mm',height:'139.5mm',margin:{top:0,bottom:0,left:0,right:0},printBackground:true});
      await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
      assert.equal(JSON.stringify(data.print_template),original);
      evidence.push({name,screen,printed});
    }
    await render(fixture('yl'),{paper_width_mm:220,paper_height_mm:150,content_width_mm:200,offset_x_mm:2,offset_y_mm:1});
    const custom=await measure();assert(Math.abs(custom[0].paperMM-220)<.2);assert(Math.abs(custom[0].widthMM-200)<.2);
    await page.evaluate(()=>document.querySelector('.cd-table').style.width='230mm');
    assert.match(await page.evaluate(()=>{try{CustomerDeliveryPrint.validate(document.getElementById('sheets'));return 'missed';}catch(e){return e.message;}}),/安全宽度/);
    await page.locator('#printButton').click();
    await page.waitForFunction(()=>!document.getElementById('errorBox').hidden);
    assert.equal(posts,0);assert.equal(prints,0);
    await render(fixture('yl',true));
    await page.locator('#printButton').click();await page.waitForFunction(()=>!document.getElementById('printButton').disabled);
    assert.equal(posts,1);assert.equal(prints,1);
    fs.writeFileSync(path.join(output,'printable-width-evidence.json'),JSON.stringify({evidence,custom,overflowBlocked:true,printCalls:prints,localMockEvents:posts,formalAccess:false},null,2));
    console.log(JSON.stringify({cases:evidence.length,custom,overflowBlocked:true,prints,posts}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
