<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { masterDataApi, type DeliveryNote, type DeliveryPendingItem, type ReturnReceipt } from '../api/masterData'

const activeTab = ref('pending')
const loading = ref(false)
const pendingItems = ref<DeliveryPendingItem[]>([])
const deliveries = ref<DeliveryNote[]>([])
const receipts = ref<ReturnReceipt[]>([])
const selectedPendingIds = ref<number[]>([])
const selectedReceiptIds = ref<number[]>([])
const receiptDialog = ref(false)
const activeDelivery = ref<DeliveryNote | null>(null)
const receiptRows = ref<Array<{ delivery_item_id: number; product_name: string; delivery_qty: number; actual_signed_qty: number; difference_reason: string }>>([])

const today = new Date().toISOString().slice(0, 10)
const deliveryForm = ref({
  delivery_date: today,
  vehicle_number: '苏E12345',
  driver_name: '张师傅',
})
const receiptForm = ref({
  actual_received_date: today,
  signed_by: '客户仓库',
})

const selectedPending = computed(() => pendingItems.value.filter((item) => selectedPendingIds.value.includes(item.order_item_id)))
const selectedReceiptRows = computed(() => receipts.value.filter((item) => selectedReceiptIds.value.includes(item.id)))

function toggleAllPending(value: boolean) {
  selectedPendingIds.value = value ? pendingItems.value.map((item) => item.order_item_id) : []
}

function toggleAllReview(value: boolean) {
  selectedReceiptIds.value = value ? receipts.value.map((item) => item.id) : []
}

async function loadAll() {
  loading.value = true
  try {
    const [pending, waitReceipt, waitReview] = await Promise.all([
      masterDataApi.listDeliveryPendingItems(),
      masterDataApi.listDeliveries('DELIVERED_WAIT_RECEIPT'),
      masterDataApi.listReceipts('SIGNED_WAIT_OWNER_REVIEW'),
    ])
    pendingItems.value = pending.items
    deliveries.value = waitReceipt.items
    receipts.value = waitReview.items
    selectedPendingIds.value = selectedPendingIds.value.filter((id) => pendingItems.value.some((item) => item.order_item_id === id))
    selectedReceiptIds.value = selectedReceiptIds.value.filter((id) => receipts.value.some((item) => item.id === id))
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '加载出货数据失败')
  } finally {
    loading.value = false
  }
}

async function createDelivery() {
  if (!selectedPending.value.length) {
    ElMessage.warning('请先勾选待送货明细')
    return
  }
  const customerId = selectedPending.value[0].customer_id
  if (selectedPending.value.some((item) => item.customer_id !== customerId)) {
    ElMessage.warning('一张送货单只能包含同一客户的产品')
    return
  }
  const delivery = await masterDataApi.createDelivery({
    customer_id: customerId,
    delivery_date: deliveryForm.value.delivery_date,
    vehicle_number: deliveryForm.value.vehicle_number,
    driver_name: deliveryForm.value.driver_name,
    items: selectedPending.value.map((item) => ({
      order_item_id: item.order_item_id,
      delivery_qty: item.remaining_qty,
    })),
  })
  ElMessage.success(`送货单已生成：${delivery.delivery_number}`)
  selectedPendingIds.value = []
  await loadAll()
}

