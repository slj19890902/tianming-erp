import assert from "node:assert/strict";
import test from "node:test";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import * as moldView from "../src/moldRackView.mjs";
import * as moldPrint from "../src/moldRackPrint.mjs";
import {filterShelfMolds} from "../src/shelfDisplay.mjs";

const source=readFileSync(new URL('../src/MoldRackElevation.tsx',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
function fixture(overrides={}) {
  const slots=[],effects=[],deps=[];let cursor=0,tree,failNext=false;const calls=[],moves=[],windows=[];
  const react={useState(initial){const index=cursor++;if(!(index in slots))slots[index]=typeof initial==='function'?initial():initial;return[slots[index],value=>{slots[index]=typeof value==='function'?value(slots[index]):value}]},useMemo:fn=>fn(),useRef(initial){const index=cursor++;return slots[index]||(slots[index]={current:initial})},useEffect(fn,values){const index=cursor++;if(!deps[index]||values.some((value,i)=>value!==deps[index][i])){deps[index]=values;effects.push(fn)}}};
  const exports={},context={exports,URL,URLSearchParams,Math,Date,crypto:{randomUUID:()=>"fixed-key"},location:{href:'http://fixture/warehouse.html?floor=3F',pathname:'/warehouse.html',search:'?floor=3F',assign(){}},window:{addEventListener(){},removeEventListener(){},confirm(){return true},open(url){const popup={location:{href:url},close(){}};windows.push(popup);return popup}},require(path){if(path==='react')return react;if(path==='react/jsx-runtime')return{jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};if(path.endsWith('moldRackView.mjs'))return moldView;if(path.endsWith('moldRackPrint.mjs'))return moldPrint;if(path.endsWith('shelfDisplay.mjs'))return{filterShelfMolds};return{}},fetch:async(path,init={})=>{const body=init.body?JSON.parse(init.body):null;calls.push({path,body});if(path.includes('/location-movement/batch')&&failNext){failNext=false;return{ok:false,status:503,json:async()=>({detail:'network uncertain'})}}return{ok:true,status:200,json:async()=>path.includes('/by-map-rack')?props.response:path.includes('/label-prints')?{print_job_id:123}:path.includes('/location-movement/batch')?{message:'saved'}:path.includes('/location-options')?{racks:[]}:{items:[{id:7,mold_code:'M-7',mold_name:'长片',rack_location:'old',location_version:4,products:[{id:1,product_code:'80012083',customer_name:'客户'}]}]}}}};
  vm.runInNewContext(compiled,context);
  const props={rack:{id:'rack-a',mold_rack_code:'A',levels:1,level_cell_counts:[2],level_heights_mm:[],width_mm:2000,depth_mm:600,height_mm:1200},response:{floor_code:'3F',rack:{rack_id:'rack-a',blocked_levels:[],cells:[{id:'c1',location_code:'MCELL-c1',level:1,grid:1,alias:'A1'},{id:'c2',location_code:'MCELL-c2',level:1,grid:2,alias:'A2'}]},items:[],total:0,truncated:false},canMoveMolds:true,rackIndex:0,rackCount:1,onPrevious(){},onNext(){},onClose(){},onMoldMoved:message=>moves.push(message),...overrides};
  function render(){cursor=0;tree=exports.MoldRackElevation(props);while(effects.length)effects.shift()();return nodes(tree)}
  render();render();
  return{render,calls,moves,windows,fail(){failNext=true}};
}
function nodes(node){if(!node||typeof node!=='object')return[];return[node,...[node.props?.children].flat(Infinity).flatMap(nodes)]}
function text(node){if(node===null||node===undefined||typeof node==='boolean')return '';return typeof node==='string'||typeof node==='number'?String(node):[node?.props?.children].flat(Infinity).map(text).join('')}
function button(f,label){return f.render().find(node=>node.type==='button'&&text(node)===label)}

test('旧货架缺少层高数组时模具搜索正视图仍可展示稳定格位，不发出业务请求',()=>{
 const rack={id:'rack-a',mold_rack_code:'A',levels:2,level_cell_counts:[2,2],width_mm:2000,depth_mm:600,height_mm:1200};
 const f=fixture({rack});
 assert.ok(f.render().some(n=>n.type==='h2'&&text(n).includes('模具')));
 assert.equal(f.render().filter(n=>n.type==='section'&&String(n.props.className||'').includes('mold-rack-cell')).length,4);
 assert.equal(f.calls.length,0);
 assert.equal(button(f,'打印整架模具标签').props.disabled,true);
});
