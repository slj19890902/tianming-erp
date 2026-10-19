const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const {parseHTML}=require('linkedom');
const source=fs.readFileSync(require.resolve('../../static/ui/stock-origins.js'),'utf8');
const tick=()=>new Promise(r=>setImmediate(r));
const fixture={summary:{actual:1,available:1,reserved:0},unit:'片',sources:{stocktake:{actual:1},replenishment:{actual:0},order:{actual:0},other:{actual:0}},items:[{lot_id:1574,lot_number:'FG-TEST',source_kind:'stocktake',source_label:'盘点入库',actual:1,available:1,reserved:0,unit:'片',detail_allowed:true,location_label:'货H5',entered_at:'2026-10-17T13:57:31Z'}],total:1,page:1,has_more:false};
function setup(){const {window}=parseHTML('<html><body><div id="original"><input value="80012139"><button id="row">1片</button></div></body></html>');const sandbox={window,document:window.document,setTimeout,clearTimeout,console,AbortController,URLSearchParams};vm.runInNewContext(source,sandbox);return {window,root:window.document.body,module:window.ERPStockOrigins};}
function click(h,s){const b=h.root.querySelector(s);assert.ok(b,s);b.dispatchEvent(new h.window.Event('click',{bubbles:true}));}
test('stock number dialog keeps original row, filters and quantity while viewing batch and closing',async()=>{
 const h=setup(),original=h.root.querySelector('#original'),calls=[];
 h.module.open({productId:3886,label:'海外 <script>',request:async path=>{calls.push(path);return path.endsWith('/1574')?{batch:fixture.items[0],source_note:'无报料收料',events:[{label:'盘点新增',at:'2026-10-17T13:57:31Z',quantity:1,unit:'片',note:'<img src=x onerror=1>'}]}:fixture;}});await tick();
 assert.equal(h.root.querySelector('#original'),original);assert.match(h.root.textContent,/盘点入库/);assert.equal(h.root.querySelector('script'),null);
 click(h,'[data-so-category="stocktake"]');await tick();assert.match(calls.at(-1),/source_kind=stocktake/);
 click(h,'[data-so-batch="1574"]');await tick();assert.equal(h.root.querySelectorAll('.so-timeline li').length,1);assert.equal(h.root.querySelector('[onerror]'),null);
 click(h,'[data-so-back]');assert.equal(h.root.querySelector('[aria-pressed=true]').dataset.soCategory,'stocktake');click(h,'[data-so-close]');
 assert.equal(h.root.querySelector('.stock-origins'),null);assert.equal(h.root.querySelector('#original'),original);assert.equal(original.querySelector('input').value,'80012139');
});
test('late response after close or newer category cannot revive stale private data',async()=>{
 const h=setup();let resolve;let count=0;
 h.module.open({productId:3886,request:()=>++count===1?new Promise(r=>resolve=r):Promise.resolve({...fixture,items:[]})});await tick();
 h.module.close();resolve(fixture);await tick();assert.equal(h.root.querySelector('.stock-origins'),null);
 let finish;h.module.open({productId:3886,request:()=>new Promise(r=>finish=r)});await tick();
 h.module.open({productId:3886,request:async()=>({...fixture,items:[]})});await tick();finish(fixture);await tick();assert.equal(h.root.querySelectorAll('[data-so-batch]').length,0);h.module.close();
});
test('error is not zero inventory and retry recovers; shared stock is separate',async()=>{
 const h=setup();let calls=0;
 h.module.open({productId:3886,request:async()=>{if(++calls===1)throw Error('无权读取');return {...fixture,shared_available:290,shared_total:1,shared_items:[{source_label:'盘点入库',actual:290,unit:'片',location_label:'原货位',detail_allowed:false}]};}});await tick();
 assert.match(h.root.textContent,/无权读取/);assert.doesNotMatch(h.root.textContent,/没有匹配批次/);click(h,'[data-so-retry]');await tick();
 assert.match(h.root.querySelector('.so-summary').textContent,/1/);assert.match(h.root.querySelector('.so-shared').textContent,/290/);assert.equal(h.root.querySelectorAll('[data-so-batch]').length,1);h.module.close();
});
test('closing restores viewport and scrolling panel while account change does not move the new page',async()=>{
 const h=setup(),row=h.root.querySelector('#row'),panel=h.root.querySelector('#original'),moves=[];
 Object.defineProperty(h.window.document,'activeElement',{value:row,configurable:true});
 h.window.scrollX=0;h.window.scrollY=480;h.window.scrollTo=p=>moves.push(p);
 panel.scrollTop=125;panel.scrollLeft=6;
 h.module.open({productId:3886,request:async()=>fixture});await tick();panel.scrollTop=900;
 h.module.close();assert.equal(panel.scrollTop,125);assert.equal(panel.scrollLeft,6);assert.equal(moves.at(-1).top,480);
 let current=true;h.module.open({productId:3886,isCurrent:()=>current,request:async()=>fixture});await tick();current=false;panel.scrollTop=200;const n=moves.length;
 h.module.close();assert.equal(panel.scrollTop,200);assert.equal(moves.length,n);
});
