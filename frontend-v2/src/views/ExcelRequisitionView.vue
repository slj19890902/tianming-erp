<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as XLSX from 'xlsx'
import { requisitionApi, type HistorySuggestion } from '../api/client'

interface ExcelRow {
  id: number
  customer: string
  cardboard_len: string
  cardboard_width: string
  layer_flute: string
  score_line: string
  box_name: string
  order_qty: string
  sheet_qty: string
  material: string
  unit_price: string
  note: string
  suggestions: HistorySuggestion[]
  showSuggestions: boolean
}

const rows = ref<ExcelRow[]>([])
const historySearch = ref('')
const historyResults = ref<HistorySuggestion[]>([])
const materials = ref<string[]>([])
const newMaterial = ref('')
const fluteTypes = ref<string[]>([])
const newFlute = ref('')
const customerKeyword = ref('')
const customers = ref<string[]>([])

let nextId = 1

const COLUMN_LABELS = [
  '客户', '纸长', '纸宽', '层数楞型', '压线', '箱名', '订单数', '片数', '材质', '单价', '备注',
] as const

function emptyRow(): ExcelRow {
  return {
    id: nextId++,
    customer: '',
    cardboard_len: '',
    cardboard_width: '',
    layer_flute: '',
    score_line: '',
    box_name: '',
    order_qty: '',
    sheet_qty: '',
    material: '',
    unit_price: '',
    note: '',
    suggestions: [],
    showSuggestions: false,
  }
}

function suggestionText(record: HistorySuggestion, key: string): string {
  const value = record[key]
  return value === null || value === undefined ? '' : String(value)
}

async function loadHistory(keyword: string) {
  if (keyword.trim().length < 1) {
    historyResults.value = []
    return
  }
  try {
    const result = await requisitionApi.searchHistory(keyword.trim())
    historyResults.value = result.items
  } catch {
    historyResults.value = []
  }
}

function handleBoxNameInput(row: ExcelRow) {
  row.showSuggestions = false
  if (row.box_name.trim().length < 1) {
    row.suggestions = []
    return
  }
  void requisitionApi
    .searchHistory(row.box_name.trim())
    .then((result) => {
      row.suggestions = result.items.slice(0, 8)
      row.showSuggestions = row.suggestions.length > 0
    })
    .catch(() => {
      row.suggestions = []
      row.showSuggestions = false
    })
}

/** 历史联想回填：后端真实字段为 cardboard_len / cardboard_width */
function fillFromHistory(row: ExcelRow, record: HistorySuggestion) {
  row.cardboard_len = suggestionText(record, 'cardboard_len')
  row.cardboard_width = suggestionText(record, 'cardboard_width')
  row.material = suggestionText(record, 'material_name') || suggestionText(record, 'material_code')
  row.box_name = suggestionText(record, 'product_name') || row.box_name
  row.unit_price = suggestionText(record, 'unit_price') || row.unit_price
  row.customer = suggestionText(record, 'customer_name') || row.customer
  row.score_line = suggestionText(record, 'score_line') || row.score_line
  row.showSuggestions = false
  row.suggestions = []
}

function addRow() {
  rows.value.push(emptyRow())
}

function clearRows() {
  ElMessageBox.confirm('确认清空所有行？', '清空确认', { type: 'warning' })
    .then(() => {
      rows.value = [emptyRow()]
      ElMessage.success('已清空')
    })
    .catch(() => undefined)
}

function deleteRow(index: number) {
  rows.value.splice(index, 1)
  if (rows.value.length === 0) rows.value.push(emptyRow())
}

function addMaterial() {
  const value = newMaterial.value.trim()
  if (value && !materials.value.includes(value)) {
    materials.value.push(value)
    newMaterial.value = ''
  }
}

function addFlute() {
  const value = newFlute.value.trim()
  if (value && !fluteTypes.value.includes(value)) {
    fluteTypes.value.push(value)
    newFlute.value = ''
  }
}

async function searchCustomer(keyword: string) {
  customerKeyword.value = keyword
  try {
    const result = await requisitionApi.searchHistory(keyword)
    customers.value = Array.from(
      new Set(result.items.map((item) => suggestionText(item, 'customer_name')).filter(Boolean)),
    )
  } catch {
    customers.value = []
  }
}

const totals = computed(() => {
  let quantity = 0
  let sheets = 0
  for (const row of rows.value) {
    quantity += Number(row.order_qty) || 0
    sheets += Number(row.sheet_qty) || 0
  }
  return { quantity, sheets }
})

