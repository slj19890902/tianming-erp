<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  masterApi,
  orderApi,
  pricingApi,
  type Customer,
  type Material,
  type Product,
  ApiError,
} from '../api/client'
import { businessDate } from '../utils/businessDate'
import { buildReadbackExpectations, verifyOrderCreateReadback } from '../utils/orderCreateReadback'
import { useTabsStore } from '../stores/tabs'
import { useRouter } from 'vue-router'
import { resolveHoldPreview, previousBatchSelections, holdPreviewText, type HoldDecision } from '../utils/orderRequisitionHold'
import { useAuthStore } from '../stores/auth'
import { useOrderInventory } from '../composables/useOrderInventory'
import OrderInventoryPanel from '../components/OrderInventoryPanel.vue'
import { warehouseApi } from '../api/client'
import { emptyPlan } from '../utils/orderInventory'
import { newDrawingState, invalidateDrawing, uploadDrawing, drawingFields, draftDrawingPath, verifyDrawingContent, type DrawingState } from '../utils/orderDrawings'
import OrderDrawingPreview from '../components/OrderDrawingPreview.vue'

const today = businessDate()
const defaultDeliveryDate = () => businessDate(new Date(Date.now() + 7 * 86400000))

const customers = ref<Customer[]>([])
const chosenCustomer = ref<Customer | null>(null)
const products = ref<Product[]>([])
const materials = ref<Material[]>([])
const loading = ref(false)
const calculating = ref(false)
const drawingPreviewSource = ref('')
const committedAttempt = ref<{ order: { id: number; order_number: string; [key: string]: unknown }; payload: NonNullable<ReturnType<typeof buildOrderPayload>>; expected: ReturnType<typeof buildReadbackExpectations>; drawingDigests: Record<string, string> } | null>(null)
const entryLocked = computed(() => loading.value || committedAttempt.value !== null)
const drawingPermissions = computed(() => ({ edit: auth.hasPermission('products.edit'), overwrite: auth.hasPermission('products.edit') && auth.hasPermission('products.delete') }))
const chosenProducts = ref<Record<number, Product>>({})
const productSearchSerial = ref(0)
const createKey = ref(crypto.randomUUID())
const tempExpanded = ref<string[]>([])
const tabsStore = useTabsStore()
const router = useRouter()
const auth = useAuthStore()
const inventoryPermitted = computed(() => auth.hasPermission('warehouse.view') && auth.hasPermission('orders.create'))

// 下单主信息
const orderForm = ref({
  customer_id: null as number | null,
  customer_po: '',
  order_date: today,
  delivery_date: defaultDeliveryDate(),
  requisition_strategy: 'normal' as 'normal' | 'wait_previous_batch',
  note: '',
})

// 常用箱明细
const orderLines = ref<Array<{
  client_line_id: string
  product_id: number | null
  unit_price: string
  quantity: string
  production_notes: string
  drawing: DrawingState
}>>([])
const inventoryInputs = () => orderLines.value.flatMap(line => {
  const product = selectedProduct(line.product_id), quantity = Number(line.quantity)
  return product && Number.isSafeInteger(quantity) && quantity > 0 ? [{ id: line.client_line_id, product, quantity }] : []
})
const inventory = useOrderInventory(() => orderForm.value.customer_id, inventoryInputs, () => inventoryPermitted.value)
const holdDecisions = ref<Record<string, HoldDecision>>({})
const holdLoading = ref(false)
const holdError = ref('')
const holdChecked = ref(false)
let holdSerial = 0
const holdFingerprint = computed(() => JSON.stringify({
  customer: orderForm.value.customer_id, strategy: orderForm.value.requisition_strategy,
  lines: orderLines.value.map(line => ({ ...line, product: selectedProduct(line.product_id) })),
  inventory: inventory.checked.value ? Object.fromEntries(Object.entries(inventory.states.value).map(([id,s])=>[id,s.plan])) : null,
}))
watch(holdFingerprint, () => {
  holdSerial++
  holdDecisions.value = {}
  holdChecked.value = false
  holdError.value = ''
  holdLoading.value = false
})
watch([orderForm, orderLines, inventory.states], () => tabsStore.markDirty('/review/orders/native-new', true), { deep: true })

