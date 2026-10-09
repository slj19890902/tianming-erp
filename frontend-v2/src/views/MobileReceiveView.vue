<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { incomingApi, type IncomingItem } from '../api/client'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useTabsStore } from '../stores/tabs'

const router = useRouter()
const auth = useAuthStore()
const tabs = useTabsStore()

const loading = ref(false)
const dataReady = ref(false)
const loadError = ref('')
let loadRequest = 0
let disposed = false
const pendingItems = ref<IncomingItem[]>([])
const receivedItems = ref<IncomingItem[]>([])
const confirmingId = ref<number | null>(null)

async function loadAll() {
  if (disposed) return
  const request = ++loadRequest
  loading.value = true
  dataReady.value = false
  loadError.value = ''
  try {
    const [pending, received] = await Promise.all([
      incomingApi.listPending(),
      incomingApi.listReceived(),
    ])
    if (request !== loadRequest) return
    pendingItems.value = pending.items
    receivedItems.value = received.items
    dataReady.value = true
  } catch (error) {
    if (request !== loadRequest) return
    loadError.value = (error instanceof Error ? error.message : '') || '收料数据加载失败'
    ElMessage.error(loadError.value)
  } finally {
    if (request === loadRequest) loading.value = false
  }
}

/** 确认收料：后端整单确认（PUT /api/incoming/receive/{item_id}，无 body） */
async function confirmReceive(item: IncomingItem) {
  if (disposed || !dataReady.value || !auth.hasPermission('incoming.execute') || confirmingId.value !== null) return
  confirmingId.value = item.item_id
  try {
    await incomingApi.receiveItem(item.item_id)
    if (disposed) return
    ElMessage.success('收料已确认')
    await loadAll()
  } catch (error) {
    if (disposed) return
    ElMessage.error(error instanceof Error ? error.message : '确认收料失败')
  } finally {
    if (!disposed) confirmingId.value = null
  }
}

/** 撤销收料：PUT /api/incoming/revert/{item_id} */
async function revertReceive(item: IncomingItem) {
  if (disposed || !dataReady.value || !auth.hasPermission('incoming.execute')) return
  try {
    await incomingApi.revertItem(item.item_id)
    if (disposed) return
    ElMessage.success('已撤销该条收料')
    await loadAll()
  } catch (error) {
    if (disposed) return
    ElMessage.error(error instanceof Error ? error.message : '撤销失败')
  }
}

const totalPendingQty = computed(() =>
  pendingItems.value.reduce((sum: number, item: IncomingItem) => sum + (Number(item.remaining_quantity ?? item.incoming_quantity ?? 0) || 0), 0),
)

async function logout() {
  await auth.logout()
  tabs.reset()
  await router.replace('/login')
}

onMounted(loadAll)
onUnmounted(() => {
  disposed = true
  loadRequest++
})
</script>

