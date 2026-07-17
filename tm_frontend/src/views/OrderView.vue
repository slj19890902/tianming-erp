<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { masterDataApi, type Customer, type Product } from '../api/masterData'
import { addCalendarDays, beijingToday } from '../utils/time'

interface OrderLine {
  id: number
  mode: 'history' | 'temporary'
  product_id?: number
  product_code: string
  product_name: string
  material: string
  flute_type: string
  length_mm: string
  width_mm: string
  height_mm: string
  quantity: number | null
  unit_price: string
  paper_length_mm: string
  paper_width_mm: string
  score_line: string
  subtotal: string
  note: string
}

const loading = ref(false)
const saving = ref(false)
const customers = ref<Customer[]>([])
const allProducts = ref<Product[]>([])
const customerId = ref<number | null>(null)
const customerPo = ref('UAT-ORDER-001')
const orderDate = ref(beijingToday())
const deliveryDate = ref(defaultDeliveryDate())
const note = ref('')
const lines = ref<OrderLine[]>([])
const lastSavedOrder = ref('')

const tempDialogVisible = ref(false)
const tempLineIndex = ref<number | null>(null)
const tempForm = reactive({
  product_code: 'TEMP-BOX',
  product_name: '临时新箱',
  length_mm: '450',
  width_mm: '340',
  height_mm: '300',
  material: 'K=A',
  flute_type: 'AB',
  customer_square_price: '3.50',
  supplier_square_price: '2.85',
})

const filteredProducts = computed(() =>
  allProducts.value.filter((item) => item.customer_id === customerId.value),
)

const totalAmount = computed(() =>
  lines.value
    .reduce((sum, line) => sum + Number(line.subtotal || 0), 0)
    .toFixed(2),
)

function defaultDeliveryDate() {
  return addCalendarDays(beijingToday(), 7)
}

function emptyLine(): OrderLine {
  return {
    id: Date.now() + Math.floor(Math.random() * 1000),
    mode: 'history',
    product_code: '',
    product_name: '',
    material: '',
    flute_type: '',
    length_mm: '',
    width_mm: '',
    height_mm: '',
    quantity: null,
    unit_price: '',
    paper_length_mm: '',
    paper_width_mm: '',
    score_line: '',
    subtotal: '0.00',
    note: '',
  }
}

function addLine() {
  if (!customerId.value) {
    ElMessage.warning('请先选择客户，再新增明细。')
    return
  }
  lines.value.push(emptyLine())
}

function fillFromProduct(row: OrderLine, productId: number) {
  const product = filteredProducts.value.find((item) => item.id === productId)
  if (!product) return
  row.mode = 'history'
  row.product_id = product.id
  row.product_code = product.product_code
  row.product_name = product.product_name
  row.material = product.material_code || product.default_material_text || ''
  row.flute_type = product.flute_type_code || ''
  row.length_mm = product.length_mm || ''
  row.width_mm = product.width_mm || ''
  row.height_mm = product.height_mm || ''
  row.paper_length_mm = product.default_cardboard_length_mm || ''
  row.paper_width_mm = product.default_cardboard_width_mm || ''
  row.score_line = product.default_score_line || ''
  row.unit_price = product.default_unit_price || '0.0000'
  updateSubtotal(row)
  nextTick(() => focusQuantity(row.id))
}

function focusQuantity(rowId: number) {
  const input = document.querySelector<HTMLInputElement>(`[data-qty-row="${rowId}"] input`)
  input?.focus()
  input?.select()
}

function updateSubtotal(row: OrderLine) {
  row.subtotal = ((Number(row.quantity || 0) || 0) * (Number(row.unit_price || 0) || 0)).toFixed(2)
}

function displaySubtotal(row: OrderLine) {
  return ((Number(row.quantity || 0) || 0) * (Number(row.unit_price || 0) || 0)).toFixed(2)
}

function removeLine(row: OrderLine) {
  lines.value = lines.value.filter((item) => item.id !== row.id)
}

function openTemporaryDialog(index: number) {
  tempLineIndex.value = index
  tempDialogVisible.value = true
}

