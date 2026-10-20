import { API_BASE, request } from "../api/client";
export interface Entry {
  id: string;
  label: string;
  can_execute?: boolean;
  can_correct?: boolean;
}
export interface Shell {
  user: { id: number; role: string; display_name: string };
  entries: Entry[];
  search_categories?: string[];
  management_summary_allowed: boolean;
  delivery_margin_allowed: boolean;
}
export interface Drawing {
  id: string;
  kind: string;
  preview_url: string;
  original_url: string;
}
export interface StockSummary {
  actual?: number;
  available?: number;
  reserved?: number;
  unit?: string;
}
export interface Position {
  lot_id: number;
  location_id: number;
  location_label: string;
  actual: number;
  available: number;
  unit: string;
  inventory_type: string;
  shared_confirmed?: boolean;
  floor?: number;
}
export interface Product {
  id: number;
  product_id: number;
  product_code: string;
  product_name: string;
  customer_name: string;
  specification: string;
  is_active: boolean;
  report_length_mm?: number;
  report_width_mm?: number;
  report_material?: string;
  inventory_summary?: { finished?: StockSummary; visibility?: string };
  inventory_positions?: Position[];
  drawings?: { items: Drawing[] };
}
export interface Production {
  production_process?: string;
  process_steps?: { label: string; detail?: string }[];
  molds?: {
    short_label?: string;
    location_short_label?: string;
    location_label?: string;
  }[];
  joining_method?: string;
  crease_values_mm?: (string | number | null)[];
  report_length_mm?: number;
  report_width_mm?: number;
}
export interface ProductDetail {
  product: Product;
  production: Production;
  inventory: {
    items: Position[];
    summary?: { finished?: StockSummary };
    visibility?: string;
    source_summary?: Record<string, { label: string; actual: number }>;
  };
}
export interface Goods {
  lot_id: number;
  product_id?: number;
  product_code?: string;
  product_name: string;
  customer_name?: string;
  customer_short_name?: string;
  quantity_total: number;
  quantity_available: number;
  unit: string;
  stocktake_quantity?: number | null;
}
export interface Location {
  location_id: number;
  short_location_label?: string;
  employee_location_name?: string;
  location_name: string;
  geometry: {
    left_pct: number;
    top_pct: number;
    width_pct: number;
    height_pct: number;
  } | null;
  goods: Goods[];
  can_select_target?: boolean;
}
export interface Floor {
  floor_code: string;
  floor_name: string;
  floor_number: number;
  areas: { area_code: string; area_name: string }[];
}
export interface Area {
  feature_id: string;
  area_code: string;
  area_name: string;
  points: number[][];
}
export interface Incoming {
  item_id: number | string;
  product_id?: number;
  product_code?: string;
  product_name?: string;
  customer_name?: string;
  supplier_name?: string;
  snapshot_supplier_name?: string;
  received_quantity_this_time?: number;
  incoming_quantity?: number;
  receipt_item_id?: number;
  history_key?: string;
  can_revert_receipt?: boolean;
  supplier_delivery_time?: string;
  material_received_at?: string;
  cardboard_len?: number;
  cardboard_width?: number;
  report_length_mm?: number;
  report_width_mm?: number;
  requisition_spec?: string;
  flute_type?: string;
  material?: string;
  quantity: number;
  remaining_quantity?: number;
  requisition_qty?: number;
  production_detail_url?: string;
  drawing_path?: string;
  production_process?: string;
  stock_yield_per_sheet?: number;
  received_by_name?: string;
}
export interface IncomingDetail {
  incoming_item: Incoming;
  production_tasks: {
    process_tags?: string[];
    drawing_path?: string;
    product_code?: string;
    product_name?: string;
    customer_name?: string;
  }[];
}
export interface Unfulfilled {
  order_item_id: number;
  product_id?: number;
  product_code: string;
  product_name: string;
  customer_name: string;
  remaining_quantity: number;
  specification: string;
}
export interface Overview {
  business_status_counts?: Record<string, number>;
  cards?: { key: string; count: number }[];
  month?: string;
}
export interface Margin {
  summary: {
    sales_amount: string | null;
    material_cost: string | null;
    material_margin: string | null;
    material_margin_rate: string | null;
  };
}
const get = <T>(path: string, signal?: AbortSignal) =>
  request<T>(path, { signal });
export const mobileApi = {
  unfulfilled: (page = 1, signal?: AbortSignal) =>
    get<{ groups: { total: number; page: number; items: Unfulfilled[] }[] }>(
      `/api/mobile/erp/search?${new URLSearchParams({ category: "orders", unfulfilled_only: "true", page: String(page), page_size: "12" })}`,
      signal,
    ),
  shell: () => get<Shell>("/api/mobile/erp/shell"),
  overview: () => get<Overview>("/api/dashboard/overview"),
  margin: () =>
    get<Margin>("/api/dashboard/customer-delivery-margin?page_size=1"),
  products: (params: URLSearchParams, signal?: AbortSignal) =>
    get<{ items: Product[]; total: number; page: number; page_size: number }>(
      `/api/product-workbench/search?${params}`,
      signal,
    ),
  product: (id: string, signal?: AbortSignal) =>
    get<ProductDetail>(
      `/api/product-workbench/products/${encodeURIComponent(id)}`,
      signal,
    ),
  floors: () =>
    get<{ floors: Floor[]; can_execute: boolean; can_correct: boolean }>(
      "/api/mobile/erp/warehouse/map/floors",
    ),
  overviewMap: (floor: string, signal?: AbortSignal) =>
    get<{ areas: Area[]; map_status: string }>(
      `/api/mobile/erp/warehouse/map/overview/${encodeURIComponent(floor)}`,
      signal,
    ),
  area: (floor: string, area: string, signal?: AbortSignal) =>
    get<{ locations: Location[] }>(
      `/api/mobile/erp/warehouse/map/floors/${encodeURIComponent(floor)}?area_code=${encodeURIComponent(area)}`,
      signal,
    ),
  locations: (q: string, signal?: AbortSignal) =>
    get<{
      items: {
        location_id: number;
        location_name: string;
        floor_code: string;
        area_code: string;
      }[];
      total: number;
    }>(
      `/api/mobile/erp/warehouse/map/locations-search?${new URLSearchParams({ q, page_size: "12" })}`,
      signal,
    ),
  pending: (signal?: AbortSignal) =>
    get<{ items: Incoming[] }>("/api/incoming/pending", signal),
  received: (signal?: AbortSignal) =>
    get<{ items: Incoming[] }>("/api/incoming/received", signal),
  incomingDetail: (id: string, signal?: AbortSignal) =>
    get<IncomingDetail>(
      `/api/mobile/erp/incoming/${encodeURIComponent(id)}/production-detail`,
      signal,
    ),
};
/** Only same-backend URLs can receive session cookies; never follow a QR-provided origin. */
export function backendUrl(path: string): string {
  const base = new URL(API_BASE || "/", window.location.origin);
  const url = new URL(path, base);
  if (
    url.origin !== base.origin ||
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password
  )
    return "";
  return url.href;
}
export function legacyUrl(hash: "predelivery" | "production"): string {
  return backendUrl(`/mobile/erp.html#${hash === "predelivery" ? "pre_delivery" : hash}`);
}
export function stocktakeUrl(
  location: number,
  floor?: string,
  area?: string,
): string {
  return backendUrl(
    `/mobile/stocktake.html?${new URLSearchParams({ location_id: String(location), ...(floor ? { return_floor: floor } : {}), ...(area ? { return_area: area } : {}) })}`,
  );
}
