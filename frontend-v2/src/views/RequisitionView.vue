<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import QrcodeVue from 'qrcode.vue'
import {
  requisitionApi,
  SPECIAL_PROCESSES,
  type RequisitionItem,
  type RequisitionPendingItem,
  ApiError,
} from '../api/client'
import { useAuthStore } from '../stores/auth'

type ReqTab = 'pending' | 'records'

const authStore = useAuthStore()
const activeTab = ref<ReqTab>('pending')
const loading = ref(false)
const keyword = ref('')
const items = ref<RequisitionPendingItem[]>([])
const selectedIds = ref<number[]>([])
const selectedDialog = ref(false)
const submitting = ref(false)
const groupRequestKeys = new Map<string, string>()
let pendingRequestSerial = 0
let pendingDisposed = false

/** 每行可编辑的报料参数：纸长/纸宽必填（后端 gt=0），默认带入建议值 */
const rowParams = ref<Record<number, { requisition_qty: string; inventory_deducted_qty: string; cardboard_len: string; cardboard_width: string; special_process: string }>>({})

const records = ref<RequisitionItem[]>([])
const recordsLoading = ref(false)
const recordStatusFilter = ref('')
const cancelReasonDialog = ref(false)
const cancellingItem = ref<RequisitionItem | null>(null)
const cancelReason = ref('')

const isAdmin = computed(() => authStore.user?.role === 'admin')

async function loadPending() {
  if (pendingDisposed) return
  const serial = ++pendingRequestSerial
  loading.value = true
  try {
    const result = await requisitionApi.listPending()
    if (serial !== pendingRequestSerial) return
    items.value = result.items
    const activeIds = new Set(result.items.map((item) => item.item_id))
    selectedIds.value = selectedIds.value.filter((id) => activeIds.has(id))
    const priorParams = rowParams.value
    const nextParams: typeof rowParams.value = {}
    for (const item of result.items) {
      nextParams[item.item_id] = priorParams[item.item_id] || {
        requisition_qty: String(item.requisition_qty ?? item.quantity ?? ''),
        inventory_deducted_qty: String(item.inventory_deducted_qty ?? 0),
        cardboard_len: String(item.suggested_cardboard_len ?? ''),
        cardboard_width: String(item.suggested_cardboard_width ?? ''),
        special_process: SPECIAL_PROCESSES.includes(item.special_process as typeof SPECIAL_PROCESSES[number])
          ? String(item.special_process) : '无',
      }
    }
    rowParams.value = nextParams
  } catch (error) {
    if (serial !== pendingRequestSerial) return
    ElMessage.error(error instanceof Error ? error.message : '待报料加载失败')
  } finally {
    if (serial === pendingRequestSerial) loading.value = false
  }
}

async function loadRecords() {
  recordsLoading.value = true
  try {
    const result = await requisitionApi.listItems(recordStatusFilter.value)
    records.value = result.items
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '报料记录加载失败')
  } finally {
    recordsLoading.value = false
  }
}

const filteredItems = computed(() => {
  const value = keyword.value.trim()
  if (!value) return items.value
  return items.value.filter((item) =>
    [item.customer_name, item.order_number, item.product_code, item.product_name]
      .filter(Boolean)
      .some((field) => String(field).includes(value)),
  )
})

function toggleSelect(itemId: number) {
  const index = selectedIds.value.indexOf(itemId)
  if (index >= 0) selectedIds.value.splice(index, 1)
  else selectedIds.value.push(itemId)
}

function toggleSelectAll(checked: boolean) {
  const visible = new Set(filteredItems.value.map((item) => item.item_id))
  selectedIds.value = checked
    ? [...new Set([...selectedIds.value, ...visible])]
    : selectedIds.value.filter((id) => !visible.has(id))
}

const visibleSelectedCount = computed(() => {
  const selected = new Set(selectedIds.value)
  return filteredItems.value.filter((item) => selected.has(item.item_id)).length
})
const hiddenSelectedCount = computed(() => selectedIds.value.length - visibleSelectedCount.value)
const selectedItems = computed(() => items.value.filter((item) => selectedIds.value.includes(item.item_id)))
const isAllSelected = computed(
  () => filteredItems.value.length > 0 && visibleSelectedCount.value === filteredItems.value.length,
)
const isIndeterminate = computed(
  () => visibleSelectedCount.value > 0 && visibleSelectedCount.value < filteredItems.value.length,
)

