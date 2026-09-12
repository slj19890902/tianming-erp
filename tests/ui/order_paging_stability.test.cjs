const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const root=path.resolve(__dirname,'../..');
function fixture(){
  const mixins=[], events={};
  const dimensions={row:60,count:6};
  const main={scrollTop:0,getBoundingClientRect:()=>({bottom:800})};
  const table={dataset:{},closest:()=>null,getBoundingClientRect:()=>({height:400,top:250,bottom:650}),
    querySelectorAll:()=>Array.from({length:dimensions.count},()=>({cells:[1,2],getBoundingClientRect:()=>({height:dimensions.row})})),
    tHead:{getBoundingClientRect:()=>({height:36})}};
  const panel={closest:()=>main,querySelector:()=>table,getBoundingClientRect:()=>({bottom:650}),parentElement:main,nextElementSibling:null};
  const browser={innerWidth:1920,innerHeight:800,addEventListener:(name,fn)=>events[name]=fn};
  const sandbox={window:browser,document:{querySelector:()=>null,querySelectorAll:()=>[table]},getComputedStyle:()=>({}),setTimeout:fn=>{events.timer=fn;return 1;},clearTimeout:()=>{}};
  vm.runInNewContext(fs.readFileSync(path.join(root,'static/ui/workspace.js'),'utf8'),sandbox);
  browser.ERPWorkspace.install({mixin:m=>mixins.push(m),component:()=>{}});
  const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
  const body=html.match(/async reloadOrdersForUiModeChange\(previousMode, nextMode\) \{([\s\S]*?)\n          \},/)[1];
  const reload=new (Object.getPrototypeOf(async function(){}).constructor)('previousMode','nextMode',body);
  const calls=[];
  const ctx={activePage:'orders',uiMode:'standard',orderWorkspace:'queue',pages:{orders:1},pageSize:50,
    workspaceCapacities:{},workspaceLocalSizes:{},workspaceHeight:800,
    invalidatePageCache:()=>{},runExplicitPageListLoad:async()=>{calls.push(ctx.pages.orders);return true;},
    reloadOrdersForUiModeChange:reload,$forceUpdate:()=>{}};
  Object.assign(ctx,mixins[0].methods);
  return {ctx,dimensions,calls,measure:()=>ctx.measureWorkspace(panel),resize:()=>{mixins[1].mounted.call(ctx);events.resize();events.timer();}};
}
test('ordinary next/previous pages keep initial capacity despite taller rows',async()=>{
  const f=fixture();await f.measure();const size=f.ctx.screenPageSize(11);f.calls.length=0;
  for(const page of [2,3,2,1]){f.ctx.pages.orders=page;f.dimensions.row=100;await f.measure();assert.equal(f.ctx.pages.orders,page);assert.equal(f.ctx.screenPageSize(11),size);}
  assert.deepEqual(f.calls,[]);
});
test('underfilled last page and empty results do not resize or reload',async()=>{
  const f=fixture();await f.measure();const size=f.ctx.screenPageSize(11);f.calls.length=0;f.ctx.pages.orders=9;
  for(const count of [1,0]){f.dimensions.count=count;f.dimensions.row=200;await f.measure();assert.equal(f.ctx.pages.orders,9);assert.equal(f.ctx.screenPageSize(11),size);}
  assert.deepEqual(f.calls,[]);
});
test('real resize permits one new capacity then stable paging',async()=>{
  const f=fixture();await f.measure();f.ctx.pages.orders=2;f.dimensions.row=100;f.resize();await f.measure();
  assert.equal(f.ctx.pages.orders,1);assert.equal(f.ctx.screenPageSize(11),4);
  f.calls.length=0;f.ctx.pages.orders=2;f.dimensions.row=150;await f.measure();assert.equal(f.ctx.pages.orders,2);assert.deepEqual(f.calls,[]);
});
test('font mode has separate sizing and becomes stable',async()=>{
  const f=fixture();await f.measure();f.ctx.uiMode='large';f.dimensions.row=100;await f.measure();assert.equal(f.ctx.screenPageSize(6),4);
  f.calls.length=0;f.ctx.pages.orders=2;f.dimensions.row=150;await f.measure();assert.equal(f.ctx.pages.orders,2);assert.deepEqual(f.calls,[]);
});
test('empty initial page does not lock future measurement',async()=>{
  const f=fixture();f.dimensions.count=0;await f.measure();assert.equal(Object.keys(f.ctx.workspaceCapacities).length,0);
  f.dimensions.count=6;await f.measure();assert.equal(Object.keys(f.ctx.workspaceCapacities).length,1);
});
test('other workspaces retain previous capacity reduction behavior',async()=>{
  const f=fixture();f.ctx.activePage='incoming';f.ctx.incomingWorkspace='board';f.ctx.incomingTab='pending';f.ctx.loadIncomingPendingPage=async()=>{};
  await f.measure();f.dimensions.row=100;await f.measure();assert.equal(f.ctx.screenPageSize(11),4);
});
