const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('static/warehouse.html', 'utf8');
const fields = {
  moldCustomerFilter: {value: '聚晟达'},
  moldRackFilter: {value: 'R02'},
  moldKeyword: {value: '  778-A  '},
  moldIncludeUnprinted: {checked: true},
  moldIncludeInactive: {checked: false},
  moldIncludeRepair: {checked: false},
  moldBody: {innerHTML: ''},
};
let resolveRequest;
const urls = [];
const ctx = {
  $(id) {assert.ok(fields[id], `Unexpected input dependency: ${id}`); return fields[id];},
  state: {moldPage: 4, moldPageSize: 20, moldSortBy: 'updated_desc', moldTotal: 0,
    loadRequests: {molds: 0}, molds: [], moldSelection: new Set([99]), tab: 'molds'},
  api(url) {urls.push(new URL(url, 'http://isolated.test')); return new Promise(resolve => {resolveRequest = resolve;});},
  renderMolds() {}, renderMoldPager() {}, h: String,
  toast(message) {throw new Error(message);},
};
vm.createContext(ctx);
const source = html.slice(html.indexOf('    function loadMoldsFirstPage()'), html.indexOf('    function renderMolds(){'));
vm.runInContext(source, ctx);
ctx.updateMoldStatusSortControl = () => {};
(async () => {
  let request = ctx.loadMoldsFirstPage();
  assert.equal(urls[0].searchParams.get('q'), '778-A');
  assert.equal(urls[0].searchParams.has('product_code'), false);
  assert.equal(urls[0].searchParams.get('customer_keyword'), '聚晟达');
  assert.equal(urls[0].searchParams.get('rack_location'), 'R02');
  assert.equal(urls[0].searchParams.get('page'), '1');
  resolveRequest({items: [{id: 1}], total: 1, page: 1, page_size: 20});
  assert.equal(await request, true);
  assert.equal(ctx.state.molds[0].id, 1);
  assert.equal(ctx.state.moldSelection.size, 0);
  fields.moldKeyword.value = '产品名称';
  request = ctx.loadMoldsFirstPage();
  assert.equal(urls[1].searchParams.get('q'), '产品名称');
  fields.moldKeyword.value = '另一个词';
  resolveRequest({items: [{id: 2}], total: 1});
  assert.equal(await request, false, 'Changed input must reject an old result');
  assert.equal(ctx.state.molds[0].id, 1);
  fields.moldKeyword.value = '';
  request = ctx.loadMoldsFirstPage();
  assert.equal(urls[2].searchParams.has('q'), false);
  assert.equal(urls[2].searchParams.has('product_code'), false);
  resolveRequest({items: [], total: 0, page: 1, page_size: 20});
  assert.equal(await request, true);
  assert.doesNotMatch(html, /id="moldProductCodeFilter"/);
  for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
    if (match[1].trim()) new vm.Script(match[1]);
  }
  console.log('Mold unified query: code/name requests, independent filters, page reset, stale response and clear passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