const productOptions = computed(() => {
  const found = new Map<number, Product>()
  for (const product of Object.values(chosenProducts.value)) found.set(product.id, product)
  for (const product of products.value) found.set(product.id, product)
  return [...found.values()].filter((product) => product.customer_id === orderForm.value.customer_id)
})
function selectedProduct(id: number | null) { return id ? chosenProducts.value[id] || productOptions.value.find((item) => item.id === id) : null }
const lineAmount = (line: { quantity: string; unit_price: string }) => {
  const quantity = Number(line.quantity)
  const price = Number(line.unit_price)
  return Number.isFinite(quantity) && Number.isFinite(price) ? quantity * price : 0
}
const orderAmount = computed(() => orderLines.value.reduce((sum, line) => sum + lineAmount(line), 0))

watch(() => orderForm.value.customer_id, (customerId, previousId) => {
  productSearchSerial.value++
  products.value = []
  if (previousId && customerId !== previousId) {
    const hadLines = orderLines.value.some((line) => line.product_id)
    orderLines.value.forEach(line => invalidateDrawing(line.drawing))
    orderLines.value = []
    addOrderLine()
    chosenProducts.value = {}
    createKey.value = crypto.randomUUID()
    if (hadLines) ElMessage.info('客户已变更，原客户的产品明细已移除，请重新选货')
  }
})

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
watch([tempBox, tempLine], () => tabsStore.markDirty('/review/orders/native-new', true), { deep: true })

const selectedCustomer = computed(() =>
  customers.value.find((item) => item.id === orderForm.value.customer_id) ||
  (chosenCustomer.value?.id === orderForm.value.customer_id ? chosenCustomer.value : null),
)
const customerOptions = computed(() => {
  const found = new Map<number, Customer>()
  if (chosenCustomer.value) found.set(chosenCustomer.value.id, chosenCustomer.value)
  for (const customer of customers.value) found.set(customer.id, customer)
  return [...found.values()]
})

function onCustomerSelected(id: number) {
  chosenCustomer.value = customerOptions.value.find((item) => item.id === id) || null
}

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
  const customerId = orderForm.value.customer_id
  const serial = ++productSearchSerial.value
  if (!keyword || !customerId) {
    products.value = []
    return
  }
  try {
    const result = await masterApi.listProducts(keyword, { customerId })
    if (serial === productSearchSerial.value && customerId === orderForm.value.customer_id) {
      products.value = result.items.filter((product) => product.customer_id === customerId)
    }
  } catch {
    // 静默
  }
}

async function loadMaterials() {
  try {
    const first = await masterApi.listMaterials(1, 200)
    const remaining = await Promise.all(Array.from(
      { length: Math.max(Math.ceil(first.total / 200) - 1, 0) },
      (_, index) => masterApi.listMaterials(index + 2, 200),
    ))
    materials.value = [first, ...remaining].flatMap((result) => result.items)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '材质加载失败')
  }
}

function addOrderLine() {
  orderLines.value.push({ client_line_id: crypto.randomUUID(), product_id: null, unit_price: '', quantity: '', production_notes: '', drawing: newDrawingState() })
}

function removeOrderLine(index: number) {
  invalidateDrawing(orderLines.value[index]!.drawing)
  orderLines.value.splice(index, 1)
}

function onProductSelected(index: number) {
  invalidateDrawing(orderLines.value[index]!.drawing)
  const product = productOptions.value.find((item) => item.id === orderLines.value[index].product_id)
  if (product) chosenProducts.value[product.id] = product
  const price = product?.sale_unit_price ?? product?.default_unit_price
  if (price != null) {
    orderLines.value[index].unit_price = String(price)
  }
}

