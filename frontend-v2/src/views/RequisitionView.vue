<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import QrcodeVue from 'qrcode.vue'
import {
  requisitionApi,
  SPECIAL_PROCESSES,
  type RequisitionItem,
  type RequisitionPendingItem,
} from '../api/client'
import { useAuthStore } from '../stores/auth'

type ReqTab = 'pending' | 'records'

const authStore = useAuthStore()
const activeTab = ref<ReqTab>('pending')
const loading = ref(false)
const keyword = ref('')
const items = ref<RequisitionPendingItem[]>([])
const selectedIds = ref<number[]>([])
const submitting = ref(false)

/** 每行可编辑的报料参数：纸长/纸宽必填（后端 gt=0），默认带入建议值 */
const rowParams = ref<Record<number, { requisition_qty: string; cardboard_len: string; cardboard_width: string; special_process: string }>>({})

const records = ref<RequisitionItem[]>([])
const recordsLoading = ref(false)
const recordStatusFilter = ref('')
const cancelReasonDialog = ref(false)
const cancellingItem = ref<RequisitionItem | null>(null)
const cancelReason = ref('')

const isAdmin = computed(() => authStore.user?.role === 'admin')

async function loadPending() {
  loading.value = true
  try {
    const result = await requisitionApi.listPending()
    items.value = result.items
    selectedIds.value = []
    rowParams.value = {}
    for (const item of result.items) {
      rowParams.value[item.item_id] = {
        requisition_qty: String(item.requisition_qty ?? item.quantity ?? ''),
        cardboard_len: String(item.suggested_cardboard_len ?? ''),
        cardboard_width: String(item.suggested_cardboard_width ?? ''),
        special_process: '无',
      }
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '待报料加载失败')
  } finally {
    loading.value = false
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
    [item.customer_name, item.order_number, item.product_name]
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
  selectedIds.value = checked ? filteredItems.value.map((item) => item.item_id) : []
}

const isAllSelected = computed(
  () => filteredItems.value.length > 0 && selectedIds.value.length === filteredItems.value.length,
)
const isIndeterminate = computed(
  () => selectedIds.value.length > 0 && selectedIds.value.length < filteredItems.value.length,
)

/** 二维码直达移动收料页；后端没有收料单据 URL，扫码后由移动端确认 */
function receivePageUrl() {
  return `${window.location.origin}/mobile-receive`
}

/**
 * 生成报料批次：后端 RequisitionLinePayload 要求
 * order_item_id 必填、cardboard_len/width 必填且 > 0、
 * special_process ∈ {无,大做小,双拼,多拼}
 */
async function submitBatch() {
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
    if (!len || len <= 0 || !width || width <= 0) {
      ElMessage.warning(`“${item?.product_name || item?.order_number}”的纸长/纸宽必须大于 0`)
      return
    }
    batchItems.push({
      order_item_id: itemId,
      requisition_qty: params.requisition_qty ? Number(params.requisition_qty) : undefined,
      cardboard_len: params.cardboard_len,
      cardboard_width: params.cardboard_width,
      special_process: params.special_process,
    })
  }
  try {
    await ElMessageBox.confirm(`确认将 ${batchItems.length} 行生成报料批次？`, '报料确认', {
      type: 'warning',
      confirmButtonText: '确认报料',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  submitting.value = true
  try {
    const batch = await requisitionApi.createBatch({ items: batchItems })
    ElMessage.success(`报料批次已生成（${batch.requisition_number}）`)
    await loadPending()
    await loadRecords()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '报料失败')
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
              :disabled="selectedIds.length === 0"
              :loading="submitting"
              @click="submitBatch"
            >
              报料（已选 {{ selectedIds.length }} 行）
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
          <vxe-column field="product_name" title="产品" min-width="170" />
          <vxe-column field="material" title="材质" width="130" />
          <vxe-column field="quantity" title="订单数" width="90" />
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
      <div class="tm-muted" style="margin-bottom: 12px">扫码直达移动收料页（/mobile-receive），纸板到厂后扫码确认。</div>
      <div class="qr-row">
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
