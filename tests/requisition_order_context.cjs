// Isolated view-state check; it neither starts the ERP nor reaches a network service.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
let mixin;
global.window = global;
global.document = {activeElement: null, querySelectorAll: () => [], querySelector: () => null};
eval(fs.readFileSync(path.join(root, 'static/ui/order-context.js'), 'utf8'));
global.ERPOrderContext.install({component() {}, mixin(value) { mixin = value; }});

const row = {item_id: 41, product_id: 7, product_code: 'P007'};
const restored = [];
const toasts = [];
let reloads = 0;
const vm = {
  ...mixin.data(),
  activePage: 'requisition', requisitionWorkspace: 'board', requisitionTab: 'pending',
  requisitionSupplierFilter: '鸣朋', requisitionPending: [row],
  pages: {requisitionPending: 3}, selectedPendingKeys: ['order_item:41'],
  requisitionSelected: {'order_item:41': true}, authGeneration: 9, user: {id: 5},
  canEditProducts: true, masterSavePending: false, modal: {type: 'requisition', title: '待报料'},
  productForm: {product_code: 'P007'}, pendingRowKey: source => `order_item:${source.item_id}`,
  captureOrderContextView: () => ({scroll: [], marker: 'pending-table'}),
  restoreOrderContextView: view => restored.push(view),
  openProduct: async ({id}) => { assert.equal(id, 7); vm.modal = {type: 'product'}; return true; },
  resetProductEditorState: () => { vm.productForm = {}; },
  commonBoxReadiness: product => product.common_box_readiness,
  showToast: (...args) => toasts.push(args), errorMessage: error => error.message,
  $nextTick: callback => callback(),
  loadRequisition: async options => { reloads += 1; assert.deepEqual(options, {skipAutoRelease: true}); },
};
for (const [name, method] of Object.entries(mixin.methods)) vm[name] = method.bind(vm);
vm.captureOrderContextView = () => ({scroll: [], marker: 'pending-table'});
vm.restoreOrderContextView = view => restored.push(view);
global.axios = {get: async url => {
  assert.equal(url, '/api/master/products/7');
  return {data: {id: 7, common_box_readiness: {ready: true}}};
}};

(async () => {
  assert.equal(await vm.openRequisitionContextProduct(row), true);
  assert.equal(vm.modal.title, '补齐常用箱 · P007');
  assert.equal(vm.productEditReturnContext.source, 'requisition');
  await vm.restoreOrderContextProduct(7);
  assert.equal(reloads, 0, 'cancel must not refresh or write the pending list');
  assert.deepEqual(vm.selectedPendingKeys, ['order_item:41']);
  assert.equal(vm.requisitionSupplierFilter, '鸣朋');
  assert.equal(vm.pages.requisitionPending, 3);

  assert.equal(await vm.openRequisitionContextProduct(row), true);
  await vm.restoreOrderContextProduct(7, {saved: true});
  assert.equal(reloads, 1, 'a saved common box refreshes pending read data once');
  assert.deepEqual(vm.selectedPendingKeys, ['order_item:41']);
  assert.equal(vm.requisitionSupplierFilter, '鸣朋');
  assert.equal(vm.pages.requisitionPending, 3);
  assert.deepEqual(restored.map(view => view.marker), ['pending-table', 'pending-table']);
  assert.match(toasts.at(-1)[0], /已返回原报料筛选/);
  console.log('pending requisition context keeps filter, page, selection, and view on cancel/save');
})().catch(error => { console.error(error); process.exitCode = 1; });
