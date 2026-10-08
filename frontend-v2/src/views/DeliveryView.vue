<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  deliveryApi,
  financeApi,
  masterApi,
  type Customer,
  type DeliveryNote,
  type DeliveryPendingItem,
  type DeliveryPrintData,
  type ReturnReceipt,
  type UnorderedFinishedCandidate,
  ApiError,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import { businessDate } from '../utils/businessDate'
import { buildReadonlyInventoryLocatorHref } from '../utils/inventoryLocator'

type DeliveryTab = 'pending' | 'unordered' | 'deliveries' | 'receipts'

const authStore = useAuthStore()
const activeTab = ref<DeliveryTab>('pending')
const loading = ref(false)
const keyword = ref('')

// 待送货（主键为订单明细 id：item_id）
const pendingItems = ref<DeliveryPendingItem[]>([])
const selectedPendingIds = ref<number[]>([])
const selectedCustomerId = computed(() => pendingItems.value.find((item) => selectedPendingIds.value.includes(item.item_id))?.customer_id ?? null)
const customers = ref<Customer[]>([])
const chosenCustomer = ref<Customer | null>(null)
const customerOptions = computed(() => {
  const options = new Map<number, { id: number; name: string }>()
  if (chosenCustomer.value) options.set(chosenCustomer.value.id, chosenCustomer.value)
  for (const customer of customers.value) options.set(customer.id, customer)
  const pending = pendingItems.value.find((item) => item.customer_id === createForm.value.customer_id)
  if (pending) options.set(pending.customer_id, { id: pending.customer_id, name: pending.customer_name })
  return [...options.values()]
})
const createForm = ref({
  customer_id: null as number | null,
  delivery_date: businessDate(),
  vehicle_number: '',
})

/** 每行本次送货数量，默认=剩余数 */
const deliveryQtys = ref<Record<number, string>>({})
const deliveryCreating = ref(false)
const deliveryOutcomeUnknown = ref(false)
const unorderedCustomerId = ref<number | null>(null)
const unorderedChosenCustomer = ref<Customer | null>(null)
const unorderedKeyword = ref('')
const unorderedPage = ref(1)
const unorderedTotal = ref(0)
const unorderedCandidates = ref<UnorderedFinishedCandidate[]>([])
const unorderedSelectedLotId = ref<number | null>(null)
const unorderedCustomerQty = ref('')
const unorderedUnitPrice = ref('')
const unorderedLoading = ref(false)
const unorderedSaving = ref(false)
const unorderedOutcomeUnknown = ref(false)
let unorderedSerial = 0
const unorderedSelected = computed(() => unorderedCandidates.value.find((row) => row.inventory_lot_id === unorderedSelectedLotId.value))
const unorderedCustomerOptions = computed(() => {
  const options = new Map<number, Customer>()
  if (unorderedChosenCustomer.value) options.set(unorderedChosenCustomer.value.id, unorderedChosenCustomer.value)
  for (const customer of customers.value) options.set(customer.id, customer)
  return [...options.values()]
})
const unorderedPhysicalQty = computed(() => {
  const basis = unorderedSelected.value?.quantity_contract
  const customerQty = Number(unorderedCustomerQty.value)
  if (!basis || !Number.isInteger(customerQty) || customerQty <= 0) return null
  const physical = customerQty * Number(basis.physical_basis) / Number(basis.customer_basis)
  return Number.isInteger(physical) && physical > 0 ? physical : null
})

// 送货单管理
const deliveries = ref<DeliveryNote[]>([])
const deliveriesTotal = ref(0)
const deliveriesPage = ref(1)
const deliverySearch = ref('')
const deliveryPageSize = 20
const printDialog = ref(false)
const printData = ref<DeliveryPrintData | null>(null)
const printSaving = ref(false)
const printRequestKey = ref('')

