<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { masterApi, type Customer, type Material, type Product } from '../api/client'

type TabName = 'customers' | 'products' | 'materials' | 'flutes'

const activeTab = ref<TabName>('customers')
const loading = ref(false)
const keyword = ref('')
const dialogVisible = ref(false)
const editingId = ref<number | null>(null)
const showInactiveCustomers = ref(false)

const customers = ref<Customer[]>([])
const products = ref<Product[]>([])
const materials = ref<Material[]>([])

const totals = reactive<Record<TabName, number>>({
  customers: 0,
  products: 0,
  materials: 0,
  flutes: 0,
})
const customerPage = reactive({ page: 1, pageSize: 50 })
const form = reactive<Record<string, unknown>>({})

const tabTitle: Record<TabName, string> = {
  customers: '客户资料',
  products: '常用箱资料',
  materials: '材质资料',
  flutes: '楞型资料',
}
const dialogTitle = computed(() => `${editingId.value ? '编辑' : '新增'}${tabTitle[activeTab.value]}`)

const longestCustomerNameLength = computed(() =>
  customers.value.reduce((max, item) => Math.max(max, Array.from(item.name || '').length), 4),
)
const customerNameColumnWidth = computed(() =>
  Math.max(180, Math.min(520, (longestCustomerNameLength.value + 2) * 16 + 28)),
)

function resetForm(payload: Record<string, unknown>) {
  Object.keys(form).forEach((key) => delete form[key])
  Object.assign(form, payload)
}

async function loadCurrent() {
  if (activeTab.value === 'flutes') return
  loading.value = true
  try {
    if (activeTab.value === 'customers') {
      let result = await masterApi.listCustomers(keyword.value, {
        includeInactive: showInactiveCustomers.value,
        page: customerPage.page,
        pageSize: customerPage.pageSize,
      })
      if (result.items.length === 0 && result.total > 0 && customerPage.page > 1) {
        customerPage.page -= 1
        result = await masterApi.listCustomers(keyword.value, {
          includeInactive: showInactiveCustomers.value,
          page: customerPage.page,
          pageSize: customerPage.pageSize,
        })
      }
      customers.value = result.items
      totals.customers = result.total
    } else if (activeTab.value === 'products') {
      const result = await masterApi.listProducts(keyword.value)
      products.value = result.items
      totals.products = result.total
    } else {
      // 后端材质列表无 keyword 参数：拉全量后前端本地过滤
      const result = await masterApi.listMaterials(1, 200)
      const value = keyword.value.trim()
      const filtered = value
        ? result.items.filter((item) =>
            [item.code, item.name, item.paper_composition]
              .filter(Boolean)
              .some((field) => String(field).includes(value)),
          )
        : result.items
      materials.value = filtered
      totals.materials = filtered.length
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '基础资料加载失败')
  } finally {
    loading.value = false
  }
}

async function loadDictionaries() {
  const [customerResult, materialResult] = await Promise.all([
    masterApi.listCustomers('', { pageSize: 200 }),
    masterApi.listMaterials(1, 200),
  ])
  customers.value = customerResult.items
  totals.customers = customerResult.total
  materials.value = materialResult.items
  totals.materials = materialResult.items.length
}

function openCreate() {
  editingId.value = null
  if (activeTab.value === 'customers') {
    resetForm({
      customer_code: '', name: '', short_name: '', contact_person: '', phone: '',
      address: '', payment_term_days: 30, delivery_method: '配送',
      default_tax_rate: '0.13', note: '',
    })
  } else if (activeTab.value === 'products') {
    resetForm({
      customer_id: customers.value[0]?.id, product_code: '', customer_material_code: '',
      product_name: '', material_id: materials.value[0]?.id, length_mm: '', width_mm: '',
      height_mm: '', box_category: 'normal', box_style: '0201', production_process: '钉箱',
      default_score_line: '', default_cardboard_length_mm: '', default_cardboard_width_mm: '',
      default_unit_price: '', note: '',
    })
  } else if (activeTab.value === 'materials') {
    resetForm({
      code: '', name: '', paper_composition: '', basis_weight_description: '',
      layer_count: 5, customer_square_price: '', supplier_square_price: '', note: '',
    })
  }
  dialogVisible.value = true
}

function openEdit(row: Record<string, unknown>) {
  editingId.value = row.id as number
  resetForm({ ...row })
  dialogVisible.value = true
}

function cleanPayload(payload: Record<string, unknown>) {
  return Object.fromEntries(
    Object.entries(payload).map(([key, value]) => [key, value === '' ? null : value]),
  )
}

