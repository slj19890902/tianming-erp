<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError, financeApi, masterApi, type Customer, type PendingStatementItem, type Statement } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { businessDate, businessMonth } from '../utils/businessDate'

const authStore = useAuthStore()
const loading = ref(false)
const customers = ref<Customer[]>([])
const generateForm = ref({
  customer_id: null as number | null,
  statement_month: businessMonth(),
})
const pendingItems = ref<PendingStatementItem[]>([])
const selectedPendingIds = ref<number[]>([])
const loadedFor = ref('')
let pendingRequestSerial = 0
const createKey = ref(crypto.randomUUID())
const statementSaving = ref(false)
watch(() => [generateForm.value.customer_id, generateForm.value.statement_month], () => {
  pendingRequestSerial++
  pendingLoading.value = false
  loadedFor.value = ''
  pendingItems.value = []
  selectedPendingIds.value = []
  createKey.value = crypto.randomUUID()
})
const pendingLoading = ref(false)
const statements = ref<Statement[]>([])
const statementsTotal = ref(0)
const statementsPage = ref(1)
const statementsPageSize = 20
const settleDialog = ref(false)
const settlingStatement = ref<Statement | null>(null)
const settleForm = ref({ amount: '', settlement_date: businessDate(), account: '' })
const settleSaving = ref(false)
const settleKey = ref(crypto.randomUUID())

const canFinance = computed(() => authStore.hasPermission('finance.execute'))

async function searchCustomers(keyword: string) {
  try {
    const result = await masterApi.listCustomers(keyword, { pageSize: 20 })
    customers.value = result.items
  } catch {
    customers.value = []
  }
}

async function loadStatements() {
  loading.value = true
  try {
    const result = await financeApi.listStatements(statementsPage.value, statementsPageSize)
    statements.value = result.items
    statementsTotal.value = result.total
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '对账单加载失败')
  } finally {
    loading.value = false
  }
}

/** 第一步：拉取该客户可对账的回单明细（仅 confirmed 且未被使用的明细） */
async function loadPendingStatements() {
  if (!generateForm.value.customer_id) {
    ElMessage.warning('请先选择客户')
    return
  }
  pendingLoading.value = true
  const customerId = generateForm.value.customer_id
  const month = generateForm.value.statement_month
  const serial = ++pendingRequestSerial
  try {
    const result = await financeApi.listPendingStatements(customerId, month)
    if (serial !== pendingRequestSerial || customerId !== generateForm.value.customer_id || month !== generateForm.value.statement_month) return
    pendingItems.value = result.items
    selectedPendingIds.value = pendingItems.value.map((item) => item.return_receipt_item_id)
    loadedFor.value = `${customerId}:${month}`
  } catch (error) {
    if (serial !== pendingRequestSerial) return
    ElMessage.error(error instanceof Error ? error.message : '可对账明细加载失败')
  } finally {
    if (serial === pendingRequestSerial) pendingLoading.value = false
  }
}

function togglePendingSelect(id: number) {
  const index = selectedPendingIds.value.indexOf(id)
  if (index >= 0) selectedPendingIds.value.splice(index, 1)
  else selectedPendingIds.value.push(id)
}

const pendingTotal = computed(() =>
  pendingItems.value
    .filter((item) => selectedPendingIds.value.includes(item.return_receipt_item_id))
    .reduce((sum, item) => sum + (Number(item.receivable_amount) || 0), 0),
)

