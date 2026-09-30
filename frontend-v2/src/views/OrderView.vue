<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  masterApi,
  orderApi,
  pricingApi,
  type Customer,
  type Material,
  type Product,
} from '../api/client'

const today = new Date().toISOString().slice(0, 10)

const customers = ref<Customer[]>([])
const products = ref<Product[]>([])
const materials = ref<Material[]>([])
const loading = ref(false)
const calculating = ref(false)

// 下单主信息
const orderForm = ref({
  customer_id: null as number | null,
  order_date: today,
  delivery_date: today,
  note: '',
})

// 常用箱明细
const orderLines = ref<Array<{
  product_id: number | null
  unit_price: string
  quantity: string
  note: string
}>>([])

// 临时新箱
const tempBox = ref({
  material_id: null as number | null,
  box_category: 'normal' as 'normal' | 'plane' | 'cover' | 'special' | 'cover_bottom',
  length_mm: '',
  width_mm: '',
  height_mm: '',
  unit_price: '',
})
const tempLine = ref({ quantity: '', note: '' })

const selectedCustomer = computed(() =>
  customers.value.find((item) => item.id === orderForm.value.customer_id),
)

function customerLabel(customer: Customer) {
  return customer.name
}

function productLabel(product: Product) {
  const spec = product.length_mm && product.width_mm && product.height_mm
    ? `${product.length_mm}×${product.width_mm}×${product.height_mm}`
    : ''
  return [product.product_code, product.product_name, spec].filter(Boolean).join(' ')
}

async function searchCustomers(keyword: string) {
  if (!keyword) {
    customers.value = []
    return
  }
  try {
    const result = await masterApi.listCustomers(keyword, { pageSize: 20 })
    customers.value = result.items
  } catch {
    // 搜索失败时静默，等待用户继续输入
  }
}

async function searchProducts(keyword: string) {
  if (!keyword) {
    products.value = []
    return
  }
  try {
    const result = await masterApi.listProducts(keyword)
    products.value = result.items
  } catch {
    // 静默
  }
}

async function loadMaterials() {
  try {
    const result = await masterApi.listMaterials(1, 200)
    materials.value = result.items
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '材质加载失败')
  }
}

function addOrderLine() {
  orderLines.value.push({ product_id: null, unit_price: '', quantity: '', note: '' })
}

function removeOrderLine(index: number) {
  orderLines.value.splice(index, 1)
}

function onProductSelected(index: number) {
  const product = products.value.find((item) => item.id === orderLines.value[index].product_id)
  if (product?.default_unit_price) {
    orderLines.value[index].unit_price = String(product.default_unit_price)
  }
}

/** 临时新箱算单价：后端只返回单价与面积，无纸板展开尺寸 */
async function calculateTempPrice() {
  const material = materials.value.find((item) => item.id === tempBox.value.material_id)
  if (!material) {
    ElMessage.warning('请先选择材质')
    return
  }
  if (!tempBox.value.length_mm || !tempBox.value.width_mm || !tempBox.value.height_mm) {
    ElMessage.warning('请填写长宽高')
    return
  }
  calculating.value = true
  try {
    const result = await pricingApi.calculate({
      box_category: tempBox.value.box_category,
      board_square_price: String(material.customer_square_price ?? ''),
      length_mm: tempBox.value.length_mm.trim(),
      width_mm: tempBox.value.width_mm.trim(),
      height_mm: tempBox.value.height_mm.trim(),
    })
    tempBox.value.unit_price = result.unit_price
    ElMessage.success(`测算完成：单价 ${result.unit_price}，用板面积 ${result.area_m2}㎡（仅供参考）`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '算料失败')
  } finally {
    calculating.value = false
  }
}

function buildOrderPayload() {
  if (!orderForm.value.customer_id) {
    ElMessage.warning('请先选择客户')
    return null
  }
  if (orderLines.value.length === 0) {
    ElMessage.warning('请至少添加一行常用箱明细')
    return null
  }
  const items = []
  for (const [index, line] of orderLines.value.entries()) {
    if (!line.product_id) {
      ElMessage.warning(`第 ${index + 1} 行尚未选择常用箱`)
      return null
    }
    if (!line.quantity || Number(line.quantity) <= 0) {
      ElMessage.warning(`第 ${index + 1} 行数量必须大于 0`)
      return null
    }
    // 后端 OrderItemCreate 无行备注字段，备注只留在前端
    items.push({
      product_id: line.product_id,
      unit_price: String(line.unit_price || '0'),
      quantity: Number(line.quantity),
    })
  }
  return {
    customer_id: orderForm.value.customer_id,
    order_date: orderForm.value.order_date,
    delivery_date: orderForm.value.delivery_date,
    remark: orderForm.value.note || undefined,
    items,
  }
}

async function submitOrder() {
  const payload = buildOrderPayload()
  if (!payload) return
  loading.value = true
  try {
    const order = await orderApi.createOrder(payload)
    ElMessage.success(`订单已创建（${order.order_number}）`)
    orderLines.value = []
    addOrderLine()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '下单失败')
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadMaterials()
  addOrderLine()
})
</script>

