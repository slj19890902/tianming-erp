<script setup lang="ts">
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { orderApi, type OrderDetail, type OrderSummary } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { formalOrderEntryQuery, type FormalOrderEntry } from '../utils/formalOrderEntry'
import OrderDrawingPreview from '../components/OrderDrawingPreview.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const canViewSalesAmounts = computed(() => ['admin', 'boss', 'sales', 'finance'].includes(auth.user?.role || '') && auth.hasPermission('orders.view'))
function openOriginalEntry(action: FormalOrderEntry) {
  if (!auth.hasPermission('orders.create')) return
  if (action === 'email' && !['admin', 'boss'].includes(auth.user?.role || '')) return
  void router.push({ path: '/orders', query: formalOrderEntryQuery(action) })
}
const rows = ref<OrderSummary[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const keyword = ref('')
const scope = ref('active')
const loading = ref(false)
const detailLoading = ref(false)
const detail = ref<OrderDetail | null>(null)
const detailVisible = ref(false)
const drawingPreviewSource = ref('')
const editing = ref(false)
const saving = ref(false)
const editForm = ref({ customer_po: '', delivery_date: '', remark: '' })
type OrdersQuery = { page: number; keyword: string; scope: string }
const loadError = ref('')
const hasSuccessfulLoad = ref(false)
const lastSuccessfulQuery = ref<OrdersQuery | null>(null)
const failedQuery = ref<OrdersQuery | null>(null)
const listCard = ref<HTMLElement | null>(null)
const tableHost = ref<HTMLElement | null>(null)
const paginationHost = ref<HTMLElement | null>(null)
const tableHeight = ref(480)
let measureFrame: number | null = null
let resizeObserver: ResizeObserver | null = null
let measuring = false
let requestSerial = 0
let refreshPending = false
function refreshSavedOrders() {
  if (!refreshPending || route.path !== '/review/orders') return
  refreshPending = false
  // Re-read the committed query; never submit draft filter text or reset edits.
  void loadOrders({ ...(lastSuccessfulQuery.value || failedQuery.value || currentQuery()) })
}
function receiveSavedOrders(event: Event) {
  if ((event as CustomEvent).detail?.actorId !== auth.user?.id || !auth.hasPermission('orders.view')) return
  refreshPending = true
  refreshSavedOrders()
}

function currentQuery(): OrdersQuery {
  return { page: page.value, keyword: keyword.value.trim(), scope: scope.value }
}

function describeQuery(query: OrdersQuery | null) {
  if (!query) return '尚无成功查询'
  const names: Record<string, string> = { active: '进行中', completed: '已完成', cancelled: '已取消', all: '全部' }
  return `第${query.page}页 · ${names[query.scope] || query.scope} · ${query.keyword ? `关键词“${query.keyword}”` : '无关键词'}`
}

function calculateTableHeight(viewportHeight: number, top: number, footerHeight: number) {
  return Math.max(240, Math.floor(viewportHeight - Math.max(0, top) - footerHeight - 28))
}

function measureTableHeight() {
  const host = tableHost.value
  if (!measuring || !host || !host.isConnected || !host.getClientRects().length) return
  const top = host.getBoundingClientRect().top
  const footerHeight = paginationHost.value?.getBoundingClientRect().height || 32
  if (!Number.isFinite(top) || !Number.isFinite(footerHeight)) return
  tableHeight.value = calculateTableHeight(window.innerHeight, top, footerHeight)
}

function scheduleTableMeasure() {
  if (!measuring || measureFrame !== null) return
  measureFrame = window.requestAnimationFrame(() => {
    measureFrame = null
    measureTableHeight()
  })
}

function startTableMeasurements() {
  if (!measuring) {
    measuring = true
    window.addEventListener('resize', scheduleTableMeasure)
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(scheduleTableMeasure)
      if (listCard.value) resizeObserver.observe(listCard.value)
      if (paginationHost.value) resizeObserver.observe(paginationHost.value)
    }
  }
  void nextTick(scheduleTableMeasure)
}

