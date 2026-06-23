<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  masterDataApi,
  type Customer,
  type FluteType,
  type Material,
  type Product,
} from '../api/masterData'

type TabName = 'customers' | 'products' | 'materials' | 'flutes'
type EditingType = TabName

const activeTab = ref<TabName>('customers')
const loading = ref(false)
const keyword = ref('')
const dialogVisible = ref(false)
const editingType = ref<EditingType>('customers')
const editingId = ref<number | null>(null)
const showInactiveCustomers = ref(false)

const customers = ref<Customer[]>([])
const products = ref<Product[]>([])
const materials = ref<Material[]>([])
const flutes = ref<FluteType[]>([])

const totals = reactive<Record<TabName, number>>({
  customers: 0,
  products: 0,
  materials: 0,
  flutes: 0,
})
const customerPage = reactive({
  page: 1,
  pageSize: 50,
})

const form = reactive<Record<string, any>>({})

const tabTitle: Record<TabName, string> = {
  customers: '客户资料',
  products: '常用箱资料',
  materials: '材质资料',
  flutes: '楞型资料',
}

const dialogTitle = computed(() => `${editingId.value ? '编辑' : '新增'}${tabTitle[editingType.value]}`)
const longestCustomerNameLength = computed(() =>
  customers.value.reduce((max, item) => Math.max(max, Array.from(item.name || '').length), 4),
)
const customerNameColumnWidth = computed(() =>
  Math.max(180, Math.min(520, (longestCustomerNameLength.value + 2) * 18 + 28)),
)

function resetForm(payload: Record<string, any>) {
  Object.keys(form).forEach((key) => delete form[key])
  Object.assign(form, payload)
}

async function loadCurrent() {
  loading.value = true
  try {
    if (activeTab.value === 'customers') {
      let result = await masterDataApi.listCustomers(keyword.value, {
        includeInactive: showInactiveCustomers.value,
        page: customerPage.page,
        pageSize: customerPage.pageSize,
      })
      if (result.items.length === 0 && result.total > 0 && customerPage.page > 1) {
        customerPage.page -= 1
        result = await masterDataApi.listCustomers(keyword.value, {
          includeInactive: showInactiveCustomers.value,
          page: customerPage.page,
          pageSize: customerPage.pageSize,
        })
      }
      customers.value = result.items
      totals.customers = result.total
    } else if (activeTab.value === 'products') {
      const result = await masterDataApi.listProducts(keyword.value)
      products.value = result.items
      totals.products = result.total
    } else if (activeTab.value === 'materials') {
      const result = await masterDataApi.listMaterials(keyword.value)
      materials.value = result.items
      totals.materials = result.total
    } else {
      const result = await masterDataApi.listFluteTypes(keyword.value)
      flutes.value = result.items
      totals.flutes = result.total
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '基础资料加载失败')
  } finally {
    loading.value = false
  }
}

async function loadDictionaries() {
  const [customerResult, materialResult, fluteResult] = await Promise.all([
    masterDataApi.listCustomers('', { pageSize: 200 }),
    masterDataApi.listMaterials(''),
    masterDataApi.listFluteTypes(''),
  ])
  customers.value = customerResult.items
  totals.customers = customerResult.total
  materials.value = materialResult.items
  totals.materials = materialResult.total
  flutes.value = fluteResult.items
  totals.flutes = fluteResult.total
}

