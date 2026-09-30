<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { incomingApi, type IncomingItem } from '../api/client'

const loading = ref(false)
const pendingItems = ref<IncomingItem[]>([])
const receivedItems = ref<IncomingItem[]>([])
const confirmingId = ref<number | null>(null)

async function loadAll() {
  loading.value = true
  try {
    const [pending, received] = await Promise.all([
      incomingApi.listPending(),
      incomingApi.listReceived(),
    ])
    pendingItems.value = pending.items
    receivedItems.value = received.items
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '收料数据加载失败')
  } finally {
    loading.value = false
  }
}

/** 确认收料：后端整单确认（PUT /api/incoming/receive/{item_id}，无 body） */
async function confirmReceive(item: IncomingItem) {
  confirmingId.value = item.item_id
  try {
    await incomingApi.receiveItem(item.item_id)
    ElMessage.success('收料已确认')
    await loadAll()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '确认收料失败')
  } finally {
    confirmingId.value = null
  }
}

/** 撤销收料：PUT /api/incoming/revert/{item_id} */
async function revertReceive(item: IncomingItem) {
  try {
    await incomingApi.revertItem(item.item_id)
    ElMessage.success('已撤销该条收料')
    await loadAll()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '撤销失败')
  }
}

const totalPendingQty = computed(() =>
  pendingItems.value.reduce((sum: number, item: IncomingItem) => sum + (Number(item.quantity) || 0), 0),
)

onMounted(loadAll)
</script>

<template>
  <div class="mobile-page">
    <header class="mobile-header">
      <div class="brand-logo">天</div>
      <div>
        <h1>移动收料 <span class="v2-badge">v2</span></h1>
        <p class="tm-muted">天明包装 ERP · 仓管专用</p>
      </div>
      <el-button class="refresh-btn" type="primary" circle @click="loadAll">↻</el-button>
    </header>

    <main class="mobile-main" v-loading="loading">
      <section class="stat-row">
        <div class="tm-kpi">
          <div class="kpi-label">待收料任务</div>
          <div class="kpi-value">{{ pendingItems.length }}</div>
        </div>
        <div class="tm-kpi">
          <div class="kpi-label">待收数量（片）</div>
          <div class="kpi-value">{{ totalPendingQty }}</div>
        </div>
      </section>

      <section>
        <h2 class="tm-section-title">待收料</h2>
        <div v-if="pendingItems.length === 0" class="empty-card tm-card tm-muted">
          暂无待收料任务，纸板到厂后会在此显示。
        </div>
        <div v-for="item in pendingItems" :key="item.item_id" class="tm-card receive-card">
          <div class="receive-head">
            <strong>{{ item.product_name || item.order_number || `任务 #${item.item_id}` }}</strong>
            <el-tag type="warning">待收</el-tag>
          </div>
          <div class="receive-meta">
            <span>客户：{{ item.customer_name || '—' }}</span>
            <span>数量：<strong class="tm-mono">{{ item.quantity ?? '—' }}</strong></span>
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
            @click="confirmReceive(item)"
          >
            确认收料
          </el-button>
        </div>
      </section>

      <section>
        <h2 class="tm-section-title">已收料</h2>
        <div class="tm-muted" style="margin-bottom: 10px">后端仅返回近 24 小时已收料记录。</div>
        <div v-if="receivedItems.length === 0" class="empty-card tm-card tm-muted">暂无收料记录。</div>
        <div v-for="item in receivedItems.slice(0, 20)" :key="item.item_id" class="tm-card received-card">
          <div class="receive-head">
            <strong>{{ item.product_name || item.order_number || `任务 #${item.item_id}` }}</strong>
            <el-tag type="success">已收</el-tag>
          </div>
          <div class="receive-meta tm-muted">
            <span>数量：{{ item.quantity ?? '—' }}</span>
            <span>{{ item.material_received_at || '' }}</span>
            <span v-if="item.received_by_name">经手：{{ item.received_by_name }}</span>
          </div>
          <el-button type="warning" link @click="revertReceive(item)">撤销收料</el-button>
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
  color: #f0f6ff;
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
  color: #04121f;
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
  color: #04121f;
  background: var(--tm-accent-gradient);
}

.refresh-btn {
  margin-left: auto;
  width: 44px;
  height: 44px;
  font-size: 20px;
}

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
  color: #f0f6ff;
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
