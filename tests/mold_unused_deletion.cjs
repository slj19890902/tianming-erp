const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('static/warehouse.html', 'utf8');
const source = html.slice(html.indexOf('    function moldDeletionBusy()'), html.indexOf('    async function enableLegacyMold(id)'));
function setup() {
  const fields = {moldId: {value: '17'}, moldVersion: {value: '3'}, moldDeleteButton: {disabled: false}};
  const calls = [], messages = [];
  const ctx = {state: {readOnly: false, user: {role: 'admin'}, molds: [{id: 17, mold_name: '误建模具'}],
      moldEditLocation: {}, moldBoundProducts: {}, moldBinding: {}, moldSelection: new Set([17])},
    $(id) {return fields[id];}, toast(message) {messages.push(message);}, updateMoldSaveAvailability() {},
    window: {confirm() {ctx.confirmCount++; return ctx.confirmResult;}}, confirmResult: true, confirmCount: 0,
    createIdempotencyKey() {ctx.keys++; return 'original-key-001';}, keys: 0,
    async api(url, options) {calls.push({url, options}); return ctx.respond(url, options);},
    respond(url, options) {return options ? {id: 17, deleted: true} : {id: 17, version: 3, eligible: true};},
    async loadMolds() {ctx.refreshes++; return ctx.refreshResult;}, refreshes: 0, refreshResult: true,
    resetMoldForm() {ctx.closed++;}, closed: 0};
  vm.createContext(ctx); vm.runInContext(source, ctx);
  return {ctx, fields, calls, messages};
}
(async () => {
  let t = setup(); t.ctx.confirmResult = false; await t.ctx.deleteMold();
  assert.equal(t.calls.length, 1); assert.equal(t.ctx.keys, 0); assert.equal(t.ctx.closed, 0); // cancel / Escape
  t = setup(); t.ctx.respond = () => ({eligible: false, reasons: ['已有历史绑定']}); await t.ctx.deleteMold();
  assert.equal(t.calls.length, 1); assert.equal(t.ctx.confirmCount, 0); assert.match(t.messages[0], /历史绑定/);
  t = setup(); await t.ctx.deleteMold();
  assert.equal(t.ctx.confirmCount, 1); assert.equal(t.ctx.closed, 1); assert.equal(t.ctx.state.moldSelection.size, 0);
  assert.deepEqual(JSON.parse(t.calls[1].options.body), {expected_version: 3, idempotency_key: 'original-key-001', confirmed: true});
  t = setup(); let fail = true;
  t.ctx.respond = (_, options) => {if (options && fail) throw new Error('network disconnected'); return options ? {id: 17, deleted: true} : {eligible: true, version: 3};};
  await t.ctx.deleteMold(); assert.equal(t.ctx.moldDeletionBusy(), true); assert.equal(t.ctx.closed, 0);
  fail = false; await t.ctx.deleteMold();
  assert.equal(t.ctx.confirmCount, 1); assert.equal(t.ctx.keys, 1); assert.equal(t.calls[1].options.body, t.calls[2].options.body);
  t = setup(); t.ctx.refreshResult = false; await t.ctx.deleteMold();
  assert.equal(t.ctx.state.moldDeleteAttempt.committed, true); t.ctx.refreshResult = true; await t.ctx.deleteMold();
  assert.equal(t.calls.length, 2, 'a successful delete with failed refresh must never be sent again');
  t = setup(); t.ctx.respond = () => ({eligible: true, version: 4}); await t.ctx.deleteMold();
  assert.equal(t.ctx.confirmCount, 0); assert.equal(t.calls.length, 1);
  t = setup(); t.ctx.state.user.role = 'sales'; await t.ctx.deleteMold(); assert.equal(t.calls.length, 0);
  t = setup(); let resolve; t.ctx.respond = () => new Promise(r => {resolve = r;});
  const first = t.ctx.deleteMold(); await t.ctx.deleteMold(); assert.equal(t.calls.length, 1);
  t.fields.moldId.value = '18'; resolve({eligible: true, version: 3}); await first;
  assert.equal(t.ctx.confirmCount, 0, 'stale eligibility must not delete a different editor record');
  for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) if (match[1].trim()) new vm.Script(match[1]);
  console.log('8 mold delete UI cases passed: cancel, blockers, confirmation, network replay, refresh-only retry, version, permissions and stale requests');
})().catch(error => {console.error(error); process.exitCode = 1;});
