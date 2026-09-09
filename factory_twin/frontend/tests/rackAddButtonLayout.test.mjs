import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, writeFileSync, mkdtempSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';

const css = readFileSync(new URL('../src/warehouseTwin.css', import.meta.url), 'utf8');
test('Chrome: compact add button stays beside the cell number and leaves room for products', () => {
  const dir = mkdtempSync(join(tmpdir(), 'tm-rack-hitbox-'));
  const fixture = join(dir, 'fixture.html');
  writeFileSync(fixture, `<meta charset="utf-8"><style>${css}</style>
    <section class="twin-rack-focus-panel twin-rack-stage"><div class="twin-elevation-frame" style="height:220px">
    <div class="twin-elevation-level"><span>第1层 · 2格</span><div>
    <section class="mold-rack-cell occupied can-add-product" style="height:150px"><div class="shelf-cell-heading"><button class="mold-rack-cell-summary"><b>1格</b></button>
    <button type="button" class="shelf-cell-add-product">＋ 添加货物</button>
    <span class="shelf-cell-status">有货 · 26只 · 客户</span><button class="shelf-position-print">打印货位</button></div>
    <div class="shelf-product-cards"><small class="shelf-cell-kind">混放 · 3款</small><article class="shelf-product-card"><button>TEST-001<br>原有产品<br>规格信息<br>批次信息</button></article><article class="shelf-product-card"><button>TEST-002<br>原有产品<br>规格信息</button></article><article class="shelf-product-card"><button>TEST-003<br>原有产品</button></article></div>
    </section></div></div></div></section><pre id="result"></pre>
    <script>
    const results=[]; let clicks=0;
    const button=document.querySelector('.shelf-cell-add-product');button.onclick=()=>clicks++;
    for(const width of [920,390,240]) {
      document.querySelector('section').style.width=width+'px';
      const b=button.getBoundingClientRect(), products=document.querySelector('.shelf-product-cards').getBoundingClientRect();
      const range=document.createRange();range.selectNodeContents(button);const text=range.getBoundingClientRect();
      const x=b.x+b.width/2,y=b.y+b.height/2;
      const hit=document.elementFromPoint(x,y);
      const number=document.querySelector('.mold-rack-cell-summary').getBoundingClientRect();
      results.push(b.height>=28 && b.height<=36 && b.width<110 && b.left>=number.right && Math.abs(b.top-number.top)<15 && text.top>=b.top && text.bottom<=b.bottom && products.top>=b.bottom && products.height>=90 && hit===button);
      if(hit===button)hit.click();
    }
    document.querySelector('#result').textContent=results.every(Boolean)&&clicks===3?'HITBOX_PASS':JSON.stringify({results,clicks,height:button.getBoundingClientRect().height});
    </script>`);
  const chrome = process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
  const result = spawnSync(chrome, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--window-size=1200,900', '--user-data-dir='+join(dir,'profile'), '--dump-dom', pathToFileURL(fixture).href], {encoding:'utf8',timeout:30000});
  assert.equal(result.status,0,result.error?.message || result.stderr);
  assert.ok(result.stdout.includes('<pre id="result">HITBOX_PASS</pre>'),result.stdout.slice(-1500));
});

test('Chrome: product code and details share a row, wrap on narrow cards and have separate hitboxes', () => {
  const dir = mkdtempSync(join(tmpdir(), 'tm-product-row-'));
  const fixture = join(dir, 'fixture.html');
  writeFileSync(fixture, `<meta charset="utf-8"><style>${css}</style>
  <article class="shelf-product-card" style="width:450px"><small class="shelf-product-customer">客户 · 26只</small>
  <div class="shelf-product-code-row"><button class="shelf-product-label-button"><strong class="shelf-inventory-code">Z.001.000096</strong></button><button class="shelf-product-details-toggle">2个批次 · 查看明细</button></div>
  <div class="shelf-product-description">产品名称 · 400×300</div><div class="shelf-product-details" hidden>批次1、批次2</div></article><pre id="result"></pre>
  <script>
  const code=document.querySelector('.shelf-product-label-button'),toggle=document.querySelector('.shelf-product-details-toggle'),details=document.querySelector('.shelf-product-details'),card=document.querySelector('article');let labels=0;code.onclick=()=>labels++;toggle.onclick=()=>details.hidden=!details.hidden;
  const checks=[];
  for(const width of [450,300,180]) {
    card.style.width=width+'px';const c=code.getBoundingClientRect(),t=toggle.getBoundingClientRect(),r=card.getBoundingClientRect();
    checks.push(width>=300 ? Math.abs(c.top-t.top)<8 && c.right<=t.left : c.bottom<=t.top);
    checks.push(t.right<=r.right && c.right<=r.right);
    document.elementFromPoint(c.x+c.width/2,c.y+c.height/2).click();checks.push(details.hidden);
    document.elementFromPoint(t.x+t.width/2,t.y+t.height/2).click();checks.push(!details.hidden);toggle.click();
  }
  document.querySelector('#result').textContent=checks.every(Boolean)&&labels===3?'INLINE_PASS':JSON.stringify({checks,labels});
  </script>`);
  const result=spawnSync(process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--disable-gpu','--no-first-run','--user-data-dir='+join(dir,'profile'),'--dump-dom',pathToFileURL(fixture).href],{encoding:'utf8',timeout:30000});
  assert.equal(result.status,0,result.error?.message || result.stderr);
  assert.ok(result.stdout.includes('<pre id="result">INLINE_PASS</pre>'),result.stdout.slice(-1500));
});
