// Run with the installed Chrome and isolated fictional customer data only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const output = process.argv[2];
(async () => {
  const browser = await chromium.launch({channel:'chrome', headless:true});
  const page = await browser.newPage({viewport:{width:1920,height:1080}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  try {
    await page.setContent('<main id="app"></main>');
    await page.addScriptTag({path:path.join(root,'static/vendor/vue-3.5.40.global.prod.js')});
    await page.addScriptTag({path:path.join(root,'static/ui/home-workbench.js')});
    await page.addStyleTag({path:path.join(root,'static/ui/home-workbench.css')});
    await page.evaluate(() => {
      const {mixin,template}=ERPHomeWorkbench;
      const card=template.match(/<button v-for="g in vm\.homeStockGroupPage\.rows"[\s\S]*?<\/button>/)[0];
      window.testVm=Vue.createApp({
        data(){return {...mixin.data.call({$parent:null}),calls:0,isLargeUi:false,overview:{low_stock_warnings:[
          {customer_id:1,customer_label:'虚构客户甲',product_code:'CARD-A',product_name:'甲客户款号',draft_ready:true,suggested_new_requisition_sheet_quantity:10},
          {customer_id:2,customer_label:'虚构客户乙',product_code:'CARD-B',product_name:'乙客户款号',draft_ready:true,suggested_new_requisition_sheet_quantity:10}
        ]}};},
        computed:{...mixin.computed,vm(){return this;}},
        methods:{homeChooseCustomer:mixin.methods.homeChooseCustomer,homeChooseStockCustomer(id){this.calls++;mixin.methods.homeChooseStockCustomer.call(this,id);}},
        template:'<section class="home-workbench home-main"><div class="home-panel"><div class="home-queue">'+card+'</div><div class="home-detail"><h2>当前客户款号</h2><p v-for="r in homeStocksPage.rows" :key="r.product_code">{{r.product_code}} {{r.product_name}}</p></div></div></section>'
      }).mount('#app');
    });
    const cards=page.locator('.home-customer-row');
    for(const target of ['padding','.home-customer-name strong','.home-customer-name small','.amber','.home-button','Enter','Space']) {
      await page.evaluate(()=>{testVm.homeStockCustomer=1;testVm.calls=0;});
      const card=cards.nth(1);
      if(target==='padding') {const box=await card.boundingBox();await card.click({position:{x:box.width-3,y:box.height-3}});}
      else if(target==='Enter'||target==='Space') {await card.focus();await page.keyboard.press(target);}
      else await card.locator(target).click();
      assert.equal(await page.evaluate(()=>testVm.homeSelectedCustomer),2,target);
      assert.equal(await page.evaluate(()=>testVm.calls),1,target+' must fire once');
      assert.equal(await card.getAttribute('aria-pressed'),'true');
      assert.match(await page.locator('.home-detail').innerText(),/CARD-B/);
      assert.doesNotMatch(await page.locator('.home-detail').innerText(),/CARD-A/);
    }
    assert.equal(await cards.locator('button,a,input').count(),0);
    assert.deepEqual(errors,[]);
    if(output) {fs.mkdirSync(output,{recursive:true});await page.screenshot({path:path.join(output,'customer-card.png')});fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({status:'passed',browser:'installed Chrome',targets:7,single_action:true,fictional_data:true},null,2));}
    console.log('Chrome: card padding, name, count, status, label, Enter and Space select the customer exactly once.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
