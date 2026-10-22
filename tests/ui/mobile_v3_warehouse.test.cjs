const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../../frontend-v2/node_modules/typescript');
const base = path.resolve(__dirname, '../../frontend-v2/src/mobile');
const tick = () => new Promise(resolve => setImmediate(resolve));

function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function evaluate(source, modules, redirects = []) {
  const module = { exports: {} };
  const context = {
    module, exports: module.exports, require: name => modules[name] || {},
    URL, URLSearchParams, AbortController,
    window: { location: { origin: 'http://localhost', assign: url => redirects.push(url) } },
  };
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(compiled, context);
  return module.exports;
}

function setup(api, query = {}) {
  const watchers = [], unmounts = [], redirects = [];
  const auth = { user: { id: 1 } };
  const vue = {
    ref: value => ({ value }),
    computed: get => ({ get value() { return get(); } }),
    nextTick: async () => {},
    onMounted: () => {},
    onBeforeUnmount: fn => unmounts.push(fn),
    watch: (source, fn) => watchers.push({ source, fn }),
  };
  const source = fs.readFileSync(path.join(base, 'MobileWarehouse.vue'), 'utf8')
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
    + '\nmodule.exports = { load, loadFloor, floors, floor, areas, loading, error };';
  const state = evaluate(source, {
    vue,
    'vue-router': { useRoute: () => ({ query }) },
    '../stores/auth': { useAuthStore: () => auth },
    './api': {
      mobileApi: api,
      warehouseAreaUrl: (floor, area, location) => `/area/${floor}/${area}/${location}`,
    },
    './format': { safeMessage: e => String(e.message || e), compactText: String, shortLocation: String },
  }, redirects);
  return {
    ...state, redirects,
    unmount: () => unmounts.forEach(fn => fn()),
    switchUser: user => {
      const previous = watchers.map(w => w.source());
      auth.user = user;
      watchers.forEach((w, i) => { if (w.source() !== previous[i]) w.fn(); });
    },
  };
}

const floor = code => ({ floor_code: code, floor_number: Number(code[0]) });
const area = code => ({ area_code: code, points: [[0, 0], [10, 0], [10, 10]] });

test('floors API forwards the cancellation signal to the shared HTTP client', async () => {
  const calls = [];
  const { mobileApi } = evaluate(fs.readFileSync(path.join(base, 'api.ts'), 'utf8'), {
    '../api/client': { API_BASE: '', request: async (...args) => { calls.push(args); return { floors: [] }; } },
  });
  const controller = new AbortController();
  await mobileApi.floors(controller.signal);
  assert.equal(calls[0][0], '/api/mobile/erp/warehouse/map/floors');
  assert.equal(calls[0][1].signal, controller.signal);
});

test('current warehouse deep link still opens the selected floor and location', async () => {
  const requested = [];
  const h = setup({
    floors: async () => ({ floors: [floor('2F'), floor('3F')] }),
    overviewMap: async code => { requested.push(code); return { areas: [area('A')] }; },
  }, { floor: '2F', area: 'A', location: '17' });
  await h.load();
  assert.deepEqual(requested, ['2F']);
  assert.deepEqual(h.redirects, ['/area/2F/A/17']);
  assert.equal(h.loading.value, false);
});

test('leaving during the floor request aborts it and prevents later map loads or redirects', async () => {
  const late = deferred(); let signal, maps = 0;
  const h = setup({
    floors: current => { signal = current; return late.promise; },
    overviewMap: async () => { maps++; return { areas: [area('A')] }; },
  }, { floor: '3F', area: 'A' });
  const reading = h.load();
  h.unmount();
  assert.equal(signal.aborted, true);
  late.resolve({ floors: [floor('3F')] });
  await reading;
  assert.equal(maps, 0);
  assert.equal(h.floors.value.length, 0);
  assert.equal(h.areas.value.length, 0);
  assert.equal(h.redirects.length, 0);
});

test('leaving during a map request prevents late facts and old-context navigation', async () => {
  const late = deferred(); let signal;
  const h = setup({
    floors: async () => ({ floors: [floor('3F')] }),
    overviewMap: (_code, current) => { signal = current; return late.promise; },
  }, { area: 'A' });
  const reading = h.load(); await tick();
  h.unmount();
  assert.equal(signal.aborted, true);
  late.resolve({ areas: [area('A')] }); await reading;
  assert.equal(h.areas.value.length, 0);
  assert.equal(h.redirects.length, 0);
});

