import { inventoryPhysicalQuantity } from './warehouseInventory.mjs';

// Reading groups only: each source batch and its identity remain intact.
export function groupShelfProducts(items) {
  const groups = new Map();
  const seen = new Set();
  for (const item of items) {
    const batch = `${item.inventory_type || ''}/${item.lot_id}/${item.location_id || item.location_code || ''}`;
    if (seen.has(batch)) continue;
    seen.add(batch);
    const key = JSON.stringify([item.customer_id, item.product_id || `lot:${item.lot_id}`,
      item.inventory_type, item.unit, item.specification || '', item.material || '',
      item.composite_parent_group_key || null, item.location_id || item.location_code || null]);
    let group = groups.get(key);
    if (!group) {
      group = { key, item, items: [], physical: 0, available: 0, reserved: 0, damaged: 0 };
      groups.set(key, group);
    }
    group.items.push(item);
    group.physical += inventoryPhysicalQuantity(item);
    group.available += Number(item.available_quantity || 0);
    group.reserved += Number(item.reserved_quantity || 0);
    group.damaged += Number(item.damaged_quantity || 0);
  }
  return [...groups.values()];
}

export function filterShelfMolds(items, query = '') {
  const needle = query.trim().toLocaleLowerCase('zh-CN');
  return [...new Map(items.map(item => [item.id, item])).values()].filter(item => !needle ||
    [item.mold_name, item.chinese_abbreviation, ...(item.products || []).flatMap(product =>
      [product.customer_name, product.product_code, product.product_name])]
      .some(value => String(value || '').toLocaleLowerCase('zh-CN').includes(needle)));
}

export function shelfStockDates(items) {
  const dates = items.filter(item => item.stock_date_accuracy !== 'unknown' && item.stock_date)
    .map(item => item.stock_date).sort();
  return { first: dates[0] || null, latest: dates.at(-1) || null,
    incomplete: dates.length !== items.length,
    approximate: items.some(item => item.stock_date && item.stock_date_accuracy !== 'exact') };
}
