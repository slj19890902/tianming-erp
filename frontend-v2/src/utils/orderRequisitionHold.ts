export interface PreviousBatchCandidate {
  order_item_id: number
  order_number: string
  item_order_number?: string
  remaining_quantity?: number
  specification?: string | null
  material?: string | null
  flute_type?: string | null
}
export interface OrderHoldPreview {
  client_line_id: string
  status: 'normal' | 'covered' | 'hold' | 'select_required' | 'blocked'
  candidates: PreviousBatchCandidate[]
  warnings: string[]
  selected_previous_order_item_id: number | null
}
export interface HoldDecision {
  preview: OrderHoldPreview
  selected: number | null
  requiresSelection: boolean
}
// Match identities, never array positions. A partial response cannot authorize
// waiting on a different order line.
export function resolveHoldPreview(expectedIds: string[], rows: OrderHoldPreview[], manual: Record<string, number | null>): Record<string, HoldDecision> {
  if (!Array.isArray(rows) || rows.length !== expectedIds.length || new Set(expectedIds).size !== expectedIds.length) throw new Error('报料策略预检明细不完整，请重新核对')
  const expected = new Set(expectedIds)
  const decisions: Record<string, HoldDecision> = {}
  for (const row of rows) {
    if (!row || !expected.has(row.client_line_id) || decisions[row.client_line_id] ||
      !['normal', 'covered', 'hold', 'select_required', 'blocked'].includes(row.status) ||
      !Array.isArray(row.candidates) || !Array.isArray(row.warnings) || row.warnings.some(warning => typeof warning !== 'string')) throw new Error('报料策略预检身份或状态无效')
    const ids = row.candidates.map(candidate => candidate.order_item_id)
    if (ids.some(id => !Number.isSafeInteger(id) || id < 1) || new Set(ids).size !== ids.length) throw new Error('上一批候选身份无效')
    const recommended = row.selected_previous_order_item_id
    if (recommended !== null && (!Number.isSafeInteger(recommended) || !ids.includes(recommended))) throw new Error('上一批推荐不属于当前候选')
    if ((row.status === 'hold' && recommended === null) || (['normal', 'covered'].includes(row.status) && ids.length)) throw new Error('报料策略预检状态与候选不一致')
    const requiresSelection = row.status === 'select_required' || row.status === 'blocked' || ids.length > 1
    const choice = manual[row.client_line_id]
    const validChoice = choice === 0 || (typeof choice === 'number' && ids.includes(choice))
    decisions[row.client_line_id] = { preview: row, requiresSelection,
      selected: validChoice ? choice! : requiresSelection ? null : recommended }
  }
  return decisions
}
export function previousBatchSelections(decisions: Record<string, HoldDecision>, lineIds: string[]) {
  return lineIds.flatMap((id, index) => {
    const decision = decisions[id]
    if (!decision || (decision.requiresSelection && decision.selected === null)) throw new Error(`第${index + 1}条请选择上一批或正常待报料`)
    if (decision.selected === null) return []
    if (decision.selected !== 0 && !decision.preview.candidates.some(candidate => candidate.order_item_id === decision.selected)) throw new Error(`第${index + 1}条上一批候选已变化，请重新核对`)
    return [{ client_line_id: id, previous_order_item_id: decision.selected }]
  })
}
export function holdPreviewText(row: OrderHoldPreview) {
  return ({ normal: '这是首批，正常进入待报料。', covered: '成品库存已全额覆盖，无需报料或等候。',
    hold: '已找到上一批，保存时建立等候报料。', select_required: '请明确选择上一批或正常待报料。',
    blocked: '不能自动关联上一批，请明确处理。' })[row.status]
}
