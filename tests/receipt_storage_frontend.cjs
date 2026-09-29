const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const start = html.indexOf('          async loadReceiptStorage()');
const end = html.indexOf('          customerPhoneAnalysis(', start);
assert(start >= 0 && end > start);
let get, put;
const methods = vm.runInNewContext(`({${html.slice(start, end)}})`, {
  axios: {get: (...args) => get(...args), put: (...args) => put(...args)},
  createIdempotencyKey: () => 'test-key',
});
const locations = [
  {location_id: 101, area_id: 10, floor: 3, name: 'R013 2层 2格', address_version: 2, layout_version: 4},
  {location_id: 102, area_id: 10, floor: 3, name: 'R014 1层 1格', address_version: 3, layout_version: 5},
];
const pref = {product_id: 1, configured: true, version: 1, area_id: 10, location_id: 101, location: locations[0], locations};

(async () => {
  const state = {productForm: {id: 1}, ...methods};
  get = async () => ({data: pref});
  await state.loadReceiptStorage();
  assert.equal(state.receiptStorage.saved_label, locations[0].name);
  state.receiptStorage.location_id = 102;
  assert.equal(state.receiptStorage.saved_label, locations[0].name, 'unsaved choice must not impersonate saved preference');
  let payload;
  put = async (_url, value) => {
    payload = value;
    return {data: {...pref, version: 2, location_id: 102, location: locations[1]}};
  };
  await state.saveReceiptStorage(false);
  assert.equal(payload.expected_version, 1);
  assert.equal(payload.address_version, 3);
  assert.equal(payload.layout_version, 5);
  assert.equal(state.receiptStorage.saved_label, locations[1].name);
  put = async (_url, value) => {
    payload = value;
    return {data: {...pref, version: 3, area_id: null, location_id: null, location: null}};
  };
  await state.saveReceiptStorage(true);
  assert.equal(payload.area_id, null);
  assert.equal(payload.location_id, null);
  assert.equal(payload.expected_version, 2);
  assert.equal(state.receiptStorage.saved_label, '已取消自动记忆');

  get = async () => {throw {response: {data: {detail: '无读取权限'}}};};
  await state.loadReceiptStorage();
  assert.equal(state.receiptStorage.error, '无读取权限');
  assert.equal(state.receiptStorage.loading, false);
  let called = false;
  put = async () => {called = true; throw new Error('unexpected save');};
  await state.saveReceiptStorage(true);
  assert.equal(called, false);

  let finish;
  get = () => new Promise(resolve => {finish = resolve;});
  const oldRequest = state.loadReceiptStorage();
  state.productForm.id = 2;
  state.receiptStorage = {product_id: 2, saved_label: '第二款'};
  finish({data: pref});
  await oldRequest;
  assert.equal(state.receiptStorage.product_id, 2);
  assert.equal(state.receiptStorage.saved_label, '第二款');
  console.log('PASS storage read/save/cancel, saved versus draft, failure guard and stale-product response');
})().catch(error => {console.error(error); process.exitCode = 1;});
