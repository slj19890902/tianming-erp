<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  deliveryApi,
  financeApi,
  masterApi,
  type Customer,
  type DeliveryNote,
  type DeliveryPendingItem,
  type ReturnReceipt,
} from '../api/client'
import { useAuthStore } from '../stores/auth'

type DeliveryTab = 'pending' | 'deliveries' | 'receipts'

const authStore = useAuthStore()
const activeTab = ref<DeliveryTab>('pending')
const loading = ref(false)
const keyword = ref('')

// 待送货（主键为订单明细 id：item_id）
const pendingItems = ref<DeliveryPendingItem[]>([])
const selectedPendingIds = ref<number[]>([])
const customers = ref<Customer[]>([])
const createForm = ref({
  customer_id: null as number | null,
  delivery_date: new Date().toISOString().slice(0, 10),
  vehicle_number: '',
})

/** 每行本次送货数量，默认=剩余数 */
const deliveryQtys = ref<Record<number, string>>({})

// 送货单管理
const deliveries = ref<DeliveryNote[]>([])
const deliveriesTotal = ref(0)

// 回单：以送货单的 return_receipt_id 为入口（后端无回单列表/确认/作废接口）
const receipts = computed(() =>
  deliveries.value.filter((delivery) => delivery.status !== 'cancelled'),
)
const receiptDialog = ref(false)
const receiptEditing = ref<ReturnReceipt | null>(null)
const receiptForm = ref({
  delivery_id: 0,
  delivery_number: '',
  actual_received_date: new Date().toISOString().slice(0, 10),
  signed_by: '',
  lines: [] as Array<{ delivery_item_id: number; product_name: string; delivered_quantity: number; actual_received_quantity: string; difference_reason: string }>,
})
const receiptSaving = ref(false)
const canFinance = computed(() => ['admin', 'finance'].includes(authStore.user?.role || ''))

async function searchCustomers(term: string) {
  try {
    const result = await masterApi.listCustomers(term, { pageSize: 20 })
    customers.value = result.items
  } catch {
    customers.value = []
  }
}

async function loadPending() {
  loading.value = true
  try {
    const result = await deliveryApi.listPendingItems()
    pendingItems.value = result.items
    selectedPendingIds.value = []
    deliveryQtys.value = {}
    for (const item of result.items) {
      deliveryQtys.value[item.item_id] = String(item.remaining_quantity)
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '待送货加载失败')
  } finally {
    loading.value = false
  }
}

const filteredPending = computed(() => {
  const value = keyword.value.trim()
  if (!value) return pendingItems.value
  return pendingItems.value.filter((item) =>
    [item.customer_name, item.order_number, item.product_name]
      .filter(Boolean)
      .some((field) => String(field).includes(value)),
  )
})

function togglePendingSelect(itemId: number) {
  const index = selectedPendingIds.value.indexOf(itemId)
  if (index >= 0) selectedPendingIds.value.splice(index, 1)
  else selectedPendingIds.value.push(itemId)
}

/** 生成送货单：items[].order_item_id + delivered_quantity 必填 */
async function createDelivery() {
  if (!createForm.value.customer_id) {
    ElMessage.warning('请先选择客户')
    return
  }
  if (selectedPendingIds.value.length === 0) {
    ElMessage.warning('请先勾选要装车的待送货行')
    return
  }
  const lines = []
  for (const itemId of selectedPendingIds.value) {
    const qty = Number(deliveryQtys.value[itemId])
    if (!qty || qty <= 0) {
      ElMessage.warning('每行本次送货数量必须大于 0')
      return
    }
    lines.push({ order_item_id: itemId, delivered_quantity: qty })
  }
  try {
    await ElMessageBox.confirm(
      `确认将 ${lines.length} 行生成送货单？`,
      '生成送货单',
      { type: 'warning', confirmButtonText: '确认生成', cancelButtonText: '取消' },
    )
    const delivery = await deliveryApi.createDelivery({
      customer_id: createForm.value.customer_id,
      delivery_date: createForm.value.delivery_date,
      vehicle_number: createForm.value.vehicle_number || undefined,
      items: lines,
    })
    ElMessage.success(`送货单已生成（${delivery.delivery_number}）`)
    selectedPendingIds.value = []
    await loadPending()
    await loadDeliveries()
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof Error ? error.message : '生成送货单失败')
  }
}

