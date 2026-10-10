import test from 'node:test';
import assert from 'node:assert/strict';
import '../static/ui/warehouse-movement-view.js';

const base = {
  created_at: '2026-10-10', operator_name: '仓库员', customer_name: '光洋',
  inventory_type: 'semi_finished', inventory_code: null,
  source_product_code: '80012273', source_product_name: '来源常用箱',
  product_name: '片料', operation_label: '入库',
  before_physical: 0, after_physical: 500, physical_delta: 500,
  unit: 'sheets', quantity_scope: '本次涉及库存，非全仓合计',
  lot_number: 'SEMI-1', movement_number: 'IM-1',
  before_available: 0, after_available: 500,
  before_reserved: 0, after_reserved: 0,
  before_damaged: 0, after_damaged: 0,
};

test('source code is labelled as provenance, separately from confirmed usage', () => {
  const html = globalThis.WarehouseMovementView.row(base, value => value);
  assert.match(html, /80012273/);
  assert.match(html, /报料来源：/);
  assert.doesNotMatch(html, /适用款号/);
  assert.match(html, /0 → 500 张/);
  assert.doesNotMatch(html, /适用款号：80012273/);
});

test('confirmed binding remains separate and source text is escaped', () => {
  const html = globalThis.WarehouseMovementView.row({
    ...base, source_product_name: '<来源>', inventory_code: 'OTHER-CODE',
  }, value => value);
  assert.match(html, /&lt;来源&gt;/);
  assert.match(html, /适用款号：OTHER-CODE/);
});

test('multiple source facts stay visible and product names do not repeat', () => {
  const html = globalThis.WarehouseMovementView.row({
    ...base, source_product_code: null, source_product_name: null,
    source_products: [
      {code: 'A', name: '同名产品', kind: 'order_purchase_reserve'},
      {code: 'B', name: '<另一产品>', kind: 'order_purchase_reserve'},
    ], product_name: '同名产品',
  }, value => value);
  assert.match(html, /报料来源：/);
  assert.match(html, /A \/ B/);
  assert.match(html, /&lt;另一产品&gt;/);
  assert.equal((html.match(/同名产品/g)||[]).length, 1);
  assert.doesNotMatch(html, /适用款号/);
});

test('a sheet without source or binding says the product is unspecified', () => {
  const html = globalThis.WarehouseMovementView.row({
    ...base, source_product_code: null, source_product_name: null,
    source_products: [],
  }, value => value);
  assert.match(html, /未指定产品/);
  assert.doesNotMatch(html, /未绑定编码|适用款号/);
});

test('a confirmed binding without purchase source is labelled only as usage', () => {
  const html = globalThis.WarehouseMovementView.row({
    ...base, source_product_code: null, source_product_name: null,
    source_products: [], inventory_code: 'BOUND-1',
  }, value => value);
  assert.match(html, /适用款号：BOUND-1/);
  assert.doesNotMatch(html, /报料来源/);
});
