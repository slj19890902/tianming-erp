import { withRequestDeadline } from '../utils/requestLifecycle'
import type { OrderHoldPreview } from '../utils/orderRequisitionHold'
import { trustedDrawingPath, type DrawingSaveOption } from '../utils/orderDrawings'
/* ============================================================
 * 天明包装 ERP v2 · API 客户端
 * - 所有路径/方法/字段均逐条对照后端 app/api/*.py 真实路由核实
 *   （2026-10-01 人工核对：auth/customers/materials/products/
 *    pricing/orders/requisition/incoming/deliveries/finance/
 *    dashboard；前缀见 app/main.py 的 include_router）
 * - 旧前端 tm_frontend 的废弃前缀一律不用：
 *   /api/engine/*、/api/wms/*、/api/receipts*、/api/statements/*
 *   /api/requisitions/*（复数）、/api/deliveries/pending-items
 *   （连字符）、根 main.py 的 /api/login、/api/customer-management*
 * - fetch 统一携带 credentials:'include'，传递 httpOnly session
 *   cookie（erp_session）；401（非登录接口）触发 onUnauthorized
 * - 角色矩阵（RoleChecker，见各模块 deps）：
 *   admin/finance/sales/workshop 四种角色；
 *   基础资料写操作仅 admin；报料读 admin+sales；
 *   算料 admin+sales+finance（workshop 不可）；收料 admin+workshop；
 *   财务 admin+finance；出货/订单/仪表盘读四角色均可
 * ============================================================ */

// 默认使用同源地址：开发时由 Vite 代理，部署时由 ERP 入口托管。
// 只有显式配置 VITE_API_BASE 时才允许指向独立 API 地址。
const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '')

export interface PageResult<T> {
  total: number
  page: number
  page_size: number
  items: T[]
}

export class ApiError extends Error {
  status: number
  code?: string
  constructor(status: number, message: string, code?: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

/** 401 回调由 main.ts 在路由就绪后注入，避免循环依赖 */
let unauthorizedHandler: (() => void) | null = null
export function onUnauthorized(handler: () => void) {
  unauthorizedHandler = handler
}

export async function request<T>(path: string, init?: RequestInit, responseType: 'json' | 'blob' = 'json'): Promise<T> {
  const writing = !['GET','HEAD'].includes(String(init?.method || 'GET').toUpperCase())
  return withRequestDeadline(async signal => {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init, credentials:'include', signal,
    headers: {
      ...(typeof FormData !== 'undefined' && init?.body instanceof FormData ? {} : {'Content-Type':'application/json'}),
      ...(init?.headers || {}),
    },
  })
  if (signal.aborted) throw new DOMException('请求已取消','AbortError')
  if (response.status === 401 && !path.startsWith('/api/auth/login')) {
    if (path !== '/api/auth/me') unauthorizedHandler?.()
    throw new ApiError(401, '登录已失效，请重新登录')
  }
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    let code: string | undefined
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') message = body.detail
      else if (Array.isArray(body.detail)) {
        message = body.detail
          .map((item: { msg?: string }) => item.msg || JSON.stringify(item))
          .join('；')
      } else if (body.detail && typeof body.detail === 'object') {
        code = typeof body.detail.code === 'string' ? body.detail.code : undefined
        message = typeof body.detail.message === 'string' ? body.detail.message : JSON.stringify(body.detail)
      } else if (body.detail) message = JSON.stringify(body.detail)
    } catch {
      // 保留 HTTP 状态兜底
    }
    throw new ApiError(response.status, message, code)
  }
  if (response.status === 204) return undefined as T
  return await (responseType === 'blob' ? response.blob() : response.json()) as T
  }, () => new ApiError(0, writing
    ? '提交连接超时，结果待核对；请先查看原单据或使用该业务的结果查询，不要重复新建'
    : '读取超时，请检查网络或 ERP 服务后重试', 'TIMEOUT'), init?.signal)

}

/* ---------------- 类型定义（以后端实际返回为准） ---------------- */

export interface AuthUser {
  id: number
  username: string
  role: string
  real_name: string
  display_name?: string | null
  must_change_password: boolean
  is_active: boolean
}