async function printDelivery(delivery: DeliveryNote) {
  const html = `
    <div style="font-family:SimSun,serif;padding:24px;">
      <h1 style="text-align:center;margin:0;">苏州天明包装有限公司</h1>
      <h2 style="text-align:center;margin:8px 0 18px;">送货单</h2>
      <div style="display:flex;justify-content:space-between;font-size:16px;margin-bottom:12px;">
        <span>客户：${delivery.customer_name || ''}</span>
        <span>单号：${delivery.delivery_number}</span>
        <span>日期：${delivery.delivery_date}</span>
        <span>车号：${delivery.vehicle_number || ''}</span>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:15px;">
        <thead><tr>
          <th style="border:1px solid #000;padding:6px;">客户单号</th>
          <th style="border:1px solid #000;padding:6px;">款号(含品名规格)</th>
          <th style="border:1px solid #000;padding:6px;">单位</th>
          <th style="border:1px solid #000;padding:6px;">数量</th>
          <th style="border:1px solid #000;padding:6px;">备注</th>
        </tr></thead>
        <tbody>
          ${delivery.items.map((item) => `
            <tr>
              <td style="border:1px solid #000;padding:6px;">${item.customer_po || ''}</td>
              <td style="border:1px solid #000;padding:6px;">${item.product_code || ''} ${item.product_name} ${item.spec || ''}</td>
              <td style="border:1px solid #000;padding:6px;text-align:center;">PCS</td>
              <td style="border:1px solid #000;padding:6px;text-align:right;">${item.delivery_qty}</td>
              <td style="border:1px solid #000;padding:6px;">${item.remark || ''}</td>
            </tr>`).join('')}
        </tbody>
      </table>
      <p style="font-size:16px;">送货总量：${delivery.total_quantity}</p>
      <div style="display:flex;justify-content:space-between;margin-top:34px;font-size:16px;">
        <span>送货人：__________</span>
        <span>收货单位(签章)：__________</span>
        <span>经手人：__________</span>
      </div>
      <p style="margin-top:28px;">白联:存档　红联:客户　黄联:回单</p>
    </div>`
  const printJSModule = await import('print-js')
  printJSModule.default({ printable: html, type: 'raw-html', scanStyles: false })
}

function openReceipt(delivery: DeliveryNote) {
  activeDelivery.value = delivery
  receiptRows.value = delivery.items.map((item) => ({
    delivery_item_id: item.id,
    product_name: item.product_name,
    delivery_qty: item.delivery_qty,
    actual_signed_qty: item.delivery_qty,
    difference_reason: '',
  }))
  receiptDialog.value = true
}

async function submitReceipt() {
  if (!activeDelivery.value) return
  await masterDataApi.createReceipt({
    delivery_id: activeDelivery.value.id,
    actual_received_date: receiptForm.value.actual_received_date,
    signed_by: receiptForm.value.signed_by,
    items: receiptRows.value.map((item) => ({
      delivery_item_id: item.delivery_item_id,
      actual_signed_qty: item.actual_signed_qty,
      difference_reason: item.difference_reason || undefined,
    })),
  })
  ElMessage.success('回单已录入，等待老板核对')
  receiptDialog.value = false
  await loadAll()
}

async function ownerConfirmSelected() {
  if (!selectedReceiptRows.value.length) {
    ElMessage.warning('请先勾选待核对回单')
    return
  }
  await ElMessageBox.confirm(`确认 ${selectedReceiptRows.value.length} 张回单老板已核对无误？`, '老板核对确认', {
    type: 'warning',
  })
  for (const receipt of selectedReceiptRows.value) {
    await masterDataApi.ownerConfirmReceipt(receipt.id, '老板')
  }
  ElMessage.success('老板核对完成，已进入待对账池')
  selectedReceiptIds.value = []
  await loadAll()
}

onMounted(loadAll)
</script>