function openCreate(type = activeTab.value) {
  editingType.value = type
  editingId.value = null
  if (type === 'customers') {
    resetForm({
      customer_code: '',
      name: '',
      short_name: '',
      contact_person: '',
      phone: '',
      address: '',
      payment_term_days: 30,
      delivery_method: '配送',
      default_tax_rate: '0.13',
      note: '',
      is_active: true,
    })
  } else if (type === 'products') {
    resetForm({
      customer_id: customers.value[0]?.id,
      product_code: '',
      customer_material_code: '',
      product_name: '',
      material_id: materials.value[0]?.id,
      flute_type_id: flutes.value[0]?.id,
      length_mm: '',
      width_mm: '',
      height_mm: '',
      box_category: 'normal',
      box_style: '0201',
      production_process: '钉箱',
      default_score_line: '',
      default_cardboard_length_mm: '',
      default_cardboard_width_mm: '',
      default_unit_price: '',
      note: '',
      is_active: true,
    })
  } else if (type === 'materials') {
    resetForm({
      code: '',
      name: '',
      paper_composition: '',
      basis_weight_description: '',
      layer_count: 5,
      flute_type_id: flutes.value[0]?.id,
      customer_square_price: '',
      supplier_square_price: '',
      note: '',
      is_active: true,
    })
  } else {
    resetForm({
      code: '',
      name: '',
      add_width_mm: '0',
      basis_weight_gsm: '',
      freight_rate: '',
      loss_rate: '0',
      note: '',
      is_active: true,
    })
  }
  dialogVisible.value = true
}

function openEdit(type: EditingType, row: Record<string, any>) {
  editingType.value = type
  editingId.value = row.id
  resetForm({ ...row })
  dialogVisible.value = true
}

function cleanPayload(payload: Record<string, any>) {
  return Object.fromEntries(
    Object.entries(payload).map(([key, value]) => [key, value === '' ? null : value]),
  )
}

async function submitForm() {
  try {
    const payload = cleanPayload(form)
    if (editingType.value === 'customers') {
      editingId.value
        ? await masterDataApi.updateCustomer(editingId.value, payload)
        : await masterDataApi.createCustomer(payload)
    } else if (editingType.value === 'products') {
      editingId.value
        ? await masterDataApi.updateProduct(editingId.value, payload)
        : await masterDataApi.createProduct(payload)
    } else if (editingType.value === 'materials') {
      editingId.value
        ? await masterDataApi.updateMaterial(editingId.value, payload)
        : await masterDataApi.createMaterial(payload)
    } else {
      editingId.value
        ? await masterDataApi.updateFluteType(editingId.value, payload)
        : await masterDataApi.createFluteType(payload)
    }
    ElMessage.success('保存成功')
    dialogVisible.value = false
    await loadDictionaries()
    await loadCurrent()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存失败')
  }
}

async function disableRecord(type: EditingType, row: Record<string, any>) {
  try {
    await ElMessageBox.confirm(
      `确认停用“${row.name || row.product_name || row.code}”吗？停用后不会出现在新单选择列表中。`,
      '停用确认',
      { type: 'warning', confirmButtonText: '确认停用', cancelButtonText: '取消' },
    )
    if (type === 'customers') await masterDataApi.deleteCustomer(row.id)
    else if (type === 'products') await masterDataApi.deleteProduct(row.id)
    else if (type === 'materials') await masterDataApi.deleteMaterial(row.id)
    else await masterDataApi.deleteFluteType(row.id)
    ElMessage.success('已停用')
    await loadDictionaries()
    await loadCurrent()
  } catch (error) {
    if (error === 'cancel') return
    ElMessage.error(error instanceof Error ? error.message : '停用失败')
  }
}

