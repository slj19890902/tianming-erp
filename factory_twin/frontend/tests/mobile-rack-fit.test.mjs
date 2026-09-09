import {readFileSync} from 'node:fs';
import {createServer} from 'node:http';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const html = readFileSync(new URL('../../../static/mobile_erp.html', import.meta.url), 'utf8');
const render = html.match(/      function renderWarehouseMap\(\) \{[\s\S]*?\n      \}/)[0];
const data = {floor_code:'3F',floor_name:'三楼',area_name:'匿名验收区',map_status:'ready',compass:{label:'现实东向 E'},
  bounds_mm:{min_x:0,min_y:0,max_x:5000,max_y:10000}, features:[],
  locations:[9,2,5,1,7,3,8,4,6].map(id=>({location_id:id,map_rack_id:'fixture',rack_display_name:'F1 样本',level_no:Math.ceil(id/3),slot_no:(id-1)%3+1,
    can_select_target:true,goods:id===8?[{customer_short_name:'样本',product_code:'TEST-008',product_name:'样本纸箱',specification:'400×300×200',quantity_total:99,unit:'个'}]:[]}))};
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.style={};this.dataset={};this.attributes={};this.events={};this.classList={add:()=>{}};this.clientWidth=390;}
  append(...children){for(const child of children){child.parentElement=this;this.children.push(child);}}
  replaceChildren(...children){this.children=[];this.append(...children);}
  setAttribute(key,value){this.attributes[key]=value;}
  addEventListener(key,fn){this.events[key]=fn;}
}
test('real render preserves unsorted layer/cell identities and fits both axes without distortion',()=>{
  const elements = new Map(); const byId=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  byId('warehouseMapStage').parentElement=new Element();
  const state={warehouseMapData:data,warehouseSelectedRack:'fixture',warehouseMapZoom:1};
  vm.runInNewContext(render+';renderWarehouseMap()', {state,byId,window:{innerHeight:844},
    document:{createElementNS:(_,tag)=>new Element(tag)},node:(tag,cls,text)=>Object.assign(new Element(tag),{className:cls,textContent:text}),compactWarehouseLocation:()=>'',showStatus:()=>{}});
  const rack=byId('warehouseRackList').children[0];
  assert.deepEqual(rack.children.slice(1).map(section=>section.children[0].textContent),['3层','2层','1层']);
  for(const section of rack.children.slice(1))assert.deepEqual(section.children[1].children.map(cell=>cell.style.gridColumn),['1','2','3']);
  const stage=byId('warehouseMapStage');
  assert.equal(parseFloat(stage.style.height)/parseFloat(stage.style.width),2);
  assert.ok(parseFloat(stage.style.height)<=674);
  assert.equal(stage.children[0].attributes.viewBox,'0 -10000 5000 10000');
  assert.equal(stage.style.transform,'none');
  assert.equal(byId('warehouseMapCompass').textContent,'↑ 现实东向 E');
  const occupied = rack.children[1].children[1].children[1];
  assert.equal(occupied.children[0].textContent,'2格 · 有货 · 99个 · 样本');
  assert.equal(occupied.children[1].children[0].textContent,'TEST-008');
  assert.equal(occupied.children[1].children[1].textContent,'样本纸箱');
  assert.equal(data.locations[0].location_id,9,'response and formal identities must not be mutated');
});

// Optional anonymous Chrome fixture. Reads real CSS/render function; no database or APIs.
if(process.argv.includes('--serve')) {
  const style=html.match(/<style>([\s\S]*?)<\/style>/)[1];
  const body=html.match(/<section id="warehouseMapLayer"[\s\S]*?<\/section>/)[0].replace(' hidden aria-label',' aria-label');
  const source=`const state={warehouseMapData:${JSON.stringify(data)},warehouseSelectedRack:'fixture',warehouseMapZoom:1};
    const byId=id=>document.getElementById(id);const node=(tag,cls,text)=>{const e=document.createElement(tag);e.className=cls||'';if(text!==undefined)e.textContent=text;return e;};
    const compactWarehouseLocation=l=>'样本-'+l.location_id;const showStatus=()=>{};
    const renderWarehouseLocationGoods=l=>{document.getElementById('warehouseMapGoods').hidden=false;document.getElementById('warehouseMapGoods').textContent='样本货位 '+l.location_id;};
    ${render};renderWarehouseMap();`;
  createServer((req,res)=>{res.setHeader('Content-Type','text/html; charset=utf-8');res.end(`<meta name="viewport" content="width=device-width,initial-scale=1"><style>${style}</style>${body}<script>${source}</script>`);}).listen(5189,'127.0.0.1');
}
