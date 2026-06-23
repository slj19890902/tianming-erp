<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { Html5Qrcode } from 'html5-qrcode'
import { ElMessage } from 'element-plus'

const scannerId = 'tm-scan-reader'
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
  } catch (error) {
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
    <section class="scan-header">
      <h1>车间扫码状态面板</h1>
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
        <div class="code-line">绑定码：{{ scannedCode || '未扫码' }}</div>
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
  padding: 10px;
  background: #eef5fd;
}

.scan-header {
  padding: 14px 18px;
  color: #fff;
  background: #0f4f2a;
}

.scan-header h1 {
  margin: 0;
  font-size: 36px;
}

.scan-header p {
  margin: 6px 0 0;
  font-size: 22px;
  font-weight: 900;
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
  border: 2px dashed #7f9bb8;
  background: #fff;
}

.scan-actions {
  display: flex;
  gap: 10px;
  margin-top: 12px;
}

.code-line {
  font-size: 22px;
  font-weight: 900;
  color: #0f2e53;
}

.order-big {
  margin: 18px 0;
  padding: 16px;
  color: #0f2e53;
  background: #d6e9ff;
  border: 2px solid #1d5d95;
  font-size: 42px;
  font-weight: 900;
  text-align: center;
}

.order-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  font-size: 24px;
}

.confirm-button {
  width: 100%;
  height: 42vh;
  min-height: 260px;
  margin-top: 20px;
  border: 0;
  border-radius: 10px;
  color: #fff;
  background: #16a34a;
  font-size: 56px;
  font-weight: 900;
  cursor: pointer;
}

.confirm-button:disabled {
  background: #9ca3af;
  cursor: not-allowed;
}
</style>