async function toggleCustomerStatus(row: Customer) {
  const nextActive = !row.is_active
  const action = nextActive ? '启用' : '停用'
  try {
    await ElMessageBox.confirm(
      `确认${action}“${row.name}”吗？${nextActive ? '启用后可重新用于新业务。' : '停用后将立即从默认列表和新业务选择框中隐藏。'}`,
      `${action}确认`,
      { type: 'warning', confirmButtonText: `确认${action}`, cancelButtonText: '取消' },
    )
    await masterDataApi.updateCustomerStatus(row.id, nextActive)
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

async function refreshAll() {
  await loadDictionaries()
  await loadCurrent()
}

onMounted(refreshAll)
</script>

<template>
  <div class="tm-page master-page">
    <section class="tm-card">
      <div class="tm-toolbar master-toolbar">
        <div>
          <h2 class="tm-section-title">基础资料</h2>
          <div class="tm-muted">客户、常用箱、材质、楞型统一维护；数据来自 Phase 3 FastAPI。</div>
        </div>
        <div class="toolbar-actions">
          <el-input
            v-model="keyword"
            clearable
            placeholder="输入客户、存货编码、品名、材质搜索"
            style="width: 360px"
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
          <el-button type="success" @click="openCreate()">新增{{ tabTitle[activeTab] }}</el-button>
        </div>
      </div>

      <el-tabs v-model="activeTab" class="master-tabs" @tab-change="loadCurrent">
        <el-tab-pane label="客户资料" name="customers">
          <vxe-table
            border
            stripe
            show-overflow="ellipsis"
            height="520"
            :loading="loading"
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
                <el-button type="primary" link @click="openEdit('customers', row)">编辑</el-button>
                <el-button
                  :type="row.is_active ? 'danger' : 'success'"
                  link
                  @click="toggleCustomerStatus(row)"
                >
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
            border
            stripe
            show-overflow="ellipsis"
            height="520"
            :loading="loading"
            :keyboard-config="{ isArrow: true, isEnter: true }"
            :row-config="{ isHover: true }"
            :data="products"
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
            <vxe-column title="操作" width="150" fixed="right">
              <template #default="{ row }">
                <el-button type="primary" link @click="openEdit('products', row)">编辑</el-button>
                <el-button type="danger" link @click="disableRecord('products', row)">停用</el-button>
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
                <el-button type="primary" link @click="openEdit('materials', row)">编辑</el-button>
                <el-button type="danger" link @click="disableRecord('materials', row)">停用</el-button>
              </template>
            </vxe-column>
          </vxe-table>
        </el-tab-pane>

        <el-tab-pane label="楞型资料" name="flutes">
          <vxe-table border stripe show-overflow="ellipsis" height="520" :loading="loading" :data="flutes">
            <vxe-column type="seq" width="70" title="序号" />
            <vxe-column field="code" title="楞型代码" width="140" />
            <vxe-column field="name" title="楞型名称" min-width="180" />
            <vxe-column field="add_width_mm" title="加放宽度" width="140" />
            <vxe-column field="basis_weight_gsm" title="基准克重" width="140" />
            <vxe-column field="freight_rate" title="运费系数" width="140" />
            <vxe-column title="操作" width="150" fixed="right">
              <template #default="{ row }">
                <el-button type="primary" link @click="openEdit('flutes', row)">编辑</el-button>
                <el-button type="danger" link @click="disableRecord('flutes', row)">停用</el-button>
              </template>
            </vxe-column>
          </vxe-table>
        </el-tab-pane>
      </el-tabs>

      <div class="master-status">当前模块：{{ tabTitle[activeTab] }}，共 {{ totals[activeTab] }} 条</div>
    </section>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="920px" class="master-dialog">
      <el-form :model="form" label-width="112px" class="master-form">
        <template v-if="editingType === 'customers'">
          <el-form-item label="客户编码"><el-input v-model="form.customer_code" /></el-form-item>
          <el-form-item label="客户名称"><el-input v-model="form.name" /></el-form-item>
          <el-form-item label="简称"><el-input v-model="form.short_name" /></el-form-item>
          <el-form-item label="联系人"><el-input v-model="form.contact_person" /></el-form-item>
          <el-form-item label="电话"><el-input v-model="form.phone" /></el-form-item>
          <el-form-item label="账期"><el-input-number v-model="form.payment_term_days" :min="0" /></el-form-item>
          <el-form-item label="送货方式">
            <el-select v-model="form.delivery_method"><el-option label="配送" value="配送" /><el-option label="自提" value="自提" /><el-option label="物流" value="物流" /></el-select>
          </el-form-item>
          <el-form-item label="地址" class="wide"><el-input v-model="form.address" /></el-form-item>
          <el-form-item label="备注" class="wide"><el-input v-model="form.note" type="textarea" :rows="2" /></el-form-item>
        </template>

        <template v-else-if="editingType === 'products'">
          <el-form-item label="客户">
            <el-select v-model="form.customer_id" filterable>
              <el-option v-for="item in customers" :key="item.id" :label="item.name" :value="item.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="存货编码"><el-input v-model="form.product_code" /></el-form-item>
          <el-form-item label="客户料号"><el-input v-model="form.customer_material_code" /></el-form-item>
          <el-form-item label="产品名称"><el-input v-model="form.product_name" /></el-form-item>
          <el-form-item label="材质">
            <el-select v-model="form.material_id" filterable clearable>
              <el-option v-for="item in materials" :key="item.id" :label="`${item.code} ${item.name || ''}`" :value="item.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="楞型">
            <el-select v-model="form.flute_type_id" filterable clearable>
              <el-option v-for="item in flutes" :key="item.id" :label="`${item.code} ${item.name}`" :value="item.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="长"><el-input v-model="form.length_mm" /></el-form-item>
          <el-form-item label="宽"><el-input v-model="form.width_mm" /></el-form-item>
          <el-form-item label="高"><el-input v-model="form.height_mm" /></el-form-item>
          <el-form-item label="箱型"><el-input v-model="form.box_style" /></el-form-item>
          <el-form-item label="工艺"><el-input v-model="form.production_process" /></el-form-item>
          <el-form-item label="压线"><el-input v-model="form.default_score_line" /></el-form-item>
          <el-form-item label="纸长"><el-input v-model="form.default_cardboard_length_mm" /></el-form-item>
          <el-form-item label="纸宽"><el-input v-model="form.default_cardboard_width_mm" /></el-form-item>
          <el-form-item label="默认单价"><el-input v-model="form.default_unit_price" /></el-form-item>
          <el-form-item label="备注" class="wide"><el-input v-model="form.note" /></el-form-item>
        </template>

        <template v-else-if="editingType === 'materials'">
          <el-form-item label="材质代码"><el-input v-model="form.code" /></el-form-item>
          <el-form-item label="材质名称"><el-input v-model="form.name" /></el-form-item>
          <el-form-item label="层数"><el-input-number v-model="form.layer_count" :min="1" /></el-form-item>
          <el-form-item label="楞型">
            <el-select v-model="form.flute_type_id" filterable clearable>
              <el-option v-for="item in flutes" :key="item.id" :label="`${item.code} ${item.name}`" :value="item.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="克重组合" class="wide"><el-input v-model="form.basis_weight_description" /></el-form-item>
          <el-form-item label="报价平方价"><el-input v-model="form.customer_square_price" /></el-form-item>
          <el-form-item label="采购平方价"><el-input v-model="form.supplier_square_price" /></el-form-item>
          <el-form-item label="备注" class="wide"><el-input v-model="form.note" /></el-form-item>
        </template>

        <template v-else>
          <el-form-item label="楞型代码"><el-input v-model="form.code" /></el-form-item>
          <el-form-item label="楞型名称"><el-input v-model="form.name" /></el-form-item>
          <el-form-item label="加放宽度"><el-input v-model="form.add_width_mm" /></el-form-item>
          <el-form-item label="基准克重"><el-input v-model="form.basis_weight_gsm" /></el-form-item>
          <el-form-item label="运费系数"><el-input v-model="form.freight_rate" /></el-form-item>
          <el-form-item label="损耗率"><el-input v-model="form.loss_rate" /></el-form-item>
          <el-form-item label="备注" class="wide"><el-input v-model="form.note" /></el-form-item>
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
}

.master-status {
  margin-top: 10px;
  color: #34506d;
  font-weight: 800;
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

:deep(.vxe-header--column) {
  background: #eaf2fb;
  color: #12385f;
  font-size: 17px;
  font-weight: 900;
}

:deep(.vxe-body--column) {
  font-size: 17px;
}

:deep(.inactive-customer-row) {
  color: #8a8f98;
  background: #f1f2f4;
}

:deep(.inactive-customer-row .vxe-body--column) {
  background: #f1f2f4;
}
</style>
