import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync,mkdtempSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';

test('isolated layout keeps map, elevation and inspector visible with compact cards and aligned floor/area',()=>{
  const css=['styles.css','warehouseTwin.css','warehouseWorkspace.css'].map(file=>readFileSync(new URL('../src/'+file,import.meta.url),'utf8')).join('\n');
  const dir=mkdtempSync(join(tmpdir(),'tm-workspace-layout-')),fixture=join(dir,'fixture.html');
  const cards=[1,2,3].map(n=>`<article class="shelf-product-card"><div class="shelf-product-summary"><span class="shelf-product-customer">驿力</span><span class="shelf-product-name">风机纸箱${n}</span><span class="shelf-specification">630×480mm</span></div><div class="shelf-product-code-row"><button class="shelf-product-label-button"><strong class="shelf-inventory-code">Z.001.00013${n}</strong></button><strong class="shelf-product-quantity">120只</strong><button class="shelf-product-details-toggle">1个批次 · 查看明细</button></div></article>`).join('');
  writeFileSync(fixture,`<!doctype html><meta charset="utf-8"><style>${css}</style><body class="warehouse-twin-page"><main id="warehouse-twin-root" class="warehouse-twin-shell">
  <section class="twin-toolbar"><div class="twin-floor-area-row"><label>楼层<select><option>3F</option></select></label><label>区域<select><option>南A1</option></select></label></div><div class="twin-top-search"><input placeholder="全仓查货"><button>查找</button></div><div class="twin-operation-modes"><button>移货</button><button>盘点</button><details class="twin-workspace-more"><summary>更多</summary></details></div></section>
  <section class="twin-workspace rack-focused context-open"><div class="twin-stage rack-focused"><div class="twin-map-pane">地图</div><section class="twin-rack-focus-panel twin-rack-stage"><header>货架正视图</header><div class="twin-rack-content"><div class="twin-elevation-shell"><div class="mold-rack-cell" style="height:250px;width:300px"><div class="shelf-cell-heading"><b>1格</b><small>混放 · 3款</small></div><div class="shelf-product-cards">${cards}</div></div></div></div></section></div><aside class="twin-inspector"><section class="twin-move-control-panel">移动货物</section><section class="twin-location-card"><warehouse-costs>费用折叠</warehouse-costs><div class="twin-location-card-title">当前货位</div><div class="twin-location-item">货物标签</div></section></aside></section></main><pre id="result"></pre>
  <script>
  const checks=[];const rect=s=>document.querySelector(s).getBoundingClientRect();
  const width=window.innerWidth;const map=rect('.twin-map-pane'),rack=rect('.twin-rack-focus-panel'),label=rect('.twin-inspector');checks.push({mapVisible:map.width>120&&map.height>100,labelVisible:label.width>250,rackVisible:rack.width>150&&rack.height>100,notOverlapping:width<=700?label.top>=rack.bottom-2:label.left>=rack.right-2,floorAreaSameRow:Math.abs(rect('.twin-floor-area-row label:first-child').top-rect('.twin-floor-area-row label:last-child').top)<2,productsFirst:rect('.twin-location-card').top<rect('.twin-move-control-panel').top,debug:{width,map:map.toJSON(),rack:rack.toJSON(),label:label.toJSON()}});
  const area=rect('.shelf-product-cards');const third=[...document.querySelectorAll('.shelf-product-card')][2].getBoundingClientRect();checks.push({threeCards:third.bottom<=area.bottom,debug:{area:area.toJSON(),third:third.toJSON()}});
  document.querySelector('#result').textContent=JSON.stringify(checks);
  </script></body>`);
  for(const width of [1440,1024,600]) {
  const result=spawnSync(process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--disable-gpu','--no-first-run','--force-device-scale-factor=1',`--window-size=${width},950`,'--user-data-dir='+join(dir,'profile-'+width),'--dump-dom',pathToFileURL(fixture).href],{encoding:'utf8',timeout:30000});
  assert.equal(result.status,0,result.error?.message||result.stderr);
  const checks=JSON.parse(result.stdout.match(/<pre id="result">([^<]+)/)?.[1]||'null');
  assert.ok(checks, result.stdout.slice(-1200));
  assert.ok(checks.every(row=>Object.entries(row).every(([key,value])=>key==='debug'||value===true)),JSON.stringify(checks));
  }
});
