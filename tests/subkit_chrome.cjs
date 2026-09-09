// Real Chrome rendering of the exact Common Box BOM heading (no formal API writes).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
  if (match[1].trim()) new vm.Script(match[1]);
}
const start = html.indexOf('<div class="bom-editor-heading">');
const end = html.indexOf('<div class="field" style="max-width:340px', start);
const heading = html.slice(start, end);
// Only document styles, not a supplier-print HTML string embedded in script.
const css = [...html.slice(0, html.indexOf('</head>')).matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n');
(async () => {
  const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  try {
    const page = await browser.newPage();
    const errors=[];
    page.on('pageerror', e=>errors.push(e.message));
    await page.setContent(`<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>${css} body{padding:12px;background:white;min-width:0}</style><div id="app" class="bom-editor-panel">${heading}</div>`);
    await page.addScriptTag({path:path.join(root,'static/vendor/vue-3.5.40.global.prod.js')});
    await page.evaluate(()=>{
      window.editor = Vue.createApp({data(){return {
        productForm:{id:3765,is_virtual_composite_parent:false,composite_fulfillment_mode:'parent_delivery',combination_mode:'parent_priced_set'},
        bomEditor:{enabled:true,subkit:null}
      }}}).mount('#app');
    });
    await page.getByRole('button',{name:'组成子套件',exact:true}).click();
    await page.getByLabel('子套件名称').fill('000148内衬');
    await page.getByLabel('每父件子套件数').fill('1');
    assert.equal(await page.evaluate(()=>window.editor.bomEditor.subkit.name),'000148内衬');
    for (const width of [1100,390]) {
      await page.setViewportSize({width,height:500});
      await page.screenshot({path:`D:/tm-uat/bom-subkit-auto-20260909/chrome-bom-${width}.png`});
      const overflow = await page.evaluate(()=>[...document.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().right > innerWidth).map(e=>({tag:e.tagName,cls:e.className,width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right})));
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),`overflow at ${width}: ${JSON.stringify(overflow)}`);
      assert.equal(await page.getByLabel('子套件名称').isVisible(),true);
    }
    assert.deepEqual(errors,[]);
    console.log('Chrome BOM heading and inline JavaScript: passed (1100px, 390px).');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