export interface Customer {
  id: number
  customer_number?: number | null
  customer_code: string
  name: string
  short_name?: string | null
  contact_person?: string | null
  phone?: string | null
  address?: string | null
  payment_term_days: number
  delivery_method: string
  default_tax_rate?: string
  note?: string | null
  is_active: boolean
}

export interface Material {
  id: number
  code: string
  name?: string | null
  paper_composition?: string | null
  basis_weight_description?: string | null
  layer_count?: number | null
  flute_type_id?: number | null
  flute_type_code?: string | null
  customer_square_price?: string | null
  supplier_square_price?: string | null
  note?: string | null
  is_active: boolean
}

import type { InventoryCandidate, InventoryAuthority, InventoryDraftLine, ReservationPlan } from '../utils/orderInventory'

export interface Product {
  drawings?: Array<{ id: number; image_path: string; purpose?: string }>
  id: number
  customer_id: number
  version?: number
  production_notes?: string | null
  supply_mode?: string | null
  customer_name?: string | null
  product_code: string
  customer_material_code?: string | null
  product_name: string
  unit?: string | null
  unit_label?: string | null
  unit_needs_review?: boolean
  material_id?: number | null
  material_code?: string | null
  default_material_text?: string | null
  material_supplier_name?: string | null
  flute_type?: string | null
  flute_type_id?: number | null
  flute_type_code?: string | null
  length_mm?: string | null
  width_mm?: string | null
  height_mm?: string | null
  box_category?: string | null
  box_style?: string | null
  production_process?: string | null
  default_score_line?: string | null
  default_cardboard_length_mm?: string | null
  default_cardboard_width_mm?: string | null
  default_unit_price?: string | null
  sale_unit_price?: string | null
  note?: string | null
  is_active: boolean
  is_composite?: boolean
  report_length_mm?: number | null
  report_width_mm?: number | null
  base_report_length_mm?: number | null
  base_report_width_mm?: number | null
  pieces_per_box?: number | null
  default_cutting_mode?: string | null
  layer_count?: number | null
  crease_type?: string | null
  crease_left_mm?: number | null
  crease_middle_mm?: number | null
  crease_right_mm?: number | null
  base_crease_type?: string | null
  base_crease_left_mm?: number | null
  base_crease_middle_mm?: number | null
  base_crease_right_mm?: number | null
}

/**
 * 报料待办（GET /api/requisition/pending 真实字段）。
 * 注意：纸板尺寸建议值字段名为 suggested_cardboard_len/width；
 * cancel 仅 admin 可调，且仅对已报料明细有效（未报料会 409）。
 */
export interface RequisitionPendingItem {
  item_id: number
  order_number: string
  display_order_number?: string
  customer_id: number
  customer_name: string
  product_id?: number | null
  product_code?: string | null
  product_name?: string | null
  specification?: string | null
  material?: string | null
  quantity: number
  delivery_date?: string | null
  inventory_deducted_qty?: number | null
  requisition_qty?: number | null
  requisition_status?: string | null
  special_process?: string | null
  snapshot_supplier_name?: string | null
  suggested_cardboard_len?: string | null
  suggested_cardboard_width?: string | null
}

/** 报料记录（GET /api/requisition/items 真实字段） */
export interface RequisitionItem {
  item_id: number
  requisition_status: string
  order_number: string
  display_order_number?: string
  customer_id: number
  customer_name: string
  product_id?: number | null
  product_code?: string | null
  product_name?: string | null
  specification?: string | null
  material?: string | null
  quantity: number
  delivery_date?: string | null
  material_status?: string | null
  inventory_deducted_qty?: number | null
  requisition_qty?: number | null
  cardboard_len?: string | null
  cardboard_width?: string | null
  special_process?: string | null
  requisition_date?: string | null
  supplier_delivery_time?: string | null
  supplier_order_number?: string | null
}

/** 特殊处理仅允许：无、大做小、双拼、多拼 */
export const SPECIAL_PROCESSES = ['无', '大做小', '双拼', '多拼'] as const

