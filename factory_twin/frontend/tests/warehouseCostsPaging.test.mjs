import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const source=fs.readFileSync(new URL('../../../static/factory-twin-assets/warehouse-costs.js',import.meta.url),'utf8');
class Element {
  constructor(tag='host'){this.tag=tag;this.children=[];this.attrs={};this.events={};this.value='';this.isConnected=true;this._text='';}
  attachShadow(){return this.shadowRoot=new Element('shadow');}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[...nodes];this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
  setAttribute(k,v){this.attrs[k]=String(v);}
  getAttribute(k){return this.attrs[k]??null;}
  hasAttribute(k){return k in this.attrs;}
  addEventListener(name,handler){this.events[name]=handler;}
  click(){if(!this.disabled)this.events.click?.();}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
  const calls=[],timers=new Map();let Constructor,nextTimer=0;
  const context={HTMLElement:Element,document:{createElement:t=>new Element(t)},AbortController,URLSearchParams,
    customElements:{get:()=>null,define:(_n,c)=>{Constructor=c;}},
    setTimeout:fn=>{timers.set(++nextTimer,fn);return nextTimer;},clearTimeout:id=>timers.delete(id),
    fetch:(url,options)=>new Promise(resolve=>calls.push({url,options,resolve:(body,status=200)=>resolve({ok:status===200,status,json:async()=>body})}))};
  vm.runInNewContext(source,context);
  const host=new Constructor();
  const nodes=()=>{const all=[];const visit=n=>{all.push(n);n.children.forEach(visit);};visit(host.shadowRoot);return all;};
  return {host,calls,nodes,find:(tag,text)=>nodes().find(n=>n.tag===tag&&(text===undefined||n.textContent===text)),
    timer(){const pending=[...timers.values()];timers.clear();pending.forEach(fn=>fn());}};
}
const page=(number=1,total=70,name='测试库存')=>({page:number,page_size:50,total,has_more:number*50<total,
  inventory_value:'999.00',missing_lots:1,basis:'按批次冻结',rows:[{lot_id:number,customer_name:'客户',product_code:'A001',product_name:name,
    quantity:3,unit:'boxes',unit_cost:'1.0000',inventory_value:'3.00',stock_date:'2026-09-12',lot_number:'批次',location_name:'三楼',label:'冻结成本',product_id:7}]});
async function mount(f,role='admin'){
  const done=f.host.load();f.calls.at(-1).resolve({user:{role}});await tick();
  if(role==='admin'||role==='boss'){f.calls.at(-1).resolve(page());await done;}else await done;
}

test('real cost component pages on server and does not replace whole-stock total with page value',async()=>{
  const f=fixture();await mount(f);
  assert.match(f.calls[1].url,/page=1&page_size=50/);
  assert.match(f.find('summary').textContent,/999.00/);
  assert.equal(f.find('button','上一页').disabled,true);
  assert.ok(f.find('button','成本依据'));
  f.find('button','下一页').click();f.find('button','下一页').click();assert.equal(f.calls.length,3);
  assert.match(f.calls[2].url,/page=2/);f.calls[2].resolve(page(2));await tick();
  assert.match(f.find('summary').textContent,/999.00/);
  assert.equal(f.find('button','下一页').disabled,true);
  assert.equal(f.find('button','上一页').disabled,false);
  f.find('button','上一页').click();assert.match(f.calls.at(-1).url,/page=1/);
});

test('typing resets page and invalidates in-flight data before debounce finishes',async()=>{
  const f=fixture();await mount(f);f.find('button','下一页').click();const old=f.calls.at(-1);
  const input=f.find('input');input.value='后页';input.events.input();
  assert.equal(old.options.signal.aborted,true);old.resolve(page(2,70,'过期产品'));await tick();
  assert.ok(!f.host.shadowRoot.textContent.includes('过期产品'));
  f.timer();assert.equal(new URL(f.calls.at(-1).url,'http://local').searchParams.get('keyword'),'后页');
  assert.match(f.calls.at(-1).url,/page=1/);f.calls.at(-1).resolve(page(1,1,'后页产品'));await tick();
  assert.ok(f.host.shadowRoot.textContent.includes('后页产品'));assert.equal(input.value,'后页');
});

