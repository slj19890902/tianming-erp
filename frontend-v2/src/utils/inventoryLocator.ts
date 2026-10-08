/** Read-only lookup by lot identity; the destination resolves its current formal location. */
export function buildReadonlyInventoryLocatorHref(lotId: unknown, inventoryType: unknown): string | undefined {
  if (typeof lotId !== 'number' || !Number.isSafeInteger(lotId) || lotId <= 0) return undefined
  if (inventoryType !== 'finished' && inventoryType !== 'semi_finished') return undefined

  const query = new URLSearchParams({
    readonly: '1',
    mode: 'lookup',
    tab: inventoryType,
    inventory_type: inventoryType,
    lot_id: String(lotId),
  })
  const target = `/warehouse-ledger.html?${query.toString()}`
  const workspace = new URLSearchParams({ page: 'warehouse', warehouse_target: target })
  return `/static/index.html?${workspace.toString()}`
}