async function submitForm() {
  try {
    const payload = cleanPayload(form)
    if (activeTab.value === 'customers') {
      if (editingId.value) await masterApi.updateCustomer(editingId.value, payload)
      else await masterApi.createCustomer(payload)
    } else if (activeTab.value === 'products') {
      if (editingId.value) await masterApi.updateProduct(editingId.value, payload)
      else await masterApi.createProduct(payload)
    } else {
      if (editingId.value) await masterApi.updateMaterial(editingId.value, payload)
      else await masterApi.createMaterial(payload)
    }
    ElMessage.success('保存成功')
    dialogVisible.value = false
    await loadDictionaries()
    await loadCurrent()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存失败')
  }
}

async function disableRecord(row: { id: number; name?: string; product_name?: string; code?: string }) {
  const label = row.name || row.product_name || row.code || `ID ${row.id}`
  try {
    await ElMessageBox.confirm(
      `确认停用“${label}”吗？停用后不会出现在新单选择列表中。`,
      '停用确认',
      { type: 'warning', confirmButtonText: '确认停用', cancelButtonText: '取消' },
    )
    if (activeTab.value === 'products') await masterApi.updateProductStatus(row.id, false)
    else await masterApi.deleteMaterial(row.id)
    ElMessage.success('已停用')
    await loadDictionaries()
    await loadCurrent()
  } catch (error) {
    if (error === 'cancel') return
    ElMessage.error(error instanceof Error ? error.message : '停用失败')
  }
}

async function moveProductToTrash(row: Product) {
  try {
    await ElMessageBox.confirm(
      `确认将“${row.product_name || row.product_code}”移入垃圾站吗？仅管理员可用，移入后可恢复。`,
      '移入垃圾站',
      { type: 'warning', confirmButtonText: '确认移入', cancelButtonText: '取消' },
    )
    await masterApi.deleteProduct(row.id)
    ElMessage.success('已移入垃圾站')
    await loadCurrent()
  } catch (error) {
    if (error === 'cancel') return
    ElMessage.error(error instanceof Error ? error.message : '移入垃圾站失败')
  }
}

async function toggleCustomerStatus(row: Customer) {
  const nextActive = !row.is_active
  const action = nextActive ? '启用' : '停用'
  try {
    await ElMessageBox.confirm(`确认${action}“${row.name}”吗？`, `${action}确认`, {
      type: 'warning',
      confirmButtonText: `确认${action}`,
      cancelButtonText: '取消',
    })
    await masterApi.updateCustomerStatus(row.id, nextActive)
    ElMessage.success(`已${action}`)
    await loadDictionaries()
    await loadCurrent()
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof Error ? error.message : `${action}失败`)
  }
}

function customerRowClassName({ row }: { row: Customer }) {
  return row.is_active ? '' : 'inactive-customer-row'
}

async function handleCustomerPageChange({ currentPage, pageSize }: { currentPage: number; pageSize: number }) {
  customerPage.page = currentPage
  customerPage.pageSize = pageSize
  await loadCurrent()
}

async function resetCustomerPageAndLoad() {
  customerPage.page = 1
  await loadCurrent()
}

onMounted(async () => {
  await loadDictionaries()
  await loadCurrent()
})
</script>

