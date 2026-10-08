import type { Product } from '../api/client'
export type InventoryComponent = 'whole' | 'cover' | 'base'
export type InventoryPart = 'finished' | InventoryComponent
export interface InventoryCandidate {
  lot_id: number; version: number; lot_number?: string
  quantity_available?: number; available_stock_quantity?: number
  quantity_contract?: { customer_id: number; product_id: number; customer_basis: number; physical_basis: number; physical_unit: string }
  warehouse_location?: { employee_location_name?: string; current_address_name?: string; location_name?: string; location_id?: number; id?: number }
  customer_id?: number | null; customer_bound?: boolean; applicability_scope?: string
  source?: string; recommendation_source?: string; recommendation_tier?: string
  automatic_recommendation?: boolean; direct_deduction_eligible?: boolean; selectable?: boolean
  requires_override?: boolean; override_required?: boolean; requires_confirmation?: boolean
  is_general?: boolean; warning_codes?: string[]; warning_messages?: string[]; signature_differences?: string[]
  internal_name?: string; board_length_mm?: number; board_width_mm?: number; material_code?: string; flute_type?: string
  processing?: string; stock_yield_per_sheet?: number; component_type?: InventoryComponent; match_rule_id?: number | null
  manual_page?: number
}
export interface ReservationEntry {
  lot_id: number; expected_version: number; requested_qty: number; recommendation_source: string; confirmed: boolean
  component_type?: InventoryComponent; match_rule_id?: number | null; override?: boolean; direct_deduction?: boolean; warning_acknowledged_codes?: string[]
}
export interface ReservationPlan { finished: ReservationEntry[]; semi: ReservationEntry[] }
export interface InventoryAuthority {
  client_line_id: string; product_id: number; order_quantity: number; finished_planned_quantity?: number; production_required_quantity?: number
  coverage_state: string; interaction_state: string; message?: string; finished_stock_unit?: string
  finished_stock_on_hand_quantity?: number; finished_stock_reserved_quantity?: number; finished_stock_remaining_quantity?: number
  requisition_sheet_quantity?: number; requisition_components: Array<{ component_type: InventoryComponent; required_piece_quantity: number; remaining_required_piece_quantity: number; requisition_sheet_quantity: number }>
}
export interface InventoryDraftLine { client_line_id: string; product_id: number; quantity: number; material?: string; flute_type?: string; reservation_plan: ReservationPlan; finished_skipped: boolean }
export interface InventoryPartState { candidates: InventoryCandidate[]; manual: InventoryCandidate[]; selected: number[]; mode: 'automatic' | 'exact' | 'skipped'; page: number; total: number; error: string }
export interface InventoryLineState { product_id: number; quantity: number; parts: Record<InventoryPart, InventoryPartState>; plan: ReservationPlan; authority: InventoryAuthority | null; allocations: Array<{ part: InventoryPart; lot_id: number; stock_quantity: number; credited_quantity: number }> }
export const emptyPlan = (): ReservationPlan => ({ finished: [], semi: [] })
export function newInventoryLine(product_id: number, quantity: number): InventoryLineState {
  const part = (): InventoryPartState => ({ candidates: [], manual: [], selected: [], mode: 'automatic', page: 1, total: 0, error: '' })
  return { product_id, quantity, parts: { finished: part(), whole: part(), cover: part(), base: part() }, plan: emptyPlan(), authority: null, allocations: [] }
}
function integer(value: unknown, name: string, zero = false): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < (zero ? 0 : 1)) throw new Error(`${name}无效，请刷新库存`)
  return value
}
export function inventoryComponents(product: Product): InventoryComponent[] {
  if (product.is_composite) return []
  return /A3|天地盖/i.test(product.box_style || '') || (product.base_report_length_mm && product.base_report_width_mm) ? ['cover', 'base'] : ['whole']
}
export function semiCandidateRequest(product: Product, customer: number, component: InventoryComponent) {
  const base = component === 'base', length = Number(base ? product.base_report_length_mm : product.report_length_mm), width = Number(base ? product.base_report_width_mm : product.report_width_mm)
  const material = String(product.material_code || product.default_material_text || '').split('-')[0], flute = product.flute_type || product.flute_type_code || ''
  if (!Number.isSafeInteger(length) || length <= 0 || !Number.isSafeInteger(width) || width <= 0 || !material || !flute) return null
  const cutting = product.default_cutting_mode || '一开一'
  const legacy: Record<string, number> = { 一开一: 1, 一开二: 2, 一开三: 3, 一开四: 4, 一开五: 5, 一开六: 6 }
  const digits = cutting.match(/^一开([1-9]\d*)$/)?.[1] || (/^[1-9]\d*$/.test(cutting) ? cutting : '')
  const factor = legacy[cutting] || Number(digits)
  if (!Number.isSafeInteger(factor) || factor < 1) throw new Error('当前开料方式需在原报料流程核对，不能猜测出数')
  return { customer_id: customer, board_length_mm: length, board_width_mm: width, material_code: material, flute_type: flute.toUpperCase(), component_type: component,
    pieces_per_box: component === 'whole' ? Number(product.pieces_per_box || 1) : 1, stock_yield_per_sheet: factor, layer_count: product.layer_count || null,
    crease_type: base ? product.base_crease_type || null : product.crease_type || null,
    crease_left_mm: base ? product.base_crease_left_mm ?? null : product.crease_left_mm ?? null,
    crease_middle_mm: base ? product.base_crease_middle_mm ?? null : product.crease_middle_mm ?? null,
    crease_right_mm: base ? product.base_crease_right_mm ?? null : product.crease_right_mm ?? null, stage: 'order' as const }
}
export function generalSheet(c: InventoryCandidate) { return (c.recommendation_source || c.source) === 'general_signature' || (c.warning_codes || []).includes('GENERAL_SEMI_FINISHED_STOCK') }
export function directSheet(c: InventoryCandidate) { return c.direct_deduction_eligible === true && (c.automatic_recommendation === true || (!generalSheet(c) && !(c.signature_differences || []).length)) }
export function safeCandidate(part: InventoryPart, c: InventoryCandidate): boolean {
  if (c.source === 'manual') return false
  if (part !== 'finished') {
    if (c.selectable === false || c.customer_bound !== true || c.recommendation_tier === 'more') return false
    if (c.automatic_recommendation === true && directSheet(c)) return true
    if (directSheet(c) && (c.recommendation_source || c.source) === 'customer_generic' && c.source !== 'manual' && !c.requires_override && !c.override_required && !(c.warning_codes || []).some(x => x !== 'CUSTOMER_GENERIC_SEMI_FINISHED_STOCK')) return true
  }
  if (c.source === 'manual' || c.requires_confirmation || c.requires_override || c.override_required || generalSheet(c) || (c.warning_codes || []).length || (c.warning_messages || []).length) return false
  if (part === 'finished') return c.is_general !== true
  return !(c.signature_differences || []).length && (c.recommendation_source || c.source) !== 'customer_generic'
}
export function selectedCandidates(part: InventoryPart, state: InventoryPartState): InventoryCandidate[] {
  if (state.mode === 'skipped') return []
  const unique = [...new Map([...state.candidates, ...state.manual].map(c => [c.lot_id, c])).values()]
  if (state.mode === 'automatic') return unique.filter(c => safeCandidate(part, c))
  if (new Set(state.selected).size !== state.selected.length) throw new Error('已采用批次重复，请重新选择')
  return state.selected.map(id => {
    const c = unique.find(row => row.lot_id === id)
    if (!c || c.selectable === false) throw new Error('已采用批次已变化，请重新核对或明确不采用')
    return c
  })
}
export function finishedPlans(lines: Array<{ id: string; product: Product; quantity: number; state: InventoryLineState }>) {
  const used = new Map<number, number>()
  for (const line of lines) {
    line.state.plan = emptyPlan()
    line.state.allocations = []
    const candidates = selectedCandidates('finished', line.state.parts.finished)
    if (!candidates.length) continue
    const first = candidates[0]!.quantity_contract
    if (!first) throw new Error('成品库存缺少实物换算合同，请刷新核对')
    const a = integer(first.customer_basis, '客户换算数'), b = integer(first.physical_basis, '实物换算数')
    for (const c of candidates) {
      integer(c.lot_id, '批次'); integer(c.version, '批次版本'); integer(c.quantity_available, '成品可用数量', true)
      const rule = c.quantity_contract
      if (!rule || rule.customer_id !== line.product.customer_id || rule.product_id !== line.product.id || rule.customer_basis !== a || rule.physical_basis !== b || rule.physical_unit !== first.physical_unit) throw new Error('库存客户/产品/换算合同不一致，请重新核对')
    }
    function gcd(x: number, y: number): number { return y ? gcd(y, x % y) : x }
    const divisor = gcd(a, b), customerGroup = a / divisor, physicalGroup = b / divisor
    const capacity = candidates.map(c => Math.max(c.quantity_available! - (used.get(c.lot_id) || 0), 0))
    let remaining = Math.min(Math.floor(integer(line.quantity, '订单数量') / customerGroup), Math.floor(capacity.reduce((s, n) => s + n, 0) / physicalGroup)) * physicalGroup
    candidates.forEach((c, index) => {
      const take = Math.min(capacity[index]!, remaining)
      if (take > 0) {
        line.state.plan.finished.push({ lot_id: c.lot_id, expected_version: c.version, requested_qty: Math.ceil(take / physicalGroup) * customerGroup, recommendation_source: 'dedicated', confirmed: true })
        line.state.allocations.push({ part: 'finished', lot_id: c.lot_id, stock_quantity: take, credited_quantity: take * customerGroup / physicalGroup })
        used.set(c.lot_id, (used.get(c.lot_id) || 0) + take); remaining -= take
      }
    })
  }
}
export function semiPlans(lines: Array<{ product: Product; state: InventoryLineState }>) {
  const used = new Map<number, number>()
  for (const line of lines) {
    line.state.plan.semi = []
    line.state.allocations = line.state.allocations.filter(a => a.part === 'finished')
    for (const component of line.state.authority?.requisition_components || []) {
      let remaining = integer(component.required_piece_quantity, '本体需求', true)
      for (const c of selectedCandidates(component.component_type, line.state.parts[component.component_type])) {
        integer(c.lot_id, '批次'); integer(c.version, '批次版本')
        const factor = integer(c.stock_yield_per_sheet, '片料出数'), free = Math.max(integer(c.available_stock_quantity, '片料可用张数', true) - (used.get(c.lot_id) || 0), 0)
        const request = Math.min(remaining, free * factor)
        if (!request) continue
        const source = c.recommendation_source || c.source || 'signature', direct = directSheet(c), general = generalSheet(c)
        const override = !direct && (general || source === 'customer_generic' || c.source === 'manual' || (source === 'learned' && !!c.signature_differences?.length))
        const warnings = [...(source === 'customer_generic' ? ['CUSTOMER_GENERIC_SEMI_FINISHED_STOCK'] : []), ...(override && !general ? ['SEMI_SIGNATURE_OVERRIDE'] : []), ...(general ? ['GENERAL_SEMI_FINISHED_STOCK'] : [])]
        line.state.plan.semi.push({ lot_id: c.lot_id, expected_version: c.version, requested_qty: request, component_type: component.component_type, recommendation_source: source,
          match_rule_id: c.match_rule_id || null, direct_deduction: direct, override, confirmed: true, warning_acknowledged_codes: warnings })
        line.state.allocations.push({ part: component.component_type, lot_id: c.lot_id, stock_quantity: Math.ceil(request / factor), credited_quantity: request })
        used.set(c.lot_id, (used.get(c.lot_id) || 0) + Math.ceil(request / factor)); remaining -= request
      }
    }
  }
}
export function applyInventoryAuthority(lines: Array<{ id: string; product: Product; quantity: number; state: InventoryLineState }>, items: InventoryAuthority[]) {
  if (items.length !== lines.length || new Set(items.map(r => r.client_line_id)).size !== items.length) throw new Error('库存计算缺少明细或返回重复行')
  for (const line of lines) {
    const row = items.find(r => r.client_line_id === line.id)
    if (!row || row.product_id !== line.product.id || row.order_quantity !== line.quantity) throw new Error('库存计算明细身份或数量不一致')
    if (row.coverage_state === 'unsupported') throw new Error(row.message || '当前组合产品库存须按原明细组件流程核对')
    line.state.authority = row
  }
}
export function inventoryLocation(c: InventoryCandidate) { const l = c.warehouse_location; return l?.employee_location_name || l?.current_address_name || l?.location_name || '位置未登记' }
