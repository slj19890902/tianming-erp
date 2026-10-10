// Synthetic fixture only: every network request is intercepted; no live ERP actions.
const fs=require('node:fs'), path=require('node:path'), assert=require('node:assert/strict');
const {chromium}=require(process.env.PAPER_PLAYWRIGHT || 'playwright');
const root=path.resolve(__dirname,'../..'), output=process.argv[2];
const svg='<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400"><rect width="640" height="400" fill="white"/><path d="M50 100H590V300H50Z M170 40H470V360H170Z" fill="none" stroke="black" stroke-width="3"/><path d="M50 100H590M50 300H590M170 40V360M470 40V360" fill="none" stroke="#666" stroke-dasharray="8 4"/><text x="275" y="215" font-family="Arial" font-size="28">540 x 318</text></svg>';
function drawing(code,n=1) {return {key:`drawing-${code}-${n}`,product_code:code,name:`内衬展开图 ${n}`,source_label:'参考图',kind:'image',preview_url:`/api/requisition/production-paper-drawings/order-item/${code}/drawing-${n}/preview`};}
function card(code,options={}) {
  const component={product_code:code,product_name:'模切内衬',report_length_mm:954,report_width_mm:540,
    material_code:'VIK',flute_type:'B',finished_unit:'片',customer_order_quantity:100,finished_deduction_quantity:0,planned_finished_quantity:100,
    joining_method:'打钉',printing_colors:['黑色'],print_content:'客户标志',mold_code:code+'长模',mold_location_display:'A11',
    sheet_cutting_snapshot:{cutting_factor:3,length_parts:3,width_parts:1,theoretical_length_mm:318,theoretical_width_mm:540,is_die_cut:true,mold_count:2},
    paper_drawings:[drawing(code)]};
  return {paper_group_id:options.group || code,source_identity:code,supplier_order_number:'SRO-SAMPLE',customer_id:1,customer_name:'研光',
    customer_pos:['PO-20261010-001'],paper_phase:'planned',requisition_quantity:50,components:[component],...options};
}
(async()=>{
  fs.mkdirSync(output,{recursive:true}); const browser=await chromium.launch({channel:'chrome',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1100,height:1250}});
    let cards=[card('80011929'),card('80012273',{source_type:'stock_replenishment'})], failed=false, hold=false, release=null, prints=0;
    const errors=[], unexpected=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',async route=>{
      const u=new URL(route.request().url());
      if(u.pathname.startsWith('/api/requisition/production-paper-drawings/')) {
        if(hold) await new Promise(resolve=>{release=resolve;});
        return route.fulfill({status:failed?403:200,contentType:'image/svg+xml',body:failed?'':svg,headers:{'Cache-Control':'no-store'}});
      }
      if(u.pathname==='/api/requisition/supplier-orders/1/production-print-package')
        return route.fulfill({contentType:'application/json',body:JSON.stringify({cards,pages:[],printable:true,production_label_count:0})});
      const name=u.pathname==='/requisition-production-print.html'?'static/requisition-production-print.html':u.pathname.slice(1);
      if(!name.startsWith('static/') || name.includes('..')) {unexpected.push(u.href); return route.abort();}
      const file=path.join(root,name); if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
      return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(file)});
    });
    await page.exposeFunction('capturePaperPrint',()=>{prints++;});
    await page.addInitScript(()=>window.print=()=>{
      if(window.capturePdf){window.capturePaperPrint();return;}
      window.dispatchEvent(new Event('beforeprint'));window.capturePaperPrint();window.dispatchEvent(new Event('afterprint'));
    });
    const ready=()=>page.waitForFunction(()=>!document.getElementById('printButton').disabled);
    const pdf=async name=>{
      await page.emulateMedia({media:'screen'});await page.evaluate(()=>window.capturePdf=true);
      const count=prints;await page.click('#printButton');
      while(prints===count)await new Promise(r=>setTimeout(r,20));
      await page.emulateMedia({media:'print'});
      await page.pdf({path:path.join(output,name),preferCSSPageSize:true,printBackground:true});
      await page.evaluate(()=>window.capturePdf=false);await page.emulateMedia({media:'screen'});
    };
    const inspect=()=>page.evaluate(()=>({pages:document.querySelectorAll('.paper-v2').length,
      figures:[...document.querySelectorAll('.paper-drawing')].map(e=>({code:e.querySelector('b').textContent,src:e.querySelector('img')?.getAttribute('src'),
        width:e.clientWidth,height:e.clientHeight,right:e.getBoundingClientRect().left>e.closest('.paper-work').getBoundingClientRect().left+200})),
      clipped:[...document.querySelectorAll('.paper-half,.paper-work,.paper-drawing')].some(e=>e.scrollWidth>e.clientWidth+1 || e.scrollHeight>e.clientHeight+1),
      footerOverlap:[...document.querySelectorAll('.paper-half:not(.blank)')].some(e=>[...e.children].some(c=>!c.matches('.paper-foot') && c.getBoundingClientRect().bottom > e.querySelector('.paper-foot').getBoundingClientRect().top-3)),
      font:parseFloat(getComputedStyle(document.querySelector('.paper-operation')).fontSize),text:document.getElementById('pages').textContent}));
    const snapshots=[];
    await page.goto('http://paper.test/requisition-production-print.html?id=1');await ready();
    let result=await inspect();assert.equal(result.pages,1);assert.equal(result.figures.length,2);assert(!result.clipped);assert(!result.footerOverlap);assert(result.font>=18);
    for(const f of result.figures){assert(f.right);assert(f.src.includes(f.code));}
    for(const s of ['2模','A11','318×540','黑色','打钉'])assert(result.text.includes(s));
    snapshots.push({scenario:'ordinary-and-stock',...result});
    await page.emulateMedia({media:'print'});await page.screenshot({path:path.join(output,'two-tasks-with-drawings.png'),fullPage:true});
    await page.emulateMedia({media:'screen'});await pdf('two-tasks-with-drawings.pdf');
    cards=Array.from({length:4},(_,i)=>card(String(80011940+i),{group:'combined'}));
    cards[0].components[0].paper_drawings.push(drawing('80011940',2));
    await page.reload();await ready();result=await inspect();assert(!result.clipped);assert(!result.footerOverlap);assert.equal(result.figures.length,5);assert(result.pages>1);assert(result.text.includes('续页'));
    for(const f of result.figures)assert(f.src.includes(f.code));snapshots.push({scenario:'merged-multiple-drawings-continuation',...result});
    await page.screenshot({path:path.join(output,'merged-drawings.png'),fullPage:true});
    await pdf('merged-drawings.pdf');
    await page.click('#modeButton');await ready();assert.equal(await page.locator('.paper-drawings').first().isVisible(),false);
    await page.click('#modeButton');await ready();
    cards=[card('80011929')];cards[0].components[0].paper_drawings=[];
    await page.reload();await ready();assert.equal(await page.locator('.with-drawings').count(),0);
    cards=[card('80011929')];cards[0].components[0].paper_drawings[0]={...drawing('80011929'),source_label:'任务图',preview_url:'data:image/svg+xml;base64,'+Buffer.from(svg).toString('base64')};
    const frozen={release_id:71,product_id:7,number:'DW71',revision:'A',geometry:{width_mm:640,height_mm:400,dimensions:{}},print_objects:[],svg_urls:{structure:cards[0].components[0].paper_drawings[0].preview_url,print:cards[0].components[0].paper_drawings[0].preview_url}};
    cards[0].managed_drawings=[frozen];cards[0].components[0].managed_drawing=frozen;
    await page.reload();await ready();assert.equal(await page.locator('.paper-drawing img').count(),1);assert.equal(await page.locator('.drawing-appendix img').count(),1);
    cards=[card('80011929')];hold=true;
    await page.reload({waitUntil:'domcontentloaded'});await page.waitForFunction(()=>document.querySelector('.paper-drawing img'));
    assert(await page.locator('#printButton').isDisabled());hold=false;while(!release)await new Promise(r=>setTimeout(r,20));release();await ready();
    await page.click('#printButton');await page.waitForTimeout(200);assert.equal(prints,3);
    failed=true;await page.reload();await page.waitForFunction(()=>document.getElementById('message').textContent.includes('图纸加载失败'));
    assert(await page.locator('#printButton').isDisabled());failed=false;await page.click('#retryButton');await ready();
    cards[0].components[0].paper_drawings[0].preview_url='https://untrusted.test/drawing.png';
    await page.reload();await page.waitForFunction(()=>document.getElementById('message').textContent.includes('图纸预览不可用'));
    assert(await page.locator('#printButton').isDisabled());assert.equal(unexpected.length,0);assert.equal(errors.length,0,errors.join('\n'));
    fs.writeFileSync(path.join(output,'drawing-results.json'),JSON.stringify({passed:true,checks:['right-side previews','ordinary and replenishment','full-size processes','multiple drawings','correct merged product identity','half A4 continuation','no drawing reclaims space','frozen SVG','loading blocks print','403 blocks print','retry','customer-safe hidden','unsafe URL rejected'],snapshots},null,2));
    console.log('Production task drawings: 13 directed Chrome checks passed.');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