<template>
  <div class="mobile-page">
    <header class="mobile-header">
      <div class="brand-logo">天</div>
      <div>
        <h1>移动收料 <span class="v2-badge">v2</span></h1>
        <p class="tm-muted">天明包装 ERP · 仓管专用</p>
      </div>
      <div class="mobile-actions">
        <span class="tm-muted">{{ auth.displayName() }}</span>
        <el-button link @click="router.push('/')">返回工作台</el-button>
        <el-button link @click="logout">退出</el-button>
        <el-button class="refresh-btn" type="primary" circle @click="loadAll">↻</el-button>
      </div>
    </header>

    <main class="mobile-main" v-loading="loading">
      <section v-if="loadError" class="tm-card tm-section" role="alert">
        <el-alert type="error" show-icon :closable="false" title="收料数据加载失败" :description="loadError" />
        <p class="tm-muted">当前数量暂不可用，旧记录需重新加载后核对。</p>
        <el-button type="primary" @click="loadAll">重新加载</el-button>
      </section>
      <p v-else-if="!dataReady" class="tm-muted" role="status">正在加载收料数据，数量暂不可用。</p>
      <section class="stat-row">
        <div class="tm-kpi">
          <div class="kpi-label">待收料任务</div>
          <div class="kpi-value">{{ dataReady ? pendingItems.length : '—' }}</div>
        </div>
        <div class="tm-kpi">
          <div class="kpi-label">待收纸板（张）</div>
          <div class="kpi-value">{{ dataReady ? totalPendingQty : '—' }}</div>
        </div>
      </section>

      <section>
        <h2 class="tm-section-title">待收料</h2>
        <div v-if="dataReady && pendingItems.length === 0" class="empty-card tm-card tm-muted">
          暂无待收料任务，纸板到厂后会在此显示。
        </div>
        <div v-for="item in pendingItems" :key="item.item_id" class="tm-card receive-card">
          <div class="receive-head">
            <strong>{{ item.product_name || item.order_number || `任务 #${item.item_id}` }}</strong>
            <el-tag type="warning">待收</el-tag>
          </div>
          <div class="receive-meta">
            <span>客户：{{ item.customer_name || '—' }}</span>
            <span>订单：<strong class="tm-mono">{{ item.quantity ?? '—' }} {{ item.order_unit_label || '单位待完善' }}</strong></span>
            <span>报料：<strong class="tm-mono">{{ item.planned_quantity ?? item.requisition_qty ?? '—' }} 张</strong></span>
            <span>已收：{{ item.cumulative_received_quantity ?? 0 }} 张</span>
            <span>待收：<strong class="tm-mono">{{ item.remaining_quantity ?? item.incoming_quantity ?? '—' }} 张</strong></span>
            <span>纸板规格：{{ item.requisition_spec || '—' }}</span>
            <span>供应商：{{ item.snapshot_supplier_name || '—' }}</span>
            <span>材质：{{ item.material || '—' }}</span>
          </div>
          <div class="receive-meta tm-muted tm-mono">
            {{ item.delivery_date || '' }}
          </div>
          <el-button
            type="success"
            size="large"
            class="receive-btn"
            :loading="confirmingId === item.item_id"
            :disabled="!dataReady || !auth.hasPermission('incoming.execute')"
            @click="confirmReceive(item)"
          >
            确认收料
          </el-button>
          <a class="legacy-receive" href="/mobile/erp.html" target="_blank" rel="noopener">部分到货或异常：打开现版收料流程</a>
        </div>
      </section>

      <section>
        <h2 class="tm-section-title">已收料</h2>
        <div class="tm-muted" style="margin-bottom: 10px">后端仅返回近 24 小时已收料记录。</div>
        <div v-if="dataReady && receivedItems.length === 0" class="empty-card tm-card tm-muted">暂无收料记录。</div>
        <div v-for="item in receivedItems.slice(0, 20)" :key="item.item_id" class="tm-card received-card">
          <div class="receive-head">
            <strong>{{ item.product_name || item.order_number || `任务 #${item.item_id}` }}</strong>
            <el-tag type="success">已收</el-tag>
          </div>
          <div class="receive-meta tm-muted">
            <span>订单：{{ item.quantity ?? '—' }} {{ item.order_unit_label || '单位待完善' }}</span>
            <span>本次实收：{{ item.incoming_quantity ?? '—' }} 张</span>
            <span>{{ item.material_received_at || '' }}</span>
            <span v-if="item.received_by_name">经手：{{ item.received_by_name }}</span>
          </div>
          <el-button type="warning" link :disabled="!dataReady || !auth.hasPermission('incoming.execute')" @click="revertReceive(item)">撤销收料</el-button>
        </div>
      </section>

      <section class="tm-card tm-section">
        <h2 class="tm-section-title">工序流转</h2>
        <el-alert
          type="info"
          show-icon
          :closable="false"
          title="后端暂未提供工序流转接口，开始/完成工序按钮待后端补充后启用。"
        />
      </section>
    </main>
  </div>
</template>

<style scoped>
.mobile-page {
  min-height: 100vh;
  max-width: 720px;
  margin: 0 auto;
  padding: 0 12px 32px;
}

.mobile-header {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 16px 4px;
}

.mobile-header h1 {
  margin: 0;
  font-size: 19px;
  color: var(--tm-title);
  display: flex;
  align-items: center;
  gap: 8px;
}

.mobile-header p {
  margin: 2px 0 0;
  font-size: 12px;
}

.brand-logo {
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  color: var(--tm-on-accent);
  background: var(--tm-accent-gradient);
  box-shadow: var(--tm-glow);
  font-size: 24px;
  font-weight: 900;
  flex-shrink: 0;
}

.v2-badge {
  font-size: 11px;
  font-weight: 800;
  padding: 2px 8px;
  border-radius: 20px;
  color: var(--tm-on-accent);
  background: var(--tm-accent-gradient);
}

.refresh-btn {
  width: 44px;
  height: 44px;
  font-size: 20px;
}

.mobile-actions { margin-left: auto; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }

.stat-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 4px;
}

.receive-card {
  padding: 16px;
  margin-bottom: 12px;
}

.receive-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.receive-head strong {
  font-size: 16px;
  color: var(--tm-title);
}

.receive-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  font-size: 14px;
  color: var(--tm-text-dim);
  margin-bottom: 12px;
}

.receive-btn {
  width: 100%;
  min-height: 52px;
  font-size: 17px;
  font-weight: 800;
  letter-spacing: 4px;
}
.legacy-receive { display: inline-block; margin-top: 9px; color: var(--tm-accent-b); font-size: 13px; }

.received-card {
  padding: 12px 16px;
  margin-bottom: 8px;
  opacity: 0.85;
}

.empty-card {
  padding: 24px;
  text-align: center;
  margin-bottom: 12px;
}
</style>
