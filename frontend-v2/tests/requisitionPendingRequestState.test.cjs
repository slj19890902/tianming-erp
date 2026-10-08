'use strict';
// Execute the actual RequisitionView script with memory-only pending transports.
// ERP_FRONTEND_DEPS optionally selects an existing read-only dependency directory.
// ERP_REQUISITION_TEST_EVIDENCE optionally saves the result outside product source.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');

const sourcePath = path.resolve(__dirname, '../src/views/RequisitionView.vue');
const deps = process.env.ERP_FRONTEND_DEPS || path.resolve(__dirname, '../node_modules');
const ts = require(path.join(deps, 'typescript/lib/typescript.js'));
const Vue = require(path.join(deps, 'vue'));
const sfc = require(path.join(deps, '@vue/compiler-sfc'));
const sourceBytes = fs.readFileSync(sourcePath);
const parsed = sfc.parse(sourceBytes.toString('utf8'), { filename: sourcePath });
assert.deepEqual(parsed.errors, []);
const script = ts.transpileModule(parsed.descriptor.scriptSetup.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
};
const row = (id, supplier, extra = {}) => ({
  item_id: id, order_number: `SYNTHETIC-PO-${id}`, customer_id: 91,
  customer_name: 'SYNTHETIC CUSTOMER', product_code: `SYN-${id}`,
  product_name: `SYNTHETIC ITEM ${id}`, snapshot_supplier_name: supplier,
  quantity: 500, requisition_qty: 600, inventory_deducted_qty: 0,
  suggested_cardboard_len: '800', suggested_cardboard_width: '600',
  special_process: '无', ...extra,
});
const a = row(101, 'SYNTHETIC SUPPLIER A');
const b = row(102, 'SYNTHETIC SUPPLIER B');
let scriptExecutions = 0;
function harness() {
  const errors = [], requests = [], mutations = [], beforeUnmount = [];
  const unexpectedMutation = name => () => { mutations.push(name); throw Error('Forbidden test transport ' + name); };
  const sandbox = {
    exports: {}, Error,
    window: { location: { origin: 'http://localhost', hostname: 'localhost' } },
    crypto: { randomUUID: () => { throw Error('No submission expected'); } },
    require: id => {
      if (id === 'vue') return {
        ...Vue, onMounted: () => {}, onBeforeUnmount: callback => beforeUnmount.push(callback),
      };
      if (id === 'element-plus') return {
        ElMessage: { error: value => errors.push(String(value)), warning: unexpectedMutation('warning'), success: unexpectedMutation('success') },
        ElMessageBox: { confirm: unexpectedMutation('confirm') },
      };
      if (id === 'qrcode.vue') return {};
      if (id === '../stores/auth') return { useAuthStore: () => ({ user: { role: 'synthetic-reader' }, hasPermission: () => false }) };
      if (id === '../api/client') return {
        requisitionApi: {
          listPending: () => { const d = deferred(); requests.push(d); return d.promise; },
          listItems: unexpectedMutation('listItems'), createBatch: unexpectedMutation('createBatch'), cancelItem: unexpectedMutation('cancelItem'),
        },
        SPECIAL_PROCESSES: ['无', '大做小', '双拼', '多拼'], ApiError: class ApiError extends Error {},
      };
      throw Error('Unexpected SFC import: ' + id);
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(script + '\nglobalThis.exposed = {loadPending, items, selectedIds, rowParams, loading, keyword, activeTab, toggleSelect, visibleSelectedCount, hiddenSelectedCount};', sandbox, { filename: sourcePath, timeout: 1500 });
  scriptExecutions++;
  return { state: sandbox.exposed, errors, requests, mutations, unmount: () => beforeUnmount.forEach(callback => callback()) };
}
function snapshot(h) {
  return {
    items: Array.from(h.state.items.value, item => ({ id: item.item_id, supplier: item.snapshot_supplier_name })),
    selectedIds: Array.from(h.state.selectedIds.value),
    parameterIds: Object.keys(h.state.rowParams.value),
    parameters102: h.state.rowParams.value[102] ? { ...h.state.rowParams.value[102] } : null,
    loading: h.state.loading.value, activeTab: h.state.activeTab.value,
    visibleSelectedCount: h.state.visibleSelectedCount.value,
    hiddenSelectedCount: h.state.hiddenSelectedCount.value,
    errors: [...h.errors],
  };
}
async function seed(h, rows) {
  const work = h.state.loadPending();
  h.requests.at(-1).resolve({ items: rows });
  await work;
}
const cases = [
  // The four original failing/control cases retain their original assertions.
  ['late older success overwrites the current list and loses a newly selected row', async evidence => {
    const h = harness();
    await seed(h, [a]);
    const older = h.state.loadPending();
    const olderTransport = h.requests.at(-1);
    const newer = h.state.loadPending();
    const newerTransport = h.requests.at(-1);
    newerTransport.resolve({ items: [a, b] });
    await newer;
    h.state.toggleSelect(102);
    h.state.rowParams.value[102].requisition_qty = '610';
    h.state.activeTab.value = 'records';
    h.state.activeTab.value = 'pending';
    evidence.afterLatestAndSelection = snapshot(h);
    olderTransport.resolve({ items: [a] });
    await older;
    evidence.afterLateOlderResponse = snapshot(h);
    evidence.expected = { itemIds: [101, 102], selectedIds: [102], editedQuantity102: '610' };
    evidence.syntheticMutationCalls = h.mutations;
    assert.deepEqual(Array.from(h.state.items.value, item => item.item_id), [101, 102]);
    assert.deepEqual(Array.from(h.state.selectedIds.value), [102]);
    assert.equal(h.state.rowParams.value[102].requisition_qty, '610');
  }],
  ['older completion clears loading while the newest request is unresolved', async evidence => {
    const h = harness();
    await seed(h, [a]);
    const older = h.state.loadPending();
    const olderTransport = h.requests.at(-1);
    const newer = h.state.loadPending();
    const newerTransport = h.requests.at(-1);
    olderTransport.resolve({ items: [a] });
    await older;
    evidence.whileLatestUnresolved = snapshot(h);
    newerTransport.resolve({ items: [a, b] });
    await newer;
    evidence.afterLatest = snapshot(h);
    evidence.expectedLoadingWhileLatestUnresolved = true;
    evidence.syntheticMutationCalls = h.mutations;
    assert.equal(evidence.whileLatestUnresolved.loading, true);
  }],
  ['older failure still reports an error after the latest response succeeds', async evidence => {
    const h = harness();
    await seed(h, [a]);
    const older = h.state.loadPending();
    const olderTransport = h.requests.at(-1);
    const newer = h.state.loadPending();
    const newerTransport = h.requests.at(-1);
    newerTransport.resolve({ items: [a, b] });
    await newer;
    evidence.afterLatest = snapshot(h);
    olderTransport.reject(Error('SYNTHETIC OLD TRANSPORT FAILURE'));
    await older;
    evidence.afterOldFailure = snapshot(h);
    evidence.expectedErrors = [];
    evidence.syntheticMutationCalls = h.mutations;
    assert.deepEqual(h.errors, []);
  }],
  ['control: newest authoritative removal still prunes invalid selection', async evidence => {
    const h = harness();
    await seed(h, [a, b]);
    h.state.toggleSelect(102);
    await seed(h, [a]);
    evidence.observed = snapshot(h);
    evidence.syntheticMutationCalls = h.mutations;
    assert.deepEqual(Array.from(h.state.selectedIds.value), []);
    assert.equal(h.state.rowParams.value[102], undefined);
    assert.equal(h.state.loading.value, false);
  }],
  ['newest failure stays authoritative when an older success arrives later', async evidence => {
    const h = harness();
    await seed(h, [a, b]);
    h.state.toggleSelect(102);
    h.state.rowParams.value[102].requisition_qty = '610';
    const older = h.state.loadPending(), olderTransport = h.requests.at(-1);
    const newer = h.state.loadPending(), newerTransport = h.requests.at(-1);
    newerTransport.reject(Error('SYNTHETIC LATEST FAILURE'));
    await newer;
    evidence.afterLatestFailure = snapshot(h);
    olderTransport.resolve({ items: [a] });
    await older;
    evidence.afterOlderSuccess = snapshot(h);
    assert.deepEqual(Array.from(h.state.items.value, item => item.item_id), [101, 102]);
    assert.deepEqual(Array.from(h.state.selectedIds.value), [102]);
    assert.equal(h.state.rowParams.value[102].requisition_qty, '610');
    assert.deepEqual(h.errors, ['SYNTHETIC LATEST FAILURE']);
    assert.equal(h.state.loading.value, false);
  }],
  ['older failure while newest is pending cannot report or finish newest load', async evidence => {
    const h = harness();
    const older = h.state.loadPending(), olderTransport = h.requests.at(-1);
    const newer = h.state.loadPending(), newerTransport = h.requests.at(-1);
    olderTransport.reject(Error('SYNTHETIC OLD FAILURE'));
    await older;
    evidence.whileLatestUnresolved = snapshot(h);
    newerTransport.resolve({ items: [a, b] });
    await newer;
    evidence.afterLatest = snapshot(h);
    assert.equal(evidence.whileLatestUnresolved.loading, true);
    assert.deepEqual(h.errors, []);
    assert.deepEqual(Array.from(h.state.items.value, item => item.item_id), [101, 102]);
  }],
  ['newest failure then older failure produces only the newest error', async evidence => {
    const h = harness();
    const older = h.state.loadPending(), olderTransport = h.requests.at(-1);
    const newer = h.state.loadPending(), newerTransport = h.requests.at(-1);
    newerTransport.reject(Error('SYNTHETIC LATEST FAILURE'));
    await newer;
    olderTransport.reject(Error('SYNTHETIC OLD FAILURE'));
    await older;
    evidence.observed = snapshot(h);
    assert.deepEqual(h.errors, ['SYNTHETIC LATEST FAILURE']);
    assert.equal(h.state.loading.value, false);
  }],
  ['current request failure keeps the existing rows and ends loading with feedback', async evidence => {
    const h = harness();
    await seed(h, [a, b]);
    h.state.toggleSelect(102);
    const work = h.state.loadPending();
    h.requests.at(-1).reject(Error('SYNTHETIC CURRENT FAILURE'));
    await work;
    evidence.observed = snapshot(h);
    assert.deepEqual(Array.from(h.state.items.value, item => item.item_id), [101, 102]);
    assert.deepEqual(Array.from(h.state.selectedIds.value), [102]);
    assert.deepEqual(h.errors, ['SYNTHETIC CURRENT FAILURE']);
    assert.equal(h.state.loading.value, false);
  }],
  ['response after before-unmount cannot change selection or edited parameters', async evidence => {
    const h = harness();
    await seed(h, [a, b]);
    h.state.toggleSelect(102);
    h.state.rowParams.value[102].requisition_qty = '610';
    const work = h.state.loadPending();
    const beforeDispose = snapshot(h);
    h.unmount();
    h.requests.at(-1).resolve({ items: [a] });
    await work;
    evidence.beforeDispose = beforeDispose;
    evidence.afterLateResponse = snapshot(h);
    assert.deepEqual(snapshot(h), beforeDispose);
  }],
  ['failure after before-unmount does not emit feedback or mutate pending state', async evidence => {
    const h = harness();
    await seed(h, [a]);
    const work = h.state.loadPending();
    const beforeDispose = snapshot(h);
    h.unmount();
    h.requests.at(-1).reject(Error('SYNTHETIC DISPOSED FAILURE'));
    await work;
    evidence.beforeDispose = beforeDispose;
    evidence.afterFailure = snapshot(h);
    assert.deepEqual(snapshot(h), beforeDispose);
    assert.deepEqual(h.errors, []);
  }],
  ['async follow-up after before-unmount cannot start another pending request', async evidence => {
    const h = harness();
    await seed(h, [a]);
    h.unmount();
    const callsBefore = h.requests.length;
    const work = h.state.loadPending();
    // Always settle an unexpected transport so the test cannot hang on a broken guard.
    if (h.requests.length > callsBefore) h.requests.at(-1).resolve({ items: [b] });
    await work;
    evidence.callsBefore = callsBefore;
    evidence.callsAfter = h.requests.length;
    evidence.observed = snapshot(h);
    assert.equal(h.requests.length, callsBefore);
    assert.deepEqual(Array.from(h.state.items.value, item => item.item_id), [101]);
  }],
  ['current successful refresh keeps active selection and existing edited parameters', async evidence => {
    const h = harness();
    await seed(h, [a, b]);
    h.state.toggleSelect(102);
    h.state.rowParams.value[102].requisition_qty = '610';
    h.state.rowParams.value[102].cardboard_len = '810';
    await seed(h, [row(101, 'SYNTHETIC SUPPLIER A'), row(102, 'SYNTHETIC SUPPLIER B', { requisition_qty: 700 })]);
    evidence.observed = snapshot(h);
    assert.deepEqual(Array.from(h.state.selectedIds.value), [102]);
    assert.equal(h.state.rowParams.value[102].requisition_qty, '610');
    assert.equal(h.state.rowParams.value[102].cardboard_len, '810');
    assert.equal(h.state.items.value.find(item => item.item_id === 102).quantity, 500);
    assert.equal(h.state.loading.value, false);
  }],
];

(async () => {
  const results = [];
  for (const [name, run] of cases) {
    const evidence = {};
    try { await run(evidence); results.push({ name, passed: true, evidence }); }
    catch (error) { results.push({ name, passed: false, errorType: error.name, error: error.message, evidence }); }
  }
  const report = {
    checkedUTC: new Date().toISOString(), sourcePath,
    sourceSHA256: crypto.createHash('sha256').update(sourceBytes).digest('hex').toUpperCase(),
    testPath: __filename,
    testSHA256: crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex').toUpperCase(),
    depsPath: deps, versions: { node: process.version, typescript: ts.version, vue: Vue.version, compilerSfc: sfc.version },
    actualSFCScriptExecuted: true, scriptExecutions, syntheticMemoryOnly: true,
    lifecycleScope: 'actual registered onBeforeUnmount callback; not a browser or mounted Element Plus/VXE test',
    networkCalls: 0, realDatabaseOrSessionRead: false, A08ExistingAcceptanceRerun: false,
    passCount: results.filter(r => r.passed).length, failCount: results.filter(r => !r.passed).length, results,
  };
  if (process.env.ERP_REQUISITION_TEST_EVIDENCE) {
    fs.writeFileSync(process.env.ERP_REQUISITION_TEST_EVIDENCE, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
  }
  console.log(JSON.stringify({ sourceSHA256: report.sourceSHA256, passCount: report.passCount, failCount: report.failCount }));
  if (report.failCount) process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 2; });
