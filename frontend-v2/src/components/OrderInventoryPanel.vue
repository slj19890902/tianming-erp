<script setup lang="ts">
import type { Product } from '../api/client'
import { inventoryComponents, inventoryLocation, type InventoryLineState, type InventoryPart, type InventoryCandidate } from '../utils/orderInventory'
defineProps<{ product: Product; state: InventoryLineState; disabled: boolean; lineNumber: number }>()
const emit = defineEmits<{ choose: [part: InventoryPart, lot: number | null, automatic?: boolean]; browse: [part: 'whole' | 'cover' | 'base', page: number] }>()
const componentName = (p: InventoryPart) => ({ finished: '成品', whole: '本体片料', cover: '盖片料', base: '底片料' })[p]
const processingName = (c: InventoryCandidate) => ({ raw: '未加工', cut: '已裁切', die_cut: '已模切', creased: '已压线', printed: '已印刷' } as Record<string,string>)[c.processing || ''] || '未登记'
const warningText = (text: string) => text === '人工浏览候选必须核对差异并明确 override。' ? '规格存在差异；采用此批表示已核对，并保留该批实际规格、材质和来源。' : text
function locationURL(c: InventoryCandidate, part: InventoryPart) {
  const id = c.warehouse_location?.id
  return id ? `/warehouse.html?${new URLSearchParams({ readonly: '1', mode: 'lookup', view: '2d', location_id: String(id), lot_id: String(c.lot_id), inventory_type: part === 'finished' ? 'finished' : 'semi_finished' })}` : ''
}
</script>
<template>
  <details class="inventory-panel" :aria-label="`第${lineNumber}行库存安排`">
    <summary>第{{ lineNumber }}行 · {{ product.product_code }} · 库存安排
      <span v-if="state.authority">：成品抵扣 {{ state.authority.finished_planned_quantity || 0 }} {{ product.unit || '只' }} · 剩余生产 {{ state.authority.production_required_quantity }} {{ product.unit || '只' }} · 需报 {{ state.authority.requisition_sheet_quantity ?? '按BOM' }} 张</span>
      <span v-else>（尚未核对当前库存）</span>
    </summary>
    <p v-if="state.authority">实存 {{ state.authority.finished_stock_on_hand_quantity }} / 已预占 {{ state.authority.finished_stock_reserved_quantity }} / 安排后可用余量 {{ state.authority.finished_stock_remaining_quantity }} {{ state.authority.finished_stock_unit }}。预览不扣库，保存时再次校验。</p>
    <section v-for="part in (['finished', ...inventoryComponents(product)] as InventoryPart[])" :key="part">
      <div class="inventory-actions">
        <strong>{{ componentName(part) }}</strong>
        <el-button size="small" type="success" :disabled="disabled" @click="emit('choose',part,null,true)">采用安全推荐</el-button>
        <el-button size="small" type="danger" :disabled="disabled" @click="emit('choose',part,null)">本行{{ componentName(part) }}不采用</el-button>
        <span v-if="state.parts[part].mode==='skipped'">本次不采用</span>
      </div>
      <p v-if="state.parts[part].error" role="alert">{{ state.parts[part].error }}</p>
      <div class="inventory-table-scroll">
        <table>
          <thead><tr v-if="part==='finished'"><th>成品可用实物</th><th>位置</th><th>本次拟用</th><th>采用/不采用</th></tr>
            <tr v-else><th>货物名称</th><th>纸板长</th><th>纸板宽</th><th>楞型</th><th>材质</th><th>加工类型</th><th>数量</th><th>位置</th><th>采用/不采用</th></tr></thead>
          <tbody><tr v-for="c in [...state.parts[part].candidates,...state.parts[part].manual.filter(c=>!state.parts[part].candidates.some(p=>p.lot_id===c.lot_id))]" :key="c.lot_id">
            <template v-if="part==='finished'"><td>{{ c.quantity_available }} {{ c.quantity_contract?.physical_unit || '实物' }}</td>
              <td>{{ inventoryLocation(c) }} <a v-if="locationURL(c,part)" :href="locationURL(c,part)" target="_blank" rel="noopener">地图定位</a></td>
              <td>{{ state.allocations.filter(a=>a.part===part&&a.lot_id===c.lot_id).reduce((s,a)=>s+a.stock_quantity,0) }} {{ c.quantity_contract?.physical_unit || '实物' }}</td></template>
            <template v-else><td>{{ c.internal_name || '未登记' }}</td><td>{{ c.board_length_mm || '未登记' }}</td><td>{{ c.board_width_mm || '未登记' }}</td><td>{{ c.flute_type || '未登记' }}</td><td>{{ c.material_code || '未登记' }}</td><td>{{ processingName(c) }}</td><td>{{ c.available_stock_quantity }} 张
              <strong v-if="state.allocations.some(a=>a.part===part&&a.lot_id===c.lot_id)"> · 拟用 {{ state.allocations.filter(a=>a.part===part&&a.lot_id===c.lot_id).reduce((s,a)=>s+a.stock_quantity,0) }} 张</strong></td>
              <td>{{ inventoryLocation(c) }} <a v-if="locationURL(c,part)" :href="locationURL(c,part)" target="_blank" rel="noopener">地图定位</a></td></template>
            <td><el-button size="small" type="success" :disabled="disabled||c.selectable===false" @click="emit('choose',part,c.lot_id)">采用此批</el-button>
              <p v-for="warning in c.warning_messages" :key="warning">{{ warningText(warning) }}</p>
              <span v-if="c.selectable===false">用途待核，不能采用</span></td>
          </tr><tr v-if="!state.parts[part].candidates.length&&!state.parts[part].manual.length"><td :colspan="part==='finished'?4:9">暂无可用候选</td></tr></tbody>
        </table>
      </div>
      <template v-if="part!=='finished'">
        <el-button size="small" :disabled="disabled" @click="emit('browse',part,1)">查看其他可用片料</el-button>
        <template v-if="state.parts[part].total"><el-button size="small" :disabled="disabled||state.parts[part].page<=1" @click="emit('browse',part,state.parts[part].page-1)">上一页</el-button>
          <span>第{{ state.parts[part].page }}页 · {{ state.parts[part].total }}批</span>
          <el-button size="small" :disabled="disabled||state.parts[part].page*20>=state.parts[part].total" @click="emit('browse',part,state.parts[part].page+1)">下一页</el-button></template>
      </template>
    </section>
  </details>
</template>
<style scoped>
.inventory-panel{border:1px solid #e5d28d;border-radius:6px;padding:10px;margin:8px 0;background:#fffdf4}.inventory-panel summary{cursor:pointer;font-weight:600}.inventory-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:10px 0}.inventory-table-scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:760px;font-size:13px}th,td{border-bottom:1px solid #e2e8f0;padding:8px;text-align:left}th{white-space:nowrap}td p{color:#9a5b00;max-width:220px}a{color:#1761b7}
</style>
