// Merge only pages from the same request generation (checked by the caller).
export function mergeWarehouseSearchPage(previous, page) {
  const unique = (rows, key) => [...new Map(rows.map(row => [row[key], row])).values()];
  const items = unique([...(previous?.items || []), ...(page.items || [])], 'lot_id');
  const resources = unique([...(previous?.resources || []), ...(page.resources || [])], 'resource_id');
  const pending_receipts = unique([...(previous?.pending_receipts || []), ...(page.pending_receipts || [])], 'delivery_item_id');
  return {...page, items, resources, pending_receipts, inventory_result_count: items.length,
    resource_result_count: resources.length, result_count: items.length + resources.length};
}

export function searchPageRequestIsCurrent(requestId, currentRequestId, aborted = false) {
  return !aborted && requestId === currentRequestId;
}
