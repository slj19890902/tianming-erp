type Row = Record<string, unknown>
export type ReadbackExpectations = { customerId: number; lines: Array<{
  clientLineId: string; productId: number; version: number; sequence: number;
  supplyMode: string; defaultNotes: string | null; notes: string | null; quantity: string;
}> }
function record(value: unknown): Row {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('保存回读数据结构不完整，请保留草稿核对')
  return value as Row
}
function integer(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) throw new Error('保存回读身份或版本无效，请保留草稿核对')
  return value
}
function note(value: unknown): string | null {
  if (value !== null && typeof value !== 'string') throw new Error('产品说明字段缺失或类型不正确，请重新选择产品')
  return value as string | null
}
function quantity(value: unknown): string {
  if (typeof value !== 'number' && typeof value !== 'string') throw new Error('数量字段不完整')
  const text = String(value)
  if (!/^\d+(?:\.\d+)?$/.test(text)) throw new Error('数量无法精确核对，请保留草稿')
  const [whole = '', fraction = ''] = text.split('.')
  const left = whole.replace(/^0+(?=\d)/, '')
  const right = fraction.replace(/0+$/, '')
  const result = right ? `${left}.${right}` : left
  if (result === '0') throw new Error('数量必须大于零')
  return result
}
function same(actual: unknown, expected: unknown) {
  if (actual !== expected) throw new Error('保存快照与请求行或产品说明来源不一致，请保留草稿核对')
}
export function buildReadbackExpectations(payloadValue: unknown, products: Record<number, unknown>): ReadbackExpectations {
  const payload = record(payloadValue)
  same(payload.readback_contract, 'a01-v1')
  const customerId = integer(payload.customer_id)
  if (!Array.isArray(payload.items) || !payload.items.length) throw new Error('请求明细不完整')
  const used = new Set<string>()
  const lines = payload.items.map((value, index) => {
    const line = record(value)
    const productId = integer(line.product_id)
    const product = record(products[productId])
    same(integer(product.id), productId)
    same(integer(product.customer_id), customerId)
    const version = integer(product.version)
    same(integer(line.product_expected_version), version)
    if (typeof line.client_line_id !== 'string' || !line.client_line_id || used.has(line.client_line_id)) throw new Error('请求行标识缺失或重复')
    used.add(line.client_line_id)
    if (typeof product.supply_mode !== 'string' || !product.supply_mode) throw new Error('产品供应方式不完整')
    const rawDefault = note(product.production_notes)
    const defaultNotes = rawDefault?.trim() || null
    if (line.production_notes !== undefined && typeof line.production_notes !== 'string') throw new Error('请求说明类型不正确')
    const explicit = typeof line.production_notes === 'string' ? line.production_notes.trim() || null : null
    if (product.supply_mode === 'external_purchase' && explicit) throw new Error('外购包材不能提交生产说明')
    return { clientLineId: line.client_line_id, productId, version, sequence: index + 1,
      supplyMode: product.supply_mode, defaultNotes,
      notes: product.supply_mode === 'external_purchase' ? null : explicit || defaultNotes,
      quantity: quantity(line.quantity) }
  })
  return { customerId, lines }
}
export function verifyOrderCreateReadback(expected: ReadbackExpectations, createdValue: unknown, savedValue: unknown): void {
  const created = record(createdValue)
  const saved = record(savedValue)
  const orderId = integer(created.id)
  same(integer(saved.id), orderId)
  same(integer(created.customer_id), expected.customerId)
  same(integer(saved.customer_id), expected.customerId)
  const proof = record(created.create_readback)
  const savedProof = record(saved.create_readback)
  for (const value of [proof, savedProof]) {
    same(value.schema, 'a01-v1')
    same(integer(value.order_id), orderId)
    same(integer(value.customer_id), expected.customerId)
    if (!Array.isArray(value.lines) || value.lines.length !== expected.lines.length) throw new Error('保存来源行数不一致')
  }
  const expectedByLine = new Map(expected.lines.map(line => [line.clientLineId, line]))
  const identityByLine = new Map<string, { id: number; sequence: number }>()
  for (const value of [proof, savedProof]) {
    const seen = new Set<string>()
    const ids = new Set<number>()
    for (const raw of value.lines as unknown[]) {
      const row = record(raw)
      if (typeof row.client_line_id !== 'string' || seen.has(row.client_line_id)) throw new Error('保存来源行标识重复或缺失')
      const line = expectedByLine.get(row.client_line_id)
      if (!line) throw new Error('保存来源含未知请求行')
      seen.add(row.client_line_id)
      const id = integer(row.order_item_id)
      if (ids.has(id)) throw new Error('保存来源明细身份重复')
      ids.add(id)
      same(integer(row.product_id), line.productId)
      same(integer(row.customer_id), expected.customerId)
      same(integer(row.product_version), line.version)
      same(row.supply_mode, line.supplyMode)
      same(note(row.default_production_notes), line.defaultNotes)
      same(note(row.expected_production_notes), line.notes)
      same(quantity(row.quantity), line.quantity)
      same(integer(row.item_sequence), line.sequence)
      const previous = identityByLine.get(line.clientLineId)
      if (previous) same(previous.id, id)
      else identityByLine.set(line.clientLineId, { id, sequence: line.sequence })
    }
  }
  for (const response of [created, saved]) {
    if (!Array.isArray(response.items) || response.items.length !== expected.lines.length) throw new Error('保存回读行数不一致')
    const seen = new Set<string>()
    for (const raw of response.items) {
      const row = record(raw)
      if (typeof row.client_line_id !== 'string' || seen.has(row.client_line_id)) throw new Error('保存回读行标识缺失或重复')
      const line = expectedByLine.get(row.client_line_id)
      const identity = identityByLine.get(row.client_line_id)
      if (!line || !identity) throw new Error('保存回读含未知行')
      seen.add(row.client_line_id)
      same(integer(row.id), identity.id)
      same(integer(row.item_sequence), identity.sequence)
      same(integer(row.product_id), line.productId)
      same(quantity(row.quantity), line.quantity)
      same(row.supply_mode_snapshot, line.supplyMode)
      same(note(row.snapshot_production_notes), line.notes)
    }
  }
}