/** 第二步：生成对账单（statement_month 须为 YYYY-MM） */
async function generateStatement() {
  if (statementSaving.value || !canFinance.value) return
  if (!generateForm.value.customer_id) {
    ElMessage.warning('请先选择客户')
    return
  }
  if (loadedFor.value !== `${generateForm.value.customer_id}:${generateForm.value.statement_month}`) {
    ElMessage.warning('客户或月份已变化，请重新拉取可对账明细')
    return
  }
  const visibleIds = new Set(pendingItems.value.map((item) => item.return_receipt_item_id))
  if (selectedPendingIds.value.some((id) => !visibleIds.has(id))) {
    ElMessage.warning('所选明细已过期，请重新拉取')
    return
  }
  if (selectedPendingIds.value.length === 0) {
    ElMessage.warning('请至少勾选一条回单明细')
    return
  }
  const customerId = generateForm.value.customer_id
  const month = generateForm.value.statement_month
  const ids = [...selectedPendingIds.value]
  try {
    await ElMessageBox.confirm(
      `确认生成 ${month} 对账单（${ids.length} 条，合计 ￥${pendingTotal.value.toFixed(2)}）？`,
      '生成对账单',
      { type: 'warning', confirmButtonText: '确认生成', cancelButtonText: '取消' },
    )
    if (loadedFor.value !== `${customerId}:${month}` ||
        customerId !== generateForm.value.customer_id || month !== generateForm.value.statement_month ||
        ids.length !== selectedPendingIds.value.length ||
        ids.some((id) => !selectedPendingIds.value.includes(id))) {
      ElMessage.warning('客户、月份或选择已变化，请重新拉取并核对')
      return
    }
    statementSaving.value = true
    const statement = await financeApi.createStatement({
      customer_id: customerId,
      statement_month: month,
      idempotency_key: createKey.value,
      return_receipt_item_ids: ids,
    })
    ElMessage.success(`对账单已生成（${statement.statement_number}）`)
    selectedPendingIds.value = []
    pendingItems.value = []
    loadedFor.value = ''
    createKey.value = crypto.randomUUID()
    await loadStatements()
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof Error ? error.message : '生成对账单失败')
  } finally {
    statementSaving.value = false
  }
}

/** 第三步：导出——浏览器导航到导出 URL（cookie 自动携带），由后端返回文件下载 */
function exportStatement(statement: Statement) {
  window.location.href = financeApi.exportStatementUrl(statement.id)
}

/** 收款登记：累计 settled_amount，收满后状态自动变为 settled */
function openSettle(statement: Statement) {
  if (!canFinance.value) {
    ElMessage.warning('收款登记需要 admin 或 finance 角色')
    return
  }
  settlingStatement.value = statement
  settleKey.value = crypto.randomUUID()
  const remain = Number(statement.total_receivable || 0) - Number(statement.settled_amount || 0)
  settleForm.value = {
    amount: remain > 0 ? remain.toFixed(2) : '',
    settlement_date: businessDate(),
    account: '',
  }
  settleDialog.value = true
}

async function submitSettle() {
  if (!settlingStatement.value || settleSaving.value) return
  if (!settleForm.value.amount || Number(settleForm.value.amount) <= 0) {
    ElMessage.warning('请输入有效的收款金额')
    return
  }
  settleSaving.value = true
  try {
    await financeApi.settleStatement(settlingStatement.value.id, {
      amount: settleForm.value.amount,
      settlement_date: settleForm.value.settlement_date,
      account: settleForm.value.account || undefined,
      expected_version: settlingStatement.value.version,
      expected_ledger_version: settlingStatement.value.ledger_version,
      idempotency_key: settleKey.value,
    })
    ElMessage.success('收款已登记')
    settleDialog.value = false
    await loadStatements()
  } catch (error) {
    if (error instanceof ApiError && error.status === 0 && settlingStatement.value) {
      try {
        const latest = await financeApi.getStatement(settlingStatement.value.id)
        if (latest.ledger_version > settlingStatement.value.ledger_version) {
          ElMessage.warning('收款请求结果需核对：对账单版本已变化，请查看明细，勿重复提交')
          settleDialog.value = false
          await loadStatements()
          return
        }
      } catch { /* 维持原幂等标识，供安全重试 */ }
      ElMessage.warning('收款结果暂不确定；请先刷新查询，同一表单重试仍使用原请求标识')
      return
    }
    ElMessage.error(error instanceof Error ? error.message : '收款登记失败')
  } finally {
    settleSaving.value = false
  }
}

function statementStatusLabel(status: string) {
  return { unsettled: '未结清', settled: '已结清' }[status] || status
}

function statementStatusType(status: string) {
  return { unsettled: 'warning', settled: 'success' }[status] || 'info'
}

onMounted(async () => {
  await searchCustomers('')
  await loadStatements()
})
</script>

