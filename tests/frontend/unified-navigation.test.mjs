import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import ts from '../../frontend-v2/node_modules/typescript/lib/typescript.js';
import { webcrypto } from 'node:crypto';

const source = readFileSync(new URL('../../static/js/unified-navigation.js', import.meta.url), 'utf8');
const bridge = readFileSync(new URL('../../static/js/frontend-shell-bridge.js', import.meta.url), 'utf8');
function setup(overrides = {}, bootstrap = {}) {
  const calls = [], messages = [], listeners = {};
  const parent = {postMessage: data => messages.push(data)};
  const classes = new Set();
  const classList = {add: (...names) => names.forEach(n => classes.add(n)), remove: (...names) => names.forEach(n => classes.delete(n)), toggle: (n, on) => on ? classes.add(n) : classes.delete(n)};
  const window = {parent, addEventListener: (type, fn) => {listeners[type] = fn}, setInterval: fn => {listeners.tick = fn; return 1}};
  window.erpCheckingSession = bootstrap.checking === true;
  const location = {origin:'http://127.0.0.1:18569', search:'?embedded=1&frontend_shell=1'};
  if (bootstrap.unified) location.search += '&unified_navigation=1';
  if (bootstrap.standalone) window.parent = window;
  const document = {documentElement:{classList}, head:{appendChild:()=>{}}, createElement:()=>({})};
  runInNewContext(source, {window});
  const vm = {
    user:{id:1, role:'admin', real_name:'测试', username:'test'}, authGeneration:3,
    menus:[{key:'warehouse',label:'仓库'},{key:'workbench',label:'订单主链'}],
    activePage:'warehouse', warehouseView:'map', warehouseLedgerTab:'finished',
    modal:{type:''}, roleLabel:()=> '管理员', isMenuActive:k=>k==='warehouse',
    pageAllowed:()=>true, warehouseStocktakeVisible:()=>true, canViewInvoiceTasks:true, canViewFinanceCosts:true,
    $watch:()=>()=>{},
    ...Object.fromEntries(['chooseWarehouseView','setFinanceView','setUiMode','refreshCurrent','openChangePassword','logout','openBusinessRequests','goMenu','go'].map(name => [name,async (...args)=>calls.push([name,...args])])),
    ...overrides,
  };
  runInNewContext(bridge, {window, document, location, URLSearchParams, clearInterval:()=>{}});
  window.ERPFrontendShell?.install(vm);
  const send = data => listeners.message({source:parent,origin:location.origin,data});
  const connect = (more = {}) => send({type:'tianming-formal-shell-v1', unifiedNavigation:true, ...more});
  const command = (key, extra = {}) => send({type:'tianming-unified-command-v1', requestId:'11111111-1111-4111-8111-111111111111',actorId:1,generation:3,key,...extra});
  return {vm,window,classes,messages,calls,listeners,parent,send,connect,command,ui:()=>window.ERPUnifiedNavigation.describe(vm),execute:key=>window.ERPUnifiedNavigation.execute(vm,key)};
}
const flush = () => new Promise(resolve=>setImmediate(resolve));

test('cold embedded layout is stable while business data loads; menus project but commands stay blocked', async () => {
  const s=setup({}, {unified:true,checking:true});
  assert.equal(s.classes.has('frontend-shell-layout'),true);
  assert.equal(s.messages.at(-1).type,'tianming-formal-bridge-ready-v1');
  s.connect();
  assert.equal(s.messages.at(-1).ready,false);
  assert.equal(s.messages.at(-1).menus[0].label,'仓库');
  s.command('action:logout'); await flush();
  assert.equal(s.calls.length,0); assert.equal(s.messages.at(-1).status,'denied');
  s.window.erpCheckingSession=false;s.listeners.tick();
  assert.equal(s.messages.at(-1).ready,true);
  s.vm.user.must_change_password=true;s.listeners.tick();
  assert.equal(s.classes.has('frontend-shell-layout'),true);
  assert.equal(s.messages.at(-1).menus.length,0);
  s.vm.user=null;s.listeners.tick();
  assert.equal(s.messages.at(-1).authenticated,false);
});

