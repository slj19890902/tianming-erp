const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const {parseHTML}=require('linkedom');
const source=fs.readFileSync(require.resolve('../../static/ui/product-workbench.js'),'utf8');
const tick=()=>new Promise(r=>setImmediate(r));
const product={id:17,product_id:17,customer_id:3,customer_name:'测试客户',product_code:'C000017',product_name:'长片',specification:'100×80mm',inventory:{finished:{actual:200,available:100,reserved:100,unit:'片'}},drawings:{status:'available',items:[{id:'drawing:4',name:'工程图',kind:'image',preview_url:'/api/mobile/erp/products/17/drawings/drawing%3A4/preview',original_url:'/api/mobile/erp/products/17/drawings/drawing%3A4/original'}]}};
const details={product,production:{report_length_mm:100,report_width_mm:80,process_steps:[{label:'模切',detail:'一模'}],molds:[],bom:[]},inventory:{summary:product.inventory,items:[{location_id:22,floor:'3F',mapped:true,url:'/warehouse.html?location_id=22',location_label:'A1-1-1',actual:200,available:100,reserved:100,unit:'片',inventory_type:'finished'}]},orders:{items:[]},actions:{can_requisition:false,stock_only:false}};
function harness(request){
 const {window}=parseHTML('<html><body><div id="test"></div></body></html>');
 const sandbox={document:window.document,URLSearchParams,AbortController,console,setTimeout,clearTimeout,FormData:class {constructor(f){this.f=f;}get(k){return this.f.querySelector(`[name="${k}"]`)?.value||null;}}};
 vm.createContext(sandbox);vm.runInContext(source,sandbox);
 const root=window.document.getElementById('test'),states=[],opened=[];
 const instance=sandbox.ERPProductWorkbench.mount({container:root,request,onState:s=>states.push(s),onOpen:s=>opened.push(s)});
 return {root,instance,states,opened,window,module:sandbox.ERPProductWorkbench};
}
function click(h,selector){const el=h.root.querySelector(selector);assert.ok(el,selector);el.dispatchEvent(new h.window.Event('click',{bubbles:true}));}
test('exact product retains C prefix, actual versus available, and authenticated drawing thumbnail',async()=>{
 const h=harness(async()=>({items:[product],total:1,has_more:false}));await h.instance.search();
 assert.match(h.root.textContent,/C000017/);assert.match(h.root.textContent,/200 片/);assert.match(h.root.textContent,/可用 100/);
 assert.equal(h.root.querySelector('img').getAttribute('src'),product.drawings.items[0].preview_url);
 assert.equal(h.root.querySelector('.pw-code').textContent,'C000017');h.instance.destroy();
});
test('product details and precise map return preserve selected product/tab without a business write',async()=>{
 const paths=[];const h=harness(async path=>{paths.push(path);return details;});await h.instance.show(17);
 click(h,'[data-tab="inventory"]');click(h,'[data-location="0"]');
 const url=new URL(h.root.querySelector('iframe').getAttribute('src'),'http://localhost');assert.equal(url.searchParams.get('location_id'),'22');assert.equal(url.searchParams.get('readonly'),'1');
 click(h,'[data-close-map]');assert.equal(h.root.querySelector('iframe'),null);assert.equal(h.instance.snapshot().productId,17);assert.equal(h.instance.snapshot().tab,'inventory');assert.equal(paths.length,1);h.instance.destroy();
});
test('late search and destroyed account responses cannot replace new data',async()=>{
 let finish;const delayed=new Promise(r=>finish=r);let calls=0;
 const h=harness(async()=>++calls===1?delayed:{items:[product],total:1,has_more:false});
 const old=h.instance.search();await h.instance.search();finish({items:[{...product,product_code:'OLD_ACCOUNT'}],total:1});await old;
 assert.doesNotMatch(h.root.textContent,/OLD_ACCOUNT/);assert.match(h.root.textContent,/C000017/);
 let finish2;const h2=harness(()=>new Promise(r=>finish2=r));const pending=h2.instance.search();h2.instance.destroy();finish2({items:[product],total:1});await pending;assert.equal(h2.root.textContent,'');h.instance.destroy();
});
test('failure is visible and does not masquerade as no inventory; retry details stays on product',async()=>{
 let calls=0;const h=harness(async()=>{if(++calls===1)throw Error('读取失败');return details;});await h.instance.show(17);
 assert.match(h.root.textContent,/读取失败/);assert.doesNotMatch(h.root.textContent,/没有实物库存/);click(h,'[data-retry]');await tick();assert.match(h.root.textContent,/C000017/);h.instance.destroy();
});
test('free-measurement search needs both axes and rejects unsafe drawing or arbitrary map URLs',async()=>{
 const h=harness(async()=>({items:[],total:0}));click(h,'[data-mode="reverse"]');
 const form=h.root.querySelector('form');form.dispatchEvent(new h.window.Event('submit',{cancelable:true}));assert.match(h.root.textContent,/实测长、宽/);
 assert.equal(h.module.locationUrl({location_id:22,floor:'evil',url:'https://evil.test'}),null);
 assert.equal(h.module.drawingItems({...product,drawings:{items:[{id:'x',preview_url:'https://evil.test',original_url:'javascript:x'}]}}).length,0);h.instance.destroy();
});
test('history query reloads separately and product content never becomes executable HTML',async()=>{
 const paths=[];const h=harness(async path=>{paths.push(path);return {...details,product:{...product,product_name:'<img src=x onerror=alert(1)>'}};});await h.instance.show(17);
 assert.match(h.root.textContent,/<img src=x/);assert.equal(h.root.querySelector('[onerror]'),null);
 click(h,'[data-tab="orders"]');const c=h.root.querySelector('[data-history]');c.checked=true;c.dispatchEvent(new h.window.Event('change'));await tick();
 assert.match(paths.at(-1),/include_history=true/);assert.equal(h.instance.snapshot().tab,'orders');h.instance.destroy();
});
test('desktop and mobile entrypoints load the same search assets',()=>{
 for(const file of ['index.html','mobile_erp.html']){const html=fs.readFileSync(require.resolve('../../static/'+file),'utf8');assert.match(html,/ui\/product-workbench\.js/);assert.match(html,/ui\/product-workbench\.css/);}
 const mobile=fs.readFileSync(require.resolve('../../static/mobile_erp.html'),'utf8');assert.match(mobile,/mountMobile/);assert.match(mobile,/destroyMobile/);
});