async function calculateTemporaryLine() {
  if (tempLineIndex.value === null) return
  try {
    const result = await masterDataApi.calculateCarton({
      length_mm: tempForm.length_mm,
      width_mm: tempForm.width_mm,
      height_mm: tempForm.height_mm,
      box_category: 'normal',
      customer_square_price: tempForm.customer_square_price,
      supplier_square_price: tempForm.supplier_square_price,
    })
    const row = lines.value[tempLineIndex.value]
    row.mode = 'temporary'
    row.product_id = undefined
    row.product_code = tempForm.product_code
    row.product_name = tempForm.product_name
    row.material = tempForm.material
    row.flute_type = tempForm.flute_type
    row.length_mm = tempForm.length_mm
    row.width_mm = tempForm.width_mm
    row.height_mm = tempForm.height_mm
    row.paper_length_mm = result.paper_length_mm
    row.paper_width_mm = result.paper_width_mm
    row.score_line = result.score_line
    row.unit_price = result.sale_unit_price
    updateSubtotal(row)
    tempDialogVisible.value = false
    await nextTick()
    focusQuantity(row.id)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '算料失败')
  }
}

function orderPayload() {
  if (!customerId.value) throw new Error('请先选择客户')
  const validLines = lines.value.filter((line) => line.product_name && Number(line.quantity || 0) > 0)
  if (!validLines.length) throw new Error('至少录入一条有效订单明细')
  return {
    customer_id: customerId.value,
    customer_po: customerPo.value,
    order_date: orderDate.value,
    delivery_date: deliveryDate.value,
    note: note.value,
    items: validLines.map((line) => ({
      product_id: line.product_id || null,
      product_code: line.product_code,
      product_name: line.product_name,
      length_mm: line.length_mm || null,
      width_mm: line.width_mm || null,
      height_mm: line.height_mm || null,
      quantity: Number(line.quantity || 0),
      unit_price: line.mode === 'temporary' ? line.unit_price : undefined,
      snapshot_material: line.material,
      snapshot_flute_type: line.flute_type,
      snapshot_score_line: line.score_line,
      snapshot_cardboard_length_mm: line.paper_length_mm || null,
      snapshot_cardboard_width_mm: line.paper_width_mm || null,
      note: line.note,
    })),
  }
}

async function saveOrder() {
  try {
    saving.value = true
    const result = await masterDataApi.createOrder(orderPayload())
    lastSavedOrder.value = `${result.order_number}，合计 ${result.total_amount} 元`
    ElMessage.success(`保存成功：${result.order_number}`)
    lines.value = []
    await nextTick()
    lines.value = [emptyLine()]
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存订单失败')
  } finally {
    saving.value = false
  }
}

async function loadData() {
  loading.value = true
  try {
    const [customerResult, productResult] = await Promise.all([
      masterDataApi.listCustomers(''),
      masterDataApi.listProducts(''),
    ])
    customers.value = customerResult.items
    allProducts.value = productResult.items
    customerId.value = customerResult.items[0]?.id || null
    lines.value = [emptyLine()]
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '订单基础数据加载失败')
  } finally {
    loading.value = false
  }
}

watch(customerId, () => {
  lines.value = [emptyLine()]
})

onMounted(loadData)
</script>

