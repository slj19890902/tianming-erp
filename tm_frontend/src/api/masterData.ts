const API_BASE =
  import.meta.env.VITE_API_BASE ||
  `${window.location.protocol}//${window.location.hostname}:8002`

export interface PageResult<T> {
  total: number
  page: number
  page_size: number
  items: T[]
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
  delivery_method: '自提' | '配送' | '物流'
  default_tax_rate?: string
  note?: string | null
  is_active: boolean
}

export interface CustomerListOptions {
  includeInactive?: boolean
  page?: number
  pageSize?: number
}

export interface FluteType {
  id: number
  code: string
  name: string
  add_width_mm: string
  basis_weight_gsm?: string | null
  freight_rate?: string | null
  loss_rate: string
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

export interface Product {
  id: number
  customer_id: number
  customer_name?: string | null
  product_code: string
  customer_material_code: string
  product_name: string
  material_id?: number | null
  material_code?: string | null
  default_material_text?: string | null
  flute_type_id?: number | null
  flute_type_code?: string | null
  length_mm?: string | null
  width_mm?: string | null
  height_mm?: string | null
  box_category: 'normal' | 'die_cut'
  box_style?: string | null
  production_process?: string | null
  default_score_line?: string | null
  default_cardboard_length_mm?: string | null
  default_cardboard_width_mm?: string | null
  default_unit_price?: string | null
  historical_search_key?: string | null
  historical_style_no?: string | null
  historical_material_code?: string | null
  note?: string | null
  is_active: boolean
}

export interface ProductHistoryMatch {
  id: number
  customer_id: number
  customer_name?: string | null
  historical_search_key: string
  style_no?: string | null
  product_name: string
  paper_length_mm?: string | null
  paper_width_mm?: string | null
  score_line?: string | null
  material?: string | null
}

export interface ProductHistorySearchResult {
  matched: boolean
  item: ProductHistoryMatch | null
  items: ProductHistoryMatch[]
}

export interface CartonCalculationPayload {
  length_mm: string
  width_mm: string
  height_mm: string
  box_category?: 'normal' | 'die_cut'
  length_extra_mm?: string
  width_extra_mm?: string
  glue_flap_mm?: string
  customer_square_price?: string
  supplier_square_price?: string
  extra_fee?: string
}

export interface CartonCalculationResult {
  paper_length_mm: string
  paper_width_mm: string
  score_line: string
  area_m2: string
  sale_unit_price: string
  purchase_unit_cost: string
}

export interface OrderPayload {
  customer_id: number
  customer_po?: string
  order_date: string
  delivery_date?: string
  note?: string
  items: Array<Record<string, unknown>>
}

export interface OrderResult {
  id: number
  order_number: string
  total_amount: string
  items: Array<Record<string, unknown>>
}

export interface PendingRequisitionItem {
  order_item_id: number
  order_number: string
  customer_name: string
  product_code?: string
  product_name: string
  spec?: string
  material?: string
  flute_type?: string
  paper_length_mm?: string
  paper_width_mm?: string
  score_line?: string
  quantity: number
  requisition_qty: number
  delivery_date?: string
}

export interface RequisitionResult {
  id: number
  requisition_number: string
  supplier_name: string
  total_qty: number
  print_url: string
  mobile_receive_url: string
  items: WmsPendingItem[]
}

export interface WmsPendingItem {
  id: number
  requisition_id: number
  requisition_number: string
  supplier_name: string
  order_number: string
  customer_name: string
  product_name: string
  spec?: string
  material?: string
  flute_type?: string
  paper_length_mm?: string
  paper_width_mm?: string
  score_line?: string
  requisition_qty: number
  received_qty: number
  remaining_qty: number
  status: string
}

export interface WmsProcessPanel {
  requisition_item_id: number
  order_number: string
  customer_name: string
  product_name: string
  spec?: string
  material?: string
  paper_length_mm?: string
  paper_width_mm?: string
  score_line?: string
  process_route: string[]
  drawing_status: string
}

export interface DeliveryPendingItem {
  order_item_id: number
  order_number: string
  customer_id: number
  customer_name: string
  customer_po?: string | null
  product_code?: string | null
  product_name: string
  spec?: string | null
  material?: string | null
  quantity: number
  delivered_quantity: number
  remaining_qty: number
  unit_price: string
  delivery_date?: string | null
}

export interface DeliveryItem {
  id: number
  order_item_id: number
  order_number: string
  customer_po?: string | null
  product_code?: string | null
  product_name: string
  spec?: string | null
  delivery_qty: number
  unit_price: string
  amount: string
  remark?: string | null
}

export interface DeliveryNote {
  id: number
  delivery_number: string
  customer_id: number
  customer_name?: string | null
  delivery_date: string
  vehicle_number?: string | null
  driver_name?: string | null
  status: string
  total_quantity: number
  items: DeliveryItem[]
}

export interface ReceiptItem {
  id: number
  delivery_item_id: number
  order_number: string
  product_name: string
  delivery_qty: number
  actual_signed_qty: number
  unit_price: string
  amount: string
  difference_reason?: string | null
}

export interface ReturnReceipt {
  id: number
  delivery_id: number
  delivery_number: string
  customer_id: number
  customer_name?: string | null
  actual_received_date: string
  signed_by?: string | null
  status: string
  owner_reviewed_by?: string | null
  owner_reviewed_at?: string | null
  items: ReceiptItem[]
}

export interface StatementItem {
  id: number
  receipt_item_id: number
  delivery_date: string
  delivery_number: string
  order_number: string
  customer_po?: string | null
  product_code?: string | null
  product_name: string
  spec?: string | null
  actual_signed_qty: number
  unit_price: string
  amount: string
  remark?: string | null
}

export interface Statement {
  id: number
  statement_number: string
  customer_id: number
  customer_name?: string | null
  statement_month: string
  start_date: string
  end_date: string
  total_quantity: number
  total_amount: string
  status: string
  items: StatementItem[]
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  })
  if (!response.ok) {
    let message = `Request failed with status code ${response.status}`
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') message = body.detail
      else if (Array.isArray(body.detail)) {
        message = body.detail
          .map((item: { msg?: string }) => item.msg || JSON.stringify(item))
          .join('；')
      }
      else if (body.detail) message = JSON.stringify(body.detail)
    } catch {
      // Keep HTTP status fallback.
    }
    throw new Error(message)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const masterDataApi = {
  listCustomers: (keyword = '', options: CustomerListOptions = {}) => {
    const query = new URLSearchParams()
    if (keyword) query.set('keyword', keyword)
    query.set('include_inactive', String(options.includeInactive ?? false))
    query.set('page', String(options.page ?? 1))
    query.set('page_size', String(options.pageSize ?? 50))
    return request<PageResult<Customer>>(`/api/master/customers?${query}`)
  },
  createCustomer: (payload: Partial<Customer>) =>
    request<Customer>('/api/master/customers', { method: 'POST', body: JSON.stringify(payload) }),
  updateCustomer: (id: number, payload: Partial<Customer>) =>
    request<Customer>(`/api/master/customers/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  updateCustomerStatus: (id: number, isActive: boolean) =>
    request<Customer>(`/api/master/customers/${id}/status`, {
      method: 'PUT',
      body: JSON.stringify({ is_active: isActive }),
    }),
  deleteCustomer: (id: number) =>
    request<void>(`/api/master/customers/${id}`, { method: 'DELETE' }),

  listFluteTypes: (keyword = '') =>
    request<PageResult<FluteType>>(`/api/master/flute-types?keyword=${encodeURIComponent(keyword)}`),
  createFluteType: (payload: Partial<FluteType>) =>
    request<FluteType>('/api/master/flute-types', { method: 'POST', body: JSON.stringify(payload) }),
  updateFluteType: (id: number, payload: Partial<FluteType>) =>
    request<FluteType>(`/api/master/flute-types/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteFluteType: (id: number) =>
    request<void>(`/api/master/flute-types/${id}`, { method: 'DELETE' }),

  listMaterials: (keyword = '') =>
    request<PageResult<Material>>(`/api/master/materials?keyword=${encodeURIComponent(keyword)}`),
  createMaterial: (payload: Partial<Material>) =>
    request<Material>('/api/master/materials', { method: 'POST', body: JSON.stringify(payload) }),
  updateMaterial: (id: number, payload: Partial<Material>) =>
    request<Material>(`/api/master/materials/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteMaterial: (id: number) =>
    request<void>(`/api/master/materials/${id}`, { method: 'DELETE' }),

  listProducts: (keyword = '') =>
    request<PageResult<Product>>(`/api/master/products?keyword=${encodeURIComponent(keyword)}`),
  createProduct: (payload: Partial<Product>) =>
    request<Product>('/api/master/products', { method: 'POST', body: JSON.stringify(payload) }),
  updateProduct: (id: number, payload: Partial<Product>) =>
    request<Product>(`/api/master/products/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteProduct: (id: number) =>
    request<void>(`/api/master/products/${id}`, { method: 'DELETE' }),
  searchProductHistory: (keyword: string) =>
    request<ProductHistorySearchResult>(
      `/api/master/products/history-search?keyword=${encodeURIComponent(keyword)}&limit=12`,
    ),

  calculateCarton: (payload: CartonCalculationPayload) =>
    request<CartonCalculationResult>('/api/engine/calculate-carton', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  createOrder: (payload: OrderPayload) =>
    request<OrderResult>('/api/orders', { method: 'POST', body: JSON.stringify(payload) }),

  listPendingRequisitionItems: () =>
    request<PageResult<PendingRequisitionItem>>('/api/requisitions/pending-items'),
  createRequisition: (payload: { supplier_name: string; items: Array<{ order_item_id: number; requisition_qty: number }> }) =>
    request<RequisitionResult>('/api/requisitions', { method: 'POST', body: JSON.stringify(payload) }),
  listWmsPending: (params: { supplier_name?: string; requisition_id?: number } = {}) => {
    const query = new URLSearchParams()
    if (params.supplier_name) query.set('supplier_name', params.supplier_name)
    if (params.requisition_id) query.set('requisition_id', String(params.requisition_id))
    return request<{ total: number; summary: Array<{ supplier_name: string; remaining_qty: number }>; items: WmsPendingItem[] }>(
      `/api/wms/pending${query.toString() ? `?${query}` : ''}`,
    )
  },
  receiveWmsItem: (id: number, payload: { actual_receive_qty: number; received_by?: string }) =>
    request<WmsPendingItem>(`/api/wms/receive/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  getWmsProcessPanel: (id: number) =>
    request<WmsProcessPanel>(`/api/wms/requisition-items/${id}/process-panel`),

  listDeliveryPendingItems: () =>
    request<{ total: number; items: DeliveryPendingItem[] }>('/api/deliveries/pending-items'),
  createDelivery: (payload: {
    customer_id: number
    delivery_date: string
    vehicle_number?: string
    driver_name?: string
    items: Array<{ order_item_id: number; delivery_qty: number; remark?: string }>
  }) => request<DeliveryNote>('/api/deliveries', { method: 'POST', body: JSON.stringify(payload) }),
  listDeliveries: (status?: string) =>
    request<{ total: number; items: DeliveryNote[] }>(`/api/deliveries${status ? `?status=${encodeURIComponent(status)}` : ''}`),
  createReceipt: (payload: {
    delivery_id: number
    actual_received_date: string
    signed_by?: string
    items: Array<{ delivery_item_id: number; actual_signed_qty: number; difference_reason?: string }>
  }) => request<ReturnReceipt>('/api/receipts', { method: 'POST', body: JSON.stringify(payload) }),
  listReceipts: (status?: string) =>
    request<{ total: number; items: ReturnReceipt[] }>(`/api/receipts${status ? `?status=${encodeURIComponent(status)}` : ''}`),
  ownerConfirmReceipt: (id: number, reviewedBy = '老板') =>
    request<ReturnReceipt>(`/api/receipts/${id}/owner-confirm`, {
      method: 'POST',
      body: JSON.stringify({ reviewed_by: reviewedBy }),
    }),
  generateStatement: (payload: { customer_id: number; start_date: string; end_date: string }) =>
    request<Statement>('/api/statements/generate', { method: 'POST', body: JSON.stringify(payload) }),
  exportStatementUrl: (id: number) => `${API_BASE}/api/statements/${id}/export`,
}