// 回单：以送货单的 return_receipt_id 为入口（后端无回单列表/确认/作废接口）
const receipts = computed(() =>
  deliveries.value.filter((delivery) => delivery.status !== 'cancelled'),
)
const receiptDialog = ref(false)
const receiptEditing = ref<ReturnReceipt | null>(null)
const receiptForm = ref({
  delivery_id: 0,
  delivery_number: '',
  actual_received_date: businessDate(),
  signed_by: '',
  lines: [] as Array<{ delivery_item_id: number; product_name: string; delivered_quantity: number; actual_received_quantity: string; difference_reason: string }>,
})
const receiptSaving = ref(false)
const canFinance = computed(() => authStore.hasPermission('finance.execute') && authStore.hasPermission('finance.return_receipt.period.adjust'))

function unorderedLocatorHref(row: UnorderedFinishedCandidate) {
  if (!authStore.hasPermission('warehouse.view') || typeof row.location_name !== 'string' || !row.location_name.trim()) return undefined
  return buildReadonlyInventoryLocatorHref(row.inventory_lot_id, 'finished')
}

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
    const available = new Set(result.items.map((item) => item.item_id))
    selectedPendingIds.value = selectedPendingIds.value.filter((id) => available.has(id))
    for (const item of result.items) {
      if (deliveryQtys.value[item.item_id] === undefined) deliveryQtys.value[item.item_id] = String(item.remaining_quantity)
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
    [item.customer_name, item.order_number, item.customer_po, item.product_code, item.product_name]
      .filter(Boolean)
      .some((field) => String(field).includes(value)),
  )
})

function togglePendingSelect(itemId: number) {
  const index = selectedPendingIds.value.indexOf(itemId)
  if (index >= 0) selectedPendingIds.value.splice(index, 1)
  else {
    const item = pendingItems.value.find((row) => row.item_id === itemId)
    if (!item) return
    if (selectedCustomerId.value && item.customer_id !== selectedCustomerId.value) {
      ElMessage.warning('一张送货单只能选择同一客户的货，请先取消其他客户的选择')
      return
    }
    if (createForm.value.customer_id && item.customer_id !== createForm.value.customer_id) {
      ElMessage.warning('所选行与车次客户不一致')
      return
    }
    createForm.value.customer_id = item.customer_id
    selectedPendingIds.value.push(itemId)
  }
}

function onCustomerSelected(id: number) {
  chosenCustomer.value = customers.value.find((item) => item.id === id) || null
}

function onUnorderedCustomerSelected(id: number) {
  unorderedChosenCustomer.value = customers.value.find((item) => item.id === id) || null
  unorderedCandidates.value = []
  unorderedSelectedLotId.value = null
  unorderedPage.value = 1
  unorderedSerial++
  unorderedLoading.value = false
}

async function loadUnorderedCandidates() {
  if (!unorderedCustomerId.value) return
  const customerId = unorderedCustomerId.value
  const serial = ++unorderedSerial
  unorderedLoading.value = true
  try {
    const result = await deliveryApi.listUnorderedCandidates(customerId, unorderedKeyword.value, unorderedPage.value)
    if (serial !== unorderedSerial || customerId !== unorderedCustomerId.value) return
    unorderedCandidates.value = result.items
    unorderedTotal.value = result.total
    if (!result.items.some((row) => row.inventory_lot_id === unorderedSelectedLotId.value)) unorderedSelectedLotId.value = null
  } catch (error) {
    if (serial === unorderedSerial) ElMessage.error(error instanceof Error ? error.message : '无订单库存查询失败')
  } finally {
    if (serial === unorderedSerial) unorderedLoading.value = false
  }
}

function searchUnordered() {
  unorderedPage.value = 1
  void loadUnorderedCandidates()
}

function selectUnordered(row: UnorderedFinishedCandidate) {
  unorderedSelectedLotId.value = row.inventory_lot_id
  unorderedCustomerQty.value = ''
  unorderedUnitPrice.value = row.unit_price || ''
}

