<script setup lang="ts">
const alerts = [
  { label: '今日交货', count: 12, level: 'warn' },
  { label: '待报料订单', count: 18, level: 'warn' },
  { label: '采购过期未到', count: 3, level: 'danger' },
  { label: '送货未回签', count: 9, level: 'warn' },
]

const flowNodes = [
  { label: '客户订单', count: 36 },
  { label: '报料', count: 18 },
  { label: '纸板入仓', count: 7 },
  { label: '生产', count: 11 },
  { label: '送货', count: 14 },
  { label: '回单', count: 9 },
  { label: '对账', count: 5 },
  { label: '收款', count: 4 },
]

const tableData = [
  {
    orderNo: 'PO-20260616-001',
    customer: '天华超净',
    product: '001A外箱',
    spec: '450*340*300',
    material: 'A113B',
    status: '待报料',
  },
  {
    orderNo: 'PO-20260616-002',
    customer: '井作汽车',
    product: 'SATJTP038026',
    spec: '325*295*285',
    material: '190/115/55/135/155',
    status: '待收料',
  },
  {
    orderNo: 'PO-20260616-003',
    customer: '玻安特',
    product: '吉利用车出口外包装',
    spec: '365*285*145',
    material: '175/125/50/145/130',
    status: '待送货',
  },
]
</script>

<template>
  <div class="tm-page">
    <div class="dashboard-grid">
      <section class="tm-card side-panel">
        <h2 class="tm-section-title">交期与异常</h2>
        <div v-for="item in alerts" :key="item.label" class="alert-row" :class="item.level">
          <span>{{ item.label }}</span>
          <strong>{{ item.count }}</strong>
        </div>
      </section>

      <section class="tm-card flow-panel">
        <div class="tm-toolbar">
          <h2 class="tm-section-title">纸箱厂业务流程</h2>
          <el-button type="primary">刷新</el-button>
          <el-button>导出 Excel</el-button>
        </div>
        <div class="flow-map">
          <template v-for="(node, index) in flowNodes" :key="node.label">
            <div class="flow-node">
              <span>{{ node.label }}</span>
              <strong>{{ node.count }}</strong>
            </div>
            <div v-if="index < flowNodes.length - 1" class="flow-arrow">→</div>
          </template>
        </div>
      </section>

      <section class="tm-card side-panel">
        <h2 class="tm-section-title">常用报表</h2>
        <el-button class="quick-button" type="primary">纸板实时库存</el-button>
        <el-button class="quick-button" type="primary">今日计划到货</el-button>
        <el-button class="quick-button" type="primary">计划需要出货</el-button>
        <el-button class="quick-button" type="primary">客户应收总表</el-button>
      </section>
    </div>

    <section class="tm-card table-panel">
      <div class="tm-toolbar">
        <h2 class="tm-section-title">今日重点订单</h2>
        <span class="tm-muted">vxe-table 虚拟滚动与键盘导航预留</span>
      </div>
      <vxe-table
        border
        show-overflow="ellipsis"
        height="300"
        :keyboard-config="{ isArrow: true, isEnter: true }"
        :data="tableData"
      >
        <vxe-column type="seq" width="70" title="序号" />
        <vxe-column field="orderNo" title="任务编号" width="190" />
        <vxe-column field="customer" title="客户名称" width="160" />
        <vxe-column field="product" title="款号/品名" min-width="220" />
        <vxe-column field="spec" title="规格" width="160" />
        <vxe-column field="material" title="材质" width="200" />
        <vxe-column field="status" title="当前状态" width="120" />
      </vxe-table>
    </section>
  </div>
</template>

<style scoped>
.dashboard-grid {
  display: grid;
  grid-template-columns: 260px 1fr 260px;
  gap: 12px;
}

.side-panel {
  padding: 16px;
}

.alert-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  min-height: 56px;
  margin-top: 10px;
  padding: 0 14px;
  border: 1px solid #d3a037;
  background: #fff7df;
  font-size: 20px;
  font-weight: 800;
}

.alert-row.danger {
  border-color: #c2410c;
  background: #ffe8df;
  color: #9a3412;
}

.flow-panel {
  min-height: 260px;
}

.flow-map {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  align-items: center;
  padding: 26px;
}

.flow-node {
  width: 130px;
  height: 92px;
  display: grid;
  place-items: center;
  border: 2px solid #1d5d95;
  border-radius: 6px;
  background: linear-gradient(#f9fcff, #d6e9ff);
  color: #0f2e53;
  font-size: 20px;
  font-weight: 900;
}

.flow-node strong {
  font-size: 30px;
  color: #b45309;
}

.flow-arrow {
  color: #1d5d95;
  font-size: 30px;
  font-weight: 900;
}

.quick-button {
  width: 100%;
  margin: 10px 0 0;
  min-height: 52px;
  font-size: 18px;
  font-weight: 900;
}

.table-panel {
  min-height: 360px;
}
</style>