function stopTableMeasurements() {
  measuring = false
  window.removeEventListener('resize', scheduleTableMeasure)
  resizeObserver?.disconnect()
  resizeObserver = null
  if (measureFrame !== null) window.cancelAnimationFrame(measureFrame)
  measureFrame = null
}

function retryOrders() {
  if (loading.value || !failedQuery.value) return
  void loadOrders({ ...failedQuery.value })
}

function changePage(nextPage: number) {
  const query = lastSuccessfulQuery.value
  if (!query || loading.value || loadError.value) return
  page.value = nextPage
  void loadOrders({ ...query, page: nextPage })
}

async function loadOrders(query: OrdersQuery = currentQuery()) {
  const serial = ++requestSerial
  loading.value = true
  loadError.value = ''
  try {
    const result = await orderApi.listOrders(query.page, pageSize, {
      keyword: query.keyword, scope: query.scope,
    })
    if (serial !== requestSerial) return
    rows.value = result.items
    total.value = result.total
    hasSuccessfulLoad.value = true
    lastSuccessfulQuery.value = { ...query }
    failedQuery.value = null
  } catch (error) {
    if (serial === requestSerial) {
      loadError.value = error instanceof Error ? error.message : '订单加载失败'
      failedQuery.value = { ...query }
      ElMessage.error(loadError.value)
    }
  } finally {
    if (serial === requestSerial) loading.value = false
  }
}

function search() {
  page.value = 1
  void loadOrders()
}

async function openDetail(id: number) {
  detailLoading.value = true
  editing.value = false
  try {
    detail.value = await orderApi.getOrder(id)
    editForm.value = {
      customer_po: detail.value.customer_po || '',
      delivery_date: detail.value.delivery_date || '',
      remark: detail.value.remark || '',
    }
    detailVisible.value = true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '订单详情加载失败')
  } finally {
    detailLoading.value = false
  }
}

async function saveBasicInfo() {
  if (!detail.value || saving.value) return
  const id = detail.value.id
  saving.value = true
  try {
    await orderApi.updateOrder(id, {
      customer_po: editForm.value.customer_po.trim(),
      delivery_date: editForm.value.delivery_date || null,
      remark: editForm.value.remark.trim(),
    })
    const saved = await orderApi.getOrder(id)
    if ((saved.customer_po || '') !== editForm.value.customer_po.trim()
      || (saved.delivery_date || '') !== editForm.value.delivery_date
      || (saved.remark || '') !== editForm.value.remark.trim()) {
      ElMessage.warning('基础信息已提交，但回读不一致，请核对订单详情')
      return
    }
    detail.value = saved
    editing.value = false
    ElMessage.success('订单基础信息已保存并回读')
    await loadOrders()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '订单更新失败')
  } finally {
    saving.value = false
  }
}

watch(() => route.query.order_id, (value) => {
  const id = Number(value)
  if (route.path !== '/review/orders' || !Number.isSafeInteger(id) || id <= 0) return
  void openDetail(id)
  // A saved order can return to this cached view. Refresh the last committed
  // list query without submitting draft filter controls or resetting edits.
  if (hasSuccessfulLoad.value || failedQuery.value) {
    void loadOrders({ ...(lastSuccessfulQuery.value || failedQuery.value || currentQuery()) })
  }
}, { immediate: true })
watch([loadError, hasSuccessfulLoad], scheduleTableMeasure, { flush: 'post' })
onMounted(() => { window.addEventListener('tianming-orders-changed', receiveSavedOrders);startTableMeasurements(); void loadOrders() })
onActivated(() => {startTableMeasurements();refreshSavedOrders()})
onDeactivated(stopTableMeasurements)
onBeforeUnmount(() => {window.removeEventListener('tianming-orders-changed', receiveSavedOrders);stopTableMeasurements()})
</script>