<template>
  <div class="tm-page">
    <section class="tm-card tm-section">
      <h2 class="tm-section-title">生成对账单</h2>
      <div class="tm-muted" style="margin-bottom: 12px">三步走：选择客户月份 → 勾选回单明细 → 生成；生成后可导出 Excel、登记收款。</div>
      <el-form label-width="90px" class="generate-form">
        <el-form-item label="客户">
          <el-select
            v-model="generateForm.customer_id"
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
        <el-form-item label="对账月份">
          <el-date-picker
            v-model="generateForm.statement_month"
            type="month"
            value-format="YYYY-MM"
            placeholder="选择月份"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label=" ">
          <el-button type="primary" :loading="pendingLoading" @click="loadPendingStatements">拉取可对账明细</el-button>
        </el-form-item>
      </el-form>

      <vxe-table
        v-if="pendingItems.length > 0"
        border
        stripe
        show-overflow="ellipsis"
        height="260"
        :loading="pendingLoading"
        :data="pendingItems"
      >
        <vxe-column width="52">
          <template #default="{ row }">
            <el-checkbox
              :model-value="selectedPendingIds.includes(row.return_receipt_item_id)"
              @change="togglePendingSelect(row.return_receipt_item_id)"
            />
          </template>
        </vxe-column>
        <vxe-column type="seq" width="60" title="序号" />
        <vxe-column field="delivery_number" title="送货单号" width="180" />
        <vxe-column field="order_number" title="订单号" width="160" />
        <vxe-column field="product_name" title="产品" min-width="190" />
        <vxe-column field="specification" title="规格" width="150" />
        <vxe-column field="actual_received_quantity" title="实收数" width="100" />
        <vxe-column field="unit_price" title="单价" width="110" />
        <vxe-column field="receivable_amount" title="应收金额" width="130" />
        <vxe-column field="actual_received_date" title="签收日期" width="130" />
      </vxe-table>

      <div v-if="pendingItems.length > 0" class="generate-foot">
        <span class="tm-mono tm-muted">已选 {{ selectedPendingIds.length }} 条，合计 ￥{{ pendingTotal.toFixed(2) }}</span>
        <el-button type="success" size="large" :disabled="!canFinance" :loading="statementSaving" @click="generateStatement">生成对账单</el-button>
      </div>
    </section>

    <section class="tm-card tm-section">
      <div class="tm-toolbar">
        <h2 class="tm-section-title">对账单记录</h2>
        <el-button type="primary" @click="loadStatements">刷新</el-button>
      </div>
      <vxe-table border stripe show-overflow="ellipsis" height="420" :loading="loading" :data="statements">
        <vxe-column type="seq" width="60" title="序号" />
        <vxe-column field="statement_number" title="对账单号" width="190" />
        <vxe-column field="customer_name" title="客户" width="170" />
        <vxe-column field="statement_month" title="月份" width="110" />
        <vxe-column field="total_receivable" title="应收总额" width="130" />
        <vxe-column field="settled_amount" title="已收金额" width="130" />
        <vxe-column title="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statementStatusType(row.status)">{{ statementStatusLabel(row.status) }}</el-tag>
          </template>
        </vxe-column>
        <vxe-column field="created_at" title="生成时间" width="170" />
        <vxe-column title="操作" width="170" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link @click="exportStatement(row)">导出</el-button>
            <el-button
              type="success"
              link
              :disabled="row.status === 'settled' || row.confirmation_status !== 'confirmed' || !canFinance"
              @click="openSettle(row)"
            >
              收款登记
            </el-button>
          </template>
        </vxe-column>
      </vxe-table>
      <el-pagination v-model:current-page="statementsPage" :page-size="statementsPageSize" :total="statementsTotal" layout="prev, pager, next, jumper, total" style="margin-top:12px" @current-change="loadStatements" />
    </section>

    <el-dialog v-model="settleDialog" :title="`收款登记 · ${settlingStatement?.statement_number}`" width="440px">
      <el-form label-width="90px">
        <el-form-item label="应收总额">
          <span class="tm-mono">￥{{ settlingStatement?.total_receivable }}</span>
        </el-form-item>
        <el-form-item label="已收金额">
          <span class="tm-mono">￥{{ settlingStatement?.settled_amount }}</span>
        </el-form-item>
        <el-form-item label="本次收款">
          <el-input v-model="settleForm.amount" placeholder="输入收款金额" />
        </el-form-item>
        <el-form-item label="收款日期">
          <el-date-picker v-model="settleForm.settlement_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" />
        </el-form-item>
        <el-form-item label="收款账户">
          <el-input v-model="settleForm.account" placeholder="选填" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="settleDialog = false">取消</el-button>
        <el-button type="primary" :loading="settleSaving" @click="submitSettle">确认收款</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.generate-form {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 2px 18px;
  max-width: 1080px;
}

.generate-form :deep(.el-form-item__label) {
  align-self: center;
}

.generate-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 12px;
  padding: 12px 4px 0;
}
</style>
