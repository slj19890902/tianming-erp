const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../src/WarehouseDimensionSearch.tsx'),'utf8');
function fixture(request = async () => ({total:0,offset:0,limit:15,items:[]})) {
  const states=[]; let cursor=0, tree, located;
  const hooks={useState(initial){const i=cursor++;if(!(i in states))states[i]=initial;return [states[i],v=>{states[i]=typeof v==='function'?v(states[i]):v;}];},useRef(initial){const i=cursor++;if(!(i in states))states[i]={current:initial};return states[i];},useEffect(){}};
  const jsx=(type,props)=>({type,props}); const exports={};
  const code=ts.transpileModule(source,{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS}}).outputText;
  vm.runInNewContext(code,{exports,URLSearchParams,AbortController,require(name){if(name==='react')return hooks;if(name==='react/jsx-runtime')return {jsx,jsxs:jsx,Fragment:'fragment'};return {};}});
  const render=()=>{cursor=0;tree=exports.WarehouseDimensionSearch({request,onLocate:v=>located=v});return tree;};
  function all(node=tree){if(!node||typeof node!=='object')return [];if(Array.isArray(node))return node.flatMap(v=>all(v??null));return [node,...all(node.props?.children??null)];}
  const get=(type,label)=>all().find(n=>n.type===type&&(!label||n.props['aria-label']===label||n.props.children===label));
  const input=(axis,value)=>{get('input',axis+'尺寸').props.onChange({target:{value}});render();};
  const submit=async()=>{get('form').props.onSubmit({preventDefault(){}});await new Promise(r=>setImmediate(r));render();};
  render();return {render,get,all,input,submit,located:()=>located};
}
test('numeric fields, near defaults, independent box height, flute and read-only request',async()=>{
  const calls=[];const f=fixture(async url=>{calls.push(url);return {total:0,items:[],offset:0,limit:15};});
  f.input('长','a123456x');assert.equal(f.get('input','长尺寸').props.value,'12345');
  await f.submit();let q=new URL(calls[0],'http://test').searchParams;
  assert.equal(q.get('length_op'),'near');assert.equal(q.has('height'),false);
  f.all().filter(n=>n.type==='select')[0].props.onChange({target:{value:'box'}});f.render();f.input('高','200');
  f.all().filter(n=>n.type==='select')[1].props.onChange({target:{value:'BC'}});f.render();await f.submit();
  q=new URL(calls[1],'http://test').searchParams;assert.equal(q.get('height'),'200');assert.equal(q.get('height_op'),'near');assert.equal(q.get('flute'),'BC');
});
test('empty and zero rejected, quantity/location and pagination rendered',async()=>{
  const calls=[];const row={id:7,location_id:1203,floor:3,placement_status:'placed',location_name:'三楼 北H2-10',name:'纸箱',code:'001',customer:'甲',dimensions:[500,300,200],flute:'B',unit:'只',physical:20,available:15,reserved:5,damaged:0};
  const f=fixture(async url=>{calls.push(url);return {total:16,items:[row],offset:0,limit:15};});
  await f.submit();assert.equal(calls.length,0);f.input('长','0');await f.submit();assert.equal(calls.length,0);
  f.input('长','500');await f.submit();assert.ok(JSON.stringify(f.all()).includes('三楼 北H2-10'));
  f.all().find(n=>n.type==='button'&&n.props.children?.some?.(c=>c?.type==='b')).props.onClick();assert.equal(f.located().id,7);
  f.get('button','下一页').props.onClick();await new Promise(r=>setImmediate(r));assert.ok(calls[1].includes('offset=15'));
});
test('changing criteria aborts and ignores obsolete response',async()=>{
  let resolve;const f=fixture(()=>new Promise(r=>resolve=r));f.input('长','500');
  f.get('form').props.onSubmit({preventDefault(){}});f.input('长','600');
  resolve({total:99,offset:0,items:[],limit:15});await new Promise(r=>setImmediate(r));f.render();
  assert.ok(!JSON.stringify(f.all()).includes('99'));assert.equal(f.get('input','长尺寸').props.value,'600');
});
test('desktop button before lookup and stable formal location integration',()=>{
  const app=fs.readFileSync(path.join(__dirname,'../src/WarehouseTwinApp.tsx'),'utf8');
  assert.ok(app.indexOf('>详细查找</button>')<app.indexOf('{searchPanelOpen && !detailSearchOpen ? "收起结果" : "查找"}'));
  assert.ok(app.includes('setPendingLocationId(item.location_id); setPendingRackSearchLocationId(item.location_id)'));
  assert.ok(app.includes('setPendingLotId(item.id)'));
  assert.ok(app.includes('(pendingLotId ?? focusedSearchItem?.lot_id)'));
  const css=fs.readFileSync(path.join(__dirname,'../src/warehouseDimensionSearch.css'),'utf8');
  assert.ok(css.includes('.warehouse-twin-shell .twin-workspace > .twin-context-rail[hidden]'));
  assert.ok(!source.includes('mutateJson')&&!source.includes('/take'));
});