async function uploadLineDrawing(line: typeof orderLines.value[number], event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  input.value = ''
  if (!file || entryLocked.value || !line.product_id || !orderForm.value.customer_id) return
  const customer = orderForm.value.customer_id, product = line.product_id
  const current = () => auth.hasPermission('orders.create') && orderForm.value.customer_id === customer && line.product_id === product && orderLines.value.includes(line)
  if (await uploadDrawing(line.drawing, file, orderApi.uploadDraftDrawing, current)) ElMessage.success('图纸上传成功')
  else if (line.drawing.error) ElMessage.error(line.drawing.error)
}
async function viewLineDrawing(line: typeof orderLines.value[number]) {
  try {
    if (committedAttempt.value) {
      const saved = await orderApi.getOrder(committedAttempt.value.order.id, committedAttempt.value.payload.idempotency_key)
      const item = saved.items.find(row => row.client_line_id === line.client_line_id)
      if (item?.drawing_file) drawingPreviewSource.value = item.drawing_file
      else ElMessage.info('该订单行尚无已保存图纸')
      return
    }
    if (line.drawing.token) { drawingPreviewSource.value = draftDrawingPath(line.drawing); return }
    if (!line.product_id) return
    const customer = orderForm.value.customer_id, productID = line.product_id
    const product = await masterApi.getProduct(productID)
    if (customer !== orderForm.value.customer_id || productID !== line.product_id || !orderLines.value.includes(line)) return
    const drawing = product.drawings?.find(row => !row.purpose || row.purpose === 'engineering')
    if (drawing) drawingPreviewSource.value = drawing.image_path
    else ElMessage.info('该常用箱尚无工程图纸')
  } catch (cause) { ElMessage.error(cause instanceof Error ? cause.message : '图纸读取失败') }
}
function clearLines() {
  if (entryLocked.value) return
  orderLines.value.forEach(line => invalidateDrawing(line.drawing)); orderLines.value = []; addOrderLine()
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
    const product = chosenProducts.value[line.product_id]
    if (!product || product.customer_id !== orderForm.value.customer_id) {
      ElMessage.warning(`第 ${index + 1} 行产品不属于当前客户，请重新选择`)
      return null
    }
    if (!Number.isSafeInteger(product.version) || Number(product.version) < 1 ||
      !Object.prototype.hasOwnProperty.call(product, 'production_notes')) {
      ElMessage.warning(`第 ${index + 1} 行产品版本或默认说明不完整，请重新选择常用箱`)
      return null
    }
    if (product.supply_mode === 'external_purchase' && line.production_notes.trim()) {
      ElMessage.warning(`第 ${index + 1} 行为外购包材，后端不会保存生产说明，请清空后提交`)
      return null
    }
    if (!line.quantity || !Number.isFinite(Number(line.quantity)) || Number(line.quantity) <= 0) {
      ElMessage.warning(`第 ${index + 1} 行数量必须大于 0`)
      return null
    }
    if (product.supply_mode !== 'external_purchase' && !Number.isInteger(Number(line.quantity))) {
      ElMessage.warning(`第 ${index + 1} 行常用箱数量须为整数`)
      return null
    }
    if (!Number.isFinite(Number(line.unit_price)) || Number(line.unit_price) < 0) {
      ElMessage.warning(`第 ${index + 1} 行单价无效`)
      return null
    }
    let drawing: ReturnType<typeof drawingFields>
    try { drawing = drawingFields(line.drawing, drawingPermissions.value) }
    catch (cause) { ElMessage.warning(`第${index + 1}行：${cause instanceof Error ? cause.message : '图纸尚未核对'}`); return null }
    items.push({
      ...drawing,
      product_id: line.product_id,
      client_line_id: line.client_line_id,
      product_expected_version: product.version as number,
      unit_price: String(line.unit_price || '0'),
      quantity: Number(line.quantity),
      production_notes: line.production_notes.trim() || undefined,
      reservation_plan: inventory.checked.value && inventory.states.value[line.client_line_id]?.product_id === line.product_id && inventory.states.value[line.client_line_id]?.quantity === Number(line.quantity) ? inventory.states.value[line.client_line_id]!.plan : emptyPlan(),
    })
  }
  return {
    idempotency_key: createKey.value,
    readback_contract: 'a01-v1' as const,
    customer_id: orderForm.value.customer_id,
    customer_po: orderForm.value.customer_po.trim() || undefined,
    order_date: orderForm.value.order_date,
    delivery_date: orderForm.value.delivery_date,
    remark: orderForm.value.note || undefined,
    requisition_strategy: orderForm.value.requisition_strategy,
    previous_batch_selections: [] as Array<{ client_line_id: string; previous_order_item_id: number }>,
    items,
  }
}