/** 历史配方联想（GET /api/requisition/search_history 真实字段） */
export interface HistorySuggestion {
  item_id?: number
  customer_name?: string | null
  order_number?: string | null
  display_order_number?: string | null
  product_id?: number | null
  product_code?: string | null
  product_name?: string | null
  specification?: string | null
  material_id?: number | null
  material?: string | null
  cardboard_len?: string | null
  cardboard_width?: string | null
  requisition_qty?: number | null
  special_process?: string | null
  requisition_date?: string | null
  [key: string]: unknown
}

/**
 * 收料行（GET /api/incoming/pending|received 真实字段）。
 * 注意：received 仅返回近 24 小时记录；确认收料要求
 * requisition_status ∈ {已报料, 供应商已排单} 且 material_status=pending。
 */
export interface IncomingItem {
  order_unit_label?: string | null
  item_id: number
  order_id: number
  order_number: string
  customer_po?: string | null
  customer_name: string
  product_code?: string | null
  product_name?: string | null
  specification?: string | null
  material?: string | null
  quantity: number
  incoming_quantity?: number | null
  planned_quantity?: number | null
  cumulative_received_quantity?: number | null
  remaining_quantity?: number | null
  snapshot_supplier_name?: string | null
  delivery_date?: string | null
  order_status?: string | null
  material_status?: string | null
  requisition_status?: string | null
  requisition_qty?: number | null
  requisition_spec?: string | null
  special_process?: string | null
  supplier_delivery_time?: string | null
  supplier_order_number?: string | null
  material_received_at?: string | null
  material_received_by?: number | null
  received_by_name?: string | null
}

/**
 * 送货待办（GET /api/deliveries/pending_items 真实字段）。
 * 注意：主键为 item_id（订单明细 id），剩余数为 remaining_quantity，
 * 无单价字段。
 */
export interface DeliveryPendingItem {
  item_id: number
  order_id: number
  order_number: string
  customer_po?: string | null
  customer_id: number
  customer_name: string
  product_code?: string | null
  product_name?: string | null
  specification?: string | null
  material?: string | null
  quantity: number
  delivered_quantity: number
  remaining_quantity: number
  delivery_date?: string | null
}

/** 送货单（后端真实字段；无 driver_name，只有 vehicle_number 车牌） */
export interface DeliveryNote {
  id: number
  delivery_number: string
  customer_id: number
  customer_name?: string | null
  delivery_date?: string | null
  vehicle_number?: string | null
  status: string
  total_quantity: number
  dispatched_at?: string | null
  is_printed?: boolean
  printed_at?: string | null
  printed_by?: number | null
  return_receipt_id?: number | null
  return_receipt_status?: string | null
  items: DeliveryNoteItem[]
}

export interface DeliveryNoteItem {
  id: number
  order_item_id: number
  delivered_quantity: number
  remarks?: string | null
  order_id?: number
  order_number?: string | null
  customer_po?: string | null
  product_code?: string | null
  product_name?: string | null
  specification?: string | null
}

/**
 * 回单（后端真实字段）。
 * 状态仅 confirmed/cancelled；无列表/确认/作废接口，
 * 回单管理以送货单的 return_receipt_id 为入口。
 */
export interface ReturnReceipt {
  id: number
  delivery_id: number
  actual_received_date?: string | null
  signed_by?: string | null
  status: string
  items: Array<{
    id: number
    delivery_item_id: number
    delivered_quantity: number
    actual_received_quantity: number
    difference_reason?: string | null
  }>
}

/**
 * 可对账明细（GET /api/finance/pending_statements 真实字段）。
 * 注意：主键为 return_receipt_item_id，金额字段为 receivable_amount；
 * 仅返回已确认（confirmed）且未被对账单使用的回单明细。
 */
export interface PendingStatementItem {
  return_receipt_item_id: number
  actual_received_date?: string | null
  delivery_number?: string | null
  delivery_date?: string | null
  customer_id?: number
  order_number?: string | null
  customer_po?: string | null
  product_code?: string | null
  product_name?: string | null
  specification?: string | null
  actual_received_quantity?: number
  unit_price?: string | null
  receivable_amount?: string | null
  difference_reason?: string | null
}

/** 对账单（状态仅 unsettled/settled） */
export interface Statement {
  id: number
  statement_number: string
  customer_id: number
  customer_name?: string | null
  statement_month: string
  total_receivable?: string | null
  total_gross_profit?: string | null
  invoiced_amount?: string | null
  settled_amount?: string | null
  status: string
  confirmation_status?: string | null
  version: number
  ledger_version: number
  created_at?: string | null
}

