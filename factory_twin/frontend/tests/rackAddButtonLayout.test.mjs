import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, writeFileSync, mkdtempSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';

const css = readFileSync(new URL('../src/warehouseTwin.css', import.meta.url), 'utf8');
test('Chrome: rack add button has a full-size hitbox and does not overlap products', () => {
  const dir = mkdtempSync(join(tmpdir(), 'tm-rack-hitbox-'));
  const fixture = join(dir, 'fixture.html');
  writeFileSync(fixture, `<meta charset="utf-8"><style>${css}</style>
    <section class="twin-rack-focus-panel twin-rack-stage"><div class="twin-elevation-frame" style="height:220px">
    <div class="twin-elevation-level"><span>第1层 · 2格</span><div>
    <section class="mold-rack-cell occupied can-add-product" style="height:150px"><div class="shelf-cell-heading">1格 · 有货</div>
    <button type="button" class="shelf-cell-add-product">＋ 添加货物</button>
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
      results.push(b.height>=44 && text.top>=b.top && text.bottom<=b.bottom && products.top>=b.bottom && hit===button);
      if(hit===button)hit.click();
    }
    document.querySelector('#result').textContent=results.every(Boolean)&&clicks===3?'HITBOX_PASS':JSON.stringify({results,clicks,height:button.getBoundingClientRect().height});
    </script>`);
  const chrome = process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
  const result = spawnSync(chrome, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--window-size=1200,900', '--user-data-dir='+join(dir,'profile'), '--dump-dom', pathToFileURL(fixture).href], {encoding:'utf8',timeout:30000});
  assert.equal(result.status,0,result.error?.message || result.stderr);
  assert.ok(result.stdout.includes('<pre id="result">HITBOX_PASS</pre>'),result.stdout.slice(-1500));
});
