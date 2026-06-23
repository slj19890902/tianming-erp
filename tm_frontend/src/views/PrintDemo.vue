<script setup lang="ts">
import { computed } from 'vue'
import QrcodeVue from 'qrcode.vue'
import printJS from 'print-js'

const workOrder = {
  orderNo: 'PO-20260616-001',
  bindCode: 'TM-FLOW-PO-20260616-001-01',
  customer: '天华超净',
  productCode: '001A',
  productName: '001A外箱',
  spec: '450 × 340 × 300 mm',
  material: 'A113B / AB楞',
  quantity: 1500,
  paperLength: 916,
  paperWidth: 644,
  scoreLine: '340 * 110 * 340',
  cuttingTip: '优先使用 650mm 纸宽，注意压线后复核边口',
}

const qrValue = computed(() => `tm-erp://flow/${workOrder.bindCode}`)

function printWorkOrder() {
  printJS({
    printable: 'work-order-print-area',
    type: 'html',
    targetStyles: ['*'],
    documentTitle: `智能派工单-${workOrder.orderNo}`,
    scanStyles: false,
    style: `
      @page { size: A4; margin: 12mm; }
      body { font-family: "Microsoft YaHei", Arial, sans-serif; color: #111827; }
      .print-sheet { border: 3px solid #111827; padding: 18px; }
      .print-title { font-size: 34px; font-weight: 900; text-align: center; margin-bottom: 10px; }
      .print-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 18px; font-size: 20px; }
      .big-values { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; margin: 18px 0; }
      .big-box { border: 2px solid #111827; padding: 12px; text-align: center; }
      .big-box label { display: block; font-size: 18px; }
      .big-box strong { display: block; font-size: 42px; line-height: 1.2; }
      .print-footer { display: flex; justify-content: space-between; align-items: flex-end; margin-top: 16px; }
    `,
  })
}
</script>

<template>
  <div class="tm-page">
    <div class="tm-toolbar no-print">
      <h2 class="tm-section-title">智能派工单打印 Demo</h2>
      <el-button type="primary" @click="printWorkOrder">一键打印派工单</el-button>
      <span class="tm-muted">用机器替人计算纸长、纸宽、压线，逐步替代手写批注。</span>
    </div>

    <section id="work-order-print-area" class="print-sheet tm-card">
      <div class="print-title">天明包装 智能派工单</div>
      <div class="print-grid">
        <div><b>任务编号：</b>{{ workOrder.orderNo }}</div>
        <div><b>客户：</b>{{ workOrder.customer }}</div>
        <div><b>存货编码：</b>{{ workOrder.productCode }}</div>
        <div><b>品名：</b>{{ workOrder.productName }}</div>
        <div><b>产品规格：</b>{{ workOrder.spec }}</div>
        <div><b>材质楞型：</b>{{ workOrder.material }}</div>
        <div><b>订单数量：</b>{{ workOrder.quantity }} 只</div>
        <div><b>绑定码：</b>{{ workOrder.bindCode }}</div>
      </div>

      <div class="big-values">
        <div class="big-box">
          <label>纸长</label>
          <strong>{{ workOrder.paperLength }}</strong>
          <span>mm</span>
        </div>
        <div class="big-box">
          <label>纸宽</label>
          <strong>{{ workOrder.paperWidth }}</strong>
          <span>mm</span>
        </div>
        <div class="big-box">
          <label>压线</label>
          <strong class="score">{{ workOrder.scoreLine }}</strong>
        </div>
      </div>

      <div class="cutting-tip">
        <b>开料指导：</b>{{ workOrder.cuttingTip }}
      </div>

      <div class="print-footer">
        <div>
          <p>机长签字：________________</p>
          <p>完工时间：________________</p>
        </div>
        <div class="qr-box">
          <QrcodeVue :value="qrValue" :size="132" level="M" />
          <span>扫码查看状态</span>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.print-sheet {
  width: 900px;
  margin: 0 auto;
  padding: 22px;
  border: 3px solid #111827;
  background: #fff;
}

.print-title {
  margin-bottom: 16px;
  text-align: center;
  font-size: 34px;
  font-weight: 900;
}

.print-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px 22px;
  font-size: 20px;
}

.big-values {
  display: grid;
  grid-template-columns: 1fr 1fr 1.4fr;
  gap: 14px;
  margin: 22px 0;
}

.big-box {
  min-height: 150px;
  display: grid;
  place-items: center;
  border: 2px solid #111827;
  background: #f8fbff;
  text-align: center;
}

.big-box label {
  font-size: 22px;
  font-weight: 900;
}

.big-box strong {
  font-size: 52px;
  line-height: 1;
}

.big-box .score {
  font-size: 34px;
}

.cutting-tip {
  padding: 16px;
  border: 2px solid #f2b84b;
  background: #fff7df;
  font-size: 24px;
  font-weight: 800;
}

.print-footer {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  margin-top: 22px;
  font-size: 20px;
}

.qr-box {
  display: grid;
  gap: 6px;
  justify-items: center;
  font-weight: 800;
}
</style>
