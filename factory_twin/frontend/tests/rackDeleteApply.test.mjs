import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const source = fs.readFileSync(new URL('../src/WarehouseTwinApp.tsx', import.meta.url), 'utf8');
const start = source.indexOf('  const deleteSelectedRack =');
// Stop at the next top-level handler, not the handler's local declarations.
const next = source.indexOf('\n  const ', start + 30);
assert.ok(start > 0 && next > start);
const code = ts.transpileModule(source.slice(start, next), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;

function fixture(overrides = {}) {
  const rack = { id: 'rack-f7', name: '货F7', version: 3 };
  const other = { id: 'rack-f6', name: '货F6', version: 8 };
  const calls = [], messages = [], refreshes = [];
  let key = 0, confirms = 0;
  const ctx = {
    layout: { source_sha256: 'draft-before', racks: [{ ...rack, version: 4 }, other] },
    planningPublishedLayout: { source_sha256: 'published-before', racks: [rack, other] },
    selectedRack: rack, selectedAreaFeature: { id: 'area-f' }, floorCode: '3F',
    activeFloorCodeRef: { current: '3F' },
    canEditLocations: true, spatialEditBusy: false, publishedFloorRevision: 'published-before',
    rackDeleteRequestsRef: { current: {} }, rackDrafts: { 'rack-f7': { x_mm: 100 }, 'rack-f6': { x_mm: 200 } },
    window: { confirm: () => { confirms++; return true; } }, URLSearchParams,
    operationKey: () => `delete-attempt-${++key}`,
    mutateJson: async (url, method) => {
      calls.push({ url, method });
      return { item: { id: rack.id, deleted: true, inactive_location_count: 6, published_map_changed: true }, revision: 'draft-after', published_revision: 'published-after', applied: true };
    },
    setLayout: update => { ctx.layout = update(ctx.layout); },
    setPlanningPublishedLayout: update => { ctx.planningPublishedLayout = update(ctx.planningPublishedLayout); },
    setRackDrafts: update => { ctx.rackDrafts = update(ctx.rackDrafts); },
    setSpatialEditBusy: value => { ctx.spatialEditBusy = value; },
    setSelected: value => { ctx.selection = value; }, setPublishedFloorRevision: () => {}, setPlanningPublishedRevision: () => {},
    rememberServerDraft: () => {}, setLocationEditMessage: message => messages.push(message),
    refreshPlanningTwinFloor: async () => { refreshes.push('layout'); },
    refreshDashboard: async () => { refreshes.push('dashboard'); },
    ...overrides,
  };
  vm.createContext(ctx);
  vm.runInContext(code + '\nglobalThis.remove = deleteSelectedRack;', ctx);
  return { ctx, calls, messages, refreshes, confirms: () => confirms };
}

test('delete removes published and draft object, refreshes both views and preserves another local draft', async () => {
  const f = fixture(); await f.ctx.remove();
  assert.deepEqual(Array.from(f.ctx.planningPublishedLayout.racks, r => r.id), ['rack-f6']);
  assert.deepEqual(Array.from(f.ctx.layout.racks, r => r.id), ['rack-f6']);
  const url = new URL(f.calls[0].url, 'http://synthetic.invalid');
  assert.equal(url.searchParams.get('expected_published_revision'), 'published-before');
  assert.equal(url.searchParams.get('expected_version'), '3');
  assert.deepEqual(f.refreshes.sort(), ['dashboard', 'layout']);
  assert.equal(f.ctx.rackDrafts['rack-f6'].x_mm, 200);
  assert.equal(f.ctx.rackDrafts['rack-f7'], undefined);
  assert.match(f.messages.at(-1), /已删除并生效/);
});

test('a previously draft-deleted published rack can still complete deletion', async () => {
  const f = fixture(); f.ctx.layout.racks = f.ctx.layout.racks.filter(r => r.id !== 'rack-f7');
  await f.ctx.remove(); assert.equal(f.calls.length, 1);
  assert.equal(f.ctx.planningPublishedLayout.racks.some(r => r.id === 'rack-f7'), false);
});

test('an unpublished draft rack deletes using its draft version and describes draft-only result', async () => {
  const f = fixture(); f.ctx.planningPublishedLayout.racks = f.ctx.planningPublishedLayout.racks.filter(r => r.id !== 'rack-f7');
  const good = f.ctx.mutateJson;
  f.ctx.mutateJson = async (...args) => {
    const result = await good(...args);
    return { ...result, published_revision: 'published-before', item: { ...result.item, inactive_location_count: 0, published_map_changed: false } };
  };
  await f.ctx.remove();
  assert.equal(new URL(f.calls[0].url, 'http://synthetic.invalid').searchParams.get('expected_version'), '4');
  assert.equal(f.ctx.layout.racks.length, 1);
  assert.equal(f.ctx.planningPublishedLayout.source_sha256, 'published-before');
  assert.match(f.messages.at(-1), /从布局草稿删除.*正式地图和库位未改变/);
});

test('cancel, busy and no administrator capability do not send delete', async () => {
  for (const overrides of [{ window: { confirm: () => false } }, { spatialEditBusy: true }, { canEditLocations: false }]) {
    const f = fixture(overrides); await f.ctx.remove(); assert.equal(f.calls.length, 0);
  }
});

test('unknown response reuses frozen request key and versions on retry', async () => {
  const f = fixture(); const good = f.ctx.mutateJson;
  f.ctx.mutateJson = async (url, method) => { f.calls.push({ url, method }); throw new Error('网络中断'); };
  await f.ctx.remove(); assert.equal(f.ctx.layout.racks.length, 2); assert.equal(f.ctx.planningPublishedLayout.racks.length, 2);
  f.ctx.layout.source_sha256 = 'new-readback'; f.ctx.planningPublishedLayout.source_sha256 = 'new-published';
  f.ctx.mutateJson = good; await f.ctx.remove();
  assert.equal(f.calls[0].url, f.calls[1].url); assert.equal(f.confirms(), 1);
});

test('known conflict preserves maps and allows a newly confirmed attempt', async () => {
  const f = fixture(); const good = f.ctx.mutateJson;
  f.ctx.mutateJson = async (url, method) => { f.calls.push({ url, method }); throw Object.assign(new Error('仍有库存'), { status: 409 }); };
  await f.ctx.remove(); assert.equal(f.ctx.layout.racks.length, 2); assert.equal(f.ctx.planningPublishedLayout.racks.length, 2);
  assert.match(f.messages.at(-1), /未删除/);
  f.ctx.mutateJson = good; await f.ctx.remove(); assert.notEqual(f.calls[0].url, f.calls[1].url);
});

test('missing published receipt cannot be treated as successful deletion', async () => {
  const f = fixture({ mutateJson: async () => ({ item: { id: 'rack-f7', deleted: true }, revision: 'draft-after' }) });
  await f.ctx.remove(); assert.equal(f.ctx.planningPublishedLayout.racks.length, 2); assert.equal(f.ctx.layout.racks.length, 2);
  assert.match(f.messages.at(-1), /未确认/);
});

test('server deletion stays removed if readback fails and message says already effective', async () => {
  const f = fixture({ refreshPlanningTwinFloor: async () => { throw new Error('回读超时'); } });
  await f.ctx.remove(); assert.equal(f.ctx.planningPublishedLayout.racks.length, 1); assert.equal(f.ctx.layout.racks.length, 1);
  assert.match(f.messages.at(-1), /删除已生效.*刷新/);
});

test('a late delete response cannot overwrite a newly selected floor', async () => {
  const f = fixture(); const good = f.ctx.mutateJson;
  f.ctx.mutateJson = async (...args) => { f.ctx.activeFloorCodeRef.current = '2F'; return good(...args); };
  await f.ctx.remove();
  assert.equal(f.ctx.layout.source_sha256, 'draft-before');
  assert.equal(f.ctx.planningPublishedLayout.source_sha256, 'published-before');
  assert.equal(f.refreshes.length, 0);
  assert.equal(Object.keys(f.ctx.rackDeleteRequestsRef.current).length, 0);
  assert.equal(f.ctx.spatialEditBusy, false);
});