export interface DashboardKpi {
  month: string
  monthly_revenue: string
  monthly_gross_profit: string
  outstanding_receivables: string
  today_pending_delivery_tasks: number
  today_pending_incoming_tasks: number
}

export interface UnorderedFinishedCandidate {
  inventory_lot_id: number
  inventory_lot_number: string
  product_id: number
  product_code: string
  product_name: string
  unit: string
  specification?: string | null
  location_name?: string | null
  available_quantity: number
  available_customer_quantity: number
  order_pending_quantity?: number
  quantity_contract?: { customer_basis: number; physical_basis: number; customer_unit: string; physical_unit: string } | null
  quantity_issue?: string | null
  unit_price?: string | null
  price_required: boolean
  customer_id: number
}

export interface AuthSession {
  ok: boolean
  user: AuthUser
  permissions: string[]
}

export interface DeliveryPrintData {
  id: number
  document_hash?: string
  price_display?: { shown: boolean }
  order_context?: string
  delivery_number: string
  delivery_date?: string | null
  vehicle_number?: string | null
  total_quantity: number
  total_actual_goods_quantity: number
  customer: { name: string; contact_person?: string | null; phone?: string | null; address?: string | null }
  sender: { company_name: string; address?: string | null; phone?: string | null }
  items: Array<{
    delivery_item_id: number
    customer_po?: string | null
    product_code?: string | null
    product_name?: string | null
    specification?: string | null
    unit?: string | null
    quantity: number
    actual_goods_quantity: number
    actual_goods_lines?: Array<{ product_code?: string | null; product_name?: string | null; quantity: number; unit?: string | null }>
    remarks?: string | null
  }>
}

export interface WarehouseLot {
  id: number
  lot_number: string
  inventory_type: 'finished' | 'semi_finished' | string
  quantity_available: number
  quantity_reserved: number
  unit: string
  display_unit?: string | null
  status: string
  age_warning_text?: string | null
  location?: {
    employee_location_name?: string | null
    location_name?: string | null
    warehouse_floor?: number | null
    area_code?: string | null
  } | null
  detail?: {
    owner_customer_name?: string | null
    inventory_code?: string | null
    product_name?: string | null
    internal_name?: string | null
    material_code?: string | null
    board_length_mm?: number | null
    board_width_mm?: number | null
  }
}

export interface WarehouseLotDetail extends WarehouseLot {
  timeline?: Array<{ event_type?: string; occurred_at?: string; description?: string }>
  movements?: Array<{ id?: number; movement_type?: string; quantity?: number; created_at?: string }>
  reservations?: Array<{ id?: number; order_item_id?: number; status?: string; reserved_stock_quantity?: number }>
}

export interface DeliveryPickTaskSummary {
  id: number
  delivery_id: number
  delivery_number?: string | null
  customer_name?: string | null
  status: string
  assigned_to_name?: string | null
  assignment_required: boolean
  has_exception: boolean
  created_at?: string | null
}

export interface PricingResult {
  box_category: string
  area_m2: string
  board_square_price: string
  extra_fee: string
  unit_price: string
}

/* ---------------- 鉴权（app/api/auth.py，前缀 /api/auth） ---------------- */