<template>
  <div class="tm-page">
    <section class="tm-card tm-section">
      <h2 class="tm-section-title">订单主信息</h2>
      <el-form :model="orderForm" label-width="84px" class="order-head-form">
        <el-form-item label="客户">
          <el-select
            v-model="orderForm.customer_id"
            filterable
            remote
            reserve-keyword
            placeholder="输入客户名称远程搜索"
            :remote-method="searchCustomers"
            :loading="false"
            style="width: 100%"
          >
            <el-option v-for="item in customers" :key="item.id" :label="customerLabel(item)" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="下单日期"><el-date-picker v-model="orderForm.order_date" type="date" value-format="YYYY-MM-DD" /></el-form-item>
        <el-form-item label="交货日期"><el-date-picker v-model="orderForm.delivery_date" type="date" value-format="YYYY-MM-DD" /></el-form-item>
        <el-form-item label="备注" class="wide"><el-input v-model="orderForm.note" /></el-form-item>
      </el-form>
      <div v-if="selectedCustomer" class="tm-muted" style="margin-top: 6px">
        送货方式：{{ selectedCustomer.delivery_method || '—' }}　账期：{{ selectedCustomer.payment_term_days }} 天
      </div>
    </section>

    <section class="tm-card tm-section">
      <div class="tm-toolbar">
        <h2 class="tm-section-title">常用箱明细</h2>
        <el-button type="success" @click="addOrderLine">新增一行</el-button>
      </div>
      <vxe-table border stripe show-overflow="ellipsis" :data="orderLines" height="320">
        <vxe-column type="seq" width="60" title="序号" />
        <vxe-column field="product_id" title="常用箱" min-width="260">
          <template #default="{ row }">
            <el-select
              v-model="row.product_id"
              filterable
              remote
              reserve-keyword
              placeholder="搜索产品名称/编码"
              :remote-method="searchProducts"
              style="width: 100%"
              @change="onProductSelected(orderLines.indexOf(row))"
            >
              <el-option v-for="item in products" :key="item.id" :label="productLabel(item)" :value="item.id" />
            </el-select>
          </template>
        </vxe-column>
        <vxe-column field="unit_price" title="单价" width="140">
          <template #default="{ row }"><el-input v-model="row.unit_price" /></template>
        </vxe-column>
        <vxe-column field="quantity" title="数量" width="140">
          <template #default="{ row }"><el-input v-model="row.quantity" /></template>
        </vxe-column>
        <vxe-column field="note" title="备注" min-width="160">
          <template #default="{ row }"><el-input v-model="row.note" /></template>
        </vxe-column>
        <vxe-column title="操作" width="90" fixed="right">
          <template #default="{ rowIndex }">
            <el-button type="danger" link @click="removeOrderLine(rowIndex)">删除</el-button>
          </template>
        </vxe-column>
      </vxe-table>
    </section>

    <section class="tm-card tm-section">
      <h2 class="tm-section-title">临时新箱算料</h2>
      <el-alert
        type="warning"
        show-icon
        :closable="false"
        title="后端 /api/pricing/calculate 仅返回单价与用板面积，不返回纸板展开尺寸；临时新箱须先在基础资料建档才能随订单提交。"
        style="margin-bottom: 14px"
      />
      <el-form label-width="84px" class="temp-form">
        <el-form-item label="材质">
          <el-select v-model="tempBox.material_id" filterable clearable placeholder="选择材质" style="width: 100%">
            <el-option
              v-for="item in materials"
              :key="item.id"
              :label="`${item.code} ${item.name || ''}（${item.customer_square_price ?? '无价'}）`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="箱型">
          <el-select v-model="tempBox.box_category">
            <el-option label="普通箱" value="normal" />
            <el-option label="飞机盒" value="plane" />
            <el-option label="天地盖" value="cover" />
            <el-option label="异型箱" value="special" />
            <el-option label="盖底" value="cover_bottom" />
          </el-select>
        </el-form-item>
        <el-form-item label="长 mm"><el-input v-model="tempBox.length_mm" /></el-form-item>
        <el-form-item label="宽 mm"><el-input v-model="tempBox.width_mm" /></el-form-item>
        <el-form-item label="高 mm"><el-input v-model="tempBox.height_mm" /></el-form-item>
        <el-form-item label="测算单价"><el-input v-model="tempBox.unit_price" placeholder="点击右侧测算自动填入" /></el-form-item>
        <el-form-item label="数量"><el-input v-model="tempLine.quantity" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="tempLine.note" /></el-form-item>
      </el-form>
      <div style="margin-top: 12px; display: flex; gap: 10px">
        <el-button type="primary" :loading="calculating" @click="calculateTempPrice">测算单价</el-button>
      </div>
    </section>

    <div class="tm-toolbar tm-section">
      <div class="tm-muted">提交后订单状态为 pending，可在报料工作台继续流转。</div>
      <div style="margin-left: auto; display: flex; gap: 10px">
        <el-button size="large" @click="orderLines = []; addOrderLine()">清空明细</el-button>
        <el-button type="primary" size="large" :loading="loading" @click="submitOrder">提交订单</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.order-head-form {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 2px 18px;
}

.order-head-form :deep(.wide) {
  grid-column: 1 / -1;
}

.order-head-form :deep(.el-select),
.order-head-form :deep(.el-date-editor) {
  width: 100%;
}

.temp-form {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 2px 18px;
}

.temp-form :deep(.el-select) {
  width: 100%;
}
</style>