async function loadDeliveries() {
  try {
    const result = await deliveryApi.listDeliveries(1, 50)
    deliveries.value = result.items
    deliveriesTotal.value = result.total
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '送货单加载失败')
  }
}

async function dispatchDelivery(id: number) {
  try {
    await deliveryApi.dispatch(id)
    ElMessage.success('已标记发车')
    await loadDeliveries()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '发车失败')
  }
}

async function printDelivery(id: number) {
  try {
    await deliveryApi.markPrinted(id)
    ElMessage.success('已标记打印')
    await loadDeliveries()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '标记打印失败')
  }
}

function deliveryStatusLabel(status: string) {
  return { draft: '草稿', dispatched: '已发车', delivered: '已送达', cancelled: '已取消' }[status] || status
}

function deliveryStatusType(status: string) {
  return { draft: 'info', dispatched: 'warning', delivered: 'success', cancelled: 'danger' }[status] || 'info'
}

/** 录入回单：打开对话框，明细默认实收=送货数 */
async function openReceiptDialog(delivery: DeliveryNote) {
  if (!canFinance.value) {
    ElMessage.warning('回单录入需要 admin 或 finance 角色')
    return
  }
  receiptSaving.value = true
  try {
    const detail = await deliveryApi.getDelivery(delivery.id)
    let existing: ReturnReceipt | null = null
    if (detail.return_receipt_id) {
      existing = await financeApi.getReceipt(detail.return_receipt_id)
    }
    receiptEditing.value = existing
    const existingMap = new Map((existing?.items || []).map((line) => [line.delivery_item_id, line]))
    receiptForm.value = {
      delivery_id: delivery.id,
      delivery_number: delivery.delivery_number,
      actual_received_date: existing?.actual_received_date || new Date().toISOString().slice(0, 10),
      signed_by: existing?.signed_by || '',
      lines: detail.items.map((item) => {
        const prev = existingMap.get(item.id)
        return {
          delivery_item_id: item.id,
          product_name: item.product_name || '',
          delivered_quantity: item.delivered_quantity,
          actual_received_quantity: String(prev?.actual_received_quantity ?? item.delivered_quantity),
          difference_reason: prev?.difference_reason || '',
        }
      }),
    }
    receiptDialog.value = true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '回单数据加载失败')
  } finally {
    receiptSaving.value = false
  }
}

/** 保存回单：有则更新，无则创建（后端状态仅 confirmed/cancelled，无确认环节） */
async function saveReceipt() {
  const lines = []
  for (const line of receiptForm.value.lines) {
    const qty = Number(line.actual_received_quantity)
    if (Number.isNaN(qty) || qty < 0) {
      ElMessage.warning(`“${line.product_name}”的实收数量无效`)
      return
    }
    lines.push({
      delivery_item_id: line.delivery_item_id,
      actual_received_quantity: qty,
      difference_reason: line.difference_reason || undefined,
    })
  }
  if (lines.length === 0) {
    ElMessage.warning('回单至少需要一条明细')
    return
  }
  receiptSaving.value = true
  try {
    if (receiptEditing.value) {
      await financeApi.updateReceipt(receiptEditing.value.id, {
        actual_received_date: receiptForm.value.actual_received_date,
        signed_by: receiptForm.value.signed_by || undefined,
        items: lines,
      })
      ElMessage.success('回单已更新')
    } else {
      await financeApi.createReceipt({
        delivery_id: receiptForm.value.delivery_id,
        actual_received_date: receiptForm.value.actual_received_date,
        signed_by: receiptForm.value.signed_by || undefined,
        items: lines,
      })
      ElMessage.success('回单已录入')
    }
    receiptDialog.value = false
    await loadDeliveries()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存回单失败')
  } finally {
    receiptSaving.value = false
  }
}