export const authApi = {
  login: (payload: { username: string; password: string; remember_me?: boolean }) =>
    request<AuthSession>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  me: () => request<AuthSession>('/api/auth/me'),
  /** 密码强度由后端统一校验，前端只核对两次输入是否一致 */
  changePassword: (payload: { current_password: string; new_password: string }) =>
    request<{ ok: boolean; user: AuthUser }>('/api/auth/password', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
}

/* ---------------- 基础资料 ---------------- */

export const masterApi = {
  getProduct: (id: number) => request<Product>(`/api/master/products/${id}`),
  // 客户：读 admin/sales/finance；写 admin
  listCustomers: (keyword = '', options: { includeInactive?: boolean; page?: number; pageSize?: number } = {}) => {
    const query = new URLSearchParams()
    if (keyword) query.set('keyword', keyword)
    query.set('include_inactive', String(options.includeInactive ?? false))
    query.set('page', String(options.page ?? 1))
    query.set('page_size', String(options.pageSize ?? 50))
    return request<PageResult<Customer>>(`/api/master/customers?${query}`)
  },
  createCustomer: (payload: Record<string, unknown>) =>
    request<Customer>('/api/master/customers', { method: 'POST', body: JSON.stringify(payload) }),
  updateCustomer: (id: number, payload: Record<string, unknown>) =>
    request<Customer>(`/api/master/customers/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  updateCustomerStatus: (id: number, isActive: boolean) =>
    request<Customer>(`/api/master/customers/${id}/status`, {
      method: 'PUT',
      body: JSON.stringify({ is_active: isActive }),
    }),
  deleteCustomer: (id: number) =>
    request<void>(`/api/master/customers/${id}`, { method: 'DELETE' }),

  // 材质：读 admin/sales/workshop；写 admin。注意：列表无 keyword 参数
  listMaterials: (page = 1, pageSize = 200) =>
    request<PageResult<Material>>(`/api/master/materials?page=${page}&page_size=${pageSize}`),
  createMaterial: (payload: Record<string, unknown>) =>
    request<Material>('/api/master/materials', { method: 'POST', body: JSON.stringify(payload) }),
  updateMaterial: (id: number, payload: Record<string, unknown>) =>
    request<Material>(`/api/master/materials/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteMaterial: (id: number) =>
    request<void>(`/api/master/materials/${id}`, { method: 'DELETE' }),

  // 常用箱：读 admin/sales/workshop；写 admin
  listProducts: (keyword = '', options: { customerId?: number; page?: number; pageSize?: number } = {}) => {
    const query = new URLSearchParams()
    if (keyword) query.set('keyword', keyword)
    if (options.customerId) query.set('customer_id', String(options.customerId))
    query.set('page', String(options.page ?? 1))
    query.set('page_size', String(options.pageSize ?? 50))
    return request<PageResult<Product>>(`/api/master/products?${query}`)
  },
  createProduct: (payload: Record<string, unknown>) =>
    request<Product>('/api/master/products', { method: 'POST', body: JSON.stringify(payload) }),
  updateProduct: (id: number, payload: Record<string, unknown>) =>
    request<Product>(`/api/master/products/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  updateProductStatus: (id: number, isActive: boolean) =>
    request<Product>(`/api/master/products/${id}/status`, {
      method: 'PUT',
      body: JSON.stringify({ is_active: isActive }),
    }),
  /** 移入垃圾站（admin；软删除，可恢复） */
  deleteProduct: (id: number) =>
    request<{ ok: boolean }>(`/api/master/products/${id}`, { method: 'DELETE' }),
  listProductTrash: () =>
    request<{ items: Product[] }>('/api/master/products/trash'),
  restoreProduct: (id: number) =>
    request<Product>(`/api/master/products/${id}/restore`, { method: 'PUT' }),
  purgeProduct: (id: number) =>
    request<void>(`/api/master/products/${id}/purge`, { method: 'DELETE' }),
  emptyProductTrash: () =>
    request<{ ok: boolean }>('/api/master/products/trash/empty', { method: 'POST' }),
}

/* ---------------- 计价（app/api/pricing.py，仅算单价；权限 admin/sales/finance） ---------------- */

export const pricingApi = {
  calculate: (payload: {
    box_category: string
    board_square_price: string
    length_mm?: string
    width_mm?: string
    height_mm?: string
    unfolded_length_mm?: string
    unfolded_width_mm?: string
    extra_fee?: string
  }) =>
    request<PricingResult>('/api/pricing/calculate', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
}

/* ---------------- 订单（app/api/orders.py） ---------------- */

export interface OrderSummary {
  id: number
  order_number: string
  customer_id: number
  customer_name: string
  customer_po?: string | null
  order_date: string
  delivery_date?: string | null
  status: string
  business_status_label?: string | null
  business_delivery_progress?: string | null
  total_quantity?: number
  business_remaining_quantity?: number
  item_count?: number
  total_amount?: string | null
}

export interface OrderDetail extends OrderSummary {
  remark?: string | null
  items: Array<{
    id: number
    client_line_id?: string
    requisition_hold?: { id: number; status: string; release_mode: string } | null
    fully_covered_by_finished_inventory?: boolean
    production_required_qty?: number
    snapshot_product_code?: string | null
    snapshot_product_name?: string | null
    snapshot_spec?: string | null
    snapshot_material?: string | null
    snapshot_production_notes?: string | null
    drawing_file?: string | null
    product_drawing_file?: string | null
    quantity: number
    delivered_quantity: number
    business_remaining_quantity?: number
    unit_price?: string | null
    subtotal?: string | null
  }>
}

export const orderApi = {
  uploadDraftDrawing: (file: File) => {
    const body = new FormData(); body.append('file', file)
    return request<{ token: string }>('/api/orders/draft-drawing', { method: 'POST', body })
  },
  drawingContent: (path: string) => request<Blob>(trustedDrawingPath(path), undefined, 'blob'),
  createOrder: (payload: {
    idempotency_key: string
    readback_contract?: 'a01-v1'
    customer_id: number
    customer_po?: string
    order_date: string
    delivery_date?: string
    remark?: string
    requisition_strategy?: 'normal' | 'wait_previous_batch'
    previous_batch_selections?: Array<{ client_line_id: string; previous_order_item_id: number }>
    items: Array<{
      product_id: number
      client_line_id?: string
      product_expected_version?: number
      quantity: number
      unit_price: string
      product_code?: string
      product_name?: string
      material?: string
      specification?: string
      production_notes?: string
      reservation_plan?: ReservationPlan
      temp_drawing_token?: string
      drawing_save_option?: DrawingSaveOption
    }>
  }) =>
    request<{ id: number; order_number: string; [key: string]: unknown }>('/api/orders', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  listOrders: (page = 1, pageSize = 50, filters: { keyword?: string; scope?: string } = {}) => {
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize), detail_level: 'summary' })
    if (filters.keyword) query.set('keyword', filters.keyword)
    if (filters.scope) query.set('scope', filters.scope)
    return request<PageResult<OrderSummary>>(`/api/orders?${query}`)
  },
  getOrder: (id: number, createKey?: string) =>
    request<OrderDetail>(`/api/orders/${id}${createKey ? `?create_key=${encodeURIComponent(createKey)}` : ''}`),
  updateOrder: (id: number, payload: { customer_po?: string; delivery_date?: string | null; remark?: string }) =>
    request<OrderDetail>(`/api/orders/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  createAttempt: (key: string) =>
    request<{ status: string; order?: { id: number; customer_id: number } }>(`/api/orders/create-attempts/${encodeURIComponent(key)}`),
  previewRequisitionHold: (payload: { customer_id: number; lines: Array<{
    client_line_id: string; product_code: string; specification: string;
    material: string; flute_type: string; quantity: number; finished_covered_quantity: number; reservation_plan?: ReservationPlan
  }> }) => request<{ items: OrderHoldPreview[] }>('/api/requisition/order-entry/hold-preview', {
    method: 'POST', body: JSON.stringify(payload),
  }),
}

/* ---------------- 报料（app/api/requisition.py，单数前缀；读 admin/sales） ---------------- */

export const requisitionApi = {
  listPending: () =>
    request<{ items: RequisitionPendingItem[] }>('/api/requisition/pending'),
  /** 报料记录（可用于查看已报料明细并取消） */
  listItems: (status = '') => {
    const query = new URLSearchParams()
    if (status) query.set('status', status)
    const suffix = query.toString() ? `?${query}` : ''
    return request<{ items: RequisitionItem[] }>(`/api/requisition/items${suffix}`)
  },
  /**
   * 生成报料批次。
   * 后端 RequisitionLinePayload 要求：order_item_id 必填；
   * cardboard_len / cardboard_width 必填且 > 0；
   * special_process ∈ {无,大做小,双拼,多拼}。
   */
  createBatch: (payload: {
    supplier_name?: string
    request_key: string
    items: Array<{
      order_item_id: number
      inventory_deducted_qty?: number
      requisition_qty?: number
      cardboard_len: string
      cardboard_width: string
      special_process?: string
      remark?: string
    }>
  }) =>
    request<{ id: number; requisition_number: string; [key: string]: unknown }>(
      '/api/requisition/batches',
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  /** 取消报料：仅 admin；仅对已报料明细有效（未报料会 409） */
  cancelItem: (itemId: number, reason: string) =>
    request<{ ok: boolean }>(`/api/requisition/items/${itemId}/cancel`, {
      method: 'PUT',
      body: JSON.stringify({ reason }),
    }),
  /** 登记供应商排单 */
  supplierSchedule: (itemId: number, payload: { supplier_delivery_time: string; supplier_order_number?: string }) =>
    request<{ ok: boolean }>(`/api/requisition/items/${itemId}/supplier-schedule`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  searchHistory: (keyword: string) => {
    const query = new URLSearchParams({ keyword })
    return request<{ items: HistorySuggestion[] }>(`/api/requisition/search_history?${query}`)
  },
  printBatch: (batchId: number) =>
    request<Record<string, unknown>>(`/api/requisition/batches/${batchId}/print`),
}

/* ---------------- 收料（app/api/incoming.py；读 admin/workshop） ---------------- */

export const incomingApi = {
  listPending: () => request<{ items: IncomingItem[] }>('/api/incoming/pending'),
  /** 注意：后端仅返回近 24 小时已收料记录 */
  listReceived: () => request<{ items: IncomingItem[] }>('/api/incoming/received'),
  /** 整单确认收料，无 body */
  receiveItem: (itemId: number) =>
    request<{ ok: boolean; item_id: number }>(`/api/incoming/receive/${itemId}`, { method: 'PUT' }),
  revertItem: (itemId: number) =>
    request<{ ok: boolean }>(`/api/incoming/revert/${itemId}`, { method: 'PUT' }),
}

/* ---------------- 出货（app/api/deliveries.py） ---------------- */

export const deliveryApi = {
  listPendingItems: () =>
    request<{ items: DeliveryPendingItem[] }>('/api/deliveries/pending_items'),
  /**
   * 创建送货单：items[].order_item_id + delivered_quantity 必填；
   * 车辆信息只有 vehicle_number（车牌），无司机字段。
   */
  createDelivery: (payload: {
    customer_id: number
    delivery_date?: string
    vehicle_number?: string
    items: Array<{ order_item_id: number; delivered_quantity: number; remarks?: string }>
  }) => request<DeliveryNote>('/api/deliveries', { method: 'POST', body: JSON.stringify(payload) }),
  listUnorderedCandidates: (customerId: number, keyword: string, page = 1) => {
    const query = new URLSearchParams({ customer_id: String(customerId), q: keyword.trim(), page: String(page), page_size: '12' })
    return request<PageResult<UnorderedFinishedCandidate>>(`/api/deliveries/unordered-finished-candidates?${query}`)
  },
  createUnorderedDelivery: (payload: {
    customer_id: number
    delivery_date: string
    vehicle_number?: string
    source_mode: 'unordered_finished'
    items: Array<{
      source_type: 'unordered_finished'
      product_id: number
      delivered_quantity: number
      unit_price: string
      allocations: Array<{ inventory_lot_id: number; quantity: number }>
    }>
  }) => request<DeliveryNote>('/api/deliveries', { method: 'POST', body: JSON.stringify(payload) }),
  listDeliveries: (page = 1, pageSize = 50, keyword = '') => {
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize), view: 'summary' })
    if (keyword.trim()) query.set('keyword', keyword.trim())
    return request<PageResult<DeliveryNote>>(`/api/deliveries?${query}`)
  },
  getDelivery: (id: number) => request<DeliveryNote>(`/api/deliveries/${id}`),
  getDeliveryPrint: (id: number) =>
    request<DeliveryPrintData>(`/api/deliveries/${id}/print`),
  dispatch: (id: number) => request<{ ok: boolean }>(`/api/deliveries/${id}/dispatch`, { method: 'PUT' }),
  markPrinted: (id: number) => request<{ ok: boolean }>(`/api/deliveries/${id}/printed`, { method: 'PUT' }),
  recordCustomerPrintRequest: (id: number, payload: {
    idempotency_key: string
    document_hash: string
    show_prices: boolean
    order_context: string
  }) => request<{ status: string }>(`/api/deliveries/${id}/customer-print-events`, {
    method: 'POST', body: JSON.stringify(payload),
  }),
}

/* ---------------- 仓库与拿货（只读预览） ---------------- */

export const warehouseApi = {
  finishedCandidates: (product: number, customer: number) => request<{ items: InventoryCandidate[] }>(`/api/warehouse/finished/products/${product}/candidates?customer_id=${customer}`),
  semiCandidates: (product: number, payload: object, manualPage?: number) => request<{ items: InventoryCandidate[]; total?: number }>(`/api/warehouse/semi-finished/products/${product}/${manualPage ? `inventory?page=${manualPage}&page_size=20` : 'candidates'}`, { method: 'POST', body: JSON.stringify(payload) }),
  previewDraft: (customer: number, items: InventoryDraftLine[]) => request<{ items: InventoryAuthority[] }>('/api/orders/inventory-draft-preview', { method: 'POST', body: JSON.stringify({ customer_id: customer, items }) }),
  listLots: (options: { keyword?: string; inventoryType?: string; page?: number; pageSize?: number } = {}) => {
    const query = new URLSearchParams()
    if (options.keyword) query.set('keyword', options.keyword)
    if (options.inventoryType) query.set('inventory_type', options.inventoryType)
    query.set('page', String(options.page ?? 1))
    query.set('page_size', String(options.pageSize ?? 50))
    return request<PageResult<WarehouseLot>>(`/api/warehouse/lots?${query}`)
  },
  getLot: (id: number) => request<WarehouseLotDetail>(`/api/warehouse/lots/${id}`),
  listPickTasks: (page = 1, pageSize = 50) =>
    request<PageResult<DeliveryPickTaskSummary>>(
      `/api/delivery-picks?response_mode=summary&page=${page}&page_size=${pageSize}`,
    ),
}

/* ---------------- 财务（app/api/finance.py；finance_only = admin/finance） ---------------- */

export const financeApi = {
  createReceipt: (payload: {
    delivery_id: number
    actual_received_date: string
    signed_by?: string
    items: Array<{ delivery_item_id: number; actual_received_quantity: number; difference_reason?: string }>
  }) => request<ReturnReceipt>('/api/finance/return_receipts', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
  updateReceipt: (
    id: number,
    payload: {
      actual_received_date: string
      signed_by?: string
      items: Array<{ delivery_item_id: number; actual_received_quantity: number; difference_reason?: string }>
    },
  ) =>
    request<ReturnReceipt>(`/api/finance/return_receipts/${id}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  getReceipt: (id: number) => request<ReturnReceipt>(`/api/finance/return_receipts/${id}`),
  /** 可对账明细：按 customer_id；仅 confirmed 且未被使用的回单明细 */
  listPendingStatements: (customerId: number, month: string) =>
    request<{ items: PendingStatementItem[] }>(
      `/api/finance/pending_statements?customer_id=${customerId}&statement_month=${encodeURIComponent(month)}`,
    ),
  createStatement: (payload: {
    customer_id: number
    statement_month: string
    idempotency_key: string
    return_receipt_item_ids: number[]
  }) =>
    request<Statement>('/api/finance/statements', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  listStatements: (page = 1, pageSize = 50) =>
    request<PageResult<Statement>>(`/api/finance/statements?page=${page}&page_size=${pageSize}`),
  getStatement: (id: number) => request<Statement>(`/api/finance/statements/${id}`),
  /** 登记收款：CAS 版本和幂等标识由后端校验 */
  settleStatement: (id: number, payload: { amount: string; settlement_date: string; account?: string; expected_version: number; expected_ledger_version: number; idempotency_key: string }) =>
    request<{ ok: boolean }>(`/api/finance/statements/${id}/settle`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  /** 对账单导出走浏览器直接导航（cookie 自动携带），返回可直接赋值的 URL */
  exportStatementUrl: (id: number) => `${API_BASE}/api/finance/statements/${id}/export`,
}

/* ---------------- 仪表盘（app/api/dashboard.py） ---------------- */

export const dashboardApi = {
  kpi: () => request<DashboardKpi>('/api/dashboard/kpi'),
}

export { API_BASE }
