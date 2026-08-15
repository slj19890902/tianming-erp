import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EditorCanvas, type CanvasFocusTarget } from "./EditorCanvas";
import { filterOperationalFeatures } from "./operationalView.mjs";
import {
  clearFormalAreaOptions,
  formalAreaOptionsEffectEnabled,
  stableTwinFeatures
} from "./formalAreaOptions.mjs";
import {
  floor1CandidateBlockerDetail,
  floor1CandidateBlockerHref
} from "./floor1CandidateBlockers.mjs";
import type { Floor1CandidateBlockingItem } from "./floor1CandidateBlockers.mjs";
import { pointsBoundsMm, resizeAndMovePointsMm, translatePointsMm } from "./layoutGeometry.mjs";
import {
  buildMeasuredDispatchPallets,
  buildMappedLocationPallets,
  expandAreaInventory,
  filterAreaInventory,
  findPalletColumnConflicts,
  inventoryAgeLabel,
  inventoryAgeTone,
  inventoryLocationItems,
  inventoryLocationPallets,
  inventoryUnitLabel,
  locationLayoutGeometry,
  searchHighlightAreaCodes,
  warehouseSearchFloorSummaries,
  warehouseSearchLocationSummaries,
  warehouseSearchProductKey,
  singleLocationPallet
} from "./warehouseInventory.mjs";
import {
  buildMoveBatchPayload,
  intersectMappedMoveTargets,
  resolveMoveDropTarget,
  upsertMoveDraft
} from "./warehouseMoveDraft.mjs";
import type { MoveCandidate, WarehouseMoveDraft } from "./warehouseMoveDraft.mjs";
import {
  buildPalletMergeBatchPayload,
  normalizePalletMergeCandidate,
  palletMergeCompatibility,
  palletMergeTargetChoices,
  togglePalletMergeSource
} from "./warehousePalletMergeDraft.mjs";
import type { PalletMergeCandidate } from "./warehousePalletMergeDraft.mjs";
import {
  buildMoldRackView,
  buildMoldShelfSpines,
  moldRacksForArea
} from "./moldRackView.mjs";
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

type TwinFeature = LayoutFeature & {
  formal_area_id?: number | null;
  formal_floor_id?: number | null;
  erp_area_code?: string | null;
  formal_area_name?: string | null;
  formal_binding_status?: string | null;
  formal_policy_status?: "draft" | "published" | null;
  formal_construction_status?: string | null;
  planned_pallet_capacity?: number | null;
  capacity_review_status?: string | null;
  capacity_eligible?: boolean | null;
  confirmed_pallet_capacity?: number | null;
};
type InventoryUsage = "finished" | "semi_finished" | "raw_material" | "mold" | "print_plate" | "temporary_turnover";
type StorageLayout = "rack" | "pallet_ground" | "mixed";
type WarehouseSearchType = "finished" | "mold" | "printing_plate";
type WarehouseMapMode = "lookup" | "move" | "planning";
type InboundInventoryType = "finished" | "semi_finished" | "raw_material";
type RackDraft = Rack & { level_clear_heights_mm: number[]; level_cell_counts: number[] };
const P1_47D_ENABLED = false;
const P1_49C_ENABLED = true;

interface LayoutMutationResponse<T> {
  item: T;
  revision: string;
  applied: boolean;
  formal_area?: FormalWarehouseAreaOption | null;
}

interface FormalWarehouseAreaOption {
  id: number;
  floor_id: number;
  floor_code: string;
  floor_number: number;
  area_code: string;
  area_name: string;
  planned_pallet_capacity: number;
  capacity_review_status: string;
  confirmed_pallet_capacity?: number | null;
  construction_status: string;
  recorded_location_count: number;
  storage_policy?: { map_feature_id: string; status: string } | null;
}

interface WarehouseSpaceResponse {
  items: Array<{
    id: number;
    floor_code: string;
    floor_number: number;
    areas: FormalWarehouseAreaOption[];
  }>;
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
  product_id?: number | null;
  inventory_type?: "finished" | "semi_finished";
  customer_id?: number | null;
  lot_number?: string;
  inventory_code?: string;
  product_name?: string;
  customer_name?: string;
  quantity?: number;
  available_quantity?: number;
  reserved_quantity?: number;
  damaged_quantity?: number;
  unit?: string;
  status?: "active" | "frozen" | string;
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

interface DashboardPallet {
  pallet_id: number;
  pallet_code: string;
  version: number;
  item_count: number;
  items: InventoryItem[];
}

interface DashboardLocation {
  location_id: number;
  location_code: string;
  location_name: string;
  floor_code: string;
  area_code: string | null;
  warehouse_type: string;
  allowed_inventory_types?: InventoryUsage[];
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
    source_type?: "seeded" | "manual";
    layout_kind?: "unknown" | "physical_pallet" | "logical_anchor";
  } | null;
  layout_draft_position?: {
    left_pct: number;
    top_pct: number;
    width_pct: number;
    height_pct: number;
    version: number;
    z_index: number;
    source_type?: "seeded" | "manual";
    layout_kind?: "unknown" | "physical_pallet" | "logical_anchor";
  } | null;
  pallet: DashboardPallet | null;
  pallets?: DashboardPallet[];
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
  specification: string | null;
  total_quantity: number;
  unit: string;
  location_count: number;
  floor_summaries: Array<{ floor_code: string; quantity: number; location_count: number }>;
  location_summaries: Array<{ key: string; floor_code: string; area_code: string | null; location_id: number | null; location_name: string; position_status: string; quantity: number }>;
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

interface MoldAreaItem {
  id: number;
  mold_code: string;
  mold_name: string;
  rack_location: string;
  product_count: number;
  location_guide?: {
    kind?: string | null;
    prompt?: string | null;
    floor?: string | null;
    rack?: number | null;
    level?: number | null;
    grid?: number | null;
    row?: number | null;
  } | null;
  products: Array<{
    id: number;
    customer_name?: string | null;
    product_code?: string | null;
    product_name?: string | null;
  }>;
}

interface MoldRackResponse {
  floor_code: string;
  rack: {
    rack_id: string;
    rack_code: string;
    mold_rack_code: string;
    name: string;
    area_code: string;
    levels: number;
    level_cell_counts: number[];
    blocked_levels: number[];
    uses_legacy_bays: boolean;
  };
  items: MoldAreaItem[];
  total: number;
  truncated: boolean;
}

interface MoldAreaResponse {
  floor_code: string;
  feature_code: string;
  area_name: string;
  rack_codes: string[];
  items: MoldAreaItem[];
  total: number;
  page: number;
  page_size: number;
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

interface LocationCandidatesResponse {
  items: MoveCandidate[];
}

interface WarehouseMoveSource {
  source_key: string;
  operation: "pallet_move" | "lot_transfer";
  pallet_id?: number;
  lot_id?: number;
  expected_version: number;
  quantity?: number;
  max_quantity?: number;
  source_location_id: number;
  source_floor_code: string;
  source_area_code: string | null;
  source_location_code: string;
  source_location_name: string;
  inventory_code: string;
  product_name: string;
  customer_name: string;
  unit: string;
}

interface AreaLocationCountResponse {
  area_code: string;
  target_count: number;
  active_count: number;
  created_count: number;
  enabled_count: number;
  disabled_count: number;
  policy_version?: number | null;
  published_map_revision?: string | null;
  message: string;
  items: Array<{
    action: "created" | "enabled" | "disabled";
    location: { id: number; location_code: string; location_name: string };
    layout: DashboardLocation["map_position"];
  }>;
}

interface AreaLocationManagement {
  floor_code: string;
  area_code: string;
  management_mode: "floor3_v11" | "formal_area";
  source_version: "V11" | "TWIN_V1";
  available_actions: Array<"location_count" | "layout" | "auto_arrange" | "disable_empty" | "enable_empty">;
  policy_version?: number | null;
  published_map_revision?: string | null;
}

interface Floor1FormalCandidate {
  map_feature_id: string;
  feature_code: string;
  area_code: string;
  area_name: string;
  usage: InventoryUsage;
  storage_layout: StorageLayout;
  measured_pallet_slots: number;
  planned_pallet_capacity: number;
  formal_location_count: number;
  long_term_capacity_eligible: boolean;
  is_outdoor: boolean;
  is_temporary: boolean;
  capacity_note: string;
}

interface Floor1FormalCandidatePlan {
  floor_code: "1F";
  map_revision: string;
  plan_fingerprint: string;
  standard_pallet_mm: { width: number; depth: number };
  candidate_count: number;
  excluded_out_of_bounds_count: number;
  excluded_out_of_bounds: Array<{
    id: string;
    feature_code: string;
    name: string;
    reason: "outside_measured_bounds";
  }>;
  obstacle_count: number;
  formal_location_count: number;
  long_term_pallet_capacity: number;
  candidates: Floor1FormalCandidate[];
  formal_state: {
    fingerprint: string;
    already_applied: boolean;
    archivable_legacy_area_count: number;
    legacy_areas: Array<{
      area_code: string;
      area_name: string;
      active_location_count: number;
      live_lot_count: number;
      current_pallet_count: number;
      archive_required: boolean;
      action: "block" | "archive_empty_legacy" | "keep_archived_history";
    }>;
    blocking_conflicts: string[];
    blocking_items: Floor1CandidateBlockingItem[];
  };
  confirmation_required: boolean;
  applied?: boolean;
  message?: string;
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

interface LayoutDraftControl {
  has_draft: boolean;
  status: "none" | "draft" | "validated" | "published";
  published_revision: string;
  draft_revision: string | null;
  base_published_sha256?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  validated_at?: string | null;
  blockers: string[];
  warnings: string[];
}

interface TwinFloorDraftResponse extends TwinFloorResponse {
  draft_control: LayoutDraftControl;
}

interface LayoutDraftValidationResponse {
  status: "draft" | "validated";
  floor_code: string;
  draft_revision: string;
  blockers: string[];
  warnings: string[];
  validated_at?: string | null;
  inventory_changed: false;
  applied: boolean;
}

interface LayoutDraftPublishResponse {
  status: "published";
  floor_code: string;
  published_revision: string;
  backup_name: string;
  inventory_changed: false;
  applied: boolean;
}

interface OneStepAreaConfirmResponse extends LayoutDraftPublishResponse {
  area: FormalWarehouseAreaOption;
  message: string;
  advanced_draft_preserved: boolean;
  created_location_count: number;
  available_location_count: number;
  pallet_binding_changed: false;
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

export function availableFormalAreasForFeature(
  areas: FormalWarehouseAreaOption[],
  features: TwinFeature[],
  selectedFeature: TwinFeature
) {
  const occupiedDraftCodes = new Set(
    features
      .filter((feature) => feature.id !== selectedFeature.id && feature.erp_area_code)
      .map((feature) => String(feature.erp_area_code).toUpperCase())
  );
  return areas.filter((area) => (
    !area.storage_policy
    && Number(area.recorded_location_count || 0) === 0
    && !occupiedDraftCodes.has(area.area_code.toUpperCase())
    && (!selectedFeature.erp_area_code || area.area_code === selectedFeature.erp_area_code)
  ));
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
    source_sha256: raw.revision || raw.source_sha256 || "",
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

function palletMoveSource(location: DashboardLocation, pallet?: DashboardPallet | null): WarehouseMoveSource | null {
  const selectedPallet = pallet || singleLocationPallet(location) as DashboardPallet | null;
  if (!selectedPallet?.pallet_id || !selectedPallet.version) return null;
  const firstItem = selectedPallet.items[0];
  return {
    source_key: `pallet:${selectedPallet.pallet_id}`,
    operation: "pallet_move",
    pallet_id: selectedPallet.pallet_id,
    expected_version: selectedPallet.version,
    source_location_id: location.location_id,
    source_floor_code: location.floor_code,
    source_area_code: location.area_code,
    source_location_code: location.location_code,
    source_location_name: location.location_name,
    inventory_code: selectedPallet.pallet_code,
    product_name: firstItem?.product_name || `整栈板 ${selectedPallet.pallet_code}`,
    customer_name: firstItem?.customer_name || "客户待确认",
    unit: firstItem?.unit || "boxes"
  };
}

function dashboardPalletSummary(pallet: DashboardPallet) {
  const items = pallet.items || [];
  const products = [...new Set(items.map((item) => item.product_name).filter(Boolean))];
  const customers = [...new Set(items.map((item) => item.customer_name).filter(Boolean))];
  return {
    product: products.length === 1 ? products[0] : products.length > 1 ? `${products[0]} 等 ${products.length} 款` : "产品名称待补充",
    customer: customers.length === 1 ? customers[0] : customers.length > 1 ? `${customers.length} 个客户` : "客户待确认",
    quantity: items.reduce((sum, item) => sum + Number(inventoryLabelQuantity(item) || 0), 0),
    unit: items.find((item) => item.unit)?.unit || "boxes"
  };
}

function movableLotQuantity(item: InventoryItem) {
  if (item.available_quantity !== undefined || item.reserved_quantity !== undefined) {
    return Number(item.available_quantity || 0) + Number(item.reserved_quantity || 0);
  }
  return Number(item.quantity || 0);
}

function searchProductKey(item: InventoryItem) {
  return warehouseSearchProductKey(item);
}

function groupSearchProducts(items: SearchItem[]): SearchProductGroup[] {
  const groups = new Map<string, SearchProductGroup>();
  for (const item of items) {
    const key = searchProductKey(item);
    const current: SearchProductGroup = groups.get(key) || {
      key,
      customer_id: item.customer_id || null,
      customer_name: item.customer_name || "客户待确认",
      inventory_code: item.inventory_code || item.lot_number || `批次 ${item.lot_id}`,
      product_name: item.product_name || "产品名称待补充",
      specification: item.specification || null,
      total_quantity: 0,
      unit: item.unit || "boxes",
      location_count: 0,
      floor_summaries: [],
      location_summaries: [],
      items: [] as SearchItem[]
    };
    current.total_quantity += Number(item.quantity ?? inventoryLabelQuantity(item));
    current.items.push(item);
    current.location_count = new Set(current.items.map((row) => row.location_id || `text:${row.location_name}`)).size;
    current.floor_summaries = warehouseSearchFloorSummaries(current.items);
    current.location_summaries = warehouseSearchLocationSummaries(current.items);
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

function moldRackBlockedLevels(rack: Pick<Rack, "mold_rack_code">) {
  return ["R01", "R02"].includes(rack.mold_rack_code || "") ? [1] : [];
}

function rackLevelCellCounts(rack: Pick<Rack, "levels" | "cargo_rows" | "bays" | "level_cell_counts" | "mold_rack_code">) {
  const levels = Math.max(1, Math.min(20, Math.round(Number(rack.levels) || 1)));
  if (Array.isArray(rack.level_cell_counts) && rack.level_cell_counts.length === levels) {
    return rack.level_cell_counts.map((value) => Math.max(0, Math.min(50, Math.round(Number(value) || 0))));
  }
  const legacyCount = rack.mold_rack_code
    ? Math.max(1, Math.min(50, Math.round(Number(rack.bays) || 1)))
    : Math.max(3, Math.min(5, Math.round(Number(rack.cargo_rows) || 3)));
  return Array.from({ length: levels }, () => legacyCount);
}

function rackDraft(rack: Rack): RackDraft {
  const levelCellCounts = rackLevelCellCounts(rack);
  for (const level of moldRackBlockedLevels(rack)) {
    if (level <= levelCellCounts.length) levelCellCounts[level - 1] = 0;
  }
  return {
    ...rack,
    level_clear_heights_mm: rackClearHeights(rack),
    level_cell_counts: levelCellCounts
  };
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

function isMeasuredDispatchFeature(feature: LayoutFeature | undefined) {
  return Boolean(
    feature
    && feature.feature_kind === "zone"
    && String((feature as TwinFeature).subtype || "").toLowerCase() === "finished_wait_delivery"
  );
}

function defaultInventoryUsages(feature: TwinFeature): InventoryUsage[] {
  const subtype = String(feature.subtype || "").toLowerCase();
  if (subtype.includes("semi")) return ["semi_finished"];
  if (subtype.includes("raw")) return ["raw_material"];
  if (subtype.includes("mold")) return ["mold"];
  if (subtype.includes("plate") || subtype.includes("printing")) return ["print_plate"];
  if (subtype.includes("temporary")) return ["temporary_turnover"];
  return ["finished"];
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

function MoldRackElevation({
  rack,
  response,
  loading,
  error,
  rackIndex,
  rackCount,
  onPrevious,
  onNext,
  onClose
}: {
  rack: Rack;
  response: MoldRackResponse | null;
  loading: boolean;
  error: string;
  rackIndex: number;
  rackCount: number;
  onPrevious: () => void;
  onNext: () => void;
  onClose: () => void;
}) {
  const rackView = useMemo(
    () => buildMoldRackView(rack, response?.items || [], response?.rack.blocked_levels || []),
    [rack, response]
  );
  const occupiedCells = rackView.levels.flatMap((level) => level.cells
    .filter((cell) => cell.items.length)
    .map((cell) => ({ key: `L${level.level}-G${cell.grid}`, level: level.level, grid: cell.grid, items: cell.items })));
  const [selectedSlotKey, setSelectedSlotKey] = useState<string | null>(null);
  const [selectedMoldId, setSelectedMoldId] = useState<number | null>(null);
  const [selectedProductId, setSelectedProductId] = useState<number | null>(null);
  useEffect(() => {
    const first = occupiedCells[0];
    setSelectedSlotKey(first?.key || null);
    setSelectedMoldId(null);
    setSelectedProductId(null);
  }, [rack.id, response?.total]);
  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowLeft") onPrevious();
      if (event.key === "ArrowRight") onNext();
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose, onPrevious, onNext]);
  const selectedSlot = occupiedCells.find((cell) => cell.key === selectedSlotKey) || null;
  const selectedMold = response?.items.find((item) => item.id === selectedMoldId) || null;
  const selectedProduct = selectedMold?.products.find((product) => product.id === selectedProductId) || null;
  const visibleSpines = buildMoldShelfSpines(selectedSlot?.items || response?.items || []);
  const unmatchedCount = rackView.unmatched_items.length + rackView.rack_only_items.length
    + rackView.levels.reduce((sum, level) => sum + level.level_only_items.length, 0);
  const levels = [...rackView.levels].reverse();

  return <div className="twin-rack-modal" role="dialog" aria-modal="true" aria-label={`${rack.rack_code} 模具资产正视图`}>
    <button className="twin-modal-backdrop" type="button" aria-label="关闭模具货架正视图" onClick={onClose} />
    <section className="twin-rack-stage twin-mold-rack-stage">
      <header>
        <div><small>LIVE MOLD ASSET ELEVATION · {response?.rack.mold_rack_code || rack.mold_rack_code || rack.rack_code}</small><h2>{rack.name}</h2><p>{formatNumber(rack.width_mm)} × {formatNumber(rack.depth_mm)} × {formatNumber(rack.height_mm)} mm · {rack.levels} 层 · 同区货架 {rackIndex + 1}/{rackCount}</p></div>
        <button type="button" onClick={onClose}>返回孪生地图</button>
      </header>
      {response?.rack.uses_legacy_bays && <div className="twin-mold-rack-legacy-note">当前发布地图仍沿用旧统一 {rack.bays || 1} 格结构；请在“区域规划”中补齐每层实际格数，发布前不会改动任何模具位置。</div>}
      <div className="twin-rack-content">
        <button type="button" className="twin-rack-switch previous" aria-label="上一个同区域货架" onClick={onPrevious}>‹</button>
        <div className="twin-elevation-shell">
          <div className="twin-height-ruler"><b>{formatNumber(rack.height_mm)} mm</b></div>
          <div className="twin-elevation-frame">
            {levels.map((level) => <div className={`twin-elevation-level mold-level ${level.blocked ? "blocked" : ""}`} key={level.level}>
              <span>第 {level.level} 层 · {level.blocked ? "设备占用，不作为模具位置" : level.cell_count ? `${level.cell_count} 格` : "尚未分格"}</span>
              <div className={level.blocked || level.cell_count === 0 ? "unpartitioned" : ""}>
                {level.blocked ? <i className="twin-unpartitioned-cell mold-blocked-cell">设备占用层</i> : level.cell_count === 0 ? <i className="twin-unpartitioned-cell">本层尚未分格</i> : level.cells.map((cell) => {
                  const key = `L${level.level}-G${cell.grid}`;
                  const spines = buildMoldShelfSpines(cell.items);
                  return <section className={`mold-rack-cell ${cell.items.length ? "occupied" : "empty"} ${selectedSlotKey === key ? "selected" : ""}`} key={key}>
                    <button type="button" className="mold-rack-cell-summary" onClick={() => { setSelectedSlotKey(key); setSelectedMoldId(null); setSelectedProductId(null); }}><b>第 {cell.grid} 格</b><strong>{cell.items.length ? `${cell.items.length} 件模具 · ${spines.length} 个产品书脊` : "空格"}</strong></button>
                    {spines.length ? <div className="mold-rack-book-spines">{spines.map((spine, index) => <button
                      type="button"
                      className={selectedMoldId === spine.mold_id && selectedProductId === spine.product_id ? "selected" : ""}
                      key={spine.key}
                      title={`${spine.code} · ${spine.name} · 模具 ${spine.mold_code}`}
                      onClick={() => { setSelectedSlotKey(key); setSelectedMoldId(spine.mold_id); setSelectedProductId(spine.product_id); }}
                    ><small>{index + 1}</small><b>{spine.code}</b><span>{spine.name}</span></button>)}</div> : <span className="mold-rack-empty-spine">尚无模具</span>}
                  </section>;
                })}
              </div>
            </div>)}
          </div>
          <div className="twin-width-ruler">正面宽度 {formatNumber(rack.width_mm)} mm · 共 {response?.total || 0} 件正式模具</div>
        </div>
        <aside className="twin-mold-rack-aside">
          <small>ERP MOLD ASSET · 实时台账</small>
          {loading && <><h3>正在读取正式模具台账…</h3><p>只读，不修改模具位置或绑定。</p></>}
          {!loading && error && <><h3>模具读取失败</h3><p className="error">{error}</p></>}
          {!loading && !error && selectedMold && <article className="twin-rack-product-label mold-label">
            <span>{selectedProduct ? "当前产品书脊" : "未绑定产品的模具"}</span><h3>{selectedProduct?.product_code || selectedMold.mold_code}</h3><strong>{selectedProduct?.product_name || selectedMold.mold_name}</strong>
            <dl>{selectedProduct && <div><dt>客户</dt><dd>{selectedProduct.customer_name || "客户待确认"}</dd></div>}<div><dt>关联模具</dt><dd>{selectedMold.mold_code} · {selectedMold.mold_name}</dd></div><div><dt>正式位置</dt><dd>{selectedMold.rack_location}</dd></div><div><dt>现场指引</dt><dd>{selectedMold.location_guide?.prompt || "位置指引待补充"}</dd></div><div><dt>关联产品</dt><dd>{selectedMold.product_count} 款</dd></div></dl>
            <button type="button" className="twin-rack-detail-toggle" onClick={() => { setSelectedMoldId(null); setSelectedProductId(null); }}>返回本格产品书脊</button>
            <div className="twin-mold-product-links">{selectedMold.products.length ? selectedMold.products.map((product) => <button type="button" className={selectedProductId === product.id ? "selected" : ""} key={product.id} onClick={() => setSelectedProductId(product.id)}><b>{product.customer_name || "客户待确认"}</b><span>{product.product_code || "无存货编码"} · {product.product_name || "产品名称待补充"}</span></button>) : <p>当前模具尚未绑定产品。</p>}</div>
          </article>}
          {!loading && !error && !selectedMold && <>
            <h3>{selectedSlot ? `第 ${selectedSlot.level} 层 · 第 ${selectedSlot.grid} 格` : "当前货架模具"}</h3>
            <p>同一格可登记多件模具；货架按绑定产品显示为书脊。点击存货编码或名称，右侧查看产品、模具和正式位置。同格顺序只为阅读，不代表现场左右次序。</p>
            <strong>{selectedSlot ? `${selectedSlot.items.length} 件模具 · ${visibleSpines.length} 个书脊` : `${response?.total || 0} 件模具 · ${visibleSpines.length} 个书脊`}{unmatchedCount ? ` · ${unmatchedCount} 件未精确到当前格` : ""}</strong>
            <div className="twin-mold-rack-item-list">{visibleSpines.map((spine) => <button type="button" key={spine.key} onClick={() => { setSelectedMoldId(spine.mold_id); setSelectedProductId(spine.product_id); }}><b>{spine.code}</b><span>{spine.name}</span><small>模具 {spine.mold_code} · {spine.rack_location}</small></button>)}</div>
            {response?.truncated && <p className="error">该货架超过 500 件，本页只显示前 500 件；请按模具编号查找其精确位置。</p>}
            {unmatchedCount > 0 && <p className="twin-mold-rack-warning">未精确到当前格的模具仍保留在台账中；调整层格不会自动搬动它们。</p>}
          </>}
        </aside>
        <button type="button" className="twin-rack-switch next" aria-label="下一个同区域货架" onClick={onNext}>›</button>
      </div>
    </section>
  </div>;
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
  const levelCellCounts = rackLevelCellCounts(rack);
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
  const slots = Array.from({ length: levelCellCounts.reduce((sum, value) => sum + value, 0) }, (_, index) => ({
    item: items[index] || null,
    location: items[index] ? null : emptyLocations[index - items.length] || null
  }));
  let slotOffset = 0;
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
            {levels.map((level) => {
              const cellCount = levelCellCounts[level - 1] || 0;
              const levelSlots = slots.slice(slotOffset, slotOffset + cellCount);
              slotOffset += cellCount;
              return <div className="twin-elevation-level" key={level}>
              <span>第 {level} 层 · {cellCount ? `${cellCount} 格` : "尚未分格"}</span>
              <div className={cellCount ? "" : "unpartitioned"}>{cellCount === 0 ? <i className="twin-unpartitioned-cell">本层尚未分格</i> : levelSlots.map((slot, bay) => {
                const item = slot.item;
                if (item) return <button type="button" className={selectedItem?.lot_id === item.lot_id ? "selected" : ""} key={`${item.lot_id}-${bay}`} onClick={() => { setSelectedItem(item); setDetailOpen(false); }}><b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b><span>{item.customer_name || "客户待确认"}</span><strong>{formatNumber(inventoryLabelQuantity(item))} {inventoryUnitLabel(item.unit)}</strong></button>;
                if (slot.location) return <button type="button" className="empty" key={`empty-${slot.location.location_id}`} disabled={!canChooseProducts} onClick={() => onChooseEmptyLocation(slot.location!.location_id)}><b>＋ 为此货位选产品</b><span>{slot.location.location_name}</span><strong>当前空位</strong></button>;
                return <i key={bay}>暂无已建空货位</i>;
              })}</div>
            </div>})}
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
  const [layoutDraftControl, setLayoutDraftControl] = useState<LayoutDraftControl | null>(null);
  const [planningPublishedRevision, setPlanningPublishedRevision] = useState("");
  const [assets, setAssets] = useState<AssetTemplate[]>([]);
  const [dashboard, setDashboard] = useState<TwinDashboard | null>(null);
  const [selected, setSelected] = useState<SelectedEntity>(null);
  const [layers, setLayers] = useState<LayerVisibility>(DEFAULT_LAYERS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [searchType, setSearchType] = useState<WarehouseSearchType>("finished");
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [searchRetryToken, setSearchRetryToken] = useState(0);
  const [searchResponse, setSearchResponse] = useState<SearchResponse | null>(null);
  const [focusedSearchItem, setFocusedSearchItem] = useState<SearchItem | null>(null);
  const [focusedSearchProductKey, setFocusedSearchProductKey] = useState<string | null>(null);
  const [focusedResource, setFocusedResource] = useState<LocateResource | null>(null);
  const [cameraFocusTarget, setCameraFocusTarget] = useState<CanvasFocusTarget | null>(null);
  const cameraFocusSequenceRef = useRef(0);
  const [pendingLocateResource, setPendingLocateResource] = useState<LocateResource | null>(null);
  const [pendingLocationId, setPendingLocationId] = useState<number | null>(() => {
    const requested = Number(query.get("location_id") || 0);
    return requested > 0 ? requested : null;
  });
  const [areaInventorySearch, setAreaInventorySearch] = useState("");
  const [areaInventoryDetailsOpen, setAreaInventoryDetailsOpen] = useState(false);
  const [moldAreaResponse, setMoldAreaResponse] = useState<MoldAreaResponse | null>(null);
  const [moldAreaPage, setMoldAreaPage] = useState(1);
  const [moldAreaLoading, setMoldAreaLoading] = useState(false);
  const [moldAreaError, setMoldAreaError] = useState("");
  const [moldRackResponse, setMoldRackResponse] = useState<MoldRackResponse | null>(null);
  const [moldRackLoading, setMoldRackLoading] = useState(false);
  const [moldRackError, setMoldRackError] = useState("");
  const [layerPanelOpen, setLayerPanelOpen] = useState(false);
  const [searchPanelOpen, setSearchPanelOpen] = useState(true);
  const [mapMode, setMapMode] = useState<WarehouseMapMode>(() => {
    const requested = query.get("mode");
    return requested === "move" || requested === "planning" ? requested : "lookup";
  });
  const [productionPanelOpen, setProductionPanelOpen] = useState(false);
  const [pendingAreaCode, setPendingAreaCode] = useState<string | null>(() => query.get("area_code")?.trim().toUpperCase() || null);
  const [pendingMapFeatureId, setPendingMapFeatureId] = useState<string | null>(() => query.get("map_feature_id")?.trim() || null);
  const [pendingAreaPolicyEdit, setPendingAreaPolicyEdit] = useState(query.get("edit") === "area_policy");
  const areaPolicyDeepLinkStartedRef = useRef(false);
  const [productionProjection, setProductionProjection] = useState<ProductionProjectionResponse | null>(null);
  const [productionTaskId, setProductionTaskId] = useState<number | null>(null);
  const [productionSearch, setProductionSearch] = useState("");
  const [productionTargetKind, setProductionTargetKind] = useState<"pallet" | "zone">("pallet");
  const [productionTargetId, setProductionTargetId] = useState("");
  const [productionBusy, setProductionBusy] = useState(false);
  const [productionMessage, setProductionMessage] = useState("");
  const [canEditLocations, setCanEditLocations] = useState(false);
  const [canExecuteWarehouse, setCanExecuteWarehouse] = useState(false);
  const [canViewProductionProjection, setCanViewProductionProjection] = useState(false);
  const [uiMode, setUiMode] = useState<"standard" | "large">("standard");
  const [locationEditMode, setLocationEditMode] = useState(false);
  const [areaPolicyEditMode, setAreaPolicyEditMode] = useState(false);
  const [locationDrafts, setLocationDrafts] = useState<Record<number, LocationLayoutGeometry>>({});
  const [rackDrafts, setRackDrafts] = useState<Record<string, RackDraft>>({});
  const [zonePolicyDrafts, setZonePolicyDrafts] = useState<Record<string, { allowed_inventory_types: InventoryUsage[]; storage_layout: StorageLayout }>>({});
  const [zoneGeometryDrafts, setZoneGeometryDrafts] = useState<Record<string, number[][]>>({});
  const [spatialEditBusy, setSpatialEditBusy] = useState(false);
  const [locationEditBusy, setLocationEditBusy] = useState(false);
  const [locationEditMessage, setLocationEditMessage] = useState("");
  const [swapSourceLocationId, setSwapSourceLocationId] = useState<number | null>(null);
  const [targetAreaLocationCount, setTargetAreaLocationCount] = useState("");
  const [areaLocationManagement, setAreaLocationManagement] = useState<AreaLocationManagement | null>(null);
  const [formalAreaCodeDraft, setFormalAreaCodeDraft] = useState("");
  const [formalAreaNameDraft, setFormalAreaNameDraft] = useState("");
  const [simpleAreaUsage, setSimpleAreaUsage] = useState<InventoryUsage>("finished");
  const [simpleAreaLayout, setSimpleAreaLayout] = useState<Exclude<StorageLayout, "mixed">>("pallet_ground");
  const [simpleAreaCapacity, setSimpleAreaCapacity] = useState("0");
  const [advancedAreaMaintenanceOpen, setAdvancedAreaMaintenanceOpen] = useState(false);
  const [locationPointEditAreaCode, setLocationPointEditAreaCode] = useState<string | null>(null);
  const [formalAreaOptions, setFormalAreaOptions] = useState<FormalWarehouseAreaOption[]>([]);
  const [selectedExistingAreaId, setSelectedExistingAreaId] = useState("");
  const [formalAreaOptionsError, setFormalAreaOptionsError] = useState("");
  const [floor1CandidatePlan, setFloor1CandidatePlan] = useState<Floor1FormalCandidatePlan | null>(null);
  const [floor1CandidateBusy, setFloor1CandidateBusy] = useState(false);
  const [warehouseOperationBusy, setWarehouseOperationBusy] = useState(false);
  const [warehouseOperationMessage, setWarehouseOperationMessage] = useState("");
  const [moveCandidates, setMoveCandidates] = useState<MoveCandidate[]>([]);
  const [moveCandidatesLoading, setMoveCandidatesLoading] = useState(false);
  const [moveCandidatesError, setMoveCandidatesError] = useState("");
  const [moveSource, setMoveSource] = useState<WarehouseMoveSource | null>(null);
  const [moveQuantity, setMoveQuantity] = useState("");
  const [moveTargetFloorCode, setMoveTargetFloorCode] = useState("");
  const [moveTargetAreaCode, setMoveTargetAreaCode] = useState("");
  const [moveDraftTargetLocationId, setMoveDraftTargetLocationId] = useState("");
  const [moveDrafts, setMoveDrafts] = useState<WarehouseMoveDraft[]>([]);
  const [moveBatchIdempotencyKey, setMoveBatchIdempotencyKey] = useState(() => operationKey("warehouse-move-batch"));
  const [moveBatchBusy, setMoveBatchBusy] = useState(false);
  const [moveAction, setMoveAction] = useState<"relocate" | "merge">("relocate");
  const [mergeSources, setMergeSources] = useState<PalletMergeCandidate[]>([]);
  const [mergeTarget, setMergeTarget] = useState<PalletMergeCandidate | null>(null);
  const [mergeBatchIdempotencyKey, setMergeBatchIdempotencyKey] = useState(() => operationKey("warehouse-pallet-merge-batch"));
  const [mergeBatchBusy, setMergeBatchBusy] = useState(false);
  const [locationDetailOpen, setLocationDetailOpen] = useState(false);
  const [locationItemsExpanded, setLocationItemsExpanded] = useState(false);
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
  const [dispatchTransferLotId, setDispatchTransferLotId] = useState<number | null>(null);
  const [dispatchTransferQuantity, setDispatchTransferQuantity] = useState("");
  const [dispatchTransferTargetId, setDispatchTransferTargetId] = useState("");
  const [dispatchTransferIdempotencyKey, setDispatchTransferIdempotencyKey] = useState(() => operationKey("dispatch-to-floor3"));
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
        setCanExecuteWarehouse(value.permissions.includes("warehouse.execute"));
        setCanViewProductionProjection(value.permissions.includes("warehouse.view"));
        setUiMode(value.user.ui_mode === "large" ? "large" : "standard");
      })
      .catch(() => {
        setCanEditLocations(false);
        setCanExecuteWarehouse(false);
        setCanViewProductionProjection(false);
      });
  }, [refreshDashboard]);

  useEffect(() => {
    if (viewMode === "25d") {
      setMapMode("lookup");
      setSearchPanelOpen(true);
      setLocationEditMode(false);
      setAreaPolicyEditMode(false);
      setRackDrafts({});
      setZoneGeometryDrafts({});
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
    setZoneGeometryDrafts({});
    setLayoutDraftControl(null);
    setMapMode((current) => current === "move" || (current === "planning" && pendingAreaPolicyEdit) ? current : "lookup");
    setSearchPanelOpen(true);
    setLocationEditMode(false);
    setAreaPolicyEditMode(false);
    setFloor1CandidatePlan(null);
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

  useEffect(() => {
    if (!canExecuteWarehouse) {
      setMoveCandidates([]);
      setMoveCandidatesError("");
      return;
    }
    let active = true;
    setMoveCandidatesLoading(true);
    setMoveCandidatesError("");
    requestJson<LocationCandidatesResponse>(
      "/api/warehouse/location-candidates?inventory_type=finished&empty_only=true&pallet_storage_only=true&include_hierarchy=true&published_only=true"
    ).then((value) => {
      if (active) setMoveCandidates(value.items || []);
    }).catch((reason: Error) => {
      if (active) setMoveCandidatesError(reason.message);
    }).finally(() => {
      if (active) setMoveCandidatesLoading(false);
    });
    return () => { active = false; };
  }, [canExecuteWarehouse, dashboard?.generated_at]);

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
    const keyword = search.trim();
    if (!searchPanelOpen || keyword.length < 2) {
      setSearchResponse(null);
      setSearchLoading(false);
      setSearchError("");
      setFocusedSearchItem(null);
      setFocusedSearchProductKey(null);
      setFocusedResource(null);
      setCameraFocusTarget(null);
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ search_type: searchType, keyword });
      setSearchLoading(true);
      setSearchError("");
      requestJson<SearchResponse>(`/api/warehouse/twin-operations/locate?${params.toString()}`)
        .then((value) => {
          if (!active) return;
          setSearchResponse(value);
          setSearchError("");
        })
        .catch((reason: Error) => {
          if (!active) return;
          setSearchError(reason.message);
        })
        .finally(() => active && setSearchLoading(false));
    }, 300);
    return () => { active = false; window.clearTimeout(timer); };
  }, [searchPanelOpen, searchType, search, searchRetryToken]);

