import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EditorCanvas, type CanvasFocusTarget } from "./EditorCanvas";
import { filterOperationalFeatures } from "./operationalView.mjs";
import {
  buildMappedLocationPallets,
  expandAreaInventory,
  filterAreaInventory,
  findPalletColumnConflicts,
  inventoryAgeLabel,
  inventoryAgeTone,
  inventoryUnitLabel,
  locationLayoutGeometry,
  searchHighlightAreaCodes
} from "./warehouseInventory.mjs";
import type {
  AssetTemplate,
  CameraPreset,
  LayerVisibility,
  Layout,
  LayoutFeature,
  Pallet,
  ProductionProjectionResponse,
  ProductionTaskProjection,
  Rack,
  SelectedEntity,
  ViewMode
} from "./types";

type TwinFeature = LayoutFeature & { erp_area_code?: string | null };
type InventoryUsage = "finished" | "semi_finished" | "raw_material" | "mold" | "print_plate" | "temporary_turnover";
type StorageLayout = "rack" | "pallet_ground" | "mixed";
type WarehouseSearchType = "finished" | "mold" | "printing_plate";
type InboundInventoryType = "finished" | "semi_finished";
type RackDraft = Rack & { level_clear_heights_mm: number[] };

interface LayoutMutationResponse<T> {
  item: T;
  revision: string;
  applied: boolean;
}

interface QuantityGroup {
  key: string;
  label: string;
  available: number;
  reserved: number;
  damaged: number;
  lot_count: number;
  unit: string;
}

interface AreaDistribution {
  floor_code: string;
  area_code: string;
  lot_count: number;
  quantities: QuantityGroup[];
}

interface InventoryItem {
  lot_id: number;
  inventory_type?: "finished" | "semi_finished";
  customer_id?: number | null;
  lot_number?: string;
  inventory_code?: string;
  product_name?: string;
  customer_name?: string;
  quantity?: number;
  available_quantity?: number;
  reserved_quantity?: number;
  unit?: string;
  age_days?: number | null;
  version?: number;
  specification?: string | null;
  material?: string | null;
}

interface RackInventoryItem extends InventoryItem {
  location_code?: string | null;
  location_name?: string | null;
  pallet_code?: string | null;
}

interface DashboardLocation {
  location_id: number;
  location_code: string;
  location_name: string;
  floor_code: string;
  area_code: string | null;
  warehouse_type: string;
  storage_type: string;
  is_active: boolean;
  occupancy_status: "occupied" | "empty";
  position_status: string;
  map_position: {
    left_pct: number;
    top_pct: number;
    width_pct: number;
    height_pct: number;
    version: number;
    z_index: number;
  } | null;
  layout_draft_position?: {
    left_pct: number;
    top_pct: number;
    width_pct: number;
    height_pct: number;
    version: number;
    z_index: number;
  } | null;
  pallet: { pallet_id: number; pallet_code: string; version: number; item_count: number; items: InventoryItem[] } | null;
  loose_items: InventoryItem[];
}

type LocationLayoutGeometry = NonNullable<ReturnType<typeof locationLayoutGeometry>>;

interface AuthResponse {
  user: { role: string; ui_mode?: "standard" | "large" };
  permissions: string[];
}

interface TwinDashboard {
  generated_at: string;
  read_only: boolean;
  scope: { notice: string };
  summary: {
    active_lots: number;
    occupied_pallets: number;
    long_age_lots: number;
    unlocated_lots: number;
  };
  floors: Array<{ floor_code: string; occupied_locations: number; active_lots: number }>;
  locations: DashboardLocation[];
  distribution: { areas: AreaDistribution[] };
}

interface SearchItem extends InventoryItem {
  floor_code: string;
  area_code: string | null;
  location_id: number | null;
  location_code: string | null;
  location_name: string;
  position_status: string;
}

interface SearchResponse {
  search_type?: WarehouseSearchType | "all";
  result_count: number;
  inventory_result_count: number;
  resource_result_count: number;
  items: SearchItem[];
  resources: LocateResource[];
  notice: string;
}

interface SearchProductGroup {
  key: string;
  customer_id: number | null;
  customer_name: string;
  inventory_code: string;
  product_name: string;
  total_quantity: number;
  unit: string;
  location_count: number;
  items: SearchItem[];
}

interface LocateResource {
  resource_id: string;
  kind: "delivery_pick" | "mold" | "printing_plate" | "mold_area" | "printing_plate_area";
  primary_code?: string | null;
  title: string;
  subtitle: string;
  floor_code: string;
  area_code?: string | null;
  location_id?: number | null;
  location_code?: string | null;
  pallet_id?: number | null;
  feature_codes: string[];
  map_status: string;
  prompt: string;
}

interface ProductCandidate {
  product_id: number;
  customer_id: number;
  customer_name: string;
  product_code?: string | null;
  customer_material_code?: string | null;
  product_name: string;
  specification?: string;
}

interface ProductCandidatesResponse {
  items: ProductCandidate[];
}

interface StagingProductCandidate extends InventoryItem {
  total_quantity: number;
  customer_id?: number | null;
  product_id?: number | null;
  source_location_id: number;
  source_location_code: string;
  source_location_name: string;
  stock_date: string;
}

interface StagingProductCandidatesResponse {
  items: StagingProductCandidate[];
}

interface CustomerOption {
  id: number;
  name: string;
  customer_code?: string | null;
}

interface CustomerOptionsResponse {
  items: CustomerOption[];
}

interface AreaLocationCountResponse {
  area_code: string;
  target_count: number;
  active_count: number;
  created_count: number;
  enabled_count: number;
  disabled_count: number;
  message: string;
  items: Array<{
    action: "created" | "enabled" | "disabled";
    location: { id: number; location_code: string; location_name: string };
    layout: DashboardLocation["map_position"];
  }>;
}

interface TwinFloorResponse {
  layout_id: string;
  name: string;
  floor_code: string;
  source_name: string;
  source_sha256?: string;
  source_units: string;
  bounds_mm: Layout["bounds_mm"];
  structures: Layout["structures"];
  features: TwinFeature[];
  placements: Layout["placements"];
  racks: Rack[];
  pallets?: Pallet[];
  assets?: AssetTemplate[];
  warnings?: string[];
  source_created_at?: string;
  source_updated_at?: string;
  revision: string;
  projection_notice: string;
}

const DEFAULT_LAYERS: LayerVisibility = {
  structures: true,
  equipment: true,
  racks: true,
  pallets: true,
  zones: true,
  aisles: true,
  noGo: true,
  customStructures: true,
  labels: false,
  production: true
};

const noop = () => undefined;
const EMPTY_CANVAS_POINTS: number[][] = [];
const EMPTY_PRODUCTION_PROJECTIONS: ProductionTaskProjection[] = [];

function apiErrorMessage(body: unknown, status: number) {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return status === 401 ? "登录状态已失效" : `请求失败（${status}）`;
}

async function requestJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      const embedded = new URLSearchParams(window.location.search).get("embedded") === "1";
      const target = embedded ? "/?page=warehouse" : "/?redirect=%2Fwarehouse.html";
      if (embedded && window.top && window.top !== window) window.top.location.replace(target);
      else window.location.replace(target);
    }
    throw new Error(apiErrorMessage(body, response.status));
  }
  return body as T;
}

async function mutateJson<T>(path: string, method: "POST" | "PUT" | "PATCH" | "DELETE", body?: unknown): Promise<T | null> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const payload = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(apiErrorMessage(payload, response.status));
  return payload as T | null;
}

function hydrateLayout(raw: TwinFloorResponse): Layout {
  const operationalFeatures = filterOperationalFeatures(raw.floor_code, raw.bounds_mm, raw.features || []);
  return {
    id: raw.layout_id,
    name: raw.name,
    floor_code: raw.floor_code,
    source_name: raw.source_name,
    source_sha256: raw.source_sha256 || raw.revision,
    source_units: raw.source_units,
    bounds_mm: raw.bounds_mm,
    created_at: raw.source_created_at || raw.source_updated_at || "",
    updated_at: raw.source_updated_at || "",
    structures: raw.structures || [],
    warnings: raw.warnings || [],
    placements: raw.placements || [],
    racks: raw.racks || [],
    pallets: raw.pallets || [],
    features: operationalFeatures,
    violations: [],
    rule_defaults: {}
  };
}

function formatNumber(value: number | null | undefined) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function inventoryLabelQuantity(item: InventoryItem) {
  if (item.quantity !== undefined && item.quantity !== null) return item.quantity;
  return Number(item.available_quantity || 0) + Number(item.reserved_quantity || 0);
}

function searchProductKey(item: Pick<InventoryItem, "customer_id" | "customer_name" | "inventory_code" | "product_name">) {
  return [item.customer_id || 0, item.customer_name || "", item.inventory_code || "", item.product_name || ""].join("::").toLocaleLowerCase("zh-CN");
}

function groupSearchProducts(items: SearchItem[]): SearchProductGroup[] {
  const groups = new Map<string, SearchProductGroup>();
  for (const item of items) {
    const key = searchProductKey(item);
    const current = groups.get(key) || {
      key,
      customer_id: item.customer_id || null,
      customer_name: item.customer_name || "客户待确认",
      inventory_code: item.inventory_code || item.lot_number || `批次 ${item.lot_id}`,
      product_name: item.product_name || "产品名称待补充",
      total_quantity: 0,
      unit: item.unit || "boxes",
      location_count: 0,
      items: []
    };
    current.total_quantity += Number(item.quantity ?? inventoryLabelQuantity(item));
    current.items.push(item);
    current.location_count = new Set(current.items.map((row) => row.location_id || `text:${row.location_name}`)).size;
    groups.set(key, current);
  }
  return [...groups.values()].sort((left, right) => left.customer_name.localeCompare(right.customer_name, "zh-CN") || left.inventory_code.localeCompare(right.inventory_code, "zh-CN", { numeric: true }));
}

function formatTime(value: string | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function operationKey(prefix: string) {
  const suffix = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}

function rackClearHeights(rack: Pick<Rack, "levels" | "height_mm" | "level_heights_mm">) {
  const shelves = [...(rack.level_heights_mm || [])].sort((left, right) => left - right);
  const boundaries = [0, ...shelves, rack.height_mm];
  if (boundaries.length !== rack.levels + 1) {
    return Array.from({ length: rack.levels }, () => Math.round(rack.height_mm / rack.levels));
  }
  return boundaries.slice(1).map((value, index) => Math.round(value - boundaries[index]));
}

function rackShelfHeights(clearHeights: number[]) {
  let cumulative = 0;
  return clearHeights.slice(0, -1).map((height) => {
    cumulative += Number(height) || 0;
    return cumulative;
  });
}

function rackDraft(rack: Rack): RackDraft {
  return { ...rack, level_clear_heights_mm: rackClearHeights(rack) };
}

function featureCenter(feature: LayoutFeature) {
  const points = feature.points || [];
  if (!points.length) return { x: 0, y: 0 };
  return {
    x: points.reduce((sum, point) => sum + Number(point[0] || 0), 0) / points.length,
    y: points.reduce((sum, point) => sum + Number(point[1] || 0), 0) / points.length
  };
}

function pointInPolygon(x: number, y: number, points: number[][]) {
  let inside = false;
  for (let index = 0, previous = points.length - 1; index < points.length; previous = index++) {
    const [xi, yi] = points[index];
    const [xj, yj] = points[previous];
    const intersects = yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi || 1) + xi;
    if (intersects) inside = !inside;
  }
  return inside;
}

function featureAreaCode(feature: LayoutFeature | undefined) {
  return (feature as TwinFeature | undefined)?.erp_area_code || null;
}

function rackAreaCode(rack: Rack, features: LayoutFeature[]) {
  if (rack.area_code) return rack.area_code.toUpperCase();
  const zone = features.find((item) => item.feature_kind === "zone" && item.points.length > 2 && pointInPolygon(rack.x_mm, rack.y_mm, item.points));
  const geometryAreaCode = featureAreaCode(zone);
  if (geometryAreaCode) return geometryAreaCode;

  // Rack centres can sit exactly on a measured zone edge after manual fine-tuning.
  // The rack code/name prefix is the operator-confirmed readable area identity;
  // using it here changes display grouping only and never writes an ERP mapping.
  const rackPrefix = rack.rack_code.match(/^RACK-[^-]+-([A-Z]+\d+)/i)?.[1]?.toUpperCase();
  const namePrefix = rack.name.match(/^([A-Z]+\d+)/i)?.[1]?.toUpperCase();
  const candidate = rackPrefix || namePrefix;
  return candidate || null;
}

function WarehouseRackElevation({
  rack,
  areaCode,
  area,
  items,
  emptyLocations,
  canChooseProducts,
  rackIndex,
  rackCount,
  onPrevious,
  onNext,
  onChooseEmptyLocation,
  onClose
}: {
  rack: Rack;
  areaCode?: string | null;
  area?: AreaDistribution;
  items: RackInventoryItem[];
  emptyLocations: DashboardLocation[];
  canChooseProducts: boolean;
  rackIndex: number;
  rackCount: number;
  onPrevious: () => void;
  onNext: () => void;
  onChooseEmptyLocation: (locationId: number) => void;
  onClose: () => void;
}) {
  const levels = Array.from({ length: Math.max(1, rack.levels) }, (_, index) => Math.max(1, rack.levels) - index);
  const bays = Math.max(1, Math.min(6, rack.cargo_rows || rack.bays || 3));
  const [selectedItem, setSelectedItem] = useState<RackInventoryItem | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  useEffect(() => { setSelectedItem(null); setDetailOpen(false); }, [rack.id]);
  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowLeft") onPrevious();
      if (event.key === "ArrowRight") onNext();
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose, onPrevious, onNext]);
  const slots = Array.from({ length: levels.length * bays }, (_, index) => ({
    item: items[index] || null,
    location: items[index] ? null : emptyLocations[index - items.length] || null
  }));
  return <div className="twin-rack-modal" role="dialog" aria-modal="true" aria-label={`${rack.rack_code} 参数化正视图`}>
    <button className="twin-modal-backdrop" type="button" aria-label="关闭货架正视图" onClick={onClose} />
    <section className="twin-rack-stage">
      <header>
      <div><small>PARAMETRIC RACK ELEVATION · {area?.area_code || areaCode || "未匹配区域"}</small><h2>{rack.name}</h2><p>{formatNumber(rack.width_mm)} × {formatNumber(rack.depth_mm)} × {formatNumber(rack.height_mm)} mm · {rack.levels} 层 · 同区货架 {rackIndex + 1}/{rackCount}</p></div>
        <button type="button" onClick={onClose}>返回孪生地图</button>
      </header>
      <div className="twin-rack-content">
        <button type="button" className="twin-rack-switch previous" aria-label="上一个同区域货架" onClick={onPrevious}>‹</button>
        <div className="twin-elevation-shell">
          <div className="twin-height-ruler"><b>{formatNumber(rack.height_mm)} mm</b></div>
          <div className="twin-elevation-frame">
            {levels.map((level, levelIndex) => <div className="twin-elevation-level" key={level}>
              <span>{level === 1 ? "地面栈板层" : `${level} 层`}</span>
              <div>{Array.from({ length: bays }, (_, bay) => {
                const slot = slots[levelIndex * bays + bay];
                const item = slot.item;
                if (item) return <button type="button" className={selectedItem?.lot_id === item.lot_id ? "selected" : ""} key={`${item.lot_id}-${bay}`} onClick={() => { setSelectedItem(item); setDetailOpen(false); }}><b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b><span>{item.customer_name || "客户待确认"}</span><strong>{formatNumber(inventoryLabelQuantity(item))} {inventoryUnitLabel(item.unit)}</strong></button>;
                if (slot.location) return <button type="button" className="empty" key={`empty-${slot.location.location_id}`} disabled={!canChooseProducts} onClick={() => onChooseEmptyLocation(slot.location!.location_id)}><b>＋ 为此货位选产品</b><span>{slot.location.location_name}</span><strong>当前空位</strong></button>;
                return <i key={bay}>暂无已建空货位</i>;
              })}</div>
            </div>)}
          </div>
          <div className="twin-width-ruler">正面宽度 {formatNumber(rack.width_mm)} mm</div>
        </div>
        <aside>
          <small>ERP PRODUCT LABEL</small>
          {!selectedItem ? <><h3>点击货架上的产品或空货位</h3><p>产品标签显示常用信息；管理员可从空货位直接选择待入位产品。</p><strong>{items.length} 条产品标签 · {emptyLocations.length} 个空货位</strong>{area?.quantities.map((item) => <div className="twin-quantity-row" key={item.key}><span>{item.label}</span><b>{formatNumber(item.available)} {inventoryUnitLabel(item.unit)}</b></div>)}</> : <article className="twin-rack-product-label">
            <span>当前产品标签</span>
            <h3>{selectedItem.inventory_code || selectedItem.lot_number || `批次 ${selectedItem.lot_id}`}</h3>
            <strong>{selectedItem.product_name || "产品名称待补充"}</strong>
            <dl><div><dt>客户</dt><dd>{selectedItem.customer_name || "待确认"}</dd></div><div><dt>产品数量</dt><dd>{formatNumber(inventoryLabelQuantity(selectedItem))} {inventoryUnitLabel(selectedItem.unit)}</dd></div></dl>
            <button type="button" className="twin-rack-detail-toggle" aria-expanded={detailOpen} onClick={() => setDetailOpen((value) => !value)}>{detailOpen ? "收起详情" : "查看详情"}</button>
            {detailOpen && <dl className="twin-rack-product-detail"><div><dt>可用数量</dt><dd>{formatNumber(selectedItem.available_quantity)} {inventoryUnitLabel(selectedItem.unit)}</dd></div><div><dt>已预占</dt><dd>{formatNumber(selectedItem.reserved_quantity)} {inventoryUnitLabel(selectedItem.unit)}</dd></div><div><dt>实际位置</dt><dd>{selectedItem.location_name || selectedItem.location_code || "待定位"}</dd></div><div><dt>栈板</dt><dd>{selectedItem.pallet_code || "地堆/散存"}</dd></div><div><dt>批次</dt><dd>{selectedItem.lot_number || "—"}</dd></div></dl>}
          </article>}
        </aside>
        <button type="button" className="twin-rack-switch next" aria-label="下一个同区域货架" onClick={onNext}>›</button>
      </div>
    </section>
  </div>;
}