async function previewRequisitionHolds() {
  const payload = buildOrderPayload()
  if (!payload) return false
  if (orderForm.value.requisition_strategy !== 'wait_previous_batch') return true
  const serial = ++holdSerial
  const fingerprint = holdFingerprint.value
  holdLoading.value = true
  holdError.value = ''
  holdChecked.value = false
  const manual = Object.fromEntries(Object.entries(holdDecisions.value).map(([id, decision]) => [id, decision.selected]))
  try {
    const response = await orderApi.previewRequisitionHold({ customer_id: payload.customer_id,
      lines: payload.items.map(line => {
        const product = chosenProducts.value[line.product_id]!
        return { client_line_id: line.client_line_id, product_code: product.product_code,
          specification: [product.length_mm, product.width_mm, product.height_mm].filter(value => value && Number(value) > 0).map(value => String(Number(value))).join('×'),
          material: product.material_code || product.default_material_text || '',
          flute_type: product.flute_type || product.flute_type_code || '', quantity: line.quantity,
          reservation_plan: line.reservation_plan,
          finished_covered_quantity: inventory.checked.value ? inventory.states.value[line.client_line_id]?.authority?.finished_planned_quantity || 0 : 0 }
      }) })
    if (serial !== holdSerial || fingerprint !== holdFingerprint.value) return false
    holdDecisions.value = resolveHoldPreview(payload.items.map(line => line.client_line_id), response.items, manual)
    holdChecked.value = true
    return true
  } catch (error) {
    if (serial === holdSerial) holdError.value = error instanceof Error ? error.message : '报料策略预检失败'
    return false
  } finally {
    if (serial === holdSerial) holdLoading.value = false
  }
}

async function finishCommittedOrder() {
  const attempt = committedAttempt.value
  if (!attempt) return
  const { order, payload, expected } = attempt
    const saved = await orderApi.getOrder(order.id, payload.idempotency_key)
    verifyOrderCreateReadback(expected, order.create_readback ? order : saved, saved)
      for (const line of payload.items) {
        const state = inventory.states.value[line.client_line_id]
        if (!line.reservation_plan.finished.length && !line.reservation_plan.semi.length) continue
        const item = saved.items.find(row=>row.client_line_id===line.client_line_id)
        if (!state || !item || !inventory.checked.value) throw new Error('库存安排回读缺少行身份')
        for (const lot of new Set(state.allocations.map(a=>a.lot_id))) {
          const actual = await warehouseApi.getLot(lot)
          const entries = (actual.reservations || []).filter(r=>r.order_item_id===item.id && r.status==='active')
          const planned = state.allocations.filter(a=>a.lot_id===lot)
          if (!entries.length || entries.reduce((s,r)=>s+Number(r.reserved_stock_quantity||0),0)!==planned.reduce((s,a)=>s+a.stock_quantity,0)) throw new Error('批次预占实物数量回读不一致')
        }
      }
      for (const selection of payload.previous_batch_selections || []) {
        const item = saved.items.find(row => row.client_line_id === selection.client_line_id)
        const covered = item?.fully_covered_by_finished_inventory === true && item.production_required_qty === 0
        if (!item || (selection.previous_order_item_id > 0 ? item.requisition_hold?.status !== 'active' && !covered : item.requisition_hold != null)) throw new Error('报料等待关系回读不一致')
      }
    for (const [id, digest] of Object.entries(attempt.drawingDigests)) {
      const item = saved.items.find(row => row.client_line_id === id)
      if (!item?.drawing_file) throw new Error('已保存订单缺少图纸回读，请核对详情')
      await verifyDrawingContent(digest, await orderApi.drawingContent(item.drawing_file))
    }
    committedAttempt.value = null
    drawingPreviewSource.value = ''
    ElMessage.success(`订单已创建（${order.order_number}）`)
    orderForm.value = { customer_id: null, customer_po: '', order_date: businessDate(), delivery_date: defaultDeliveryDate(), requisition_strategy: 'normal', note: '' }
    orderLines.value.forEach(line => invalidateDrawing(line.drawing))
    orderLines.value = []
    addOrderLine()
    chosenCustomer.value = null
    chosenProducts.value = {}
    products.value = []
    tempBox.value = { material_id: null, box_category: 'normal', length_mm: '', width_mm: '', height_mm: '', unit_price: '' }
    tempLine.value = { quantity: '', note: '' }
    createKey.value = crypto.randomUUID()
    await nextTick()
    tabsStore.markDirty('/review/orders/native-new', false)
    void router.push({ path: '/review/orders', query: { order_id: String(saved.id) } })
}

