const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map(), requests=[], posts=[], tick=()=>new Promise(setImmediate);
class Element {
  constructor(id){Object.assign(this,{id,value:'',hidden:false,disabled:false,dataset:{},textContent:''});}
  set innerHTML(value){this.html=value;for(const m of value.matchAll(/id="([^"]+)"/g))nodes.set(m[1],new Element(m[1]));}
  get innerHTML(){return this.html || '';}
  querySelector(s){return nodes.get(s.slice(1));} querySelectorAll(){return [];}
  replaceChildren(){this.html='';} scrollIntoView(){} reset(){}
}
const root=new Element('dimensionStock');nodes.set(root.id,root);const get=id=>nodes.get(id);
let fail=false, defer=null;
const product={product_id:7,code:'000380',customer:'甲 <客户>',name:'空白纸箱',dimensions:[385,337,420],dimension_deltas:[5,-3,0],has_stock:false,match_exact:false,can_take:false};
const result={items:[],total:0,product_matches:[product],product_total:21,near_mm:10};
const context=vm.createContext({window:{},document:{getElementById:get},URLSearchParams,Uint8Array,crypto:require('node:crypto').webcrypto,sessionStorage:{getItem:()=>null},console});
vm.runInContext(fs.readFileSync('static/ui/mobile-dimension-stock.js','utf8'),context);
const mount=()=>context.window.TmDimensionStock.mount({userId:1,apiGet:async url=>{requests.push(url);if(fail)throw Error('网络中断');if(defer)await defer;return result;},apiPost:async(...args)=>posts.push(args)});
function inputs(){get('dsKind').value='box';get('dsFlute').value='';get('dsTolerance').value='10';for(const [axis,value] of [['length','380'],['width','340'],['height','420']]){get('ds-'+axis).value=value;get('ds-'+axis+'-op').value='near';}}
async function search(){get('dsForm').onsubmit({preventDefault(){}});await tick();}
(async()=>{
  mount();inputs();await search();
  assert.match(requests[0],/include_products=true/);assert.match(requests[0],/height=420/);assert.match(requests[0],/near_mm=10/);
  assert.equal(get('dsProducts').hidden,false);assert.match(get('dsProductResults').html,/000380/);
  assert.match(get('dsProductResults').html,/甲 &lt;客户&gt;/);assert.match(get('dsProductResults').html,/暂无实存/);
  assert.match(get('dsProductResults').html,/长\+5 mm · 宽-3 mm · 高0 mm/);
  assert.doesNotMatch(get('dsProductResults').html,/<button|dsTake|location_id/);assert.equal(posts.length,0);
  get('dsProductNext').onclick();await tick();assert.match(requests.at(-1),/product_offset=20/);assert.equal(get('dsProductNext').disabled,true);
  get('dsProductPrev').onclick();await tick();assert.match(requests.at(-1),/product_offset=0/);
  get('ds-height').value='';await search();assert.match(get('dsProductStatus').textContent,/填全长、宽、高/);
  // A newer query/clear owns the page even if the prior request completes late.
  inputs();let resolve;defer=new Promise(r=>resolve=r);await search();
  get('dsClear').onclick();resolve();defer=null;await tick();assert.equal(get('dsProducts').hidden,true);assert.equal(get('dsProductResults').html,'');
  inputs();fail=true;await search();assert.equal(get('dsProducts').hidden,true);assert.match(get('dsStatus').textContent,/网络中断/);fail=false;
  await search();assert.equal(get('dsProducts').hidden,false);
  get('dsKind').value='board';get('dsKind').onchange();assert.equal(get('ds-height-row').hidden,true);assert.equal(get('ds-length-op').value,'ge');
  await search();assert.equal(get('dsProducts').hidden,true);assert.equal(posts.length,0);
  console.log('PASS: product identity, dimensions/deltas, no-stock label, escaping, no take, pagination, incomplete input, stale response, failure/retry, board mode');
})().catch(error=>{console.error(error);process.exitCode=1});
