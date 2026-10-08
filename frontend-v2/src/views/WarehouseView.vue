<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  warehouseApi,
  type DeliveryPickTaskSummary,
  type WarehouseLot,
  type WarehouseLotDetail,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import { buildReadonlyInventoryLocatorHref } from '../utils/inventoryLocator'

const authStore = useAuthStore()
const loading = ref(true)
const loadError = ref(false)
const lots = ref<WarehouseLot[]>([])
const lotTotal = ref(0)
const pickTasks = ref<DeliveryPickTaskSummary[]>([])
const pickTotal = ref(0)
const lotDetail = ref<WarehouseLotDetail | null>(null)
const lotDialog = ref(false)
const keyword = ref('')
const inventoryType = ref('')
const lotPage = ref(1)
const pickPage = ref(1)
const pageSize = 20
let requestSerial = 0

const quantityGroups = computed(() => {
  const groups = new Map<string, { available: number; reserved: number }>()
  for (const row of lots.value) {
    const key = `${inventoryTypeLabel(row.inventory_type)} · ${row.display_unit || row.unit || '单位未登记'}`
    const current = groups.get(key) || { available: 0, reserved: 0 }
    current.available += Number(row.quantity_available || 0)
    current.reserved += Number(row.quantity_reserved || 0)
    groups.set(key, current)
  }
  return [...groups.entries()].map(([label, amounts]) => ({ label, ...amounts }))
})

function lotName(row: WarehouseLot) {
  return row.detail?.product_name || row.detail?.internal_name || row.detail?.material_code || '未登记品名'
}

function lotSpecification(row: WarehouseLot) {
  if (row.inventory_type === 'finished') return row.detail?.inventory_code || '—'
  const length = row.detail?.board_length_mm
  const width = row.detail?.board_width_mm
  return length && width ? `${length} × ${width} mm` : '—'
}

function locationName(row: WarehouseLot) {
  return row.location?.employee_location_name || row.location?.location_name || '尚未绑定正式位置'
}

function lotLocatorHref(row: WarehouseLot) {
  const positionName = row.location?.employee_location_name || row.location?.location_name
  if (!authStore.hasPermission('warehouse.view') || typeof positionName !== 'string' || !positionName.trim()) return undefined
  return buildReadonlyInventoryLocatorHref(row.id, row.inventory_type)
}

function inventoryTypeLabel(value: string) {
  return value === 'finished' ? '成品' : value === 'semi_finished' ? '半成品/纸板' : value
}

function pickStatusLabel(value: string) {
  return ({ pushed: '待拿货', picking: '拿货中', submitted: '待确认', applied: '已确认', dispatched: '已发车', exception: '异常' } as Record<string, string>)[value] || value
}

function pickPrintUrl(taskId: number) {
  return `/static/delivery-pick-print.html?task_id=${encodeURIComponent(taskId)}&preview=1`
}

async function loadData() {
  const serial = ++requestSerial
  loading.value = true
  loadError.value = false
  try {
    const [lotResult, pickResult] = await Promise.all([
      warehouseApi.listLots({
        keyword: keyword.value.trim(),
        inventoryType: inventoryType.value,
        page: lotPage.value,
        pageSize,
      }),
      warehouseApi.listPickTasks(pickPage.value, pageSize),
    ])
    if (serial !== requestSerial) return
    lots.value = lotResult.items
    lotTotal.value = lotResult.total
    pickTasks.value = pickResult.items
    pickTotal.value = pickResult.total
  } catch (error) {
    if (serial === requestSerial) {
      loadError.value = true
      ElMessage.error(error instanceof Error ? error.message : '库存与拿货数据加载失败')
    }
  } finally {
    if (serial === requestSerial) loading.value = false
  }
}

function search() {
  lotPage.value = 1
  void loadData()
}

async function openLot(id: number) {
  try {
    lotDetail.value = await warehouseApi.getLot(id)
    lotDialog.value = true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '批次详情加载失败')
  }
}

onMounted(loadData)
</script>