/** 导出 xlsx：前端本地生成并下载，不依赖后端 */
function exportExcel() {
  const dataRows = rows.value.map((row) => ({
    客户: row.customer,
    纸长: row.cardboard_len,
    纸宽: row.cardboard_width,
    层数楞型: row.layer_flute,
    压线: row.score_line,
    箱名: row.box_name,
    订单数: row.order_qty,
    片数: row.sheet_qty,
    材质: row.material,
    单价: row.unit_price,
    备注: row.note,
  }))
  const workbook = XLSX.utils.book_new()
  const sheet = XLSX.utils.json_to_sheet(dataRows)
  sheet['!cols'] = COLUMN_LABELS.map(() => ({ wch: 16 }))
  XLSX.utils.book_append_sheet(workbook, sheet, '报料单')
  const stamp = new Date().toISOString().slice(0, 10)
  XLSX.writeFile(workbook, `报料单_${stamp}.xlsx`)
  ElMessage.success('报料单已导出')
}

async function submitToRequisition() {
  ElMessage.info('该行提交为正式报料暂未接入后端：请先通过“报料工作台”处理已下单的产品需求')
}

onMounted(() => {
  rows.value = [emptyRow(), emptyRow(), emptyRow()]
  materials.value = ['B楞', 'C楞', 'E楞', 'BC楞']
  fluteTypes.value = ['B', 'C', 'E', 'BC']
})
</script>

<template>
  <div class="tm-page">
    <section class="tm-card tm-section">
      <div class="tm-toolbar">
        <div>
          <h2 class="tm-section-title">超级K列报料台</h2>
          <div class="tm-muted">在电子表格里直接填单；箱名输入时按历史订单联想，纸长纸宽等参数一键回填。</div>
        </div>
        <div style="display: flex; gap: 10px; flex-wrap: wrap">
          <el-button type="primary" @click="addRow">新增一行</el-button>
          <el-button @click="exportExcel">导出 Excel</el-button>
          <el-button type="danger" @click="clearRows">清空</el-button>
        </div>
      </div>

      <div class="excel-toolbar">
        <el-input
          v-model="historySearch"
          clearable
          placeholder="历史搜索（订单号/产品名）"
          style="width: 300px"
          @input="loadHistory(historySearch)"
        />
        <el-select
          v-model="customerKeyword"
          filterable
          remote
          reserve-keyword
          placeholder="客户历史联想"
          :remote-method="searchCustomer"
          style="width: 220px"
        >
          <el-option v-for="name in customers" :key="name" :label="name" :value="name" />
        </el-select>
        <div class="tm-muted">共 {{ rows.length }} 行 · 订单数合计 {{ totals.quantity }} · 片数合计 {{ totals.sheets }}</div>
      </div>

      <div class="excel-scroll">
        <table class="excel-table">
          <thead>
            <tr>
              <th class="index-col">#</th>
              <th v-for="label in COLUMN_LABELS" :key="label">{{ label }}</th>
              <th class="action-col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in rows" :key="row.id">
              <td class="index-col">{{ index + 1 }}</td>
              <td><el-input v-model="row.customer" /></td>
              <td><el-input v-model="row.cardboard_len" /></td>
              <td><el-input v-model="row.cardboard_width" /></td>
              <td><el-input v-model="row.layer_flute" /></td>
              <td><el-input v-model="row.score_line" /></td>
              <td class="suggest-cell">
                <el-input v-model="row.box_name" @input="handleBoxNameInput(row)" />
                <div v-if="row.showSuggestions" class="suggest-panel">
                  <button
                    v-for="record in row.suggestions"
                    :key="String(record.id ?? suggestionText(record, 'product_name'))"
                    type="button"
                    class="suggest-item"
                    @click="fillFromHistory(row, record)"
                  >
                    <span class="suggest-name">{{ suggestionText(record, 'product_name') || '—' }}</span>
                    <span class="tm-muted tm-mono suggest-meta">
                      {{ suggestionText(record, 'cardboard_len') }}×{{ suggestionText(record, 'cardboard_width') }}
                      · {{ suggestionText(record, 'customer_name') }}
                    </span>
                  </button>
                </div>
              </td>
              <td><el-input v-model="row.order_qty" /></td>
              <td><el-input v-model="row.sheet_qty" /></td>
              <td>
                <el-select v-model="row.material" filterable allow-create clearable>
                  <el-option v-for="item in materials" :key="item" :label="item" :value="item" />
                </el-select>
              </td>
              <td><el-input v-model="row.unit_price" /></td>
              <td><el-input v-model="row.note" /></td>
              <td class="action-col">
                <el-button type="danger" link @click="deleteRow(index)">删除</el-button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="excel-foot">
        <el-button type="success" @click="submitToRequisition">提交报料（待接入）</el-button>
        <span class="tm-muted">提示：正式报料请使用“报料工作台”按订单行处理。</span>
      </div>
    </section>

    <section class="tm-card tm-section">
      <h2 class="tm-section-title">快捷资料</h2>
      <div class="quick-grid">
        <div class="quick-block">
          <div class="quick-label">材质快捷维护</div>
          <div class="quick-row">
            <el-input v-model="newMaterial" placeholder="输入材质名称" style="width: 220px" />
            <el-button type="primary" @click="addMaterial">添加</el-button>
          </div>
          <div class="quick-tags">
            <el-tag v-for="item in materials" :key="item" class="quick-tag">{{ item }}</el-tag>
          </div>
        </div>
        <div class="quick-block">
          <div class="quick-label">楞型快捷维护</div>
          <div class="quick-row">
            <el-input v-model="newFlute" placeholder="输入楞型" style="width: 220px" />
            <el-button type="primary" @click="addFlute">添加</el-button>
          </div>
          <div class="quick-tags">
            <el-tag v-for="item in fluteTypes" :key="item" class="quick-tag">{{ item }}</el-tag>
          </div>
        </div>
      </div>
      <div class="tm-muted">历史搜索结果（{{ historyResults.length }}）：</div>
      <div class="history-list">
        <div
          v-for="(record, index) in historyResults.slice(0, 10)"
          :key="index"
          class="history-item tm-mono"
        >
          {{ suggestionText(record, 'order_number') }} · {{ suggestionText(record, 'product_name') }} ·
          {{ suggestionText(record, 'cardboard_len') }}×{{ suggestionText(record, 'cardboard_width') }} ·
          {{ suggestionText(record, 'customer_name') }}
        </div>
        <div v-if="historyResults.length === 0" class="tm-muted">暂无历史记录。</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.excel-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin-bottom: 12px;
}

