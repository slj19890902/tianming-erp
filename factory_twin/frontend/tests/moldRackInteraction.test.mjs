import assert from "node:assert/strict";
import test from "node:test";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import * as moldView from "../src/moldRackView.mjs";
import {filterShelfMolds} from "../src/shelfDisplay.mjs";

const source=readFileSync(new URL('../src/MoldRackElevation.tsx',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
function fixture(overrides={}) {
  const slots=[],effects=[],deps=[];let cursor=0,tree,failNext=false;const calls=[],moves=[];
  const react={useState(initial){const index=cursor++;if(!(index in slots))slots[index]=typeof initial==='function'?initial():initial;return[slots[index],value=>{slots[index]=typeof value==='function'?value(slots[index]):value}]},useMemo:fn=>fn(),useRef(initial){const index=cursor++;return slots[index]||(slots[index]={current:initial})},useEffect(fn,values){const index=cursor++;if(!deps[index]||values.some((value,i)=>value!==deps[index][i])){deps[index]=values;effects.push(fn)}}};
  const exports={},context={exports,URL,URLSearchParams,Math,Date,crypto:{randomUUID:()=>"fixed-key"},location:{href:'http://fixture/warehouse.html?floor=3F',pathname:'/warehouse.html',search:'?floor=3F',assign(){}},window:{addEventListener(){},removeEventListener(){}},require(path){if(path==='react')return react;if(path==='react/jsx-runtime')return{jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};if(path.endsWith('moldRackView.mjs'))return moldView;if(path.endsWith('shelfDisplay.mjs'))return{filterShelfMolds};return{}},fetch:async(path,init={})=>{const body=init.body?JSON.parse(init.body):null;calls.push({path,body});if(path.includes('/location-movement/batch')&&failNext){failNext=false;return{ok:false,status:503,json:async()=>({detail:'network uncertain'})}}return{ok:true,status:200,json:async()=>path.includes('/location-movement/batch')?{message:'saved'}:path.includes('/location-options')?{racks:[]}:{items:[{id:7,mold_code:'M-7',mold_name:'长片',rack_location:'old',location_version:4,products:[{id:1,product_code:'80012083',customer_name:'客户'}]}]}}}};
  vm.runInNewContext(compiled,context);
  const props={rack:{id:'rack-a',mold_rack_code:'A',levels:1,level_cell_counts:[2],level_heights_mm:[],width_mm:2000,depth_mm:600,height_mm:1200},response:{floor_code:'3F',rack:{rack_id:'rack-a',blocked_levels:[],cells:[{id:'c1',location_code:'MCELL-c1',level:1,grid:1,alias:'A1'},{id:'c2',location_code:'MCELL-c2',level:1,grid:2,alias:'A2'}]},items:[],total:0,truncated:false},canMoveMolds:true,rackIndex:0,rackCount:1,onPrevious(){},onNext(){},onClose(){},onMoldMoved:message=>moves.push(message),...overrides};
  function render(){cursor=0;tree=exports.MoldRackElevation(props);while(effects.length)effects.shift()();return nodes(tree)}
  render();render();
  return{render,calls,moves,fail(){failNext=true}};
}
function nodes(node){if(!node||typeof node!=='object')return[];return[node,...[node.props?.children].flat(Infinity).flatMap(nodes)]}
function text(node){if(node===null||node===undefined||typeof node==='boolean')return '';return typeof node==='string'||typeof node==='number'?String(node):[node?.props?.children].flat(Infinity).map(text).join('')}
function button(f,label){return f.render().find(node=>node.type==='button'&&text(node)===label)}

test('空格选择与多选归位绑定稳定格身份，网络未知结果重试沿用原键与每块版本',async()=>{
  const f=fixture();const cell=f.render().find(node=>node.type==='button'&&text(node).includes('A2空格'));cell.props.onClick();
  button(f,'＋ 放入模具').props.onClick();
  const input=f.render().find(node=>node.type==='input'&&node.props.placeholder==='编码、名称或模具二维码');input.props.onChange({target:{value:'80012083'}});
  await f.render().find(node=>node.type==='form').props.onSubmit({preventDefault(){}});
  await new Promise(setImmediate);
  const checkbox=f.render().find(node=>node.props.type==='checkbox');checkbox.props.onChange({target:{checked:true}});
  f.fail();await button(f,'实物已放好，保存归位').props.onClick();await new Promise(setImmediate);
  assert.equal(button(f,'取消').props.disabled,true);
  await button(f,'用原凭证核对结果').props.onClick();await new Promise(setImmediate);
  const requests=f.calls.filter(call=>call.path.includes('/location-movement/batch'));
  assert.equal(requests.length,2);assert.deepEqual(requests[0].body,requests[1].body);
  assert.equal(requests[0].body.target_location,'MCELL-c2');assert.deepEqual(requests[0].body.items,[{mold_code:'M-7',expected_version:4}]);assert.equal(f.moves.length,1);
  assert.ok(f.render().some(node=>node.type==='h3'&&text(node)==='A2'),'归位后仍在来源格');
});

test('只读用户可以点选空格而没有放入入口',()=>{
  const f=fixture({canMoveMolds:false});f.render().find(node=>node.type==='button'&&text(node).includes('A2空格')).props.onClick();
  assert.ok(f.render().some(node=>node.type==='h3'&&text(node)==='A2'));assert.equal(button(f,'＋ 放入模具'),undefined);assert.equal(f.calls.length,0);
});

test('移动未选目标不会回退源格，取消后仍能向空格放入',async()=>{
  const f=fixture({response:{floor_code:'3F',rack:{rack_id:'rack-a',blocked_levels:[],cells:[{id:'c1',location_code:'MCELL-c1',level:1,grid:1,alias:'A1'},{id:'c2',location_code:'MCELL-c2',level:1,grid:2,alias:'A2'}]},items:[{id:7,mold_code:'M-7',mold_name:'长片',rack_location:'MCELL-c1',location_version:4,location_guide:{level:1,grid:1,prompt:'3F · 模具A架 · A1'},products:[]}],total:1,truncated:false}});
  f.render().find(node=>node.type==='button'&&text(node).startsWith('长片')).props.onClick();
  button(f,'移动模具').props.onClick();await new Promise(setImmediate);
  assert.equal(button(f,'实物已放好，保存归位').props.disabled,true,'目标不得回退源格');
  button(f,'取消').props.onClick();
  f.render().find(node=>node.type==='button'&&text(node).includes('A2空格')).props.onClick();
  assert.equal(button(f,'＋ 放入模具').props.disabled,false);
});

test('手机格位入口首次打开保留指定格，目录不完整时不声称空格',()=>{
  const f=fixture({initialCellId:'c2'});assert.ok(f.render().some(node=>node.type==='h3'&&text(node)==='A2'));
  const truncated=fixture({response:{floor_code:'3F',rack:{rack_id:'rack-a',blocked_levels:[],cells:[{id:'c1',level:1,grid:1,alias:'A1'}]},items:[],total:1200,truncated:true}});
  assert.equal(truncated.render().some(node=>node.type==='strong'&&text(node)==='空格'),false);
});

test('新手机与标签页脚本语法完整，旧M二维码入口保留',()=>{
  for(const file of ['mobile_mold_rack.html','mold-location-label.html','mobile_mold_lookup.html','mobile_mold_live.html']){
    const html=readFileSync(new URL('../../../static/'+file,import.meta.url),'utf8');
    for(const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))if(script[1].trim())assert.doesNotThrow(()=>new vm.Script(script[1]),file);
  }
  const live=readFileSync(new URL('../../../static/mobile_mold_live.html',import.meta.url),'utf8');assert.ok(live.includes('parts[0]==="M"'));
});

test('图号查找命中实际模具并只显示当前可见产品资料',()=>{
  const molds=[{id:1,mold_name:'长片',products:[{id:1,customer_drawing_number:'DRAW-01',customer_drawing_display:'DRAW-01 R2'}]},{id:2,mold_name:'短片',products:[{id:2,customer_drawing_number:'DRAW-02'}]}];
  assert.deepEqual(filterShelfMolds(molds,'draw-01').map(mold=>mold.id),[1]);
});
