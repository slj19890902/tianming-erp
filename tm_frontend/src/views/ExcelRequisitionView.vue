<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { ElMessage } from 'element-plus'
import * as XLSX from 'xlsx'
import { masterDataApi, type ProductHistoryMatch } from '../api/masterData'

interface ExcelReqRow {
  id: number
  k_keyword: string
  style_no: string
  paper_length_mm: string
  paper_width_mm: string
  score_line: string
  material: string
  quantity: number | null
  remark: string
  matched_product_id?: number
  matched_customer?: string | null
}

interface HistorySuggestion extends ProductHistoryMatch {
  value: string
  label: string
  size_label: string
}

const xTable = ref()
const loading = ref(false)
const rows = ref<ExcelReqRow[]>(
  Array.from({ length: 20 }, (_, index) => ({
    id: index + 1,
    k_keyword: '',
    style_no: '',
    paper_length_mm: '',
    paper_width_mm: '',
    score_line: '',
    material: '',
    quantity: null,
    remark: '',
  })),
)

const filledRows = computed(() =>
  rows.value.filter((row) => row.quantity && row.material && row.paper_length_mm && row.paper_width_mm),
)

function addRows(count = 10) {
  const start = rows.value.length
  for (let index = 0; index < count; index += 1) {
    rows.value.push({
      id: start + index + 1,
      k_keyword: '',
      style_no: '',
      paper_length_mm: '',
      paper_width_mm: '',
      score_line: '',
      material: '',
      quantity: null,
      remark: '',
    })
  }
}

function suggestionLabel(item: ProductHistoryMatch) {
  const size = [item.paper_length_mm, item.paper_width_mm].filter(Boolean).join('x')
  return `${item.customer_name || item.product_name || ''} ${item.style_no || ''} - ${size || '未填尺寸'} - ${
    item.material || '未填材质'
  }`
}

async function fetchHistorySuggestions(query: string, callback: (items: HistorySuggestion[]) => void) {
  const keyword = query.trim()
  if (keyword.length < 2) {
    callback([])
    return
  }
  try {
    const result = await masterDataApi.searchProductHistory(keyword)
    callback(
      (result.items || []).map((item) => ({
        ...item,
        value: item.historical_search_key || item.product_name,
        label: suggestionLabel(item),
        size_label: [item.paper_length_mm, item.paper_width_mm].filter(Boolean).join('x'),
      })),
    )
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '历史配方查询失败')
    callback([])
  }
}

function selectHistory(row: ExcelReqRow, item: HistorySuggestion) {
  row.matched_product_id = item.id
  row.matched_customer = item.customer_name
  row.k_keyword = item.historical_search_key || row.k_keyword
  row.style_no = item.style_no || row.style_no
  row.paper_length_mm = item.paper_length_mm || ''
  row.paper_width_mm = item.paper_width_mm || ''
  row.score_line = item.score_line || ''
  row.material = item.material || ''
  ElMessage.success(`已回填：${item.material || ''} ${item.size_label || ''}`)
  focusQuantityCell(row)
}

async function focusQuantityCell(row: ExcelReqRow) {
  const table = xTable.value as any
  await nextTick()
  try {
    table?.scrollToRow?.(row, 'quantity')?.catch?.(() => undefined)
  } catch {
    // vxe-table may not own the horizontal scroll container in this layout.
  }
  try {
    table?.setEditCell?.(row, 'quantity')?.catch?.(() => undefined)
  } catch {
    // Keep the manual focus fallback below available.
  }
  await nextTick()
  window.setTimeout(() => {
    const rowElement = document.querySelector(`.vxe-body--row[rowid="${row.id}"]`)
    const quantityInput =
      rowElement?.querySelector<HTMLInputElement>('[data-field="quantity"] input') ||
      rowElement?.querySelector<HTMLInputElement>('.col_19 input') ||
      document.querySelector<HTMLInputElement>('.col_19 input')
    const workPanel = document.querySelector<HTMLElement>('.work-panel')
    if (quantityInput && workPanel) {
      const inputRect = quantityInput.getBoundingClientRect()
      const panelRect = workPanel.getBoundingClientRect()
      if (inputRect.right > panelRect.right - 40) {
        workPanel.scrollLeft += inputRect.right - panelRect.right + 80
      } else if (inputRect.left < panelRect.left + 40) {
        workPanel.scrollLeft -= panelRect.left - inputRect.left + 80
      }
    }
    window.requestAnimationFrame(() => {
      quantityInput?.focus()
      quantityInput?.select?.()
    })
  }, 80)
}

function exportSupplierSheet() {
  if (!filledRows.value.length) {
    ElMessage.warning('请先填写数量')
    return
  }
  const data = filledRows.value.map((row) => ({
    材质: row.material,
    纸长: Number(row.paper_length_mm),
    纸宽: Number(row.paper_width_mm),
    压线: row.score_line,
    数量: Number(row.quantity || 0),
  }))
  const worksheet = XLSX.utils.json_to_sheet(data)
  worksheet['!cols'] = [{ wch: 14 }, { wch: 12 }, { wch: 12 }, { wch: 22 }, { wch: 10 }]
  const workbook = XLSX.utils.book_new()
  XLSX.utils.book_append_sheet(workbook, worksheet, '供应商报料单')
  XLSX.writeFile(workbook, `供应商报料单-${new Date().toISOString().slice(0, 10)}.xlsx`)
  ElMessage.success(`已导出 ${filledRows.value.length} 行供应商报料单`)
}
</script>