async function createUnordered() {
  const item = unorderedSelected.value
  if (!item || !unorderedCustomerId.value || unorderedSaving.value || unorderedOutcomeUnknown.value || !authStore.hasPermission('deliveries.execute')) return
  const customerQty = Number(unorderedCustomerQty.value)
  const physicalQty = unorderedPhysicalQty.value
  if (item.customer_id !== unorderedCustomerId.value || item.quantity_issue || Number(item.order_pending_quantity || 0) > 0 || !physicalQty
    || customerQty > item.available_customer_quantity || physicalQty > item.available_quantity) {
    ElMessage.warning('客户、数量或换算不匹配，请重新选择库存批次')
    return
  }
  if (!unorderedUnitPrice.value.trim() || !Number.isFinite(Number(unorderedUnitPrice.value)) || Number(unorderedUnitPrice.value) < 0) {
    ElMessage.warning('请填写有效的客户销售单价')
    return
  }
  unorderedSaving.value = true
  try {
    const delivery = await deliveryApi.createUnorderedDelivery({
      customer_id: unorderedCustomerId.value,
      delivery_date: createForm.value.delivery_date,
      vehicle_number: createForm.value.vehicle_number || undefined,
      source_mode: 'unordered_finished',
      items: [{
        source_type: 'unordered_finished', product_id: item.product_id,
        delivered_quantity: customerQty, unit_price: unorderedUnitPrice.value,
        allocations: [{ inventory_lot_id: item.inventory_lot_id, quantity: physicalQty }],
      }],
    })
    ElMessage.success(`无订单送货单已生成（${delivery.delivery_number}）`)
    unorderedSelectedLotId.value = null
    await loadUnorderedCandidates()
    await loadDeliveries()
  } catch (error) {
    if (error instanceof ApiError && error.status === 0) {
      unorderedOutcomeUnknown.value = true
      await loadDeliveries()
      await loadUnorderedCandidates()
      ElMessage.warning('结果暂不确定；请核对送货单和该库存批次，暂不可直接重试')
      return
    }
    ElMessage.error(error instanceof Error ? error.message : '无订单送货失败')
  } finally {
    unorderedSaving.value = false
  }
}

async function unlockUnorderedRetry() {
  try {
    await ElMessageBox.confirm('已核对送货单和原库存批次，确认本次无订单送货没有生成？解锁后重试可能产生重复单。', '核对不确定结果', {
      type: 'warning', confirmButtonText: '确认未生成，解锁', cancelButtonText: '继续核对',
    })
    unorderedOutcomeUnknown.value = false
  } catch { /* 保持锁定 */ }
}

watch(() => createForm.value.customer_id, (customerId) => {
  if (selectedPendingIds.value.length && customerId !== selectedCustomerId.value) {
    selectedPendingIds.value = []
    ElMessage.info('已清除原客户的待送货选择')
  }
})

/** 生成送货单：items[].order_item_id + delivered_quantity 必填 */
async function createDelivery() {
  if (deliveryCreating.value || deliveryOutcomeUnknown.value || !authStore.hasPermission('deliveries.execute')) return
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
    const item = pendingItems.value.find((row) => row.item_id === itemId)
    if (!item || item.customer_id !== createForm.value.customer_id) {
      ElMessage.warning('待送货行与当前客户不一致，请刷新并重新选择')
      return
    }
    const qty = Number(deliveryQtys.value[itemId])
    if (!Number.isFinite(qty) || qty <= 0 || qty > item.remaining_quantity) {
      ElMessage.warning('每行本次送货数量须大于 0 且不超过剩余数')
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
    deliveryCreating.value = true
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
    if (error instanceof ApiError && error.status === 0) {
      deliveryOutcomeUnknown.value = true
      await loadDeliveries()
      await loadPending()
      ElMessage.warning('送货结果暂不确定，已刷新列表；普通送货接口不接受幂等键，请核对是否已有该单，暂不可直接重试')
      return
    }
    ElMessage.error(error instanceof Error ? error.message : '生成送货单失败')
  } finally {
    deliveryCreating.value = false
  }
}

async function loadDeliveries() {
  try {
    const result = await deliveryApi.listDeliveries(deliveriesPage.value, deliveryPageSize, deliverySearch.value)
    deliveries.value = result.items
    deliveriesTotal.value = result.total
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '送货单加载失败')
  }
}

async function dispatchDelivery(id: number) {
  if (!authStore.hasPermission('deliveries.execute')) return
  try {
    await deliveryApi.dispatch(id)
    ElMessage.success('已标记发车')
    await loadDeliveries()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '发车失败')
  }
}

