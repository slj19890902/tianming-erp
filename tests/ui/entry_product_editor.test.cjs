const test=require('node:test'), assert=require('node:assert/strict'), vm=require('node:vm'), fs=require('node:fs');
const source=fs.readFileSync('static/ui/entry-product-editor.js','utf8');
class Element{
  constructor(tag,text=''){this.tagName=tag.toUpperCase();this.textContent=text;this.value='';this.children=[];this.style={};}
  append(...nodes){this.children.push(...nodes);}
  showModal(){} close(){} remove(){}
  querySelectorAll(){return this.children.flatMap(n=>['INPUT','SELECT'].includes(n.tagName)?[n]:n.querySelectorAll());}
}
test('entry editor freezes identity, prevents double submit and preserves saved result after refresh failure',async()=>{
  const body=new Element('body'), calls=[];let releaseWrite, costReads=0;
  const initial={id:9,version:3,product_code:'BOX',product_name:'箱',customer_id:2,report_length_mm:1000,
    material_id:4,flute_type:'B',layer_count:3,splice_mode:'single',sale_unit_price:'7.00',cost_unit_price:'2.00'};
  const fetch=async(url,options={})=>{
    calls.push({url,...options});let data;
    if(options.method==='PUT'){await new Promise(resolve=>releaseWrite=resolve);data={...initial,version:4};}
    else if(url.endsWith('update-preview'))data={can_update:true,changes:{report_length_mm:{before:1000,after:1100}},confirmation_token:'version-bound',warnings:[]};
    else if(url.includes('entry-preview')){if(++costReads===2)throw new Error('refresh failed');data={unit_cost:'2',note:'current'};}
    else if(url.endsWith('/materials'))data=[{id:4,code:'K',supplier_name:'供应商'}];
    else data=initial;
    return {ok:true,json:async()=>data};
  };
  const window={confirm:()=>true};
  vm.runInNewContext(source,{window,document:{body,createElement:tag=>new Element(tag)},fetch,
    Option:class extends Element{constructor(text,value){super('option',text);this.value=value;}}});
  const result=window.TMEntryProduct.open({productId:9});
  for(let i=0;i<15;i++)await Promise.resolve();
  const form=body.children[0].children[0],grid=form.children[2],actions=form.children[4];
  const length=grid.children.find(n=>n.textContent==='展开纸板长（毫米）').children[0];length.value='1100';
  const save=form.onsubmit({preventDefault(){}});await form.onsubmit({preventDefault(){}});
  for(let i=0;i<15;i++)await Promise.resolve();
  assert.equal(calls.filter(c=>c.method==='PUT').length,1);
  const payload=JSON.parse(calls.find(c=>c.method==='PUT').body);
  assert.equal(payload.expected_version,3);assert.equal(payload.confirmation_token,'version-bound');
  assert.equal(payload.customer_id,2);assert.equal(payload.sale_unit_price,'7.00');
  releaseWrite();await save;
  assert.match(form.children[3].textContent,/已保存.*刷新失败/);
  assert.equal(actions.children[0].hidden,true);
  actions.children[1].onclick();assert.equal((await result).version,4);
});
