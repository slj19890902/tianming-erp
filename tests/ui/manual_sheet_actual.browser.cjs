// UI-only fictional fixture. No production server, login or business writes.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'../..'),out=process.env.SHEET_UI_EVIDENCE;
if(!out)throw Error('SHEET_UI_EVIDENCE required');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:'<p>隔离测试：仅校验现有地图入口参数</p>'}));
  await page.goto('http://127.0.0.1:18997/isolated-ui-fixture');
  await page.setContent('<html><head><meta charset="UTF-8"></head><body><div id="app"><sheet ref="sheet" :actor-id="1" :order-item-id="1"/></div></body></html>');
  await page.addStyleTag({content:'body{font-family:Arial,sans-serif}button{padding:8px 12px}input{box-sizing:border-box}.btn{border:1px solid #cbd5e1;border-radius:6px;background:white;cursor:pointer}.primary{background:#2563eb;color:white}'});
  await page.addStyleTag({path:path.join(root,'static/ui/manual-sheet-deduction.css')});
  await page.addScriptTag({path:path.join(root,'static/vendor/vue-3.5.40.global.prod.js')});
  await page.addScriptTag({path:path.join(root,'static/ui/manual-sheet-deduction.js')});
  await page.evaluate(()=>{
   const items=[100,92.5,80,65].map((score,i)=>({lot_id:i+1,version:1,selectable:true,board_length_mm:1950+i*100,
     board_width_mm:805,material_code:'VIK',flute_type:'B',stock_yield_per_sheet:32,available_stock_quantity:100,
     max_stock_quantity:9,actual_max_stock_quantity:100,utilization_percent:score,waste_percent:100-score,
     waste_area_cm2:(1950+i*100)*805*(100-score)/10000,waste_basis:'rectangular_nesting',
     match_color:score>=90?'green':score>=70?'yellow':'orange',recommendation_label:'大张裁切／模切',warning_codes:[],
     warehouse_location:{id:i+40,location_name:'一楼库料架 A'+(i+1),warehouse_floor:1},
     cut_plan:{rows:4,columns:4,blank_yield:2},review_hash:'a'.repeat(64)}));
   window.axios={get:async()=>({data:{product_id:4,product_code:'82020014',product_name:'隔离演示产品',
     board_length_mm:388,board_width_mm:268,flute_type:'B',material_code:'VIK',remaining_required_piece_qty:288,
     pieces_per_box:1,scrap_completion_allowed:true,items}})};
   window.app=Vue.createApp({components:{sheet:TMManualSheetDeduction.component}}).mount('#app');
  });
  await page.getByRole('button',{name:'使用库存片料',exact:true}).click();
  await page.locator('tbody input[type=checkbox]').first().check();
  const quantity=page.locator('tbody input[type=number]').first();await quantity.fill('70');
  await page.getByText('完工后预计余 1952 片入库存，订单数量不变').waitFor();
  assert.equal(await page.locator('.manual-sheet-score-green').count(),2);
  assert.equal(await page.locator('.manual-sheet-score-yellow').count(),1);
  assert.equal(await page.locator('.manual-sheet-score-orange').count(),1);
  assert.ok(await page.locator('thead th').nth(3).evaluate(el=>el.getBoundingClientRect().width)<125);
  fs.mkdirSync(out,{recursive:true});await page.screenshot({path:path.join(out,'1280.png')});
  await page.locator('.manual-sheet-location').first().click();
  const url=new URL(await page.locator('iframe').getAttribute('src'),'http://isolated.test');
  assert.equal(url.searchParams.get('location_id'),'40');assert.equal(url.searchParams.get('readonly'),'1');
  await page.getByRole('button',{name:'返回片料选择'}).click();assert.equal(await quantity.inputValue(),'70');
  assert.ok(await page.locator('tbody input[type=checkbox]').first().isChecked());
  await page.setViewportSize({width:1024,height:768});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  assert.ok(await page.locator('.manual-sheet-table-wrap').evaluate(el=>el.scrollWidth<=el.clientWidth));
  assert.equal(await page.locator('.manual-sheet-dialog:visible').count(),1);
  assert.ok(await page.locator('.manual-sheet-foot').evaluate(el=>el.getBoundingClientRect().bottom<=innerHeight));
  await page.screenshot({path:path.join(out,'1024.png')});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({passed:true,fictional_ui:true,
   production_clicks:false,viewports:[[1280,900],[1024,768]],map_url_and_return:true,real_map_render_tested:false},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