<template>
  <div class="tm-page" v-loading="loading">
    <div class="quantity-contract">
      <strong>数量口径：</strong>
      客户订单/送货单使用客户单据数量；本页库存使用仓库实际数量。两者只做业务关联，不互相覆盖。
    </div>

    <section class="kpi-row">
      <div class="tm-kpi">
        <div class="kpi-label">当前筛选批次</div>
        <div class="kpi-value">{{ lotTotal }}</div>
      </div>
      <div class="tm-kpi">
        <div class="kpi-label">当前页仓库可用实数（分单位）</div>
        <div v-if="loading" class="quantity-state tm-muted" role="status">正在加载库存数量…</div>
        <div v-else-if="loadError" class="quantity-state quantity-state-error" role="status">库存数量加载失败，请查询重试</div>
        <div v-else-if="quantityGroups.length === 0" class="quantity-state tm-muted" role="status">当前页暂无库存批次</div>
        <template v-else>
          <div v-for="group in quantityGroups" :key="group.label" class="quantity-group">{{ group.label }}：{{ group.available }}</div>
        </template>
      </div>
      <div class="tm-kpi warn">
        <div class="kpi-label">当前页已预占实数（分单位）</div>
        <div v-if="loading" class="quantity-state tm-muted" role="status">正在加载库存数量…</div>
        <div v-else-if="loadError" class="quantity-state quantity-state-error" role="status">库存数量加载失败，请查询重试</div>
        <div v-else-if="quantityGroups.length === 0" class="quantity-state tm-muted" role="status">当前页暂无库存批次</div>
        <template v-else>
          <div v-for="group in quantityGroups" :key="group.label" class="quantity-group">{{ group.label }}：{{ group.reserved }}</div>
        </template>
      </div>
      <div class="tm-kpi">
        <div class="kpi-label">拿货任务</div>
        <div class="kpi-value">{{ pickTotal }}</div>
      </div>
    </section>

    <section class="tm-card tm-section">
      <div class="tm-toolbar">
        <div>
          <h2 class="tm-section-title">仓库实际库存</h2>
          <div class="tm-muted">隔离测试库使用现行成品/半成品批次口径；原片库存尚未接入本页。地图位置只作空间投影，不另建数量账。</div>
        </div>
        <el-input v-model="keyword" clearable placeholder="批次、品名、编码或库位" style="width: 260px" @keyup.enter="search" @clear="search" />
        <el-select v-model="inventoryType" clearable placeholder="全部库存" style="width: 150px">
          <el-option label="成品" value="finished" />
          <el-option label="半成品/纸板" value="semi_finished" />
        </el-select>
        <el-button type="primary" @click="search">查询</el-button>
      </div>
      <vxe-table border stripe show-overflow="ellipsis" height="390" :data="lots">
        <vxe-column field="lot_number" title="库存批次" width="180" />
        <vxe-column title="类型" width="110">
          <template #default="{ row }">{{ inventoryTypeLabel(row.inventory_type) }}</template>
        </vxe-column>
        <vxe-column title="品名/材质" min-width="180">
          <template #default="{ row }">{{ lotName(row) }}</template>
        </vxe-column>
        <vxe-column title="编码/规格" min-width="160">
          <template #default="{ row }">{{ lotSpecification(row) }}</template>
        </vxe-column>
        <vxe-column title="客户" min-width="150">
          <template #default="{ row }">{{ row.detail?.owner_customer_name || '通用/未指定' }}</template>
        </vxe-column>
        <vxe-column title="实际可用" width="110">
          <template #default="{ row }"><strong>{{ row.quantity_available }} {{ row.display_unit || row.unit }}</strong></template>
        </vxe-column>
        <vxe-column field="quantity_reserved" title="已预占" width="90" />
        <vxe-column title="实际库位" min-width="180">
          <template #default="{ row }">
            {{ locationName(row) }}
            <a v-if="lotLocatorHref(row)" :href="lotLocatorHref(row)" target="_blank" rel="noopener noreferrer" title="在新标签查看该批次的正式库存位置" style="margin-left:8px">定位 ↗</a>
          </template>
        </vxe-column>
        <vxe-column field="age_warning_text" title="库龄" min-width="140" />
        <vxe-column title="操作" width="85" fixed="right"><template #default="{ row }"><el-button type="primary" link @click="openLot(row.id)">下钻</el-button></template></vxe-column>
      </vxe-table>
      <el-pagination v-model:current-page="lotPage" :page-size="pageSize" :total="lotTotal" layout="prev, pager, next, jumper, total" style="margin-top:12px" @current-change="loadData" />
    </section>
    <el-dialog v-model="lotDialog" :title="`库存批次 ${lotDetail?.lot_number || ''}`" width="700px">
      <template v-if="lotDetail">
        <p>品名：{{ lotName(lotDetail) }}　客户：{{ lotDetail.detail?.owner_customer_name || '通用/未指定' }}</p>
        <p>
          库位：{{ locationName(lotDetail) }}
          <a v-if="lotLocatorHref(lotDetail)" :href="lotLocatorHref(lotDetail)" target="_blank" rel="noopener noreferrer" title="在新标签查看该批次的正式库存位置" style="margin-left:8px">定位 ↗</a>
          　类型：{{ inventoryTypeLabel(lotDetail.inventory_type) }}
        </p>
        <p>实际可用：{{ lotDetail.quantity_available }} {{ lotDetail.display_unit || lotDetail.unit }}　已预占：{{ lotDetail.quantity_reserved }} {{ lotDetail.display_unit || lotDetail.unit }}</p>
        <p class="tm-muted">本页只读；批次流水 {{ lotDetail.movements?.length || 0 }} 条、预占记录 {{ lotDetail.reservations?.length || 0 }} 条。正式拿货仍按现有任务流程。</p>
      </template>
      <template #footer><el-button @click="lotDialog = false">关闭</el-button></template>
    </el-dialog>

    <section class="tm-card tm-section">
      <div class="tm-toolbar">
        <div>
          <h2 class="tm-section-title">送货拿货任务</h2>
          <div class="tm-muted">本批预览先提供任务和异常总览；逐库位确认、提交和正式扣减仍使用现有成熟流程。</div>
        </div>
        <el-button @click="loadData">刷新</el-button>
      </div>
      <vxe-table border stripe show-overflow="ellipsis" height="300" :data="pickTasks">
        <vxe-column field="delivery_number" title="送货单号" width="190" />
        <vxe-column field="customer_name" title="客户" min-width="180" />
        <vxe-column title="状态" width="120">
          <template #default="{ row }"><el-tag :type="row.has_exception ? 'danger' : 'info'">{{ pickStatusLabel(row.status) }}</el-tag></template>
        </vxe-column>
        <vxe-column field="assigned_to_name" title="拿货员" width="140" />
        <vxe-column title="分配" width="120">
          <template #default="{ row }">{{ row.assignment_required ? '待分配' : '已分配' }}</template>
        </vxe-column>
        <vxe-column field="created_at" title="建立时间" min-width="180" />
        <vxe-column title="内部拿货单" width="125" fixed="right">
          <template #default="{ row }">
            <a :href="pickPrintUrl(row.id)" target="_blank" rel="noopener noreferrer">预览打印</a>
          </template>
        </vxe-column>
      </vxe-table>
      <el-pagination v-model:current-page="pickPage" :page-size="pageSize" :total="pickTotal" layout="prev, pager, next, jumper, total" style="margin-top:12px" @current-change="loadData" />
    </section>
  </div>
</template>

<style scoped>
.quantity-contract {
  margin-bottom: 14px;
  padding: 12px 16px;
  border: 1px solid var(--tm-warn-line);
  border-radius: 8px;
  background: var(--tm-warn-bg);
  color: var(--tm-warn);
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-bottom: 14px;
}
.quantity-group { margin-top: 4px; font-size: 13px; font-weight: 700; }
.quantity-state { margin-top: 4px; font-size: 13px; line-height: 1.5; }
.quantity-state-error { color: var(--tm-danger); }
</style>
