<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import QrcodeVue from 'qrcode.vue'
import { masterDataApi, type PendingRequisitionItem, type RequisitionResult } from '../api/masterData'

const loading = ref(false)
const supplierName = ref('佳丰纸板')
const rows = ref<PendingRequisitionItem[]>([])
const selectedIds = ref<number[]>([])
const latest = ref<RequisitionResult | null>(null)

const selectedRows = computed(() => rows.value.filter((item) => selectedIds.value.includes(item.order_item_id)))
const totalQty = computed(() => selectedRows.value.reduce((sum, item) => sum + item.requisition_qty, 0))
const mobileUrl = computed(() => latest.value ? `${location.origin}${latest.value.mobile_receive_url}` : '')

async function loadPending() {
  loading.value = true
  try {
    const result = await masterDataApi.listPendingRequisitionItems()
    rows.value = result.items
    selectedIds.value = result.items.map((item) => item.order_item_id)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '待报料明细加载失败')
  } finally {
    loading.value = false
  }
}

function toggleAll(checked: boolean) {
  selectedIds.value = checked ? rows.value.map((item) => item.order_item_id) : []
}

async function createRequisition() {
  if (!selectedRows.value.length) {
    ElMessage.warning('请至少勾选一条明细')
    return
  }
  try {
    latest.value = await masterDataApi.createRequisition({
      supplier_name: supplierName.value,
      items: selectedRows.value.map((item) => ({
        order_item_id: item.order_item_id,
        requisition_qty: item.requisition_qty,
      })),
    })
    ElMessage.success(`报料单已生成：${latest.value.requisition_number}`)
    await loadPending()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '生成报料单失败')
  }
}

onMounted(loadPending)
</script>

<template>
  <div class="tm-page">
    <section class="tm-card">
      <div class="tm-toolbar req-toolbar">
        <div>
          <h2 class="tm-section-title">报料工作台</h2>
          <div class="tm-muted">左侧指定供应商，右侧勾选订单明细合并生成纸板报料单。</div>
        </div>
        <div class="supplier-box">
          <span>供应商</span>
          <el-input v-model="supplierName" style="width: 180px" />
          <el-button type="primary" @click="loadPending">刷新待报料</el-button>
          <el-button type="success" @click="createRequisition">生成报料单</el-button>
        </div>
      </div>

      <div class="req-grid">
        <aside class="supplier-panel">
          <div class="panel-title">本次报料</div>
          <div class="supplier-name">{{ supplierName }}</div>
          <div class="metric">勾选 {{ selectedRows.length }} 条</div>
          <div class="metric">合计 {{ totalQty }} 张</div>
          <el-checkbox
            :model-value="selectedIds.length === rows.length && rows.length > 0"
            @change="(value: boolean) => toggleAll(value)"
          >
            全选/取消全选
          </el-checkbox>
        </aside>

        <main v-loading="loading" class="req-table">
          <vxe-table border stripe height="470" show-overflow="ellipsis" :data="rows">
            <vxe-column title="选择" width="80">
              <template #default="{ row }">
                <el-checkbox v-model="selectedIds" :label="row.order_item_id" />
              </template>
            </vxe-column>
            <vxe-column field="order_number" title="订单号" width="170" />
            <vxe-column field="customer_name" title="客户" width="150" />
            <vxe-column field="product_name" title="产品" min-width="180" />
            <vxe-column field="material" title="材质" width="120" />
            <vxe-column title="纸板规格" width="170">
              <template #default="{ row }">{{ row.paper_length_mm }}×{{ row.paper_width_mm }}</template>
            </vxe-column>
            <vxe-column field="score_line" title="压线" width="160" />
            <vxe-column field="requisition_qty" title="报料张数" width="120" />
          </vxe-table>
        </main>
      </div>
    </section>

    <section v-if="latest" class="tm-card print-preview">
      <div class="preview-head">
        <div>
          <h2>供应商采购报料单</h2>
          <div>单号：{{ latest.requisition_number }}　供应商：{{ latest.supplier_name }}　总数：{{ latest.total_qty }}</div>
        </div>
        <qrcode-vue :value="mobileUrl" :size="96" />
      </div>
      <div class="tm-muted">扫码进入移动端待收料看板：{{ mobileUrl }}</div>
    </section>
  </div>
</template>

<style scoped>
.req-toolbar {
  justify-content: space-between;
}
.supplier-box {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 800;
}
.req-grid {
  display: grid;
  grid-template-columns: 220px 1fr;
  min-height: 500px;
}
.supplier-panel {
  padding: 18px;
  background: #eef5fb;
  border-right: 1px solid #9fb7d2;
}
.panel-title {
  color: #12385f;
  font-size: 20px;
  font-weight: 900;
}
.supplier-name {
  margin: 18px 0;
  font-size: 26px;
  font-weight: 900;
}
.metric {
  margin-bottom: 12px;
  font-size: 20px;
}
.req-table {
  min-width: 0;
}
.print-preview {
  padding: 16px;
}
.preview-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