export function WarehouseTwinApp() {
  const query = useMemo(() => new URLSearchParams(window.location.search), []);
  const embedded = query.get("embedded") === "1";
  const [floorCode, setFloorCode] = useState(query.get("floor")?.toUpperCase() === "1F" ? "1F" : "3F");
  const [viewMode, setViewMode] = useState<ViewMode>(query.get("view") === "25d" ? "25d" : "2d");
  const [cameraPreset, setCameraPreset] = useState<CameraPreset>("fit");
  const [viewResetToken, setViewResetToken] = useState(0);
  const [layout, setLayout] = useState<Layout | null>(null);
  const [assets, setAssets] = useState<AssetTemplate[]>([]);
  const [dashboard, setDashboard] = useState<TwinDashboard | null>(null);
  const [selected, setSelected] = useState<SelectedEntity>(null);
  const [layers, setLayers] = useState<LayerVisibility>(DEFAULT_LAYERS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [searchType, setSearchType] = useState<WarehouseSearchType>("finished");
  const [searchCustomerQuery, setSearchCustomerQuery] = useState("");
  const [searchCustomers, setSearchCustomers] = useState<CustomerOption[]>([]);
  const [searchCustomerId, setSearchCustomerId] = useState("");
  const [searchProductQuery, setSearchProductQuery] = useState("");
  const [searchResponse, setSearchResponse] = useState<SearchResponse | null>(null);
  const [focusedSearchItem, setFocusedSearchItem] = useState<SearchItem | null>(null);
  const [focusedSearchProductKey, setFocusedSearchProductKey] = useState<string | null>(null);
  const [focusedResource, setFocusedResource] = useState<LocateResource | null>(null);
  const [cameraFocusTarget, setCameraFocusTarget] = useState<CanvasFocusTarget | null>(null);
  const cameraFocusSequenceRef = useRef(0);
  const [pendingLocateResource, setPendingLocateResource] = useState<LocateResource | null>(null);
  const [pendingLocationId, setPendingLocationId] = useState<number | null>(null);
  const [areaInventorySearch, setAreaInventorySearch] = useState("");
  const [layerPanelOpen, setLayerPanelOpen] = useState(false);
  const [searchPanelOpen, setSearchPanelOpen] = useState(false);
  const [productionPanelOpen, setProductionPanelOpen] = useState(false);
  const [pendingAreaCode, setPendingAreaCode] = useState<string | null>(null);
  const [productionProjection, setProductionProjection] = useState<ProductionProjectionResponse | null>(null);
  const [productionTaskId, setProductionTaskId] = useState<number | null>(null);
  const [productionSearch, setProductionSearch] = useState("");
  const [productionTargetKind, setProductionTargetKind] = useState<"pallet" | "zone">("pallet");
  const [productionTargetId, setProductionTargetId] = useState("");
  const [productionBusy, setProductionBusy] = useState(false);
  const [productionMessage, setProductionMessage] = useState("");
  const [canEditLocations, setCanEditLocations] = useState(false);
  const [canViewProductionProjection, setCanViewProductionProjection] = useState(false);
  const [uiMode, setUiMode] = useState<"standard" | "large">("standard");
  const [locationEditMode, setLocationEditMode] = useState(false);
  const [areaPolicyEditMode, setAreaPolicyEditMode] = useState(false);
  const [locationDrafts, setLocationDrafts] = useState<Record<number, LocationLayoutGeometry>>({});
  const [rackDrafts, setRackDrafts] = useState<Record<string, RackDraft>>({});
  const [zonePolicyDrafts, setZonePolicyDrafts] = useState<Record<string, { allowed_inventory_types: InventoryUsage[]; storage_layout: StorageLayout }>>({});
  const [spatialEditBusy, setSpatialEditBusy] = useState(false);
  const [locationEditBusy, setLocationEditBusy] = useState(false);
  const [locationEditMessage, setLocationEditMessage] = useState("");
  const [swapSourceLocationId, setSwapSourceLocationId] = useState<number | null>(null);
  const [targetAreaLocationCount, setTargetAreaLocationCount] = useState("");
  const [warehouseOperationBusy, setWarehouseOperationBusy] = useState(false);
  const [warehouseOperationMessage, setWarehouseOperationMessage] = useState("");
  const [locationDetailOpen, setLocationDetailOpen] = useState(false);
  const [inboundMode, setInboundMode] = useState<"staging" | "catalog" | "temporary">("staging");
  const [inboundInventoryType, setInboundInventoryType] = useState<InboundInventoryType>("finished");
  const [inboundCustomerQuery, setInboundCustomerQuery] = useState("");
  const [inboundCustomers, setInboundCustomers] = useState<CustomerOption[]>([]);
  const [inboundCustomerId, setInboundCustomerId] = useState("");
  const [stagingQuery, setStagingQuery] = useState("");
  const [stagingCandidates, setStagingCandidates] = useState<StagingProductCandidate[]>([]);
  const [stagingLotId, setStagingLotId] = useState("");
  const [stagingIdempotencyKey, setStagingIdempotencyKey] = useState(() => operationKey("map-place-staging"));
  const [productQuery, setProductQuery] = useState("");
  const [productCandidates, setProductCandidates] = useState<ProductCandidate[]>([]);
  const [inboundProductId, setInboundProductId] = useState("");
  const [inboundQuantity, setInboundQuantity] = useState("");
  const [inboundStockDate, setInboundStockDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [inboundPalletCode, setInboundPalletCode] = useState("");
  const [inboundIdempotencyKey, setInboundIdempotencyKey] = useState(() => operationKey("map-inbound"));
  const [temporaryCustomerQuery, setTemporaryCustomerQuery] = useState("");
  const [temporaryCustomers, setTemporaryCustomers] = useState<CustomerOption[]>([]);
  const [temporaryCustomerId, setTemporaryCustomerId] = useState("");
  const [temporaryInventoryCode, setTemporaryInventoryCode] = useState("");
  const [temporaryProductName, setTemporaryProductName] = useState("");
  const [temporaryReason, setTemporaryReason] = useState("");
  const [temporaryIdempotencyKey, setTemporaryIdempotencyKey] = useState(() => operationKey("map-temporary-inbound"));
  const [moveTargetLocationId, setMoveTargetLocationId] = useState("");
  const [moveIdempotencyKey, setMoveIdempotencyKey] = useState(() => operationKey("map-move"));
  const [rackFocusId, setRackFocusId] = useState<string | null>(null);
  const [correctionLotId, setCorrectionLotId] = useState<number | null>(null);
  const [correctionQuantity, setCorrectionQuantity] = useState("");
  const [correctionReason, setCorrectionReason] = useState("");
  const [correctionIdempotencyKey, setCorrectionIdempotencyKey] = useState(() => operationKey("map-correction"));

  const refreshDashboard = useCallback(async () => {
    const value = await requestJson<TwinDashboard>("/api/warehouse/twin-dashboard/overview?days=30");
    setDashboard(value);
    return value;
  }, []);

  useEffect(() => {
    refreshDashboard().catch((reason: Error) => setError(reason.message));
    requestJson<AuthResponse>("/api/auth/me")
      .then((value) => {
        setCanEditLocations(value.user.role === "admin");
        setCanViewProductionProjection(value.permissions.includes("warehouse.view"));
        setUiMode(value.user.ui_mode === "large" ? "large" : "standard");
      })
      .catch(() => {
        setCanEditLocations(false);
        setCanViewProductionProjection(false);
      });
  }, [refreshDashboard]);

  useEffect(() => {
    if (viewMode === "25d") {
      setLocationEditMode(false);
      setAreaPolicyEditMode(false);
      setRackDrafts({});
      setSwapSourceLocationId(null);
    }
  }, [viewMode]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    setSelected(null);
    setRackDrafts({});
    setZonePolicyDrafts({});
    setAreaPolicyEditMode(false);
    requestJson<TwinFloorResponse>(`/api/warehouse/twin-layout/floors/${floorCode}`)
      .then((raw) => {
        if (!active) return;
        setLayout(hydrateLayout(raw));
        setAssets(raw.assets || []);
      })
      .catch((reason: Error) => active && setError(reason.message))
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [floorCode]);

  const refreshProduction = useCallback(async (layoutId: string, announce = false) => {
    const value = await requestJson<ProductionProjectionResponse>(`/api/warehouse/twin-production/layouts/${layoutId}/tasks`);
    setProductionProjection(value);
    if (announce) setProductionMessage(`已刷新：${value.items.length} 项待生产，${value.items.filter((item) => item.mapping && !item.mapping.target_missing).length} 项已定位`);
    return value;
  }, []);

  useEffect(() => {
    if (!layout || floorCode !== "1F" || !canViewProductionProjection) {
      setProductionProjection(null);
      setProductionTaskId(null);
      return;
    }
    let active = true;
    const refresh = () => refreshProduction(layout.id).catch((reason: Error) => {
      if (active) setProductionMessage(reason.message);
    });
    refresh();
    const timer = window.setInterval(refresh, 30_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [floorCode, layout?.id, refreshProduction, canViewProductionProjection]);

  useEffect(() => {
    if (!searchPanelOpen || searchType !== "finished") {
      setSearchCustomers([]);
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ keyword: searchCustomerQuery.trim(), page: "1", page_size: "50" });
      requestJson<CustomerOptionsResponse>(`/api/master/customers?${params.toString()}`)
        .then((value) => active && setSearchCustomers(value.items || []))
        .catch((reason: Error) => active && setError(reason.message));
    }, 220);
    return () => { active = false; window.clearTimeout(timer); };
  }, [searchPanelOpen, searchType, searchCustomerQuery]);

  useEffect(() => {
    const keyword = searchType === "finished" ? searchProductQuery.trim() : search.trim();
    if ((searchType === "finished" && !searchCustomerId) || (searchType !== "finished" && keyword.length < 2)) {
      setSearchResponse(null);
      setFocusedSearchItem(null);
      setFocusedSearchProductKey(null);
      setFocusedResource(null);
      setCameraFocusTarget(null);
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ search_type: searchType, keyword });
      if (searchType === "finished") params.set("customer_id", searchCustomerId);
      requestJson<SearchResponse>(`/api/warehouse/twin-operations/locate?${params.toString()}`)
        .then((value) => active && setSearchResponse(value))
        .catch((reason: Error) => active && setError(reason.message));
    }, 300);
    return () => { active = false; window.clearTimeout(timer); };
  }, [searchType, search, searchCustomerId, searchProductQuery]);

  useEffect(() => {
    if (!layout || layout.floor_code !== floorCode || !pendingAreaCode) return;
    const target = (layout.features as TwinFeature[]).find(
      (feature) => feature.feature_kind === "zone" && feature.erp_area_code === pendingAreaCode
    );
    if (target) {
      setSelected({ kind: "feature", id: target.id });
      cameraFocusSequenceRef.current += 1;
      setCameraFocusTarget({ entity: { kind: "feature", id: target.id }, token: cameraFocusSequenceRef.current, source: "search" });
    }
    setPendingAreaCode(null);
  }, [layout, pendingAreaCode]);

  const features = (layout?.features || []) as TwinFeature[];
  const currentFloor = dashboard?.floors.find((item) => item.floor_code === floorCode);
  const visualLocations = useMemo<DashboardLocation[]>(() => (dashboard?.locations || []).map((location) => {
    const draft = locationDrafts[location.location_id];
    return draft ? {
      ...location,
      position_status: "mapped",
      map_position: {
        left_pct: draft.left_pct,
        top_pct: draft.top_pct,
        width_pct: draft.width_pct,
        height_pct: draft.height_pct,
        z_index: draft.z_index,
        version: Number(location.map_position?.version ?? draft.expected_version)
      }
    } : location;
  }), [dashboard?.locations, locationDrafts]);
  useEffect(() => {
    if (!locationEditMode || floorCode !== "3F") return;
    setLocationDrafts((current) => {
      const seeded = { ...current };
      let changed = false;
      for (const location of dashboard?.locations || []) {
        const proposal = location.layout_draft_position;
        if (
          location.floor_code !== "3F"
          || location.position_status !== "unplaced"
          || !proposal
          || seeded[location.location_id]
        ) continue;
        seeded[location.location_id] = {
          location_id: location.location_id,
          expected_version: proposal.version,
          left_pct: proposal.left_pct,
          top_pct: proposal.top_pct,
          width_pct: proposal.width_pct,
          height_pct: proposal.height_pct,
          z_index: proposal.z_index
        };
        changed = true;
      }
      return changed ? seeded : current;
    });
  }, [locationEditMode, floorCode, dashboard?.locations]);
  const mappedLocationPallets = useMemo(
    () => buildMappedLocationPallets(features, visualLocations, floorCode, layout?.id),
    [features, visualLocations, floorCode, layout?.id]
  );
  const palletColumnConflicts = useMemo(
    () => layout ? findPalletColumnConflicts(mappedLocationPallets, layout.structures, features) : [],
    [mappedLocationPallets, layout?.structures, features]
  );
  const palletColumnConflictIds = useMemo(
    () => new Set(palletColumnConflicts.map((item) => item.pallet_id)),
    [palletColumnConflicts]
  );
  const visualLayout = useMemo(
    () => layout ? {
      ...layout,
      racks: layout.racks.map((rack) => rackDrafts[rack.id] || rack),
      pallets: [...layout.pallets, ...mappedLocationPallets],
      violations: [
        ...layout.violations,
        ...palletColumnConflicts.map((item) => ({
          id: `location-column-${item.pallet_id}-${item.column_id}`,
          severity: "error" as const,
          rule_code: "LOCATION_OVERLAPS_COLUMN",
          message: "货位与固定柱子重叠",
          entity_kind: "pallet",
          entity_id: item.pallet_id,
          related_kind: "feature",
          related_id: item.column_id
        }))
      ]
    } : null,
    [layout, rackDrafts, mappedLocationPallets, palletColumnConflicts]
  );
  const searchProductGroups = useMemo(
    () => groupSearchProducts(searchResponse?.items || []),
    [searchResponse?.items]
  );
  const focusedSearchProduct = useMemo(
    () => searchProductGroups.find((item) => item.key === focusedSearchProductKey) || null,
    [searchProductGroups, focusedSearchProductKey]
  );
  const searchHighlightItems = focusedSearchProduct?.items || searchResponse?.items || [];
  const highlightedAreaCodes = useMemo(
    () => searchHighlightAreaCodes(searchHighlightItems, floorCode),
    [searchHighlightItems, floorCode]
  );
  const searchHighlightFeatureIds = useMemo(() => {
    const codes = new Set(highlightedAreaCodes);
    const byArea = features
      .filter((feature) => feature.feature_kind === "zone" && feature.erp_area_code && codes.has(feature.erp_area_code))
      .map((feature) => feature.id);
    const resourceCodes = new Set(
      (searchResponse?.resources || [])
        .filter((item) => item.floor_code === floorCode)
        .flatMap((item) => item.feature_codes || [])
    );
    const byResource = features
      .filter((feature) => resourceCodes.has(feature.feature_code))
      .map((feature) => feature.id);
    return [...new Set([...byArea, ...byResource])];
  }, [features, highlightedAreaCodes, searchResponse?.resources, floorCode]);
  const searchHighlightPalletIds = useMemo(() => [...new Set(
    [
      ...searchHighlightItems
        .filter((item) => item.floor_code === floorCode && item.location_id && !["disabled", "unplaced", "unlocated"].includes(item.position_status))
        .map((item) => `erp-location-${item.location_id}`),
      ...(searchResponse?.resources || [])
        .filter((item) => item.floor_code === floorCode && item.location_id && item.map_status === "mapped")
        .map((item) => `erp-location-${item.location_id}`)
    ]
  )], [searchHighlightItems, searchResponse?.resources, floorCode]);
  const areaStats = useMemo(() => new Map(
    (dashboard?.distribution.areas || []).filter((item) => item.floor_code === floorCode).map((item) => [item.area_code, item])
  ), [dashboard, floorCode]);
  const selectOperationalEntity = useCallback((entity: SelectedEntity) => {
    setFocusedSearchItem(null);
    setFocusedSearchProductKey(null);
    setFocusedResource(null);
    setCameraFocusTarget(null);
    setAreaInventorySearch("");
    if (!entity) {
      setSelected(null);
      return;
    }
    if (entity.kind === "equipment") {
      setSelected(entity);
      return;
    }
    if (entity.kind === "rack" && layout?.racks.some((item) => item.id === entity.id)) {
      setSelected(entity);
      if (locationEditMode) {
        const current = rackDrafts[entity.id] || layout.racks.find((item) => item.id === entity.id);
        if (current) setRackDrafts((drafts) => ({ ...drafts, [entity.id]: rackDraft(current) }));
        setRackFocusId(null);
        setLocationEditMessage("已选中货架；可拖动或在右侧修改参数，保存后才写入布局。");
      } else {
        setRackFocusId(entity.id);
      }
      return;
    }
    if (entity.kind === "pallet" && entity.id.startsWith("erp-location-")) {
      setSelected(entity);
      return;
    }
    if (
      entity.kind === "feature"
      && layout?.features.some((item) => item.id === entity.id && item.feature_kind === "zone")
    ) {
      setSelected(entity);
      return;
    }
    setSelected(null);
  }, [layout, locationEditMode, rackDrafts]);
  const selectedFeature = selected?.kind === "feature"
    ? features.find((item) => item.id === selected.id && item.feature_kind === "zone")
    : undefined;
  const selectedPlacement = selected?.kind === "equipment" ? layout?.placements.find((item) => item.id === selected.id) : undefined;
  const selectedRack = selected?.kind === "rack" ? visualLayout?.racks.find((item) => item.id === selected.id) : undefined;
  const selectedLocation = selected?.kind === "pallet"
    ? visualLocations.find((item) => `erp-location-${item.location_id}` === selected.id)
    : undefined;
  const selectedLocationItems = selectedLocation
    ? (selectedLocation.pallet?.items || selectedLocation.loose_items)
    : [];
  const selectedLocationCustomers = Array.from(new Set(
    selectedLocationItems.map((item) => item.customer_name?.trim()).filter((name): name is string => Boolean(name))
  ));
  const selectedLocationCustomerLabel = selectedLocationCustomers.length === 1
    ? selectedLocationCustomers[0]
    : selectedLocationCustomers.length > 1
      ? `${selectedLocationCustomers.length} 个客户混合存放`
      : selectedLocation?.occupancy_status === "empty"
        ? "当前空库位"
        : "客户待确认";
  const selectedLocationHasColumnConflict = Boolean(
    selectedLocation && palletColumnConflictIds.has(`erp-location-${selectedLocation.location_id}`)
  );
  const selectedLocationBaseReceivable = Boolean(
    selectedLocation
    && selectedLocation.is_active
    && selectedLocation.position_status === "mapped"
    && !locationDrafts[selectedLocation.location_id]
    && !selectedLocationHasColumnConflict
  );
  const selectedLocationCanReceiveFinished = Boolean(
    selectedLocationBaseReceivable
    && selectedLocation
    && ["finished", "shared"].includes(selectedLocation.warehouse_type)
  );
  const selectedLocationCanReceiveSemiFinished = Boolean(
    selectedLocationBaseReceivable
    && selectedLocation?.floor_code === "1F"
    && ["semi_finished", "shared"].includes(selectedLocation.warehouse_type)
  );
  const selectedLocationCanReceiveProduct = inboundInventoryType === "finished"
    ? selectedLocationCanReceiveFinished
    : selectedLocationCanReceiveSemiFinished;
  const selectedLocationSupportsPallet = Boolean(
    selectedLocationCanReceiveFinished
    && floorCode === "3F"
    && selectedLocation?.storage_type !== "rack"
  );
  const emptyMoveTargets = visualLocations.filter(
    (item) => item.floor_code === "3F"
      && item.occupancy_status === "empty"
      && item.position_status === "mapped"
      && item.storage_type !== "rack"
      && ["finished", "shared"].includes(item.warehouse_type)
      && item.location_id !== selectedLocation?.location_id
  );
  const selectedInboundProduct = productCandidates.find(
    (item) => String(item.product_id) === inboundProductId
  );
  const selectedInboundCustomer = inboundCustomers.find(
    (item) => String(item.id) === inboundCustomerId
  );
  const selectedStagingProduct = stagingCandidates.find(
    (item) => String(item.lot_id) === stagingLotId
  );
  const selectedLocationAreaCode = selectedLocation?.area_code?.trim() || null;
  const selectedRackAreaCode = selectedRack ? rackAreaCode(selectedRack, features) : null;
  const selectedAreaFeature = selectedFeature || features.find(
    (item) => item.feature_kind === "zone" && featureAreaCode(item) === (selectedLocationAreaCode || selectedRackAreaCode)
  );
  const selectedAreaCode = featureAreaCode(selectedAreaFeature) || selectedLocationAreaCode || selectedRackAreaCode;
  const selectedArea = selectedAreaCode ? areaStats.get(selectedAreaCode) : undefined;
  const selectedAreaLocations = visualLocations.filter(
    (item) => item.floor_code === floorCode && item.area_code === selectedAreaCode && item.is_active
  );
  const selectedAreaLocationCount = selectedAreaLocations.length;
  const selectedAreaPendingLocationCount = selectedAreaLocations.filter((item) => item.position_status === "unplaced").length;
  const selectedAreaRacks = (visualLayout?.racks || []).filter((rack) => rackAreaCode(rack, features) === selectedAreaCode);
  const inferredAreaInventoryTypes = Array.from(new Set(selectedAreaLocations.flatMap((location): InventoryUsage[] => {
    if (location.warehouse_type === "semi_finished") return ["semi_finished"];
    if (location.warehouse_type === "shared") return ["finished", "semi_finished"];
    return ["finished"];
  })));
  const selectedZonePolicy = selectedAreaFeature ? (zonePolicyDrafts[selectedAreaFeature.id] || {
    allowed_inventory_types: selectedAreaFeature.allowed_inventory_types?.length
      ? selectedAreaFeature.allowed_inventory_types
      : inferredAreaInventoryTypes.length ? inferredAreaInventoryTypes : ["finished"],
    storage_layout: selectedAreaFeature.storage_layout
      || (selectedAreaFeature.subtype.includes("rack") ? "rack" : selectedAreaRacks.length ? "mixed" : "pallet_ground")
  }) : null;
  const selectedRackEditDraft = selectedRack
    ? (rackDrafts[selectedRack.id] || rackDraft(selectedRack))
    : null;
  const selectedInventory = useMemo(
    () => expandAreaInventory(dashboard?.locations || [], floorCode, selectedAreaCode),
    [dashboard?.locations, floorCode, selectedAreaCode]
  );
  useEffect(() => {
    setTargetAreaLocationCount(selectedAreaCode ? String(selectedAreaLocationCount) : "");
  }, [selectedAreaCode, selectedAreaLocationCount]);
  const focusedRack = rackFocusId ? layout?.racks.find((item) => item.id === rackFocusId) || null : null;
  const focusedRackAreaCode = focusedRack ? rackAreaCode(focusedRack, features) : null;
  const focusedAreaRacks = useMemo(() => {
    if (!focusedRack || !layout) return [];
    const sameArea = layout.racks
      .filter((rack) => rackAreaCode(rack, features) === focusedRackAreaCode)
      .sort((left, right) => left.x_mm - right.x_mm || left.y_mm - right.y_mm || left.rack_code.localeCompare(right.rack_code, "zh-CN", { numeric: true }));
    return sameArea.length ? sameArea : [focusedRack];
  }, [focusedRack, focusedRackAreaCode, layout?.racks, features]);
  const focusedRackIndex = Math.max(0, focusedAreaRacks.findIndex((item) => item.id === focusedRack?.id));
  const rackInventoryItems = useMemo<RackInventoryItem[]>(() => {
    if (!focusedRack || !focusedRackAreaCode || !focusedAreaRacks.length) return [];
    const rackByLocation = new Map<number, Rack>();
    for (const location of visualLocations) {
      if (location.floor_code !== floorCode || location.area_code !== focusedRackAreaCode || location.position_status !== "mapped") continue;
      const pallet = mappedLocationPallets.find((item) => item.id === `erp-location-${location.location_id}`);
      if (!pallet) continue;
      const nearest = focusedAreaRacks.reduce((best, rack) => {
        const distance = Math.hypot(pallet.x_mm - rack.x_mm, pallet.y_mm - rack.y_mm);
        return !best || distance < best.distance ? { rack, distance } : best;
      }, null as { rack: Rack; distance: number } | null);
      if (nearest) rackByLocation.set(location.location_id, nearest.rack);
    }
    return visualLocations
      .filter((location) => rackByLocation.get(location.location_id)?.id === focusedRack.id)
      .flatMap((location) => [
        ...(location.pallet?.items || []),
        ...location.loose_items
      ].map((item) => ({
        ...item,
        location_code: location.location_code,
        location_name: location.location_name,
        pallet_code: location.pallet?.pallet_code || null
      })));
  }, [focusedRack, focusedRackAreaCode, focusedAreaRacks, visualLocations, mappedLocationPallets, floorCode]);
  const focusedRackEmptyLocations = useMemo<DashboardLocation[]>(() => {
    if (!focusedRack || !focusedRackAreaCode || !focusedAreaRacks.length) return [];
    return visualLocations
      .filter((location) => location.floor_code === floorCode
        && location.area_code === focusedRackAreaCode
        && location.storage_type === "rack"
        && location.is_active
        && location.position_status === "mapped"
        && location.occupancy_status === "empty"
        && !locationDrafts[location.location_id]
        && !palletColumnConflictIds.has(`erp-location-${location.location_id}`))
      .filter((location) => {
        const pallet = mappedLocationPallets.find((item) => item.id === `erp-location-${location.location_id}`);
        if (!pallet) return false;
        const nearest = focusedAreaRacks.reduce((best, rack) => {
          const distance = Math.hypot(pallet.x_mm - rack.x_mm, pallet.y_mm - rack.y_mm);
          return !best || distance < best.distance ? { rack, distance } : best;
        }, null as { rack: Rack; distance: number } | null);
        return nearest?.rack.id === focusedRack.id;
      })
      .sort((left, right) => left.location_code.localeCompare(right.location_code, "zh-CN", { numeric: true }));
  }, [focusedRack, focusedRackAreaCode, focusedAreaRacks, visualLocations, mappedLocationPallets, floorCode, locationDrafts, palletColumnConflictIds]);
  const selectedCorrectionItem = selectedLocationItems.find((item) => item.lot_id === correctionLotId) || null;

  useEffect(() => {
    setLocationDetailOpen(false);
    const nextInventoryType: InboundInventoryType = selectedLocation?.warehouse_type === "semi_finished" ? "semi_finished" : "finished";
    setInboundInventoryType(nextInventoryType);
    setInboundMode(selectedLocation?.floor_code === "3F" && selectedLocation?.occupancy_status === "empty" ? "staging" : "catalog");
    setInboundCustomerQuery("");
    setInboundCustomers([]);
    setInboundCustomerId("");
    setProductQuery("");
    setProductCandidates([]);
    setInboundProductId("");
    setStagingQuery("");
    setStagingCandidates([]);
    setStagingLotId("");
    setCorrectionLotId(null);
    setCorrectionQuantity("");
    setCorrectionReason("");
    setWarehouseOperationMessage("");
  }, [selected?.kind, selected?.id, selectedLocation?.occupancy_status]);
  const filteredSelectedInventory = useMemo(
    () => filterAreaInventory(selectedInventory, areaInventorySearch),
    [selectedInventory, areaInventorySearch]
  );
  const selectedProductionTask = productionTaskId === null
    ? null
    : productionProjection?.items.find((item) => item.source_task_id === productionTaskId) || null;
  const visibleProductionTasks = (productionProjection?.items || []).filter((item) => {
    const keyword = productionSearch.trim().toLowerCase();
    return !keyword || `${item.order_number} ${item.customer_name} ${item.product_code} ${item.product_name}`.toLowerCase().includes(keyword);
  });

  useEffect(() => {
    if (!pendingLocateResource || pendingLocateResource.floor_code !== floorCode || !layout) return;
    if (pendingLocateResource.location_id) {
      setSelected({ kind: "pallet", id: `erp-location-${pendingLocateResource.location_id}` });
      setPendingLocateResource(null);
      return;
    }
    const feature = features.find((item) => pendingLocateResource.feature_codes.includes(item.feature_code));
    setSelected(feature ? { kind: "feature", id: feature.id } : null);
    if (feature) {
      cameraFocusSequenceRef.current += 1;
      setCameraFocusTarget({ entity: { kind: "feature", id: feature.id }, token: cameraFocusSequenceRef.current, source: "search" });
    }
    setPendingLocateResource(null);
  }, [pendingLocateResource, floorCode, layout?.id, features]);

  useEffect(() => {
    if (pendingLocationId === null) return;
    const location = visualLocations.find(
      (item) => item.location_id === pendingLocationId && item.floor_code === floorCode
    );
    if (!location) return;
    setSelected({ kind: "pallet", id: `erp-location-${pendingLocationId}` });
    setPendingLocationId(null);
  }, [pendingLocationId, floorCode, visualLocations]);

  useEffect(() => {
    if (!canEditLocations || viewMode !== "2d" || !selectedLocation || inboundMode !== "catalog") {
      setInboundCustomers([]);
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ keyword: inboundCustomerQuery.trim(), page: "1", page_size: "50" });
      requestJson<CustomerOptionsResponse>(`/api/master/customers?${params.toString()}`)
        .then((value) => active && setInboundCustomers(value.items || []))
        .catch((reason: Error) => active && setWarehouseOperationMessage(reason.message));
    }, 220);
    return () => { active = false; window.clearTimeout(timer); };
  }, [inboundCustomerQuery, canEditLocations, viewMode, selectedLocation?.location_id, inboundMode]);

  useEffect(() => {
    const keyword = productQuery.trim();
    if (!canEditLocations || viewMode !== "2d" || !selectedLocationCanReceiveProduct || inboundMode !== "catalog" || !inboundCustomerId) {
      setProductCandidates([]);
      setInboundProductId("");
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ q: keyword, customer_id: inboundCustomerId, limit: "50" });
      requestJson<ProductCandidatesResponse>(`/api/warehouse/floor3/product-candidates?${params.toString()}`)
        .then((value) => {
          if (!active) return;
          setProductCandidates(value.items || []);
          setInboundProductId((current) => current && value.items.some((item) => String(item.product_id) === current) ? current : "");
        })
        .catch((reason: Error) => active && setWarehouseOperationMessage(reason.message));
    }, 300);
    return () => { active = false; window.clearTimeout(timer); };
  }, [productQuery, inboundCustomerId, canEditLocations, viewMode, selectedLocation?.location_id, selectedLocationCanReceiveProduct, inboundMode]);

  useEffect(() => {
    if (!canEditLocations || viewMode !== "2d" || floorCode !== "3F" || !selectedLocationCanReceiveProduct || selectedLocation?.occupancy_status !== "empty" || inboundMode !== "staging") {
      setStagingCandidates([]);
      setStagingLotId("");
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ location_id: String(selectedLocation.location_id), limit: "30" });
      if (stagingQuery.trim()) params.set("q", stagingQuery.trim());
      requestJson<StagingProductCandidatesResponse>(`/api/warehouse/twin-operations/location-product-candidates?${params.toString()}`)
        .then((value) => {
          if (!active) return;
          setStagingCandidates(value.items || []);
          setStagingLotId((current) => current && value.items.some((item) => String(item.lot_id) === current) ? current : "");
        })
        .catch((reason: Error) => active && setWarehouseOperationMessage(reason.message));
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [stagingQuery, canEditLocations, viewMode, floorCode, selectedLocation?.location_id, selectedLocation?.occupancy_status, selectedLocationCanReceiveProduct, inboundMode]);

  useEffect(() => {
    if (!canEditLocations || inboundMode !== "temporary") {
      setTemporaryCustomers([]);
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ keyword: temporaryCustomerQuery.trim(), page: "1", page_size: "50" });
      requestJson<CustomerOptionsResponse>(`/api/master/customers?${params.toString()}`)
        .then((value) => {
          if (!active) return;
          setTemporaryCustomers(value.items || []);
          setTemporaryCustomerId((current) => current || (value.items[0] ? String(value.items[0].id) : ""));
        })
        .catch((reason: Error) => active && setWarehouseOperationMessage(reason.message));
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [temporaryCustomerQuery, canEditLocations, inboundMode]);

  const chooseProductionTask = (task: ProductionTaskProjection) => {
    setProductionTaskId(task.source_task_id);
    setProductionMessage("");
    if (task.mapping && !task.mapping.target_missing) {
      setProductionTargetKind(task.mapping.target_kind);
      setProductionTargetId(task.mapping.target_id);
      if (task.mapping.target_kind === "zone") {
        selectOperationalEntity({ kind: "feature", id: task.mapping.target_id });
      } else {
        setSelected(null);
      }
      return;
    }
    if (layout?.pallets.length) {
      setProductionTargetKind("pallet");
      setProductionTargetId(layout.pallets[0].id);
    } else {
      const firstZone = layout?.features.find((item) => item.feature_kind === "zone");
      setProductionTargetKind("zone");
      setProductionTargetId(firstZone?.id || "");
    }
  };

  const saveProductionMapping = async () => {
    if (!layout || !selectedProductionTask || !productionTargetId) return;
    setProductionBusy(true);
    try {
      await mutateJson(
        `/api/warehouse/twin-production/layouts/${layout.id}/tasks/${selectedProductionTask.source_task_id}`,
        "PUT",
        {
          target_kind: productionTargetKind,
          target_id: productionTargetId,
          ...(selectedProductionTask.mapping ? { version: selectedProductionTask.mapping.version } : {})
        }
      );
      const next = await refreshProduction(layout.id);
      const mapping = next.items.find((item) => item.source_task_id === selectedProductionTask.source_task_id)?.mapping;
      if (mapping?.target_kind === "zone") {
        selectOperationalEntity({ kind: "feature", id: mapping.target_id });
      } else {
        setSelected(null);
      }
      setProductionMessage(`${selectedProductionTask.order_number} 已人工定位；ERP任务、数量和状态未修改`);
    } catch (reason) {
      setProductionMessage((reason as Error).message);
    } finally {
      setProductionBusy(false);
    }
  };

  const removeProductionMapping = async () => {
    if (!layout || !selectedProductionTask?.mapping) return;
    setProductionBusy(true);
    try {
      await mutateJson(
        `/api/warehouse/twin-production/layouts/${layout.id}/tasks/${selectedProductionTask.source_task_id}?version=${selectedProductionTask.mapping.version}`,
        "DELETE"
      );
      await refreshProduction(layout.id);
      setProductionMessage(`${selectedProductionTask.order_number} 已移回待定位；ERP任务保持不变`);
    } catch (reason) {
      setProductionMessage((reason as Error).message);
    } finally {
      setProductionBusy(false);
    }
  };

  const moveLocationDraft = (palletId: string, xMm: number, yMm: number) => {
    if (!locationEditMode || floorCode !== "3F") return;
    const locationId = Number(palletId.replace("erp-location-", ""));
    const location = visualLocations.find((item) => item.location_id === locationId);
    const zone = features.find((item) => item.feature_kind === "zone" && item.erp_area_code === location?.area_code);
    if (!location || !zone || location.position_status !== "mapped") {
      setLocationEditMessage("该库位尚无已确认布局，不能用拖动伪造坐标。");
      return;
    }
    const geometry = locationLayoutGeometry(zone, location, xMm, yMm);
    if (!geometry) {
      setLocationEditMessage("库位布局版本缺失，请刷新后重试。");
      return;
    }
    setLocationDrafts((current) => ({ ...current, [locationId]: geometry }));
    setSelected({ kind: "pallet", id: palletId });
    const currentPallet = mappedLocationPallets.find((item) => item.id === palletId);
    const hitsColumn = currentPallet && layout
      ? findPalletColumnConflicts([{ ...currentPallet, x_mm: xMm, y_mm: yMm }], layout.structures, features).length > 0
      : false;
    setLocationEditMessage(hitsColumn
      ? `${location.location_code} 与柱子重叠，已标红；请拖离柱子后再保存。`
      : `${location.location_code} 已形成二维草稿；点击保存后才写入布局。`);
  };

  const saveLocationDrafts = async () => {
    const drafts = Object.values(locationDrafts);
    if (!drafts.length) return;
    const conflictingDrafts = palletColumnConflicts.filter((item) => {
      const locationId = Number(item.pallet_id.replace("erp-location-", ""));
      return Boolean(locationDrafts[locationId]);
    });
    if (conflictingDrafts.length) {
      setLocationEditMessage(`有 ${conflictingDrafts.length} 个货位仍与柱子重叠，已阻止保存；请先在二维地图中拖离柱子。`);
      return;
    }
    setLocationEditBusy(true);
    setLocationEditMessage("");
    try {
      const grouped = new Map<string, LocationLayoutGeometry[]>();
      drafts.forEach((draft) => {
        const areaCode = dashboard?.locations.find((item) => item.location_id === draft.location_id)?.area_code;
        if (areaCode) grouped.set(areaCode, [...(grouped.get(areaCode) || []), draft]);
      });
      for (const [areaCode, slots] of grouped) {
        await mutateJson(`/api/warehouse/floor3/layout/areas/${areaCode}`, "PATCH", { slots });
      }
      setLocationDrafts({});
      setSwapSourceLocationId(null);
      await refreshDashboard();
      setLocationEditMessage(`已保存 ${drafts.length} 个库位的二维布局位置。`);
    } catch (reason) {
      setLocationEditMessage((reason as Error).message);
    } finally {
      setLocationEditBusy(false);
    }
  };

  const exchangeLocationDraft = () => {
    if (!selectedLocation?.map_position) return;
    if (swapSourceLocationId === null) {
      setSwapSourceLocationId(selectedLocation.location_id);
      setLocationEditMessage(`已选择 ${selectedLocation.location_code}；再选择同一区域库位后交换平面位置。`);
      return;
    }
    const source = visualLocations.find((item) => item.location_id === swapSourceLocationId);
    if (!source?.map_position || source.area_code !== selectedLocation.area_code || source.location_id === selectedLocation.location_id) {
      setLocationEditMessage("只能选择同一区域内两个不同的已布局库位进行位置交换。");
      return;
    }
    const geometry = (location: DashboardLocation, from: DashboardLocation): LocationLayoutGeometry => ({
      location_id: location.location_id,
      expected_version: Number(location.map_position!.version),
      left_pct: Number(from.map_position!.left_pct),
      top_pct: Number(from.map_position!.top_pct),
      width_pct: Number(from.map_position!.width_pct),
      height_pct: Number(from.map_position!.height_pct),
      z_index: Number(from.map_position!.z_index || 0)
    });
    setLocationDrafts((current) => ({
      ...current,
      [source.location_id]: geometry(source, selectedLocation),
      [selectedLocation.location_id]: geometry(selectedLocation, source)
    }));
    setSwapSourceLocationId(null);
    setLocationEditMessage(`${source.location_code} 与 ${selectedLocation.location_code} 已交换二维平面位置；库存和栈板绑定未改变。`);
  };

  const applyAreaLocationCount = async () => {
    if (!selectedAreaCode) return;
    const targetCount = Number(targetAreaLocationCount);
    if (!Number.isInteger(targetCount) || targetCount < 0 || targetCount > 500) {
      setLocationEditMessage("目标库位数必须是 0 到 500 的整数。");
      return;
    }
    if (targetCount === selectedAreaLocationCount) {
      setLocationEditMessage(`${selectedAreaCode} 区当前已经是 ${targetCount} 个有效库位。`);
      return;
    }
    const direction = targetCount > selectedAreaLocationCount ? "增加" : "减少";
    if (!window.confirm(`确认把 ${selectedAreaCode} 区有效库位从 ${selectedAreaLocationCount} 个${direction}到 ${targetCount} 个吗？\n\n新增库位会自动编号并先进入待布局草稿；减少时只停用无库存、无预占、无实体栈板的空库位。`)) return;
    setLocationEditBusy(true);
    try {
      const result = await mutateJson<AreaLocationCountResponse>(`/api/warehouse/floor3/layout/areas/${selectedAreaCode}/location-count`, "POST", {
        target_count: targetCount,
        confirmed: true
      });
      await refreshDashboard();
      if (result?.items.length) {
        setLocationDrafts((current) => {
          const next = { ...current };
          for (const item of result.items) {
            if (item.action !== "created" || !item.layout) continue;
            next[item.location.id] = {
              location_id: item.location.id,
              expected_version: item.layout.version,
              left_pct: item.layout.left_pct,
              top_pct: item.layout.top_pct,
              width_pct: item.layout.width_pct,
              height_pct: item.layout.height_pct,
              z_index: item.layout.z_index
            };
          }
          return next;
        });
      }
      setLocationEditMessage(result?.message || `${selectedAreaCode} 区库位数量已更新。`);
    } catch (reason) {
      setLocationEditMessage((reason as Error).message);
    } finally {
      setLocationEditBusy(false);
    }
  };

  const disableSelectedLocation = async () => {
    if (!selectedLocation?.map_position || selectedLocation.occupancy_status !== "empty") return;
    if (!window.confirm(`确认停用空库位 ${selectedLocation.location_code} 吗？历史身份和操作记录会保留。`)) return;
    setLocationEditBusy(true);
    try {
      await mutateJson(`/api/warehouse/floor3/layout/slots/${selectedLocation.location_id}/disable`, "POST", {
        expected_version: selectedLocation.map_position.version
      });
      setSelected(null);
      await refreshDashboard();
      setLocationEditMessage(`${selectedLocation.location_code} 已逻辑停用，没有物理删除历史记录。`);
    } catch (reason) {
      setLocationEditMessage((reason as Error).message);
    } finally {
      setLocationEditBusy(false);
    }
  };

  const focusSearchItem = (item: SearchItem) => {
    setFocusedSearchItem(item);
    setFocusedSearchProductKey(searchProductKey(item));
    setFocusedResource(null);
    setAreaInventorySearch(item.inventory_code || item.lot_number || "");
    if (item.location_id) {
      cameraFocusSequenceRef.current += 1;
      setCameraFocusTarget({ entity: { kind: "pallet", id: `erp-location-${item.location_id}` }, token: cameraFocusSequenceRef.current, source: "search" });
      setPendingAreaCode(null);
      setPendingLocationId(item.location_id);
    } else if (item.area_code) {
      setCameraFocusTarget(null);
      setPendingAreaCode(item.area_code);
      setPendingLocationId(null);
    } else {
      setCameraFocusTarget(null);
      setPendingLocationId(null);
      setSelected(null);
    }
    if (item.floor_code === "1F" || item.floor_code === "3F") setFloorCode(item.floor_code);
  };

  const focusSearchProduct = (group: SearchProductGroup) => {
    const target = group.items.find((item) => item.floor_code === floorCode && item.position_status === "mapped")
      || group.items.find((item) => item.position_status === "mapped")
      || group.items[0];
    if (!target) return;
    setFocusedSearchProductKey(group.key);
    focusSearchItem(target);
  };

  const focusLocateResource = (resource: LocateResource) => {
    setFocusedSearchItem(null);
    setFocusedSearchProductKey(null);
    setFocusedResource(resource);
    if (resource.floor_code === "1F" || resource.floor_code === "3F") {
      if (resource.location_id) {
        cameraFocusSequenceRef.current += 1;
        setCameraFocusTarget({ entity: { kind: "pallet", id: `erp-location-${resource.location_id}` }, token: cameraFocusSequenceRef.current, source: "search" });
      } else {
        setCameraFocusTarget(null);
      }
      setFloorCode(resource.floor_code);
      setPendingLocateResource(resource);
    } else {
      setCameraFocusTarget(null);
      setSelected(null);
      setPendingLocateResource(null);
    }
  };

  const confirmStagingProductPlacement = async () => {
    if (!selectedLocation || !selectedStagingProduct || !inboundQuantity) return;
    const quantity = Number(inboundQuantity);
    if (!Number.isInteger(quantity) || quantity <= 0 || quantity > selectedStagingProduct.total_quantity) {
      setWarehouseOperationMessage(`转入数量必须为 1 到 ${selectedStagingProduct.total_quantity} 的整数。`);
      return;
    }
    if (!window.confirm(`管理员二次确认：从当前空货位接收这批已完工未送产品？\n\n目标货位：${selectedLocation.location_name}\n客户：${selectedStagingProduct.customer_name || "待确认"}\n产品：${selectedStagingProduct.inventory_code || selectedStagingProduct.product_name}\n转入数量：${quantity} 箱\n\n本次只移动原库存位置，不增加库存总数。`)) return;
    setWarehouseOperationBusy(true);
    setWarehouseOperationMessage("");
    try {
      await mutateJson(`/api/warehouse/twin-operations/staging-lots/${selectedStagingProduct.lot_id}/place`, "POST", {
        location_id: selectedLocation.location_id,
        expected_version: selectedStagingProduct.version,
        quantity,
        idempotency_key: stagingIdempotencyKey,
        confirmed: true
      });
      await refreshDashboard();
      setStagingLotId("");
      setInboundQuantity("");
      setStagingIdempotencyKey(operationKey("map-place-staging"));
      setWarehouseOperationMessage(`${selectedLocation.location_name} 已接收 ${quantity} 箱；原库存总数未改变。`);
    } catch (reason) {
      setWarehouseOperationMessage((reason as Error).message);
    } finally {
      setWarehouseOperationBusy(false);
    }
  };

  const confirmTemporaryProductInbound = async () => {
    if (!selectedLocation || !temporaryCustomerId || !temporaryInventoryCode.trim() || !temporaryProductName.trim() || !temporaryReason.trim() || !inboundQuantity || !inboundStockDate) return;
    const quantity = Number(inboundQuantity);
    if (!Number.isInteger(quantity) || quantity <= 0) {
      setWarehouseOperationMessage("临时产品数量必须是正整数。");
      return;
    }
    const customer = temporaryCustomers.find((item) => String(item.id) === temporaryCustomerId);
    if (!customer) {
      setWarehouseOperationMessage("请选择已确认客户。");
      return;
    }
    if (!window.confirm(`管理员二次确认：创建临时产品档案并登记当前货位实物？\n\n目标货位：${selectedLocation.location_name}\n客户：${customer.name}\n存货编码：${temporaryInventoryCode.trim()}\n产品名称：${temporaryProductName.trim()}\n数量：${quantity} 箱\n原因：${temporaryReason.trim()}\n\n该操作会创建产品档案、正式库存批次和审计记录。`)) return;
    setWarehouseOperationBusy(true);
    setWarehouseOperationMessage("");
    try {
      await mutateJson("/api/warehouse/twin-operations/temporary-finished-inbound", "POST", {
        location_id: selectedLocation.location_id,
        pallet_code: inboundPalletCode.trim() || null,
        customer_id: Number(temporaryCustomerId),
        inventory_code: temporaryInventoryCode.trim(),
        product_name: temporaryProductName.trim(),
        quantity,
        stock_date: inboundStockDate,
        reason: temporaryReason.trim(),
        idempotency_key: temporaryIdempotencyKey,
        confirmed: true
      });
      await refreshDashboard();
      setTemporaryInventoryCode("");
      setTemporaryProductName("");
      setTemporaryReason("");
      setInboundQuantity("");
      setInboundPalletCode("");
      setTemporaryIdempotencyKey(operationKey("map-temporary-inbound"));
      setWarehouseOperationMessage(`${selectedLocation.location_name} 已完成临时产品建档和入位。`);
    } catch (reason) {
      setWarehouseOperationMessage((reason as Error).message);
    } finally {
      setWarehouseOperationBusy(false);
    }
  };

  const confirmMapFinishedInbound = async () => {
    if (!selectedLocation || !selectedInboundProduct || !inboundQuantity || !inboundStockDate) return;
    const quantity = Number(inboundQuantity);
    if (!Number.isInteger(quantity) || quantity <= 0) {
      setWarehouseOperationMessage("补录数量必须是正整数。");
      return;
    }
    const correctionMode = selectedLocation.occupancy_status === "occupied";
    const inventoryTypeLabel = inboundInventoryType === "finished" ? "成品" : "半成品";
    const quantityUnit = inboundInventoryType === "finished" ? "只" : "张";
    if (!window.confirm(`管理员二次确认：把 ${selectedInboundProduct.customer_name} / ${selectedInboundProduct.product_name} 共 ${quantity} ${quantityUnit}${correctionMode ? "合并补录到已有同产品位置" : "登记到当前地图位置"}？\n\n楼层：${floorCode}\n区域：${selectedLocation.area_code || "未分区"}\n位置：${selectedLocation.location_name}\n库存类型：${inventoryTypeLabel}\n\n系统使用内部 location_id 落账，员工无需记忆库位编码。`)) return;
    setWarehouseOperationBusy(true);
    setWarehouseOperationMessage("");
    try {
      const endpoint = inboundInventoryType === "finished"
        ? "/api/warehouse/twin-operations/finished-inbound"
        : "/api/warehouse/twin-operations/semi-finished-inbound";
      await mutateJson(endpoint, "POST", {
        location_id: selectedLocation.location_id,
        ...(inboundInventoryType === "finished" ? { pallet_code: inboundPalletCode.trim() || null } : {}),
        customer_id: selectedInboundProduct.customer_id,
        product_id: selectedInboundProduct.product_id,
        quantity,
        stock_date: inboundStockDate,
        idempotency_key: inboundIdempotencyKey,
        confirmed: true,
        remarks: correctionMode ? `二维地图管理员确认${inventoryTypeLabel}差异合并补录` : `二维地图人工确认${inventoryTypeLabel}入仓`
      });
      await refreshDashboard();
      setInboundQuantity("");
      setInboundPalletCode("");
      setInboundIdempotencyKey(operationKey("map-inbound"));
      setWarehouseOperationMessage(`${selectedLocation.location_name} 已${correctionMode ? "完成同产品合并补录" : `完成${inventoryTypeLabel}入仓`}；正式库存账已保存。`);
    } catch (reason) {
      setWarehouseOperationMessage((reason as Error).message);
    } finally {
      setWarehouseOperationBusy(false);
    }
  };

  const confirmMapPalletMove = async () => {
    if (!selectedLocation?.pallet || !moveTargetLocationId) return;
    const target = emptyMoveTargets.find((item) => String(item.location_id) === moveTargetLocationId);
    if (!target) {
      setWarehouseOperationMessage("请选择当前空闲且已确认布局的目标库位。");
      return;
    }
    if (!window.confirm(`确认将实体栈板 ${selectedLocation.pallet.pallet_code} 从 ${selectedLocation.location_code} 移到 ${target.location_code} 吗？`)) return;
    setWarehouseOperationBusy(true);
    setWarehouseOperationMessage("");
    try {
      await mutateJson(`/api/warehouse/twin-operations/pallets/${selectedLocation.pallet.pallet_id}/move`, "POST", {
        expected_version: selectedLocation.pallet.version,
        to_location_id: target.location_id,
        idempotency_key: moveIdempotencyKey,
        confirmed: true,
        remarks: "二维地图人工确认正式栈板移位"
      });
      await refreshDashboard();
      setSelected({ kind: "pallet", id: `erp-location-${target.location_id}` });
      setMoveTargetLocationId("");
      setMoveIdempotencyKey(operationKey("map-move"));
      setWarehouseOperationMessage(`${selectedLocation.pallet.pallet_code} 已移到 ${target.location_code}，关联正式库存位置已同步更新。`);
    } catch (reason) {
      setWarehouseOperationMessage((reason as Error).message);
    } finally {
      setWarehouseOperationBusy(false);
    }
  };

  const correctSelectedInventoryLot = async (action: "decrease" | "remove") => {
    if (!selectedLocation || !selectedCorrectionItem?.lot_id || !selectedCorrectionItem.version) return;
    const available = Number(selectedCorrectionItem.available_quantity || 0);
    const reserved = Number(selectedCorrectionItem.reserved_quantity || 0);
    const quantity = Number(correctionQuantity);
    if (action === "decrease" && (!Number.isInteger(quantity) || quantity <= 0 || quantity > available)) {
      setWarehouseOperationMessage(`减少数量必须是 1 到 ${formatNumber(available)} 之间的整数。`);
      return;
    }
    if (action === "remove" && reserved > 0) {
      setWarehouseOperationMessage("该货物仍有预占，必须先释放预占后才能移除。");
      return;
    }
    if (correctionReason.trim().length < 2) {
      setWarehouseOperationMessage("请填写至少 2 个字的现场差异原因。");
      return;
    }
    const actionLabel = action === "remove" ? "把该货物数量归零并从当前标签移除" : `减少 ${quantity} ${inventoryUnitLabel(selectedCorrectionItem.unit)}`;
    if (!window.confirm(`管理员二次确认：${actionLabel}？\n\n客户：${selectedCorrectionItem.customer_name || "待确认"}\n产品：${selectedCorrectionItem.inventory_code || selectedCorrectionItem.product_name || selectedCorrectionItem.lot_number}\n位置：${selectedLocation.location_name}\n原因：${correctionReason.trim()}\n\n该操作会写正式库存流水，不能用删除页面记录的方式撤销。`)) return;
    setWarehouseOperationBusy(true);
    setWarehouseOperationMessage("");
    try {
      await mutateJson(`/api/warehouse/twin-operations/lots/${selectedCorrectionItem.lot_id}/quantity-correction`, "POST", {
        expected_version: selectedCorrectionItem.version,
        action,
        ...(action === "decrease" ? { quantity } : {}),
        reason: correctionReason.trim(),
        idempotency_key: correctionIdempotencyKey,
        confirmed: true
      });
      await refreshDashboard();
      setCorrectionQuantity("");
      setCorrectionReason("");
      setCorrectionLotId(null);
      setCorrectionIdempotencyKey(operationKey("map-correction"));
      setWarehouseOperationMessage(action === "remove" ? "该货物已受控归零并保留历史流水。" : "库存数量已按管理员确认减少。");
    } catch (reason) {
      setWarehouseOperationMessage((reason as Error).message);
    } finally {
      setWarehouseOperationBusy(false);
    }
  };

  const updateRackDraft = (rackId: string, patch: Partial<RackDraft>) => {
    const source = rackDrafts[rackId] || (layout?.racks.find((item) => item.id === rackId) ? rackDraft(layout.racks.find((item) => item.id === rackId)!) : null);
    if (!source) return;
    const next = { ...source, ...patch };
    setRackDrafts((current) => ({ ...current, [rackId]: next }));
  };

  const moveRackDraft = (rackId: string, xMm: number, yMm: number) => {
    if (!locationEditMode) return;
    updateRackDraft(rackId, { x_mm: Math.round(xMm), y_mm: Math.round(yMm) });
    setSelected({ kind: "rack", id: rackId });
    setLocationEditMessage("货架位置已形成草稿；右侧确认参数后点击保存货架。");
  };

  const changeRackLevels = (rack: RackDraft, levels: number) => {
    const normalized = Math.max(1, Math.min(20, Math.round(levels || 1)));
    const base = Math.floor(rack.height_mm / normalized);
    const clear = Array.from({ length: normalized }, (_, index) => index === normalized - 1 ? rack.height_mm - base * (normalized - 1) : base);
    updateRackDraft(rack.id, { levels: normalized, level_clear_heights_mm: clear, level_heights_mm: rackShelfHeights(clear) });
  };

  const changeRackTotalHeight = (rack: RackDraft, heightMm: number) => {
    const normalized = Math.max(rack.levels, Math.round(heightMm || rack.height_mm));
    const base = Math.floor(normalized / rack.levels);
    const clear = Array.from({ length: rack.levels }, (_, index) => index === rack.levels - 1 ? normalized - base * (rack.levels - 1) : base);
    updateRackDraft(rack.id, { height_mm: normalized, level_clear_heights_mm: clear, level_heights_mm: rackShelfHeights(clear) });
  };

  const changeRackLevelHeight = (rack: RackDraft, levelIndex: number, heightMm: number) => {
    const clear = [...rack.level_clear_heights_mm];
    clear[levelIndex] = Math.max(1, Math.round(heightMm || 1));
    updateRackDraft(rack.id, { height_mm: clear.reduce((sum, value) => sum + value, 0), level_clear_heights_mm: clear, level_heights_mm: rackShelfHeights(clear) });
  };

  const rackMutationPayload = (rack: RackDraft) => ({
    name: rack.name,
    x_mm: rack.x_mm,
    y_mm: rack.y_mm,
    width_mm: rack.width_mm,
    depth_mm: rack.depth_mm,
    height_mm: rack.height_mm,
    levels: rack.levels,
    level_heights_mm: rackShelfHeights(rack.level_clear_heights_mm),
    cargo_rows: rack.cargo_rows,
    bays: rack.bays,
    access_side: rack.access_side,
    min_aisle_width_mm: rack.min_aisle_width_mm,
    rotation_deg: rack.rotation_deg,
    color: rack.color
  });

  const saveSelectedRack = async () => {
    if (!layout || !selectedRack) return;
    const original = layout.racks.find((item) => item.id === selectedRack.id);
    const draft = rackDrafts[selectedRack.id] || rackDraft(selectedRack);
    if (!original) return;
    setSpatialEditBusy(true);
    try {
      const response = await mutateJson<LayoutMutationResponse<Rack>>(
        `/api/warehouse/twin-layout/floors/${floorCode}/racks/${selectedRack.id}`,
        "PATCH",
        {
          ...rackMutationPayload(draft),
          expected_revision: layout.source_sha256,
          expected_version: original.version,
          operation_key: operationKey("rack-update")
        }
      );
      if (!response) return;
      setLayout((current) => current ? {
        ...current,
        source_sha256: response.revision,
        racks: current.racks.map((item) => item.id === response.item.id ? response.item : item)
      } : current);
      setRackDrafts((current) => {
        const next = { ...current };
        delete next[selectedRack.id];
        return next;
      });
      setLocationEditMessage(`${response.item.name} 已保存；库存数量与正式库位未改变。`);
    } catch (reason) {
      setLocationEditMessage(`保存货架失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const addRackToSelectedArea = async () => {
    if (!layout || !selectedAreaFeature) return;
    const center = featureCenter(selectedAreaFeature);
    const areaCode = featureAreaCode(selectedAreaFeature) || "区域";
    setSpatialEditBusy(true);
    try {
      const response = await mutateJson<LayoutMutationResponse<Rack>>(
        `/api/warehouse/twin-layout/floors/${floorCode}/racks`,
        "POST",
        {
          expected_revision: layout.source_sha256,
          operation_key: operationKey("rack-create"),
          area_feature_id: selectedAreaFeature.id,
          name: `${areaCode} 新货架`,
          x_mm: Math.round(center.x),
          y_mm: Math.round(center.y),
          width_mm: 2800,
          depth_mm: 1100,
          height_mm: 2200,
          levels: 3,
          level_heights_mm: [733, 1466],
          cargo_rows: 4,
          bays: 1,
          access_side: "south",
          min_aisle_width_mm: 1500,
          rotation_deg: 0,
          color: "#38bdf8"
        }
      );
      if (!response) return;
      setLayout((current) => current ? { ...current, source_sha256: response.revision, racks: [...current.racks, response.item] } : current);
      setRackDrafts((current) => ({ ...current, [response.item.id]: rackDraft(response.item) }));
      setSelected({ kind: "rack", id: response.item.id });
      setLocationEditMessage(`${response.item.rack_code} 已加入当前区域；请拖到实际位置并编辑参数。`);
    } catch (reason) {
      setLocationEditMessage(`新增货架失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const deleteSelectedRack = async () => {
    if (!layout || !selectedRack) return;
    const original = layout.racks.find((item) => item.id === selectedRack.id);
    if (!original) return;
    if (!window.confirm(`确认删除 ${original.name} 并将占地释放为空地吗？\n此操作不删除库存、栈板或正式库位。`)) return;
    setSpatialEditBusy(true);
    try {
      const key = operationKey("rack-delete");
      const response = await mutateJson<LayoutMutationResponse<{ id: string; deleted: boolean }>>(
        `/api/warehouse/twin-layout/floors/${floorCode}/racks/${original.id}?expected_revision=${encodeURIComponent(layout.source_sha256)}&expected_version=${original.version}&operation_key=${encodeURIComponent(key)}`,
        "DELETE"
      );
      if (!response) return;
      setLayout((current) => current ? { ...current, source_sha256: response.revision, racks: current.racks.filter((item) => item.id !== original.id) } : current);
      setRackDrafts((current) => {
        const next = { ...current };
        delete next[original.id];
        return next;
      });
      setSelected(selectedAreaFeature ? { kind: "feature", id: selectedAreaFeature.id } : null);
      setLocationEditMessage(`${original.name} 已从视觉布局删除并释放为空地；库存与正式库位未改变。`);
    } catch (reason) {
      setLocationEditMessage(`删除货架失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const toggleAreaUsage = (usage: InventoryUsage) => {
    if (!selectedAreaFeature || !selectedZonePolicy) return;
    const current = selectedZonePolicy.allowed_inventory_types;
    const next = current.includes(usage) ? current.filter((item) => item !== usage) : [...current, usage];
    setZonePolicyDrafts((drafts) => ({ ...drafts, [selectedAreaFeature.id]: { ...selectedZonePolicy, allowed_inventory_types: next } }));
  };

  const saveSelectedZonePolicy = async () => {
    if (!layout || !selectedAreaFeature || !selectedZonePolicy) return;
    if (!selectedZonePolicy.allowed_inventory_types.length) {
      setLocationEditMessage("区域至少选择一种允许存放类型。");
      return;
    }
    setSpatialEditBusy(true);
    try {
      const response = await mutateJson<LayoutMutationResponse<TwinFeature>>(
        `/api/warehouse/twin-layout/floors/${floorCode}/zones/${selectedAreaFeature.id}/storage-policy`,
        "PATCH",
        {
          expected_revision: layout.source_sha256,
          expected_version: selectedAreaFeature.version,
          operation_key: operationKey("zone-policy"),
          ...selectedZonePolicy
        }
      );
      if (!response) return;
      setLayout((current) => current ? {
        ...current,
        source_sha256: response.revision,
        features: current.features.map((item) => item.id === response.item.id ? response.item : item)
      } : current);
      setZonePolicyDrafts((current) => {
        const next = { ...current };
        delete next[selectedAreaFeature.id];
        return next;
      });
      setLocationEditMessage(`${selectedAreaCode || selectedAreaFeature.name} 的存放策略已保存；未自动搬动或转换库存。`);
    } catch (reason) {
      setLocationEditMessage(`保存区域策略失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const toggleLayer = (key: keyof LayerVisibility) => setLayers((current) => ({ ...current, [key]: !current[key] }));
  const floorTitle = floorCode === "3F" ? "三楼实测仓库" : "一楼生产车间";
  const switchFocusedRack = (offset: number) => {
    if (!focusedAreaRacks.length) return;
    const nextIndex = (focusedRackIndex + offset + focusedAreaRacks.length) % focusedAreaRacks.length;
    setRackFocusId(focusedAreaRacks[nextIndex].id);
    setSelected({ kind: "rack", id: focusedAreaRacks[nextIndex].id });
  };
  const chooseRackEmptyLocation = (locationId: number) => {
    setRackFocusId(null);
    setViewMode("2d");
    setInboundMode("staging");
    setSelected({ kind: "pallet", id: `erp-location-${locationId}` });
    cameraFocusSequenceRef.current += 1;
    setCameraFocusTarget({ entity: { kind: "pallet", id: `erp-location-${locationId}` }, token: cameraFocusSequenceRef.current, source: "search" });
  };

  return <main className={`warehouse-twin-shell ${uiMode === "large" ? "large-text" : ""}`}>
    {focusedRack && <WarehouseRackElevation
      rack={focusedRack}
      areaCode={focusedRackAreaCode}
      area={focusedRackAreaCode ? areaStats.get(focusedRackAreaCode) : undefined}
      items={rackInventoryItems}
      emptyLocations={focusedRackEmptyLocations}
      canChooseProducts={canEditLocations}
      rackIndex={focusedRackIndex}
      rackCount={focusedAreaRacks.length || 1}
      onPrevious={() => switchFocusedRack(-1)}
      onNext={() => switchFocusedRack(1)}
      onChooseEmptyLocation={chooseRackEmptyLocation}
      onClose={() => setRackFocusId(null)}
    />}
    <header className={`twin-command-bar ${embedded ? "embedded" : ""}`}>
      <div className="twin-brand"><div><small>TIANMING WAREHOUSE</small><h1>天明智慧仓储</h1></div><nav className="twin-floor-switch" aria-label="楼层切换">
        <button type="button" className={floorCode === "1F" ? "active" : ""} onClick={() => setFloorCode("1F")}><b>1F</b><span>生产车间</span></button>
        <button type="button" className={floorCode === "3F" ? "active" : ""} onClick={() => setFloorCode("3F")}><b>3F</b><span>成品仓库</span></button>
      </nav>{selectedAreaCode && <div className="twin-header-area-summary"><small>当前区域</small><b>{selectedAreaCode} · {selectedAreaFeature?.name || "仓储区域"}</b><span>{selectedAreaFeature?.area_mm2 ? `${(selectedAreaFeature.area_mm2 / 1_000_000).toFixed(1)} m²` : "面积待确认"} · {selectedAreaLocationCount} 库位 · {selectedArea?.lot_count || 0} 批次</span></div>}<p>{floorTitle} · 正式仓库作业层</p></div>
      <div className="twin-command-status"><span className="live">{canEditLocations ? "管理员作业" : "只读定位"}</span><b>{currentFloor?.active_lots || 0}</b><small>当前层有效批次</small></div>
      <a className="twin-ledger-link" href="/warehouse-ledger.html">库存台账</a>
    </header>

    <section className="twin-toolbar">
      <button type="button" className={`twin-layer-toggle ${layerPanelOpen ? "active" : ""}`} aria-expanded={layerPanelOpen} onClick={() => setLayerPanelOpen((value) => !value)}>图层</button>
      <div className="twin-segmented" aria-label="视图模式">
        <button type="button" className={viewMode === "2d" ? "active" : ""} onClick={() => setViewMode("2d")}>二维平面</button>
        <button type="button" className={viewMode === "25d" ? "active" : ""} onClick={() => setViewMode("25d")}>2.5D 等距</button>
      </div>
      {viewMode === "25d" && <div className="twin-camera-presets">
        <button type="button" onClick={() => setCameraPreset("north_east")}>东北</button>
        <button type="button" onClick={() => setCameraPreset("north_west")}>西北</button>
        <button type="button" onClick={() => setCameraPreset("south_east")}>东南</button>
        <button type="button" onClick={() => setCameraPreset("south_west")}>西南</button>
      </div>}
      <button type="button" className="twin-reset" onClick={() => { setCameraPreset("fit"); setViewResetToken((value) => value + 1); }}>全图复位</button>
      <button type="button" className={`twin-warehouse-search-toggle ${searchPanelOpen || searchResponse ? "active" : ""}`} aria-expanded={searchPanelOpen} onClick={() => setSearchPanelOpen((value) => !value)}>全仓查找{searchResponse ? ` ${searchType === "finished" ? searchProductGroups.length : searchResponse.resource_result_count}` : ""}</button>
      {viewMode === "2d" ? <button type="button" className={`twin-location-edit-toggle ${locationEditMode ? "active" : ""}`} disabled={!canEditLocations} title={!canEditLocations ? "仅管理员可以修改库位布局" : "二维编辑：货架、区域与真实库位布局"} onClick={() => { const next = !locationEditMode; setLocationEditMode(next); setAreaPolicyEditMode(false); setLocationEditMessage(next ? "布局编辑已开启：点货架编辑结构，点区域配置存放策略。" : ""); setSwapSourceLocationId(null); if (!next) { setRackDrafts({}); setZonePolicyDrafts({}); } }}>库位布局</button> : <span className="twin-view-note">2.5D 流畅查看 · 详情见右侧</span>}
      {locationEditMode && <button type="button" className={`twin-area-policy-toggle ${areaPolicyEditMode ? "active" : ""}`} onClick={() => { setAreaPolicyEditMode((value) => !value); setLocationEditMessage("请选择一个区域，设置允许存放类型与货架/栈板地堆形式。"); }}>区域设置</button>}
      {locationEditMode && floorCode === "3F" && <><button type="button" className="twin-save-location-layout" disabled={locationEditBusy || !Object.keys(locationDrafts).length} onClick={saveLocationDrafts}>保存库位位置 {Object.keys(locationDrafts).length || ""}</button><button type="button" className="twin-cancel-location-layout" disabled={locationEditBusy || !Object.keys(locationDrafts).length} onClick={() => { setLocationDrafts({}); setSwapSourceLocationId(null); setLocationEditMessage("已取消未保存的库位位置草稿。"); }}>取消位置草稿</button></>}
      <div className="twin-toolbar-spacer" />
      <div className="twin-toolbar-summary"><span><b>{currentFloor?.active_lots || 0}</b> 有效批次</span><span><b>{currentFloor?.occupied_locations || 0}</b> 占用库位</span><span><b>{mappedLocationPallets.length}</b> 地图库位</span><span><b>{dashboard?.summary.unlocated_lots || 0}</b> 待定位</span>{palletColumnConflicts.length > 0 && <span className="column-conflict"><b>{palletColumnConflicts.length}</b> 柱子冲突</span>}</div>
    </section>

    <section className={`twin-workspace ${layerPanelOpen ? "layers-open" : "layers-collapsed"} ${searchPanelOpen ? "context-open" : "context-collapsed"} ${locationEditMode ? "location-editing" : ""}`}>
      {layerPanelOpen && <aside className="twin-layer-rail">
        <div className="twin-rail-title"><small>VIEW LAYERS</small><b>图层</b></div>
        {([
          ["zones", "区域"], ["aisles", "通道"], ["racks", "货架"], ["equipment", "设备"],
          ["structures", "原始墙柱"], ["customStructures", "补充墙柱门窗"], ["noGo", "禁放区"], ["pallets", "栈板"], ["production", "生产投影"]
        ] as Array<[keyof LayerVisibility, string]>).map(([key, label]) => <button type="button" key={key} className={layers[key] ? "active" : ""} onClick={() => toggleLayer(key)}><i /><span>{label}</span></button>)}
        <div className="twin-rail-safety"><b>数据边界</b><p>{canEditLocations ? "仅管理员确认入库或移位；查找不会改动业务数据。" : "当前账号只可查找和定位，不可执行仓库写操作。"}</p></div>
      </aside>}

      {searchPanelOpen && <aside className="twin-context-rail">
        <header><small>WAREHOUSE SEARCH</small><h2>全仓查找</h2></header>
        <section className="twin-global-search">
          <div className="twin-context-heading"><b>先选择查找类型</b>{(search || searchCustomerId || searchProductQuery) && <button type="button" onClick={() => { setSearch(""); setSearchCustomerQuery(""); setSearchCustomerId(""); setSearchProductQuery(""); setSearchResponse(null); setFocusedSearchItem(null); setFocusedSearchProductKey(null); setFocusedResource(null); setCameraFocusTarget(null); setAreaInventorySearch(""); }}>清除</button>}</div>
          <div className="twin-search-type-grid" role="tablist" aria-label="全仓查找类型">
            <button type="button" className={searchType === "finished" ? "active" : ""} onClick={() => { setSearchType("finished"); setSearch(""); setSearchResponse(null); setFocusedResource(null); setFocusedSearchProductKey(null); }}>纸箱成品</button>
            <button type="button" className={searchType === "mold" ? "active" : ""} onClick={() => { setSearchType("mold"); setSearchResponse(null); setFocusedSearchItem(null); setFocusedSearchProductKey(null); }}>模具</button>
            <button type="button" className={searchType === "printing_plate" ? "active" : ""} onClick={() => { setSearchType("printing_plate"); setSearchResponse(null); setFocusedSearchItem(null); setFocusedSearchProductKey(null); }}>印刷版</button>
          </div>
          {searchType === "finished" ? <>
            <label className="twin-search-step"><span>1　客户名称或简写</span><input value={searchCustomerQuery} onChange={(event) => { setSearchCustomerQuery(event.target.value); setSearchCustomerId(""); setSearchResponse(null); }} placeholder="例如：天华、TH 或客户全称" autoFocus /></label>
            <label className="twin-search-step"><span>选择匹配客户</span><select value={searchCustomerId} onChange={(event) => { setSearchCustomerId(event.target.value); setSearchProductQuery(""); setFocusedSearchProductKey(null); }}><option value="">请选择客户</option>{searchCustomers.map((item) => <option key={item.id} value={item.id}>{item.customer_code ? `${item.customer_code} · ` : ""}{item.name}</option>)}</select></label>
            <label className="twin-search-step"><span>2　存货编码或产品名称</span><input value={searchProductQuery} disabled={!searchCustomerId} onChange={(event) => { setSearchProductQuery(event.target.value); setFocusedSearchProductKey(null); }} placeholder={searchCustomerId ? "可输入编码/名称；留空列出该客户库存" : "请先选择客户"} /></label>
            <small>{!searchCustomerId ? "先确认客户，再从该客户真实库存中选产品。" : searchResponse ? `匹配 ${searchProductGroups.length} 个产品 · ${searchResponse.inventory_result_count} 个真实位置批次` : "正在读取该客户库存…"}</small>
          </> : <>
            <label className="twin-search-step"><span>{searchType === "mold" ? "模具编码或名称" : "印刷版编码、产品或位置"}</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={searchType === "mold" ? "输入模具编码或名称" : "输入印刷版、产品或区域"} autoFocus /></label>
            <small>{search.trim().length < 2 ? "至少输入 2 个字符" : searchResponse ? `${searchResponse.resource_result_count} 条真实定位结果` : "正在查找…"}</small>
          </>}
          {searchResponse && <div className="twin-search-result-list">
            {searchType === "finished" && searchProductGroups.slice(0, 80).map((group) => <button type="button" className={focusedSearchProductKey === group.key ? "selected product-selected" : ""} key={group.key} onClick={() => focusSearchProduct(group)}><b>{group.inventory_code}</b><strong>{group.product_name}</strong><span>{group.customer_name}</span><small><em>{formatNumber(group.total_quantity)} {inventoryUnitLabel(group.unit)}</em> · {group.location_count} 个实际位置</small></button>)}
            {searchType !== "finished" && searchResponse.resources.slice(0, 60).map((item) => <button type="button" className={focusedResource?.resource_id === item.resource_id ? "selected" : ""} key={item.resource_id} onClick={() => focusLocateResource(item)}><b>{item.primary_code || item.location_code || "功能区域"}</b><strong>{item.title}</strong><span>{item.subtitle}</span><small>{item.floor_code === "TEXT" ? "文字位置" : item.floor_code} · {item.map_status === "mapped" ? "点击定位到地图" : "仅有文字位置"}</small></button>)}
            {searchType === "finished" && !searchProductGroups.length && <div className="twin-area-empty"><b>没有匹配的成品库存</b><span>请确认客户，或更换存货编码、产品名称。</span></div>}
            {searchType !== "finished" && !searchResponse.resources.length && <div className="twin-area-empty"><b>没有匹配结果</b><span>请更换编码、名称或区域关键词。</span></div>}
          </div>}
          {focusedSearchProduct && <div className="twin-search-focus-note product-focus"><b>地图已突出显示</b><span>{focusedSearchProduct.customer_name} · {focusedSearchProduct.inventory_code} · 共 {formatNumber(focusedSearchProduct.total_quantity)} {inventoryUnitLabel(focusedSearchProduct.unit)} · {focusedSearchProduct.location_count} 个位置</span></div>}
          {focusedResource && <div className="twin-search-focus-note"><b>{focusedResource.map_status === "mapped" ? "地图定位指引" : "文字定位指引"}</b><span>{focusedResource.prompt}</span></div>}
        </section>
      </aside>}

      <div className="twin-stage">
        <div className="twin-stage-heading"><div><small>{floorCode} · MEASURED LAYOUT</small><b>{layout?.name || floorTitle}</b></div><span>{viewMode === "2d" ? "平移：按住左键拖动 · 滚轮缩放" : "平移：左键拖动 · 旋转：右键拖动 · 滚轮缩放"}</span></div>
        {loading && <div className="twin-loading">正在加载实测布局…</div>}
        {error && <div className="twin-error"><b>地图加载失败</b><span>{error}</span><button type="button" onClick={() => window.location.reload()}>重新加载</button></div>}
        {visualLayout && !loading && <EditorCanvas
          layout={visualLayout}
          assets={assets}
          viewMode={viewMode}
          cameraPreset={cameraPreset}
          viewResetToken={viewResetToken}
          selected={selected}
          layers={layers}
          palletSnapEnabled={false}
          palletSnapThresholdMm={0}
          drawMode={null}
          drawPoints={EMPTY_CANVAS_POINTS}
          measureMode={false}
          measurePoints={EMPTY_CANVAS_POINTS}
          onSelect={selectOperationalEntity}
          onMoveEquipment={noop}
          onMoveRack={moveRackDraft}
          onMovePallet={moveLocationDraft}
          onMoveFeature={noop}
          onDropAsset={noop}
          onDropRack={noop}
          onDropPallet={noop}
          onDrawPoint={noop}
          onMeasurePoint={noop}
          productionProjections={productionProjection?.items || EMPTY_PRODUCTION_PROJECTIONS}
          highlightFeatureIds={searchHighlightFeatureIds}
          highlightedPalletIds={searchHighlightPalletIds}
          focusTarget={cameraFocusTarget}
          palletEditingOnly={locationEditMode}
          rackEditingEnabled={locationEditMode}
          allowPalletSelection={viewMode === "25d" || locationEditMode || (canEditLocations && floorCode === "3F")}
          readOnly={!locationEditMode}
          visualTheme="warehouse"
        />}
      </div>

      <aside className="twin-inspector">
        <header><small>ERP INVENTORY</small><h2>库存与库位</h2></header>
        {floorCode === "1F" && canViewProductionProjection && <section className="twin-production-panel">
          <div className="twin-production-title"><div><small>ERP PRODUCTION</small><b>真实生产周转</b><span>{productionProjection?.items.length || 0} 待生产 · {productionProjection?.items.filter((item) => item.mapping && !item.mapping.target_missing).length || 0} 已定位</span></div><button type="button" aria-expanded={productionPanelOpen} onClick={() => setProductionPanelOpen((value) => !value)}>{productionPanelOpen ? "收起" : "展开"}</button></div>
          {productionPanelOpen && <div className="twin-production-details">
            <div className="twin-production-tools"><span>只读定位，不改数量和状态</span><button type="button" disabled={productionBusy || !layout} onClick={() => layout && refreshProduction(layout.id, true)}>刷新</button></div>
            {productionMessage && <div className="twin-production-message">{productionMessage}</div>}
            <input value={productionSearch} onChange={(event) => setProductionSearch(event.target.value)} placeholder="订单、客户或品号" />
            <div className="twin-production-list">
              {productionProjection && !visibleProductionTasks.length && <div className="twin-empty-note">当前没有待生产任务。</div>}
              {visibleProductionTasks.map((task) => <button type="button" key={task.source_task_id} className={productionTaskId === task.source_task_id ? "selected" : ""} onClick={() => chooseProductionTask(task)}><span><b>{task.order_number}</b><em>{task.mapping && !task.mapping.target_missing ? task.mapping.target_code : "待定位"}</em></span><strong>{task.customer_name}</strong><small>{task.product_code} · {task.product_name}</small><small>计划 {formatNumber(task.planned_quantity)} {task.production_quantity_unit === "pieces" ? "件" : "套"} · 交期 {task.delivery_date || "未填"}</small></button>)}
            </div>
            {selectedProductionTask && layout && <div className="twin-production-bind">
              <b>{selectedProductionTask.order_number} · ERP只读</b><small>{selectedProductionTask.customer_name} / {selectedProductionTask.product_name}</small>
              <label>定位对象<select value={productionTargetKind} onChange={(event) => { const kind = event.target.value as "pallet" | "zone"; setProductionTargetKind(kind); setProductionTargetId(kind === "pallet" ? layout.pallets[0]?.id || "" : layout.features.find((item) => item.feature_kind === "zone")?.id || ""); }}><option value="pallet">现有栈板</option><option value="zone">现有区域</option></select></label>
              <label>人工选择<select value={productionTargetId} onChange={(event) => setProductionTargetId(event.target.value)}>{productionTargetKind === "pallet" ? layout.pallets.map((item) => <option key={item.id} value={item.id}>{item.pallet_code} · {item.zone_code}</option>) : layout.features.filter((item) => item.feature_kind === "zone").map((item) => <option key={item.id} value={item.id}>{item.feature_code} · {item.name}</option>)}</select></label>
              <button type="button" className="twin-primary-action" disabled={productionBusy || !productionTargetId} onClick={saveProductionMapping}>{selectedProductionTask.mapping ? "更新人工定位" : "确认投影到地图"}</button>
              {selectedProductionTask.mapping && <button type="button" className="twin-production-remove" disabled={productionBusy} onClick={removeProductionMapping}>移回待定位</button>}
              <small>只保存隔离地图库的任务ID与位置关系。</small>
            </div>}
          </div>}
        </section>}
        {locationEditMessage && <div className={`twin-location-message ${locationEditMessage.includes("失败") || locationEditMessage.includes("缺失") ? "error" : ""}`}>{locationEditMessage}</div>}
        {locationEditMode && selectedRackEditDraft && <section className="twin-rack-layout-editor">
          <div className="twin-layout-editor-title"><div><small>货架编辑 · {selectedRackEditDraft.rack_code}</small><b>{selectedRackEditDraft.name}</b></div><em>草稿</em></div>
          <label><span>货架名称</span><input value={selectedRackEditDraft.name} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { name: event.target.value })} /></label>
          <div className="twin-rack-coordinate-grid">
            <label><span>X 坐标 mm</span><input type="number" value={selectedRackEditDraft.x_mm} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { x_mm: Number(event.target.value) })} /></label>
            <label><span>Y 坐标 mm</span><input type="number" value={selectedRackEditDraft.y_mm} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { y_mm: Number(event.target.value) })} /></label>
          </div>
          <div className="twin-rack-dimension-grid">
            <label><span>长度 mm</span><input type="number" min="1" value={selectedRackEditDraft.width_mm} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { width_mm: Number(event.target.value) })} /></label>
            <label><span>宽度 mm</span><input type="number" min="1" value={selectedRackEditDraft.depth_mm} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { depth_mm: Number(event.target.value) })} /></label>
            <label><span>总高度 mm</span><input type="number" min="1" value={selectedRackEditDraft.height_mm} onChange={(event) => changeRackTotalHeight(selectedRackEditDraft, Number(event.target.value))} /></label>
          </div>
          <div className="twin-rack-dimension-grid">
            <label><span>层数</span><input type="number" min="1" max="20" value={selectedRackEditDraft.levels} onChange={(event) => changeRackLevels(selectedRackEditDraft, Number(event.target.value))} /></label>
            <label><span>每层货位数</span><input type="number" min="3" max="5" value={selectedRackEditDraft.cargo_rows} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { cargo_rows: Number(event.target.value) })} /></label>
            <label><span>结构格数</span><input type="number" min="1" max="50" value={selectedRackEditDraft.bays} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { bays: Number(event.target.value) })} /></label>
          </div>
          <div className="twin-rack-level-editor"><b>每层净高（修改后自动合计总高度）</b>{selectedRackEditDraft.level_clear_heights_mm.map((height, index) => <label key={`${selectedRackEditDraft.id}-level-${index}`}><span>第 {index + 1} 层 mm</span><input type="number" min="1" value={height} onChange={(event) => changeRackLevelHeight(selectedRackEditDraft, index, Number(event.target.value))} /></label>)}</div>
          <div className="twin-rack-coordinate-grid">
            <label><span>正面操作方向</span><select value={selectedRackEditDraft.access_side} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { access_side: event.target.value as Rack["access_side"] })}><option value="north">北</option><option value="south">南</option><option value="east">东</option><option value="west">西</option><option value="both">双面</option></select></label>
            <label><span>最小通道 mm</span><input type="number" min="0" value={selectedRackEditDraft.min_aisle_width_mm} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { min_aisle_width_mm: Number(event.target.value) })} /></label>
          </div>
          <div className="twin-layout-editor-actions"><button type="button" onClick={() => updateRackDraft(selectedRackEditDraft.id, { rotation_deg: ((selectedRackEditDraft.rotation_deg + 90) % 360) as Rack["rotation_deg"] })}>旋转 90°</button><button type="button" className="primary" disabled={spatialEditBusy} onClick={saveSelectedRack}>保存货架</button><button type="button" disabled={spatialEditBusy} onClick={() => setRackDrafts((current) => { const next = { ...current }; delete next[selectedRackEditDraft.id]; return next; })}>取消草稿</button><button type="button" className="danger" disabled={spatialEditBusy || selectedRackEditDraft.is_locked} onClick={deleteSelectedRack}>删除货架</button></div>
          <p>拖动和参数修改只改变孪生布局；不会修改库存数量、栈板或正式库位身份。</p>
        </section>}
        {selectedLocation && <section className="twin-location-card">
          <div className="twin-location-card-title"><div><small>当前栈板货物</small><b>{selectedLocationCustomerLabel}</b></div><em className={selectedLocation.occupancy_status}>{selectedLocation.occupancy_status === "occupied" ? "有货" : "空位"}</em></div>
          {selectedLocationItems.length === 0 && <p className="twin-location-empty-primary">该位置当前没有货物</p>}
          {selectedLocationItems.slice(0, 8).map((item, itemIndex) => <button type="button" className={`twin-location-item ${correctionLotId === item.lot_id ? "correction-selected" : ""} ${focusedSearchProductKey && searchProductKey(item) === focusedSearchProductKey ? "warehouse-search-hit" : ""}`} key={item.lot_id || `${item.inventory_code}-${itemIndex}`} onClick={() => { setCorrectionLotId(item.lot_id || null); setCorrectionQuantity(""); setCorrectionReason(""); setWarehouseOperationMessage(""); }}>
            <div className="twin-location-item-code"><span>存货编码</span><b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b></div>
            <h4>{item.product_name || "产品名称待补充"}</h4>
            <div className="twin-location-item-summary"><span><small>客户</small>{item.customer_name || "客户待确认"}</span><strong><small>产品数量</small>{formatNumber(inventoryLabelQuantity(item))} {inventoryUnitLabel(item.unit)}</strong></div>
          </button>)}
          {selectedLocationItems.length > 8 && <p className="twin-location-more">另有 {selectedLocationItems.length - 8} 条货物记录</p>}
          {palletColumnConflictIds.has(`erp-location-${selectedLocation.location_id}`) && <p className="twin-location-column-warning">该货位与固定柱子重叠，请在二维“库位布局”中拖离柱子后再保存。</p>}
          <button type="button" className="twin-detail-toggle" aria-expanded={locationDetailOpen} onClick={() => setLocationDetailOpen((current) => !current)}>{locationDetailOpen ? "收起详细信息" : "详细信息"}</button>
          {locationDetailOpen && <div className="twin-location-secondary">
            <h3>{selectedLocation.location_name}</h3>
            <dl>
              <div><dt>区域与库位</dt><dd>{selectedLocation.area_code || "未分区"} · {selectedLocation.location_code}</dd></div>
              <div><dt>地图状态</dt><dd>{locationDrafts[selectedLocation.location_id] ? "未保存草稿" : selectedLocation.position_status === "mapped" ? "已确认布局" : "待布局"}</dd></div>
              <div><dt>实体栈板</dt><dd>{selectedLocation.pallet?.pallet_code || "当前无栈板"}</dd></div>
              <div><dt>库存明细</dt><dd>{selectedLocation.pallet?.item_count || selectedLocation.loose_items.length || 0} 条</dd></div>
            </dl>
          </div>}
          {viewMode === "25d" && <p className="twin-location-readonly-note">2.5D 仅查看库位与货物标签；调整请切换二维平面。</p>}
          {warehouseOperationMessage && <div className="twin-location-message">{warehouseOperationMessage}</div>}
          {canEditLocations && viewMode === "2d" && !locationEditMode && (selectedLocationCanReceiveFinished || selectedLocationCanReceiveSemiFinished) && <section className="twin-formal-operation">
            <div className="twin-formal-operation-title"><b>地图选点入仓 / 差异补录</b><span>{floorCode} · {selectedLocation.area_code} · {selectedLocation.location_name} · 仅 admin</span></div>
            <div className="twin-map-inbound-type" role="tablist" aria-label="入仓库存类型">
              <button type="button" className={inboundInventoryType === "finished" ? "active" : ""} disabled={!selectedLocationCanReceiveFinished} onClick={() => { setInboundInventoryType("finished"); setInboundMode(selectedLocation.occupancy_status === "empty" && floorCode === "3F" ? "staging" : "catalog"); setInboundCustomerId(""); setInboundProductId(""); setWarehouseOperationMessage(""); }}>成品</button>
              <button type="button" className={inboundInventoryType === "semi_finished" ? "active" : ""} disabled={!selectedLocationCanReceiveSemiFinished} onClick={() => { setInboundInventoryType("semi_finished"); setInboundMode("catalog"); setInboundCustomerId(""); setInboundProductId(""); setWarehouseOperationMessage(""); }}>半成品</button>
            </div>
            <p className="twin-map-pick-hint">先在顶部选择 1F/3F，再直接点地图上的真实位置。页面以区域和现场位置为主，内部库位编码只在详情中保留。</p>
            <div className="twin-inbound-mode" role="tablist" aria-label="货位选货来源">
              {inboundInventoryType === "finished" && floorCode === "3F" && selectedLocation.occupancy_status === "empty" && <button type="button" className={inboundMode === "staging" ? "active" : ""} onClick={() => { setInboundMode("staging"); setInboundQuantity(""); setWarehouseOperationMessage(""); }}>已完工未送</button>}
              <button type="button" className={inboundMode === "catalog" ? "active" : ""} onClick={() => { setInboundMode("catalog"); setInboundQuantity(""); setWarehouseOperationMessage(""); }}>ERP 已有产品</button>
              {inboundInventoryType === "finished" && <button type="button" className={inboundMode === "temporary" ? "active" : ""} onClick={() => { setInboundMode("temporary"); setInboundQuantity(""); setWarehouseOperationMessage(""); }}>临时新产品</button>}
            </div>
            {inboundInventoryType === "finished" && inboundMode === "staging" && floorCode === "3F" && selectedLocation.occupancy_status === "empty" && <>
              <label><span>查找待送产品</span><input value={stagingQuery} onChange={(event) => setStagingQuery(event.target.value)} placeholder="客户、存货编码、产品名称或批次" /></label>
              <div className="twin-staging-candidates">
                {stagingCandidates.slice(0, 12).map((item) => <button type="button" className={stagingLotId === String(item.lot_id) ? "selected" : ""} key={item.lot_id} onClick={() => { setStagingLotId(String(item.lot_id)); setInboundQuantity(String(item.total_quantity)); }}><b>{item.inventory_code || item.lot_number}</b><strong>{item.product_name || "产品名称待补充"}</strong><span>{item.customer_name || "客户待确认"} · {formatNumber(item.total_quantity)} {inventoryUnitLabel(item.unit)}</span></button>)}
                {stagingCandidates.length === 0 && <p>当前没有匹配的已完工未送产品</p>}
              </div>
              {selectedStagingProduct && <small className="twin-formal-selected">从一楼待送区转入 · 可选 {formatNumber(selectedStagingProduct.total_quantity)} {inventoryUnitLabel(selectedStagingProduct.unit)}</small>}
              <label><span>本次转入数量（箱）</span><input type="number" min="1" max={selectedStagingProduct?.total_quantity} step="1" value={inboundQuantity} onChange={(event) => setInboundQuantity(event.target.value)} /></label>
              <button type="button" className="twin-primary-action" disabled={warehouseOperationBusy || !selectedStagingProduct?.version || !inboundQuantity} onClick={confirmStagingProductPlacement}>二次确认并转入当前货位</button>
            </>}
            {inboundMode === "catalog" && <>
              <label><span>1　客户名称或简写</span><input value={inboundCustomerQuery} onChange={(event) => { setInboundCustomerQuery(event.target.value); setInboundCustomerId(""); setInboundProductId(""); }} placeholder="输入客户全称、简称或客户编码" /></label>
              <label><span>确认客户</span><select value={inboundCustomerId} onChange={(event) => { setInboundCustomerId(event.target.value); setProductQuery(""); setInboundProductId(""); }}><option value="">请选择客户</option>{inboundCustomers.map((item) => <option key={item.id} value={item.id}>{item.customer_code ? `${item.customer_code} · ` : ""}{item.name}</option>)}</select></label>
              <label><span>2　筛选该客户常用箱</span><input value={productQuery} disabled={!inboundCustomerId} onChange={(event) => { setProductQuery(event.target.value); setInboundProductId(""); }} placeholder={inboundCustomerId ? "输入存货编码或产品名称；留空显示常用箱" : "请先确认客户"} /></label>
              <label><span>确认常用箱</span><select value={inboundProductId} disabled={!inboundCustomerId} onChange={(event) => setInboundProductId(event.target.value)}><option value="">请选择已确认产品</option>{productCandidates.map((item) => <option key={item.product_id} value={item.product_id}>{item.product_code || item.customer_material_code || item.product_id} · {item.product_name}</option>)}</select></label>
              {selectedInboundProduct && <small className="twin-formal-selected">{selectedInboundProduct.customer_name} / {selectedInboundProduct.product_code || selectedInboundProduct.customer_material_code || "编码待补充"} / {selectedInboundProduct.product_name} / {selectedInboundProduct.specification || "规格待补充"}</small>}
              <div className="twin-formal-operation-grid"><label><span>{selectedLocation.occupancy_status === "empty" ? "现场实物" : "合并补录"}数量（{inboundInventoryType === "finished" ? "只" : "张"}）</span><input type="number" min="1" step="1" value={inboundQuantity} onChange={(event) => setInboundQuantity(event.target.value)} /></label><label><span>库存日期</span><input type="date" value={inboundStockDate} onChange={(event) => setInboundStockDate(event.target.value)} /></label></div>
              {inboundInventoryType === "finished" && selectedLocation.occupancy_status === "empty" && selectedLocation.storage_type !== "rack" && <label><span>实体栈板编号（可留空自动生成）</span><input value={inboundPalletCode} onChange={(event) => setInboundPalletCode(event.target.value)} placeholder="例如 PAL-3F-001" /></label>}
              {selectedLocation.occupancy_status === "occupied" && <small className="twin-merge-rule">已有货物不代表禁止入仓：同客户、同存货产品且类型兼容时可合并；其他情况系统会阻止。</small>}
              <button type="button" className="twin-primary-action" disabled={warehouseOperationBusy || !selectedInboundCustomer || !selectedInboundProduct || !inboundQuantity || !inboundStockDate || !selectedLocationCanReceiveProduct} onClick={confirmMapFinishedInbound}>{selectedLocation.occupancy_status === "empty" ? `二次确认并登记${inboundInventoryType === "finished" ? "成品" : "半成品"}` : "二次确认并合并补录"}</button>
            </>}
            {inboundInventoryType === "finished" && inboundMode === "temporary" && <div className="twin-temporary-product-form">
              <b>仅用于现场已有实物、ERP 尚无产品档案</b>
              <label><span>查找客户</span><input value={temporaryCustomerQuery} onChange={(event) => { setTemporaryCustomerQuery(event.target.value); setTemporaryCustomerId(""); }} placeholder="输入客户名称或编码" /></label>
              <label><span>确认客户</span><select value={temporaryCustomerId} onChange={(event) => setTemporaryCustomerId(event.target.value)}><option value="">请选择客户</option>{temporaryCustomers.map((item) => <option key={item.id} value={item.id}>{item.name}{item.customer_code ? ` · ${item.customer_code}` : ""}</option>)}</select></label>
              <label><span>存货编码</span><input value={temporaryInventoryCode} onChange={(event) => setTemporaryInventoryCode(event.target.value)} placeholder="现场标签上的存货编码" /></label>
              <label><span>产品名称</span><input value={temporaryProductName} onChange={(event) => setTemporaryProductName(event.target.value)} placeholder="完整产品名称" /></label>
              <div className="twin-formal-operation-grid"><label><span>现场数量（箱）</span><input type="number" min="1" step="1" value={inboundQuantity} onChange={(event) => setInboundQuantity(event.target.value)} /></label><label><span>盘点日期</span><input type="date" value={inboundStockDate} onChange={(event) => setInboundStockDate(event.target.value)} /></label></div>
              <label><span>临时建档原因（必填）</span><textarea value={temporaryReason} onChange={(event) => setTemporaryReason(event.target.value)} placeholder="例如：首次盘点发现现场实物，历史系统没有该产品" /></label>
              <button type="button" className="twin-primary-action" disabled={warehouseOperationBusy || !temporaryCustomerId || !temporaryInventoryCode.trim() || !temporaryProductName.trim() || temporaryReason.trim().length < 2 || !inboundQuantity || !inboundStockDate} onClick={confirmTemporaryProductInbound}>二次确认：建档并放入当前货位</button>
            </div>}
            {selectedLocation.occupancy_status === "occupied" && <div className="twin-formal-divider"><span>或调整现有货物</span></div>}
            {selectedLocation.occupancy_status === "occupied" && selectedCorrectionItem && selectedCorrectionItem.version ? <div className="twin-correction-form">
              <b>{selectedCorrectionItem.inventory_code || selectedCorrectionItem.lot_number} · 可用 {formatNumber(selectedCorrectionItem.available_quantity)} {inventoryUnitLabel(selectedCorrectionItem.unit)}</b>
              <label><span>减少数量</span><input type="number" min="1" max={selectedCorrectionItem.available_quantity || undefined} step="1" value={correctionQuantity} onChange={(event) => setCorrectionQuantity(event.target.value)} placeholder="输入本次减少数量" /></label>
              <label><span>现场差异原因（必填）</span><textarea value={correctionReason} onChange={(event) => setCorrectionReason(event.target.value)} placeholder="例如：现场盘点少 20 只、历史数据未同步" /></label>
              <div><button type="button" disabled={warehouseOperationBusy || !correctionQuantity || correctionReason.trim().length < 2} onClick={() => correctSelectedInventoryLot("decrease")}>二次确认后减少</button><button type="button" className="danger" disabled={warehouseOperationBusy || Number(selectedCorrectionItem.reserved_quantity || 0) > 0 || correctionReason.trim().length < 2} onClick={() => correctSelectedInventoryLot("remove")}>移除该货物</button></div>
              <small>移除不会物理删除批次或流水；有预占时必须先释放预占。</small>
            </div> : selectedLocation.occupancy_status === "occupied" && <p className="twin-correction-hint">先点击上方某个产品标签，再进行减量或移除。</p>}
            {selectedLocation.occupancy_status === "occupied" && selectedLocationSupportsPallet && <div className="twin-formal-divider"><span>实体栈板移位</span></div>}
            {selectedLocation.occupancy_status === "occupied" && selectedLocationSupportsPallet && <>
              <label><span>目标空库位</span><select value={moveTargetLocationId} onChange={(event) => setMoveTargetLocationId(event.target.value)}><option value="">请选择空库位</option>{emptyMoveTargets.map((item) => <option key={item.location_id} value={item.location_id}>{item.area_code} · {item.location_code} · {item.location_name}</option>)}</select></label>
              <small className="twin-formal-selected">本次会同步移动实体栈板及其关联正式库存位置，不改变库存数量。</small>
              <button type="button" className="twin-primary-action" disabled={warehouseOperationBusy || !moveTargetLocationId || !selectedLocation.pallet} onClick={confirmMapPalletMove}>确认正式栈板移位</button>
            </>}
          </section>}
          {canEditLocations && viewMode === "2d" && !locationEditMode && !selectedLocationCanReceiveFinished && !selectedLocationCanReceiveSemiFinished && <p className="twin-location-readonly-note">该位置尚未启用、未完成布局、库存类型不匹配或与柱子冲突，暂不能办理入仓；请直接在地图上改选兼容位置。</p>}
          {locationEditMode && canEditLocations && <div className="twin-location-edit-actions">
            <button type="button" disabled={!selectedLocation.map_position || locationEditBusy} onClick={exchangeLocationDraft}>{swapSourceLocationId === null ? "设为交换起点" : swapSourceLocationId === selectedLocation.location_id ? "已选交换起点" : `与 ${visualLocations.find((item) => item.location_id === swapSourceLocationId)?.location_code || "起点"} 交换位置`}</button>
            <button type="button" className="danger" disabled={selectedLocation.occupancy_status !== "empty" || !selectedLocation.map_position || locationEditBusy} onClick={disableSelectedLocation}>停用空库位</button>
            <small>拖动、交换只改变地图位置，不改变库存数量、实体栈板或库位绑定。</small>
          </div>}
        </section>}
        {(selectedFeature || (locationEditMode && selectedRack)) && <section className="twin-inventory-card">
          {selectedAreaFeature ? <>
            <div className="twin-inventory-title"><div><small>AREA · {selectedAreaCode || selectedAreaFeature.feature_code}</small><b>{selectedArea?.lot_count || 0} 个有效批次</b></div><span>数据截至 {formatTime(dashboard?.generated_at)}</span></div>
            <label className="twin-area-filter twin-area-filter-prominent">
              <span>当前区域库存筛选</span>
              <input value={areaInventorySearch} onChange={(event) => setAreaInventorySearch(event.target.value)} placeholder="存货编码、产品、客户、位置" />
              <small>显示 {filteredSelectedInventory.length} / {selectedInventory.length} 条</small>
            </label>
            {focusedSearchProduct && focusedSearchProduct.items.some((item) => item.area_code === selectedAreaCode) && <div className="twin-search-focus-note product-focus"><b>已找到该产品</b><span>{focusedSearchProduct.inventory_code} · 本区域位置已高亮</span></div>}
            {locationEditMode && canEditLocations && <div className="twin-area-layout-summary"><div><b>区域布局</b><small>{selectedAreaRacks.length} 个货架 · {selectedAreaLocationCount} 个正式库位</small></div><button type="button" disabled={spatialEditBusy || !selectedAreaFeature} onClick={addRackToSelectedArea}>＋ 添加货架</button></div>}
            {locationEditMode && areaPolicyEditMode && canEditLocations && selectedAreaFeature && selectedZonePolicy && <div className="twin-zone-policy-editor">
              <div><b>区域允许存放类型</b><small>可多选；只保存区域策略，不自动转换现有库存</small></div>
              <div className="twin-zone-policy-options">{([[
                "finished", "成品"
              ], ["semi_finished", "半成品"], ["raw_material", "原材料"], ["mold", "模具"], ["print_plate", "印刷版"], ["temporary_turnover", "临时周转"]] as Array<[InventoryUsage, string]>).map(([value, label]) => <label key={value}><input type="checkbox" checked={selectedZonePolicy.allowed_inventory_types.includes(value)} onChange={() => toggleAreaUsage(value)} /><span>{label}</span></label>)}</div>
              <label><span>空间存储形式</span><select value={selectedZonePolicy.storage_layout} onChange={(event) => setZonePolicyDrafts((current) => ({ ...current, [selectedAreaFeature.id]: { ...selectedZonePolicy, storage_layout: event.target.value as StorageLayout } }))}><option value="rack">货架区</option><option value="pallet_ground">栈板地堆区</option><option value="mixed">货架＋栈板混合区</option></select></label>
              <button type="button" className="primary" disabled={spatialEditBusy || !selectedZonePolicy.allowed_inventory_types.length} onClick={saveSelectedZonePolicy}>保存区域策略</button>
            </div>}
            {locationEditMode && floorCode === "3F" && canEditLocations && <div className="twin-location-create">
              <div><b>区域库位数量</b><small>当前 {selectedAreaLocationCount} 个{selectedAreaPendingLocationCount ? ` · ${selectedAreaPendingLocationCount} 个待布局` : ""}</small></div>
              <label><span>目标库位数</span><input type="number" min="0" max="500" step="1" value={targetAreaLocationCount} onChange={(event) => setTargetAreaLocationCount(event.target.value)} /></label>
              <button type="button" disabled={locationEditBusy || targetAreaLocationCount === "" || Number(targetAreaLocationCount) === selectedAreaLocationCount} onClick={applyAreaLocationCount}>确认调整</button>
              <p>系统按区域自动生成内部唯一编码和员工可读名称；减少时只逻辑停用空库位，不删除历史身份。</p>
            </div>}
            <div className="twin-inventory-quantities">{selectedArea?.quantities.map((item) => <div className="twin-quantity-row" key={item.key}><span>{item.label}</span><b>{formatNumber(item.available)} {inventoryUnitLabel(item.unit)}</b></div>)}</div>
            <div className="twin-area-lot-list">
              {!selectedInventory.length && <div className="twin-area-empty"><b>当前区域没有有效库存</b><span>这是 ERP 当前真实空态，不生成模拟货物。</span></div>}
              {selectedInventory.length > 0 && !filteredSelectedInventory.length && <div className="twin-area-empty"><b>本区域没有匹配结果</b><span>请更换存货编码、产品、客户或位置关键词。</span></div>}
              {filteredSelectedInventory.map((item) => <article className={`twin-area-lot ${focusedSearchProductKey && searchProductKey(item) === focusedSearchProductKey ? "search-hit product-search-hit" : focusedSearchItem?.lot_id === item.lot_id ? "search-hit" : ""}`} key={item.lot_id}>
                <div className="twin-location-line"><b>{item.location_code || "未编位置"}</b><span>{item.location_name || "位置待补充"}</span><em>{item.pallet_code || "地堆/散存"}</em></div>
                <div><b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b><em className={inventoryAgeTone(item.age_days)}>{inventoryAgeLabel(item.age_days)}</em></div>
                <strong>{item.product_name || "待补充库存名称"}</strong>
                <span>{item.customer_name || "客户待确认"}</span>
                <div className="twin-area-lot-meta"><span>{item.lot_number || "批次待补充"}</span><b>可用 {formatNumber(item.available_quantity ?? item.quantity)} {inventoryUnitLabel(item.unit)}</b></div>
                {(item.reserved_quantity || 0) > 0 && <small>已预占 {formatNumber(item.reserved_quantity)} {inventoryUnitLabel(item.unit)}</small>}
              </article>)}
            </div>
          </> : <div className="twin-unmapped">该区域尚未建立空间策略。</div>}
        </section>}
        {selectedPlacement && <section className="twin-object-card twin-equipment-card"><span className="twin-object-kind">生产设备</span><h3>{selectedPlacement.name}</h3><dl><div><dt>长 × 宽</dt><dd>{formatNumber(selectedPlacement.width_mm)} × {formatNumber(selectedPlacement.depth_mm)} mm</dd></div><div><dt>高度</dt><dd>{formatNumber(selectedPlacement.height_mm)} mm</dd></div><div><dt>坐标</dt><dd>X {formatNumber(selectedPlacement.x_mm)} / Y {formatNumber(selectedPlacement.y_mm)}</dd></div></dl></section>}
      </aside>
    </section>
  </main>;
}
