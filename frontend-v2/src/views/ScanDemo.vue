<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { Html5Qrcode } from 'html5-qrcode'
import { ElMessage } from 'element-plus'

/** 演示页：与 v1 ScanDemo 相同逻辑，换深色皮肤 */
const scannerId = 'tm-scan-reader-v2'
const scanner = ref<Html5Qrcode | null>(null)
const scanning = ref(false)
const scannedCode = ref('')
const statusText = ref('等待扫码')

const orderInfo = ref({
  orderNo: 'PO-20260616-001',
  customer: '天华超净',
  product: '001A外箱',
  spec: '450 × 340 × 300 mm',
  material: 'A113B / AB楞',
  quantity: 1500,
  action: '确认收料',
})

async function startScan() {
  if (scanning.value) return
  scanner.value = new Html5Qrcode(scannerId)
  scanning.value = true
  statusText.value = '摄像头启动中'

  try {
    await scanner.value.start(
      { facingMode: 'environment' },
      { fps: 10, qrbox: { width: 260, height: 260 } },
      async (decodedText) => {
        scannedCode.value = decodedText
        statusText.value = '扫码成功'
        await stopScan()
      },
      () => undefined,
    )
  } catch {
    scanning.value = false
    statusText.value = '摄像头不可用，可手工模拟扫码'
    ElMessage.warning('当前浏览器未授权摄像头，已保留手工模拟入口。')
  }
}

async function stopScan() {
  if (!scanner.value) return
  try {
    if (scanner.value.isScanning) await scanner.value.stop()
    await scanner.value.clear()
  } finally {
    scanner.value = null
    scanning.value = false
  }
}

function simulateScan() {
  scannedCode.value = 'TM-FLOW-PO-20260616-001-01'
  statusText.value = '扫码成功'
}

function confirmAction() {
  ElMessage.success(`${orderInfo.value.action}成功：${orderInfo.value.orderNo}`)
  statusText.value = '已确认'
}

onBeforeUnmount(() => {
  void stopScan()
})
</script>

<template>
  <div class="scan-page">
    <section class="scan-header tm-card">
      <h1>车间扫码状态面板 <span class="demo-tag">演示</span></h1>
      <p>{{ statusText }}</p>
    </section>

    <section class="scan-content">
      <div class="scan-camera tm-card">
        <div :id="scannerId" class="reader"></div>
        <div class="scan-actions">
          <el-button type="primary" @click="startScan">启动扫码</el-button>
          <el-button @click="simulateScan">模拟扫码</el-button>
          <el-button @click="stopScan">停止</el-button>
        </div>
      </div>

      <div class="order-panel tm-card">
        <div class="code-line">绑定码：<span class="tm-mono">{{ scannedCode || '未扫码' }}</span></div>
        <div class="order-big">{{ orderInfo.orderNo }}</div>
        <div class="order-grid">
          <div><b>客户：</b>{{ orderInfo.customer }}</div>
          <div><b>品名：</b>{{ orderInfo.product }}</div>
          <div><b>规格：</b>{{ orderInfo.spec }}</div>
          <div><b>材质：</b>{{ orderInfo.material }}</div>
          <div><b>数量：</b>{{ orderInfo.quantity }} 只</div>
        </div>
        <button class="confirm-button" type="button" :disabled="!scannedCode" @click="confirmAction">
          {{ orderInfo.action }}
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.scan-page {
  min-height: calc(100vh - 130px);
  padding: 4px;
}

.scan-header {
  padding: 16px 20px;
  border-left: 4px solid var(--tm-accent-a);
}

.scan-header h1 {
  margin: 0;
  font-size: 28px;
  color: #f0f6ff;
  display: flex;
  align-items: center;
  gap: 10px;
}

.scan-header p {
  margin: 6px 0 0;
  font-size: 17px;
  font-weight: 700;
  color: var(--tm-accent-a);
}

.demo-tag {
  font-size: 11px;
  font-weight: 800;
  padding: 2px 10px;
  border-radius: 20px;
  color: #fde68a;
  background: rgba(251, 191, 36, 0.14);
  border: 1px solid rgba(251, 191, 36, 0.5);
}

.scan-content {
  display: grid;
  grid-template-columns: 420px 1fr;
  gap: 14px;
  margin-top: 14px;
}

.scan-camera,
.order-panel {
  padding: 16px;
}

.reader {
  min-height: 320px;
  display: grid;
  place-items: center;
  border: 2px dashed rgba(34, 211, 238, 0.5);
  border-radius: 10px;
  background: rgba(7, 11, 20, 0.8);
  overflow: hidden;
}

.scan-actions {
  display: flex;
  gap: 10px;
  margin-top: 12px;
}

.code-line {
  font-size: 19px;
  font-weight: 800;
  color: #9fd9ff;
}

.order-big {
  margin: 18px 0;
  padding: 16px;
  color: #a5f3fc;
  background: linear-gradient(135deg, rgba(34, 211, 238, 0.14), rgba(59, 130, 246, 0.08));
  border: 1px solid rgba(34, 211, 238, 0.45);
  border-radius: 10px;
  font-size: 38px;
  font-weight: 900;
  text-align: center;
}

.order-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  font-size: 19px;
  color: var(--tm-text-dim);
}

.order-grid b {
  color: var(--tm-text);
}

.confirm-button {
  width: 100%;
  height: 30vh;
  min-height: 200px;
  margin-top: 20px;
  border: 0;
  border-radius: 10px;
  color: #04121f;
  background: linear-gradient(135deg, #34d399, #22d3ee);
  box-shadow: 0 0 24px rgba(52, 211, 153, 0.4);
  font-size: 48px;
  font-weight: 900;
  cursor: pointer;
  transition: transform 0.12s ease;
}

.confirm-button:hover:not(:disabled) {
  transform: translateY(-2px);
}

.confirm-button:disabled {
  background: rgba(92, 111, 140, 0.3);
  color: var(--tm-text-faint);
  box-shadow: none;
  cursor: not-allowed;
}

@media (max-width: 900px) {
  .scan-content {
    grid-template-columns: 1fr;
  }
}
</style>