test('location change and disconnect cannot render the old location cost',async()=>{
  const f=fixture();f.host.setAttribute('location-id','11');await mount(f);
  assert.equal(f.find('input'),undefined);assert.match(f.calls.at(-1).url,/location_id=11/);
  f.find('button','下一页').click();const old=f.calls.at(-1);
  f.host.setAttribute('location-id','12');const done=f.host.load();f.calls.at(-1).resolve({user:{role:'admin'}});await tick();
  assert.match(f.calls.at(-1).url,/location_id=12/);f.calls.at(-1).resolve(page(1,1,'新货位'));await done;
  old.resolve(page(2,70,'旧货位'));await tick();assert.ok(!f.host.shadowRoot.textContent.includes('旧货位'));
  f.host.disconnectedCallback();assert.equal(f.host.shadowRoot.textContent,'');
});

test('failure can retry same page, permission loss immediately removes all costs',async()=>{
  const f=fixture();await mount(f);f.find('button','下一页').click();f.calls.at(-1).resolve({},500);await tick();
  assert.ok(f.find('button','重试'));f.find('button','重试').click();assert.match(f.calls.at(-1).url,/page=2/);
  f.calls.at(-1).resolve({},403);await tick();assert.equal(f.host.shadowRoot.textContent,'');
});

test('employee never fetches prices; boss can view but cannot edit cost basis',async()=>{
  const f=fixture();await mount(f,'warehouse');assert.equal(f.calls.length,1);assert.equal(f.host.shadowRoot.textContent,'');
  const b=fixture();await mount(b,'boss');assert.match(b.find('summary').textContent,/999.00/);assert.equal(b.find('button','成本依据'),undefined);
});

test('stock removed on the last page returns to the last valid page',async()=>{
  const f=fixture();await mount(f);f.find('button','下一页').click();f.calls.at(-1).resolve({...page(2,1),rows:[]});await tick();
  assert.match(f.calls.at(-1).url,/page=1/);f.calls.at(-1).resolve(page(1,1));await tick();
  assert.equal(f.find('button','上一页').disabled,true);assert.equal(f.find('button','下一页').disabled,true);
});

test('all live desktop/mobile references use the new shared cost asset version',()=>{
  for(const path of ['factory_twin/frontend/warehouse-twin.html','static/factory-twin-assets/warehouse-twin.html',
    'static/factory-twin-assets/warehouse-costs.html','static/mobile_stocktake.html','static/mobile_erp.html','static/warehouse.html']){
    const html=fs.readFileSync(new URL('../../../'+path,import.meta.url),'utf8');
    assert.match(html,/warehouse-costs\.js\?v=20260916-bom438/);
    assert.ok(!html.includes('warehouse-costs.js?v=20260911-3'));
  }
});

test('assembled batch keeps material separate from standard labour and displays sets',async()=>{
  const f=fixture();await mount(f);f.find('button','下一页').click();
  const data=page(2);Object.assign(data.rows[0],{display_unit:'套',standard_labour_unit_cost:'0.5288',standard_total_unit_cost:'1.5288',standard_total_value:'4.59'});
  f.calls.at(-1).resolve(data);await tick();
  assert.match(f.host.shadowRoot.textContent,/3套 · 材料单价/);
  assert.match(f.host.shadowRoot.textContent,/标准组装人工 ¥0.5288\/套/);
  assert.match(f.host.shadowRoot.textContent,/含组装人工单价 ¥1.5288/);
  assert.match(f.find('summary').textContent,/材料成本金额 ¥999.00/);
});
