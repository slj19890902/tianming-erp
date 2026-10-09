// Isolated, synthetic browser test. All HTTP requests are fulfilled locally.
const fs=require('node:fs'), path=require('node:path'), assert=require('node:assert/strict');
const {chromium}=require(process.env.PAPER_PLAYWRIGHT || 'playwright');
const root=path.resolve(__dirname,'../..'), output=process.argv[2];
function card(n,group=`report:${n}`) {
  return {paper_group_id:group,source_identity:`line:${n}`,supplier_order_number:'SRO-SAMPLE',customer_id:1,customer_name:'研光',
    customer_pos:['PO-20261009-001','PO-20261009-002'],paper_phase:'planned',requisition_quantity:92,
    components:[{product_code:`800119${String(n).padStart(2,'0')}`,product_name:'瓦楞内衬',report_length_mm:910,report_width_mm:1120,
      material_code:'VIK',flute_type:'B',finished_unit:'片',customer_order_quantity:100,finished_deduction_quantity:8,
      planned_finished_quantity:92,joining_method:'无需结合',printing_situation:'无印刷',production_notes:['无需结合'],
      sheet_cutting_snapshot:{cutting_factor:1,length_parts:1,width_parts:1,is_die_cut:false,mold_count:1}}]};
}
function pack(cards) { return {cards,pages:[],printable:true,production_label_count:0}; }
(async()=>{
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1050,height:1250}});
    let current=pack([card(1)]), status=200, prints=0;
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',async route=>{
      const u=new URL(route.request().url());
      if (u.pathname.startsWith('/api/')) return route.fulfill({status,contentType:'application/json',body:JSON.stringify(status===200?current:{detail:'权限不足'})});
      const name=u.pathname==='/requisition-production-print.html'?'static/requisition-production-print.html':u.pathname.slice(1);
      if (!name.startsWith('static/') || name.includes('..')) return route.abort();
      const file=path.join(root,name);
      if (!fs.existsSync(file)) return route.fulfill({status:404,body:''});
      return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(file)});
    });
    await page.exposeFunction('capturePaperPrint',()=>{prints++;});
    await page.addInitScript(()=>window.print=()=>{window.dispatchEvent(new Event('beforeprint')); window.capturePaperPrint(); window.dispatchEvent(new Event('afterprint'));});
    const ready=async()=>{await page.waitForFunction(()=>!document.getElementById('printButton').disabled);};
    const inspect=async()=>page.evaluate(()=>({
      pages:document.querySelectorAll('.paper-v2').length,
      halves:document.querySelectorAll('.paper-half:not(.blank)').length,
      clipped:[...document.querySelectorAll('.paper-half')].some(e=>e.scrollHeight>e.clientHeight+1 || e.scrollWidth>e.clientWidth+1),
      text:document.getElementById('pages').textContent,
      font:parseFloat(getComputedStyle(document.querySelector('.paper-products td')).fontSize),
    }));
    const snapshots=[];
    for(const n of [1,2,3]) {
      current=pack(Array.from({length:n},(_,i)=>card(i+1)));
      await page.goto('http://paper.test/requisition-production-print.html?id=1'); await ready();
      const result=await inspect(); assert.equal(result.pages,Math.ceil(n/2)); assert.equal(result.halves,n); assert(!result.clipped);
      assert(!result.text.includes('无需结合')); assert(!result.text.includes('一开一')); assert(result.font>=17);
      snapshots.push({scenario:`${n}-cards`,...result});
    }
    current=pack([card(1),...Array.from({length:10},(_,i)=>card(i+10,'report:merged')),card(2)]);
    await page.reload(); await ready();
    let result=await inspect(); assert(!result.clipped); assert(result.pages>=3); assert(result.text.includes('续页'));
    assert(result.text.includes('80011919'));
    const mixed=await page.evaluate(()=>[...document.querySelectorAll('.paper-v2')].some(p=>p.textContent.includes('80011901') && p.textContent.includes('80011910')));
    assert(!mixed,'continued group must own its A4');
    await page.emulateMedia({media:'print'});
    await page.screenshot({path:path.join(output,'merged-continuation.png'),fullPage:true});
    snapshots.push({scenario:'merged-continuation',...result});
    await page.emulateMedia({media:'screen'});
    const die=card(46); Object.assign(die.components[0],{
      product_name:'内衬长片',joining_method:'打钉',print_content:'客户标志',printing_colors:['黑色'],
      mold_code:'80011946长模',mold_location_display:'A11',
      sheet_cutting_snapshot:{cutting_factor:3,length_parts:3,width_parts:1,theoretical_length_mm:318,theoretical_width_mm:540,is_die_cut:true,mold_count:2},
      inventory_pick_lines:[{reservation_id:1,kind:'finished',quantity:8,unit:'片',location_name:'A1-2-3'},
       {reservation_id:2,kind:'semi_finished',quantity:12,unit:'张',location_name:'B2-1-1'}]});
    const slot=card(47); Object.assign(slot.components[0],{box_type_code:'a1_0201',crease_display:'100 / 200 / 100',joining_method:'粘贴'});
    current=pack([die,slot]); await page.reload(); await ready();
    result=await inspect(); assert(!result.clipped); for(const value of ['318×540','2模','A11','黑色','打钉','粘贴','100 / 200 / 100','A1-2-3','B2-1-1'])assert(result.text.includes(value),value);
    await page.emulateMedia({media:'print'}); await page.screenshot({path:path.join(output,'operations.png'),fullPage:true}); await page.emulateMedia({media:'screen'});
    await page.click('#printButton'); await page.waitForFunction(()=>!document.body.classList.contains('print-blocked')); await page.waitForTimeout(100); assert.equal(prints,1);
    current=structuredClone(current); current.cards[0].components[0].planned_finished_quantity=93;
    await page.click('#printButton'); await page.waitForFunction(()=>document.getElementById('message').textContent.includes('任务内容已更新'));
    assert.equal(prints,1);
    await page.click('#printButton'); await page.waitForTimeout(200); assert.equal(prints,2);
    await page.click('#modeButton'); await ready();
    assert.equal(await page.locator('.paper-operation.internal-only').first().isVisible(),false);
    await page.click('#modeButton'); await ready(); assert(!(await inspect()).clipped);
    status=403; await page.click('#printButton'); await page.waitForFunction(()=>document.getElementById('printButton').disabled);
    assert.equal(prints,2); assert.equal(errors.length,0,errors.join('\n'));
    fs.writeFileSync(path.join(output,'chrome-results.json'),JSON.stringify({passed:true,checks:['1/2/3 cards','five columns','whole A4 continuation','operations','readable font','fresh print','changed data requires review','permission rejection','customer safe'],snapshots},null,2));
    console.log('Chrome: pagination, processes, live print checks and permissions passed.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