function receiptStatusLabel(status?: string | null) {
  if (!status) return '未录入'
  return { confirmed: '已确认', cancelled: '已作废' }[status] || status
}

function receiptStatusType(status?: string | null) {
  if (!status) return 'info'
  return { confirmed: 'success', cancelled: 'danger' }[status] || 'info'
}

onMounted(async () => {
  await searchCustomers('')
  await loadPending()
  await loadDeliveries()
})
</script>

<template>
  <div class="tm-page">
    <el-tabs v-model="activeTab" class="tm-card delivery-tabs">
      <el-tab-pane label="待送货" name="pending">
        <div class="tm-toolbar">
          <div class="tm-muted">从订单明细勾选本次要装车的内容，填写车次信息生成送货单。</div>
          <div style="display: flex; gap: 10px">
            <el-input v-model="keyword" clearable placeholder="客户 / 订单号 / 产品关键字" style="width: 260px" />
            <el-button type="primary" @click="loadPending">刷新</el-button>
          </div>
        </div>

        <vxe-table border stripe show-overflow="ellipsis" height="380" :loading="loading" :data="filteredPending">
          <vxe-column width="52">
            <template #default="{ row }">
              <el-checkbox :model-value="selectedPendingIds.includes(row.item_id)" @change="togglePendingSelect(row.item_id)" />
            </template>
          </vxe-column>
          <vxe-column type="seq" width="60" title="序号" />
          <vxe-column field="order_number" title="订单号" width="160" />
          <vxe-column field="customer_name" title="客户" width="170" />
          <vxe-column field="product_name" title="产品" min-width="190" />
          <vxe-column field="specification" title="规格" width="150" />
          <vxe-column field="quantity" title="订单数" width="90" />
          <vxe-column field="delivered_quantity" title="已送" width="90" />
          <vxe-column field="remaining_quantity" title="剩余" width="90" />
          <vxe-column title="本次送货数" width="130">
            <template #default="{ row }">
              <el-input v-model="deliveryQtys[row.item_id]" size="small" />
            </template>
          </vxe-column>
          <vxe-column field="delivery_date" title="交期" width="120" />
        </vxe-table>

        <div class="delivery-form tm-section">
          <h3 class="tm-section-title">车次信息</h3>
          <el-form label-width="84px" class="delivery-form-grid">
            <el-form-item label="客户">
              <el-select
                v-model="createForm.customer_id"
                filterable
                remote
                reserve-keyword
                placeholder="输入客户名称搜索"
                :remote-method="searchCustomers"
                style="width: 100%"
              >
                <el-option v-for="item in customers" :key="item.id" :label="item.name" :value="item.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="送货日期">
              <el-date-picker v-model="createForm.delivery_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" />
            </el-form-item>
            <el-form-item label="车牌号"><el-input v-model="createForm.vehicle_number" placeholder="后端仅记录车牌，无司机字段" /></el-form-item>
          </el-form>
          <div style="margin-top: 12px">
            <el-button type="success" size="large" :disabled="selectedPendingIds.length === 0" @click="createDelivery">
              生成送货单（已选 {{ selectedPendingIds.length }} 行）
            </el-button>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="送货单管理" name="deliveries">
        <div class="tm-toolbar">
          <div class="tm-muted">送货单状态：草稿 → 已发车 → 已送达。共 {{ deliveriesTotal }} 单。</div>
          <el-button type="primary" @click="loadDeliveries">刷新</el-button>
        </div>
        <vxe-table border stripe show-overflow="ellipsis" height="480" :loading="loading" :data="deliveries">
          <vxe-column type="seq" width="60" title="序号" />
          <vxe-column field="delivery_number" title="送货单号" width="190" />
          <vxe-column title="状态" width="110">
            <template #default="{ row }">
              <el-tag :type="deliveryStatusType(row.status)">{{ deliveryStatusLabel(row.status) }}</el-tag>
            </template>
          </vxe-column>
          <vxe-column field="customer_name" title="客户" width="170" />
          <vxe-column field="delivery_date" title="送货日期" width="130" />
          <vxe-column field="vehicle_number" title="车牌" width="130" />
          <vxe-column field="total_quantity" title="总数量" width="100" />
          <vxe-column title="回单" width="110">
            <template #default="{ row }">
              <el-tag :type="receiptStatusType(row.return_receipt_status)">
                {{ receiptStatusLabel(row.return_receipt_status) }}
              </el-tag>
            </template>
          </vxe-column>
          <vxe-column title="操作" width="200" fixed="right">
            <template #default="{ row }">
              <el-button type="primary" link :disabled="row.status !== 'draft'" @click="dispatchDelivery(row.id)">
                发车
              </el-button>
              <el-button type="primary" link @click="printDelivery(row.id)">标记打印</el-button>
              <el-button type="success" link @click="openReceiptDialog(row)">回单</el-button>
            </template>
          </vxe-column>
        </vxe-table>
      </el-tab-pane>

      <el-tab-pane label="回单管理" name="receipts">
        <div class="tm-toolbar">
          <div class="tm-muted">
            后端无回单列表/确认/作废接口，回单以送货单为入口录入与更新；状态仅 confirmed/cancelled。
            <span v-if="!canFinance" class="tm-danger">（当前角色无回单录入权限，需要 admin 或 finance）</span>
          </div>
          <el-button type="primary" @click="loadDeliveries">刷新</el-button>
        </div>
        <vxe-table border stripe show-overflow="ellipsis" height="480" :loading="loading" :data="receipts">
          <vxe-column type="seq" width="60" title="序号" />
          <vxe-column field="delivery_number" title="送货单号" width="190" />
          <vxe-column field="customer_name" title="客户" width="170" />
          <vxe-column field="delivery_date" title="送货日期" width="130" />
          <vxe-column field="total_quantity" title="送货数" width="100" />
          <vxe-column title="回单状态" width="120">
            <template #default="{ row }">
              <el-tag :type="receiptStatusType(row.return_receipt_status)">
                {{ receiptStatusLabel(row.return_receipt_status) }}
              </el-tag>
            </template>
          </vxe-column>
          <vxe-column title="操作" width="150" fixed="right">
            <template #default="{ row }">
              <el-button type="success" link :disabled="!canFinance" @click="openReceiptDialog(row)">
                {{ row.return_receipt_id ? '查看/更新' : '录入回单' }}
              </el-button>
            </template>
          </vxe-column>
        </vxe-table>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="receiptDialog" :title="`${receiptEditing ? '更新' : '录入'}回单 · ${receiptForm.delivery_number}`" width="860px">
      <el-form label-width="100px" class="receipt-form">
        <el-form-item label="实际签收日期">
          <el-date-picker v-model="receiptForm.actual_received_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" />
        </el-form-item>
        <el-form-item label="签收人"><el-input v-model="receiptForm.signed_by" /></el-form-item>
      </el-form>
      <vxe-table border stripe show-overflow="ellipsis" max-height="320" :data="receiptForm.lines">
        <vxe-column type="seq" width="56" title="序号" />
        <vxe-column field="product_name" title="产品" min-width="200" />
        <vxe-column field="delivered_quantity" title="送货数" width="100" />
        <vxe-column field="actual_received_quantity" title="实收数" width="140">
          <template #default="{ row }"><el-input v-model="row.actual_received_quantity" size="small" /></template>
        </vxe-column>
        <vxe-column field="difference_reason" title="差异原因" min-width="160">
          <template #default="{ row }"><el-input v-model="row.difference_reason" size="small" /></template>
        </vxe-column>
      </vxe-table>
      <template #footer>
        <el-button @click="receiptDialog = false">取消</el-button>
        <el-button type="primary" :loading="receiptSaving" @click="saveReceipt">保存回单</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.delivery-tabs {
  padding: 16px;
}

.delivery-form {
  margin-top: 14px;
  padding: 16px;
  border: 1px solid var(--tm-line);
  border-radius: 10px;
  background: rgba(13, 21, 38, 0.6);
}

.delivery-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2px 18px;
}

.receipt-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2px 18px;
  margin-bottom: 12px;
}

.tm-danger {
  color: var(--tm-danger);
  font-weight: 700;
}
</style>
