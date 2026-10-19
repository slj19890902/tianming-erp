const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const {parseHTML}=require('linkedom');
const source=fs.readFileSync(require.resolve('../../static/ui/product-workbench.js'),'utf8');
const tick=()=>new Promise(r=>setImmediate(r));
const product={id:17,product_id:17,customer_id:3,customer_name:'测试客户',product_code:'C000017',product_name:'长片',specification:'100×80mm',inventory:{finished:{actual:200,available:100,reserved:100,unit:'片'}},drawings:{status:'available',items:[{id:'drawing:4',name:'工程图',kind:'image',preview_url:'/api/mobile/erp/products/17/drawings/drawing%3A4/preview',original_url:'/api/mobile/erp/products/17/drawings/drawing%3A4/original'}]}};
const inventoryFixture=()=>({summary:Object.fromEntries(['finished','semi_finished','processed_component'].map(k=>[k,{actual:0,available:0,reserved:0,unit:k==='finished'?'只':k==='semi_finished'?'张':'片'}])),groups:Object.fromEntries(['finished','semi_finished','processed_component'].map(k=>[k,{unit:k==='finished'?'只':k==='semi_finished'?'张':'片',positions:[]}]))});
const details={product,production:{report_length_mm:100,report_width_mm:80,process_steps:[{label:'模切',detail:'一模'}],molds:[],bom:[]},inventory:{summary:{...inventoryFixture().summary,...product.inventory},groups:inventoryFixture().groups,items:[{location_id:22,floor:'3F',mapped:true,url:'/warehouse.html?location_id=22',location_label:'A1-1-1',actual:200,available:100,reserved:100,unit:'片',inventory_type:'finished'}]},orders:{items:[]},actions:{can_requisition:false,stock_only:false}};
function harness(request){
 const {window}=parseHTML('<html><body><div id="test"></div></body></html>');
 const sandbox={document:window.document,URLSearchParams,AbortController,console,setTimeout,clearTimeout,FormData:class {constructor(f){this.f=f;}get(k){return this.f.querySelector(`[name="${k}"]`)?.value||null;}}};
 vm.createContext(sandbox);vm.runInContext(source,sandbox);
 const root=window.document.getElementById('test'),states=[],opened=[];
 const instance=sandbox.ERPProductWorkbench.mount({container:root,request,onState:s=>states.push(s),onOpen:s=>opened.push(s)});
 return {root,instance,states,opened,window,module:sandbox.ERPProductWorkbench};
}
function click(h,selector){const el=h.root.querySelector(selector);assert.ok(el,selector);el.dispatchEvent(new h.window.Event('click',{bubbles:true}));}
function fill(h,name,value){const e=h.root.querySelector(`[name="${name}"]`);assert.ok(e,name);if(e.tagName==='SELECT'){for(const o of e.options)o.removeAttribute('selected');e.querySelector(`[value="${value}"]`).setAttribute('selected','');}else e.value=value;e.dispatchEvent(new h.window.Event('change',{bubbles:true}));}
test('sheet discovery selects actual batch, filters recommendations and returns to the same inventory page',async()=>{
 const paths=[],sheet={lot_id:97,name:'空白库存片料',length:800,width:600,flute_type:'B',layer_count:3,material_code:'K=A',actual:10,available:8,reserved:2,unit:'张',location_label:'三楼原料区',reverse_source:{lot_id:97,length:800,width:600,flute_type:'B'}};
 const match={...product,match_score:100,report_material:'K=A',report_length_mm:800,report_width_mm:600,match_reasons:['尺寸一致'],check_items:[]};
 const h=harness(async path=>{paths.push(path);return path.includes('/products/')?details:path.includes('/sheets?')?{items:[sheet],total:13,page:2,page_size:12,has_more:false}:{items:[match],total:1,page:1,page_size:12,has_more:false};});
 await h.instance.query({length:800,width:600},2,'sheets');assert.match(paths.at(-1),/\/sheets\?/);assert.match(h.root.textContent,/可用 8 张/);assert.match(h.root.textContent,/三楼原料区/);
 click(h,'[data-sheet="0"]');await tick();let q=new URL(paths.at(-1),'http://localhost').searchParams;assert.equal(q.get('lot_id'),'97');assert.equal(q.get('processed_state'),null);
 for(const text of ['存货编码 / 产品','成品尺寸','报料材质','报料尺寸','匹配度','图纸','查看资料','100%','K=A'])assert.ok(h.root.textContent.includes(text),text);
 assert.equal(h.root.querySelector('.pw-match-details'),null);assert.doesNotMatch(h.root.textContent,/匹配说明|待核对|仅供查找|核对实际/);
 assert.equal(h.root.querySelector('[name="length"]').readOnly,true);assert.equal(h.root.querySelector('[name="flute_type"]').disabled,true);
 fill(h,'min_score','90');await tick();q=new URL(paths.at(-1),'http://localhost').searchParams;assert.equal(q.get('min_score'),'90');assert.equal(q.get('lot_id'),'97');assert.equal(q.get('flute_type'),'B');assert.equal(q.get('page'),'1');
 click(h,'[data-product="17"]');await tick();assert.equal(h.instance.snapshot().productId,17);click(h,'[data-back]');assert.ok(h.root.querySelector('.pw-reverse-results'));
 click(h,'[data-reselect-sheet]');await tick();q=new URL(paths.at(-1),'http://localhost').searchParams;assert.equal(q.get('page'),'2');assert.ok(h.root.querySelector('.pw-sheet-results'));h.instance.destroy();
});
test('free sheet search shows inferred layers with no material, state or layer input',async()=>{
 const paths=[],h=harness(async path=>{paths.push(path);return {items:[],total:0,page:1,page_size:12,has_more:false};});click(h,'[data-mode="reverse"]');
 for(const name of ['material_code','processed_state','layer_count'])assert.equal(h.root.querySelector(`[name="${name}"]`),null);
 fill(h,'length','800');fill(h,'width','600');fill(h,'flute_type','AB');assert.equal(h.root.querySelector('.pw-auto-layers').textContent,'5 层');
 h.root.querySelector('form').dispatchEvent(new h.window.Event('submit',{cancelable:true}));await tick();const q=new URL(paths.at(-1),'http://localhost').searchParams;
 assert.equal(q.get('flute_type'),'AB');for(const key of ['material_code','processed_state','layer_count'])assert.equal(q.get(key),null);h.instance.destroy();
});
test('late sheet list cannot overwrite newer recommendations',async()=>{
 let resolve;const pending=new Promise(r=>resolve=r);const h=harness(path=>path.includes('/sheets?')?pending:Promise.resolve({items:[{...product,match_score:100}],total:1,page:1,page_size:12,has_more:false}));
 const old=h.instance.query({length:800,width:600},1,'sheets');await h.instance.query({length:800,width:600,flute_type:'B'},1,'reverse');resolve({items:[],total:0,page:1,page_size:12,has_more:false});await old;assert.ok(h.root.querySelector('.pw-reverse-results'));assert.match(h.root.textContent,/C000017/);h.instance.destroy();
});
test('exact product retains C prefix, actual versus available, and authenticated drawing thumbnail',async()=>{
 const h=harness(async()=>({items:[product],total:1,page:1,page_size:12,has_more:false}));await h.instance.search();
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
 const h=harness(async()=>++calls===1?delayed:{items:[product],total:1,page:1,page_size:12,has_more:false});
 const old=h.instance.search();await h.instance.search();finish({items:[{...product,product_code:'OLD_ACCOUNT'}],total:1,page:1,page_size:12,has_more:false});await old;
 assert.doesNotMatch(h.root.textContent,/OLD_ACCOUNT/);assert.match(h.root.textContent,/C000017/);
 let finish2;const h2=harness(()=>new Promise(r=>finish2=r));const pending=h2.instance.search();h2.instance.destroy();finish2({items:[product],total:1,page:1,page_size:12,has_more:false});await pending;assert.equal(h2.root.textContent,'');h.instance.destroy();
});
test('failure is visible and does not masquerade as no inventory; retry details stays on product',async()=>{
 let calls=0;const h=harness(async()=>{if(++calls===1)throw Error('读取失败');return details;});await h.instance.show(17);
 assert.match(h.root.textContent,/读取失败/);assert.doesNotMatch(h.root.textContent,/没有实物库存/);click(h,'[data-retry]');await tick();assert.match(h.root.textContent,/C000017/);h.instance.destroy();
});
test('free-measurement search needs both axes and rejects unsafe drawing or arbitrary map URLs',async()=>{
 const h=harness(async()=>({items:[],total:0,page:1,page_size:12,has_more:false}));click(h,'[data-mode="reverse"]');
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
test('mold IDs use their own route and hidden shared-lot links cannot be reconstructed',()=>{
 const h=harness(async()=>details);
 assert.equal(h.module.locationUrl({mold_id:22,location_id:5,floor:'1F',map_url:'/mobile/mold-lookup?mold_id=22&readonly=1'}),'/mobile/mold-lookup?mold_id=22&readonly=1');
 assert.equal(h.module.locationUrl({mold_id:22,map_url:null}),null);
 assert.equal(h.module.locationUrl({...details.inventory.items[0],url:null}),null);
 assert.equal(h.module.locationUrl({...details.inventory.items[0],mapped:false}),null);h.instance.destroy();
});
test('actual processed stock reverses with original lot identity and retains measured conditions',async()=>{
 const paths=[],reverse_source={length:400,width:300,material_code:'TEST',flute_type:'B',layer_count:3,processed_state:'die_cut',lot_id:97};
 const h=harness(async path=>{paths.push(path);return path.includes('/reverse?')?{items:[],total:0,page:1,page_size:12,has_more:false,source:'actual_lot'}:{...details,inventory:{...details.inventory,items:[{...details.inventory.items[0],reverse_source}]}};});
 await h.instance.show(17);click(h,'[data-tab="inventory"]');click(h,'[data-reverse="0"]');await tick();
 const url=new URL(paths.at(-1),'http://localhost');assert.equal(url.searchParams.get('lot_id'),'97');assert.equal(url.searchParams.get('processed_state'),'die_cut');assert.equal(h.root.querySelector('[name="lot_id"]').value,'97');h.instance.destroy();
});
test('requisition purpose selection does not submit and existing orders remain available',async()=>{
 const h=harness(async()=>({...details,actions:{can_requisition:true}}));await h.instance.show(17);
 click(h,'[data-requisition]');assert.match(h.root.textContent,/本次报料用途/);click(h,'[data-purpose="order"]');
 assert.equal(h.instance.snapshot().tab,'orders');assert.equal(h.root.querySelector('.pw-purpose'),null);h.instance.destroy();
});
test('drawing failure offers a local retry without losing the selected product',async()=>{
 const h=harness(async()=>details);await h.instance.show(17);const img=h.root.querySelector('img');img.dispatchEvent(new h.window.Event('error'));
 assert.match(h.root.textContent,/图纸加载失败 · 重试/);click(h,'.pw-engineering button');assert.ok(h.root.querySelector('img'));assert.equal(h.instance.snapshot().productId,17);h.instance.destroy();
});

test('lifecycle distinguishes 2500 materials from 10000 theoretical outputs and pages delivery history',async()=>{
 const paths=[];const activity={page:1,has_more:true,items:[{source:'补库收料',status:'待生产',document:'TEST-PURCHASE',quantity:2500,unit:'张',physical_quantity:2500,theoretical_output:10000,output_unit:'只',factor:4,pieces_per_box:1,location:'原料堆放区003',frozen_spec:'795×457 mm'}],deliveries:[{document:'TEST-DELIVERY',status:'已送货',date:'2026-01-01',quantity:40,unit:'只',code:'80011965',specification:'原规格'}],related_products:[{product_id:18,code:'BOM18',name:'组合产品',relation:'组合产品'}]};
 const h=harness(async path=>{paths.push(path);return {...details,activity:{...activity,page:path.includes('activity_page=2')?2:1}};});await h.instance.show(17);
 assert.equal(h.instance.snapshot().tab,'activity');assert.match(h.root.textContent,/理论待产 10,000 只/);assert.match(h.root.textContent,/2,500 张/);assert.match(h.root.textContent,/原料堆放区003/);assert.match(h.root.textContent,/TEST-DELIVERY/);assert.match(h.root.textContent,/组合产品/);
 click(h,'[data-activity-page="2"]');await tick();assert.match(paths.at(-1),/activity_page=2/);assert.equal(h.instance.snapshot().tab,'activity');
 click(h,'[data-tab="inventory"]');assert.doesNotMatch(h.root.textContent,/理论待产 10,000/);h.instance.destroy();
});

test('search displays exact physical address and unmapped reason as escaped text',async()=>{
 const row={...product,inventory_positions:[{inventory_type:'processed_component',actual:30,available:20,unit:'片',location_id:2153,location_label:'三楼·G货架·2层·2格',map_issue:'地图关联待核 <img src=x>'}]};
 const h=harness(async()=>({items:[row],total:1,page:1,page_size:12,has_more:false}));await h.instance.search();
 assert.match(h.root.textContent,/三楼·G货架·2层·2格/);assert.match(h.root.textContent,/实存 30 片 \/ 可用 20/);
 assert.match(h.root.textContent,/地图关联待核 <img src=x>/);assert.equal(h.root.querySelector('img[src="x"]'),null);h.instance.destroy();
});

function responseReader(response){
 const sandbox={fetch:async()=>response};vm.createContext(sandbox);vm.runInContext(source,sandbox);
 return sandbox.ERPProductWorkbench.read('/api/product-workbench/products/17');
}
test('plain-text 500 and HTML gateway failures remain readable request errors',async()=>{
 for(const [status,body] of [[500,'Internal Server Error'],[502,'<html>Bad Gateway</html>']]){
  await assert.rejects(responseReader(new Response(body,{status})),error=>
   /产品资料读取失败/.test(error.message)&&error.message.includes(String(status))&&!/Unexpected token|not valid JSON/.test(error.message));
 }
});
test('reader preserves JSON permission details and rejects malformed success without an empty product',async()=>{
 await assert.rejects(responseReader(new Response(JSON.stringify({detail:'无权查看当前客户'}),{status:403})),/无权查看当前客户/);
 await assert.rejects(responseReader(new Response('Login',{status:401})),/登录已失效/);
 await assert.rejects(responseReader(new Response('<html>Wrong response</html>',{status:200})),/产品资料响应格式异常/);
 const result=await responseReader(new Response(JSON.stringify({product:{id:17}}),{status:200}));
 assert.equal(result.product.id,17);
});
test('reader preserves cancellation rather than replacing it with a response error',async()=>{
 const error=Object.assign(new Error('cancelled'),{name:'AbortError'});
 await assert.rejects(responseReader({ok:true,status:200,json:async()=>{throw error;}}),e=>e===error);
});
