// Isolated UI contract. It executes the real pending-response and refresh methods
// from static/index.html; no ERP server, database, or external request is started.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const indexSource = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const main = {scrollTop: 72, scrollLeft: 3};
const scrollCalls = [];
global.window = global;
global.window.scrollX = 3;
global.window.scrollY = 72;
global.window.scrollTo = (left, top) => scrollCalls.push([left, top]);
global.document = {
  activeElement: null,
  querySelectorAll: selector => selector === '.main' ? [main] : [],
  querySelector: () => null,
};

function methodFromIndex(signature) {
  const start = indexSource.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  let parens = 0;
  let brace = -1;
  for (let cursor = start; cursor < indexSource.length; cursor += 1) {
    if (indexSource[cursor] === '(') parens += 1;
    if (indexSource[cursor] === ')') parens -= 1;
    if (indexSource[cursor] === '{' && parens === 0) { brace = cursor; break; }
  }
  assert.notEqual(brace, -1, `missing body for ${signature}`);
  let depth = 0;
  let end = brace;
  for (; end < indexSource.length; end += 1) {
    if (indexSource[end] === '{') depth += 1;
    if (indexSource[end] === '}' && --depth === 0) break;
  }
  let method = indexSource.slice(start, end + 1);
  method = method.replace(/^async\s+([\w$]+)\(/, 'async function $1(');
  method = method.replace(/^([\w$]+)\(/, 'function $1(');
  return eval(`(${method})`);
}

let mixin;
eval(fs.readFileSync(path.join(root, 'static/ui/order-context.js'), 'utf8'));
global.ERPOrderContext.install({component() {}, mixin(value) { mixin = value; }});

const keep = {key: 'order_item:41', item_id: 41, product_id: 7, product_code: 'P007'};
const dropped = {key: 'order_item:42', item_id: 42, product_id: 7, product_code: 'P007'};
const toasts = [];
const vm = {
  ...mixin.data(),
  activePage: 'requisition', requisitionWorkspace: 'board', requisitionTab: 'pending',
  requisitionSupplierFilter: '鸣朋', requisitionPending: [keep, dropped],
  pages: {requisitionPending: 3}, selectedPendingKeys: [keep.key, dropped.key],
  requisitionPendingAppliedPage: 3, requisitionPendingAppliedSupplierFilter: '鸣朋',
  requisitionSelected: {[keep.key]: true, [dropped.key]: true}, authGeneration: 9, user: {id: 5},
  canEditProducts: true, masterSavePending: false, modal: null, productForm: {product_code: 'P007'},
  pendingRowKey: row => row.key, isPendingRowSelectable: () => true,
  compositeRequisitionComponents: () => [], bomSourceSelectionKey: () => '',
  $nextTick: callback => callback(), resetProductEditorState: () => { vm.productForm = {}; },
  openProduct: async ({id}) => { assert.equal(id, 7); vm.modal = {type: 'product'}; return true; },
  commonBoxReadiness: product => product.common_box_readiness,
  showToast: (...args) => toasts.push(args), errorMessage: error => error.message,
  beginLatestRequest: () => ({signal: {aborted: false}}), finishLatestRequest: () => {},
  pendingRequisitionRequestIsCurrent: () => vm.activePage === 'requisition' && vm.requisitionWorkspace === 'board' && vm.user?.id === 5,
  requisitionPendingRequestParams: (page, supplierName) => ({page, supplierName}),
  invalidatePageCache: () => {}, markPageCache: () => {}, applyRequisitionHoldsResponse: () => {}, isCancelledRequest: () => false,
};
for (const [name, method] of Object.entries(mixin.methods)) vm[name] = method.bind(vm);
vm.applyRequisitionPendingResponse = methodFromIndex('applyRequisitionPendingResponse(data, {supplierName="", clearSelection=false}={}) {').bind(vm);
vm.loadRequisition = methodFromIndex('async loadRequisition({skipAutoRelease=false}={}) {').bind(vm);

let scenario = 'success';
let resolvePending;
global.axios = {get: async url => {
  if (url === '/api/master/products/7') return {data: {id: 7, common_box_readiness: {ready: true}}};
  if (url === '/api/requisition/holds') return {data: {items: []}};
  assert.equal(url, '/api/requisition/pending');
  if (scenario === 'failure') throw new Error('offline');
  const response = {data: {items: [keep], total: 1, page: 2, supplier_counts: []}};
  if (scenario === 'delayed') return new Promise(resolve => { resolvePending = () => resolve(response); });
  return response;
}};

function resetPending() {
  vm.activePage = 'requisition'; vm.requisitionWorkspace = 'board'; vm.requisitionTab = 'pending';
  vm.requisitionSupplierFilter = '鸣朋'; vm.pages.requisitionPending = 3;
  vm.requisitionPendingAppliedPage = 3; vm.requisitionPendingAppliedSupplierFilter = '鸣朋';
  vm.requisitionPending = [keep, dropped]; vm.selectedPendingKeys = [keep.key, dropped.key];
  vm.requisitionSelected = {[keep.key]: true, [dropped.key]: true}; vm.modal = null; vm.user = {id: 5};
}

(async () => {
  assert.equal(await vm.openRequisitionContextProduct(keep), true);
  assert.equal(vm.modal.title, '补齐常用箱 · P007');
  await vm.restoreOrderContextProduct(7);
  assert.equal(vm.modal, null, 'cancel returns to the real no-modal pending page');
  assert.deepEqual(vm.selectedPendingKeys, [keep.key, dropped.key]);
  assert.equal(vm.pages.requisitionPending, 3);

  assert.equal(await vm.openRequisitionContextProduct(keep), true);
  assert.equal(await vm.restoreOrderContextProduct(7, {saved: true}), true);
  assert.equal(vm.pages.requisitionPending, 2, 'server-clamped page replaces stale page 3');
  assert.deepEqual(vm.selectedPendingKeys, [keep.key], 'only still-visible selection survives refresh');
  assert.deepEqual(vm.requisitionSelected, {}, 'stale legacy selection map is not restored');
  assert.deepEqual(scrollCalls.at(-1), [3, 72], 'pending page viewport is restored after real list response');
  assert.equal(main.scrollTop, 72);
  assert.match(toasts.at(-1)[0], /已返回原报料筛选/);

  resetPending(); scenario = 'failure'; const beforeFailureToasts = toasts.length;
  await vm.openRequisitionContextProduct(keep);
  assert.equal(await vm.restoreOrderContextProduct(7, {saved: true}), false);
  assert.equal(vm.pages.requisitionPending, 3);
  assert.deepEqual(vm.selectedPendingKeys, [keep.key, dropped.key]);
  assert.match(toasts.at(-1)[0], /待报料刷新失败：offline/);
  assert.equal(toasts.length, beforeFailureToasts + 1);

  resetPending(); const actualLoadRequisition = vm.loadRequisition; const beforeFalseToasts = toasts.length;
  vm.loadRequisition = async () => false;
  await vm.openRequisitionContextProduct(keep);
  assert.equal(await vm.restoreOrderContextProduct(7, {saved: true}), false);
  assert.match(toasts.at(-1)[0], /待报料未刷新/);
  assert.equal(toasts.length, beforeFalseToasts + 1);
  vm.loadRequisition = actualLoadRequisition;

  resetPending(); scenario = 'delayed'; const beforePageChangeToasts = toasts.length;
  await vm.openRequisitionContextProduct(keep);
  const pageChangeReturn = vm.restoreOrderContextProduct(7, {saved: true});
  await Promise.resolve(); await Promise.resolve(); vm.activePage = 'orders'; resolvePending();
  assert.equal(await pageChangeReturn, false);
  assert.equal(vm.activePage, 'orders');
  assert.equal(toasts.length, beforePageChangeToasts, 'page switch is silent and never restores stale pending UI');

  resetPending(); scenario = 'delayed'; const beforeLogoutToasts = toasts.length;
  await vm.openRequisitionContextProduct(keep);
  const logoutReturn = vm.restoreOrderContextProduct(7, {saved: true});
  await Promise.resolve(); await Promise.resolve(); vm.user = null; resolvePending();
  assert.equal(await logoutReturn, false);
  assert.equal(toasts.length, beforeLogoutToasts, 'logout is silent and never writes stale view state');
  console.log('pending requisition edit preserves real response state, scroll, and async boundaries');
})().catch(error => { console.error(error); process.exitCode = 1; });