async function submitOrder() {
  if (loading.value) return
  if (committedAttempt.value) {
    loading.value = true
    try { await finishCommittedOrder() }
    catch { ElMessage.warning('订单已保存，图纸或明细仍未完成回读；请重新读取，勿重复录单') }
    finally { loading.value = false }
    return
  }
  const payload = buildOrderPayload()
  if (!payload) return
  if (payload.items.some(line => line.drawing_save_option === 'overwrite_product')) {
    try { await ElMessageBox.confirm('将替换所选常用箱的现有工程图纸。确认覆盖？', '覆盖常用箱图纸', { confirmButtonText: '确认覆盖', cancelButtonText: '取消', type: 'warning' }) }
    catch { return }
  }
  loading.value = true
  try {
    const previousHoldDecisions = holdDecisions.value
    if (inventoryPermitted.value && inventoryInputs().length) {
      if (!await inventory.refresh(true)) throw new Error(inventory.error.value || '库存尚未核对，请保留草稿重试')
      for (const line of payload.items) line.reservation_plan = inventory.states.value[line.client_line_id]?.plan || emptyPlan()
      await nextTick()
      holdDecisions.value = previousHoldDecisions
    }
    if (payload.requisition_strategy === 'wait_previous_batch') {
      if (!await previewRequisitionHolds()) throw new Error(holdError.value || '报料策略尚未核对，请保留草稿重试')
      payload.previous_batch_selections = previousBatchSelections(holdDecisions.value, payload.items.map(line => line.client_line_id))
    }
    const expected = buildReadbackExpectations(payload, chosenProducts.value)
    const order = await orderApi.createOrder(payload)
    committedAttempt.value = { order, payload, expected, drawingDigests: Object.fromEntries(orderLines.value.filter(line => line.drawing.token).map(line => [line.client_line_id, line.drawing.digest])) }
    await finishCommittedOrder()
  } catch (error) {
    if (committedAttempt.value) {
      ElMessage.warning(`订单 ${committedAttempt.value.order.order_number} 已保存，图纸或明细回读未完成；请重新读取已保存订单`)
    } else if (error instanceof ApiError && error.code === 'ORDER_PRODUCT_VERSION_CONFLICT') {
      try {
        const ids = [...new Set(payload.items.map(line => line.product_id))]
        const refreshed = await Promise.all(ids.map(id => masterApi.getProduct(id)))
        if (refreshed.some((product, index) => product.id !== ids[index] ||
          product.customer_id !== payload.customer_id || !Number.isSafeInteger(product.version) ||
          Number(product.version) < 1 || !Object.prototype.hasOwnProperty.call(product, 'production_notes') ||
          (product.production_notes !== null && typeof product.production_notes !== 'string'))) {
          throw new Error('刷新产品的身份、版本或说明不完整')
        }
        chosenProducts.value = { ...chosenProducts.value, ...Object.fromEntries(refreshed.map(product => [product.id, product])) }
        ElMessage.warning('常用箱版本已变化，产品信息已刷新；请确认后再次保存，草稿已保留')
      } catch {
        ElMessage.warning('常用箱版本已变化；请重新选择产品后再次保存，草稿已保留')
      }
    } else if (error instanceof ApiError && error.status === 0) {
      try {
        const attempt = await orderApi.createAttempt(createKey.value)
        if (attempt.status === 'completed' && attempt.order) {
          committedAttempt.value = { order: { id: attempt.order.id, order_number: '' }, payload, expected: buildReadbackExpectations(payload, chosenProducts.value), drawingDigests: Object.fromEntries(orderLines.value.filter(line => line.drawing.token).map(line => [line.client_line_id, line.drawing.digest])) }
          ElMessage.warning(`订单已保存（ID ${attempt.order.id}），请重新读取已保存订单`)
          return
        }
      } catch { /* 保持同一保存标识供重试 */ }
      ElMessage.warning('保存结果暂不确定；请先查订单列表，再使用当前表单重试')
    } else {
      ElMessage.error(error instanceof Error ? error.message : '下单失败')
    }
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadMaterials()
  addOrderLine()
  await nextTick()
  tabsStore.markDirty('/review/orders/native-new', false)
})
</script>

