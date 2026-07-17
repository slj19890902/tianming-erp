<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { masterDataApi, type Customer, type Statement } from '../api/masterData'
import { beijingToday, formatBusinessDate } from '../utils/time'

const loading = ref(false)
const customers = ref<Customer[]>([])
const selectedCustomerId = ref<number>()
const currentStatement = ref<Statement | null>(null)

function defaultPeriod() {
  const month = beijingToday().slice(0, 7)
  return {
    start_date: formatBusinessDate(month + '-01'),
    end_date: formatBusinessDate(month + '-20'),
  }
}

const period = ref(defaultPeriod())

const totalQuantity = computed(() => currentStatement.value?.total_quantity || 0)
const totalAmount = computed(() => currentStatement.value?.total_amount || '0.00')

async function loadCustomers() {
  const result = await masterDataApi.listCustomers()
  customers.value = result.items
  if (!selectedCustomerId.value && customers.value.length) {
    selectedCustomerId.value = customers.value[0].id
  }
}

async function generateAndExport() {
  if (!selectedCustomerId.value) {
    ElMessage.warning('请先选择客户')
    return
  }
  loading.value = true
  try {
    const statement = await masterDataApi.generateStatement({
      customer_id: selectedCustomerId.value,
      start_date: period.value.start_date,
      end_date: period.value.end_date,
    })
    currentStatement.value = statement
    ElMessage.success(`对账单已生成：${statement.statement_number}`)
    const link = document.createElement('a')
    link.href = masterDataApi.exportStatementUrl(statement.id)
    link.download = `${statement.statement_number}.xlsx`
    document.body.appendChild(link)
    link.click()
    link.remove()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '生成对账单失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadCustomers)
</script>

<template>
  <section class="tm-page">
    <div class="tm-page-head">
      <div>
        <h1>每月20号月结对账中心</h1>
        <p>只汇总“老板已核对”的回单，金额按客户实际签收数量计算。</p>
      </div>
      <el-button type="primary" :loading="loading" @click="generateAndExport">生成并导出对账清单</el-button>
    </div>

    <section class="filter-card">
      <el-select v-model="selectedCustomerId" filterable placeholder="选择客户">
        <el-option v-for="customer in customers" :key="customer.id" :label="customer.name" :value="customer.id" />
      </el-select>
      <el-date-picker v-model="period.start_date" value-format="YYYY-MM-DD" type="date" placeholder="开始日期" />
      <el-date-picker v-model="period.end_date" value-format="YYYY-MM-DD" type="date" placeholder="结束日期" />
      <el-tag size="large" type="warning">默认周期截止每月20号</el-tag>
    </section>

    <section class="total-card">
      <div>
        <span>总解缴箱数</span>
        <strong>{{ totalQuantity }}</strong>
      </div>
      <div>
        <span>对账总金额</span>
        <strong>￥{{ totalAmount }}</strong>
      </div>
    </section>

    <vxe-table border stripe height="540" :data="currentStatement?.items || []">
      <vxe-column field="delivery_date" title="送货日期" width="130" />
      <vxe-column field="delivery_number" title="送货单号" width="180" />
      <vxe-column field="order_number" title="订单号" width="180" />
      <vxe-column field="customer_po" title="客户单号" width="150" />
      <vxe-column field="product_code" title="品号" width="120" />
      <vxe-column field="product_name" title="产品名称" min-width="180" />
      <vxe-column field="spec" title="规格" width="160" />
      <vxe-column field="actual_signed_qty" title="回单确认数量" width="140" />
      <vxe-column field="unit_price" title="单价" width="110" />
      <vxe-column field="amount" title="金额" width="120" />
    </vxe-table>
  </section>
</template>

<style scoped>
.tm-page {
  display: grid;
  gap: 14px;
}
.tm-page-head,
.filter-card,
.total-card {
  padding: 16px 18px;
  background: #fff;
  border: 1px solid #9fb7d2;
}
.tm-page-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
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
.filter-card {
  display: flex;
  gap: 12px;
  align-items: center;
}
.filter-card .el-select {
  width: 260px;
}
.total-card {
  display: grid;
  grid-template-columns: repeat(2, 240px);
  gap: 18px;
}
.total-card div {
  padding: 14px;
  color: #12385f;
  background: #eef6ff;
  border: 1px solid #8aa8c8;
}
.total-card span {
  display: block;
  font-weight: 800;
}
.total-card strong {
  display: block;
  margin-top: 6px;
  font-size: 30px;
}
</style>