test('standalone pages and old shell do not opt into prepaint layout', () => {
  const direct=setup({}, {unified:true,standalone:true});
  assert.equal(direct.classes.size,0);assert.equal(direct.window.ERPFrontendShell,undefined);
  const legacy=setup();assert.equal(legacy.classes.has('frontend-shell-layout'),false);
});

test('showing a cached workspace remeasures locally; inactive and foreign messages cannot trigger it',()=>{
  let measured=0;const f=setup({queueViewportPageMeasure:()=>measured++});
  f.connect({active:false});assert.equal(measured,0);
  f.listeners.message({source:{},origin:'http://127.0.0.1:18569',data:{type:'tianming-formal-shell-v1',active:true}});
  assert.equal(measured,0);f.connect({active:true});assert.equal(measured,1);
});

const shellModule = {exports:{},crypto:{getRandomValues: array=>webcrypto.getRandomValues(array)}};
runInNewContext(ts.transpileModule(readFileSync(new URL('../../frontend-v2/src/utils/unifiedNavigation.ts',import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,shellModule);
test('LAN HTTP without randomUUID can create unique well-formed command IDs', () => {
  const ids=Array.from({length:1000},()=>shellModule.exports.navigationRequestId());
  assert.equal(new Set(ids).size,1000);
  assert.ok(ids.every(id=>/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(id)));
});
test('outer shell rejects another actor and malformed navigation projection', () => {
  const s=setup(), ui=s.ui(), parse=shellModule.exports.parseShellUi;
  assert.ok(parse(ui,1)); assert.equal(parse(ui,2),null);
  assert.equal(parse({...ui,generation:'3'},1),null);
  assert.equal(parse({...ui,busy:'false'},1),null);
  assert.equal(parse({...ui,items:[{key:'javascript:alert(1)',label:'bad'}]},1),null);
});

test('warehouse tabs use existing guarded navigation and preserve the current inventory kind', async () => {
  const s=setup();
  assert.equal(s.ui().items.length,5);
  await s.execute('warehouse:ledger'); await s.execute('warehouse:movements');
  assert.deepEqual(s.calls,[['chooseWarehouseView','ledger',null],['chooseWarehouseView','ledger','movements']]);
  s.vm.warehouseStocktakeVisible=()=>false;
  assert.equal(s.ui().items.some(x=>x.key==='warehouse:stocktake_review'),false);
  assert.equal(await s.execute('warehouse:stocktake_review'),'denied');
});
test('finance removes invoice and cost entries without capabilities, including direct dispatch', async () => {
  const s=setup({activePage:'finance',financeView:'current',canViewInvoiceTasks:false,canViewFinanceCosts:false});
  assert.equal(s.ui().items.map(x=>x.label).join(','),'经营概览,客户对账,收款办理,供应商付款');
  assert.equal(await s.execute('finance:invoice_tasks'),'denied');
  assert.equal(await s.execute('finance:expenses'),'denied');
  await s.execute('finance:payables'); assert.deepEqual(s.calls,[['setFinanceView','payables']]);
});
test('flow, master data and system tabs respect visible pages and same-page clicks preserve filters', async () => {
  const s=setup({activePage:'orders', businessFlowCurrentStep:{key:'orders'}, businessFlowSteps:[{key:'orders',page:'orders',label:'订单'},{key:'incoming',page:'incoming',label:'来料'}],pageAllowed:p=>p==='orders'});
  assert.equal(s.ui().items.length,1); assert.equal(s.ui().flow,true);
  await s.execute('page:orders'); assert.equal(s.calls.length,0);
  assert.equal(await s.execute('page:incoming'),'denied');
  s.vm.businessFlowCurrentStep=null; s.vm.activePage='customers';
  s.vm.currentNavigationGroup={label:'主数据',pages:[{key:'customers',label:'客户'},{key:'products',label:'常用箱与材质'}]};
  s.vm.pageAllowed=()=>true;
  await s.execute('page:products'); assert.deepEqual(s.calls,[['go','products']]);
});
test('open drafts and in-flight warehouse guards block navigation, refresh and logout', async () => {
  for (const state of [{modal:{type:'order'}},{productionEntry:{}},{stockPrepDialog:{}},{stockAssemblyDialog:{}},{loading:true},{warehouseNavigating:true}]) {
    const s=setup(state);
    for(const key of ['menu:workbench','warehouse:ledger','action:refresh','action:logout']) assert.equal(await s.execute(key),'busy');
    assert.equal(s.calls.length,0);
  }
});
test('account tools call the existing implementations and approval stays permission-bound', async () => {
  const s=setup();
  for(const key of ['ui:large','action:refresh','action:password','action:logout']) assert.equal(await s.execute(key),'done');
  assert.deepEqual(s.calls.map(x=>x[0]),['setUiMode','refreshCurrent','openChangePassword','logout']);
  assert.equal(s.ui().canApprove,true);
  s.vm.user.role='workshop'; s.vm.canSubmitBusinessRequest=false;
  assert.equal(s.ui().canApprove,false);
  assert.equal(await s.execute('action:approval'),'denied');
});
test('legacy and missing handshakes keep original header; supported handshake hides only after ready', () => {
  const s=setup(); assert.equal(s.classes.has('frontend-shell-unified'),false);
  s.connect({unifiedNavigation:false}); assert.equal(s.classes.has('frontend-shell-unified'),false);
  s.connect(); assert.equal(s.classes.has('frontend-shell-unified'),true);
  s.vm.user.must_change_password=true; s.listeners.tick();
  assert.equal(s.classes.size,0); assert.equal(s.messages.at(-1).ready,false);
  s.vm.user=null; s.listeners.tick(); assert.equal(s.messages.at(-1).authenticated,false);
});
test('messages require parent, same origin, active frame and exact current actor/generation', async () => {
  const s=setup(); s.connect();
  for(const extra of [{actorId:2},{generation:2}]) s.command('action:refresh',extra);
  s.connect({active:false}); s.command('action:refresh');
  s.connect();
  for(const changed of [{source:{}},{origin:'http://evil.invalid'}]) s.listeners.message({source:s.parent,origin:'http://127.0.0.1:18569',...changed,data:{type:'tianming-unified-command-v1',key:'action:refresh',requestId:'11111111-1111-4111-8111-111111111111',actorId:1,generation:3}});
  await flush(); assert.equal(s.calls.length,0);
});
test('duplicate request never repeats an action and cannot be reused with another command', async () => {
  const s=setup(); s.connect(); s.command('action:refresh'); await flush();
  s.command('action:refresh'); s.command('action:logout'); await flush();
  assert.deepEqual(s.calls,[['refreshCurrent']]); assert.equal(s.messages.at(-1).status,'denied');
});
test('unknown commands and hidden menu targets cannot invoke arbitrary methods', async () => {
  const s=setup(); s.connect();
  for(const key of ['deleteAll','action:save','menu:permissions','warehouse:secret']) assert.equal(await s.execute(key),'denied');
  assert.equal(s.calls.length,0);
});
test('pending command blocks a second action until the first finishes', async () => {
  let finish;
  const s=setup({refreshCurrent:()=>new Promise(resolve=>{finish=resolve})});
  s.connect(); s.command('action:refresh');
  s.command('action:logout',{requestId:'22222222-2222-4222-8222-222222222222'});
  assert.equal(s.messages.at(-1).status,'busy');
  finish(); await flush(); assert.equal(s.calls.length,0);
});