<template>
  <section class="tm-page">
    <div class="tm-page-head">
      <div>
        <h1>出货与回签工作台</h1>
        <p>按纸质送货单流程管理：生成送货单 → 回单签收 → 老板核对 → 月结对账。</p>
      </div>
      <el-button type="primary" @click="loadAll">刷新</el-button>
    </div>

    <el-tabs v-model="activeTab" type="border-card" class="tm-tabs">
      <el-tab-pane label="待送货" name="pending">
        <div class="toolbar">
          <el-date-picker v-model="deliveryForm.delivery_date" value-format="YYYY-MM-DD" type="date" />
          <el-input v-model="deliveryForm.vehicle_number" placeholder="车号" />
          <el-input v-model="deliveryForm.driver_name" placeholder="司机" />
          <el-button type="success" @click="createDelivery">生成送货单</el-button>
        </div>
        <div class="summary">
          <el-checkbox :model-value="selectedPendingIds.length === pendingItems.length && pendingItems.length > 0" @change="(v: boolean) => toggleAllPending(v)">全选/取消全选</el-checkbox>
          <strong>已选 {{ selectedPending.length }} 条</strong>
        </div>
        <vxe-table v-loading="loading" border stripe height="520" :data="pendingItems">
          <vxe-column title="选择" width="80">
            <template #default="{ row }">
              <el-checkbox v-model="selectedPendingIds" :label="row.order_item_id" />
            </template>
          </vxe-column>
          <vxe-column field="order_number" title="订单号" width="170" />
          <vxe-column field="customer_name" title="客户" width="170" />
          <vxe-column field="product_name" title="产品" min-width="170" />
          <vxe-column field="spec" title="规格" width="160" />
          <vxe-column field="remaining_qty" title="可送数量" width="120" />
          <vxe-column field="unit_price" title="单价" width="100" />
        </vxe-table>
      </el-tab-pane>

      <el-tab-pane label="待回签" name="receipt">
        <vxe-table v-loading="loading" border stripe height="560" :data="deliveries">
          <vxe-column field="delivery_number" title="送货单号" width="180" />
          <vxe-column field="customer_name" title="客户" width="180" />
          <vxe-column field="delivery_date" title="送货日期" width="130" />
          <vxe-column field="vehicle_number" title="车号" width="120" />
          <vxe-column field="total_quantity" title="送货数量" width="120" />
          <vxe-column title="操作" width="260">
            <template #default="{ row }">
              <el-button type="primary" @click="printDelivery(row)">打印送货单</el-button>
              <el-button type="success" @click="openReceipt(row)">录入回单</el-button>
            </template>
          </vxe-column>
        </vxe-table>
      </el-tab-pane>

      <el-tab-pane label="待老板核对" name="review">
        <div class="summary">
          <el-checkbox :model-value="selectedReceiptIds.length === receipts.length && receipts.length > 0" @change="(v: boolean) => toggleAllReview(v)">全选/取消全选</el-checkbox>
          <el-button type="danger" size="large" @click="ownerConfirmSelected">确认核对无误</el-button>
        </div>
        <vxe-table v-loading="loading" border stripe height="520" :data="receipts">
          <vxe-column title="选择" width="80">
            <template #default="{ row }">
              <el-checkbox v-model="selectedReceiptIds" :label="row.id" />
            </template>
          </vxe-column>
          <vxe-column field="delivery_number" title="送货单号" width="180" />
          <vxe-column field="customer_name" title="客户" width="180" />
          <vxe-column field="actual_received_date" title="回单日期" width="130" />
          <vxe-column field="signed_by" title="签收人" width="130" />
          <vxe-column title="实签数量" width="130">
            <template #default="{ row }">{{ row.items.reduce((sum: number, item: any) => sum + item.actual_signed_qty, 0) }}</template>
          </vxe-column>
        </vxe-table>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="receiptDialog" title="录入客户回单" width="720px">
      <div class="receipt-form">
        <el-date-picker v-model="receiptForm.actual_received_date" value-format="YYYY-MM-DD" type="date" />
        <el-input v-model="receiptForm.signed_by" placeholder="签收人" />
        <vxe-table border :data="receiptRows">
          <vxe-column field="product_name" title="产品" min-width="160" />
          <vxe-column field="delivery_qty" title="送货数" width="100" />
          <vxe-column title="实签数" width="150">
            <template #default="{ row }">
              <el-input-number v-model="row.actual_signed_qty" :min="0" :max="row.delivery_qty" />
            </template>
          </vxe-column>
          <vxe-column title="差异原因" min-width="180">
            <template #default="{ row }">
              <el-input v-model="row.difference_reason" placeholder="少签时必填" />
            </template>
          </vxe-column>
        </vxe-table>
      </div>
      <template #footer>
        <el-button @click="receiptDialog = false">取消</el-button>
        <el-button type="primary" @click="submitReceipt">保存回单</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.tm-page {
  display: grid;
  gap: 14px;
}
.tm-page-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 18px;
  background: #fff;
  border: 1px solid #9fb7d2;
}
.tm-page-head h1 {
  margin: 0;
  font-size: 28px;
  color: #14395f;
}
.tm-page-head p {
  margin: 6px 0 0;
  color: #4b6278;
}
.tm-tabs {
  min-height: 640px;
}
.toolbar,
.summary {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-bottom: 12px;
}
.toolbar .el-input {
  width: 180px;
}
.receipt-form {
  display: grid;
  gap: 12px;
}
</style>