<template>
  <div class="tm-page">
    <section class="tm-card">
      <div class="tm-toolbar master-toolbar">
        <div>
          <h2 class="tm-section-title">基础资料</h2>
          <div class="tm-muted">客户、常用箱、材质统一维护；楞型资料待后端接口支持。</div>
        </div>
        <div class="toolbar-actions">
          <el-input
            v-model="keyword"
            clearable
            placeholder="输入客户、存货编码、品名、材质搜索"
            style="width: 340px"
            @keyup.enter="resetCustomerPageAndLoad"
            @clear="resetCustomerPageAndLoad"
          />
          <el-checkbox
            v-if="activeTab === 'customers'"
            v-model="showInactiveCustomers"
            @change="resetCustomerPageAndLoad"
          >
            显示已停用客户
          </el-checkbox>
          <el-button type="primary" @click="resetCustomerPageAndLoad">查询</el-button>
          <el-button @click="keyword = ''; resetCustomerPageAndLoad()">重置</el-button>
          <el-button v-if="activeTab !== 'flutes'" type="success" @click="openCreate()">
            新增{{ tabTitle[activeTab] }}
          </el-button>
        </div>
      </div>

      <el-tabs v-model="activeTab" class="master-tabs" @tab-change="loadCurrent">
        <el-tab-pane label="客户资料" name="customers">
          <vxe-table
            border stripe show-overflow="ellipsis" height="520" :loading="loading"
            :keyboard-config="{ isArrow: true, isEnter: true }"
            :row-config="{ isHover: true }"
            :row-class-name="customerRowClassName"
            :seq-config="{ startIndex: (customerPage.page - 1) * customerPage.pageSize }"
            :data="customers"
          >
            <vxe-column type="seq" width="70" title="序号" />
            <vxe-column field="customer_code" title="客户编码" width="140" />
            <vxe-column field="name" title="客户名称" :width="customerNameColumnWidth" />
            <vxe-column field="contact_person" title="联系人" width="130" />
            <vxe-column field="phone" title="电话" width="160" />
            <vxe-column field="delivery_method" title="送货方式" width="120" />
            <vxe-column title="操作" width="170" fixed="right">
              <template #default="{ row }">
                <el-button type="primary" link @click="openEdit(row)">编辑</el-button>
                <el-button :type="row.is_active ? 'danger' : 'success'" link @click="toggleCustomerStatus(row)">
                  {{ row.is_active ? '停用' : '启用' }}
                </el-button>
              </template>
            </vxe-column>
          </vxe-table>
          <vxe-pager
            v-model:current-page="customerPage.page"
            v-model:page-size="customerPage.pageSize"
            :page-sizes="[20, 50, 100]"
            :total="totals.customers"
            :layouts="['PrevPage', 'JumpNumber', 'NextPage', 'Sizes', 'FullJump', 'Total']"
            @page-change="handleCustomerPageChange"
          />
        </el-tab-pane>

        <el-tab-pane label="常用箱资料" name="products">
          <vxe-table
            border stripe show-overflow="ellipsis" height="520" :loading="loading"
            :keyboard-config="{ isArrow: true, isEnter: true }"
            :row-config="{ isHover: true }" :data="products"
          >
            <vxe-column type="seq" width="70" title="序号" />
            <vxe-column field="customer_name" title="客户" width="180" />
            <vxe-column field="product_code" title="存货编码" width="130" />
            <vxe-column field="product_name" title="产品名称" min-width="180" />
            <vxe-column field="material_code" title="材质" width="130" />
            <vxe-column field="flute_type_code" title="楞型" width="100" />
            <vxe-column title="规格" width="190">
              <template #default="{ row }">{{ row.length_mm }}×{{ row.width_mm }}×{{ row.height_mm }}</template>
            </vxe-column>
            <vxe-column field="default_score_line" title="压线" width="160" />
            <vxe-column field="default_unit_price" title="默认单价" width="120" />
            <vxe-column title="操作" width="190" fixed="right">
              <template #default="{ row }">
                <el-button type="primary" link @click="openEdit(row)">编辑</el-button>
                <el-button type="danger" link @click="disableRecord(row)">停用</el-button>
                <el-button type="warning" link @click="moveProductToTrash(row)">垃圾站</el-button>
              </template>
            </vxe-column>
          </vxe-table>
        </el-tab-pane>

        <el-tab-pane label="材质资料" name="materials">
          <vxe-table border stripe show-overflow="ellipsis" height="520" :loading="loading" :data="materials">
            <vxe-column type="seq" width="70" title="序号" />
            <vxe-column field="code" title="材质代码" width="140" />
            <vxe-column field="name" title="材质名称" min-width="180" />
            <vxe-column field="basis_weight_description" title="克重组合" min-width="240" />
            <vxe-column field="layer_count" title="层数" width="90" />
            <vxe-column field="flute_type_code" title="楞型" width="100" />
            <vxe-column field="customer_square_price" title="报价平方价" width="140" />
            <vxe-column title="操作" width="150" fixed="right">
              <template #default="{ row }">
                <el-button type="primary" link @click="openEdit(row)">编辑</el-button>
                <el-button type="danger" link @click="disableRecord(row)">停用</el-button>
              </template>
            </vxe-column>
          </vxe-table>
        </el-tab-pane>

        <el-tab-pane label="楞型资料" name="flutes">
          <el-empty description="后端暂未提供楞型资料接口（/api/master/flute-types），该页签待后端补充后启用">
            <template #image>
              <div class="empty-icon">▤</div>
            </template>
          </el-empty>
          <p class="tm-muted" style="text-align: center">
            楞型当前作为材质/产品的一个文本字段（flute_type_code）维护，不影响开单。
          </p>
        </el-tab-pane>
      </el-tabs>

      <div class="master-status">当前模块：{{ tabTitle[activeTab] }}，共 {{ totals[activeTab] }} 条</div>
    </section>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="920px">
      <el-form :model="form" label-width="112px" class="master-form">
        <template v-if="activeTab === 'customers'">
          <el-form-item label="客户编码"><el-input v-model="(form as any).customer_code" /></el-form-item>
          <el-form-item label="客户名称"><el-input v-model="(form as any).name" /></el-form-item>
          <el-form-item label="简称"><el-input v-model="(form as any).short_name" /></el-form-item>
          <el-form-item label="联系人"><el-input v-model="(form as any).contact_person" /></el-form-item>
          <el-form-item label="电话"><el-input v-model="(form as any).phone" /></el-form-item>
          <el-form-item label="账期"><el-input-number v-model="(form as any).payment_term_days" :min="0" /></el-form-item>
          <el-form-item label="送货方式">
            <el-select v-model="(form as any).delivery_method">
              <el-option label="配送" value="配送" /><el-option label="自提" value="自提" /><el-option label="物流" value="物流" />
            </el-select>
          </el-form-item>
          <el-form-item label="地址" class="wide"><el-input v-model="(form as any).address" /></el-form-item>
          <el-form-item label="备注" class="wide"><el-input v-model="(form as any).note" type="textarea" :rows="2" /></el-form-item>
        </template>

        <template v-else-if="activeTab === 'products'">
          <el-form-item label="客户">
            <el-select v-model="(form as any).customer_id" filterable>
              <el-option v-for="item in customers" :key="item.id" :label="item.name" :value="item.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="存货编码"><el-input v-model="(form as any).product_code" /></el-form-item>
          <el-form-item label="客户料号"><el-input v-model="(form as any).customer_material_code" /></el-form-item>
          <el-form-item label="产品名称"><el-input v-model="(form as any).product_name" /></el-form-item>
          <el-form-item label="材质">
            <el-select v-model="(form as any).material_id" filterable clearable>
              <el-option v-for="item in materials" :key="item.id" :label="`${item.code} ${item.name || ''}`" :value="item.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="长"><el-input v-model="(form as any).length_mm" /></el-form-item>
          <el-form-item label="宽"><el-input v-model="(form as any).width_mm" /></el-form-item>
          <el-form-item label="高"><el-input v-model="(form as any).height_mm" /></el-form-item>
          <el-form-item label="箱型"><el-input v-model="(form as any).box_style" /></el-form-item>
          <el-form-item label="工艺"><el-input v-model="(form as any).production_process" /></el-form-item>
          <el-form-item label="压线"><el-input v-model="(form as any).default_score_line" /></el-form-item>
          <el-form-item label="纸长"><el-input v-model="(form as any).default_cardboard_length_mm" /></el-form-item>
          <el-form-item label="纸宽"><el-input v-model="(form as any).default_cardboard_width_mm" /></el-form-item>
          <el-form-item label="默认单价"><el-input v-model="(form as any).default_unit_price" /></el-form-item>
          <el-form-item label="备注" class="wide"><el-input v-model="(form as any).note" /></el-form-item>
        </template>

        <template v-else>
          <el-form-item label="材质代码"><el-input v-model="(form as any).code" /></el-form-item>
          <el-form-item label="材质名称"><el-input v-model="(form as any).name" /></el-form-item>
          <el-form-item label="层数"><el-input-number v-model="(form as any).layer_count" :min="1" /></el-form-item>
          <el-form-item label="克重组合" class="wide"><el-input v-model="(form as any).basis_weight_description" /></el-form-item>
          <el-form-item label="报价平方价"><el-input v-model="(form as any).customer_square_price" /></el-form-item>
          <el-form-item label="采购平方价"><el-input v-model="(form as any).supplier_square_price" /></el-form-item>
          <el-form-item label="备注" class="wide"><el-input v-model="(form as any).note" /></el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitForm">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.master-toolbar {
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.toolbar-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 10px;
}

.master-tabs {
  margin-top: 14px;
  padding: 0 16px;
}

.master-status {
  margin-top: 10px;
  padding: 0 16px 14px;
  color: var(--tm-text-dim);
  font-weight: 700;
}

.master-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2px 14px;
}

.master-form :deep(.el-form-item) {
  margin-bottom: 14px;
}

.master-form :deep(.wide) {
  grid-column: 1 / -1;
}

.master-form :deep(.el-select),
.master-form :deep(.el-input-number) {
  width: 100%;
}

.empty-icon {
  font-size: 64px;
  color: var(--tm-accent-a);
  opacity: 0.6;
}

:deep(.inactive-customer-row) {
  color: var(--tm-text-faint);
}

:deep(.inactive-customer-row .vxe-body--column) {
  background: rgba(92, 111, 140, 0.08);
}
</style>
