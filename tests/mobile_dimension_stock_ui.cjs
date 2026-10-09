const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const script=fs.readFileSync('static/ui/mobile-dimension-stock.js','utf8');
new vm.Script(script);
const page=fs.readFileSync('static/mobile_erp.html','utf8');
for(const match of page.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(match[1]);
const elements=new Map();
class Element {
 constructor(id){this.id=id;this.value='';this.hidden=false;this.disabled=false;this.textContent='';this.dataset={};}
 set innerHTML(value){this.html=value;for(const m of value.matchAll(/id="([^"]+)"/g))elements.set(m[1],new Element(m[1]));}
 get innerHTML(){return this.html;}
 querySelector(s){return elements.get(s.slice(1));}
 querySelectorAll(){return this.buttons||[];}
 replaceChildren(){this.html='';}
 scrollIntoView(){}
 reset(){}
}
const root=new Element('dimensionStock');elements.set(root.id,root);
const storage=new Map(),requests=[],posts=[];
let failPost=false;
const item={id:1,version:3,location_id:6,address_version:1,code:'A1',name:'<纸箱>',customer:'甲',dimensions:[500,300,200],unit:'只',available:10,reserved:2,physical:12,damaged:0,status:'active',can_take:true,location_name:'三楼 北H2-10'};
const ctx=vm.createContext({window:{},document:{getElementById:id=>elements.get(id)},URLSearchParams,Uint8Array,crypto:require('node:crypto').webcrypto,sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},console});
vm.runInContext(script,ctx);
const get=id=>elements.get(id);
(async()=>{
 ctx.window.TmDimensionStock.mount({userId:1,apiGet:async url=>{
   requests.push(url);
   if(url.endsWith('/history'))return {items:[]};
   if(url.endsWith('/detail'))return {item,can_execute:true};
   get('dsResults').buttons=[Object.assign(new Element('result'),{dataset:{index:'0'}})];
   return {total:61,items:[item],can_execute:true};
 },apiPost:async(url,body)=>{posts.push({url,body:JSON.parse(JSON.stringify(body))});if(failPost)throw new Error('网络中断');return {quantity:body.quantity,movement_id:1,replayed:false};}});
 assert.equal(ctx.window.TmDimensionStock.digits('12x345678'),'12345');
 get('dsKind').value='box';get('dsFlute').value='';
 for(const axis of ['length','width','height']){get('ds-'+axis).value=axis==='length'?'500':'';get('ds-'+axis+'-op').value='ge';}
 get('dsForm').onsubmit({preventDefault(){}});await new Promise(setImmediate);
 assert.match(requests[0],/kind=box/);assert.match(requests[0],/length=500/);assert.match(get('dsResults').html,/&lt;纸箱&gt;/);
 get('dsResults').buttons[0].onclick();await new Promise(setImmediate);
 assert.match(get('dsDetail').html,/三楼 北H2-10/);
 get('dsCancel').onclick();assert.equal(posts.length,0);assert.equal(get('dsDetail').hidden,true);
 get('dsResults').buttons[0].onclick();await new Promise(setImmediate);
 get('dsQuantity').value='11';get('dsPurpose').value='sample';await get('dsTake').onclick();assert.equal(posts.length,0);
 get('dsQuantity').value='3';failPost=true;await get('dsTake').onclick();assert.equal(posts.length,1);assert.equal(storage.size,1);
 assert.equal(get('dsTake').disabled,false);failPost=false;await get('dsTake').onclick();
 assert.equal(posts.length,2);assert.deepEqual(posts[0].body,posts[1].body);assert.equal(storage.size,0);
 assert.equal(get('dsDetail').hidden,true);
 assert.match(get('dsStatus').textContent,/取用已记录/);
 console.log('UI syntax, numeric filter, result escaping, location, cancel, quantity guard, uncertain retry and refresh passed');
})().catch(error=>{console.error(error);process.exitCode=1});