async function openPrintPreview(id: number) {
  try {
    printData.value = await deliveryApi.getDeliveryPrint(id)
    printRequestKey.value = crypto.randomUUID()
    printDialog.value = true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '送货单打印数据加载失败')
  }
}

async function unlockDeliveryRetry() {
  try {
    await ElMessageBox.confirm('已核对送货单列表与明细，确认没有生成本次车次？解锁后重新提交可能产生重复单。', '核对不确定结果', {
      type: 'warning', confirmButtonText: '确认未生成，解锁', cancelButtonText: '继续核对',
    })
    deliveryOutcomeUnknown.value = false
  } catch { /* 保持锁定 */ }
}

function searchDeliveries() {
  deliveriesPage.value = 1
  void loadDeliveries()
}

async function confirmPrint() {
  if (!printData.value || printSaving.value) return
  printSaving.value = true
  try {
    if (printData.value.document_hash) {
      await deliveryApi.recordCustomerPrintRequest(printData.value.id, {
        idempotency_key: printRequestKey.value,
        document_hash: printData.value.document_hash,
        show_prices: printData.value.price_display?.shown || false,
        order_context: printData.value.order_context || '',
      })
      printRequestKey.value = crypto.randomUUID()
    }
    window.print()
    ElMessage.info('已发起打印请求；请以纸质单据核对实际完成情况')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '打印请求失败')
  } finally {
    printSaving.value = false
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
    ElMessage.warning('当前账号没有回单录入权限')
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
      actual_received_date: existing?.actual_received_date || businessDate(),
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
          <div>
            <span class="tm-muted">从订单明细勾选本次要装车的内容，填写车次信息生成送货单。</span>
            <a v-if="authStore.hasPermission('deliveries.execute')" href="/static/index.html?page=deliveries" target="_blank" rel="noopener noreferrer" title="在新标签打开完整送货工作区，再点导入预送货" style="margin-left:10px">预送货（完整工作区） ↗</a>
          </div>
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
                @change="onCustomerSelected"
                style="width: 100%"
              >
                <el-option v-for="item in customerOptions" :key="item.id" :label="item.name" :value="item.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="送货日期">
              <el-date-picker v-model="createForm.delivery_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" />
            </el-form-item>
            <el-form-item label="车牌号"><el-input v-model="createForm.vehicle_number" placeholder="后端仅记录车牌，无司机字段" /></el-form-item>
          </el-form>
          <div style="margin-top: 12px">
            <el-button type="success" size="large" :disabled="selectedPendingIds.length === 0 || deliveryOutcomeUnknown || !authStore.hasPermission('deliveries.execute')" :loading="deliveryCreating" @click="createDelivery">
              生成送货单（已选 {{ selectedPendingIds.length }} 行）
            </el-button>
            <el-button v-if="deliveryOutcomeUnknown" type="warning" @click="unlockDeliveryRetry">已核对未生成，解锁重试</el-button>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="无订单库存送货" name="unordered">
        <div class="tm-toolbar tm-muted">沿用现有“无订单成品库存直接送货”规则，不补造订单；只列当前客户的可用、未预占库存批次。</div>
        <div class="tm-toolbar">
          <el-select v-model="unorderedCustomerId" filterable remote reserve-keyword placeholder="选择客户" :remote-method="searchCustomers" style="width:230px" @change="onUnorderedCustomerSelected">
            <el-option v-for="customer in unorderedCustomerOptions" :key="customer.id" :label="customer.name" :value="customer.id" />
          </el-select>
          <el-input v-model="unorderedKeyword" clearable placeholder="存货编码 / 品名 / 规格" style="width:250px" @keyup.enter="searchUnordered" @clear="searchUnordered" />
          <el-button :loading="unorderedLoading" @click="searchUnordered">查库存</el-button>
        </div>
        <vxe-table border stripe show-overflow="ellipsis" height="300" :loading="unorderedLoading" :data="unorderedCandidates">
          <vxe-column field="product_code" title="存货编码" width="150" />
          <vxe-column field="product_name" title="品名" min-width="160" />
          <vxe-column field="specification" title="规格" width="150" />
          <vxe-column field="inventory_lot_number" title="批次" width="160" />
          <vxe-column field="location_name" title="库位" width="145">
            <template #default="{ row }">
              {{ row.location_name || '—' }}
              <a v-if="unorderedLocatorHref(row)" :href="unorderedLocatorHref(row)" target="_blank" rel="noopener noreferrer" title="在新标签查看该批次的正式库存位置" style="margin-left:8px">定位 ↗</a>
            </template>
          </vxe-column>
          <vxe-column title="客户可送数" width="115"><template #default="{ row }">{{ row.available_customer_quantity }} {{ row.quantity_contract?.customer_unit || row.unit }}</template></vxe-column>
          <vxe-column title="仓库实数" width="115"><template #default="{ row }">{{ row.available_quantity }} {{ row.quantity_contract?.physical_unit || row.unit }}</template></vxe-column>
          <vxe-column field="order_pending_quantity" title="同编码订单待送" width="120" />
          <vxe-column title="操作" width="85" fixed="right"><template #default="{ row }"><el-button type="primary" link :disabled="!!row.quantity_issue || Number(row.order_pending_quantity || 0) > 0" @click="selectUnordered(row)">选择</el-button></template></vxe-column>
        </vxe-table>
        <el-pagination v-model:current-page="unorderedPage" :page-size="12" :total="unorderedTotal" layout="prev, pager, next, total" style="margin-top:10px" @current-change="loadUnorderedCandidates" />
        <div v-if="unorderedSelected" class="tm-card tm-section" style="margin-top:14px">
          <h3 class="tm-section-title">本次送货：{{ unorderedSelected.product_code }} · {{ unorderedSelected.product_name }}</h3>
          <div class="tm-muted">
            库位 {{ unorderedSelected.location_name || '—' }}
            <a v-if="unorderedLocatorHref(unorderedSelected)" :href="unorderedLocatorHref(unorderedSelected)" target="_blank" rel="noopener noreferrer" title="在新标签查看该批次的正式库存位置" style="margin-left:8px">定位 ↗</a>
            ；客户数量和仓库实数按产品换算契约核对。
          </div>
          <div class="tm-toolbar">
            <el-input v-model="unorderedCustomerQty" placeholder="客户送货数量" style="width:150px" />
            <span>{{ unorderedSelected.quantity_contract?.customer_unit || unorderedSelected.unit }}</span>
            <span>对应仓库实拿：{{ unorderedPhysicalQty ?? '待输入有效整数' }} {{ unorderedSelected.quantity_contract?.physical_unit || unorderedSelected.unit }}</span>
            <el-input v-model="unorderedUnitPrice" placeholder="客户销售单价" style="width:150px" />
            <el-button type="success" :loading="unorderedSaving" :disabled="unorderedOutcomeUnknown || !authStore.hasPermission('deliveries.execute')" @click="createUnordered">生成无订单送货单</el-button>
          </div>
        </div>
        <el-button v-if="unorderedOutcomeUnknown" type="warning" style="margin-top:12px" @click="unlockUnorderedRetry">已核对未生成，解锁重试</el-button>
      </el-tab-pane>

      <el-tab-pane label="送货单管理" name="deliveries">
        <div class="tm-toolbar">
          <div class="tm-muted">送货单状态：草稿 → 已发车 → 已送达。共 {{ deliveriesTotal }} 单。</div>
          <el-input v-model="deliverySearch" clearable placeholder="客户 / 存货编码 / 客户PO / 单号" style="width: 285px" @keyup.enter="searchDeliveries" @clear="searchDeliveries" />
          <el-button @click="searchDeliveries">查询</el-button>
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
              <el-button type="primary" link :disabled="row.status !== 'draft' || !authStore.hasPermission('deliveries.execute')" @click="dispatchDelivery(row.id)">
                发车
              </el-button>
              <el-button type="primary" link @click="openPrintPreview(row.id)">打印预览</el-button>
              <el-button type="success" link @click="openReceiptDialog(row)">回单</el-button>
            </template>
          </vxe-column>
        </vxe-table>
        <el-pagination v-model:current-page="deliveriesPage" :page-size="deliveryPageSize" :total="deliveriesTotal" layout="prev, pager, next, jumper, total" style="margin-top:12px" @current-change="loadDeliveries" />
      </el-tab-pane>

      <el-tab-pane label="回单管理" name="receipts">
        <div class="tm-toolbar">
          <div class="tm-muted">
            后端无回单列表/确认/作废接口，回单以送货单为入口录入与更新；状态仅 confirmed/cancelled。
            <span v-if="!canFinance" class="tm-danger">（当前账号无回单录入权限）</span>
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

    <el-dialog v-model="printDialog" title="送货单打印预览" width="900px" class="delivery-print-dialog">
      <div v-if="printData" class="internal-print-review tm-muted">
        <div>内部核对（不打印）：客户数量与实际货物按各自单位逐行核对，不跨单位求和。</div>
        <div v-for="line in printData.items" :key="line.delivery_item_id">
          {{ line.product_name }}：客户 {{ line.quantity }} {{ line.unit || '单位未登记' }}；
          仓库实拿
          <span v-for="(goods, index) in line.actual_goods_lines || []" :key="index">
            {{ goods.product_name || goods.product_code || '货物' }} {{ goods.quantity }} {{ goods.unit || '单位未登记' }}；
          </span>
        </div>
        <div>库位和内部拿货单请在现有拿货流程核对。</div>
      </div>
      <section v-if="printData" class="delivery-print-sheet">
        <div class="print-test-mark">测试环境预览</div>
        <h1>{{ printData.sender.company_name || '天明包装' }}送货单</h1>
        <div class="print-meta">
          <span>单号：{{ printData.delivery_number }}</span>
          <span>日期：{{ printData.delivery_date || '—' }}</span>
          <span>车牌：{{ printData.vehicle_number || '—' }}</span>
        </div>
        <div class="print-customer">
          <strong>客户：{{ printData.customer.name }}</strong>
          <span>联系人：{{ printData.customer.contact_person || '—' }}</span>
          <span>电话：{{ printData.customer.phone || '—' }}</span>
          <span>地址：{{ printData.customer.address || '—' }}</span>
        </div>
        <table>
          <thead><tr><th>客户PO</th><th>存货编码</th><th>产品名称</th><th>规格</th><th>送货数量</th><th>备注</th></tr></thead>
          <tbody>
            <tr v-for="line in printData.items" :key="line.delivery_item_id">
              <td>{{ line.customer_po || '—' }}</td><td>{{ line.product_code || '—' }}</td><td>{{ line.product_name || '—' }}</td>
              <td>{{ line.specification || '—' }}</td><td>{{ line.quantity }} {{ line.unit || '' }}</td><td>{{ line.remarks || '' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="print-totals">客户单据数量合计：{{ printData.total_quantity }}</div>
      </section>
      <template #footer>
        <el-button @click="printDialog = false">关闭</el-button>
        <el-button type="primary" :loading="printSaving" @click="confirmPrint">调用系统打印</el-button>
      </template>
    </el-dialog>

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
  background: var(--tm-bg-2);
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

.delivery-print-sheet {
  position: relative;
  padding: 24px;
  background: white;
  color: #111827;
}

.delivery-print-sheet h1 { margin: 0 0 16px; text-align: center; font-size: 25px; }
.print-test-mark { position: absolute; top: 12px; right: 16px; padding: 4px 9px; border: 2px solid #dc2626; color: #dc2626; font-weight: 900; transform: rotate(-4deg); }
.print-meta, .print-customer { display: flex; flex-wrap: wrap; gap: 10px 26px; margin-bottom: 14px; }
.delivery-print-sheet table { width: 100%; border-collapse: collapse; font-size: 12px; }
.delivery-print-sheet th, .delivery-print-sheet td { border: 1px solid #374151; padding: 7px; }
.print-totals { margin-top: 14px; text-align: right; font-weight: 800; }
.internal-print-review { margin-bottom: 10px; }

@media print {
  :global(body *) { visibility: hidden !important; }
  .internal-print-review { display: none !important; }
  .delivery-print-sheet, .delivery-print-sheet * { visibility: visible !important; }
  .delivery-print-sheet { position: fixed; inset: 0; padding: 12mm; }
}
</style>