<template>
  <div class="tm-page">
    <OrderDrawingPreview :source="drawingPreviewSource" @close="drawingPreviewSource = ''" />
    <section class="tm-card tm-section">
      <h2 class="tm-section-title">订单主信息</h2>
      <el-form :model="orderForm" :disabled="entryLocked" label-width="84px" class="order-head-form">
        <el-form-item label="客户">
          <el-select
            v-model="orderForm.customer_id"
            filterable
            remote
            reserve-keyword
            placeholder="输入客户名称远程搜索"
            :remote-method="searchCustomers"
            @change="onCustomerSelected"
            :loading="false"
            style="width: 100%"
          >
            <el-option v-for="item in customerOptions" :key="item.id" :label="customerLabel(item)" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="客户订单号"><el-input v-model="orderForm.customer_po" /></el-form-item>
        <el-form-item label="下单日期"><el-date-picker v-model="orderForm.order_date" type="date" value-format="YYYY-MM-DD" /></el-form-item>
        <el-form-item label="报料策略">
          <el-select v-model="orderForm.requisition_strategy">
            <el-option label="正常进入待报料" value="normal" />
            <el-option label="同款上一批送完后再报" value="wait_previous_batch" />
          </el-select>
        </el-form-item>
        <el-form-item label="交货日期"><el-date-picker v-model="orderForm.delivery_date" type="date" value-format="YYYY-MM-DD" /></el-form-item>
      </el-form>
      <div v-if="selectedCustomer" class="tm-muted" style="margin-top: 6px">
        送货方式：{{ selectedCustomer.delivery_method || '—' }}　账期：{{ selectedCustomer.payment_term_days }} 天
      </div>
    </section>

    <section class="tm-card tm-section">
      <div class="tm-toolbar">
        <h2 class="tm-section-title">常用箱明细</h2>
        <el-button type="success" :disabled="entryLocked" @click="addOrderLine">增加明细</el-button>
      </div>
      <vxe-table border stripe show-overflow="ellipsis" :data="orderLines" height="320">
        <vxe-column type="seq" width="60" title="序号" />
        <vxe-column field="product_id" title="存货编码" min-width="230">
          <template #default="{ row }">
            <el-select
              v-model="row.product_id"
              filterable
              remote
              reserve-keyword
              :disabled="entryLocked"
              placeholder="搜索产品名称/编码"
              :remote-method="searchProducts"
              style="width: 100%"
              @change="onProductSelected(orderLines.indexOf(row))"
            >
              <el-option v-for="item in productOptions" :key="item.id" :label="productLabel(item)" :value="item.id" />
            </el-select>
          </template>
        </vxe-column>
        <vxe-column title="产品名称" width="160"><template #default="{ row }">{{ selectedProduct(row.product_id)?.product_name || '—' }}</template></vxe-column>
        <vxe-column title="规格mm" width="155"><template #default="{ row }">{{ selectedProduct(row.product_id) ? [selectedProduct(row.product_id)?.length_mm, selectedProduct(row.product_id)?.width_mm, selectedProduct(row.product_id)?.height_mm].filter(value => value && Number(value) > 0).map(value => String(Number(value))).join('×') : '—' }}</template></vxe-column>
        <vxe-column title="材质楞型" width="145"><template #default="{ row }">{{ selectedProduct(row.product_id)?.default_material_text || [selectedProduct(row.product_id)?.material_code, selectedProduct(row.product_id)?.flute_type || selectedProduct(row.product_id)?.flute_type_code].filter(Boolean).join('/') || '—' }}</template></vxe-column>
        <vxe-column field="quantity" title="数量" width="140">
          <template #default="{ row }"><el-input v-model="row.quantity" :disabled="entryLocked" /></template>
        </vxe-column>
        <vxe-column field="unit_price" title="单价" width="140">
          <template #default="{ row }"><el-input v-model="row.unit_price" :disabled="entryLocked" /></template>
        </vxe-column>
        <vxe-column title="单位" width="75"><template #default="{ row }">{{ selectedProduct(row.product_id)?.unit || '只' }}</template></vxe-column>
        <vxe-column title="总价" width="110"><template #default="{ row }">{{ lineAmount(row).toFixed(2) }}</template></vxe-column>
        <vxe-column field="production_notes" title="生产说明（行级）" min-width="190">
          <template #default="{ row }"><el-input v-model="row.production_notes" :disabled="entryLocked" /></template>
        </vxe-column>
        <vxe-column title="操作 / 图纸" width="230" fixed="right">
          <template #default="{ row, rowIndex }">
            <div class="drawing-actions">
              <el-button link :disabled="!row.product_id || row.drawing.busy" @click="viewLineDrawing(row)">查看</el-button>
              <label class="drawing-upload" :class="{disabled:entryLocked || !row.product_id || row.drawing.busy}">上传<input type="file" accept=".png,.jpg,.jpeg,.webp,.pdf" :aria-label="`第${rowIndex + 1}行上传图纸`" :disabled="entryLocked || !row.product_id || row.drawing.busy" @change="uploadLineDrawing(row,$event)" /></label>
              <el-button type="danger" link :disabled="entryLocked" @click="removeOrderLine(rowIndex)">删除</el-button>
            </div>
            <span v-if="row.drawing.busy" role="status">图纸上传中…</span>
            <span v-else-if="row.drawing.filename" class="drawing-filename">{{ row.drawing.filename }}</span>
            <span v-if="row.drawing.error" role="alert">{{ row.drawing.error }}</span>
            <el-select v-if="row.drawing.token" v-model="row.drawing.saveOption" :aria-label="`第${rowIndex + 1}行图纸保存位置`" :disabled="entryLocked">
              <el-option label="仅本订单" value="order_only" />
              <el-option v-if="drawingPermissions.edit" label="存到常用箱" value="save_to_product" />
              <el-option v-if="drawingPermissions.overwrite" label="覆盖常用箱" value="overwrite_product" />
            </el-select>
          </template>
        </vxe-column>
      </vxe-table>
      <div class="tm-muted" style="text-align:right;margin-top:8px">界面试算合计：￥{{ orderAmount.toFixed(2) }}（保存后以后端金额为准）</div>
      <section v-if="inventoryPermitted" aria-label="库存与需报核对">
        <div style="display:flex;gap:8px;align-items:center;margin-top:10px"><el-button :loading="inventory.busy.value" :disabled="entryLocked" @click="inventory.refresh()">核对库存与需报</el-button>
          <span v-if="inventory.changed.value">产品、客户或数量变化后请重新核对库存；保存前会再次校验。</span></div>
        <p v-if="inventory.error.value" role="alert">{{ inventory.error.value }}</p>
        <template v-for="(line,index) in orderLines" :key="line.client_line_id">
          <OrderInventoryPanel v-if="selectedProduct(line.product_id) && inventory.states.value[line.client_line_id]" :product="selectedProduct(line.product_id)!" :state="inventory.states.value[line.client_line_id]!" :line-number="index+1" :disabled="entryLocked||inventory.busy.value||inventory.changed.value"
            @choose="(part,lot,automatic)=>inventory.choose(line.client_line_id,part,lot,automatic)" @browse="(part,page)=>inventory.browse(line.client_line_id,part,page)" />
        </template>
      </section>
      <section v-if="orderForm.requisition_strategy === 'wait_previous_batch'" aria-label="报料策略核对" class="hold-preview">
        <el-button :loading="holdLoading" :disabled="entryLocked" @click="previewRequisitionHolds">核对上一批</el-button>
        <p v-if="holdError" role="alert">{{ holdError }}</p>
        <p v-else-if="!holdChecked">选择产品并填写数量后核对上一批；多批次或有差异时需逐行明确处理。</p>
        <div v-for="(line, index) in orderLines" :key="line.client_line_id">
          <template v-if="holdDecisions[line.client_line_id]">
            <p>第{{ index + 1 }}行：{{ holdDecisions[line.client_line_id]!.selected === 0 ? '已选择正常进入待报料。' : holdDecisions[line.client_line_id]!.selected ? '已选择等上一批送完后再报料。' : holdPreviewText(holdDecisions[line.client_line_id]!.preview) }}</p>
            <p v-for="warning in holdDecisions[line.client_line_id]!.preview.warnings" :key="warning">{{ warning }}</p>
            <el-select v-model="holdDecisions[line.client_line_id]!.selected" :aria-label="`第${index + 1}行上一批处理`" :disabled="entryLocked" placeholder="请选择上一批或正常待报料" style="min-width:340px">
              <el-option label="本行正常进入待报料" :value="0" />
              <el-option v-for="candidate in holdDecisions[line.client_line_id]!.preview.candidates" :key="candidate.order_item_id"
                :value="candidate.order_item_id" :label="`等 ${candidate.order_number} 送完 · ${candidate.specification || '规格未登记'} · ${candidate.material || '材质未登记'} · ${candidate.flute_type || '楞型未登记'}`" />
            </el-select>
          </template>
        </div>
      </section>
      <el-form label-width="84px" :disabled="entryLocked" style="margin-top:12px"><el-form-item label="备注（选填）"><el-input v-model="orderForm.note" /></el-form-item></el-form>
    </section>

    <section class="tm-card tm-section">
      <el-collapse v-model="tempExpanded"><el-collapse-item name="temp" title="临时新箱算料（按需展开，仅测算，不能直接下单）">
      <el-alert
        type="warning"
        show-icon
        :closable="false"
        title="临时新箱须先在基础资料登记完善，再选择常用箱下单。"
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
      </el-collapse-item></el-collapse>
    </section>

    <div class="tm-toolbar tm-section">
      <div class="tm-muted">正常进入待报料；选择等候策略的明细按核对结果等待上一批送完。</div>
      <div style="margin-left: auto; display: flex; gap: 10px">
        <el-button size="large" :disabled="entryLocked" @click="clearLines">清空明细</el-button>
        <el-button type="primary" size="large" :loading="loading" @click="submitOrder">{{ committedAttempt ? '重新读取已保存订单' : '提交订单' }}</el-button>
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
.hold-preview { margin-top:12px; padding:12px; border:1px solid var(--el-border-color); border-radius:6px; }
</style>

<style scoped>
.drawing-actions{display:flex;align-items:center;gap:10px}.drawing-upload{cursor:pointer;color:var(--el-color-primary);font-size:14px}.drawing-upload input{width:1px;height:1px;position:absolute;opacity:0}.drawing-upload.disabled{cursor:default;opacity:.5}.drawing-filename{display:block;overflow:hidden;text-overflow:ellipsis;font-size:12px}
</style>