test('account switch discards old floor response and loads only the new account map', async () => {
  const oldFloors = deferred(), newFloors = deferred();
  const signals = [], requested = [];
  const h = setup({
    floors: signal => { signals.push(signal); return signals.length === 1 ? oldFloors.promise : newFloors.promise; },
    overviewMap: async code => { requested.push(code); return { areas: [area('B')] }; },
  });
  const oldReading = h.load();
  h.switchUser({ id: 2 });
  assert.equal(signals[0].aborted, true);
  oldFloors.resolve({ floors: [floor('3F')] }); await oldReading;
  assert.equal(h.floors.value.length, 0);
  assert.equal(h.loading.value, true);
  newFloors.resolve({ floors: [floor('2F')] }); await tick();
  assert.deepEqual(requested, ['2F']);
  assert.equal(h.floor.value, '2F');
  assert.equal(h.areas.value[0].area_code, 'B');
  assert.equal(h.loading.value, false);
});

test('account switch clears old facts and ignores a late old-account map redirect', async () => {
  const oldMap = deferred(), newFloors = deferred(); let reads = 0, mapSignal;
  const h = setup({
    floors: async () => ++reads === 1 ? { floors: [floor('3F')] } : newFloors.promise,
    overviewMap: (_code, signal) => { mapSignal = signal; return oldMap.promise; },
  }, { area: 'A' });
  const reading = h.load(); await tick();
  h.switchUser({ id: 2 });
  assert.equal(mapSignal.aborted, true);
  assert.equal(h.floors.value.length, 0);
  assert.equal(h.floor.value, '');
  assert.equal(h.areas.value.length, 0);
  oldMap.resolve({ areas: [area('A')] }); await reading;
  assert.equal(h.redirects.length, 0);
  assert.equal(h.loading.value, true);
  newFloors.reject(new Error('new account has no warehouse permission')); await tick();
  assert.match(h.error.value, /no warehouse permission/);
  assert.equal(h.loading.value, false);
});

test('logging out clears displayed map facts and does not request another account', async () => {
  let reads = 0;
  const h = setup({
    floors: async () => { reads++; return { floors: [floor('3F')] }; },
    overviewMap: async () => ({ areas: [area('A')] }),
  });
  await h.load(); assert.equal(h.areas.value.length, 1);
  h.switchUser(null);
  assert.equal(h.floors.value.length, 0);
  assert.equal(h.floor.value, '');
  assert.equal(h.areas.value.length, 0);
  assert.equal(h.error.value, '');
  assert.equal(h.loading.value, false);
  await h.load(); await h.loadFloor('3F');
  assert.equal(reads, 1);
});

test('retry keeps the latest floor request active when a previous failure arrives late', async () => {
  const first = deferred(), second = deferred(); let reads = 0;
  const h = setup({ floors: () => ++reads === 1 ? first.promise : second.promise });
  const oldReading = h.load(), currentReading = h.load();
  first.reject(new Error('old failure')); await oldReading;
  assert.equal(h.error.value, '');
  assert.equal(h.loading.value, true);
  second.resolve({ floors: [] }); await currentReading;
  assert.equal(h.loading.value, false);
  assert.equal(h.error.value, '');
});

test('switching floor keeps the current map while an older map completes', async () => {
  const first = deferred(), second = deferred(); const signals = [];
  const h = setup({ overviewMap: (_code, signal) => { signals.push(signal); return signals.length === 1 ? first.promise : second.promise; } });
  const oldReading = h.loadFloor('3F'), currentReading = h.loadFloor('2F');
  assert.equal(signals[0].aborted, true);
  first.resolve({ areas: [area('OLD')] }); await oldReading;
  assert.equal(h.areas.value.length, 0);
  assert.equal(h.loading.value, true);
  second.resolve({ areas: [area('CURRENT')] }); await currentReading;
  assert.equal(h.floor.value, '2F');
  assert.equal(h.areas.value[0].area_code, 'CURRENT');
  assert.equal(h.loading.value, false);
});
