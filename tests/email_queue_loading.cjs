const fs = require('fs');
const vm = require('vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('static/index.html', 'utf8');
const names = [
  'openEmailQueue', 'openOrderPdfImport', 'prepareImportDraftReminderState',
  'restorePdfSaveAttempt', 'reloadPdfSaveRecovery', 'preparePdfImportItem',
  'refreshPdfPriceConflict', 'normalizedProductSyncValue',
  'orderSpecificationText', 'newOrderInventoryState',
];

function extract(name, context) {
  const match = new RegExp('^([\\t ]*)(?:async )?' + name + '\\(', 'm').exec(source);
  assert.ok(match, `Current frontend method missing: ${name}`);
  const closing = '\n' + match[1] + '},';
  const end = source.indexOf(closing, match.index);
  assert.ok(end > match.index, `Current frontend method end missing: ${name}`);
  return vm.runInContext('({' + source.slice(match.index, end + closing.length) + '})', context)[name];
}

async function run(data, { fail = false, role = 'admin', canCreate = true, records = [] } = {}) {
  const requests = [];
  let state;
  const context = vm.createContext({
    axios: { post: async (...args) => {
      requests.push(args);
      assert.equal(state.orderImportBatch.status, 'recognizing');
      assert.equal(state.emailQueueError, '');
      if (fail) throw Error('fixture connection failed');
      return { data };
    } },
    createIdempotencyKey: () => 'fixture',
  });
  state = {
    user: { id: 7, role }, canCreateOrders: canCreate,
    // Storage and network boundaries are synthetic; page methods are current.
    pdfSaveStore: () => ({ all: () => records, find: hash => records.filter(r => r.hash === hash) }),
    loadOrderImportReminders: async () => {}, loadOrderLineInventory: async () => {},
    refreshEmailQueueCount: async () => {}, errorMessage: error => error.message,
    showToast: () => {},
  };
  for (const name of names) state[name] = extract(name, context);
  await state.openEmailQueue();
  return { state, requests };
}

(async () => {
  const data = { drafts: [
    { file_hash: 'a', email_queue_status: 'ready', items: [
      { matched_product_id: 2, specification: '100×200mm', unit_price: 3, product_default_price: 4 },
    ] },
    { file_hash: 'b', email_queue_status: 'improve', items: [] },
  ] };
  let { state, requests } = await run(data);
  assert.equal(state.orderImportDrafts.length, 2);
  assert.equal(state.emailQueueFilter, 'ready');
  assert.equal(state.orderImportBatch.status, 'recognized');
  assert.equal(state.orderImportDrafts[0].items[0].specification, '100×200');
  assert.equal(state.orderImportDrafts[0].items[0].price_conflict.product_id, 2);
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], '/api/email-intake/queue/preview');
  assert.equal(requests[0][1], null);
  assert.equal(requests[0][2].responseType, 'json');

  const record = { hash: 'a', actorId: 7, key: 'old-attempt', bodyJson: '{"original":true}', storageKey: 'fixture' };
  ({ state } = await run(data, { records: [record] }));
  assert.equal(state.orderImportDrafts[0]._save_status, 'unknown');
  assert.equal(state.orderImportDrafts[0]._save_payload, record.bodyJson);
  assert.equal(state.orderImportDrafts[0].confirmed, false);
  assert.equal(state.orderImportRecoveryRows.length, 1);

  for (const bad of ['<html>proxy failure</html>', null, { drafts: {} }]) {
    ({ state } = await run(bad));
    assert.equal(state.orderImportBatch.status, 'failed');
    assert.equal(state.emailQueueError, '订单列表返回异常，请重新读取。');
    assert.equal(state.orderImportDrafts.length, 0);
  }
  ({ state } = await run(null, { fail: true }));
  assert.equal(state.orderImportBatch.status, 'failed');
  assert.equal(state.emailQueueError, 'fixture connection failed');
  assert.equal(state.orderImportDrafts.length, 0);
  ({ state } = await run({ drafts: [] }));
  assert.equal(state.orderImportBatch.status, 'recognized');
  assert.equal(state.emailQueueFilter, 'improve');
  for (const options of [{ role: 'worker' }, { canCreate: false }]) {
    ({ state, requests } = await run(data, options));
    assert.equal(requests.length, 0);
    assert.equal(state.modal, undefined);
  }
  assert.ok(source.includes("orderImportBatch.status==='recognizing'\" role=\"status\""));
  console.log('queue hydration, saved-request preservation, invalid responses, network failure, empty queue, permission and loading checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
