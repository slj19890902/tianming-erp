import assert from 'node:assert/strict';
import test from 'node:test';
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
