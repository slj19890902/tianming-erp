import assert from 'node:assert/strict';
import test from 'node:test';
import { groupShelfProducts, filterShelfMolds, shelfStockDates } from '../src/shelfDisplay.mjs';
const item = {lot_id: 1, customer_id: 1, product_id: 2, inventory_type: 'finished', inventory_code: 'A01', product_name: '纸箱', box_style: '普通箱', is_bom_component: false, specification: '400x300x200', unit: 'pcs',
  available_quantity: 30, reserved_quantity: 10, damaged_quantity: 2, location_id: 10};
test('stock dates distinguish unknown history and non-exact dates instead of fabricating freshness', () => {
  assert.deepEqual(shelfStockDates([
    {stock_date: '2026-09-02', stock_date_accuracy: 'exact'},
    {stock_date: '2020-01-01', stock_date_accuracy: 'unknown'},
    {stock_date: '2026-09-08', stock_date_accuracy: 'estimated'},
  ]), {first: '2026-09-02', latest: '2026-09-08', incomplete: true, approximate: true});
  assert.equal(shelfStockDates([]).first, null);
});
test('same product batches aggregate physical quantity without losing individual lots or double counting', () => {
  const input = [item, {...item, lot_id: 2}, item];
  const before = JSON.stringify(input);
  const [group] = groupShelfProducts(input);
  assert.equal(group.physical, 84);
  assert.equal(group.available, 60);
  assert.equal(group.reserved, 20);
  assert.equal(group.damaged, 4);
  assert.equal(group.items.length, 2);
  assert.equal(JSON.stringify(input), before);
});
test('customer, product, code, name, box, unit, location and BOM identities stay distinct', () => {
  for (const change of [{customer_id: 3}, {product_id: 4}, {unit: 'sets'}, {inventory_code: 'B'},
    {product_name: '子件'}, {box_style: '天地盖'}, {is_bom_component: true}, {is_bom_component: undefined}, {box_style: null}, {location_id: 11}, {composite_parent_group_key: 'assembly-1'}]) {
    assert.equal(groupShelfProducts([item, {...item, lot_id: 2, ...change}]).length, 2);
  }
  assert.equal(groupShelfProducts([{...item, product_id: null}, {...item, product_id: null, lot_id: 2}]).length, 2);
});
test('one physical mold stays one row despite multiple matching product links; search preserves input order', () => {
  const mold = {id: 1, mold_name: '中性内盒', products: [
    {customer_name: '测试甲', product_code: 'A01'}, {customer_name: '测试乙', product_code: 'A02'}]};
  assert.equal(filterShelfMolds([mold, mold], '测试').length, 1);
  assert.equal(filterShelfMolds([mold], 'a02')[0], mold);
  assert.equal(filterShelfMolds([mold], '内盒')[0], mold);
  assert.equal(filterShelfMolds([mold], '不匹配').length, 0);
});

test('relocated finished batches with differing snapshots show one total while retaining all sources', () => {
  const first = {...item, inventory_type: 'finished'};
  const second = {...first, lot_id: 2, material: 'B', specification: 'snapshot'};
  const rows = [first, second, first];
  const before = JSON.stringify(rows);
  const groups = groupShelfProducts(rows);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].physical, 84);
  assert.deepEqual(groups[0].items, [first, second]);
  assert.equal(JSON.stringify(rows), before);
  for (const change of [{customer_id: 9}, {product_id: 9}, {location_id: 11}, {unit: 'sets'}, {inventory_type: 'semi_finished'}]) {
    assert.equal(groupShelfProducts([first, {...second, ...change}]).length, 2);
  }
});

test('even identical BOM children stay separate; missing identities do not permit merging', () => {
  for (const change of [{is_bom_component: true}, {is_bom_component: undefined}, {box_style: ''}, {inventory_code: ''}, {product_name: ''}]) {
    assert.equal(groupShelfProducts([{...item, ...change}, {...item, ...change, lot_id: 2}]).length, 2);
  }
});
