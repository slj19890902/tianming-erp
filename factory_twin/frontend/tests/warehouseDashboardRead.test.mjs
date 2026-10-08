import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const source = fs.readFileSync(new URL('../src/WarehouseTwinApp.tsx', import.meta.url), 'utf8');
const start = source.indexOf('  const refreshDashboard =');
const end = source.indexOf('\n  useEffect(', start);
const code = ts.transpileModule(source.slice(start, end), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
function fixture() {
  const state = {dashboard: {generated_at: 'previous'}, loading: false, error: ''};
  const requests = [], timers = new Map();
  const context = {URLSearchParams, AbortController, dispatchIdleDays: 3,
    dashboardRequestRef: {current: 0}, dashboardAbortRef: {current: null},
    useCallback: callback => callback,
    window: {setTimeout: callback => {const id = Symbol(); timers.set(id, callback); return id;}, clearTimeout: id => timers.delete(id)},
    setDashboard: value => {state.dashboard = value;},
    setDashboardLoading: value => {state.loading = value;},
    setDashboardError: value => {state.error = value;},
    requestJson: (path, signal) => new Promise((resolve, reject) => requests.push({path, signal, resolve, reject}))};
  const refresh = vm.runInNewContext(code + '\nrefreshDashboard;', context);
  return {state, requests, timers, refresh};
}

test('slow inventory read stays pending until its own response and keeps the previous snapshot', async () => {
  const f = fixture(), read = f.refresh();
  assert.equal(f.state.loading, true);
  assert.equal(f.state.dashboard.generated_at, 'previous');
  assert.match(f.requests[0].path, /^\/api\/warehouse\/twin-dashboard\/overview\?/);
  f.requests[0].resolve({generated_at: 'fresh'});
  await read;
  assert.equal(f.state.loading, false);
  assert.equal(f.state.dashboard.generated_at, 'fresh');
  assert.equal(f.timers.size, 0);
});

test('failed read is explicit and retry recovers without replacing inventory with an empty result', async () => {
  const f = fixture(), failed = f.refresh();
  f.requests[0].reject(new Error('读取失败'));
  await assert.rejects(failed, /读取失败/);
  assert.equal(f.state.loading, false);
  assert.equal(f.state.error, '读取失败');
  assert.equal(f.state.dashboard.generated_at, 'previous');
  const retry = f.refresh();
  assert.equal(f.state.error, '');
  f.requests[1].resolve({generated_at: 'retry'});
  await retry;
  assert.equal(f.state.dashboard.generated_at, 'retry');
});

test('late old success or error cannot replace a newer inventory result or loading state', async () => {
  for (const oldFails of [false, true]) {
    const f = fixture(), old = f.refresh(), latest = f.refresh();
    assert.equal(f.requests[0].signal.aborted, true);
    f.requests[1].resolve({generated_at: 'latest'});
    await latest;
    if (oldFails) f.requests[0].reject(new Error('过期错误'));
    else f.requests[0].resolve({generated_at: 'old'});
    await old;
    assert.equal(f.state.dashboard.generated_at, 'latest');
    assert.equal(f.state.error, '');
    assert.equal(f.state.loading, false);
    assert.equal(f.timers.size, 0);
  }
});

test('timed out inventory request offers a retry and never claims a missing location', async () => {
  const f = fixture(), read = f.refresh();
  [...f.timers.values()][0]();
  assert.equal(f.requests[0].signal.aborted, true);
  f.requests[0].reject(new Error('aborted'));
  await assert.rejects(read);
  assert.match(f.state.error, /超时.*重新读取/);
  assert.equal(f.state.loading, false);
  assert.equal(f.state.dashboard.generated_at, 'previous');
});