/** 二维码直达移动收料页；后端没有收料单据 URL，扫码后由移动端确认 */
function receivePageUrl() {
  return `${window.location.origin}/mobile-receive`
}
const canShowReceiveQr = !['127.0.0.1', 'localhost'].includes(window.location.hostname)

/**
 * 生成报料批次：后端 RequisitionLinePayload 要求
 * order_item_id 必填、cardboard_len/width 必填且 > 0、
 * special_process ∈ {无,大做小,双拼,多拼}
 */
async function submitBatch() {
  if (submitting.value || !authStore.hasPermission('requisition.execute')) return
  if (selectedIds.value.length === 0) {
    ElMessage.warning('请先勾选要报料的行')
    return
  }
  const batchItems = []
  for (const itemId of selectedIds.value) {
    const params = rowParams.value[itemId]
    const item = items.value.find((entry) => entry.item_id === itemId)
    if (!params) continue
    const len = Number(params.cardboard_len)
    const width = Number(params.cardboard_width)
    const deducted = Number(params.inventory_deducted_qty)
    const required = Number(params.requisition_qty)
    if (!len || len <= 0 || !width || width <= 0) {
      ElMessage.warning(`“${item?.product_name || item?.order_number}”的纸长/纸宽必须大于 0`)
      return
    }
    if (!Number.isInteger(deducted) || deducted < 0 || deducted > Number(item?.quantity ?? 0)
      || !Number.isInteger(required) || required < 0) {
      ElMessage.warning('扣库存成品数和报料纸板张数须为有效非负整数，扣库存不能超过订单数')
      return
    }
    batchItems.push({
      order_item_id: itemId,
      supplier_name: item?.snapshot_supplier_name?.trim() || '',
      inventory_deducted_qty: deducted,
      requisition_qty: required,
      cardboard_len: params.cardboard_len,
      cardboard_width: params.cardboard_width,
      special_process: params.special_process,
    })
  }
  try {
    const supplierCount = new Set(batchItems.map((item) => item.supplier_name)).size
    await ElMessageBox.confirm(`确认将 ${batchItems.length} 行按 ${supplierCount} 个供应商分别生成报料批次？`, '报料确认', {
      type: 'warning',
      confirmButtonText: '确认报料',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  submitting.value = true
  const createdNumbers: string[] = []
  try {
    const groups = new Map<string, typeof batchItems>()
    for (const item of batchItems) groups.set(item.supplier_name, [...(groups.get(item.supplier_name) || []), item])
    for (const [supplier, group] of groups) {
      const key = groupRequestKeys.get(supplier) || crypto.randomUUID().replaceAll('-', '')
      groupRequestKeys.set(supplier, key)
      const batch = await requisitionApi.createBatch({
        request_key: key,
        supplier_name: supplier || undefined,
        items: group.map(({ supplier_name: _supplier, ...line }) => line),
      })
      createdNumbers.push(batch.requisition_number)
      groupRequestKeys.delete(supplier)
    }
    ElMessage.success(`已按供应商生成 ${createdNumbers.length} 个报料批次：${createdNumbers.join('、')}`)
    await loadPending()
    await loadRecords()
  } catch (error) {
    await loadPending()
    await loadRecords()
    if (error instanceof ApiError && error.status === 0) {
      ElMessage.warning(`已有 ${createdNumbers.length} 个批次明确生成；其余结果不确定，请先核对报料记录，重试会沿用原请求编号`)
      return
    }
    ElMessage.error(`${createdNumbers.length} 个批次已生成；其余失败：${error instanceof Error ? error.message : '报料失败'}`)
  } finally {
    submitting.value = false
  }
}

/** 取消报料：仅 admin，且仅对已报料明细有效（未报料会 409） */
function openCancel(item: RequisitionItem) {
  cancellingItem.value = item
  cancelReason.value = ''
  cancelReasonDialog.value = true
}

async function confirmCancel() {
  if (!cancellingItem.value) return
  if (!cancelReason.value.trim()) {
    ElMessage.warning('请填写取消原因')
    return
  }
  try {
    await requisitionApi.cancelItem(cancellingItem.value.item_id, cancelReason.value.trim())
    ElMessage.success('已取消该条报料')
    cancelReasonDialog.value = false
    await loadRecords()
    await loadPending()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '取消失败')
  }
}

function recordStatusLabel(status: string) {
  return status || '—'
}

onBeforeUnmount(() => {
  pendingDisposed = true
  pendingRequestSerial += 1
})

onMounted(async () => {
  await loadPending()
  await loadRecords()
})
</script>

<template>
  <div class="tm-page">
    <el-tabs v-model="activeTab" class="tm-card requisition-tabs">
      <el-tab-pane label="待报料" name="pending">
        <div class="tm-toolbar">
          <div class="tm-muted">勾选后填写纸长/纸宽（必填）生成采购批次；建议值来自产品资料。</div>
          <div style="display: flex; gap: 10px; align-items: center">
            <el-input v-model="keyword" clearable placeholder="客户 / 订单号 / 产品关键字" style="width: 260px" />
            <el-button type="primary" @click="loadPending">刷新</el-button>
            <el-button
              type="success"
              :disabled="selectedIds.length === 0 || !authStore.hasPermission('requisition.execute')"
              :loading="submitting"
              @click="submitBatch"
            >
              报料（已选 {{ selectedIds.length }} 行）
            </el-button>
            <el-button v-if="selectedIds.length" link @click="selectedDialog = true">
              查看已选 {{ selectedIds.length }} 行<span v-if="hiddenSelectedCount">（筛选外 {{ hiddenSelectedCount }} 行）</span>
            </el-button>
          </div>
        </div>

        <vxe-table border stripe show-overflow="ellipsis" height="520" :loading="loading" :data="filteredItems">
          <vxe-column width="52" fixed="left">
            <template #header>
              <el-checkbox :model-value="isAllSelected" :indeterminate="isIndeterminate" @change="toggleSelectAll" />
            </template>
            <template #default="{ row }">
              <el-checkbox :model-value="selectedIds.includes(row.item_id)" @change="toggleSelect(row.item_id)" />
            </template>
          </vxe-column>
          <vxe-column type="seq" width="56" title="序号" />
          <vxe-column field="order_number" title="订单号" width="160" />
          <vxe-column field="customer_name" title="客户" width="150" />
          <vxe-column field="snapshot_supplier_name" title="供应商" width="155" />
          <vxe-column field="product_name" title="产品" min-width="170" />
          <vxe-column field="material" title="材质" width="130" />
          <vxe-column field="quantity" title="订单数" width="90" />
          <vxe-column title="扣库存成品数" width="120"><template #default="{ row }"><el-input v-model="rowParams[row.item_id].inventory_deducted_qty" size="small" /></template></vxe-column>
          <vxe-column title="报料数" width="110">
            <template #default="{ row }">
              <el-input v-model="rowParams[row.item_id].requisition_qty" size="small" />
            </template>
          </vxe-column>
          <vxe-column title="纸长" width="110">
            <template #default="{ row }">
              <el-input v-model="rowParams[row.item_id].cardboard_len" size="small" />
            </template>
          </vxe-column>
          <vxe-column title="纸宽" width="110">
            <template #default="{ row }">
              <el-input v-model="rowParams[row.item_id].cardboard_width" size="small" />
            </template>
          </vxe-column>
          <vxe-column title="特殊处理" width="120">
            <template #default="{ row }">
              <el-select v-model="rowParams[row.item_id].special_process" size="small">
                <el-option v-for="process in SPECIAL_PROCESSES" :key="process" :label="process" :value="process" />
              </el-select>
            </template>
          </vxe-column>
          <vxe-column field="delivery_date" title="交期" width="120" />
        </vxe-table>
      </el-tab-pane>

      <el-tab-pane label="报料记录" name="records">
        <div class="tm-toolbar">
          <div class="tm-muted">已报料明细；取消报料仅管理员可用，且已入库明细禁止取消。</div>
          <div style="display: flex; gap: 10px">
            <el-select v-model="recordStatusFilter" clearable placeholder="状态筛选" style="width: 160px" @change="loadRecords">
              <el-option label="未报料" value="未报料" />
              <el-option label="已报料" value="已报料" />
              <el-option label="供应商已排单" value="供应商已排单" />
              <el-option label="已入库" value="已入库" />
            </el-select>
            <el-button type="primary" @click="loadRecords">刷新</el-button>
          </div>
        </div>
        <vxe-table border stripe show-overflow="ellipsis" height="520" :loading="recordsLoading" :data="records">
          <vxe-column type="seq" width="56" title="序号" />
          <vxe-column field="order_number" title="订单号" width="160" />
          <vxe-column field="customer_name" title="客户" width="150" />
          <vxe-column field="product_name" title="产品" min-width="170" />
          <vxe-column field="requisition_status" title="报料状态" width="120">
            <template #default="{ row }">{{ recordStatusLabel(row.requisition_status) }}</template>
          </vxe-column>
          <vxe-column field="material_status" title="物料状态" width="110" />
          <vxe-column field="requisition_qty" title="报料数" width="90" />
          <vxe-column title="纸板尺寸" width="140">
            <template #default="{ row }">
              <span v-if="row.cardboard_len && row.cardboard_width" class="tm-mono">
                {{ row.cardboard_len }}×{{ row.cardboard_width }}
              </span>
              <span v-else class="tm-muted">—</span>
            </template>
          </vxe-column>
          <vxe-column field="special_process" title="特殊处理" width="100" />
          <vxe-column field="requisition_date" title="报料日期" width="120" />
          <vxe-column title="操作" width="110" fixed="right">
            <template #default="{ row }">
              <el-button
                type="danger"
                link
                :disabled="!isAdmin || row.material_status === 'received'"
                @click="openCancel(row)"
              >
                取消
              </el-button>
            </template>
          </vxe-column>
        </vxe-table>
      </el-tab-pane>
    </el-tabs>

    <section class="tm-card tm-section">
      <h2 class="tm-section-title">收料二维码</h2>
      <div class="tm-muted" style="margin-bottom: 12px">这是通用收料入口，不是单个任务码。当前隔离预览仅限本机，回环地址不能供另一台手机访问。</div>
      <div v-if="canShowReceiveQr" class="qr-row">
        <div class="qr-card">
          <QrcodeVue :value="receivePageUrl()" :size="200" level="M" />
          <div class="qr-label">移动收料页</div>
          <div class="tm-mono tm-muted qr-url">{{ receivePageUrl() }}</div>
        </div>
      </div>
    </section>

    <el-dialog v-model="cancelReasonDialog" title="取消报料" width="440px">
      <p class="tm-muted" style="margin-bottom: 12px">
        取消“{{ cancellingItem?.product_name || cancellingItem?.order_number }}”？后端要求填写取消原因（仅管理员）。
      </p>
      <el-input v-model="cancelReason" type="textarea" :rows="3" placeholder="请输入取消原因" />
      <template #footer>
        <el-button @click="cancelReasonDialog = false">返回</el-button>
        <el-button type="danger" @click="confirmCancel">确认取消</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="selectedDialog" title="当前已选报料明细" width="650px">
      <div v-for="item in selectedItems" :key="item.item_id" class="tm-section">
        {{ item.customer_name }} · {{ item.order_number }} · {{ item.product_code || item.product_name }}
        <el-button type="danger" link @click="toggleSelect(item.item_id)">移除</el-button>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.requisition-tabs {
  padding: 16px;
}

.qr-row {
  display: flex;
  gap: 18px;
}

.qr-card {
  padding: 18px;
  border: 1px solid var(--tm-line-strong);
  border-radius: 12px;
  background: #f4f7fc;
  text-align: center;
}

.qr-label {
  margin-top: 10px;
  font-weight: 800;
  color: #123;
}

.qr-url {
  margin-top: 6px;
  font-size: 12px;
  color: #456;
  max-width: 220px;
  word-break: break-all;
}
</style>
