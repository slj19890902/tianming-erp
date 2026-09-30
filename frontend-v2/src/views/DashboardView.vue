<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { dashboardApi, type DashboardKpi } from '../api/client'

const router = useRouter()
const loading = ref(false)
const kpi = ref<DashboardKpi | null>(null)

/** 业务流程节点：以后端 KPI 口径映射，未覆盖的节点显示为待接入 */
const flowNodes = ref([
  { label: '客户订单', key: '' },
  { label: '报料', key: '' },
  { label: '纸板入仓', key: 'today_pending_incoming_tasks' },
  { label: '生产', key: '' },
  { label: '送货', key: 'today_pending_delivery_tasks' },
  { label: '回单', key: '' },
  { label: '对账', key: '' },
  { label: '收款', key: 'outstanding_receivables' },
])

function nodeValue(key: string): string {
  if (!kpi.value || !key) return '—'
  const value = (kpi.value as unknown as Record<string, string | number>)[key]
  return value === undefined || value === null ? '—' : String(value)
}

const quickReports = [
  { label: '纸板实时库存', path: '/mobile-receive' },
  { label: '今日计划到货', path: '/mobile-receive' },
  { label: '计划需要出货', path: '/deliveries' },
  { label: '客户应收总表', path: '/statements' },
]

async function loadKpi() {
  loading.value = true
  try {
    kpi.value = await dashboardApi.kpi()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'KPI 加载失败')
  } finally {
    loading.value = false
  }
}

function go(path: string) {
  void router.push(path)
}

onMounted(loadKpi)
</script>

<template>
  <div class="tm-page" v-loading="loading">
    <div class="dashboard-grid">
      <section class="tm-card side-panel">
        <h2 class="tm-section-title">交期与异常</h2>
        <div class="alert-row warn">
          <span>今日待送货任务</span>
          <strong class="tm-mono">{{ kpi?.today_pending_delivery_tasks ?? '—' }}</strong>
        </div>
        <div class="alert-row warn">
          <span>今日待收料任务</span>
          <strong class="tm-mono">{{ kpi?.today_pending_incoming_tasks ?? '—' }}</strong>
        </div>
        <div class="alert-row danger">
          <span>未收应收账款</span>
          <strong class="tm-mono">￥{{ kpi?.outstanding_receivables ?? '—' }}</strong>
        </div>
        <div class="tm-muted" style="margin-top: 12px">数据来源：/api/dashboard/kpi（{{ kpi?.month || '—' }}）</div>
      </section>

      <section class="tm-card flow-panel">
        <div class="tm-toolbar">
          <h2 class="tm-section-title">纸箱厂业务流程</h2>
          <div style="margin-left: auto; display: flex; gap: 8px">
            <el-button type="primary" @click="loadKpi">刷新</el-button>
          </div>
        </div>
        <div class="flow-map">
          <template v-for="(node, index) in flowNodes" :key="node.label">
            <button class="flow-node" type="button" @click="go('/orders')">
              <span>{{ node.label }}</span>
              <strong class="tm-mono">{{ nodeValue(node.key) }}</strong>
            </button>
            <div v-if="index < flowNodes.length - 1" class="flow-arrow">→</div>
          </template>
        </div>
      </section>

      <section class="tm-card side-panel">
        <h2 class="tm-section-title">常用报表</h2>
        <el-button
          v-for="report in quickReports"
          :key="report.label"
          class="quick-button"
          type="primary"
          plain
          @click="go(report.path)"
        >
          {{ report.label }}
        </el-button>
      </section>
    </div>

    <section class="kpi-row">
      <div class="tm-kpi">
        <div class="kpi-label">本月营收（回单确认）</div>
        <div class="kpi-value">￥{{ kpi?.monthly_revenue ?? '—' }}</div>
      </div>
      <div class="tm-kpi">
        <div class="kpi-label">本月毛利（对账单）</div>
        <div class="kpi-value">￥{{ kpi?.monthly_gross_profit ?? '—' }}</div>
      </div>
      <div class="tm-kpi warn">
        <div class="kpi-label">未结应收</div>
        <div class="kpi-value">￥{{ kpi?.outstanding_receivables ?? '—' }}</div>
      </div>
      <div class="tm-kpi">
        <div class="kpi-label">统计月份</div>
        <div class="kpi-value" style="font-size: 22px">{{ kpi?.month ?? '—' }}</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.dashboard-grid {
  display: grid;
  grid-template-columns: 280px 1fr 260px;
  gap: 14px;
}

.side-panel {
  padding: 18px;
}

.alert-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  min-height: 58px;
  margin-top: 10px;
  padding: 0 14px;
  border-radius: 8px;
  border: 1px solid var(--tm-line-strong);
  background: rgba(251, 191, 36, 0.07);
  font-size: 15px;
  font-weight: 700;
}

.alert-row strong {
  font-size: 22px;
}

.alert-row.warn strong {
  color: var(--tm-warn);
}

.alert-row.danger {
  background: rgba(248, 113, 113, 0.07);
}

.alert-row.danger strong {
  color: var(--tm-danger);
  font-size: 18px;
}

.flow-panel {
  min-height: 280px;
}

.flow-map {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  padding: 24px;
}

.flow-node {
  width: 128px;
  height: 96px;
  display: grid;
  place-items: center;
  gap: 2px;
  border: 1px solid rgba(34, 211, 238, 0.4);
  border-radius: 10px;
  background: linear-gradient(160deg, rgba(34, 211, 238, 0.1), rgba(59, 130, 246, 0.06));
  color: #dbe7f5;
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  transition: all 0.16s ease;
}

.flow-node:hover {
  box-shadow: var(--tm-glow);
  transform: translateY(-2px);
}

.flow-node strong {
  font-size: 24px;
  background: var(--tm-accent-gradient);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}

.flow-arrow {
  color: var(--tm-accent-a);
  font-size: 22px;
  font-weight: 900;
  opacity: 0.7;
}

.quick-button {
  width: 100%;
  margin: 10px 0 0;
  min-height: 48px;
  font-size: 15px;
  font-weight: 700;
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
}
</style>
