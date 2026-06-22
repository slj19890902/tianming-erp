<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { masterDataApi, type WmsPendingItem, type WmsProcessPanel } from '../api/masterData'

const loading = ref(false)
const items = ref<WmsPendingItem[]>([])
const activeItem = ref<WmsPendingItem | null>(null)
const processPanel = ref<WmsProcessPanel | null>(null)
const receiveDialog = ref(false)
const receiveQty = ref(0)
const receivedBy = ref('车间收料员')

const routeQuery = new URLSearchParams(location.search)
const requisitionId = routeQuery.get('requisition_id') ? Number(routeQuery.get('requisition_id')) : undefined

const totalRemaining = computed(() => items.value.reduce((sum, item) => sum + item.remaining_qty, 0))
const processDrawerVisible = computed({
  get: () => Boolean(processPanel.value),
  set: (visible: boolean) => {
    if (!visible) processPanel.value = null
  },
})

async function loadPending() {
  loading.value = true
  try {
    const result = await masterDataApi.listWmsPending({ requisition_id: requisitionId })
    items.value = result.items
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '待收料加载失败')
  } finally {
    loading.value = false
  }
}

function openReceive(item: WmsPendingItem) {
  activeItem.value = item
  receiveQty.value = item.remaining_qty
  receiveDialog.value = true
}

function setQuickQty(qty: number) {
  if (!activeItem.value) return
  receiveQty.value = Math.min(qty, activeItem.value.remaining_qty)
}

async function confirmReceive() {
  if (!activeItem.value) return
  try {
    const result = await masterDataApi.receiveWmsItem(activeItem.value.id, {
      actual_receive_qty: receiveQty.value,
      received_by: receivedBy.value,
    })
    ElMessage.success(result.remaining_qty > 0 ? `部分收料成功，剩余 ${result.remaining_qty} 张` : '收料完成')
    receiveDialog.value = false
    processPanel.value = await masterDataApi.getWmsProcessPanel(activeItem.value.id)
    await loadPending()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '确认收料失败')
  }
}

async function openProcess(item: WmsPendingItem) {
  processPanel.value = await masterDataApi.getWmsProcessPanel(item.id)
}

onMounted(() => {
  document.body.classList.add('mobile-wms-mode')
  loadPending()
})

onBeforeUnmount(() => {
  document.body.classList.remove('mobile-wms-mode')
})
</script>

<template>
  <div class="mobile-page">
    <header class="mobile-header">
      <div>
        <h1>车间待收料</h1>
        <p>剩余 {{ totalRemaining }} 张，点击卡片即可确认收料</p>
      </div>
      <el-button size="large" @click="loadPending">刷新</el-button>
    </header>

    <main v-loading="loading" class="card-list">
      <section v-for="item in items" :key="item.id" class="receive-card" @click="openReceive(item)">
        <div class="card-top">
          <strong>{{ item.supplier_name }}</strong>
          <el-tag size="large" type="warning">剩余 {{ item.remaining_qty }}</el-tag>
        </div>
        <div class="product">{{ item.customer_name }} - {{ item.product_name }}</div>
        <div class="spec">纸板：{{ item.paper_length_mm }} x {{ item.paper_width_mm }}，材质：{{ item.material }}</div>
        <div class="spec">订单：{{ item.order_number }}，压线：{{ item.score_line }}</div>
        <el-button class="process-btn" type="primary" size="large" @click.stop="openProcess(item)">查看生产工序/图纸</el-button>
      </section>

      <el-empty v-if="!items.length && !loading" description="暂无待收料" />
    </main>

    <el-dialog v-model="receiveDialog" title="确认收料" width="92%">
      <div v-if="activeItem" class="receive-confirm">
        <h2>{{ activeItem.product_name }}</h2>
        <p>预期剩余：{{ activeItem.remaining_qty }} 张</p>
        <div class="quick-qty">
          <el-button size="large" @click="setQuickQty(5)">5张</el-button>
          <el-button size="large" :disabled="activeItem.remaining_qty < 10" @click="setQuickQty(10)">10张</el-button>
          <el-button size="large" type="primary" @click="setQuickQty(activeItem.remaining_qty)">全数到货</el-button>
        </div>
        <el-input-number v-model="receiveQty" :min="1" :max="activeItem.remaining_qty" size="large" />
        <el-input v-model="receivedBy" size="large" placeholder="收料人" />
        <el-button class="big-confirm" type="success" @click="confirmReceive">确认收料</el-button>
      </div>
    </el-dialog>

    <el-drawer v-model="processDrawerVisible" direction="btt" size="70%" title="生产工序/图纸">
      <div v-if="processPanel" class="process-panel">
        <h2>{{ processPanel.customer_name }} - {{ processPanel.product_name }}</h2>
        <p>订单：{{ processPanel.order_number }}</p>
        <p>纸板：{{ processPanel.paper_length_mm }} x {{ processPanel.paper_width_mm }}，压线：{{ processPanel.score_line }}</p>
        <div class="route">
          <span v-for="step in processPanel.process_route" :key="step">{{ step }}</span>
        </div>
        <div class="drawing">{{ processPanel.drawing_status }}</div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.mobile-page {
  min-height: 100vh;
  padding: 14px;
  background: #e9f3ff;
  font-size: 20px;
}
.mobile-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px;
  color: #fff;
  background: #0f4c81;
  border-radius: 14px;
}
.mobile-header h1 {
  margin: 0;
  font-size: 32px;
}
.mobile-header p {
  margin: 6px 0 0;
}
.card-list {
  display: grid;
  gap: 14px;
  margin-top: 14px;
}
.receive-card {
  padding: 18px;
  background: #fff;
  border: 2px solid #8aa8c8;
  border-radius: 16px;
  box-shadow: 0 4px 12px rgba(15, 76, 129, 0.15);
}
.card-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 28px;
}
.product {
  margin-top: 12px;
  font-size: 24px;
  font-weight: 900;
}
.spec {
  margin-top: 8px;
}
.process-btn {
  width: 100%;
  height: 56px;
  margin-top: 14px;
  font-size: 22px;
}
.receive-confirm {
  display: grid;
  gap: 16px;
  font-size: 22px;
}
.quick-qty {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}
.quick-qty :deep(.el-button) {
  height: 58px;
  font-size: 22px;
  font-weight: 900;
}
.big-confirm {
  height: 86px;
  font-size: 30px;
  font-weight: 900;
}
.process-panel {
  font-size: 22px;
}
.route {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin: 18px 0;
}
.route span {
  padding: 18px 8px;
  text-align: center;
  color: #fff;
  background: #1f6aa5;
  border-radius: 12px;
  font-weight: 900;
}
.drawing {
  padding: 18px;
  background: #fff7ed;
  border: 2px solid #fdba74;
  border-radius: 12px;
}
</style>