<template>
  <div class="tm-page orders-workspace">
    <OrderDrawingPreview :source="drawingPreviewSource" @close="drawingPreviewSource = ''" />
    <section ref="listCard" class="tm-card tm-section orders-list-card">
      <div class="tm-toolbar">
        <h2 class="tm-section-title">订单列表</h2>
        <el-button v-if="auth.hasPermission('orders.create')" type="primary" @click="openOriginalEntry('new')">新建订单</el-button>
        <el-button v-if="auth.hasPermission('orders.create')" @click="openOriginalEntry('import')">导入订单</el-button>
        <el-button v-if="auth.hasPermission('orders.create') && ['admin', 'boss'].includes(auth.user?.role || '')" @click="openOriginalEntry('email')">邮箱收单</el-button>
        <el-button @click="router.push('/orders')">完整业务队列</el-button>
      </div>
      <p class="tm-muted">此页按单张主单核对。原来的客户单号合并、客户热力、列偏好、库存抵扣和完整编辑请从完整业务队列继续。</p>
      <div class="tm-toolbar">
        <el-input v-model="keyword" clearable placeholder="订单号 / 客户 / 客户PO / 存货编码 / 产品" style="width: 330px" @keyup.enter="search" @clear="search" />
        <el-select v-model="scope" style="width: 130px" @change="search">
          <el-option label="进行中" value="active" />
          <el-option label="已完成" value="completed" />
          <el-option label="已取消" value="cancelled" />
          <el-option label="全部" value="all" />
        </el-select>
        <el-button :loading="loading" @click="search">查询</el-button>
        <span v-if="hasSuccessfulLoad" class="tm-muted">共 {{ total }} 单，按服务器分页查询</span><el-tooltip content="订单数量、待交数量沿用客户单据口径；仓库实物数量请从仓库核对。" placement="top"><el-button link aria-label="查看数量口径">数量口径 ⓘ</el-button></el-tooltip>
      </div>
      <div v-if="loadError" class="orders-load-error" role="alert">
        <div><strong>订单读取失败：{{ loadError }}</strong><p v-if="hasSuccessfulLoad">当前保留上次成功结果（{{ describeQuery(lastSuccessfulQuery) }}），不是本次查询结果。</p><p v-else>尚未取得订单结果，不能判断是否存在匹配订单。</p><p>失败查询：{{ describeQuery(failedQuery) }}</p></div>
        <el-button :loading="loading" @click="retryOrders">同页同筛选重试</el-button>
      </div>
      <div ref="tableHost" class="orders-table-host">
      <vxe-table class="orders-table" border stripe show-overflow="ellipsis" :height="tableHeight" :cell-config="{ height: 56 }" :loading="loading" :data="rows">
        <vxe-column field="customer_name" title="客户名称" width="170" fixed="left" />
        <vxe-column field="customer_po" title="客户单号" width="200">
          <template #default="{ row }"><span :class="row.customer_po ? 'customer-po' : 'customer-po-missing'">{{ row.customer_po || '未填写客户单号' }}</span><small class="erp-order-number">ERP {{ row.order_number }}</small></template>
        </vxe-column>
        <vxe-column field="order_date" title="下单日期" width="120" />
        <vxe-column field="delivery_date" title="交期" width="120" />
        <vxe-column field="total_quantity" title="总数量" width="105" />
        <vxe-column v-if="canViewSalesAmounts" field="total_amount" title="总金额" width="110" />
        <vxe-column field="business_status_label" title="业务状态" width="140" />
        <vxe-column field="business_remaining_quantity" title="待交数量" width="105" />
        <vxe-column field="item_count" title="明细行数" width="90" />
        <vxe-column title="操作" width="100" fixed="right">
          <template #default="{ row }"><el-button link type="primary" :loading="detailLoading" @click="openDetail(row.id)">详情/编辑</el-button></template>
        </vxe-column>
        <template #empty><span v-if="loading">正在读取订单…</span><span v-else-if="loadError">{{ hasSuccessfulLoad ? '上次成功结果无订单；本次查询失败，请重试' : '订单读取失败，尚无可显示结果，请重试' }}</span><span v-else-if="hasSuccessfulLoad">未找到符合当前筛选条件的订单，请调整关键词或状态后查询</span><span v-else>订单尚未读取</span></template>
      </vxe-table>
      </div>
      <div ref="paginationHost" class="orders-pagination"><el-pagination v-model:current-page="page" :page-size="pageSize" :total="total" :disabled="loading || Boolean(loadError)" layout="prev, pager, next, jumper, total" @current-change="changePage" /></div>
    </section>

    <el-dialog v-model="detailVisible" :title="`订单 ${detail?.order_number || ''}`" width="1000px" destroy-on-close>
      <template v-if="detail">
        <div class="tm-muted">{{ detail.customer_name }}　｜　{{ detail.business_status_label || detail.status }}　｜　客户单据数量与仓库实际数量按各自业务口径核对</div>
        <el-form v-if="editing" :model="editForm" label-width="95px" class="order-edit">
          <el-form-item label="客户PO"><el-input v-model="editForm.customer_po" /></el-form-item>
          <el-form-item label="交期"><el-date-picker v-model="editForm.delivery_date" type="date" value-format="YYYY-MM-DD" style="width:100%" /></el-form-item>
          <el-form-item label="整单备注"><el-input v-model="editForm.remark" /></el-form-item>
        </el-form>
        <div v-else class="order-meta">客户PO：{{ detail.customer_po || '—' }}　交期：{{ detail.delivery_date || '—' }}　整单备注：{{ detail.remark || '—' }}</div>
        <vxe-table border stripe show-overflow="ellipsis" max-height="360" :data="detail.items">
          <vxe-column field="snapshot_product_code" title="存货编码" width="150" />
          <vxe-column field="snapshot_product_name" title="品名" min-width="160" />
          <vxe-column field="snapshot_spec" title="规格" width="150" />
          <vxe-column field="snapshot_material" title="材质" width="110" />
          <vxe-column field="quantity" title="订单数" width="95" />
          <vxe-column field="delivered_quantity" title="已交数" width="95" />
          <vxe-column field="business_remaining_quantity" title="待交数" width="95" />
          <vxe-column field="snapshot_production_notes" title="行生产说明" min-width="145" />
          <vxe-column title="图纸" width="105" fixed="right"><template #default="{ row }"><el-button v-if="row.drawing_file || row.product_drawing_file" link @click="drawingPreviewSource = row.drawing_file || row.product_drawing_file">查看图纸</el-button><span v-else>—</span></template></vxe-column>
        </vxe-table>
        <div class="tm-muted" style="margin-top: 8px">本预览仅支持修改客户PO、交期、整单备注；明细数量和状态须沿用现版受控流程，不在此处伪装可编辑。</div>
      </template>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
        <el-button v-if="!editing && auth.hasPermission('orders.edit')" @click="editing = true">编辑基础信息</el-button>
        <el-button v-if="editing" @click="editing = false">取消编辑</el-button>
        <el-button v-if="editing" type="primary" :loading="saving" @click="saveBasicInfo">保存并回读</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.orders-workspace, .orders-table-host { min-width: 0; }
.orders-list-card.tm-card.tm-section { padding: 12px 14px; min-width: 0; }
.orders-list-card.tm-card.tm-section > .tm-toolbar { padding-bottom: 8px; gap: 8px; flex-wrap: wrap; }
.orders-table :deep(.vxe-body--column) { padding-top: 4px; padding-bottom: 4px; }
.orders-table :deep(.vxe-header--column) { padding-top: 6px; padding-bottom: 6px; }
.orders-pagination { margin-top: 12px; }
.orders-load-error { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; padding: 10px 12px; border: 1px solid var(--tm-danger); border-radius: 6px; color: var(--tm-danger); }
.orders-load-error p { margin: 4px 0 0; color: var(--tm-text-dim); }
.order-meta { margin: 12px 0; }
.customer-po { display:inline-block; padding:2px 6px; line-height:20px; border-radius:4px; background:#dcfce7; color:#166534; font-weight:600; }
.customer-po-missing { color:var(--tm-text-dim); }
.erp-order-number { display:block; color:var(--tm-text-dim); font-size:11px; line-height:16px; }
.order-edit { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 16px; margin-top: 12px; }
</style>