<template>
  <section class="excel-page">
    <div class="excel-head">
      <div>
        <h1>超级 K 列仿 Excel 报料台</h1>
        <p>K 列输入只做智能联想；必须选中具体款号后才回填纸板尺寸、压线和材质。</p>
      </div>
      <div class="head-actions">
        <el-button @click="addRows()">增加10行</el-button>
        <el-button type="success" size="large" @click="exportSupplierSheet">导出供应商报料单</el-button>
      </div>
    </div>

    <vxe-table
      ref="xTable"
      v-loading="loading"
      border
      show-overflow="ellipsis"
      height="650"
      :data="rows"
      :row-config="{ keyField: 'id', isHover: true }"
      :keyboard-config="{ isArrow: true, isEnter: true, isEdit: true }"
      :edit-config="{ trigger: 'click', mode: 'cell' }"
    >
      <vxe-column field="k_keyword" title="K列(综合搜索)" width="300" :edit-render="{}">
        <template #edit="{ row }">
          <el-autocomplete
            v-model="row.k_keyword"
            class="k-autocomplete"
            :fetch-suggestions="fetchHistorySuggestions"
            placeholder="输入 213 / 天华 / 款号规格"
            value-key="value"
            :trigger-on-focus="false"
            clearable
            @select="(item: HistorySuggestion) => selectHistory(row, item)"
          >
            <template #default="{ item }">
              <div class="suggestion-item">
                <strong>{{ item.customer_name || item.product_name }}</strong>
                <span>{{ item.style_no || item.historical_search_key }}</span>
                <em>{{ item.size_label || '未填尺寸' }} / {{ item.material || '未填材质' }}</em>
              </div>
            </template>
          </el-autocomplete>
        </template>
        <template #default="{ row }">
          <span class="k-cell">{{ row.k_keyword }}</span>
          <el-tag v-if="row.matched_product_id" size="small" type="success">已匹配</el-tag>
        </template>
      </vxe-column>
      <vxe-column field="style_no" title="G列(款号)" width="120" :edit-render="{}">
        <template #edit="{ row }"><el-input v-model="row.style_no" /></template>
      </vxe-column>
      <vxe-column field="paper_length_mm" title="A列(纸长)" width="100" :edit-render="{}">
        <template #edit="{ row }"><el-input v-model="row.paper_length_mm" /></template>
      </vxe-column>
      <vxe-column field="paper_width_mm" title="B列(纸宽)" width="100" :edit-render="{}">
        <template #edit="{ row }"><el-input v-model="row.paper_width_mm" /></template>
      </vxe-column>
      <vxe-column field="score_line" title="C列(压线)" width="150" :edit-render="{}">
        <template #edit="{ row }"><el-input v-model="row.score_line" /></template>
      </vxe-column>
      <vxe-column field="material" title="E列(材质)" width="120" :edit-render="{}">
        <template #edit="{ row }"><el-input v-model="row.material" /></template>
      </vxe-column>
      <vxe-column field="quantity" title="D列(报料数量)" width="130" :edit-render="{ autofocus: '.el-input__inner' }">
        <template #edit="{ row }"><el-input-number v-model="row.quantity" :min="0" /></template>
      </vxe-column>
      <vxe-column field="remark" title="F/I列(备注)" min-width="180" :edit-render="{}">
        <template #edit="{ row }"><el-input v-model="row.remark" /></template>
      </vxe-column>
    </vxe-table>

    <div class="excel-summary">
      <span>可导出行数：{{ filledRows.length }}</span>
      <span>导出字段：材质、纸长、纸宽、压线、数量</span>
    </div>
  </section>
</template>

<style scoped>
.excel-page {
  display: grid;
  gap: 12px;
}
.excel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 18px;
  background: #fff;
  border: 1px solid #9fb7d2;
}
.excel-head h1 {
  margin: 0;
  color: #14395f;
  font-size: 28px;
}
.excel-head p {
  margin: 6px 0 0;
  color: #4b6278;
}
.head-actions {
  display: flex;
  gap: 10px;
}
.k-autocomplete {
  width: 100%;
}
.k-cell {
  margin-right: 8px;
  font-weight: 800;
}
.suggestion-item {
  display: grid;
  gap: 2px;
  padding: 6px 0;
  line-height: 1.25;
}
.suggestion-item strong {
  color: #12385f;
  font-size: 15px;
}
.suggestion-item span {
  color: #1f2937;
  font-weight: 700;
}
.suggestion-item em {
  color: #0f7a32;
  font-style: normal;
}
.excel-summary {
  display: flex;
  justify-content: space-between;
  padding: 12px 16px;
  color: #12385f;
  background: #eef6ff;
  border: 1px solid #9fb7d2;
  font-size: 18px;
  font-weight: 800;
}
</style>
