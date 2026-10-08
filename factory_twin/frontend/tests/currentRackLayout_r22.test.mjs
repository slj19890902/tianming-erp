import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync,mkdtempSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';

const source=readFileSync(new URL('../src/WarehouseTwinApp.tsx',import.meta.url),'utf8');
const css=['styles.css','warehouseTwin.css','warehouseWorkspace.css'].map(file=>readFileSync(new URL('../src/'+file,import.meta.url),'utf8')).join('\n');
// This is an isolated CSS regression fixture, not real ERP/browser acceptance.
test('Current two-card rack layout keeps desktop panels separate and narrow-screen map and details reachable in their own states',()=>{
 assert.ok(source.includes('productGroups.slice(0, 2)'));assert.ok(source.includes('查看全部 {productGroups.length} 款'));
 const dir=mkdtempSync(join(tmpdir(),'tm-r22-current-layout-')),fixture=join(dir,'fixture.html');
 const cards=[1,2].map(n=>`<article class="shelf-product-card"><div class="shelf-product-summary"><span class="shelf-product-customer">虚构甲</span><span class="shelf-product-name">虚构纸箱${n}</span><span class="shelf-specification">630×480</span></div><div class="shelf-product-code-row"><button class="shelf-product-label-button"><strong class="shelf-inventory-code">001-TEST-${n}</strong></button><strong class="shelf-product-quantity">120只</strong></div></article>`).join('');
 writeFileSync(fixture,`<!doctype html><meta charset="utf-8"><style>${css}</style><body class="warehouse-twin-page"><main id="warehouse-twin-root" class="warehouse-twin-shell embedded-shell">
 <section class="twin-toolbar"><div class="twin-floor-area-row"><label>楼层<select><option>3F</option></select></label><label>区域<select><option>虚构A1</option></select></label></div><div class="twin-top-search"><input placeholder="全仓查货"><button>查找</button></div><details class="twin-workspace-more"><summary>更多</summary><div><button>全图复位</button></div></details></section>
 <section class="twin-workspace rack-focused context-collapsed"><aside class="twin-context-rail"><header>搜索结果</header></aside><div class="twin-stage rack-focused"><div class="twin-map-pane">地图</div><section class="twin-rack-focus-panel twin-rack-stage"><header>货架正视图</header><div class="twin-rack-content"><div class="twin-elevation-shell"><div class="twin-height-ruler"><b>2400mm</b></div><div class="twin-elevation-frame"><div class="twin-elevation-level"><span>第1层 · 1格</span><div><div class="mold-rack-cell"><div class="shelf-cell-heading"><b>1格</b><small>混放 · 3款</small></div><div class="shelf-product-cards">${cards}<button class="shelf-view-all-products">查看全部3款</button></div></div></div></div></div><div class="twin-width-ruler"><b>2600mm</b></div></div></div></section></div><aside class="twin-inspector"><section class="twin-move-control-panel">移动货物</section><section class="twin-location-card"><div class="twin-location-card-title">当前货位</div><div class="twin-location-item">完整货物标签</div></section></aside></section></main><pre id="result"></pre>
 <script>
 const narrow=window.innerWidth<=880,sections=[...document.querySelectorAll('.twin-inspector > section')];if(narrow)sections.forEach(e=>e.hidden=true);
 const rect=s=>document.querySelector(s).getBoundingClientRect(),map=rect('.twin-map-pane'),rack=rect('.twin-rack-focus-panel'),search=rect('.twin-top-search'),view=rect('.shelf-view-all-products');
 const button=document.querySelector('.shelf-view-all-products');let opened=false;button.onclick=()=>opened=true;const hit=document.elementFromPoint(view.x+view.width/2,view.y+view.height/2);if(hit===button)hit.click();
 const checks={searchVisible:search.width>=220&&search.height>=28,searchAboveMap:search.bottom<=map.top+2,mapVisible:map.width>100&&map.height>100,rackVisible:rack.width>150&&rack.height>100,twoCards:document.querySelectorAll('.shelf-product-card').length===2,viewAllOpened:opened,viewAllVisible:view.width>80&&view.height>=28,readable:[...document.querySelectorAll('.shelf-product-customer,.shelf-product-name,.shelf-specification,.shelf-inventory-code,.shelf-product-quantity')].every(e=>parseFloat(getComputedStyle(e).fontSize)>=10)};
 if(narrow)sections.forEach(e=>e.hidden=false);const inspector=rect('.twin-inspector');
 checks.inspectorVisible=inspector.width>=300&&inspector.height>30;checks.inspectorSeparate=narrow?getComputedStyle(document.querySelector('.twin-stage')).display==='none':inspector.left>=rack.right-2;checks.productsBeforeMove=rect('.twin-location-card').top<rect('.twin-move-control-panel').top;
 if(narrow){sections.forEach(e=>e.hidden=true);checks.mapRestored=rect('.twin-map-pane').width>100&&getComputedStyle(document.querySelector('.twin-stage')).display!=='none';}
 document.querySelector('#result').textContent=JSON.stringify(checks);
 </script></body>`);
 for(const [width,height] of [[1920,1080],[1440,950],[1024,950],[600,950]]){
  const result=spawnSync(process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',['--headless=new','--disable-gpu','--no-first-run','--force-device-scale-factor=1',`--window-size=${width},${height}`,'--user-data-dir='+join(dir,'profile-'+width),'--dump-dom',pathToFileURL(fixture).href],{encoding:'utf8',timeout:30000});
  assert.equal(result.status,0,result.error?.message||result.stderr);
  const checks=JSON.parse(result.stdout.match(/<pre id="result">([^<]+)/)?.[1]||'null');assert.ok(checks);
  assert.ok(Object.values(checks).every(Boolean),JSON.stringify({width,checks}));
 }
});