<template>
  <div class="tm-page order-page">
    <section class="tm-card order-head">
      <div class="tm-toolbar">
        <div>
          <h2 class="tm-section-title">订单生产</h2>
          <div class="tm-muted">以客户为主，历史常用箱极速复用；陌生箱型才调用算料引擎。</div>
        </div>
        <div class="order-actions">
          <el-button type="primary" :loading="saving" @click="saveOrder">保存订单</el-button>
          <el-tag v-if="lastSavedOrder" type="success" size="large">已保存：{{ lastSavedOrder }}</el-tag>
        </div>
      </div>

      <el-form class="order-form" label-width="96px" :model="{ customerId, customerPo, orderDate, deliveryDate }">
        <el-form-item label="客户">
          <el-select v-model="customerId" filterable placeholder="先选客户" :disabled="loading">
            <el-option v-for="item in customers" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="客户单号"><el-input v-model="customerPo" /></el-form-item>
        <el-form-item label="下单日期"><el-input v-model="orderDate" type="date" /></el-form-item>
        <el-form-item label="交货日期"><el-input v-model="deliveryDate" type="date" /></el-form-item>
        <el-form-item label="备注" class="wide"><el-input v-model="note" /></el-form-item>
      </el-form>
    </section>

    <section class="tm-card order-detail">
      <div class="detail-toolbar">
        <strong>订单明细</strong>
        <span class="tm-muted">当前客户常用箱：{{ filteredProducts.length }} 款</span>
        <el-button type="success" @click="addLine">新增一行</el-button>
        <span class="total">合计：{{ totalAmount }} 元</span>
      </div>

      <vxe-table
        border
        stripe
        show-overflow="ellipsis"
        height="470"
        :loading="loading"
        :keyboard-config="{ isArrow: true, isEnter: true }"
        :edit-config="{ trigger: 'click', mode: 'cell' }"
        :row-config="{ isHover: true, keyField: 'id' }"
        :data="lines"
      >
        <vxe-column type="seq" width="60" title="序号" />
        <vxe-column title="历史常用箱" width="230">
          <template #default="{ row }">
            <el-select
              v-model="row.product_id"
              filterable
              clearable
              placeholder="选客户后只显示该客户"
              @change="(value: number) => fillFromProduct(row, value)"
            >
              <el-option
                v-for="item in filteredProducts"
                :key="item.id"
                :label="`${item.product_code} / ${item.product_name}`"
                :value="item.id"
              />
            </el-select>
          </template>
        </vxe-column>
        <vxe-column field="product_code" title="存货编码" width="120" />
        <vxe-column field="product_name" title="名称" min-width="160" />
        <vxe-column title="规格" width="190">
          <template #default="{ row }">{{ row.length_mm }}×{{ row.width_mm }}×{{ row.height_mm }}</template>
        </vxe-column>
        <vxe-column field="material" title="材质" width="110" />
        <vxe-column field="flute_type" title="楞型" width="90" />
        <vxe-column field="paper_length_mm" title="纸长" width="100" />
        <vxe-column field="paper_width_mm" title="纸宽" width="100" />
        <vxe-column field="score_line" title="压线" width="150" />
        <vxe-column title="数量" width="130">
          <template #default="{ row }">
            <el-input-number
              v-model="row.quantity"
              :data-qty-row="row.id"
              :min="1"
              controls-position="right"
              @change="() => updateSubtotal(row)"
              @input="() => updateSubtotal(row)"
              @keyup.enter="addLine"
            />
          </template>
        </vxe-column>
        <vxe-column title="单价" width="110">
          <template #default="{ row }">
            <el-input v-model="row.unit_price" @input="updateSubtotal(row)" @change="updateSubtotal(row)" />
          </template>
        </vxe-column>
        <vxe-column title="金额" width="110">
          <template #default="{ row }">{{ displaySubtotal(row) }}</template>
        </vxe-column>
        <vxe-column title="操作" width="170" fixed="right">
          <template #default="{ row, rowIndex }">
            <el-button type="warning" link @click="openTemporaryDialog(rowIndex)">临时新箱</el-button>
            <el-button type="danger" link @click="removeLine(row)">删除</el-button>
          </template>
        </vxe-column>
      </vxe-table>
    </section>

    <el-dialog v-model="tempDialogVisible" title="新增完全陌生的临时纸箱" width="760px">
      <el-form class="temp-form" label-width="110px" :model="tempForm">
        <el-form-item label="临时编码"><el-input v-model="tempForm.product_code" /></el-form-item>
        <el-form-item label="品名"><el-input v-model="tempForm.product_name" /></el-form-item>
        <el-form-item label="长"><el-input v-model="tempForm.length_mm" /></el-form-item>
        <el-form-item label="宽"><el-input v-model="tempForm.width_mm" /></el-form-item>
        <el-form-item label="高"><el-input v-model="tempForm.height_mm" /></el-form-item>
        <el-form-item label="材质"><el-input v-model="tempForm.material" /></el-form-item>
        <el-form-item label="楞型"><el-input v-model="tempForm.flute_type" /></el-form-item>
        <el-form-item label="销售平方价"><el-input v-model="tempForm.customer_square_price" /></el-form-item>
        <el-form-item label="采购平方价"><el-input v-model="tempForm.supplier_square_price" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="tempDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="calculateTemporaryLine">调用算料引擎并回填</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.order-head {
  overflow: hidden;
}

.order-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 12px;
}

.order-form {
  display: grid;
  grid-template-columns: 1.2fr 1fr 1fr 1fr;
  gap: 0 12px;
  padding: 14px 16px 4px;
}

.order-form :deep(.el-form-item) {
  margin-bottom: 12px;
}

.order-form :deep(.wide) {
  grid-column: 1 / -1;
}

.order-form :deep(.el-select),
.order-form :deep(.el-input-number) {
  width: 100%;
}

.detail-toolbar {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 12px;
  background: #f2f7fc;
  border-bottom: 1px solid #9fb7d2;
  font-size: 19px;
}

.total {
  margin-left: auto;
  color: #0f7a32;
  font-size: 22px;
  font-weight: 900;
}

.temp-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 14px;
}

.temp-form :deep(.el-form-item) {
  margin-bottom: 14px;
}

:deep(.vxe-header--column) {
  background: #eaf2fb;
  color: #12385f;
  font-size: 17px;
  font-weight: 900;
}

:deep(.vxe-body--column) {
  font-size: 17px;
}
</style>
