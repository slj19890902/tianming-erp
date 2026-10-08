const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const root = path.resolve(__dirname, '..');
const ts = require(process.env.A01_TYPESCRIPT_LIBRARY);
const helperPath = path.join(root, 'src/utils/orderCreateReadback.ts');
const helper = fs.readFileSync(helperPath, 'utf8');
const compiled = ts.transpileModule(helper, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const context = { exports: {} };
vm.createContext(context);
vm.runInContext(compiled, context, { timeout: 1000 });
const { buildReadbackExpectations, verifyOrderCreateReadback } = context.exports;
fs.writeFileSync(path.join(process.env.A01_NODE_OUTPUT, 'candidate_binding.json'), JSON.stringify({
  helperPath, helperSHA256: crypto.createHash('sha256').update(helper).digest('hex'),
  nodeVersion: process.version, typescriptVersion: ts.version,
  transport: 'synthetic plain objects; no network, authentication, database or DOM',
}, null, 2));

function scenario(input = '', defaultNote = 'product default', mode = 'self_produced', second = false) {
  const payload = { readback_contract: 'a01-v1', customer_id: 1, items: [{
    product_id: 1, product_expected_version: 3, client_line_id: 'line-1', quantity: 2,
    production_notes: input.trim() || undefined,
  }] };
  if (second) payload.items.push({ ...payload.items[0], client_line_id: 'line-2', quantity: 3 });
  const products = { 1: { id: 1, customer_id: 1, version: 3, supply_mode: mode, production_notes: defaultNote } };
  const expected = buildReadbackExpectations(payload, products);
  const effective = mode === 'external_purchase' ? null : input.trim() || defaultNote?.trim() || null;
  const proof = { schema: 'a01-v1', order_id: 1, customer_id: 1, lines: payload.items.map((row, index) => ({
    client_line_id: row.client_line_id, order_item_id: 11 + index, item_sequence: index + 1,
    product_id: 1, customer_id: 1, product_version: 3, supply_mode: mode,
    default_production_notes: defaultNote?.trim() || null,
    expected_production_notes: effective, quantity: String(row.quantity),
  })) };
  const created = { id: 1, customer_id: 1, create_readback: proof, items: proof.lines.map(row => ({
    id: row.order_item_id, product_id: 1, item_sequence: row.item_sequence,
    client_line_id: row.client_line_id, quantity: row.quantity, supply_mode_snapshot: mode,
    snapshot_production_notes: effective,
  })) };
  return { payload, products, expected, created, saved: JSON.parse(JSON.stringify(created)) };
}

for (const [name, input, note, mode] of [
  ['correct default', '', 'product default', 'self_produced'],
  ['whitespace input inherits default', '   ', 'product default', 'self_produced'],
  ['explicit trimmed note', ' explicit ', 'product default', 'self_produced'],
  ['no default produces null', '', null, 'self_produced'],
  ['external produces null even with stored default', '', 'unused default', 'external_purchase'],
]) test(name, () => { const s = scenario(input, note, mode); verifyOrderCreateReadback(s.expected, s.created, s.saved); });

const invalid = [
  ['wrong default snapshot', s => { s.saved.items[0].snapshot_production_notes = 'wrong'; }],
  ['missing default snapshot', s => { s.saved.items[0].snapshot_production_notes = null; }],
  ['missing snapshot field', s => { delete s.saved.items[0].snapshot_production_notes; }],
  ['empty row', s => { s.saved.items[0] = {}; }],
  ['wrong product with same note', s => { s.saved.items[0].product_id = 999; }],
  ['wrong quantity', s => { s.saved.items[0].quantity = 9; }],
  ['precision-losing wrong quantity', s => { s.saved.items[0].quantity = '2.000000000000000001'; }],
  ['wrong line identity', s => { s.saved.items[0].client_line_id = 'other'; }],
  ['wrong saved item identity', s => { s.saved.items[0].id = 999; }],
  ['wrong sequence', s => { s.saved.items[0].item_sequence = 9; }],
  ['wrong customer', s => { s.saved.customer_id = 9; }],
  ['wrong order', s => { s.saved.id = 9; }],
  ['wrong source default even if snapshots agree', s => { for (const r of [s.created, s.saved]) { r.create_readback.lines[0].default_production_notes = 'wrong'; r.create_readback.lines[0].expected_production_notes = 'wrong'; r.items[0].snapshot_production_notes = 'wrong'; } }],
  ['wrong source version', s => { s.created.create_readback.lines[0].product_version = 4; }],
  ['missing proof', s => { delete s.saved.create_readback; }],
  ['missing items', s => { delete s.saved.items; }],
  ['wrong line count', s => { s.saved.items = []; }],
];
for (const [name, mutate] of invalid) test(name, () => { const s = scenario(); mutate(s); assert.throws(() => verifyOrderCreateReadback(s.expected, s.created, s.saved)); });
test('explicit mismatch remains rejected', () => { const s = scenario('explicit'); s.saved.items[0].snapshot_production_notes = null; assert.throws(() => verifyOrderCreateReadback(s.expected, s.created, s.saved)); });
test('external non-null snapshot is rejected', () => { const s = scenario('', 'default', 'external_purchase'); s.saved.items[0].snapshot_production_notes = 'bad'; assert.throws(() => verifyOrderCreateReadback(s.expected, s.created, s.saved)); });
test('duplicate same-product rows are linked by identity despite response reordering', () => { const s = scenario('', 'default', 'self_produced', true); s.saved.items.reverse(); verifyOrderCreateReadback(s.expected, s.created, s.saved); });
test('same-product rows cannot swap quantities', () => { const s = scenario('', 'default', 'self_produced', true); s.saved.items[0].quantity = 3; s.saved.items[1].quantity = 2; assert.throws(() => verifyOrderCreateReadback(s.expected, s.created, s.saved)); });
test('duplicate response row is rejected', () => { const s = scenario('', 'default', 'self_produced', true); s.saved.items[1] = { ...s.saved.items[0] }; assert.throws(() => verifyOrderCreateReadback(s.expected, s.created, s.saved)); });
test('cache without notes is not accepted as an empty default', () => { const s = scenario(); delete s.products[1].production_notes; assert.throws(() => buildReadbackExpectations(s.payload, s.products)); });
test('stale cached version is rejected before POST', () => { const s = scenario(); s.products[1].version = 4; assert.throws(() => buildReadbackExpectations(s.payload, s.products)); });
test('external explicit note is blocked', () => { const s = scenario('', 'default', 'external_purchase'); s.payload.items[0].production_notes = 'bad'; assert.throws(() => buildReadbackExpectations(s.payload, s.products)); });

// Execute the actual submission function with synthetic APIs, without mounting a UI.
const viewPath = path.join(root, 'src/views/OrderView.vue');
const view = fs.readFileSync(viewPath, 'utf8');
const script = view.match(/<script setup[^>]*>([\s\S]*?)<\/script>/)[1];
const ast = ts.createSourceFile('OrderView.ts', script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const submit = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'submitOrder');
assert.ok(submit, 'actual submitOrder must exist');
const finish = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'finishCommittedOrder');
assert.ok(finish, 'actual committed readback recovery must exist');
const submitCode = ts.transpileModule(finish.getText(ast) + '\n' + submit.getText(ast), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
class MockApiError extends Error { constructor(code) { super(code); this.code = code; this.status = 409; } }
function submitContext(s, create) {
  const calls = { post: 0, get: 0, refresh: 0, reset: 0, navigation: 0, success: 0 };
  const refs = { orderForm: { value: { customer_id: 1, note: 'keep draft' } }, orderLines: { value: [{ product_id: 1, quantity: 2, drawing: { token: '' } }] },
    chosenProducts: { value: s.products }, createKey: { value: 'fixed-key' }, loading: { value: false } };
  const ctx = { ...refs, buildOrderPayload: () => ({ ...s.payload, items:s.payload.items.map(row=>({...row,reservation_plan:{finished:[],semi:[]}})), idempotency_key: refs.createKey.value }),
    committedAttempt:{value:null},drawingPreviewSource:{value:''},invalidateDrawing(){},
    inventoryPermitted:{value:false}, inventoryInputs:()=>[], inventory:{states:{value:{}},checked:{value:false}},holdDecisions:{value:{}},
    buildReadbackExpectations, verifyOrderCreateReadback, ApiError: MockApiError,
    orderApi: { createOrder: async p => { calls.post++; return create(p); }, getOrder: async () => { calls.get++; return s.saved; } },
    masterApi: { getProduct: async () => { calls.refresh++; return { ...s.products[1], version: 4 }; } },
    ElMessage: { warning() {}, error() {}, success() { calls.success++; } },
    addOrderLine() { calls.reset++; }, businessDate: () => '2026-10-03', defaultDeliveryDate: () => '2026-10-10', chosenCustomer: { value: 1 },
    products: { value: [] }, tempBox: { value: {} }, tempLine: { value: {} },
    crypto: { randomUUID: () => 'changed-key' }, nextTick: async () => {},
    tabsStore: { markDirty() {} }, router: { push() { calls.navigation++; } } };
  vm.createContext(ctx); vm.runInContext(submitCode, ctx, { timeout: 1000 });
  return { ctx, refs, calls };
}
test('version conflict retains draft and key, refreshes once and never automatically reposts', async () => {
  const s = scenario(); const h = submitContext(s, async () => { throw new MockApiError('ORDER_PRODUCT_VERSION_CONFLICT'); });
  const form = h.refs.orderForm.value; const lines = h.refs.orderLines.value;
  await h.ctx.submitOrder();
  assert.equal(h.calls.post, 1); assert.equal(h.calls.get, 0); assert.equal(h.calls.refresh, 1);
  assert.equal(h.refs.orderForm.value, form); assert.equal(h.refs.orderLines.value, lines);
  assert.equal(h.refs.createKey.value, 'fixed-key'); assert.equal(h.calls.reset, 0); assert.equal(h.calls.navigation, 0);
  assert.equal(h.refs.chosenProducts.value[1].version, 4); assert.equal(h.refs.loading.value, false);
});
test('readback mismatch preserves draft and same key across manual retries', async () => {
  const s = scenario(); s.saved.items[0].snapshot_production_notes = 'wrong';
  const keys = []; const h = submitContext(s, async p => { keys.push(p.idempotency_key); return s.created; });
  const form = h.refs.orderForm.value; const lines = h.refs.orderLines.value;
  await h.ctx.submitOrder(); await h.ctx.submitOrder();
  assert.deepEqual(keys, ['fixed-key']); assert.equal(h.calls.get, 2); assert.equal(h.calls.post, 1); assert.equal(h.calls.success, 0);
  assert.equal(h.calls.reset, 0); assert.equal(h.calls.navigation, 0);
  assert.equal(h.refs.orderForm.value, form); assert.equal(h.refs.orderLines.value, lines);
});
test('only strict successful readback clears draft and rotates key', async () => {
  const s = scenario(); const h = submitContext(s, async () => s.created);
  await h.ctx.submitOrder();
  assert.equal(h.calls.success, 1); assert.equal(h.calls.reset, 1); assert.equal(h.calls.navigation, 1);
  assert.equal(h.refs.createKey.value, 'changed-key'); assert.equal(h.refs.orderLines.value.length, 0);
});

test('failed inventory save recheck prevents POST and retains key and draft', async()=>{
  const s=scenario(),h=submitContext(s,async()=>s.created),form=h.refs.orderForm.value;
  h.ctx.inventoryPermitted.value=true;h.ctx.inventoryInputs=()=>[{}];
  h.ctx.inventory.refresh=async()=>false;h.ctx.inventory.error={value:'batch stale'};
  await h.ctx.submitOrder();assert.equal(h.calls.post,0);assert.equal(h.calls.reset,0);assert.equal(h.refs.orderForm.value,form);assert.equal(h.refs.createKey.value,'fixed-key');
});
test('saved inventory mismatch retains draft without a second POST',async()=>{
  const s=scenario(),h=submitContext(s,async()=>s.created),line=s.payload.items[0],item=s.saved.items[0];
  h.ctx.inventoryPermitted.value=true;h.ctx.inventoryInputs=()=>[{}];
  h.ctx.inventory={checked:{value:true},error:{value:''},refresh:async()=>true,states:{value:{[line.client_line_id]:{plan:{finished:[{lot_id:2,expected_version:1,requested_qty:2,confirmed:true,recommendation_source:'dedicated'}],semi:[]},allocations:[{part:'finished',lot_id:2,stock_quantity:2}]}}}};
  h.ctx.warehouseApi={getLot:async()=>({reservations:[{order_item_id:item.id,status:'active',reserved_stock_quantity:1}]})};
  await h.ctx.submitOrder();assert.equal(h.calls.post,1);assert.equal(h.calls.reset,0);assert.equal(h.calls.navigation,0);assert.equal(h.refs.createKey.value,'fixed-key');
});