.excel-scroll {
  overflow: auto;
  max-height: 480px;
  border: 1px solid var(--tm-line-strong);
  border-radius: 8px;
}

.excel-table {
  width: 100%;
  min-width: 1500px;
  border-collapse: collapse;
  background: rgba(11, 18, 32, 0.6);
}

.excel-table th,
.excel-table td {
  border: 1px solid var(--tm-line);
  padding: 2px 4px;
}

.excel-table thead th {
  position: sticky;
  top: 0;
  z-index: 2;
  background: rgba(20, 32, 55, 0.96);
  color: #9fd9ff;
  font-size: 13px;
  padding: 8px 6px;
  white-space: nowrap;
}

.index-col {
  width: 44px;
  text-align: center;
  color: var(--tm-text-dim);
  font-weight: 800;
}

.action-col {
  width: 76px;
  text-align: center;
  white-space: nowrap;
}

.excel-table :deep(.el-input__wrapper),
.excel-table :deep(.el-select .el-input__wrapper) {
  background: transparent;
  box-shadow: none;
  border: none;
}

.suggest-cell {
  position: relative;
}

.suggest-panel {
  position: absolute;
  left: 0;
  right: 0;
  top: 100%;
  z-index: 20;
  background: #0e1729;
  border: 1px solid var(--tm-accent-a);
  border-radius: 6px;
  box-shadow: var(--tm-panel-shadow), var(--tm-glow);
  overflow: hidden;
}

.suggest-item {
  width: 100%;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  background: transparent;
  border: none;
  color: var(--tm-text);
  text-align: left;
  cursor: pointer;
  font-size: 13px;
}

.suggest-item:hover {
  background: rgba(34, 211, 238, 0.12);
}

.suggest-name {
  font-weight: 700;
}

.suggest-meta {
  font-size: 12px;
}

.excel-foot {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 12px;
}

.quick-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
  margin-bottom: 16px;
}

.quick-block {
  padding: 14px;
  border: 1px solid var(--tm-line);
  border-radius: 10px;
  background: rgba(13, 21, 38, 0.6);
}

.quick-label {
  font-weight: 800;
  margin-bottom: 10px;
  color: #9fd9ff;
}

.quick-row {
  display: flex;
  gap: 10px;
}

.quick-tags {
  margin-top: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.history-list {
  margin-top: 8px;
  display: grid;
  gap: 6px;
}

.history-item {
  padding: 6px 10px;
  border: 1px solid var(--tm-line);
  border-radius: 6px;
  font-size: 12px;
  color: var(--tm-text-dim);
  background: rgba(13, 21, 38, 0.5);
}

@media (max-width: 900px) {
  .quick-grid {
    grid-template-columns: 1fr;
  }
}
</style>
