import { computed, ref, watch } from 'vue'
import { warehouseApi, type Product } from '../api/client'
import { newInventoryLine, inventoryComponents, semiCandidateRequest, finishedPlans, semiPlans, applyInventoryAuthority, type InventoryPart, type InventoryLineState, type InventoryDraftLine } from '../utils/orderInventory'

export interface InventoryInput { id: string; product: Product; quantity: number }
export function useOrderInventory(customer: () => number | null, inputs: () => InventoryInput[], permitted: () => boolean) {
  const states = ref<Record<string, InventoryLineState>>({}), busy = ref(false), checked = ref(false), error = ref(''), changed = ref(false)
  const signature = computed(() => JSON.stringify({ customer: customer(), rows: inputs().map(l => ({ id: l.id, product: l.product, quantity: l.quantity })) }))
  let generation = 0
  watch(signature, () => { generation++; busy.value = false; checked.value = false; changed.value = true; error.value = ''; for (const s of Object.values(states.value)) s.authority = null })
  function active() {
    const all = inputs()
    if (!customer() || !all.length || all.some(l => l.product.customer_id !== customer() || !Number.isSafeInteger(l.quantity) || l.quantity <= 0)) throw new Error('请先选客户和常用箱，并填写有效整数数量后核对库存')
    if (all.length > 200) throw new Error('一次最多核对200行库存')
    return all.map(l => ({ ...l, state: states.value[l.id]! }))
  }
  function draft(rows: ReturnType<typeof active>): InventoryDraftLine[] { return rows.map(l => ({ client_line_id: l.id, product_id: l.product.id, quantity: l.quantity, material: l.product.material_code || l.product.default_material_text || undefined, flute_type: l.product.flute_type || l.product.flute_type_code || undefined, reservation_plan: l.state.plan, finished_skipped: l.state.parts.finished.mode === 'skipped' })) }
  async function calculate(serial: number, fingerprint: string) {
    const rows = active(); finishedPlans(rows)
    const first = await warehouseApi.previewDraft(customer()!, draft(rows))
    if (serial !== generation || signature.value !== fingerprint) return false
    applyInventoryAuthority(rows, first.items); semiPlans(rows)
    const final = await warehouseApi.previewDraft(customer()!, draft(rows))
    if (serial !== generation || signature.value !== fingerprint) return false
    applyInventoryAuthority(rows, final.items); checked.value = true; changed.value = false; return true
  }
  async function refresh(forSave = false) {
    if (!permitted()) return false
    const serial = ++generation, fingerprint = signature.value, previous = states.value, wasChecked = checked.value
    busy.value = true; checked.value = false; error.value = ''
    try {
      active()
      const loaded = await Promise.all(inputs().map(async line => {
        const state = newInventoryLine(line.product.id, line.quantity), old = previous[line.id]
        state.parts.finished.candidates = (await warehouseApi.finishedCandidates(line.product.id, customer()!)).items
        for (const component of inventoryComponents(line.product)) {
          const payload = semiCandidateRequest(line.product, customer()!, component)
          if (payload) state.parts[component].candidates = (await warehouseApi.semiCandidates(line.product.id, payload)).items
          else state.parts[component].error = '常用箱报料尺寸、材质或楞型缺失，不能推荐片料'
        }
        if (old && old.product_id === line.product.id && old.quantity === line.quantity) {
          for (const key of ['finished', ...inventoryComponents(line.product)] as InventoryPart[]) {
            const part = state.parts[key], prior = old.parts[key]
            part.mode = prior.mode; part.selected = [...prior.selected]
            if (key !== 'finished' && prior.manual.length) {
              const payload = semiCandidateRequest(line.product, customer()!, key)!
              const pages = [...new Set([prior.page, ...prior.manual.filter(c=>prior.selected.includes(c.lot_id)).map(c=>c.manual_page || prior.page)])]
              for (const page of pages) {
                const manual = await warehouseApi.semiCandidates(line.product.id, payload, page)
                part.manual.push(...manual.items.filter(c=>page===prior.page || prior.selected.includes(c.lot_id)).map(c => ({ ...c, manual_page: page, recommendation_source: c.recommendation_source || c.source || 'manual', source: c.source === 'general_signature' ? c.source : 'manual' })))
                if (page===prior.page) part.total=manual.total || 0
              }
              part.page = prior.page
            }
            if (forSave && wasChecked) {
              const priorPool = [...prior.candidates, ...prior.manual], nextPool = [...part.candidates, ...part.manual]
              const selected = key === 'finished' ? old.plan.finished : old.plan.semi.filter(e => e.component_type === key)
              for (const entry of selected) {
                const before = priorPool.find(c => c.lot_id === entry.lot_id), after = nextPool.find(c => c.lot_id === entry.lot_id)
                if (!before || !after || before.version !== after.version) throw new Error('已安排库存批次或版本已变化，请重新核对库存后再保存；订单草稿已保留')
              }
            }
          }
        }
        return [line.id, state] as const
      }))
      if (serial !== generation || signature.value !== fingerprint) return false
      states.value = Object.fromEntries(loaded)
      if (!await calculate(serial, fingerprint)) return false
      if (forSave && wasChecked && JSON.stringify(Object.values(previous).map(s => s.plan)) !== JSON.stringify(Object.values(states.value).map(s => s.plan))) throw new Error('库存安排数量已变化，请核对新结果后再次保存')
      return true
    } catch (e) { if (serial === generation) { checked.value = false; for(const s of Object.values(states.value)) s.authority=null; error.value = e instanceof Error ? e.message : '库存读取失败' } return false }
    finally { if (serial === generation) busy.value = false }
  }
  async function choose(id: string, part: InventoryPart, lot: number | null, automatic = false) {
    if (busy.value) return
    const state = states.value[id]; if (!state) return
    const s = state.parts[part]
    s.mode = automatic ? 'automatic' : lot === null ? 'skipped' : 'exact'; s.selected = lot === null ? [] : [lot]
    const serial = ++generation, fingerprint = signature.value; busy.value = true; checked.value = false; error.value = ''
    try { await calculate(serial, fingerprint) } catch(e) { if (serial === generation) { for(const s of Object.values(states.value)) s.authority=null; error.value = e instanceof Error ? e.message : '库存计算失败' } }
    finally { if (serial === generation) busy.value = false }
  }
  async function browse(id: string, component: 'whole' | 'cover' | 'base', page: number) {
    if (busy.value || page < 1) return
    const line = inputs().find(l => l.id === id), state = states.value[id]; if (!line || !state) return
    const payload = semiCandidateRequest(line.product, customer()!, component); if (!payload) return
    const serial = ++generation, fingerprint = signature.value; busy.value = true
    try {
      const result = await warehouseApi.semiCandidates(line.product.id, payload, page)
      if (serial !== generation || signature.value !== fingerprint) return
      const part = state.parts[component]
      const retained = part.manual.filter(c=>part.selected.includes(c.lot_id) && !result.items.some(r=>r.lot_id===c.lot_id))
      part.manual = [...retained, ...result.items.map(c => ({ ...c, manual_page: page, recommendation_source: c.recommendation_source || c.source || 'manual', source: c.source === 'general_signature' ? c.source : 'manual' }))]
      part.page = page; part.total = result.total || 0; part.error = ''
    } catch(e) { if (serial === generation) state.parts[component].error = e instanceof Error ? e.message : '片料读取失败' }
    finally { if (serial === generation) busy.value = false }
  }
  return { states, busy, checked, error, changed, refresh, choose, browse }
}
