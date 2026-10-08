import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const source = fs.readFileSync(new URL('../../../static/ui/workspace.js', import.meta.url), 'utf8');
function fixture(page = 'customers') {
  const mixins = [], calls = [];
  const rect = (top, height, width = 1400) => ({top, bottom: top + height, height, width});
  let tableTop = 140, rowHeight = 48, rowsCount = 8, hidden = false;
  const main = {scrollTop: 0, getBoundingClientRect: () => rect(0, 900)};
  const table = {getBoundingClientRect: () => rect(tableTop, 36 + rowHeight * rowsCount),
    closest: s => s === '.main' ? main : null, parentElement: main,
    tHead: {getBoundingClientRect: () => rect(tableTop, 36)},
    querySelectorAll: () => Array.from({length:rowsCount}, () => ({cells:[1,2], getBoundingClientRect: () => rect(0,rowHeight)}))};
  const sandbox = {innerWidth:1440, innerHeight:900, requestAnimationFrame:()=>1, cancelAnimationFrame(){},
    frameElement:{getClientRects:()=>hidden?[]:[rect(0,900)]},
    document:{querySelector:s => s.includes('data-viewport-page') ? table : null},
    getComputedStyle:()=>({paddingBottom:'16',paddingTop:'0',borderBottomWidth:'0',marginBottom:'0',marginTop:'0',position:'static'})};
  sandbox.window=sandbox;
  vm.runInNewContext(source,sandbox);
  sandbox.ERPWorkspace.install({mixin:d=>mixins.push(d),component(){}});
  const ctx = Object.assign({}, ...mixins.map(x=>x.methods), {activePage:page, uiMode:'standard', productTab:'products',
    selectedProductCustomer:{id:137}, incomingTab:'history', workspaceHeight:900, workspaceFitSizes:{}, workspaceCapacities:{},
    pages:{customers:1,products:1,incomingHistory:1},
    desktopListPageSize(){return this.screenPageSize(11);},productListPageSize(){return this.screenPageSize(25);},incomingListPageSize(){return this.screenPageSize(12);},
    runExplicitPageListLoad:async key=>calls.push(key), loadIncomingHistory:async()=>calls.push('incomingHistory')});
  return {ctx,calls,table,set(o){if(o.top!==undefined)tableTop=o.top;if(o.row!==undefined)rowHeight=o.row;if(o.rows!==undefined)rowsCount=o.rows;if(o.hidden!==undefined)hidden=o.hidden;}};
}
for(const page of ['customers','products','incoming']) test(`${page}: first visible measurement fills height, clicks/last page stay stable`,async()=>{
  const f=fixture(page);
  await f.ctx.measureViewportPage();
  const size=f.ctx.screenPageSize(12);
  assert.equal(size,14);
  assert.equal(f.calls.length,1);
  f.set({rows:2});
  for(let i=0;i<5;i++)await f.ctx.measureViewportPage();
  assert.equal(f.ctx.screenPageSize(12),size);
  assert.equal(f.calls.length,1);
});
test('hidden cached frame cannot freeze a false capacity; shell chrome change recalculates without a click',async()=>{
  const f=fixture();f.set({hidden:true});await f.ctx.measureViewportPage();assert.equal(f.calls.length,0);
  f.set({hidden:false,top:240});await f.ctx.measureViewportPage();assert.equal(f.ctx.screenPageSize(12),12);
  f.set({top:140});await f.ctx.measureViewportPage();assert.equal(f.ctx.screenPageSize(12),14);
  assert.equal(f.calls.length,2);
});
test('taller rows shrink safely, retain page anchor and cannot oscillate with short last-page rows',async()=>{
  const f=fixture();await f.ctx.measureViewportPage();f.ctx.pages.customers=3;
  f.set({row:80});await f.ctx.measureViewportPage();assert.equal(f.ctx.screenPageSize(12),8);
  assert.equal(f.ctx.pages.customers,4); // old offset 28 stays on its new containing page
  f.set({row:40,rows:2});await f.ctx.measureViewportPage();assert.equal(f.ctx.screenPageSize(12),8);
  assert.equal(f.calls.length,2);
});
test('pending receipt drafts retain their existing fixed sizing',()=>{
  const f=fixture('incoming');f.ctx.incomingTab='pending';assert.equal(f.ctx.screenPageSize(12),12);
});
test('common-box customer selector resizes its local page without a server reload',async()=>{
  const f=fixture('products');f.ctx.selectedProductCustomer=null;f.ctx.pages.productCustomers=1;
  await f.ctx.measureViewportPage();assert.equal(f.ctx.desktopListPageSize(),15);assert.equal(f.calls.length,0);
});
