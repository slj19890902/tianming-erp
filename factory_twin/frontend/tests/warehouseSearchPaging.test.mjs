import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import {mergeWarehouseSearchPage, searchPageRequestIsCurrent} from '../src/warehouseSearchPaging.mjs';

test('continued pages preserve lot identity, distinct shelf cells and latest cursor', () => {
  const first = {items: [{lot_id: 1, location_id: 10}, {lot_id: 2, location_id: 11}], resources: [], pagination: {has_more: true, next_after_lot_id: 2}};
  const next = {items: [{lot_id: 2, location_id: 12}, {lot_id: 3, location_id: 13}], resources: [], pagination: {has_more: false, next_after_lot_id: null}};
  const before = JSON.stringify(first);
  const result = mergeWarehouseSearchPage(first, next);
  assert.deepEqual(result.items.map(row => row.lot_id), [1, 2, 3]);
  assert.equal(result.items[1].location_id, 12);
  assert.equal(result.inventory_result_count, 3);
  assert.equal(result.pagination.has_more, false);
  assert.equal(JSON.stringify(first), before);
});
test('starting another search replaces old customers and resources instead of appending', () => {
  const result = mergeWarehouseSearchPage(null, {items: [{lot_id: 9}], resources: [{resource_id: 'r9'}]});
  assert.equal(result.result_count, 2);
  assert.deepEqual(result.items, [{lot_id: 9}]);
});
test('retrying the same page does not double count inventory or resources', () => {
  const page = {items: [{lot_id: 1}], resources: [{resource_id: 'rack-1'}]};
  const result = mergeWarehouseSearchPage(page, page);
  assert.equal(result.inventory_result_count, 1);
  assert.equal(result.resource_result_count, 1);
});
test('query switches and closing invalidate pending pages', () => {
  assert.equal(searchPageRequestIsCurrent(1, 2), false);
  assert.equal(searchPageRequestIsCurrent(2, 2, true), false);
  assert.equal(searchPageRequestIsCurrent(2, 2), true);
});

const source = fs.readFileSync(new URL('../src/WarehouseTwinApp.tsx', import.meta.url), 'utf8').replace(/\r/g, '');
const compile = code => ts.transpileModule(code, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
function pageHarness() {
  const calls = [];
  const context = {URLSearchParams, mergeWarehouseSearchPage, searchPageRequestIsCurrent,
    searchResponse: {items: [{lot_id: 1}], resources: [], pagination: {has_more: true, next_after_lot_id: 1}},
    search: '纸箱', searchType: 'finished', searchFloor: '3F', searchLoading: false,
    searchRequestRef: {current: 1}, searchMoreBusyRef: {current: false}, searchAbortRef: {current: null},
    setSearchError: value => {context.error = value;}, setSearchMoreLoading: value => {context.loading = value;},
    setSearchResponse: update => {context.searchResponse = update(context.searchResponse);},
    requestJson: url => new Promise((resolve, reject) => calls.push({url, resolve, reject}))};
  vm.createContext(context);
  const start = source.indexOf('  async function loadMoreSearchResults()');
  vm.runInContext(compile(source.slice(start, source.indexOf('  useEffect(', start))), context);
  return {context, calls};
}
test('real load-more handler rejects double clicks and late pages after a query switch', async () => {
  const {context, calls} = pageHarness();
  const pending = context.loadMoreSearchResults();
  await context.loadMoreSearchResults();
  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.includes('after_lot_id=1'));
  context.searchRequestRef.current++;
  context.searchResponse = {items: [{lot_id: 99}], resources: []};
  calls[0].resolve({items: [{lot_id: 2}], resources: []});
  await pending;
  assert.equal(context.searchResponse.items[0].lot_id, 99);
});
test('real load-more handler preserves the loaded page on failure and can retry once', async () => {
  const {context, calls} = pageHarness();
  let pending = context.loadMoreSearchResults();
  calls[0].reject(new Error('网络暂不可用'));
  await pending;
  assert.equal(context.searchResponse.items.length, 1);
  assert.equal(context.searchMoreBusyRef.current, false);
  assert.equal(context.error, '网络暂不可用');
  pending = context.loadMoreSearchResults();
  calls[1].resolve({items: [{lot_id: 2}], resources: [], pagination: {has_more: false, next_after_lot_id: null}});
  await pending;
  assert.equal(context.searchResponse.items.length, 2);
  assert.equal(context.loading, false);
});
test('closing the actual search effect cancels loading but keeps selected map results', () => {
  let effect;
  const previousController = new AbortController();
  const response = {items: [{lot_id: 1, location_id: 8}]};
  const context = {AbortController, search: '纸箱', searchPanelOpen: false, searchType: 'finished', searchFloor: '3F', searchRetryToken: 0,
    searchRequestRef: {current: 1}, searchMoreBusyRef: {current: true}, searchAbortRef: {current: previousController},
    searchResponse: response, useEffect: callback => {effect = callback;}, setSearchMoreLoading: () => {},
    setSearchResponse: value => {context.searchResponse = value;}};
  vm.createContext(context);
  const start = source.indexOf('  useEffect(() => {\n    const keyword = search.trim();');
  const end = source.indexOf('  async function loadMoreSearchResults()', start);
  vm.runInContext(compile(source.slice(start, end)), context);
  effect();
  assert.equal(previousController.signal.aborted, true);
  assert.equal(context.searchResponse, response);
  assert.ok(!source.includes('searchProductGroups.slice(0, 80)'));
  assert.ok(source.includes('数量与位置仅汇总已加载结果'));
});