  useEffect(() => {
    if (!layout || layout.floor_code !== floorCode || !pendingMapFeatureId) return;
    const feature = (layout.features as TwinFeature[]).find(
      (item) => item.feature_kind === "zone" && item.id === pendingMapFeatureId
    );
    if (!feature) return;
    setSelected({ kind: "feature", id: feature.id });
    cameraFocusSequenceRef.current += 1;
    setCameraFocusTarget({ entity: { kind: "feature", id: feature.id }, token: cameraFocusSequenceRef.current, source: "search" });
    setPendingMapFeatureId(null);
  }, [layout, floorCode, pendingMapFeatureId]);

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

  const features = stableTwinFeatures(layout) as TwinFeature[];
  const warehouseMoveModeActive = mapMode === "move" && moveAction === "relocate" && canExecuteWarehouse && viewMode === "2d";
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
        version: Number(location.map_position?.version ?? draft.expected_version),
        source_type: "manual",
        layout_kind: location.map_position?.layout_kind
          ?? location.layout_draft_position?.layout_kind
          ?? "unknown"
      }
    } : location;
  }), [dashboard?.locations, locationDrafts]);
  const dispatchStagingLocation = useMemo(
    () => visualLocations.find((item) => item.floor_code === "1F" && item.location_code === "F1-DISPATCH-01"),
    [visualLocations]
  );
  const dispatchStagingPallets = useMemo(
    () => inventoryLocationPallets(dispatchStagingLocation) as DashboardPallet[],
    [dispatchStagingLocation]
  );
  useEffect(() => {
    if (!locationEditMode || !advancedAreaMaintenanceOpen) return;
    setLocationDrafts((current) => {
      const seeded = { ...current };
      let changed = false;
      for (const location of dashboard?.locations || []) {
        const proposal = location.layout_draft_position;
        if (
          location.floor_code !== floorCode
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
  }, [locationEditMode, advancedAreaMaintenanceOpen, floorCode, dashboard?.locations]);
  const mappedLocationPallets = useMemo(
    () => [
      ...buildMappedLocationPallets(features, visualLocations, floorCode, layout?.id)
        .filter((pallet) => pallet.id !== `erp-location-${dispatchStagingLocation?.location_id || 0}`),
      ...buildMeasuredDispatchPallets(features, dispatchStagingLocation, floorCode, layout?.id)
    ],
    [features, visualLocations, dispatchStagingLocation, floorCode, layout?.id]
  );
  const palletColumnConflicts = useMemo(
    () => layout ? findPalletColumnConflicts(mappedLocationPallets, layout.structures, features) : [],
    [mappedLocationPallets, layout?.structures, features]
  );
  const palletColumnConflictIds = useMemo(
    () => new Set(palletColumnConflicts.map((item) => item.pallet_id)),
    [palletColumnConflicts]
  );
  const columnConflictLocationIds = useMemo(
    () => palletColumnConflicts
      .map((item) => Number(item.pallet_id.replace("erp-location-", "")))
      .filter((locationId) => Number.isFinite(locationId) && locationId > 0),
    [palletColumnConflicts]
  );
  const moveReservedTargetIds = useMemo(
    () => moveDrafts
      .filter((item) => item.source_key !== moveSource?.source_key)
      .map((item) => item.target_location_id),
    [moveDrafts, moveSource?.source_key]
  );
  const mappedMoveTargets = useMemo(
    () => intersectMappedMoveTargets(moveCandidates, visualLocations, moveReservedTargetIds, columnConflictLocationIds),
    [moveCandidates, visualLocations, moveReservedTargetIds, columnConflictLocationIds]
  );
  const moveTargetFloors = useMemo(
    () => [...new Set(mappedMoveTargets.map((item) => item.floor_code))]
      .sort((left, right) => left.localeCompare(right, "zh-CN", { numeric: true })),
    [mappedMoveTargets]
  );
  const moveTargetAreas = useMemo(
    () => [...new Set(mappedMoveTargets
      .filter((item) => item.floor_code === moveTargetFloorCode)
      .map((item) => item.area_code || "未分区"))]
      .sort((left, right) => left.localeCompare(right, "zh-CN", { numeric: true })),
    [mappedMoveTargets, moveTargetFloorCode]
  );
  const moveTargetLocations = useMemo(
    () => mappedMoveTargets.filter((item) =>
      item.floor_code === moveTargetFloorCode
      && (item.area_code || "未分区") === moveTargetAreaCode
      && item.location_id !== moveSource?.source_location_id
    ),
    [mappedMoveTargets, moveTargetFloorCode, moveTargetAreaCode, moveSource?.source_location_id]
  );
  const selectedMoveTarget = moveTargetLocations.find(
    (item) => String(item.location_id) === moveDraftTargetLocationId
  ) || null;
  const mergeTargetChoices = useMemo(() => palletMergeTargetChoices(mergeSources), [mergeSources]);
  const movablePalletIds = useMemo(
    () => visualLocations
      .filter((item) => item.floor_code === floorCode
        && item.position_status === "mapped"
        && item.occupancy_status === "occupied"
        && inventoryLocationPallets(item).length === 1)
      .map((item) => `erp-location-${item.location_id}`),
    [visualLocations, floorCode]
  );
  const movePreviewPallets = useMemo(() => {
    if (mapMode !== "move" || !moveDrafts.length) return mappedLocationPallets;
    return mappedLocationPallets.map((pallet) => {
      if (pallet.id.startsWith("erp-dispatch-pallet-")) {
        const palletId = Number(pallet.id.replace("erp-dispatch-pallet-", ""));
        const outbound = moveDrafts.find((item) => item.operation === "pallet_move" && item.pallet_id === palletId);
        if (!outbound) return pallet;
        return {
          ...pallet,
          name: `${pallet.name} · 页面草稿待移出`,
          color: "#64748b",
          visual_status: "empty" as const,
          status_note: `页面草稿 · 待一次确认后移往 ${outbound.target_floor_code} / ${outbound.target_location_name}`
        };
      }
      const locationId = Number(pallet.id.replace("erp-location-", ""));
      const location = visualLocations.find((item) => item.location_id === locationId);
      const locationPallets = inventoryLocationPallets(location);
      const outboundPalletIds = new Set(moveDrafts
        .filter((item) => item.operation === "pallet_move" && item.source_location_id === locationId)
        .map((item) => item.pallet_id));
      const outbound = moveDrafts.find((item) => item.operation === "pallet_move" && item.source_location_id === locationId);
      if (outbound && locationPallets.length === 1 && outboundPalletIds.has(locationPallets[0].pallet_id)) {
        const hasLooseInventory = Boolean(location?.loose_items?.length);
        return {
          ...pallet,
          name: `${pallet.name} · 页面草稿移出`,
          color: hasLooseInventory ? "#0f766e" : "#64748b",
          visual_status: hasLooseInventory ? "waiting" as const : "empty" as const,
          status_note: hasLooseInventory
            ? `页面草稿 · 栈板待移往 ${outbound.target_location_name}，原位置仍有散存批次`
            : `页面草稿 · 待一次确认后移往 ${outbound.target_location_name}`
        };
      }
      if (outbound && locationPallets.length > 1) {
        const movedCount = locationPallets.filter((item) => outboundPalletIds.has(item.pallet_id)).length;
        const remainingCount = Math.max(locationPallets.length - movedCount, 0);
        const hasLooseInventory = Boolean(location?.loose_items?.length);
        return {
          ...pallet,
          name: `${location?.location_name || pallet.name} · ${movedCount}/${locationPallets.length} 块待移出`,
          color: remainingCount || hasLooseInventory ? "#0f766e" : "#64748b",
          visual_status: remainingCount || hasLooseInventory ? "waiting" as const : "empty" as const,
          status_note: `页面草稿 · 共享位置仍有 ${remainingCount} 块系统栈板${hasLooseInventory ? "及散存批次" : ""}`
        };
      }
      const inbound = moveDrafts.find((item) => item.target_location_id === locationId);
      if (inbound) return {
        ...pallet,
        name: `${pallet.name} · 页面草稿移入`,
        color: "#c2410c",
        visual_status: "in_process" as const,
        status_note: `页面草稿 · 来自 ${inbound.source_location_name}`
      };
      return pallet;
    });
  }, [mappedLocationPallets, mapMode, moveDrafts, visualLocations]);
  const visualLayout = useMemo(
    () => layout ? {
      ...layout,
      features: layout.features.map((feature) => zoneGeometryDrafts[feature.id]
        ? { ...feature, points: zoneGeometryDrafts[feature.id] }
        : feature),
      racks: layout.racks.map((rack) => rackDrafts[rack.id] || rack),
      pallets: [...layout.pallets, ...movePreviewPallets],
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
    [layout, zoneGeometryDrafts, rackDrafts, movePreviewPallets, palletColumnConflicts]
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
      const rack = layout.racks.find((item) => item.id === entity.id);
      if (locationPointEditAreaCode && (!rack || rackAreaCode(rack, features) !== locationPointEditAreaCode)) {
        setLocationEditMessage(`当前正在调整 ${locationPointEditAreaCode} 区货位；请先保存并固定或取消，再切换区域。`);
        return;
      }
      if (locationEditMode && !advancedAreaMaintenanceOpen && !rack?.mold_rack_code) {
        const zoneId = rack?.area_feature_id;
        if (zoneId) setSelected({ kind: "feature", id: zoneId });
        setLocationEditMessage("已定位货架所属区域；日常规划只需确认区域用途、形式和容量。");
        return;
      }
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
    if (entity.kind === "pallet" && entity.id.startsWith("erp-dispatch-pallet-")) {
      const palletId = Number(entity.id.replace("erp-dispatch-pallet-", ""));
      const pallet = dispatchStagingPallets.find((item) => item.pallet_id === palletId);
      setSelected(entity);
      if (mapMode === "move" && dispatchStagingLocation && pallet) {
        const source = palletMoveSource(dispatchStagingLocation, pallet);
        if (!source) {
          setWarehouseOperationMessage("该待送栈板缺少可移动版本，请刷新后重试。");
          return;
        }
        setMoveSource(source);
        setMoveQuantity("");
        setMoveTargetFloorCode(floorCode);
        setMoveTargetAreaCode("");
        setMoveDraftTargetLocationId("");
        setWarehouseOperationMessage(`已选待送栈板 ${pallet.pallet_code}；来源保持不变，请切换 1F 或 3F 后点区域和具体空货位。`);
      }
      return;
    }
    if (entity.kind === "pallet" && entity.id.startsWith("erp-location-")) {
      const locationId = Number(entity.id.replace("erp-location-", ""));
      const location = visualLocations.find((item) => item.location_id === locationId);
      if (locationPointEditAreaCode && location?.area_code !== locationPointEditAreaCode) {
        setLocationEditMessage(`当前只可拖动 ${locationPointEditAreaCode} 区货位；请先保存并固定或取消。`);
        return;
      }
      if (mapMode === "move" && moveSource) {
        const target = mappedMoveTargets.find((item) => item.location_id === locationId);
        if (target && target.location_id !== moveSource.source_location_id) {
          setMoveTargetFloorCode(target.floor_code);
          setMoveTargetAreaCode(target.area_code || "未分区");
          setMoveDraftTargetLocationId(String(target.location_id));
          setWarehouseOperationMessage(`已回填目标：${target.floor_code} · ${target.area_code || "未分区"} · ${target.location_name}；尚未写入。`);
        }
      }
      setSelected(entity);
      return;
    }
    if (
      entity.kind === "feature"
      && layout?.features.some((item) => item.id === entity.id && item.feature_kind === "zone")
    ) {
      const feature = features.find((item) => item.id === entity.id);
      const areaCode = featureAreaCode(feature);
      if (locationPointEditAreaCode && areaCode !== locationPointEditAreaCode) {
        setLocationEditMessage(`当前正在调整 ${locationPointEditAreaCode} 区货位；请先保存并固定或取消，再切换区域。`);
        return;
      }
      if (mapMode === "move" && moveSource) {
        const targets = mappedMoveTargets.filter((item) => item.floor_code === floorCode && item.area_code === areaCode);
        setMoveTargetFloorCode(floorCode);
        setMoveTargetAreaCode(areaCode || "");
        setMoveDraftTargetLocationId("");
        setWarehouseOperationMessage(areaCode && targets.length
          ? `已选 ${floorCode} · ${areaCode} 区；来源栈板仍保留，请继续点地图中的具体空货位。`
          : `当前区域没有可用空货位；来源栈板仍保留，可切换其他楼层或区域继续选择。`);
      }
      setSelected(entity);
      return;
    }
    setSelected(null);
  }, [layout, locationEditMode, advancedAreaMaintenanceOpen, locationPointEditAreaCode, rackDrafts, mapMode, moveSource, mappedMoveTargets, dispatchStagingLocation, dispatchStagingPallets, floorCode, features, visualLocations]);
  const selectedFeature = selected?.kind === "feature"
    ? features.find((item) => item.id === selected.id && item.feature_kind === "zone")
    : undefined;
  const selectedPlacement = selected?.kind === "equipment" ? layout?.placements.find((item) => item.id === selected.id) : undefined;
  const selectedRack = selected?.kind === "rack" ? visualLayout?.racks.find((item) => item.id === selected.id) : undefined;
  const selectedLocation = selected?.kind === "pallet"
    ? visualLocations.find((item) => `erp-location-${item.location_id}` === selected.id)
    : undefined;
  const selectedDispatchPallet = selected?.kind === "pallet" && selected.id.startsWith("erp-dispatch-pallet-")
    ? dispatchStagingPallets.find((item) => `erp-dispatch-pallet-${item.pallet_id}` === selected.id)
    : undefined;
  const selectedLocationPallets = useMemo(
    () => inventoryLocationPallets(selectedLocation) as DashboardPallet[],
    [selectedLocation]
  );
  const selectedLocationSinglePallet = useMemo(
    () => singleLocationPallet(selectedLocation) as DashboardPallet | null,
    [selectedLocation]
  );
  const selectedLocationItems = useMemo(
    () => selectedLocation ? inventoryLocationItems(selectedLocation) as InventoryItem[] : [],
    [selectedLocation]
  );
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
  const visibleSelectedLocationItems = locationItemsExpanded || mapMode !== "lookup"
    ? selectedLocationItems
    : selectedLocationItems.slice(0, 4);
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
  const selectedLocationPolicyTypes = selectedLocation?.allowed_inventory_types || [];
  const selectedLocationPolicyAllows = (inventoryType: InventoryUsage) => (
    selectedLocationPolicyTypes.length === 0
    || selectedLocationPolicyTypes.includes(inventoryType)
  );
  const selectedLocationCanReceiveFinished = Boolean(
    selectedLocationBaseReceivable
    && selectedLocation
    && ["finished", "shared"].includes(selectedLocation.warehouse_type)
    && selectedLocationPolicyAllows("finished")
  );
  const selectedLocationCanReceiveSemiFinished = Boolean(
    selectedLocationBaseReceivable
    && selectedLocation
    && ["1F", "3F"].includes(selectedLocation.floor_code)
    && ["semi_finished", "shared"].includes(selectedLocation.warehouse_type)
    && selectedLocationPolicyAllows("semi_finished")
  );
  const selectedLocationCanReceiveRawMaterial = Boolean(
    selectedLocationBaseReceivable
    && selectedLocation?.occupancy_status === "empty"
    && selectedLocation.storage_type !== "rack"
    && selectedLocation.warehouse_type === "shared"
    && selectedLocationPolicyAllows("raw_material")
  );
  const selectedLocationCanReceiveProduct = inboundInventoryType === "finished"
    ? selectedLocationCanReceiveFinished
    : inboundInventoryType === "semi_finished"
      ? selectedLocationCanReceiveSemiFinished
      : selectedLocationCanReceiveRawMaterial;
  const selectedLocationSupportsPallet = Boolean(
    selectedLocation
    && selectedLocationSinglePallet
    && ["1F", "3F"].includes(selectedLocation.floor_code)
    && selectedLocation?.storage_type !== "rack"
  );
  const selectedLocationCanReceiveStaging = Boolean(
    selectedLocationCanReceiveFinished
    && selectedLocation?.storage_type !== "rack"
    && selectedLocation?.location_code !== "F1-DISPATCH-01"
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
    (item) => item.feature_kind === "zone" && (
      item.id === selectedRack?.area_feature_id
      || featureAreaCode(item) === (selectedLocationAreaCode || selectedRackAreaCode)
      || item.feature_code === selectedRack?.area_code
    )
  );
  const selectedAreaBoundaryPoints = selectedAreaFeature
    ? (zoneGeometryDrafts[selectedAreaFeature.id] || selectedAreaFeature.points)
    : [];
  const selectedAreaBoundary = selectedAreaBoundaryPoints.length
    ? pointsBoundsMm(selectedAreaBoundaryPoints) : null;
  const selectedFeatureIsMeasuredDispatch = floorCode === "1F" && isMeasuredDispatchFeature(selectedFeature);
  const dispatchStagingItems = dispatchStagingLocation?.loose_items || [];
  const selectedDispatchStagingItem = dispatchStagingItems.find(
    (item) => item.lot_id === dispatchTransferLotId
  );
  const selectedAreaCode = featureAreaCode(selectedAreaFeature) || selectedLocationAreaCode || selectedRackAreaCode;
  const selectedAreaIsMold = Boolean(
    selectedRack?.mold_rack_code
    || selectedAreaFeature?.subtype.toLowerCase().includes("mold")
    || selectedAreaFeature?.allowed_inventory_types?.includes("mold")
  );
  const selectedArea = selectedAreaCode ? areaStats.get(selectedAreaCode) : undefined;
  const selectedAreaLocations = visualLocations.filter(
    (item) => item.floor_code === floorCode && item.area_code === selectedAreaCode && item.is_active
  );
  const selectedAreaLocationCount = selectedAreaLocations.length;
  const selectedAreaLayoutVersions = Object.fromEntries(
    selectedAreaLocations
      .map((location) => [
        location.location_id,
        location.map_position?.version ?? location.layout_draft_position?.version
      ])
      .filter((entry): entry is [number, number] => Number.isInteger(entry[1]))
      .map(([locationId, version]) => [locationId, Number(version)])
  );
  const locationPointEditPalletIds = locationPointEditAreaCode
    ? visualLocations
      .filter((item) => item.floor_code === floorCode && item.area_code === locationPointEditAreaCode && item.position_status === "mapped")
      .map((item) => `erp-location-${item.location_id}`)
    : undefined;
  const locationPointDraftCount = locationPointEditAreaCode
    ? Object.values(locationDrafts).filter((draft) => dashboard?.locations.some(
      (location) => location.location_id === draft.location_id && location.floor_code === floorCode && location.area_code === locationPointEditAreaCode
    )).length
    : 0;
  const activeLocationDraftCount = locationPointEditAreaCode
    ? locationPointDraftCount
    : Object.keys(locationDrafts).length;
  const selectedAreaPendingLocationCount = selectedAreaLocations.filter((item) => item.position_status === "unplaced").length;
  const selectedAreaRacks = (visualLayout?.racks || []).filter((rack) => (
    (selectedAreaFeature?.id && rack.area_feature_id === selectedAreaFeature.id)
    || (selectedAreaFeature?.feature_code && rack.area_code === selectedAreaFeature.feature_code)
    || (selectedAreaCode && rackAreaCode(rack, features) === selectedAreaCode)
  ));
  const selectedAreaMoldRacks = selectedAreaFeature
    ? moldRacksForArea(selectedAreaFeature, visualLayout?.racks || [])
    : selectedAreaRacks.filter((rack) => Boolean(rack.mold_rack_code));
  const reloadAreaLocationManagement = async (areaCode: string | null | undefined) => {
    if (!areaCode) return null;
    const value = await requestJson<AreaLocationManagement>(
      `/api/warehouse/spatial-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(areaCode)}/management`
    );
    setAreaLocationManagement(value);
    return value;
  };
  const inferredAreaInventoryTypes = Array.from(new Set(selectedAreaLocations.flatMap((location): InventoryUsage[] => {
    if (location.warehouse_type === "semi_finished") return ["semi_finished"];
    if (location.warehouse_type === "shared") return ["finished", "semi_finished"];
    return ["finished"];
  })));
  const selectedZonePolicy = selectedAreaFeature ? (zonePolicyDrafts[selectedAreaFeature.id] || {
    allowed_inventory_types: selectedAreaFeature.allowed_inventory_types?.length
      ? selectedAreaFeature.allowed_inventory_types
      : inferredAreaInventoryTypes.length ? inferredAreaInventoryTypes : defaultInventoryUsages(selectedAreaFeature),
    storage_layout: selectedAreaFeature.storage_layout
      || (selectedAreaFeature.subtype.includes("rack") ? "rack" : selectedAreaRacks.length ? "mixed" : "pallet_ground")
  }) : null;
  const selectedAreaHasPublishedBinding = Boolean(
    (
      selectedAreaFeature?.formal_policy_status === "published"
      || selectedAreaFeature?.formal_binding_status === "published"
    )
    && selectedAreaFeature?.formal_area_id
  );
  useEffect(() => {
    setAreaLocationManagement(null);
    if (!canEditLocations || !locationEditMode || !selectedAreaCode || !selectedAreaHasPublishedBinding) return;
    let current = true;
    requestJson<AreaLocationManagement>(
      `/api/warehouse/spatial-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(selectedAreaCode)}/management`
    ).then((value) => {
      if (current) setAreaLocationManagement(value);
    }).catch((reason: Error) => {
      if (current) setLocationEditMessage(`区域库位管理路径读取失败：${reason.message}`);
    });
    return () => { current = false; };
  }, [canEditLocations, locationEditMode, floorCode, selectedAreaCode, selectedAreaHasPublishedBinding]);
  const selectedAreaCreatesInventoryLocations = Boolean(
    selectedZonePolicy?.allowed_inventory_types.some((value) => value === "finished" || value === "semi_finished")
  );
  const selectedAreaHasFormalLedger = Boolean(
    selectedAreaHasPublishedBinding
  );
  const selectedAreaCapacityReviewUrl = selectedAreaFeature?.formal_area_id && selectedAreaFeature.erp_area_code
    ? (() => {
      const params = new URLSearchParams({
        location_view: "ledger",
        capacity_review: "1",
        area_id: String(selectedAreaFeature.formal_area_id),
        floor_id: String(selectedAreaFeature.formal_floor_id || ""),
        area_code: selectedAreaFeature.erp_area_code,
        map_feature_id: selectedAreaFeature.id
      });
      return `/warehouse-ledger.html?${params.toString()}`;
    })()
    : "";
  const selectedRackEditDraft = selectedRack
    ? (rackDrafts[selectedRack.id] || rackDraft(selectedRack))
    : null;
  const selectedInventory = useMemo(
    () => expandAreaInventory(dashboard?.locations || [], floorCode, selectedAreaCode),
    [dashboard?.locations, floorCode, selectedAreaCode]
  );
  useEffect(() => {
    setMoldAreaPage(1);
    setAreaInventorySearch("");
    setAreaInventoryDetailsOpen(false);
  }, [selectedAreaFeature?.feature_code]);
  useEffect(() => {
    if (!selectedAreaIsMold || !selectedAreaFeature) {
      setMoldAreaResponse(null);
      setMoldAreaError("");
      setMoldAreaLoading(false);
      return;
    }
    let active = true;
    setMoldAreaLoading(true);
    setMoldAreaError("");
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({
        floor_code: floorCode,
        feature_code: selectedAreaFeature.feature_code,
        page: String(moldAreaPage),
        page_size: "20"
      });
      if (areaInventorySearch.trim()) params.set("q", areaInventorySearch.trim());
      requestJson<MoldAreaResponse>(`/api/warehouse/molds/by-map-area?${params.toString()}`)
        .then((value) => {
          if (active) setMoldAreaResponse(value);
        })
        .catch((reason: Error) => {
          if (active) {
            setMoldAreaResponse(null);
            setMoldAreaError(reason.message);
          }
        })
        .finally(() => {
          if (active) setMoldAreaLoading(false);
        });
    }, 180);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [selectedAreaIsMold, selectedAreaFeature?.feature_code, floorCode, moldAreaPage, areaInventorySearch]);
  useEffect(() => {
    setTargetAreaLocationCount(selectedAreaCode ? String(selectedAreaLocationCount) : "");
  }, [selectedAreaCode, selectedAreaLocationCount]);
  useEffect(() => {
    if (!selectedAreaFeature) {
      setFormalAreaCodeDraft("");
      setFormalAreaNameDraft("");
      setSelectedExistingAreaId("");
      return;
    }
    const suggested = selectedAreaFeature.feature_code
      .replace(/^ZONE-(?:1F|3F)-/i, "")
      .replace(/[^A-Z0-9-]+/gi, "-")
      .replace(/^-+|-+$/g, "")
      .toUpperCase()
      .slice(0, 30);
    setFormalAreaCodeDraft(selectedAreaFeature.erp_area_code || suggested);
    setFormalAreaNameDraft(selectedAreaFeature.formal_area_name || selectedAreaFeature.name || suggested);
    const currentUsage = (selectedAreaFeature.allowed_inventory_types || [])[0] as InventoryUsage | undefined;
    setSimpleAreaUsage(currentUsage || "finished");
    setSimpleAreaLayout(selectedAreaFeature.storage_layout === "rack" ? "rack" : "pallet_ground");
    setSimpleAreaCapacity(String(
      selectedAreaFeature.confirmed_pallet_capacity
      ?? selectedAreaFeature.planned_pallet_capacity
      ?? 0
    ));
    setAdvancedAreaMaintenanceOpen(false);
    setSelectedExistingAreaId("");
  }, [selectedAreaFeature?.id, selectedAreaFeature?.erp_area_code, selectedAreaFeature?.formal_area_name, selectedAreaFeature?.name]);
  const shouldLoadFormalAreaOptions = formalAreaOptionsEffectEnabled({
    canEditLocations,
    locationEditMode,
    areaPolicyEditMode,
    hasSelectedFeature: Boolean(selectedAreaFeature),
    formalAreaId: selectedAreaFeature?.formal_area_id
  });
  useEffect(() => {
    setSelectedExistingAreaId("");
    setFormalAreaOptionsError("");
    if (!shouldLoadFormalAreaOptions || !selectedAreaFeature) {
      setFormalAreaOptions(clearFormalAreaOptions);
      return;
    }
    let active = true;
    requestJson<WarehouseSpaceResponse>("/api/warehouse/space/floors")
      .then((value) => {
        if (!active) return;
        const floorNumber = Number(floorCode.replace(/\D/g, ""));
        const floor = (value.items || []).find((item) => Number(item.floor_number) === floorNumber);
        setFormalAreaOptions(availableFormalAreasForFeature(floor?.areas || [], features, selectedAreaFeature));
      })
      .catch((reason: Error) => {
        if (!active) return;
        setFormalAreaOptions(clearFormalAreaOptions);
        setFormalAreaOptionsError(reason.message);
      });
    return () => { active = false; };
  }, [shouldLoadFormalAreaOptions, selectedAreaFeature?.id, selectedAreaFeature?.formal_area_id, selectedAreaFeature?.erp_area_code, floorCode, features]);
  const selectExistingFormalArea = (areaId: string) => {
    setSelectedExistingAreaId(areaId);
    const area = formalAreaOptions.find((item) => String(item.id) === areaId);
    if (!area) {
      const suggested = selectedAreaFeature?.feature_code
        .replace(/^ZONE-(?:1F|3F)-/i, "")
        .replace(/[^A-Z0-9-]+/gi, "-")
        .replace(/^-+|-+$/g, "")
        .toUpperCase()
        .slice(0, 30) || "";
      setFormalAreaCodeDraft(selectedAreaFeature?.erp_area_code || suggested);
      setFormalAreaNameDraft(selectedAreaFeature?.formal_area_name || selectedAreaFeature?.name || suggested);
      setLocationEditMessage("已取消选用现有区域；请核对下方正式区域编号后再保存草稿。");
      return;
    }
    setFormalAreaCodeDraft(area.area_code);
    setFormalAreaNameDraft(area.area_name);
    setLocationEditMessage(`已选用现有区域 ${area.floor_code} · ${area.area_code} ${area.area_name}；保存后仍是地图草稿，校验并发布才会建立绑定。`);
  };
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
  useEffect(() => {
    if (!focusedRack?.mold_rack_code) {
      setMoldRackResponse(null);
      setMoldRackLoading(false);
      setMoldRackError("");
      return;
    }
    let active = true;
    setMoldRackResponse(null);
    setMoldRackLoading(true);
    setMoldRackError("");
    const params = new URLSearchParams({ floor_code: floorCode, rack_id: focusedRack.id });
    requestJson<MoldRackResponse>(`/api/warehouse/molds/by-map-rack?${params.toString()}`)
      .then((value) => { if (active) setMoldRackResponse(value); })
      .catch((reason: Error) => { if (active) setMoldRackError(reason.message); })
      .finally(() => { if (active) setMoldRackLoading(false); });
    return () => { active = false; };
  }, [focusedRack?.id, focusedRack?.mold_rack_code, floorCode]);
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
        ...inventoryLocationPallets(location).flatMap((pallet) => (pallet.items || []).map((item) => ({
          ...item,
          location_code: location.location_code,
          location_name: location.location_name,
          pallet_code: pallet.pallet_code || null
        }))),
        ...location.loose_items.map((item) => ({
          ...item,
          location_code: location.location_code,
          location_name: location.location_name,
          pallet_code: null
        }))
      ]);
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
    setInboundMode(selectedLocationCanReceiveStaging && selectedLocation?.occupancy_status === "empty" ? "staging" : "catalog");
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
    setLocationDetailOpen(false);
    setLocationItemsExpanded(false);
    setMoveTargetLocationId("");
    setWarehouseOperationMessage("");
  }, [selected?.kind, selected?.id, selectedLocation?.occupancy_status, selectedLocationCanReceiveStaging]);
  useEffect(() => {
    if (mapMode !== "move") return;
    setMoveTargetFloorCode((current) => moveTargetFloors.includes(current)
      ? current
      : moveTargetFloors.includes(floorCode) ? floorCode : "");
  }, [mapMode, floorCode, moveTargetFloors]);
  useEffect(() => {
    if (mapMode !== "move") return;
    setMoveTargetAreaCode((current) => moveTargetAreas.includes(current) ? current : "");
  }, [mapMode, moveTargetAreas]);
  useEffect(() => {
    if (moveTargetLocations.some((item) => String(item.location_id) === moveDraftTargetLocationId)) return;
    setMoveDraftTargetLocationId("");
  }, [moveTargetLocations, moveDraftTargetLocationId]);
  const filteredSelectedInventory = useMemo(
    () => filterAreaInventory(selectedInventory, areaInventorySearch),
    [selectedInventory, areaInventorySearch]
  );
  const visibleSelectedInventory = useMemo(() => {
    if (areaInventoryDetailsOpen || areaInventorySearch.trim()) return filteredSelectedInventory;
    const focused = filteredSelectedInventory.filter((item) => (
      (focusedSearchItem?.lot_id && focusedSearchItem.lot_id === item.lot_id)
      || (focusedSearchProductKey && searchProductKey(item) === focusedSearchProductKey)
    ));
    const focusedIds = new Set(focused.map((item) => item.lot_id));
    return [...focused, ...filteredSelectedInventory.filter((item) => !focusedIds.has(item.lot_id))].slice(0, 5);
  }, [areaInventoryDetailsOpen, areaInventorySearch, filteredSelectedInventory, focusedSearchItem?.lot_id, focusedSearchProductKey]);
  const selectedAreaQuantitySummary = selectedArea?.quantities.length
    ? selectedArea.quantities.map((item) => `${formatNumber(item.available)} ${inventoryUnitLabel(item.unit)}`).join(" / ")
    : "0";
  const selectedAreaActivationLabel = selectedAreaHasPublishedBinding
    ? "已启用"
    : selectedAreaFeature?.formal_binding_status === "draft"
      ? "待启用"
      : "未启用";
  const selectedAreaCapacitySummary = selectedAreaFeature?.capacity_review_status === "confirmed"
    && selectedAreaFeature.capacity_eligible
    ? `${selectedAreaFeature.confirmed_pallet_capacity || 0} 个栈板`
    : selectedAreaFeature?.capacity_review_status === "excluded"
      || (selectedAreaFeature?.capacity_review_status === "confirmed" && !selectedAreaFeature.capacity_eligible)
      ? "不放栈板"
      : "待确认";
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
    if (!canEditLocations || viewMode !== "2d" || !selectedLocationCanReceiveStaging || selectedLocation?.occupancy_status !== "empty" || inboundMode !== "staging") {
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
  }, [stagingQuery, canEditLocations, viewMode, selectedLocation?.location_id, selectedLocation?.occupancy_status, selectedLocationCanReceiveStaging, inboundMode]);

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
    if (!locationEditMode || (!advancedAreaMaintenanceOpen && !locationPointEditAreaCode)) return;
    const locationId = Number(palletId.replace("erp-location-", ""));
    const location = visualLocations.find((item) => item.location_id === locationId);
    if (locationPointEditAreaCode && location?.area_code !== locationPointEditAreaCode) {
      setLocationEditMessage(`当前只可拖动 ${locationPointEditAreaCode} 区货位；其他区域保持固定。`);
      return;
    }
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

  const lotMoveSource = (location: DashboardLocation, item: InventoryItem): WarehouseMoveSource | null => {
    const maximum = movableLotQuantity(item);
    if (!item.lot_id || !item.version || !Number.isFinite(maximum) || maximum <= 0) return null;
    return {
      source_key: `lot:${item.lot_id}`,
      operation: "lot_transfer",
      lot_id: item.lot_id,
      expected_version: item.version,
      quantity: maximum,
      max_quantity: maximum,
      source_location_id: location.location_id,
      source_floor_code: location.floor_code,
      source_area_code: location.area_code,
      source_location_code: location.location_code,
      source_location_name: location.location_name,
      inventory_code: item.inventory_code || item.lot_number || `批次 ${item.lot_id}`,
      product_name: item.product_name || "产品名称待补充",
      customer_name: item.customer_name || "客户待确认",
      unit: item.unit || "boxes"
    };
  };

  const chooseMoveSource = (source: WarehouseMoveSource | null) => {
    if (!source) {
      setWarehouseOperationMessage("该库存缺少版本或可移动数量，请刷新后重试。");
      return;
    }
    setMoveSource(source);
    setMoveQuantity(source.operation === "lot_transfer" ? String(source.max_quantity || source.quantity || "") : "");
    setMoveDraftTargetLocationId("");
    setMoveTargetFloorCode((current) => current || source.source_floor_code);
    setWarehouseOperationMessage(`已选货物：${source.source_floor_code} · ${source.source_location_name}；请选择要移动到的空货位。`);
  };

  const queueMoveDraft = (source: WarehouseMoveSource, target: DashboardLocation) => {
    if (!target.map_position) {
      setWarehouseOperationMessage("目标货位布局版本缺失，请刷新地图后重试。");
      return;
    }
    if (source.source_location_id === target.location_id) {
      setWarehouseOperationMessage("来源与目标不能是同一货位。");
      return;
    }
    const quantity = source.operation === "lot_transfer" ? Number(moveQuantity || source.quantity) : undefined;
    if (source.operation === "lot_transfer" && (!Number.isFinite(quantity) || Number(quantity) <= 0 || Number(quantity) > Number(source.max_quantity || 0))) {
      setWarehouseOperationMessage(`移动数量必须大于 0，且不能超过可移动 ${formatNumber(source.max_quantity)} ${inventoryUnitLabel(source.unit)}。`);
      return;
    }
    const draft: WarehouseMoveDraft = {
      ...source,
      client_item_id: operationKey("move-item"),
      quantity,
      target_location_id: target.location_id,
      expected_target_layout_version: target.map_position.version,
      target_floor_code: target.floor_code,
      target_area_code: target.area_code,
      target_location_code: target.location_code,
      target_location_name: target.location_name
    };
    const result = upsertMoveDraft(moveDrafts, draft);
    if (result.error) {
      setWarehouseOperationMessage(result.error);
      return;
    }
    setMoveDrafts(result.items);
    setMoveBatchIdempotencyKey(operationKey("warehouse-move-batch"));
    setMoveDraftTargetLocationId("");
    setWarehouseOperationMessage(`已加入页面草稿：${source.source_location_name} → ${target.location_name}；尚未写入，底部一次确认后才提交。`);
  };

  const addSelectedMoveDraft = () => {
    if (!moveSource || !selectedMoveTarget) {
      setWarehouseOperationMessage("请先选来源，再按楼层、区域、具体货位选择目标空位。");
      return;
    }
    queueMoveDraft(moveSource, selectedMoveTarget);
  };

  const draftMoveFromDrag = (palletId: string, xMm: number, yMm: number) => {
    if (mapMode !== "move" || !canExecuteWarehouse) return;
    const sourceLocationId = Number(palletId.replace("erp-location-", ""));
    const sourceLocation = visualLocations.find((item) => item.location_id === sourceLocationId);
    const sourcePallet = sourceLocation ? singleLocationPallet(sourceLocation) as DashboardPallet | null : null;
    const source = sourceLocation && sourcePallet ? palletMoveSource(sourceLocation, sourcePallet) : null;
    if (!sourceLocation || !source) {
      setWarehouseOperationMessage("共享待送位置包含多块系统栈板，不能用一张聚合地图卡猜测来源；请在右侧逐块选择。散存也请在右侧选择批次。");
      return;
    }
    const candidates = mappedMoveTargets.filter((item) => item.floor_code === floorCode && item.location_id !== sourceLocationId);
    const result = resolveMoveDropTarget(features, candidates, floorCode, xMm, yMm);
    if (!result.target) {
      setWarehouseOperationMessage(result.error || "没有命中可用空货位。");
      return;
    }
    setMoveSource(source);
    queueMoveDraft(source, result.target);
    setSelected({ kind: "pallet", id: palletId });
  };

  const removeMoveDraft = (clientItemId: string) => {
    setMoveDrafts((current) => current.filter((item) => item.client_item_id !== clientItemId));
    setMoveBatchIdempotencyKey(operationKey("warehouse-move-batch"));
    setWarehouseOperationMessage("已撤销该条页面草稿，正式库存未改变。");
  };

  const clearMoveDrafts = () => {
    setMoveDrafts([]);
    setMoveBatchIdempotencyKey(operationKey("warehouse-move-batch"));
    setWarehouseOperationMessage("已清空页面草稿，正式库存未改变。");
  };

  const toggleMergeSource = (location: DashboardLocation, pallet: DashboardPallet) => {
    const normalized = normalizePalletMergeCandidate(location, pallet);
    if (!normalized.candidate) {
      setWarehouseOperationMessage(normalized.error || "该系统栈板不能作为合并来源。");
      return;
    }
    const normalizedCandidate = normalized.candidate;
    const candidate = mergeSources.some((item) => item.pallet_id === normalizedCandidate.pallet_id)
      ? normalizedCandidate
      : { ...normalizedCandidate, client_item_id: operationKey("pallet-merge-source") };
    const result = togglePalletMergeSource(mergeSources, candidate);
    if (result.error) {
      setWarehouseOperationMessage(result.error);
      return;
    }
    setMergeSources(result.items);
    if (mergeTarget && !result.items.some((item) => item.pallet_id === mergeTarget.pallet_id)) setMergeTarget(null);
    setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
    setWarehouseOperationMessage(result.items.some((item) => item.pallet_id === normalizedCandidate.pallet_id)
      ? `已加入合并集合：${normalizedCandidate.pallet_code}；可跨楼层继续选择。`
      : `已移出合并集合：${normalizedCandidate.pallet_code}；正式库存未改变。`);
  };

  const chooseMergeTarget = (candidate: PalletMergeCandidate) => {
    if (!mergeSources.some((item) => item.pallet_id === candidate.pallet_id)) {
      setWarehouseOperationMessage("目标栈板必须从已选集合中明确指定。");
      return;
    }
    setMergeTarget(candidate);
    setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
    setWarehouseOperationMessage(`已选目标：${candidate.floor_code} · ${candidate.location_name} · ${candidate.pallet_code}；尚未写入。`);
  };

  const clearMergeDraft = () => {
    setMergeSources([]);
    setMergeTarget(null);
    setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
    setWarehouseOperationMessage("已清空多栈合并草稿，正式库存未改变。");
  };

  const confirmPalletMergeBatch = async () => {
    if (mergeSources.length < 2 || !mergeTarget || mergeBatchBusy) return;
    const submittedSources = mergeSources.filter((item) => item.pallet_id !== mergeTarget.pallet_id);
    const sourcePreview = submittedSources.map((item) => `${item.floor_code}/${item.location_code} · ${item.pallet_code}`).join("\n");
    if (!window.confirm(`确认一次把 ${submittedSources.length} 块源栈的全部库存批次并入目标栈吗？\n\n来源：\n${sourcePreview}\n\n目标：${mergeTarget.floor_code}/${mergeTarget.location_code} · ${mergeTarget.pallet_code}\n\n源栈将逻辑释放；库存数量、批次、预占和库龄不变。`)) return;
    setMergeBatchBusy(true);
    setWarehouseOperationMessage("");
    try {
      await mutateJson(
        "/api/warehouse/pallets/merge-batches",
        "POST",
        buildPalletMergeBatchPayload(mergeBatchIdempotencyKey, mergeSources, mergeTarget)
      );
      await refreshDashboard();
      const target = mergeTarget;
      setMergeSources([]);
      setMergeTarget(null);
      setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
      setFloorCode(target.floor_code);
      setSelected({ kind: "pallet", id: `erp-location-${target.location_id}` });
      setWarehouseOperationMessage(`${submittedSources.length} 块源栈已一次并入 ${target.pallet_code}；源栈已逻辑释放。`);
    } catch (reason) {
      setWarehouseOperationMessage(`多栈合并失败：${(reason as Error).message}。来源、目标和本次幂等键已保留，可核对后重试。`);
    } finally {
      setMergeBatchBusy(false);
    }
  };

  const confirmMoveDrafts = async () => {
    if (!moveDrafts.length || moveBatchBusy) return;
    const preview = moveDrafts.slice(0, 5).map((item) => `${item.source_location_name} → ${item.target_location_name}`).join("\n");
    if (!window.confirm(`确认一次提交 ${moveDrafts.length} 条移货吗？\n${preview}${moveDrafts.length > 5 ? "\n……" : ""}\n\n提交后才会改变正式库存位置。`)) return;
    setMoveBatchBusy(true);
    setWarehouseOperationMessage("");
    try {
      await mutateJson(
        "/api/warehouse/twin-operations/move-batches",
        "POST",
        buildMoveBatchPayload(moveBatchIdempotencyKey, moveDrafts)
      );
      await refreshDashboard();
      setMoveDrafts([]);
      setMoveSource(null);
      setMoveQuantity("");
      setMoveDraftTargetLocationId("");
      setMoveBatchIdempotencyKey(operationKey("warehouse-move-batch"));
      setWarehouseOperationMessage("整批移货已成功；地图已刷新。 ");
    } catch (reason) {
      setWarehouseOperationMessage(`整批提交失败：${(reason as Error).message}。页面草稿与本次幂等键已保留，可核对后重试。`);
    } finally {
      setMoveBatchBusy(false);
    }
  };

  const beginSelectedAreaLocationPointEdit = () => {
    if (!selectedAreaCode || !selectedAreaHasPublishedBinding || !selectedAreaLocationCount) {
      setLocationEditMessage("请先选择已启用且已有正式货位的区域。");
      return;
    }
    const unrelatedDrafts = Object.values(locationDrafts).filter((draft) => {
      const location = dashboard?.locations.find((item) => item.location_id === draft.location_id);
      return location?.floor_code !== floorCode || location?.area_code !== selectedAreaCode;
    });
    if (unrelatedDrafts.length) {
      setLocationEditMessage("还有其他区域的位置草稿；请先在高级维护中保存或取消，避免混入本次单区调整。");
      return;
    }
    setAdvancedAreaMaintenanceOpen(false);
    setLocationPointEditAreaCode(selectedAreaCode);
    setSwapSourceLocationId(null);
    setLocationEditMessage(`已进入 ${selectedAreaCode} 区点位调整：只可拖动本区货位。占用货位请先按现场实际摆放核对；保存只更新地图点位，不移动库存、栈板或货物。`);
  };

  const cancelLocationPointEditing = () => {
    if (!locationPointEditAreaCode) return;
    const cancelledAreaCode = locationPointEditAreaCode;
    setLocationDrafts((current) => {
      const next = { ...current };
      for (const draft of Object.values(current)) {
        const location = dashboard?.locations.find((item) => item.location_id === draft.location_id);
        if (location?.floor_code === floorCode && location?.area_code === cancelledAreaCode) delete next[draft.location_id];
      }
      return next;
    });
    setLocationPointEditAreaCode(null);
    setSwapSourceLocationId(null);
    setLocationEditMessage(`已取消 ${cancelledAreaCode} 区未保存的点位调整；正式地图、库存、栈板和货物均未改变。`);
  };

  const autoArrangeSelectedAreaLocations = async () => {
    if (!selectedAreaCode || !selectedAreaHasPublishedBinding || !selectedAreaLocationCount) {
      setLocationEditMessage("请先选择已启用且已有正式货位的区域。");
      return;
    }
    if (!areaLocationManagement?.available_actions.includes("auto_arrange")) {
      setLocationEditMessage("区域货位管理信息尚未就绪，请刷新后重试。");
      return;
    }
    const selectedAreaHasDrafts = Object.values(locationDrafts).some((draft) => dashboard?.locations.some(
      (location) => location.location_id === draft.location_id && location.floor_code === floorCode && location.area_code === selectedAreaCode
    ));
    if (selectedAreaHasDrafts) {
      setLocationEditMessage("当前区域还有未保存的手工点位草稿；请先保存并固定或取消，再自动排布。");
      return;
    }
    const occupiedCount = selectedAreaLocations.filter((location) => location.occupancy_status === "occupied").length;
    if (
      selectedAreaLocations.some(
        (location) => location.position_status !== "mapped" || !location.map_position
      )
      || Object.keys(selectedAreaLayoutVersions).length !== selectedAreaLocationCount
    ) {
      setLocationEditMessage("当前区域还有未完成落位的货位，请先刷新或完成点位维护后再自动排布。");
      return;
    }
    if (!window.confirm(`确认自动均匀排布 ${selectedAreaCode} 区的空闲系统货位吗？\n\n系统会按实测区域面积重新分散可自动管理的空闲货位并避开柱子；旧版本中尚未固定的历史系统点位也会在本次确认后纳入自动管理。${occupiedCount} 个占用货位以及已手工固定货位保持原位。此操作只更新地图点位，不移动库存、栈板或货物。`)) return;
    setLocationEditBusy(true);
    setLocationEditMessage("");
    try {
      const endpoint = `/api/warehouse/spatial-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(selectedAreaCode)}/auto-arrange`;
      const result = await mutateJson<{ message?: string; auto_arranged_count?: number }>(endpoint, "POST", {
        confirmed: true,
        adopt_historical_layouts: true,
        expected_map_revision: selectedAreaHasPublishedBinding
          ? areaLocationManagement?.published_map_revision || planningPublishedRevision || undefined
          : undefined,
        expected_policy_version: areaLocationManagement?.policy_version || undefined,
        expected_layout_versions: selectedAreaLayoutVersions
      });
      await refreshDashboard();
      await reloadAreaLocationManagement(selectedAreaCode);
      setLocationEditMessage(result?.message || `${selectedAreaCode} 区空闲货位已按区域面积均匀排布；占用货位及库存、栈板、货物均未移动。`);
    } catch (reason) {
      setLocationEditMessage((reason as Error).message);
    } finally {
      setLocationEditBusy(false);
    }
  };

  const saveLocationDrafts = async () => {
    const drafts = Object.values(locationDrafts).filter((draft) => {
      if (!locationPointEditAreaCode) return true;
      const location = dashboard?.locations.find((item) => item.location_id === draft.location_id);
      return location?.floor_code === floorCode && location?.area_code === locationPointEditAreaCode;
    });
    if (!drafts.length) return;
    const conflictingDrafts = palletColumnConflicts.filter((item) => {
      const locationId = Number(item.pallet_id.replace("erp-location-", ""));
      return drafts.some((draft) => draft.location_id === locationId);
    });
    if (conflictingDrafts.length) {
      setLocationEditMessage(`有 ${conflictingDrafts.length} 个货位仍与柱子重叠，已阻止保存；请先在二维地图中拖离柱子。`);
      return;
    }
    if (locationPointEditAreaCode) {
      const occupiedDraftCount = drafts.filter((draft) => dashboard?.locations.some(
        (location) => location.location_id === draft.location_id && location.occupancy_status === "occupied"
      )).length;
      if (!window.confirm(`确认保存并固定 ${locationPointEditAreaCode} 区 ${drafts.length} 个货位点位吗？\n\n其中 ${occupiedDraftCount} 个为占用货位，请确认已按现场实际位置调整。保存只更新地图点位，不移动库存、栈板或货物。`)) return;
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
        const management = await requestJson<AreaLocationManagement>(
          `/api/warehouse/spatial-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(areaCode)}/management`
        );
        if (!management.available_actions.includes("layout")) throw new Error(`${areaCode} 区当前不允许保存库位布局。`);
        const endpoint = `/api/warehouse/spatial-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(areaCode)}`;
        await mutateJson(endpoint, "PATCH", {
          slots,
          expected_map_revision: management.published_map_revision || undefined,
          expected_policy_version: management.policy_version || undefined
        });
      }
      const savedLocationIds = new Set(drafts.map((draft) => draft.location_id));
      setLocationDrafts((current) => Object.fromEntries(
        Object.entries(current).filter(([locationId]) => !savedLocationIds.has(Number(locationId)))
      ));
      setSwapSourceLocationId(null);
      await refreshDashboard();
      await reloadAreaLocationManagement(selectedAreaCode);
      if (locationPointEditAreaCode) setLocationPointEditAreaCode(null);
      setLocationEditMessage(`已保存并固定 ${drafts.length} 个货位点位；本次只更新地图位置，库存、栈板和货物未改变。`);
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
    const sourceKind = source.map_position.layout_kind || "unknown";
    const selectedKind = selectedLocation.map_position.layout_kind || "unknown";
    if (sourceKind === "unknown" || selectedKind === "unknown" || sourceKind !== selectedKind) {
      setLocationEditMessage("不同占地语义或尚待确认的历史货位不能直接交换；请分别拖动到实际位置后保存。");
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
    if (Object.values(locationDrafts).some((draft) => dashboard?.locations.some(
      (location) => location.location_id === draft.location_id && location.floor_code === floorCode && location.area_code === selectedAreaCode
    ))) {
      setLocationEditMessage("当前区域还有未保存的点位草稿；请先保存并固定或取消，再调整货位数量。");
      return;
    }
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
    if (!window.confirm(`确认把 ${selectedAreaCode} 区有效库位从 ${selectedAreaLocationCount} 个${direction}到 ${targetCount} 个吗？\n\n新增库位会自动编号、自动排列并立即可用；减少时只停用无库存、无预占、无实体栈板的空库位。`)) return;
    setLocationEditBusy(true);
    try {
      if (!areaLocationManagement?.available_actions.includes("location_count")) {
        throw new Error("区域库位管理路径尚未就绪，请刷新后重试。");
      }
      const endpoint = `/api/warehouse/spatial-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(selectedAreaCode)}/location-count`;
      const result = await mutateJson<AreaLocationCountResponse>(endpoint, "POST", {
        target_count: targetCount,
        confirmed: true,
        expected_map_revision: selectedAreaHasPublishedBinding
          ? areaLocationManagement?.published_map_revision || planningPublishedRevision || undefined
          : undefined,
        expected_policy_version: areaLocationManagement?.policy_version || undefined,
        expected_layout_versions: selectedAreaLayoutVersions
      });
      await refreshDashboard();
      setAreaLocationManagement((current) => current ? {
        ...current,
        policy_version: result?.policy_version ?? current.policy_version,
        published_map_revision: result?.published_map_revision ?? current.published_map_revision
      } : current);
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
      if (!areaLocationManagement?.available_actions.includes("disable_empty")) {
        throw new Error("区域库位管理路径尚未就绪，请刷新后重试。");
      }
      const endpoint = `/api/warehouse/spatial-layout/locations/${selectedLocation.location_id}/disable`;
      await mutateJson(endpoint, "POST", {
        expected_version: selectedLocation.map_position.version,
        expected_map_revision: areaLocationManagement.published_map_revision || undefined,
        expected_policy_version: areaLocationManagement.policy_version || undefined
      });
      setSelected(null);
      await refreshDashboard();
      await reloadAreaLocationManagement(selectedAreaCode);
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

  const focusSearchFloor = (group: SearchProductGroup, targetFloorCode: string) => {
    const target = group.items.find(
      (item) => item.floor_code === targetFloorCode && item.position_status === "mapped"
    ) || group.items.find((item) => item.floor_code === targetFloorCode);
    if (!target) return;
    setFocusedSearchProductKey(group.key);
    focusSearchItem(target);
  };

  const focusSearchLocation = (
    group: SearchProductGroup,
    location: SearchProductGroup["location_summaries"][number]
  ) => {
    const target = group.items.find((item) => (
      location.location_id
        ? item.location_id === location.location_id
        : item.floor_code === location.floor_code
          && item.area_code === location.area_code
          && item.location_name === location.location_name
    ));
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
    if (!selectedLocation?.map_position || !selectedStagingProduct || !inboundQuantity) return;
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
        expected_layout_version: selectedLocation.map_position.version,
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
    if (!selectedLocation?.map_position || !temporaryCustomerId || !temporaryInventoryCode.trim() || !temporaryProductName.trim() || !temporaryReason.trim() || !inboundQuantity || !inboundStockDate) return;
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
        expected_layout_version: selectedLocation.map_position.version,
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

  const confirmDispatchStagingTransfer = async () => {
    if (!selectedDispatchStagingItem || !dispatchTransferQuantity || !dispatchTransferTargetId) return;
    const target = emptyMoveTargets.find(
      (item) => String(item.location_id) === dispatchTransferTargetId
    );
    const quantity = Number(dispatchTransferQuantity);
    const maximum = inventoryLabelQuantity(selectedDispatchStagingItem);
    if (!target) {
      setWarehouseOperationMessage("请在三楼缩略图中点选当前空闲的正式成品位置。");
      return;
    }
    if (!Number.isInteger(quantity) || quantity <= 0 || quantity > maximum) {
      setWarehouseOperationMessage(`转入数量必须为 1 到 ${formatNumber(maximum)} 的整数。`);
      return;
    }
    if (!selectedDispatchStagingItem.version) {
      setWarehouseOperationMessage("待送库存版本缺失，请刷新地图后重试。");
      return;
    }
    if (!window.confirm(`确认将这批暂不送的货物转入三楼？\n\n客户：${selectedDispatchStagingItem.customer_name || "待确认"}\n产品：${selectedDispatchStagingItem.inventory_code || selectedDispatchStagingItem.product_name}\n数量：${quantity} ${inventoryUnitLabel(selectedDispatchStagingItem.unit)}\n目标：${target.location_name}\n\n本次只移动正式库存位置，不增加或减少库存总数。`)) return;
    setWarehouseOperationBusy(true);
    setWarehouseOperationMessage("");
    try {
      await mutateJson(`/api/warehouse/twin-operations/staging-lots/${selectedDispatchStagingItem.lot_id}/place`, "POST", {
        location_id: target.location_id,
        expected_layout_version: target.map_position!.version,
        expected_version: selectedDispatchStagingItem.version,
        quantity,
        idempotency_key: dispatchTransferIdempotencyKey,
        confirmed: true
      });
      await refreshDashboard();
      setFloorCode("3F");
      setSelected({ kind: "pallet", id: `erp-location-${target.location_id}` });
      setDispatchTransferLotId(null);
      setDispatchTransferQuantity("");
      setDispatchTransferTargetId("");
      setDispatchTransferIdempotencyKey(operationKey("dispatch-to-floor3"));
      setWarehouseOperationMessage(`${quantity} ${inventoryUnitLabel(selectedDispatchStagingItem.unit)}已转入${target.location_name}；库存总数未改变。`);
    } catch (reason) {
      setWarehouseOperationMessage((reason as Error).message);
    } finally {
      setWarehouseOperationBusy(false);
    }
  };

  const confirmMapFinishedInbound = async () => {
    if (!selectedLocation?.map_position || !selectedInboundProduct || !inboundQuantity || !inboundStockDate) return;
    const quantity = Number(inboundQuantity);
    if (!Number.isInteger(quantity) || quantity <= 0) {
      setWarehouseOperationMessage("补录数量必须是正整数。");
      return;
    }
    const correctionMode = selectedLocation.occupancy_status === "occupied";
    const inventoryTypeLabel = inboundInventoryType === "finished"
      ? "成品"
      : inboundInventoryType === "semi_finished"
        ? "半成品"
        : "原材料栈板";
    const quantityUnit = inboundInventoryType === "finished" ? "只" : "张";
    if (!window.confirm(`管理员二次确认：把 ${selectedInboundProduct.customer_name} / ${selectedInboundProduct.product_name} 共 ${quantity} ${quantityUnit}${correctionMode ? "合并补录到已有同产品位置" : "登记到当前地图位置"}？\n\n楼层：${floorCode}\n区域：${selectedLocation.area_code || "未分区"}\n位置：${selectedLocation.location_name}\n库存类型：${inventoryTypeLabel}\n\n系统使用内部 location_id 落账，员工无需记忆库位编码。`)) return;
    setWarehouseOperationBusy(true);
    setWarehouseOperationMessage("");
    try {
      if (inboundInventoryType === "raw_material") {
        await mutateJson("/api/warehouse/pallets", "POST", {
          location_id: selectedLocation.location_id,
          expected_layout_version: selectedLocation.map_position.version,
          pallet_code: inboundPalletCode.trim() || null,
          remarks: `二维地图人工确认原材料栈板入仓 · ${inboundStockDate}`,
          items: [{
            customer_id: selectedInboundProduct.customer_id,
            product_id: selectedInboundProduct.product_id,
            item_type: "raw_material",
            quantity,
            unit: "sheets",
            match_status: "matched",
            idempotency_key: inboundIdempotencyKey,
            remarks: "实测区域原材料实体栈板"
          }]
        });
      } else {
        const endpoint = inboundInventoryType === "finished"
          ? "/api/warehouse/twin-operations/finished-inbound"
          : "/api/warehouse/twin-operations/semi-finished-inbound";
        await mutateJson(endpoint, "POST", {
          location_id: selectedLocation.location_id,
          expected_layout_version: selectedLocation.map_position.version,
          ...(inboundInventoryType === "finished" ? { pallet_code: inboundPalletCode.trim() || null } : {}),
          customer_id: selectedInboundProduct.customer_id,
          product_id: selectedInboundProduct.product_id,
          quantity,
          stock_date: inboundStockDate,
          idempotency_key: inboundIdempotencyKey,
          confirmed: true,
          remarks: correctionMode ? `二维地图管理员确认${inventoryTypeLabel}差异合并补录` : `二维地图人工确认${inventoryTypeLabel}入仓`
        });
      }
      await refreshDashboard();
      setInboundQuantity("");
      setInboundPalletCode("");
      setInboundIdempotencyKey(operationKey("map-inbound"));
      setWarehouseOperationMessage(
        `${selectedLocation.location_name} 已${correctionMode ? "完成同产品合并补录" : `完成${inventoryTypeLabel}入仓`}；`
        + (inboundInventoryType === "raw_material" ? "实体栈板账已保存。" : "正式库存账已保存。")
      );
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
        expected_target_layout_version: target.map_position!.version,
        idempotency_key: moveIdempotencyKey,
        confirmed: true,
        remarks: "二维地图人工确认正式栈板移位"
      });
      await refreshDashboard();
      setFloorCode("3F");
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

  const showTwinFloor = (raw: TwinFloorResponse) => {
    setLayout(hydrateLayout(raw));
    setAssets(raw.assets || []);
  };

  const refreshPublishedTwinFloor = async () => {
    const raw = await requestJson<TwinFloorResponse>(`/api/warehouse/twin-layout/floors/${floorCode}`);
    showTwinFloor(raw);
    setLayoutDraftControl(null);
  };

  const refreshPlanningTwinFloor = async () => {
    const raw = await requestJson<TwinFloorDraftResponse>(`/api/warehouse/twin-layout/floors/${floorCode}/draft`);
    showTwinFloor(raw);
    setLayoutDraftControl(raw.draft_control);
    setPlanningPublishedRevision(raw.draft_control.published_revision);
  };

  const previewFloor1FormalCandidates = async () => {
    if (floorCode !== "1F" || !canEditLocations || floor1CandidateBusy) return;
    setFloor1CandidateBusy(true);
    setLocationEditMessage("");
    try {
      const plan = await requestJson<Floor1FormalCandidatePlan>("/api/warehouse/twin-layout/floors/1F/formal-candidates");
      setFloor1CandidatePlan(plan);
      setLocationEditMessage(`已按实测地图生成 ${plan.candidate_count} 个区域候选；确认前正式台账和库存均未改变。`);
    } catch (reason) {
      setLocationEditMessage(`读取一楼区域候选失败：${(reason as Error).message}`);
    } finally {
      setFloor1CandidateBusy(false);
    }
  };

  const confirmFloor1FormalCandidates = async () => {
    const plan = floor1CandidatePlan;
    if (!plan || floor1CandidateBusy) return;
    const confirmed = window.confirm(
      `一次确认一楼实体区域与库位？\n\n` +
      `已确认区域：${plan.candidate_count} 个\n` +
      `长期标准栈板容量：${plan.long_term_pallet_capacity} 个\n` +
      `成品/半成品正式库位：${plan.formal_location_count} 个\n\n` +
      (plan.formal_state.archivable_legacy_area_count
        ? `另有 ${plan.formal_state.archivable_legacy_area_count} 个无库存、无栈板且未映射的重复空台账记录将停用；实测区域不受影响。\n\n`
        : "") +
      `室外、临时周转区不计长期容量；模具、印版和原料继续使用各自台账，不会改动库存数量。`
    );
    if (!confirmed) return;
    setFloor1CandidateBusy(true);
    try {
      const result = await mutateJson<Floor1FormalCandidatePlan>(
        "/api/warehouse/twin-layout/floors/1F/formal-candidates/confirm",
        "POST",
        {
          expected_map_revision: plan.map_revision,
          expected_plan_fingerprint: plan.plan_fingerprint,
          expected_formal_state_fingerprint: plan.formal_state.fingerprint,
          operation_key: operationKey("floor1-formal-candidates"),
          confirmed: true
        }
      );
      await Promise.all([refreshDashboard(), refreshPublishedTwinFloor()]);
      setFloor1CandidatePlan(null);
      setLocationEditMessage(result?.message || "一楼实体区域与正式库位已确认启用。");
    } catch (reason) {
      setLocationEditMessage(`确认一楼区域候选失败：${(reason as Error).message}`);
    } finally {
      setFloor1CandidateBusy(false);
    }
  };

  const toggleLayoutEditor = async () => {
    if (!canEditLocations || spatialEditBusy) return;
    setSpatialEditBusy(true);
    setLocationEditMessage("");
    try {
      if (locationEditMode) {
        await refreshPublishedTwinFloor();
        setLocationEditMode(false);
        setAreaPolicyEditMode(false);
        setAdvancedAreaMaintenanceOpen(false);
        setLocationPointEditAreaCode(null);
        setLocationDrafts({});
        setRackDrafts({});
        setZonePolicyDrafts({});
        setZoneGeometryDrafts({});
        setSwapSourceLocationId(null);
        setLocationEditMessage("已退出布局编辑；当前显示员工正在使用的已发布地图。");
        return;
      }
      const raw = await requestJson<TwinFloorDraftResponse>(`/api/warehouse/twin-layout/floors/${floorCode}/draft`);
      showTwinFloor(raw);
      setLayoutDraftControl(raw.draft_control);
      setPlanningPublishedRevision(raw.draft_control.published_revision);
      setMapMode('planning');
      setViewMode('2d');
      setSearchPanelOpen(false);
      setLocationEditMode(true);
      setAreaPolicyEditMode(true);
      setAdvancedAreaMaintenanceOpen(false);
      setLocationPointEditAreaCode(null);
      setRackDrafts({});
      setZonePolicyDrafts({});
      setZoneGeometryDrafts({});
      setLocationEditMessage(raw.draft_control.has_draft
        ? "检测到以前保留的高级维护草稿，员工仍只看到已发布地图；系统会保留该草稿。只要当前区域本身没有高级改动，仍可直接一次确认启用。"
        : "区域规划已开启；选中区域后填写用途、形式和最大栈板数，一次确认即可启用。"
      );
    } catch (reason) {
      setLocationEditMessage(`打开布局草稿失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const returnToLookupMode = async () => {
    if (spatialEditBusy) return;
    setSpatialEditBusy(true);
    try {
      await refreshPublishedTwinFloor();
      setLocationEditMode(false);
      setAreaPolicyEditMode(false);
      setAdvancedAreaMaintenanceOpen(false);
      setLocationPointEditAreaCode(null);
      setRackDrafts({}); setZonePolicyDrafts({}); setZoneGeometryDrafts({});
      setLocationDrafts({}); setSwapSourceLocationId(null);
      setMapMode('lookup'); setSearchPanelOpen(true);
      setLocationEditMessage('已返回查货模式，当前只显示已发布地图。');
    } catch (reason) {
      setLocationEditMessage(`返回查货模式失败：${(reason as Error).message}`);
    } finally { setSpatialEditBusy(false); }
  };

  const enterWarehouseMoveMode = async () => {
    if (!canExecuteWarehouse || spatialEditBusy) return;
    setSpatialEditBusy(true);
    try {
      if (locationEditMode || mapMode === "planning") await refreshPublishedTwinFloor();
      setMapMode("move");
      setViewMode("2d");
      setSearchPanelOpen(false);
      setLocationEditMode(false);
      setAreaPolicyEditMode(false);
      setAdvancedAreaMaintenanceOpen(false);
      setLocationPointEditAreaCode(null);
      setRackDrafts({});
      setZonePolicyDrafts({});
      setZoneGeometryDrafts({});
      setLocationDrafts({});
      setSwapSourceLocationId(null);
      setLocationEditMessage("");
      setWarehouseOperationMessage(moveDrafts.length
        ? `已回到移货 / 盘点，保留 ${moveDrafts.length} 条页面草稿；尚未写入。`
        : "移货 / 盘点已开启：拖动或三级选择只形成页面草稿，底部一次确认后才提交。"
      );
    } catch (reason) {
      setWarehouseOperationMessage(`进入移货 / 盘点失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  useEffect(() => {
    if (
      !pendingAreaPolicyEdit
      || !canEditLocations
      || !layout
      || spatialEditBusy
      || areaPolicyDeepLinkStartedRef.current
    ) return;
    areaPolicyDeepLinkStartedRef.current = true;
    let active = true;
    const openAreaPolicy = async () => {
      setSpatialEditBusy(true);
      setLocationEditMessage("");
      try {
        const raw = await requestJson<TwinFloorDraftResponse>(`/api/warehouse/twin-layout/floors/${floorCode}/draft`);
        if (!active) return;
        showTwinFloor(raw);
        setLayoutDraftControl(raw.draft_control);
        setPlanningPublishedRevision(raw.draft_control.published_revision);
        setMapMode("planning");
        setViewMode("2d");
        setSearchPanelOpen(false);
        setLocationEditMode(true);
        setAreaPolicyEditMode(true);
        setAdvancedAreaMaintenanceOpen(false);
        setLocationPointEditAreaCode(null);
        setRackDrafts({});
        setZonePolicyDrafts({});
        setZoneGeometryDrafts({});
        setPendingAreaPolicyEdit(false);
        setLocationEditMessage("已打开阻断区域设置；处理并发布后，请返回原页面重新检查。");
      } catch (reason) {
        if (!active) return;
        setLocationEditMessage(`打开阻断区域设置失败：${(reason as Error).message}`);
        setPendingAreaPolicyEdit(false);
      } finally {
        areaPolicyDeepLinkStartedRef.current = false;
        if (active) setSpatialEditBusy(false);
      }
    };
    void openAreaPolicy();
    return () => { active = false; };
  }, [pendingAreaPolicyEdit, canEditLocations, layout?.id, floorCode]);

  const rememberServerDraft = (revision: string) => {
    setLayoutDraftControl((current) => ({
      has_draft: true,
      status: "draft",
      published_revision: current?.published_revision || layout?.source_sha256 || revision,
      draft_revision: revision,
      base_published_sha256: current?.base_published_sha256,
      created_at: current?.created_at,
      updated_at: new Date().toISOString(),
      validated_at: null,
      blockers: [],
      warnings: []
    }));
  };

  const validateLayoutDraft = async () => {
    if (!layout || !layoutDraftControl?.has_draft) return;
    setSpatialEditBusy(true);
    try {
      const result = await mutateJson<LayoutDraftValidationResponse>(
        `/api/warehouse/twin-layout/floors/${floorCode}/draft/validate`,
        "POST",
        { expected_revision: layout.source_sha256 }
      );
      if (!result) return;
      setLayoutDraftControl((current) => current ? {
        ...current,
        status: result.status,
        draft_revision: result.draft_revision,
        validated_at: result.validated_at,
        blockers: result.blockers,
        warnings: result.warnings
      } : current);
      setLocationEditMessage(result.blockers.length
        ? `草稿未通过：${result.blockers.slice(0, 3).join("；")}`
        : `草稿校验通过${result.warnings.length ? `，有 ${result.warnings.length} 条现场提示` : ""}；现在可以发布。`
      );
    } catch (reason) {
      setLocationEditMessage(`校验草稿失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const publishLayoutDraft = async () => {
    if (!layout || layoutDraftControl?.status !== "validated") return;
    if (!window.confirm("确认发布已校验的仓库地图吗？发布前会自动备份旧地图；库存数量不会改变。")) return;
    setSpatialEditBusy(true);
    try {
      const result = await mutateJson<LayoutDraftPublishResponse>(
        `/api/warehouse/twin-layout/floors/${floorCode}/draft/publish`,
        "POST",
        {
          expected_published_revision: layoutDraftControl.published_revision,
          expected_draft_revision: layout.source_sha256,
          operation_key: operationKey("layout-publish")
        }
      );
      if (!result) return;
      await refreshPublishedTwinFloor();
      setLocationEditMode(false);
      setAreaPolicyEditMode(false);
      setRackDrafts({});
      setZonePolicyDrafts({});
      setZoneGeometryDrafts({});
      setLocationEditMessage(`仓库地图已发布；旧地图备份为 ${result.backup_name}，库存数量未改变。`);
    } catch (reason) {
      setLocationEditMessage(`发布布局失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const discardLayoutDraft = async () => {
    if (!layout) return;
    if (!layoutDraftControl?.has_draft) {
      await toggleLayoutEditor();
      return;
    }
    if (!window.confirm("确认放弃整个布局草稿吗？已发布地图和库存不会改变。")) return;
    setSpatialEditBusy(true);
    try {
      await mutateJson(
        `/api/warehouse/twin-layout/floors/${floorCode}/draft/discard`,
        "POST",
        { expected_revision: layout.source_sha256 }
      );
      await refreshPublishedTwinFloor();
      setLocationEditMode(false);
      setAreaPolicyEditMode(false);
      setRackDrafts({});
      setZonePolicyDrafts({});
      setZoneGeometryDrafts({});
      setLocationEditMessage("布局草稿已放弃；已发布地图和库存均未改变。");
    } catch (reason) {
      setLocationEditMessage(`放弃草稿失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const moveRackDraft = (rackId: string, xMm: number, yMm: number) => {
    if (!locationEditMode || !advancedAreaMaintenanceOpen) return;
    updateRackDraft(rackId, { x_mm: Math.round(xMm), y_mm: Math.round(yMm) });
    setSelected({ kind: "rack", id: rackId });
    setLocationEditMessage("货架位置已修改；右侧确认参数后保存到布局草稿。");
  };

  const changeRackLevels = (rack: RackDraft, levels: number) => {
    const normalized = Math.max(1, Math.min(20, Math.round(levels || 1)));
    const base = Math.floor(rack.height_mm / normalized);
    const clear = Array.from({ length: normalized }, (_, index) => index === normalized - 1 ? rack.height_mm - base * (normalized - 1) : base);
    const levelCellCounts = Array.from(
      { length: normalized },
      (_, index) => rack.level_cell_counts[index] ?? 0
    );
    updateRackDraft(rack.id, { levels: normalized, level_clear_heights_mm: clear, level_heights_mm: rackShelfHeights(clear), level_cell_counts: levelCellCounts });
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

  const changeRackLevelCellCount = (rack: RackDraft, levelIndex: number, count: number) => {
    const levelCellCounts = [...rack.level_cell_counts];
    levelCellCounts[levelIndex] = Math.max(0, Math.min(50, Math.round(count || 0)));
    updateRackDraft(rack.id, { level_cell_counts: levelCellCounts });
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
    level_cell_counts: rack.level_cell_counts,
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
      rememberServerDraft(response.revision);
      setRackDrafts((current) => {
        const next = { ...current };
        delete next[selectedRack.id];
        return next;
      });
      setLocationEditMessage(`${response.item.name} 已保存到布局草稿；员工地图和库存数量均未改变。`);
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
          level_cell_counts: [0, 0, 0],
          bays: 1,
          access_side: "south",
          min_aisle_width_mm: 1500,
          rotation_deg: 0,
          color: "#38bdf8"
        }
      );
      if (!response) return;
      setLayout((current) => current ? { ...current, source_sha256: response.revision, racks: [...current.racks, response.item] } : current);
      rememberServerDraft(response.revision);
      setRackDrafts((current) => ({ ...current, [response.item.id]: rackDraft(response.item) }));
      setSelected({ kind: "rack", id: response.item.id });
      setLocationEditMessage(`${response.item.rack_code} 已加入布局草稿；请拖到实际位置并编辑参数。`);
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
      rememberServerDraft(response.revision);
      setRackDrafts((current) => {
        const next = { ...current };
        delete next[original.id];
        return next;
      });
      setSelected(selectedAreaFeature ? { kind: "feature", id: selectedAreaFeature.id } : null);
      setLocationEditMessage(`${original.name} 已从布局草稿删除；员工地图、库存与正式库位未改变。`);
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
    if (!formalAreaCodeDraft.trim()) {
      setLocationEditMessage("请输入正式区域编号；保存后该地图区域才能生成正式库位。");
      return;
    }
    const selectedExistingArea = formalAreaOptions.find((item) => String(item.id) === selectedExistingAreaId);
    const existingAreaId = selectedExistingArea?.id
      ?? (selectedAreaFeature.formal_area_id && !selectedAreaFeature.formal_policy_status
        ? selectedAreaFeature.formal_area_id
        : null);
    if (selectedExistingArea && selectedExistingArea.area_code !== formalAreaCodeDraft.trim().toUpperCase()) {
      setLocationEditMessage("现有区域选择与正式区域编号不一致，请重新选择，系统不会按名称猜测绑定。");
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
          erp_area_code: formalAreaCodeDraft.trim().toUpperCase(),
          area_name: formalAreaNameDraft.trim() || selectedAreaFeature.name,
          existing_area_id: existingAreaId,
          ...selectedZonePolicy
        }
      );
      if (!response) return;
      if (selectedExistingArea && (!response.formal_area || Number(response.formal_area.id) !== Number(selectedExistingArea.id))) {
        setLocationEditMessage("服务器未能核验所选现有区域，草稿未在页面继续；请放弃草稿后重试。");
        return;
      }
      setLayout((current) => current ? {
        ...current,
        source_sha256: response.revision,
        features: current.features.map((item) => item.id === response.item.id ? response.item : item)
      } : current);
      rememberServerDraft(response.revision);
      setLayoutDraftControl((current) => current ? {
        ...current,
        has_draft: true,
        status: "draft",
        draft_revision: response.revision,
        updated_at: new Date().toISOString(),
        validated_at: null,
        blockers: [],
        warnings: []
      } : current);
      setZonePolicyDrafts((current) => {
        const next = { ...current };
        delete next[selectedAreaFeature.id];
        return next;
      });
      const savedAreaCode = formalAreaCodeDraft.trim().toUpperCase();
      setLocationEditMessage(
        response.formal_area
          ? `${savedAreaCode} 策略已保存为管理员草稿；正式员工地图与库存作业尚未改变。`
          : `${savedAreaCode} 策略已保存为管理员草稿；请先校验并发布建立正式区域，再规划库位。`
      );
    } catch (reason) {
      setLocationEditMessage(`保存区域策略失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const confirmSelectedAreaOnce = async () => {
    if (!layout || !selectedAreaFeature) return;
    const capacity = Number(simpleAreaCapacity);
    if (!Number.isInteger(capacity) || capacity < 0 || capacity > 500) {
      setLocationEditMessage("最大栈板数必须是 0 至 500 的整数；不放栈板的区域填写 0。");
      return;
    }
    if (!formalAreaCodeDraft.trim()) {
      setLocationEditMessage("当前地图区域缺少正式编号，无法确认启用。");
      return;
    }
    const selectedExistingArea = formalAreaOptions.find((item) => String(item.id) === selectedExistingAreaId);
    const existingAreaId = selectedExistingArea?.id
      ?? (selectedAreaFeature.formal_area_id && !selectedAreaFeature.formal_policy_status
        ? selectedAreaFeature.formal_area_id
        : null);
    if (selectedExistingArea && selectedExistingArea.area_code !== formalAreaCodeDraft.trim().toUpperCase()) {
      setLocationEditMessage("所选现有区域与地图编号不一致，请重新选择；系统不会按名称猜测绑定。");
      return;
    }
    const usageLabel = ({
      finished: "成品",
      semi_finished: "半成品",
      raw_material: "原材料",
      mold: "模具",
      print_plate: "印刷版",
      temporary_turnover: "临时周转"
    } as Record<InventoryUsage, string>)[simpleAreaUsage];
    const layoutLabel = simpleAreaLayout === "rack" ? "货架区" : "栈板区";
    if (!window.confirm(
      `确认启用 ${formalAreaCodeDraft.trim().toUpperCase()} ${formalAreaNameDraft.trim() || selectedAreaFeature.name}？\n\n` +
      `用途：${usageLabel}\n存储方式：${layoutLabel}\n最大栈板数：${capacity}\n\n` +
      "系统会自动保存、校验并启用该区域；不会移动库存、栈板或产品。"
    )) return;
    setSpatialEditBusy(true);
    setLocationEditMessage("正在确认并启用区域…");
    try {
      const result = await mutateJson<OneStepAreaConfirmResponse>(
        `/api/warehouse/twin-layout/floors/${floorCode}/zones/${selectedAreaFeature.id}/confirm-area`,
        "POST",
        {
          expected_revision: layout.source_sha256,
          expected_published_revision: planningPublishedRevision || layoutDraftControl?.published_revision || layout.source_sha256,
          expected_version: selectedAreaFeature.version,
          operation_key: operationKey("zone-one-step-confirm"),
          primary_inventory_type: simpleAreaUsage,
          storage_layout: simpleAreaLayout,
          max_pallet_capacity: capacity,
          erp_area_code: formalAreaCodeDraft.trim().toUpperCase(),
          area_name: formalAreaNameDraft.trim() || selectedAreaFeature.name,
          existing_area_id: existingAreaId,
          confirmed: true
        }
      );
      if (!result) return;
      setPlanningPublishedRevision(result.published_revision);
      await Promise.all([refreshPlanningTwinFloor(), refreshDashboard()]);
      setZonePolicyDrafts({});
      setZoneGeometryDrafts({});
      setSelectedExistingAreaId("");
      setLocationEditMessage(
        `${result.message}；区域启用状态已写入。当前没有货物时库存数量仍显示 0。` +
        (result.advanced_draft_preserved ? "原有高级维护草稿已保留，没有随本次确认发布。" : "")
      );
    } catch (reason) {
      setLocationEditMessage(`区域未启用：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const moveAreaBoundaryDraft = (id: string, deltaXmm: number, deltaYmm: number) => {
    if (!locationEditMode || !advancedAreaMaintenanceOpen || !areaPolicyEditMode) return;
    const feature = visualLayout?.features.find((item) => item.id === id);
    if (!feature || feature.feature_kind !== 'zone') return;
    setZoneGeometryDrafts((current) => ({
      ...current,
      [id]: translatePointsMm(feature.points, deltaXmm, deltaYmm)
    }));
  };

  const saveSelectedZoneGeometry = async () => {
    if (!layout || !selectedAreaFeature) return;
    const points = zoneGeometryDrafts[selectedAreaFeature.id];
    if (!points) return;
    setSpatialEditBusy(true);
    try {
      const response = await mutateJson<LayoutMutationResponse<TwinFeature>>(
        `/api/warehouse/twin-layout/floors/${floorCode}/zones/${selectedAreaFeature.id}/geometry`,
        'PATCH', { expected_revision: layout.source_sha256,
          expected_version: selectedAreaFeature.version,
          operation_key: operationKey('zone-geometry'), points }
      );
      if (!response) return;
      setLayout((current) => current ? { ...current, source_sha256: response.revision,
        features: current.features.map((item) => item.id === response.item.id ? response.item : item) } : current);
      rememberServerDraft(response.revision);
      setZoneGeometryDrafts((current) => { const next = { ...current }; delete next[selectedAreaFeature.id]; return next; });
      setLocationEditMessage('区域实测边界已保存到管理员草稿，发布前不会改变员工地图。');
    } catch (reason) { setLocationEditMessage(`保存区域边界失败：${(reason as Error).message}`); }
    finally { setSpatialEditBusy(false); }
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

  const switchWarehouseFloor = (nextFloorCode: "1F" | "3F") => {
    if (nextFloorCode !== floorCode && locationPointEditAreaCode) {
      setLocationEditMessage(`正在调整 ${locationPointEditAreaCode} 区货位；请先保存并固定或取消点位调整，再切换楼层。`);
      return;
    }
    setFloorCode(nextFloorCode);
    if (mapMode !== "move" || !moveSource) return;
    setMoveTargetFloorCode(nextFloorCode);
    setMoveTargetAreaCode("");
    setMoveDraftTargetLocationId("");
    setWarehouseOperationMessage(`来源 ${moveSource.inventory_code} 仍保留；请在 ${nextFloorCode} 地图点选区域，再点具体空货位。`);
  };

  return <main className={`warehouse-twin-shell ${uiMode === "large" ? "large-text" : ""} ${mapMode === "move" ? "move-mode" : ""}`}>
    {focusedRack?.mold_rack_code ? <MoldRackElevation
      rack={focusedRack}
      response={moldRackResponse}
      loading={moldRackLoading}
      error={moldRackError}
      rackIndex={focusedRackIndex}
      rackCount={focusedAreaRacks.length || 1}
      onPrevious={() => switchFocusedRack(-1)}
      onNext={() => switchFocusedRack(1)}
      onClose={() => setRackFocusId(null)}
    /> : focusedRack && <WarehouseRackElevation
      rack={focusedRack}
      areaCode={focusedRackAreaCode}
      area={focusedRackAreaCode ? areaStats.get(focusedRackAreaCode) : undefined}
      items={rackInventoryItems}
      emptyLocations={focusedRackEmptyLocations}
      canChooseProducts={P1_47D_ENABLED && canEditLocations && mapMode === "move"}
      rackIndex={focusedRackIndex}
      rackCount={focusedAreaRacks.length || 1}
      onPrevious={() => switchFocusedRack(-1)}
      onNext={() => switchFocusedRack(1)}
      onChooseEmptyLocation={chooseRackEmptyLocation}
      onClose={() => setRackFocusId(null)}
    />}
    <header className={`twin-command-bar ${embedded ? "embedded" : ""}`}>
      <div className="twin-brand"><div><small>TIANMING WAREHOUSE</small><h1>天明智慧仓储</h1></div><nav className="twin-floor-switch" aria-label="楼层切换">
        <button type="button" className={floorCode === "1F" ? "active" : ""} onClick={() => switchWarehouseFloor("1F")}><b>1F</b><span>生产车间</span></button>
        <button type="button" className={floorCode === "3F" ? "active" : ""} onClick={() => switchWarehouseFloor("3F")}><b>3F</b><span>成品仓库</span></button>
      </nav>{selectedAreaCode && <div className="twin-header-area-summary"><small>当前区域</small><b>{selectedAreaCode} · {selectedAreaFeature?.name || "仓储区域"}</b><span>{selectedAreaFeature?.area_mm2 ? `${(selectedAreaFeature.area_mm2 / 1_000_000).toFixed(1)} m²` : "面积待确认"} · {selectedAreaLocationCount} 库位 · {selectedArea?.lot_count || 0} 批次</span></div>}<p>{floorTitle} · 正式仓库作业层</p></div>
      <div className="twin-command-status"><span className="live">{mapMode === "planning" ? locationPointEditAreaCode ? `区域规划 · ${locationPointEditAreaCode} 点位调整` : advancedAreaMaintenanceOpen ? "区域规划 · 高级维护" : "区域规划 · 一次确认" : mapMode === "move" ? moveAction === "merge" ? `移货 · 合并栈板 · ${mergeSources.length} 块已选` : `移货 · ${moveDrafts.length} 条页面草稿` : "查货模式 · 只读"}</span><b>{currentFloor?.active_lots || 0}</b><small>当前层有效批次</small></div>
      <a className="twin-ledger-link" href="/warehouse-ledger.html?tab=finished" target="_top">库存台账</a>
    </header>

    <section className="twin-toolbar">
      <div className="twin-operation-modes" role="tablist" aria-label="仓库地图操作模式">
        <button type="button" className={mapMode === "lookup" ? "active" : ""} onClick={returnToLookupMode}>查货</button>
        {canExecuteWarehouse && <button type="button" className={mapMode === "move" ? "active" : ""} disabled={spatialEditBusy} onClick={enterWarehouseMoveMode}>移货 / 盘点</button>}
        {canEditLocations && <button type="button" className={mapMode === 'planning' ? 'active' : ''} disabled={spatialEditBusy} onClick={toggleLayoutEditor}>区域规划</button>}
      </div>
      <button type="button" className={`twin-layer-toggle ${layerPanelOpen ? "active" : ""}`} aria-expanded={layerPanelOpen} onClick={() => setLayerPanelOpen((value) => !value)}>图层</button>
      <div className="twin-segmented" aria-label="视图模式">
        <button type="button" className={viewMode === "2d" ? "active" : ""} onClick={() => setViewMode("2d")}>二维平面</button>
        <button type="button" className={viewMode === "25d" ? "active" : ""} disabled={locationEditMode || mapMode === "move"} title={mapMode === "move" ? "移货 / 盘点使用二维地图；页面草稿不会丢失" : locationEditMode ? "请先退出、发布或放弃布局草稿" : ""} onClick={() => setViewMode("25d")}>2.5D 等距</button>
      </div>
      {viewMode === "25d" && <div className="twin-camera-presets">
        <button type="button" onClick={() => setCameraPreset("north_east")}>东北</button>
        <button type="button" onClick={() => setCameraPreset("north_west")}>西北</button>
        <button type="button" onClick={() => setCameraPreset("south_east")}>东南</button>
        <button type="button" onClick={() => setCameraPreset("south_west")}>西南</button>
      </div>}
      <button type="button" className="twin-reset" onClick={() => { setCameraPreset("fit"); setViewResetToken((value) => value + 1); }}>全图复位</button>
      <button type="button" className={`twin-warehouse-search-toggle ${searchPanelOpen || searchResponse ? "active" : ""}`} aria-expanded={searchPanelOpen} onClick={() => setSearchPanelOpen((value) => !value)}>全仓查找{searchResponse ? ` ${searchType === "finished" ? searchProductGroups.length : searchResponse.resource_result_count}` : ""}</button>
      {mapMode === "planning" && (viewMode === "2d" ? <button type="button" className={`twin-location-edit-toggle ${locationEditMode ? "active" : ""}`} disabled={!canEditLocations || spatialEditBusy} title={!canEditLocations ? "仅管理员可以规划区域" : "二维编辑先选择地图区域，再一次确认用途、形式和容量"} onClick={toggleLayoutEditor}>{locationEditMode ? "退出规划" : "开始规划"}</button> : <span className="twin-view-note">2.5D 流畅查看 · 详情见右侧</span>)}
      {mapMode === "planning" && floorCode === "1F" && viewMode === "2d" && canEditLocations && !locationEditMode && <button type="button" className={`twin-floor1-candidate-toggle ${floor1CandidatePlan ? "active" : ""}`} disabled={floor1CandidateBusy} onClick={previewFloor1FormalCandidates}>{floor1CandidateBusy ? "正在测算…" : "一楼区域自动生成"}</button>}
      {mapMode === "planning" && locationEditMode && (advancedAreaMaintenanceOpen || locationPointEditAreaCode) && <><button type="button" className="twin-save-location-layout" disabled={locationEditBusy || !activeLocationDraftCount} onClick={saveLocationDrafts}>{locationPointEditAreaCode ? "保存并固定" : "保存库位位置"} {activeLocationDraftCount || ""}</button><button type="button" className="twin-cancel-location-layout" disabled={locationEditBusy || (advancedAreaMaintenanceOpen && !activeLocationDraftCount)} onClick={locationPointEditAreaCode ? cancelLocationPointEditing : () => { setLocationDrafts({}); setSwapSourceLocationId(null); setLocationEditMessage("已取消未保存的库位位置草稿。"); }}>{locationPointEditAreaCode ? "取消点位调整" : "取消位置草稿"}</button></>}
      {mapMode === "planning" && locationEditMode && advancedAreaMaintenanceOpen && <div className="twin-layout-draft-workflow">
        <span className={`status ${layoutDraftControl?.status || "none"}`}>{layoutDraftControl?.status === "validated" ? "草稿已校验" : layoutDraftControl?.has_draft ? "草稿未发布" : "尚无草稿"}</span>
        <button type="button" disabled={spatialEditBusy || !layoutDraftControl?.has_draft} onClick={validateLayoutDraft}>校验草稿</button>
        <button type="button" className="publish" disabled={spatialEditBusy || layoutDraftControl?.status !== "validated"} onClick={publishLayoutDraft}>发布布局</button>
        <button type="button" disabled={spatialEditBusy} onClick={discardLayoutDraft}>{layoutDraftControl?.has_draft ? "放弃草稿" : "取消编辑"}</button>
      </div>}
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
        <div className="twin-rail-safety"><b>数据边界</b><p>{mapMode === "planning" ? "一次确认只建立区域用途、形式和容量；不会移动库存、栈板或产品。高级维护仅在实测边界、货架或库位确需调整时使用。" : mapMode === "move" ? moveAction === "merge" ? "多选只形成页面草稿；不建档、不入仓、不增减、不盘点。明确目标后底部一次提交整批合并。" : "拖动和选择只形成页面草稿；不提供建档、入仓、增减、移除或盘点。底部一次确认后才提交整批移货。" : "当前是查货模式，只读真实库存和地图位置，不执行入库、移货、盘点或布局写入。"}</p></div>
      </aside>}

      {searchPanelOpen && <aside className="twin-context-rail">
        <header><small>WAREHOUSE SEARCH</small><h2>全仓查找</h2></header>
        <section className="twin-global-search">
          <div className="twin-context-heading"><b>统一查货</b>{search && <button type="button" onClick={() => { setSearch(""); setSearchResponse(null); setSearchError(""); setFocusedSearchItem(null); setFocusedSearchProductKey(null); setFocusedResource(null); setCameraFocusTarget(null); setAreaInventorySearch(""); }}>清除</button>}</div>
          <div className="twin-search-type-grid" role="tablist" aria-label="全仓查找类型">
            <button type="button" className={searchType === "finished" ? "active" : ""} onClick={() => { setSearchType("finished"); setSearch(""); setSearchResponse(null); setFocusedResource(null); setFocusedSearchProductKey(null); }}>纸箱成品</button>
            <button type="button" className={searchType === "mold" ? "active" : ""} onClick={() => { setSearchType("mold"); setSearchResponse(null); setFocusedSearchItem(null); setFocusedSearchProductKey(null); }}>模具</button>
            <button type="button" className={searchType === "printing_plate" ? "active" : ""} onClick={() => { setSearchType("printing_plate"); setSearchResponse(null); setFocusedSearchItem(null); setFocusedSearchProductKey(null); }}>印刷版</button>
          </div>
          {searchType === "finished" ? <>
            <label className="twin-search-step"><span>客户、简写、存货编码、产品名称或规格</span><input value={search} onChange={(event) => { setSearch(event.target.value); setFocusedSearchProductKey(null); }} placeholder="例如：天华、TH、TM-FG、加强纸箱、520×350×300" autoFocus /></label>
            <small>{search.trim().length < 2 ? "输入任意 2 个字符即可查找，不必先记住存货编码。" : searchLoading ? "正在读取有权限的真实库存…" : searchResponse ? `匹配 ${searchProductGroups.length} 个产品 · ${searchResponse.inventory_result_count} 个真实位置批次` : "等待查找结果"}</small>
          </> : <>
            <label className="twin-search-step"><span>{searchType === "mold" ? "模具编码或名称" : "印刷版编码、产品或位置"}</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={searchType === "mold" ? "输入模具编码或名称" : "输入印刷版、产品或区域"} autoFocus /></label>
            <small>{search.trim().length < 2 ? "至少输入 2 个字符" : searchResponse ? `${searchResponse.resource_result_count} 条真实定位结果` : "正在查找…"}</small>
          </>}
          {searchError && <div className="twin-search-error"><b>查货失败</b><span>{searchError}</span><button type="button" onClick={() => setSearchRetryToken((value) => value + 1)}>重试</button></div>}
          {searchResponse && <div className="twin-search-result-list">
            {searchType === "finished" && searchProductGroups.slice(0, 80).map((group) => <button type="button" className={focusedSearchProductKey === group.key ? "selected product-selected" : ""} key={group.key} onClick={() => focusSearchProduct(group)}><b>{group.inventory_code}</b><strong>{group.product_name}</strong><span>{group.customer_name}{group.specification ? ` · ${group.specification}` : ""}</span><small><em>{formatNumber(group.total_quantity)} {inventoryUnitLabel(group.unit)}</em> · {group.location_count} 个实际位置</small><small className="twin-search-floor-line">{group.floor_summaries.map((floor) => `${floor.floor_code === "UNLOCATED" ? "待定位" : floor.floor_code} ${formatNumber(floor.quantity)} ${inventoryUnitLabel(group.unit)} / ${floor.location_count}处`).join(" · ")}</small></button>)}
            {searchType !== "finished" && searchResponse.resources.slice(0, 60).map((item) => <button type="button" className={focusedResource?.resource_id === item.resource_id ? "selected" : ""} key={item.resource_id} onClick={() => focusLocateResource(item)}><b>{item.primary_code || item.location_code || "功能区域"}</b><strong>{item.title}</strong><span>{item.subtitle}</span><small>{item.floor_code === "TEXT" ? "文字位置" : item.floor_code} · {item.map_status === "mapped" ? "点击定位到地图" : "仅有文字位置"}</small></button>)}
            {searchType === "finished" && !searchProductGroups.length && <div className="twin-area-empty"><b>没有匹配的成品库存</b><span>可改用客户全称/简写、存货编码片段、产品名称或规格。</span></div>}
            {searchType !== "finished" && !searchResponse.resources.length && <div className="twin-area-empty"><b>没有匹配结果</b><span>请更换编码、名称或区域关键词。</span></div>}
          </div>}
          {focusedSearchProduct && <div className="twin-search-focus-note product-focus"><b>全部真实位置已选中</b><span>{focusedSearchProduct.customer_name} · {focusedSearchProduct.inventory_code} · 共 {formatNumber(focusedSearchProduct.total_quantity)} {inventoryUnitLabel(focusedSearchProduct.unit)} · {focusedSearchProduct.location_count} 个位置</span><div className="twin-search-floor-actions">{focusedSearchProduct.floor_summaries.map((floor) => <button type="button" key={floor.floor_code} disabled={!(["1F", "3F"].includes(floor.floor_code))} className={floorCode === floor.floor_code ? "active" : ""} onClick={() => focusSearchFloor(focusedSearchProduct, floor.floor_code)}>{floor.floor_code === "UNLOCATED" ? "待定位" : floor.floor_code} · {formatNumber(floor.quantity)} {inventoryUnitLabel(focusedSearchProduct.unit)} · {floor.location_count}处</button>)}</div><div className="twin-search-location-list">{focusedSearchProduct.location_summaries.map((location) => <button type="button" key={location.key} disabled={!(["1F", "3F"].includes(location.floor_code))} onClick={() => focusSearchLocation(focusedSearchProduct, location)}><b>{location.floor_code === "UNLOCATED" ? "待定位" : location.floor_code} · {location.area_code || "区域待确认"}</b><span>{location.location_name}</span><em>{formatNumber(location.quantity)} {inventoryUnitLabel(focusedSearchProduct.unit)} · {location.position_status === "mapped" ? "地图可定位" : "真实文字位置"}</em></button>)}</div></div>}
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
          onMovePallet={warehouseMoveModeActive ? draftMoveFromDrag : moveLocationDraft}
          onMoveFeature={moveAreaBoundaryDraft}
          onDropAsset={noop}
          onDropRack={noop}
          onDropPallet={noop}
          onDrawPoint={noop}
          onMeasurePoint={noop}
          productionProjections={productionProjection?.items || EMPTY_PRODUCTION_PROJECTIONS}
          highlightFeatureIds={searchHighlightFeatureIds}
          highlightedPalletIds={searchHighlightPalletIds}
          focusTarget={cameraFocusTarget}
          palletEditingOnly={(locationEditMode && (advancedAreaMaintenanceOpen || Boolean(locationPointEditAreaCode))) || warehouseMoveModeActive}
          rackEditingEnabled={locationEditMode && advancedAreaMaintenanceOpen}
          featureEditingEnabled={locationEditMode && advancedAreaMaintenanceOpen && areaPolicyEditMode}
          allowPalletSelection={viewMode === "25d" || locationEditMode || canEditLocations || canExecuteWarehouse}
          draggablePalletIds={warehouseMoveModeActive ? movablePalletIds : locationPointEditPalletIds}
          readOnly={(!locationEditMode || (!advancedAreaMaintenanceOpen && !locationPointEditAreaCode)) && !warehouseMoveModeActive}
          visualTheme="warehouse"
        />}
      </div>

      <aside className="twin-inspector">
        <header><small>ERP INVENTORY</small><h2>库存与库位</h2></header>
        {floor1CandidatePlan && <section className="twin-floor1-candidate-panel">
          <div className="twin-floor1-candidate-title"><div><small>1F · 实测地图候选</small><b>一次确认区域与库位</b></div><button type="button" disabled={floor1CandidateBusy} onClick={() => setFloor1CandidatePlan(null)}>关闭</button></div>
          <div className="twin-floor1-candidate-summary"><span><b>{floor1CandidatePlan.candidate_count}</b> 个区域</span><span><b>{floor1CandidatePlan.long_term_pallet_capacity}</b> 个长期栈板位</span><span><b>{floor1CandidatePlan.formal_location_count}</b> 个正式库存库位</span></div>
          <p>按 {floor1CandidatePlan.standard_pallet_mm.width}×{floor1CandidatePlan.standard_pallet_mm.depth}mm 标准栈板和已发布毫米坐标测算，已避开通道、设备、货架、柱子和禁放区；确认前不会写正式台账。</p>
          {floor1CandidatePlan.excluded_out_of_bounds_count > 0 && <p className="twin-floor1-candidate-warning">已排除 {floor1CandidatePlan.excluded_out_of_bounds_count} 个实测边界外台账记录：{floor1CandidatePlan.excluded_out_of_bounds.map((item) => item.feature_code).join("、")}。这些记录不显示、不计容量，也不会生成正式区域或库位。</p>}
          {floor1CandidatePlan.formal_state.archivable_legacy_area_count > 0 && <p className="twin-floor1-candidate-warning">将停用 {floor1CandidatePlan.formal_state.archivable_legacy_area_count} 个无库存、无栈板且未映射的重复空台账记录：{floor1CandidatePlan.formal_state.legacy_areas.filter((item) => item.archive_required).map((item) => item.area_code).join("、")}。真实实测区域不受影响。</p>}
          {floor1CandidatePlan.formal_state.blocking_items.length > 0 && <div className="twin-floor1-candidate-blockers"><b>当前不能确认</b>{floor1CandidatePlan.formal_state.blocking_items.map((item) => <article key={`${item.code}-${item.area_id || item.area_code || item.message}`}><div><span>{item.message}</span>{floor1CandidateBlockerDetail(item) && <small>{floor1CandidateBlockerDetail(item)}</small>}</div><a href={floor1CandidateBlockerHref(item)} target="_blank" rel="noreferrer">{item.action_label}</a></article>)}<button type="button" className="twin-floor1-candidate-recheck" disabled={floor1CandidateBusy} onClick={previewFloor1FormalCandidates}>{floor1CandidateBusy ? "正在重新检查…" : "处理完成，重新检查"}</button></div>}
          <div className="twin-floor1-candidate-list">{floor1CandidatePlan.candidates.map((item) => <article key={item.map_feature_id} className={item.long_term_capacity_eligible ? "eligible" : "excluded"}><div><b>{item.area_code}</b><span>{item.area_name}</span></div><strong>{item.planned_pallet_capacity ? `${item.planned_pallet_capacity} 个长期栈板位` : "不计长期容量"}</strong><small>{item.formal_location_count ? `生成 ${item.formal_location_count} 个正式库位 · ` : ""}{item.capacity_note}</small></article>)}</div>
          <button type="button" className="twin-primary-action" disabled={floor1CandidateBusy || floor1CandidatePlan.formal_state.blocking_conflicts.length > 0} onClick={confirmFloor1FormalCandidates}>{floor1CandidateBusy ? "正在确认…" : floor1CandidatePlan.formal_state.already_applied ? "已确认，无需重复生成" : "一次确认并启用"}</button>
          <small>确认只写区域、容量、位置与审计记录，不改变库存数量、栈板内容、订单或生产数据。</small>
        </section>}
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
              {selectedProductionTask && layout && mapMode === "planning" && <div className="twin-production-bind">
              <b>{selectedProductionTask.order_number} · ERP只读</b><small>{selectedProductionTask.customer_name} / {selectedProductionTask.product_name}</small>
              <label>定位对象<select value={productionTargetKind} onChange={(event) => { const kind = event.target.value as "pallet" | "zone"; setProductionTargetKind(kind); setProductionTargetId(kind === "pallet" ? layout.pallets[0]?.id || "" : layout.features.find((item) => item.feature_kind === "zone")?.id || ""); }}><option value="pallet">现有栈板</option><option value="zone">现有区域</option></select></label>
              <label>人工选择<select value={productionTargetId} onChange={(event) => setProductionTargetId(event.target.value)}>{productionTargetKind === "pallet" ? layout.pallets.map((item) => <option key={item.id} value={item.id}>{item.pallet_code} · {item.zone_code}</option>) : layout.features.filter((item) => item.feature_kind === "zone").map((item) => <option key={item.id} value={item.id}>{item.feature_code} · {item.name}</option>)}</select></label>
              <button type="button" className="twin-primary-action" disabled={productionBusy || !productionTargetId} onClick={saveProductionMapping}>{selectedProductionTask.mapping ? "更新人工定位" : "确认投影到地图"}</button>
              {selectedProductionTask.mapping && <button type="button" className="twin-production-remove" disabled={productionBusy} onClick={removeProductionMapping}>移回待定位</button>}
              <small>只保存隔离地图库的任务ID与位置关系。</small>
            </div>}
          </div>}
        </section>}
        {locationEditMessage && <div className={`twin-location-message ${locationEditMessage.includes("失败") || locationEditMessage.includes("缺失") ? "error" : ""}`}><span>{locationEditMessage}</span>{(locationEditMessage.includes("先完成移货") || locationEditMessage.includes("移到其他已启用区域")) && <button type="button" onClick={() => { setLocationEditMode(false); setMapMode("move"); setMoveAction("relocate"); setWarehouseOperationMessage("请点选当前区域内的货物或实体栈板，再切换楼层并选择目标位置；提交前不会改动库存。"); }}>前往移货</button>}</div>}
        {canExecuteWarehouse && mapMode === "move" && <section className="twin-move-control-panel">
          <div className="twin-formal-operation-title"><b>{moveAction === "merge" ? "多栈合并草稿" : "移货页面草稿"}</b><span>楼层切换不丢来源与草稿</span></div>
          {P1_49C_ENABLED && <div className="twin-move-action-tabs" role="tablist" aria-label="移货操作类型">
            <button type="button" role="tab" aria-selected={moveAction === "relocate"} className={moveAction === "relocate" ? "active" : ""} onClick={() => { setMoveAction("relocate"); setWarehouseOperationMessage(moveDrafts.length ? `已切回移动位置；保留 ${moveDrafts.length} 条移货草稿。` : "已切回移动位置。"); }}>移动位置</button>
            <button type="button" role="tab" aria-selected={moveAction === "merge"} className={moveAction === "merge" ? "active" : ""} onClick={() => { setMoveAction("merge"); setMoveSource(null); setWarehouseOperationMessage(mergeSources.length ? `已切到合并栈板；保留 ${mergeSources.length} 块来源。` : "请从真实位置逐块选择至少两块兼容系统栈板。"); }}>合并栈板</button>
          </div>}
          {moveAction === "merge" ? <>
            <p>合并集合已选 {mergeSources.length} 块；可切楼层和位置继续选择，再从集合内明确一块目标。合并不拆批次、不改数量，失败会保留本页选择与重试键。</p>
            {mergeSources.length > 0 && <div className="twin-merge-source-chips">{mergeSources.map((item) => <button type="button" key={item.pallet_id} disabled={mergeBatchBusy} onClick={() => {
              const result = togglePalletMergeSource(mergeSources, item);
              setMergeSources(result.items);
              if (mergeTarget?.pallet_id === item.pallet_id) setMergeTarget(null);
              setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
              setWarehouseOperationMessage(`已移除来源 ${item.pallet_code}；正式库存未改变。`);
            }}><b>{item.pallet_code}</b><span>{item.floor_code} / {item.location_name}</span><em>移除</em></button>)}</div>}
            {mergeSources.length < 2 ? <small className="twin-merge-compatibility-note">还需选择 {2 - mergeSources.length} 块兼容系统栈板；选满两块后从集合中明确指定目标。</small> : <div className="twin-move-target-cascade">
              <div className="twin-formal-operation-title"><b>明确选择目标栈板</b><span>目标必须是上方已选集合中的一块</span></div>
              <div className="twin-merge-target-list" role="radiogroup" aria-label="目标系统栈板">{mergeTargetChoices.map((item) => <button type="button" role="radio" aria-checked={mergeTarget?.pallet_id === item.pallet_id} className={mergeTarget?.pallet_id === item.pallet_id ? "selected" : ""} key={item.pallet_id} onClick={() => chooseMergeTarget(item)}><b>{item.pallet_code}</b><span>{item.floor_code} / {item.location_name} · {item.customer_name}</span><small>合计 {formatNumber(item.total_quantity)} {inventoryUnitLabel(item.unit)} · 设为目标后其余 {mergeSources.length - 1} 块作为来源</small></button>)}</div>
              {!mergeTargetChoices.length && <small className="error">已选集合中没有通过位置门禁的目标栈板；请移除不可作为目标的栈板后重选。</small>}
            </div>}
          </> : !moveSource ? <p>先点地图上的真实栈板；厂外待送区每个栈板图案都可单独选择，历史散存也可从右侧选择。</p> : <>
            <div className="twin-move-source-summary"><small>已选货物 · {moveSource.operation === "pallet_move" ? "整栈板" : "库存批次"}</small><b>{moveSource.inventory_code} · {moveSource.product_name}</b><span>{moveSource.source_floor_code} / {moveSource.source_area_code || "未分区"} / {moveSource.source_location_name}</span><button type="button" onClick={() => { setMoveSource(null); setMoveQuantity(""); setMoveDraftTargetLocationId(""); setWarehouseOperationMessage("已清除本次页面选择；库存、入库来源和送货单均未改变。"); }}>重新选择货物</button></div>
            <div className="twin-move-target-cascade">
              <div className="twin-formal-operation-title"><b>在地图选择目标空货位</b><span>可切换 1F / 3F；来源不会丢失</span></div>
              {moveSource.operation === "lot_transfer" && <label><span>本次移动数量（可用＋预占，损坏不计）</span><input type="number" min="1" max={moveSource.max_quantity} step="1" value={moveQuantity} onChange={(event) => setMoveQuantity(event.target.value)} /></label>}
              <label><span>楼层（选择后同步切换地图）</span><select value={moveTargetFloorCode} onChange={(event) => {
                const nextFloor = event.target.value;
                if (nextFloor === "1F" || nextFloor === "3F") switchWarehouseFloor(nextFloor);
                else { setMoveTargetFloorCode(""); setMoveTargetAreaCode(""); setMoveDraftTargetLocationId(""); }
              }}><option value="">请选择楼层</option>{moveTargetFloors.map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
              <label><span>区域（也可直接点地图区域）</span><select value={moveTargetAreaCode} disabled={!moveTargetFloorCode} onChange={(event) => { setMoveTargetAreaCode(event.target.value); setMoveDraftTargetLocationId(""); }}><option value="">请选择区域</option>{moveTargetAreas.map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
              <label><span>具体货位（也可直接点地图空位）</span><select value={moveDraftTargetLocationId} disabled={!moveTargetAreaCode} onChange={(event) => setMoveDraftTargetLocationId(event.target.value)}><option value="">请选择可用空货位</option>{moveTargetLocations.map((item) => <option value={item.location_id} key={item.location_id}>{item.location_name} · {item.location_code}</option>)}</select></label>
              {moveCandidatesLoading && <small>正在读取可用空货位…</small>}
              {moveCandidatesError && <small className="error">空货位读取失败：{moveCandidatesError}</small>}
              {!moveCandidatesLoading && !moveCandidatesError && mappedMoveTargets.length === 0 && <small>当前没有可用空货位；请先确认目标区域已启用并设有空货位。</small>}
              <button type="button" className="twin-primary-action" disabled={!selectedMoveTarget || moveBatchBusy} onClick={addSelectedMoveDraft}>加入页面草稿</button>
            </div>
          </>}
        </section>}
        {locationEditMode && advancedAreaMaintenanceOpen && selectedRackEditDraft && <section className="twin-rack-layout-editor">
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
          <label><span>货架层数</span><input type="number" min="1" max="20" value={selectedRackEditDraft.levels} onChange={(event) => changeRackLevels(selectedRackEditDraft, Number(event.target.value))} /></label>
          <div className="twin-rack-level-editor"><b>逐层设置（修改净高会自动合计总高度）</b>{selectedRackEditDraft.level_clear_heights_mm.map((height, index) => <div className="twin-rack-level-row" key={`${selectedRackEditDraft.id}-level-${index}`}><label><span>第 {index + 1} 层净高 mm</span><input type="number" min="1" value={height} onChange={(event) => changeRackLevelHeight(selectedRackEditDraft, index, Number(event.target.value))} /></label><label><span>第 {index + 1} 层格数</span><input type="number" min="0" max="50" value={selectedRackEditDraft.level_cell_counts[index]} onChange={(event) => changeRackLevelCellCount(selectedRackEditDraft, index, Number(event.target.value))} /></label></div>)}<small>格数填 0 表示本层尚未分格；地图发布后同步为模具台账逐层格位，不记录格内左右顺序。</small></div>
          <div className="twin-rack-coordinate-grid">
            <label><span>正面操作方向</span><select value={selectedRackEditDraft.access_side} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { access_side: event.target.value as Rack["access_side"] })}><option value="north">北</option><option value="south">南</option><option value="east">东</option><option value="west">西</option><option value="both">双面</option></select></label>
            <label><span>最小通道 mm</span><input type="number" min="0" value={selectedRackEditDraft.min_aisle_width_mm} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { min_aisle_width_mm: Number(event.target.value) })} /></label>
          </div>
          <div className="twin-layout-editor-actions"><button type="button" onClick={() => updateRackDraft(selectedRackEditDraft.id, { rotation_deg: ((selectedRackEditDraft.rotation_deg + 90) % 360) as Rack["rotation_deg"] })}>旋转 90°</button><button type="button" className="primary" disabled={spatialEditBusy} onClick={saveSelectedRack}>保存到草稿</button><button type="button" disabled={spatialEditBusy} onClick={() => setRackDrafts((current) => { const next = { ...current }; delete next[selectedRackEditDraft.id]; return next; })}>取消本次修改</button><button type="button" className="danger" disabled={spatialEditBusy || selectedRackEditDraft.is_locked} onClick={deleteSelectedRack}>从草稿删除</button></div>
          <p>保存后仍是管理员草稿；校验并发布前，员工地图、库存数量、栈板和正式库位均不改变。</p>
        </section>}
        {selectedDispatchPallet && dispatchStagingLocation && <section className="twin-location-card twin-dispatch-board">
          {(() => {
            const summary = dashboardPalletSummary(selectedDispatchPallet);
            return <>
              <div className="twin-location-card-title"><div><small>厂外待送 · 真实系统栈板</small><b>{selectedDispatchPallet.pallet_code}</b></div><em className="occupied">待送货</em></div>
              <div className="twin-selection-summary"><span><small>产品</small><b>{summary.product}</b></span><span><small>客户</small><b>{summary.customer}</b></span><span><small>数量</small><b>{formatNumber(summary.quantity)} {inventoryUnitLabel(summary.unit)}</b></span></div>
              <p className="twin-dispatch-weather-note">地图上的这一块图案对应这一块真实栈板；位置点只表示它位于实测厂外待送区，不伪造未登记的区内坐标。</p>
              {canExecuteWarehouse && <button type="button" className="twin-primary-action" onClick={async () => {
                if (mapMode !== "move") await enterWarehouseMoveMode();
                chooseMoveSource(palletMoveSource(dispatchStagingLocation, selectedDispatchPallet));
              }}>移动这块栈板到 1F / 3F</button>}
            </>;
          })()}
        </section>}
        {selectedFeatureIsMeasuredDispatch && <section className="twin-location-card twin-dispatch-board">
          <div className="twin-location-card-title"><div><small>1F MEASURED DISPATCH</small><b>{selectedFeature?.name || "一楼成品待送区"}</b></div><em className="occupied">临时待装车</em></div>
          <p className="twin-dispatch-weather-note">只使用当前实测地图内已经确认的待送区域轮廓；不向图外补画或扩展区域。</p>
          <div className="twin-dispatch-summary"><b>{dispatchStagingPallets.length}</b><span>块真实待送栈板</span><small>点击栈板可实时查看产品、客户和数量；转走后该区数量同步减少。另有 {dispatchStagingItems.length} 条未绑定实体栈板的历史散存。</small></div>
          <div className="twin-dispatch-label-list">
            {dispatchStagingPallets.map((pallet) => {
              const summary = dashboardPalletSummary(pallet);
              return <button type="button" className={(mapMode === "move" ? moveSource?.source_key === `pallet:${pallet.pallet_id}` : selectedDispatchPallet?.pallet_id === pallet.pallet_id) ? "selected" : ""} key={`dispatch-pallet-${pallet.pallet_id}`} onClick={() => {
                setSelected({ kind: "pallet", id: `erp-dispatch-pallet-${pallet.pallet_id}` });
                if (mapMode === "move" && canExecuteWarehouse && dispatchStagingLocation) chooseMoveSource(palletMoveSource(dispatchStagingLocation, pallet));
              }}>
                <small>真实栈板 · {pallet.pallet_code}</small>
                <b>{formatNumber(summary.quantity)} {inventoryUnitLabel(summary.unit)}</b>
                <strong>{summary.product}</strong>
                <span>{summary.customer}</span>
              </button>;
            })}
            {dispatchStagingItems.map((item, itemIndex) => <button type="button" className={(mapMode === "move" ? moveSource?.source_key === `lot:${item.lot_id}` : dispatchTransferLotId === item.lot_id) ? "selected" : ""} key={item.lot_id || `dispatch-${itemIndex}`} onClick={() => {
              if (mapMode === "move" && canExecuteWarehouse && dispatchStagingLocation) {
                chooseMoveSource(lotMoveSource(dispatchStagingLocation, item));
                return;
              }
              setDispatchTransferLotId(item.lot_id); setDispatchTransferQuantity(String(inventoryLabelQuantity(item))); setDispatchTransferTargetId(""); setDispatchTransferIdempotencyKey(operationKey("dispatch-to-floor3")); setWarehouseOperationMessage("");
            }}>
              <small>散存待送 · 未绑定实体栈板</small>
              <b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b>
              <strong>{item.product_name || "产品名称待补充"}</strong>
              <span>{item.customer_name || "客户待确认"}</span>
              <em>{formatNumber(inventoryLabelQuantity(item))} {inventoryUnitLabel(item.unit)}</em>
            </button>)}
            {dispatchStagingPallets.length === 0 && dispatchStagingItems.length === 0 && <p>当前待送区没有货物。</p>}
          </div>
          {mapMode === "move" && canExecuteWarehouse && <p className="twin-dispatch-weather-note">点击上方栈板后，可随意切换 1F / 3F 地图；来源不会丢失，再点区域和具体空货位即可移动，不会改变订单和后续送货关系。</p>}
          {P1_47D_ENABLED && selectedDispatchStagingItem && canEditLocations && mapMode === "move" && viewMode === "2d" && <div className="twin-formal-operation twin-dispatch-transfer">
            <div className="twin-formal-operation-title"><b>暂不送，转三楼成品区</b><span>只移动原库存</span></div>
            <label><span>本次转入数量（默认全部）</span><input type="number" min="1" max={inventoryLabelQuantity(selectedDispatchStagingItem)} step="1" value={dispatchTransferQuantity} onChange={(event) => setDispatchTransferQuantity(event.target.value)} /></label>
            <span className="twin-map-target-title">直接点选三楼空位缩略图</span>
            <div className="twin-map-target-grid" role="listbox" aria-label="散存待送转三楼目标">
              {emptyMoveTargets.map((item) => <button type="button" role="option" aria-selected={dispatchTransferTargetId === String(item.location_id)} className={dispatchTransferTargetId === String(item.location_id) ? "selected" : ""} key={item.location_id} onClick={() => { setDispatchTransferTargetId(String(item.location_id)); setDispatchTransferIdempotencyKey(operationKey("dispatch-to-floor3")); }}>
                <span className="twin-map-target-thumbnail"><i style={{ left: `${Math.max(4, Math.min(88, Number(item.map_position?.left_pct || 50)))}%`, top: `${Math.max(4, Math.min(82, Number(item.map_position?.top_pct || 50)))}%` }} /></span>
                <b>{item.location_name}</b><small>{item.area_code} · 当前空位</small>
              </button>)}
              {emptyMoveTargets.length === 0 && <p>三楼当前没有已发布、已放置的成品空位。</p>}
            </div>
            <button type="button" className="twin-primary-action" disabled={warehouseOperationBusy || !dispatchTransferQuantity || !dispatchTransferTargetId} onClick={confirmDispatchStagingTransfer}>确认转入点选位置</button>
          </div>}
          {warehouseOperationMessage && <div className="twin-location-message">{warehouseOperationMessage}</div>}
        </section>}
        {selectedLocation && <section className="twin-location-card">
          <div className="twin-location-card-title"><div><small>当前位置 · {selectedLocation.location_code}</small><b>{selectedLocation.location_name}</b></div><em className={selectedLocation.occupancy_status}>{selectedLocation.occupancy_status === "occupied" ? "有货" : "空位"}</em></div>
          <div className="twin-selection-summary"><span><small>货物</small><b>{selectedLocationItems.length} 条</b></span><span><small>客户</small><b>{selectedLocationCustomerLabel}</b></span><span><small>栈板</small><b>{selectedLocationPallets.length || 0} 块</b></span></div>
          {selectedLocationItems.length === 0 && <p className="twin-location-empty-primary">该位置当前没有货物</p>}
          {visibleSelectedLocationItems.map((item, itemIndex) => <button type="button" className={`twin-location-item ${correctionLotId === item.lot_id ? "correction-selected" : ""} ${focusedSearchProductKey && searchProductKey(item) === focusedSearchProductKey ? "warehouse-search-hit" : ""}`} key={item.lot_id || `${item.inventory_code}-${itemIndex}`} onClick={() => { setCorrectionLotId(item.lot_id || null); setCorrectionQuantity(""); setCorrectionReason(""); setWarehouseOperationMessage(""); }}>
            <div className="twin-location-item-code"><b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b><strong>{formatNumber(inventoryLabelQuantity(item))} {inventoryUnitLabel(item.unit)}</strong></div>
            <h4>{item.product_name || "产品名称待补充"}</h4>
            <div className="twin-location-item-summary"><span>{item.customer_name || "客户待确认"}</span></div>
          </button>)}
          {mapMode === "lookup" && selectedLocationItems.length > 4 && <button type="button" className="twin-detail-toggle" aria-expanded={locationItemsExpanded} onClick={() => setLocationItemsExpanded((current) => !current)}>{locationItemsExpanded ? "收起货物" : `查看全部 ${selectedLocationItems.length} 条货物`}</button>}
          {palletColumnConflictIds.has(`erp-location-${selectedLocation.location_id}`) && <p className="twin-location-column-warning">该货位与固定柱子重叠，请在二维“库位布局”中拖离柱子后再保存。</p>}
          <button type="button" className="twin-detail-toggle secondary" aria-expanded={locationDetailOpen} onClick={() => setLocationDetailOpen((current) => !current)}>{locationDetailOpen ? "收起位置与栈板详情" : "位置与栈板详情"}</button>
          {locationDetailOpen && <div className="twin-location-secondary">
            <h3>{selectedLocation.location_name}</h3>
            <dl>
              <div><dt>区域与库位</dt><dd>{selectedLocation.area_code || "未分区"} · {selectedLocation.location_code}</dd></div>
              <div><dt>地图状态</dt><dd>{locationDrafts[selectedLocation.location_id] ? "未保存草稿" : selectedLocation.position_status === "mapped" ? "已确认布局" : "待布局"}</dd></div>
              <div><dt>点位方式</dt><dd>{selectedLocation.map_position?.source_type === "manual" && Number(selectedLocation.map_position.version) > 1 ? "手工固定" : selectedLocation.map_position?.source_type === "manual" ? "历史系统点位（可自动接管）" : "系统均匀排布"}</dd></div>
              <div><dt>占地语义</dt><dd>{selectedLocation.map_position?.layout_kind === "physical_pallet" ? "实体栈板占地" : selectedLocation.map_position?.layout_kind === "logical_anchor" ? "逻辑货位点（非实尺度）" : "历史尺寸待确认"}</dd></div>
              <div><dt>系统栈板</dt><dd>{selectedLocationPallets.length > 1 ? `${selectedLocationPallets.length} 块（共享待送位置）` : selectedLocationSinglePallet?.pallet_code || "当前无栈板"}</dd></div>
              <div><dt>库存明细</dt><dd>{selectedLocationItems.length} 条</dd></div>
            </dl>
          </div>}
          {viewMode === "25d" && <p className="twin-location-readonly-note">2.5D 仅查看库位与货物标签；调整请切换二维平面。</p>}
          {warehouseOperationMessage && <div className="twin-location-message">{warehouseOperationMessage}</div>}
          {canExecuteWarehouse && mapMode === "move" && viewMode === "2d" && <section className="twin-move-source-panel">
            <div className="twin-formal-operation-title"><b>① {moveAction === "merge" ? "多选兼容系统栈板" : "选择要移动的货物"}</b><span>只建页面草稿</span></div>
            {selectedLocation.occupancy_status === "empty" ? <p>{moveAction === "merge" ? "当前是空货位，没有可加入合并集合的系统栈板。" : "当前是空货位。请先点有货位置选择来源，或将已绑定实体栈板的货物卡拖到此处。"}</p> : <>
              {selectedLocationPallets.length > 1 && <p className="twin-shared-pallet-note">该实际位置共有 {selectedLocationPallets.length} 块系统栈板。地图只显示一个真实位置，不伪造重叠坐标；请在下方逐块选择。</p>}
              <div className="twin-move-pallet-list">
                {selectedLocationPallets.map((pallet) => {
                  const firstItem = pallet.items[0];
                  const totalQuantity = pallet.items.reduce((total, item) => total + Number(inventoryLabelQuantity(item)), 0);
                  const productCount = new Set(pallet.items.map((item) => item.product_id || item.inventory_code || item.lot_id)).size;
                  const mergeCandidate = normalizePalletMergeCandidate(selectedLocation, pallet);
                  const mergeSelected = mergeSources.some((item) => item.pallet_id === pallet.pallet_id);
                  const mergeCompatibility = mergeSources.length && mergeCandidate.candidate
                    ? palletMergeCompatibility(mergeSources[0], mergeCandidate.candidate)
                    : { compatible: true, error: null };
                  const disabled = moveAction === "merge"
                    ? !mergeCandidate.candidate || (!mergeSelected && !mergeCompatibility.compatible) || mergeBatchBusy
                    : !pallet.version;
                  return <button type="button" aria-pressed={moveAction === "merge" ? mergeSelected : undefined} title={moveAction === "merge" ? mergeCandidate.error || mergeCompatibility.error || "" : ""} className={moveAction === "merge" ? mergeSelected ? "selected merge-selected" : disabled ? "merge-ineligible" : "" : moveSource?.source_key === `pallet:${pallet.pallet_id}` ? "selected" : ""} disabled={disabled} key={`move-pallet-${pallet.pallet_id}`} onClick={() => moveAction === "merge" ? toggleMergeSource(selectedLocation, pallet) : chooseMoveSource(palletMoveSource(selectedLocation, pallet))}>
                    <b>{moveAction === "merge" ? mergeSelected ? "已选合并栈板" : "加入合并集合" : "整栈板移动"} · {pallet.pallet_code}</b>
                    <span>{firstItem?.customer_name || "客户待确认"} · {firstItem?.product_name || "产品名称待补充"}{productCount > 1 ? ` 等 ${productCount} 款` : ""}</span>
                    <small>{mergeCandidate.candidate ? `${mergeCandidate.candidate.lot_count} 个正式批次 · 合计 ${formatNumber(mergeCandidate.candidate.total_quantity)} ${inventoryUnitLabel(mergeCandidate.candidate.unit)} · ${mergeCandidate.candidate.inventory_status === "frozen" ? "冻结" : "可用"}` : mergeCandidate.error || `${pallet.item_count ?? pallet.items.length} 条库存明细 · 合计 ${formatNumber(totalQuantity)} ${inventoryUnitLabel(firstItem?.unit)}`}</small>
                  </button>;
                })}
              </div>
              {moveAction === "relocate" && <div className="twin-move-lot-list">
                {selectedLocationItems.map((item) => <button type="button" className={moveSource?.source_key === `lot:${item.lot_id}` ? "selected" : ""} disabled={!item.version || movableLotQuantity(item) <= 0} key={`move-lot-${item.lot_id}`} onClick={() => chooseMoveSource(lotMoveSource(selectedLocation, item))}>
                  <b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b><span>{item.product_name || "产品名称待补充"}</span><small>可移动 {formatNumber(movableLotQuantity(item))} {inventoryUnitLabel(item.unit)}</small>
                </button>)}
              </div>}
            </>}
          </section>}
          {P1_47D_ENABLED && canEditLocations && mapMode === "move" && viewMode === "2d" && !locationEditMode && (selectedLocationCanReceiveFinished || selectedLocationCanReceiveSemiFinished || selectedLocationCanReceiveRawMaterial) && <section className="twin-formal-operation">
            <div className="twin-formal-operation-title"><b>地图选点入仓 / 差异补录</b><span>{floorCode} · {selectedLocation.area_code} · {selectedLocation.location_name} · 仅 admin</span></div>
            <div className="twin-map-inbound-type" role="tablist" aria-label="入仓库存类型">
              <button type="button" className={inboundInventoryType === "finished" ? "active" : ""} disabled={!selectedLocationCanReceiveFinished} onClick={() => { setInboundInventoryType("finished"); setInboundMode(selectedLocation.occupancy_status === "empty" && selectedLocationCanReceiveStaging ? "staging" : "catalog"); setInboundCustomerId(""); setInboundProductId(""); setWarehouseOperationMessage(""); }}>成品</button>
              <button type="button" className={inboundInventoryType === "semi_finished" ? "active" : ""} disabled={!selectedLocationCanReceiveSemiFinished} onClick={() => { setInboundInventoryType("semi_finished"); setInboundMode("catalog"); setInboundCustomerId(""); setInboundProductId(""); setWarehouseOperationMessage(""); }}>半成品</button>
              <button type="button" className={inboundInventoryType === "raw_material" ? "active" : ""} disabled={!selectedLocationCanReceiveRawMaterial} onClick={() => { setInboundInventoryType("raw_material"); setInboundMode("catalog"); setInboundCustomerId(""); setInboundProductId(""); setWarehouseOperationMessage(""); }}>原材料栈板</button>
            </div>
            <p className="twin-map-pick-hint">先在顶部选择 1F/3F，再直接点地图上的真实位置。页面以区域和现场位置为主，内部库位编码只在详情中保留。</p>
            <div className="twin-inbound-mode" role="tablist" aria-label="货位选货来源">
              {inboundInventoryType === "finished" && selectedLocationCanReceiveStaging && selectedLocation.occupancy_status === "empty" && <button type="button" className={inboundMode === "staging" ? "active" : ""} onClick={() => { setInboundMode("staging"); setInboundQuantity(""); setWarehouseOperationMessage(""); }}>已完工未送</button>}
              <button type="button" className={inboundMode === "catalog" ? "active" : ""} onClick={() => { setInboundMode("catalog"); setInboundQuantity(""); setWarehouseOperationMessage(""); }}>ERP 已有产品</button>
              {inboundInventoryType === "finished" && <button type="button" className={inboundMode === "temporary" ? "active" : ""} onClick={() => { setInboundMode("temporary"); setInboundQuantity(""); setWarehouseOperationMessage(""); }}>临时新产品</button>}
            </div>
            {inboundInventoryType === "finished" && inboundMode === "staging" && selectedLocationCanReceiveStaging && selectedLocation.occupancy_status === "empty" && <>
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
              <div className="twin-formal-operation-grid"><label><span>{selectedLocation.occupancy_status === "empty" ? "现场实物" : "合并补录"}数量（{inboundInventoryType === "finished" ? "只" : "张"}）</span><input type="number" min="1" step="1" value={inboundQuantity} onChange={(event) => setInboundQuantity(event.target.value)} /></label><label><span>{inboundInventoryType === "raw_material" ? "登记日期" : "库存日期"}</span><input type="date" value={inboundStockDate} onChange={(event) => setInboundStockDate(event.target.value)} /></label></div>
              {(inboundInventoryType === "finished" || inboundInventoryType === "raw_material") && selectedLocation.occupancy_status === "empty" && selectedLocation.storage_type !== "rack" && <label><span>实体栈板编号（可留空自动生成）</span><input value={inboundPalletCode} onChange={(event) => setInboundPalletCode(event.target.value)} placeholder="例如 PAL-3F-001" /></label>}
              {selectedLocation.occupancy_status === "occupied" && <small className="twin-merge-rule">已有货物不代表禁止入仓：同客户、同存货产品且类型兼容时可合并；其他情况系统会阻止。</small>}
              <button type="button" className="twin-primary-action" disabled={warehouseOperationBusy || !selectedInboundCustomer || !selectedInboundProduct || !inboundQuantity || !inboundStockDate || !selectedLocationCanReceiveProduct} onClick={confirmMapFinishedInbound}>{selectedLocation.occupancy_status === "empty" ? `二次确认并登记${inboundInventoryType === "finished" ? "成品" : inboundInventoryType === "semi_finished" ? "半成品" : "原材料栈板"}` : "二次确认并合并补录"}</button>
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
              <span className="twin-map-target-title">点选三楼空位缩略图</span>
              <div className="twin-map-target-grid" role="listbox" aria-label="三楼整托移位目标">
                {emptyMoveTargets.map((item) => <button type="button" role="option" aria-selected={moveTargetLocationId === String(item.location_id)} className={moveTargetLocationId === String(item.location_id) ? "selected" : ""} key={item.location_id} onClick={() => { setMoveTargetLocationId(String(item.location_id)); setMoveIdempotencyKey(operationKey("map-move")); }}>
                  <span className="twin-map-target-thumbnail"><i style={{ left: `${Math.max(4, Math.min(88, Number(item.map_position?.left_pct || 50)))}%`, top: `${Math.max(4, Math.min(82, Number(item.map_position?.top_pct || 50)))}%` }} /></span>
                  <b>{item.location_name}</b><small>{item.area_code} · 当前空位</small>
                </button>)}
                {emptyMoveTargets.length === 0 && <p>三楼当前没有已发布、已放置的成品空位。</p>}
              </div>
              <small className="twin-formal-selected">本次会同步移动实体栈板及其关联正式库存位置，不改变库存数量。</small>
              <button type="button" className="twin-primary-action" disabled={warehouseOperationBusy || !moveTargetLocationId || !selectedLocation.pallet} onClick={confirmMapPalletMove}>确认正式栈板移位</button>
            </>}
          </section>}
          {P1_47D_ENABLED && canEditLocations && mapMode === "move" && viewMode === "2d" && !locationEditMode && !selectedLocationCanReceiveFinished && !selectedLocationCanReceiveSemiFinished && !selectedLocationCanReceiveRawMaterial && <p className="twin-location-readonly-note">该位置尚未启用、未完成布局、区域用途不匹配或与柱子冲突，暂不能办理入仓；请直接在地图上改选兼容位置。</p>}
          {locationEditMode && advancedAreaMaintenanceOpen && canEditLocations && <div className="twin-location-edit-actions">
            <button type="button" disabled={!selectedLocation.map_position || locationEditBusy} onClick={exchangeLocationDraft}>{swapSourceLocationId === null ? "设为交换起点" : swapSourceLocationId === selectedLocation.location_id ? "已选交换起点" : `与 ${visualLocations.find((item) => item.location_id === swapSourceLocationId)?.location_code || "起点"} 交换位置`}</button>
            <button type="button" className="danger" disabled={selectedLocation.occupancy_status !== "empty" || !selectedLocation.map_position || locationEditBusy} onClick={disableSelectedLocation}>停用空库位</button>
            <small>拖动、交换只改变地图位置，不改变库存数量、实体栈板或库位绑定。</small>
          </div>}
        </section>}
        {(selectedFeature || (locationEditMode && selectedRack)) && <section className="twin-inventory-card">
          {selectedAreaFeature ? <>
            <div className="twin-inventory-title"><div><small>当前区域 · {selectedAreaCode || selectedAreaFeature.feature_code}</small><b>{selectedAreaFeature.name || "仓储区域"}</b></div><span>{selectedAreaActivationLabel}</span></div>
            <div className="twin-selection-summary area"><span><small>区域状态</small><b>{selectedAreaActivationLabel}</b></span><span><small>最大容量</small><b>{selectedAreaCapacitySummary}</b></span><span><small>{selectedAreaIsMold ? "当前模具" : "当前库存"}</small><b>{selectedAreaIsMold ? `${moldAreaResponse?.total || 0} 件` : selectedAreaQuantitySummary === "0" ? "0 · 当前无货" : selectedAreaQuantitySummary}</b></span></div>
            {(areaInventorySearch || (selectedAreaIsMold ? (moldAreaResponse?.total || 0) > 5 : selectedInventory.length > 5)) && <label className="twin-area-filter twin-area-filter-prominent">
              <span>{selectedAreaIsMold ? "当前区域模具筛选" : "当前区域库存筛选"}</span>
              <input value={areaInventorySearch} onChange={(event) => { setAreaInventorySearch(event.target.value); setMoldAreaPage(1); }} placeholder={selectedAreaIsMold ? "模具编号、名称、客户或存货编码" : "存货编码、产品、客户、位置"} />
              <small>{selectedAreaIsMold ? `显示 ${moldAreaResponse?.items.length || 0} / ${moldAreaResponse?.total || 0} 件` : `显示 ${filteredSelectedInventory.length} / ${selectedInventory.length} 条`}</small>
            </label>}
            {!selectedAreaIsMold && focusedSearchProduct && focusedSearchProduct.items.some((item) => item.area_code === selectedAreaCode) && <div className="twin-search-focus-note product-focus"><b>已找到该产品</b><span>{focusedSearchProduct.inventory_code} · 本区域位置已高亮</span></div>}
            {locationEditMode && canEditLocations && advancedAreaMaintenanceOpen && <div className="twin-area-layout-summary"><div><b>区域布局</b><small>{selectedAreaRacks.length} 个货架 · {selectedAreaLocationCount} 个正式库位</small></div><button type="button" disabled={spatialEditBusy || !selectedAreaFeature} onClick={addRackToSelectedArea}>＋ 添加货架</button></div>}
            {locationEditMode && canEditLocations && selectedAreaHasPublishedBinding && selectedAreaFeature.capacity_review_status === 'pending' && <div className="twin-location-readonly-note"><b>容量待复核</b><span>已启用区域可直接在下方填写最大栈板数并一次确认；需要独立台账时再进入高级维护。</span>{advancedAreaMaintenanceOpen && <a href={selectedAreaCapacityReviewUrl} target="_top">单独复核容量</a>}</div>}
            {locationEditMode && canEditLocations && !selectedAreaFeature.formal_area_id && selectedAreaFeature.formal_binding_status !== 'draft' && <div className="twin-location-readonly-note"><b>尚未绑定正式区域</b><span>直接使用下方简化表单确认用途、形式和容量，系统会自动建立绑定并启用。</span></div>}
            {locationEditMode && canEditLocations && selectedAreaFeature.formal_binding_status === 'draft' && selectedAreaFeature.formal_policy_status !== 'published' && <div className="twin-location-readonly-note"><b>区域绑定草稿待处理</b><span>请展开高级维护，先发布或放弃该草稿；一次确认不会夹带发布其他草稿。</span></div>}
            {locationEditMode && canEditLocations && selectedAreaFeature.capacity_review_status === 'confirmed' && selectedAreaFeature.capacity_eligible && <div className="twin-location-readonly-note"><b>现场确认最大 {selectedAreaFeature.confirmed_pallet_capacity || 0} 个栈板</b></div>}
            {locationEditMode && canEditLocations && selectedAreaFeature.capacity_review_status === 'confirmed' && !selectedAreaFeature.capacity_eligible && <div className="twin-location-readonly-note"><b>不计入长期容量</b></div>}
            {locationEditMode && canEditLocations && selectedAreaFeature.capacity_review_status === 'excluded' && <div className="twin-location-readonly-note"><b>不计入长期容量</b></div>}
            {locationEditMode && canEditLocations && selectedAreaFeature && <div className="twin-zone-simple-planner">
              <header><div><small>当前规划区域</small><b>{formalAreaCodeDraft || selectedAreaFeature.feature_code} · {formalAreaNameDraft || selectedAreaFeature.name}</b></div>{selectedAreaHasPublishedBinding && <span>已启用，可核对后更新</span>}</header>
              {!selectedAreaFeature.formal_area_id && formalAreaOptions.length > 0 && <label><span>已有同楼层区域（如之前已建，可直接选）</span><select value={selectedExistingAreaId} onChange={(event) => selectExistingFormalArea(event.target.value)}><option value="">使用地图规划编号，新建正式区域</option>{formalAreaOptions.map((area) => <option value={area.id} key={area.id}>{area.area_code} · {area.area_name}</option>)}</select></label>}
              <label><span>主要用来堆放</span><select value={simpleAreaUsage} onChange={(event) => setSimpleAreaUsage(event.target.value as InventoryUsage)}><option value="finished">成品</option><option value="semi_finished">半成品</option><option value="raw_material">原材料</option><option value="mold">模具</option><option value="print_plate">印刷版</option><option value="temporary_turnover">临时周转</option></select></label>
              <label><span>区域形式</span><select value={simpleAreaLayout} onChange={(event) => setSimpleAreaLayout(event.target.value as Exclude<StorageLayout, "mixed">)}><option value="pallet_ground">栈板区</option><option value="rack">货架区</option></select></label>
              <label><span>最大可放栈板数</span><input type="number" min="0" max="500" step="1" value={simpleAreaCapacity} onChange={(event) => setSimpleAreaCapacity(event.target.value)} /><small>不放栈板的模具架、印版架等区域可填 0。</small></label>
              <button type="button" className="confirm" disabled={spatialEditBusy || !formalAreaCodeDraft.trim() || simpleAreaCapacity === ""} onClick={confirmSelectedAreaOnce}>{spatialEditBusy ? "正在确认并启用…" : "确认并启用此区域"}</button>
              <p>系统自动完成保存、校验和启用；不会移动库存、栈板或产品。</p>
              {selectedAreaHasPublishedBinding && selectedAreaCreatesInventoryLocations && <div className="twin-location-point-planner">
                <div><b>货位点位</b><small>{selectedAreaLocationCount} 个正式货位 · 只调整当前区域</small></div>
                {locationPointEditAreaCode === selectedAreaCode ? <div className="actions">
                  <button type="button" className="save" disabled={locationEditBusy || !locationPointDraftCount} onClick={saveLocationDrafts}>保存并固定{locationPointDraftCount ? ` ${locationPointDraftCount}` : ""}</button>
                  <button type="button" disabled={locationEditBusy} onClick={cancelLocationPointEditing}>取消点位调整</button>
                </div> : <div className="actions">
                  <button type="button" disabled={locationEditBusy || !selectedAreaLocationCount} onClick={beginSelectedAreaLocationPointEdit}>调整货位点位</button>
                  <button type="button" className="auto" disabled={locationEditBusy || !selectedAreaLocationCount || !areaLocationManagement?.available_actions.includes("auto_arrange")} onClick={autoArrangeSelectedAreaLocations}>{locationEditBusy ? "正在排布…" : "自动均匀排布空闲系统货位"}</button>
                </div>}
                <p>{locationPointEditAreaCode === selectedAreaCode ? "请在二维地图拖到现场实际点位；红色柱冲突货位必须先拖离。保存只固定地图位置，不改库存或货物。" : "自动排布仅调整空闲系统货位并避开柱子；手工固定和占用货位保持原位，占用货位如需改变请先按现场实际位置手工调整。两种操作都不会移动库存、栈板或货物。"}</p>
              </div>}
              <button type="button" className="advanced-toggle" aria-expanded={advancedAreaMaintenanceOpen} disabled={Boolean(locationPointEditAreaCode)} title={locationPointEditAreaCode ? "请先保存并固定或取消点位调整" : ""} onClick={() => { setAdvancedAreaMaintenanceOpen((value) => !value); setAreaPolicyEditMode(true); }}>{advancedAreaMaintenanceOpen ? "收起高级维护" : "高级维护"}</button>
            </div>}
            {locationEditMode && canEditLocations && selectedAreaIsMold && <section className="twin-mold-rack-planner">
              <header><div><small>模具货架层格结构</small><b>直接选择货架，设置层数和每层格数</b></div><span>{selectedAreaMoldRacks.length} 个货架</span></header>
              {!selectedAreaMoldRacks.length && <p className="twin-mold-rack-planner-empty">当前模具区域还没有绑定模具货架；请在高级维护中先添加并标记货架。</p>}
              <div className="twin-mold-rack-planner-list">{selectedAreaMoldRacks.map((rack) => {
                const counts = rackLevelCellCounts(rack);
                const blockedLevels = moldRackBlockedLevels(rack);
                return <button type="button" className={selectedRack?.id === rack.id ? "active" : ""} key={rack.id} onClick={() => {
                  setSelected({ kind: "rack", id: rack.id });
                  setRackDrafts((current) => ({ ...current, [rack.id]: current[rack.id] || rackDraft(rack) }));
                  setLocationEditMessage(`已选择 ${rack.mold_rack_code || rack.rack_code}；可直接修改层数与每层格数，保存后仍是待发布草稿。`);
                }}><b>{rack.mold_rack_code || rack.rack_code}</b><span>{rack.name}</span><small>{rack.levels} 层 · {counts.map((count, index) => blockedLevels.includes(index + 1) ? `第${index + 1}层 设备占用` : `第${index + 1}层 ${count} 格`).join(" / ")}</small></button>;
              })}</div>
              {selectedRackEditDraft?.mold_rack_code && selectedAreaMoldRacks.some((rack) => rack.id === selectedRackEditDraft.id) && <div className="twin-mold-rack-structure-editor">
                <div><b>{selectedRackEditDraft.mold_rack_code} · {selectedRackEditDraft.name}</b><small>这里维护发布地图的正式层格结构，不改变模具台账中的位置。</small></div>
                <label><span>货架总层数</span><input type="number" min="1" max="20" value={selectedRackEditDraft.levels} onChange={(event) => changeRackLevels(selectedRackEditDraft, Number(event.target.value))} /></label>
                <div className="twin-mold-rack-level-counts">{selectedRackEditDraft.level_cell_counts.map((count, index) => {
                  const level = index + 1;
                  const machineBlocked = moldRackBlockedLevels(selectedRackEditDraft).includes(level);
                  return <label className={machineBlocked ? "blocked" : ""} key={`${selectedRackEditDraft.id}-simple-grid-${level}`}><span>第 {level} 层格数{machineBlocked ? "（设备占用层）" : ""}</span><input type="number" min="0" max="50" disabled={machineBlocked} value={machineBlocked ? 0 : count} onChange={(event) => changeRackLevelCellCount(selectedRackEditDraft, index, Number(event.target.value))} /></label>;
                })}</div>
                <div className="twin-mold-rack-planner-actions"><button type="button" className="primary" disabled={spatialEditBusy} onClick={saveSelectedRack}>保存层格到草稿</button><button type="button" disabled={spatialEditBusy} onClick={() => setRackDrafts((current) => { const next = { ...current }; delete next[selectedRackEditDraft.id]; return next; })}>取消本次修改</button></div>
                <p>减少已被正式模具位置使用的层或格会被系统拦截；增加格位不会自动搬动或平均分配现有模具。保存草稿后仍需“校验并发布”。</p>
              </div>}
            </section>}
            {locationEditMode && advancedAreaMaintenanceOpen && areaPolicyEditMode && canEditLocations && selectedAreaFeature && selectedZonePolicy && <div className="twin-zone-policy-editor">
              {selectedAreaBoundary && <><div><b>实测区域边界</b><small>仅在实测边界确实有变化时维护。</small></div><div className="twin-formal-operation-grid">
                {([['中心 X', 'centerXmm'], ['中心 Y', 'centerYmm'], ['长', 'widthMm'], ['宽', 'heightMm']] as const).map(([label, key]) => <label key={key}><span>{label}（mm）</span><input type="number" value={selectedAreaBoundary[key]} onChange={(event) => {
                  const next = { ...selectedAreaBoundary, [key]: Number(event.target.value) };
                  setZoneGeometryDrafts((current) => ({ ...current, [selectedAreaFeature.id]: resizeAndMovePointsMm(selectedAreaBoundaryPoints, next.centerXmm, next.centerYmm, next.widthMm, next.heightMm) }));
                }} /></label>)}
              </div><div><button type="button" className="primary" disabled={spatialEditBusy || !zoneGeometryDrafts[selectedAreaFeature.id]} onClick={saveSelectedZoneGeometry}>保存实测边界草稿</button><button type="button" disabled={!zoneGeometryDrafts[selectedAreaFeature.id]} onClick={() => setZoneGeometryDrafts((current) => { const next = { ...current }; delete next[selectedAreaFeature.id]; return next; })}>取消边界草稿</button></div></>}
              <div><b>正式区域绑定</b><small>区域编号保存后不可与其他地图区域重复；发布前仍不会进入员工入库候选。</small></div>
              {!selectedAreaFeature.formal_area_id && <label><span>选用现有未绑定区域</span><select value={selectedExistingAreaId} onChange={(event) => selectExistingFormalArea(event.target.value)}><option value="">不选，按下方编号建立新区域</option>{formalAreaOptions.map((area) => <option value={area.id} key={area.id}>{area.floor_code} · {area.area_code} {area.area_name} · {area.capacity_review_status === "confirmed" ? `已确认 ${area.confirmed_pallet_capacity || 0} 栈板` : area.capacity_review_status === "excluded" ? "不计长期容量" : "容量待复核"}</option>)}</select>{formalAreaOptionsError && <small>现有区域读取失败：{formalAreaOptionsError}</small>} {!formalAreaOptionsError && formalAreaOptions.length === 0 && <small>当前楼层没有可选的未绑定区域；可使用下方新编号。</small>}</label>}
              <label><span>正式区域编号</span><input maxLength={30} disabled={Boolean(selectedExistingAreaId)} value={formalAreaCodeDraft} onChange={(event) => setFormalAreaCodeDraft(event.target.value.toUpperCase())} placeholder="例如 FIN-001" /></label>
              <label><span>区域名称</span><input maxLength={100} disabled={Boolean(selectedExistingAreaId)} value={formalAreaNameDraft} onChange={(event) => setFormalAreaNameDraft(event.target.value)} placeholder="例如 一楼成品待送区" /></label>
              <div><b>区域允许存放类型</b><small>可多选；只保存区域策略，不自动转换现有库存</small></div>
              <div className="twin-zone-policy-options">{([[
                "finished", "成品"
              ], ["semi_finished", "半成品"], ["raw_material", "原材料"], ["mold", "模具"], ["print_plate", "印刷版"], ["temporary_turnover", "临时周转"]] as Array<[InventoryUsage, string]>).map(([value, label]) => <label key={value}><input type="checkbox" checked={selectedZonePolicy.allowed_inventory_types.includes(value)} onChange={() => toggleAreaUsage(value)} /><span>{label}</span></label>)}</div>
              <label><span>空间存储形式</span><select value={selectedZonePolicy.storage_layout} onChange={(event) => setZonePolicyDrafts((current) => ({ ...current, [selectedAreaFeature.id]: { ...selectedZonePolicy, storage_layout: event.target.value as StorageLayout } }))}><option value="rack">货架区</option><option value="pallet_ground">栈板地堆区</option><option value="mixed">货架＋栈板混合区</option></select></label>
              <button type="button" className="primary" disabled={spatialEditBusy || !formalAreaCodeDraft.trim() || !selectedZonePolicy.allowed_inventory_types.length} onClick={saveSelectedZonePolicy}>绑定正式区域并保存策略</button>
            </div>}
            {locationEditMode && advancedAreaMaintenanceOpen && canEditLocations && selectedAreaCode && selectedAreaCreatesInventoryLocations && selectedAreaHasFormalLedger && <div className="twin-location-create">
              <div><b>区域库位数量</b><small>当前 {selectedAreaLocationCount} 个{selectedAreaPendingLocationCount ? ` · ${selectedAreaPendingLocationCount} 个待布局` : ""}</small></div>
              <label><span>目标库位数</span><input type="number" min="0" max="500" step="1" value={targetAreaLocationCount} onChange={(event) => setTargetAreaLocationCount(event.target.value)} /></label>
              <button type="button" disabled={locationEditBusy || targetAreaLocationCount === "" || Number(targetAreaLocationCount) === selectedAreaLocationCount} onClick={applyAreaLocationCount}>确认调整</button>
              <p>系统按区域自动生成内部唯一编码和员工可读名称；减少时只逻辑停用空库位，不删除历史身份。</p>
            </div>}
            {locationEditMode && advancedAreaMaintenanceOpen && canEditLocations && selectedAreaCode && !selectedAreaHasFormalLedger && <div className="twin-location-create"><p>区域策略仍是管理员草稿；请先校验并发布建立正式区域，再规划正式库位。</p></div>}
            {locationEditMode && advancedAreaMaintenanceOpen && canEditLocations && selectedAreaCode && selectedAreaHasFormalLedger && !selectedAreaCreatesInventoryLocations && <div className="twin-location-create"><p>该区域使用原料、模具、印版或临时周转台账，不生成成品/半成品库存库位；发布后按对应台账定位。</p></div>}
            {locationEditMode && advancedAreaMaintenanceOpen && canEditLocations && !selectedAreaCode && <div className="twin-location-create"><p>请先确认启用区域，之后才能生成可投入使用的库位。</p></div>}
            {selectedAreaIsMold ? <div className="twin-area-mold-list">
              {!locationEditMode && selectedAreaMoldRacks.length > 0 && <div className="twin-mold-rack-lookup-shortcuts"><b>打开模具货架正视图</b><span>按正式模具台账读取，不显示模拟纸箱库存。</span><div>{selectedAreaMoldRacks.map((rack) => <button type="button" key={rack.id} onClick={() => { setRackFocusId(rack.id); setSelected({ kind: "rack", id: rack.id }); }}>{rack.mold_rack_code || rack.rack_code}<small>{rack.name}</small></button>)}</div></div>}
              {moldAreaLoading && <div className="twin-area-empty"><b>正在读取模具资产台账…</b><span>只读取已登记且位置属于当前实测区域的模具。</span></div>}
              {!moldAreaLoading && moldAreaError && <div className="twin-area-empty error"><b>模具台账读取失败</b><span>{moldAreaError}</span></div>}
              {!moldAreaLoading && !moldAreaError && !moldAreaResponse?.items.length && <div className="twin-area-empty"><b>当前区域没有已定位模具</b><span>未填写位置或仍使用旧自由文本位置的模具，不会被误算进该实测区域。</span></div>}
              {!moldAreaLoading && moldAreaResponse?.items.map((item) => {
                const rackCode = item.location_guide?.rack ? `R${String(item.location_guide.rack).padStart(2, "0")}` : null;
                const mappedRack = rackCode ? selectedAreaMoldRacks.find((rack) => rack.mold_rack_code === rackCode) : null;
                return <article className="twin-area-mold" key={item.id}>
                  <div><b>{item.mold_code}</b><em>{item.product_count ? `${item.product_count} 款产品` : "未绑产品"}</em></div>
                  <strong>{item.mold_name}</strong>
                  <span>{item.location_guide?.prompt || "位置指引待补充"}</span>
                  {mappedRack && !locationEditMode && <button type="button" onClick={() => { setRackFocusId(mappedRack.id); setSelected({ kind: "rack", id: mappedRack.id }); }}>打开 {rackCode} 正视图</button>}
                </article>;
              })}
              {!moldAreaLoading && moldAreaResponse && moldAreaResponse.total > moldAreaResponse.page_size && <div className="twin-area-mold-pagination"><button type="button" disabled={moldAreaResponse.page <= 1} onClick={() => setMoldAreaPage((value) => Math.max(1, value - 1))}>上一页</button><span>第 {moldAreaResponse.page} / {Math.ceil(moldAreaResponse.total / moldAreaResponse.page_size)} 页</span><button type="button" disabled={moldAreaResponse.page * moldAreaResponse.page_size >= moldAreaResponse.total} onClick={() => setMoldAreaPage((value) => value + 1)}>下一页</button></div>}
            </div> : <>
              <div className="twin-area-lot-list">
                {!selectedInventory.length && <div className="twin-area-empty"><b>{selectedAreaHasPublishedBinding ? "区域已启用，当前没有货物" : selectedAreaActivationLabel === "待启用" ? "区域绑定仍待启用" : "区域尚未启用"}</b><span>{selectedAreaHasPublishedBinding ? `${selectedAreaCapacitySummary}；库存为 0 不代表区域未启用。` : "进入区域规划确认用途、形式和容量后即可启用；系统不会生成模拟货物。"}</span></div>}
                {selectedInventory.length > 0 && !filteredSelectedInventory.length && <div className="twin-area-empty"><b>本区域没有匹配结果</b><span>请更换存货编码、产品、客户或位置关键词。</span></div>}
                {visibleSelectedInventory.map((item) => <article className={`twin-area-lot ${focusedSearchProductKey && searchProductKey(item) === focusedSearchProductKey ? "search-hit product-search-hit" : focusedSearchItem?.lot_id === item.lot_id ? "search-hit" : ""}`} key={item.lot_id}>
                  <div><b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b><strong>{formatNumber(item.available_quantity ?? item.quantity)} {inventoryUnitLabel(item.unit)}</strong></div>
                  <strong>{item.product_name || "待补充库存名称"}</strong>
                  <span>{item.customer_name || "客户待确认"} · {item.location_name || "位置待补充"}</span>
                  {areaInventoryDetailsOpen && <div className="twin-area-lot-details"><span>{selectedAreaCode || "区域待确认"} · {item.pallet_code || "地堆/散存"} · 内部码 {item.location_code || "未编"}</span><span>{item.lot_number || "批次待补充"} · {inventoryAgeLabel(item.age_days)}</span>{(item.reserved_quantity || 0) > 0 && <small>已预占 {formatNumber(item.reserved_quantity)} {inventoryUnitLabel(item.unit)}</small>}</div>}
                </article>)}
              </div>
              {filteredSelectedInventory.length > 0 && <button type="button" className="twin-detail-toggle" aria-expanded={areaInventoryDetailsOpen} onClick={() => setAreaInventoryDetailsOpen((current) => !current)}>{areaInventoryDetailsOpen ? "收起完整台账" : filteredSelectedInventory.length > 5 && !areaInventorySearch.trim() ? `查看全部 ${filteredSelectedInventory.length} 条库存` : "批次与预占详情"}</button>}
            </>}
          </> : <div className="twin-unmapped">该区域尚未建立空间策略。</div>}
        </section>}
        {selectedPlacement && <section className="twin-object-card twin-equipment-card"><span className="twin-object-kind">生产设备</span><h3>{selectedPlacement.name}</h3><dl><div><dt>长 × 宽</dt><dd>{formatNumber(selectedPlacement.width_mm)} × {formatNumber(selectedPlacement.depth_mm)} mm</dd></div><div><dt>高度</dt><dd>{formatNumber(selectedPlacement.height_mm)} mm</dd></div><div><dt>坐标</dt><dd>X {formatNumber(selectedPlacement.x_mm)} / Y {formatNumber(selectedPlacement.y_mm)}</dd></div></dl></section>}
      </aside>
    </section>
    {mapMode === "move" && canExecuteWarehouse && <section className={`twin-move-draft-bar ${moveAction === "merge" ? "merge-mode" : ""}`} aria-label={moveAction === "merge" ? "多栈合并页面草稿汇总" : "移货页面草稿汇总"}>
      {moveAction === "merge" ? <>
        <div className="twin-move-draft-heading"><div><small>MERGE DRAFT · 尚未写入</small><b>{mergeSources.length ? `${mergeSources.length} 块已选系统栈板` : "尚无合并草稿"}</b></div><span>{mergeSources.length ? "从集合内明确一块目标；失败后选择、目标和重试键都会保留。" : "在真实地图位置右侧逐块选择，不生成栈板假坐标。"}</span></div>
        <div className="twin-move-draft-list">{mergeSources.map((item) => <article className={mergeTarget?.pallet_id === item.pallet_id ? "merge-target" : ""} key={item.client_item_id || item.pallet_id}>
          <div><b>{item.pallet_code}</b><span>{item.customer_name} · {item.product_count} 款 / {item.lot_count} 批次</span></div>
          <strong>{item.floor_code} / {item.area_code || "未分区"} / {item.location_name}{mergeTarget?.pallet_id === item.pallet_id && <i>目标</i>}</strong>
          <small>{formatNumber(item.total_quantity)} {inventoryUnitLabel(item.unit)} · {item.inventory_status === "frozen" ? "冻结" : "可用"}</small>
          <button type="button" disabled={mergeBatchBusy} onClick={() => {
            const result = togglePalletMergeSource(mergeSources, item);
            setMergeSources(result.items);
            if (mergeTarget?.pallet_id === item.pallet_id) setMergeTarget(null);
            setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
          }}>撤销</button>
        </article>)}</div>
        <div className="twin-move-draft-actions"><button type="button" disabled={!mergeSources.length || mergeBatchBusy} onClick={clearMergeDraft}>清空草稿</button><button type="button" className="confirm" disabled={mergeSources.length < 2 || !mergeTarget || mergeBatchBusy} onClick={confirmPalletMergeBatch}>{mergeBatchBusy ? "正在一次提交…" : mergeTarget ? `一次确认 ${mergeSources.length - 1} 源 → 1 目标` : "请明确目标栈板"}</button></div>
      </> : <>
        <div className="twin-move-draft-heading"><div><small>PAGE DRAFT · 尚未写入</small><b>{moveDrafts.length ? `${moveDrafts.length} 条待确认移货` : "尚无移货草稿"}</b></div><span>{moveDrafts.length ? "可继续跨楼层选择；失败后草稿与重试键都会保留。" : "拖动整栈板，或在右侧按楼层、区域、具体货位加入。"}</span></div>
        <div className="twin-move-draft-list">
          {moveDrafts.map((item) => <article key={item.client_item_id}>
            <div><b>{item.inventory_code}</b><span>{item.customer_name} · {item.product_name}</span></div>
            <strong>{item.source_floor_code} / {item.source_area_code || "未分区"} / {item.source_location_name}<i>→</i>{item.target_floor_code} / {item.target_area_code || "未分区"} / {item.target_location_name}</strong>
            <small>{item.operation === "pallet_move" ? "整栈板" : `${formatNumber(item.quantity)} ${inventoryUnitLabel(item.unit)}`}</small>
            <button type="button" disabled={moveBatchBusy} onClick={() => removeMoveDraft(item.client_item_id)}>撤销</button>
          </article>)}
        </div>
        <div className="twin-move-draft-actions"><button type="button" disabled={!moveDrafts.length || moveBatchBusy} onClick={clearMoveDrafts}>清空草稿</button><button type="button" className="confirm" disabled={!moveDrafts.length || moveBatchBusy} onClick={confirmMoveDrafts}>{moveBatchBusy ? "正在整批提交…" : `一次确认 ${moveDrafts.length || ""} 条`}</button></div>
      </>}
    </section>}
  </main>;
}
