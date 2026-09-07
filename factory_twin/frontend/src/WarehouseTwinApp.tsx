import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { EditorCanvas, type CanvasFocusTarget } from "./EditorCanvas";
import { filterOperationalFeatures } from "./operationalView.mjs";
import {
  clearFormalAreaOptions,
  formalAreaOptionsEffectEnabled,
  filterPlanningPublishedFeatures,
  removeZoneHierarchy,
  stableTwinFeatures
} from "./formalAreaOptions.mjs";
import {
  floor1CandidateBlockerDetail,
  floor1CandidateBlockerHref
} from "./floor1CandidateBlockers.mjs";
import type { Floor1CandidateBlockingItem } from "./floor1CandidateBlockers.mjs";
import { pointsBoundsMm, polygonAreaMm2, resizeAndMovePointsMm, translatePointsMm } from "./layoutGeometry.mjs";
import {
  buildMappedLocationPallets,
  employeeAreaName,
  employeeLocationName,
  expandAreaInventory,
  filterAreaInventory,
  findPalletColumnConflicts,
  findPalletPlanningConflicts,
  inventoryAgeLabel,
  inventoryAgeTone,
  inventoryHasPhysicalQuantity,
  inventoryLocationItems,
  inventoryLocationPallets,
  inventoryPhysicalQuantity,
  inventoryUnitLabel,
  locationLayoutGeometry,
  mergePublishedFeatureGeometry,
  normalizeInventoryLocationProjection,
  normalizeStandardPalletContract,
  planningConflictWarning,
  searchHighlightAreaCodes,
  standardPalletDisplayIssue,
  standardPalletContractsMatch,
  uniquePalletConflictCount,
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
  palletMergeSuggestionMatchesFilter,
  palletMergeSuggestionProductKey,
  palletMergeTargetChoices,
  togglePalletMergeSource
} from "./warehousePalletMergeDraft.mjs";
import type { PalletMergeCandidate } from "./warehousePalletMergeDraft.mjs";
import {
  buildMoldLocationTarget,
  buildMoldRackView,
  buildMoldShelfSpines,
  moldRackEmployeeName,
  moldRackLevelUsage,
  moldRacksForArea
} from "./moldRackView.mjs";
import type { MoldLocationOption } from "./moldRackView.mjs";
import {
  buildStocktakeBatchPayload,
  clearStocktakeDrafts,
  removeStocktakeDraft,
  stocktakeAddBlockReason,
  stocktakeBlockResolution,
  stocktakeDecreaseBlockReason,
  stocktakeExistingProductLocations,
  stocktakeLocationBlockReason,
  upsertStocktakeDraft
} from "./warehouseStocktakeDraft.mjs";
import type { StocktakeExistingProductLocation, WarehouseStocktakeDraft } from "./warehouseStocktakeDraft.mjs";
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
  ground_location_draft?: { base_revision: string; slots: Array<{ location_id: number; expected_version: number; x_mm: number; y_mm: number; width_mm: number; depth_mm: number }> };
  formal_area_id?: number | null;
  formal_floor_id?: number | null;
  erp_area_code?: string | null;
  formal_area_name?: string | null;
  area_master_name?: string | null;
  employee_area_name?: string | null;
  formal_binding_status?: string | null;
  formal_policy_status?: "draft" | "published" | null;
  formal_policy_version?: number | null;
  formal_published_map_revision?: string | null;
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
type StocktakeInventoryType = "finished" | "semi_finished";
type WarehouseOperationalFloorCode = "1F" | "3F" | "4F";
type RackDraft = Rack & { level_clear_heights_mm: number[]; level_cell_counts: number[] };
const P1_49C_ENABLED = true;
const WAREHOUSE_OPERATIONAL_FLOORS: readonly WarehouseOperationalFloorCode[] = ["1F", "3F", "4F"];

function isWarehouseOperationalFloorCode(value: unknown): value is WarehouseOperationalFloorCode {
  return typeof value === "string"
    && WAREHOUSE_OPERATIONAL_FLOORS.includes(value as WarehouseOperationalFloorCode);
}

interface LayoutMutationResponse<T> {
  item: T;
  revision: string;
  applied: boolean;
  formal_area?: FormalWarehouseAreaOption | null;
}

interface Floor4CalibrationMutationResponse {
  item: {
    calibration: {
      status: "aligned";
      applied: true;
      max_residual_mm: number;
      rmse_residual_mm: number;
      measured_door_width_mm?: number;
      measured_depth_mm?: number;
    };
  };
  revision: string;
  applied: boolean;
}

interface FormalWarehouseAreaOption {
  id: number;
  floor_id: number;
  floor_code: string;
  floor_number: number;
  area_code: string;
  area_name: string;
  area_master_name?: string | null;
  employee_area_name?: string | null;
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
  customer_short_name?: string | null;
  quantity?: number;
  available_quantity?: number;
  reserved_quantity?: number;
  damaged_quantity?: number;
  unit?: string;
  status?: "active" | "frozen" | string;
  stocktake_decrease_eligible?: boolean;
  stocktake_decrease_block_reason?: string | null;
  age_days?: number | null;
  version?: number;
  specification?: string | null;
  material?: string | null;
  composite_parent_group_key?: string | null;
  allowed_product_ids?: number[];
  composite_parent_summary?: {
    group_key: string;
    order_item_id: number;
    order_number: string;
    product_id: number;
    inventory_code?: string | null;
    product_name: string;
    available_set_quantity: number;
    remaining_order_set_quantity: number;
    unit: "sets";
    component_lot_count: number;
    component_lot_ids: number[];
    components: Array<{
      snapshot_id: number;
      product_code?: string | null;
      product_name?: string | null;
      quantity_per_set: number;
      available_piece_quantity: number;
      complete_set_quantity: number;
      is_required: boolean;
    }>;
  } | null;
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
  move_eligible?: boolean | null;
  move_block_reason?: string | null;
}

interface DashboardLocation {
  location_id: number;
  location_code: string;
  location_name: string;
  employee_location_name?: string | null;
  current_address_name?: string | null;
  map_rack_id?: string | null;
  rack_display_name?: string | null;
  level_no?: number | null;
  slot_no?: number | null;
  address_kind?: "legacy" | "rack_slot" | "ground_slot" | "functional" | string;
  address_version?: number | null;
  floor_name?: string | null;
  area_name?: string | null;
  floor_code: string;
  area_code: string | null;
  map_feature_id?: string | null;
  published_map_revision?: string | null;
  source_version: string | null;
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
  has_unmatched_inventory_observation?: boolean;
  unmatched_inventory_observation_count?: number;
  unmatched_inventory_observations?: Array<{
    id: number;
    version: number;
    customer_keyword?: string | null;
    inventory_keyword: string;
    reported_quantity?: number | null;
    reported_unit?: string | null;
    reason: string;
    reported_at?: string | null;
  }>;
  has_location_discrepancy?: boolean;
  location_discrepancy_count?: number;
  location_discrepancies?: Array<{
    id: number;
    version: number;
    reported_quantity: number;
    reason: string;
    reported_at?: string | null;
    registered_location_id: number;
    lot: InventoryItem;
  }>;
}

interface DelayedDispatchCandidate {
  pallet_id: number;
  pallet_code: string;
  version: number;
  source_location_id: number;
  source_floor_code: "1F" | "3F";
  source_location_code: string;
  source_location_name: string;
  completion_id: number;
  completed_date: string;
  idle_days: number;
  order_id: number;
  order_number: string;
  order_item_id: number;
  delivery_date?: string | null;
  quantity: number;
  unit: string;
  customer_id?: number | null;
  customer_name?: string | null;
  product_names: string[];
  lot_ids: number[];
  recommended_floor_code: "3F";
  recommended_area_code: "SEMI-008";
  can_plan_move: boolean;
}

interface DelayedDispatchRelocation {
  policy: {
    idle_days: number;
    minimum_idle_days: number;
    maximum_idle_days: number;
    source_location_code: string;
    recommended_floor_code: "3F";
    recommended_area_code: "SEMI-008";
    writes_inventory: false;
    notice: string;
  };
  candidate_count: number;
  available_target_count: number;
  targets: Array<{
    location_id: number;
    location_code: string;
    location_name: string;
    layout_version: number;
  }>;
  items: DelayedDispatchCandidate[];
}

type LocationLayoutGeometry = NonNullable<ReturnType<typeof locationLayoutGeometry>>;

interface AuthResponse {
  user: { role: string; ui_mode?: "standard" | "large" };
  permissions: string[];
}

interface TwinDashboard {
  generated_at: string;
  read_only: boolean;
  standard_pallet: StandardPalletContract;
  scope: { notice: string };
  summary: {
    active_lots: number;
    occupied_pallets: number;
    long_age_lots: number;
    unlocated_lots: number;
    finished_map_coverage?: {
      total_lots: number;
      mapped_lots: number;
      unlocated_lots: number;
      total_quantity: number;
      mapped_quantity: number;
      unlocated_quantity: number;
      total_physical_quantity: number;
      all_located: boolean;
    };
  };
  floors: Array<{ floor_code: string; occupied_locations: number; active_lots: number }>;
  locations: DashboardLocation[];
  delayed_dispatch_relocation?: DelayedDispatchRelocation;
  unlocated_inventory?: SearchItem[];
  distribution: { areas: AreaDistribution[] };
}

interface StandardPalletContract {
  contract_version: "standard-pallet-v1";
  width_mm: number;
  depth_mm: number;
  height_mm: number;
}

interface SearchItem extends InventoryItem {
  floor_code: string;
  area_code: string | null;
  location_id: number | null;
  location_code: string | null;
  location_name: string;
  position_status: string;
  unlocated_reason?: string | null;
  pending_relocation?: boolean;
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
  location_version: number;
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

interface MoldLocationOptionsResponse {
  floor_code: string;
  racks: MoldLocationOption[];
}

interface MoldLocationMovePreview {
  mold: MoldAreaItem;
  target_location: string;
  target_guide?: { prompt?: string | null } | null;
  expected_version: number;
  same_location: boolean;
  can_confirm: boolean;
  co_located_count: number;
}

interface MoldLocationMoveResponse {
  message: string;
  mold: MoldAreaItem;
  idempotent_replay: boolean;
  no_change: boolean;
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

interface CustomerOption {
  id: number;
  name: string;
  customer_code?: string | null;
  chinese_short_name?: string | null;
}

interface CustomerOptionsResponse {
  items: CustomerOption[];
}

interface LocationCandidatesResponse {
  items: MoveCandidate[];
}

interface StocktakeBatchResultItem {
  operation: "add" | "decrease";
  lot_id: number;
  location_id: number;
  inventory_type: "finished" | "semi_finished";
  version_after: number;
  source_kind?: "existing_stocktake" | "partner_transfer" | null;
}

interface StocktakeBatchResult {
  items: StocktakeBatchResultItem[];
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
  customer_id?: number;
  product_id?: number;
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
  available_actions: Array<"location_count" | "layout" | "published_layout" | "auto_arrange" | "disable_empty" | "enable_empty">;
  policy_version?: number | null;
  published_map_revision?: string | null;
  ground_plan_id?: number | null;
  ground_plan_version?: number | null;
}

interface GroundStorageCandidate {
  location_id: number;
  location_name: string;
  row_no: number;
  slot_no: number;
  route_sequence: number;
  layout_version: number;
  status: "empty" | "same_product" | "capacity_full" | "unavailable" | "conflict";
  color: "green" | "blue" | "gray" | "red";
  selectable: boolean;
  reason: string;
  current_quantity: number;
  capacity_quantity?: number | null;
  remaining_capacity?: number | null;
  adjacent_location_ids: number[];
}

interface GroundStorageCandidatesResponse {
  floor_name: string;
  area_name: string;
  plan_version: number;
  published_map_revision: string;
  incoming_quantity: number;
  items: GroundStorageCandidate[];
}

interface GroundStorageMutationResponse {
  message: string;
  idempotent_replay: boolean;
  lot_id: number;
  lot_number: string;
  quantity: number;
  location_name: string;
  occupancy: {
    occupancy_id: number;
    footprint_kind: "single" | "double";
    location_ids: number[];
    capacity_quantity: number;
    current_quantity: number;
  };
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
  standard_pallet: StandardPalletContract;
  metadata?: { calibration?: { status?: string; applied?: boolean } };
  calibration?: { status?: string; applied?: boolean };
  alignment_status?: string;
  alignment_applied?: boolean;
}

const isFloor4Aligned = (layout: Pick<TwinFloorResponse, "metadata" | "calibration" | "alignment_status" | "alignment_applied">) => {
  const calibration = layout.metadata?.calibration || layout.calibration;
  return (calibration?.status === "aligned" && calibration?.applied === true)
    || (layout.alignment_status === "aligned" && layout.alignment_applied === true);
};

interface LayoutDraftControl {
  has_draft: boolean;
  has_other_floor_drafts?: boolean;
  dirty_floor_codes?: string[];
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
  remaining_draft_floor_codes?: string[];
  inventory_changed: false;
  mold_location_reassignment_count?: number;
  mold_location_changed?: boolean;
  bound_legacy_location_ids?: number[];
  applied: boolean;
}

interface LegacyRackBindingPreview {
  floor_code: string;
  revision: string;
  fingerprint: string;
  requires_confirmation: boolean;
  unresolved_count: number;
  groups: Array<{
    binding_key: string;
    area_id: number;
    area_code: string;
    area_name: string;
    legacy_rack_code: string;
    location_ids: number[];
    location_codes: string[];
    location_count: number;
    occupied_location_count: number;
    candidates: Array<{ map_rack_id: string; rack_name: string }>;
    suggested_map_rack_id?: string | null;
    blocking_reason?: string | null;
  }>;
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
const EMPTY_CANVAS_IDS: string[] = [];
const CALIBRATION_POINT_LABELS = ["门1", "门2", "内"];
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
  if (!response.ok) {
    const error = new Error(apiErrorMessage(payload, response.status)) as Error & { status?: number };
    error.status = response.status;
    throw error;
  }
  return payload as T | null;
}

function hydrateLayout(raw: TwinFloorResponse): Layout {
  const operationalFeatures = filterOperationalFeatures(raw.floor_code, raw.bounds_mm, raw.features || []);
  const floor4Aligned = raw.floor_code.toUpperCase() === "4F" && isFloor4Aligned(raw);
  return {
    id: raw.layout_id,
    name: floor4Aligned ? "四楼实测成品仓库（已与3F货梯对齐）" : raw.name,
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
    rule_defaults: {},
    metadata: raw.metadata,
    calibration: raw.calibration,
    alignment_status: raw.alignment_status,
    alignment_applied: raw.alignment_applied
  };
}

function formatNumber(value: number | null | undefined) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function inventoryLabelQuantity(item: InventoryItem) {
  return inventoryPhysicalQuantity(item);
}

function employeeCustomerName(item: { customer_id?: number | null; customer_name?: string | null; customer_short_name?: string | null } | null | undefined) {
  const shortName = String(item?.customer_short_name || "").trim();
  if (shortName) return shortName;
  if (item?.customer_id) return "客户简称待完善";
  return String(item?.customer_name || "通用库存").trim() || "通用库存";
}

function palletMoveSource(location: DashboardLocation, pallet?: DashboardPallet | null): WarehouseMoveSource | null {
  const selectedPallet = pallet || singleLocationPallet(location) as DashboardPallet | null;
  if (!selectedPallet?.pallet_id || !selectedPallet.version || selectedPallet.move_eligible === false) return null;
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
    inventory_code: firstItem?.inventory_code || "整栈板",
    product_name: firstItem?.product_name || "整栈板货物",
    customer_name: employeeCustomerName(firstItem),
    unit: firstItem?.unit || "boxes"
  };
}

function dashboardPalletSummary(pallet: DashboardPallet) {
  const items = pallet.items || [];
  const products = [...new Set(items.map((item) => item.product_name).filter(Boolean))];
  const customers = [...new Set(items.map((item) => employeeCustomerName(item)).filter(Boolean))];
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
    if (!inventoryHasPhysicalQuantity(item)) continue;
    const key = searchProductKey(item);
    const current: SearchProductGroup = groups.get(key) || {
      key,
      customer_id: item.customer_id || null,
      customer_name: employeeCustomerName(item),
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
    current.total_quantity += inventoryPhysicalQuantity(item);
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

function rackCellIdentityKey(
  mapRackId: string | null | undefined,
  levelNo: number | null | undefined,
  slotNo: number | null | undefined
) {
  if (!mapRackId || !Number.isInteger(levelNo) || !Number.isInteger(slotNo)) return null;
  return `${mapRackId}:L${levelNo}:S${slotNo}`;
}

function rackLocationInventoryItems(location: DashboardLocation): RackInventoryItem[] {
  return [
    ...inventoryLocationPallets(location).flatMap((pallet) => (pallet.items || []).map((item) => ({
      ...item,
      location_code: location.location_code,
      location_name: location.location_name,
      pallet_code: pallet.pallet_code || null
    }))),
    ...(location.loose_items || []).map((item) => ({
      ...item,
      location_code: location.location_code,
      location_name: location.location_name,
      pallet_code: null
    }))
  ];
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
  canMoveMolds,
  rackIndex,
  rackCount,
  onPrevious,
  onNext,
  onMoldMoved,
  onClose
}: {
  rack: Rack;
  response: MoldRackResponse | null;
  loading: boolean;
  error: string;
  canMoveMolds: boolean;
  rackIndex: number;
  rackCount: number;
  onPrevious: () => void;
  onNext: () => void;
  onMoldMoved: (message: string) => void;
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
  const [movePanelOpen, setMovePanelOpen] = useState(false);
  const [moveOptions, setMoveOptions] = useState<MoldLocationOption[]>([]);
  const [moveRackCode, setMoveRackCode] = useState("");
  const [moveLevel, setMoveLevel] = useState("");
  const [moveGrid, setMoveGrid] = useState("");
  const [movePreview, setMovePreview] = useState<MoldLocationMovePreview | null>(null);
  const [moveIdempotencyKey, setMoveIdempotencyKey] = useState("");
  const [moveAttemptUncertain, setMoveAttemptUncertain] = useState(false);
  const [moveBusy, setMoveBusy] = useState(false);
  const [moveMessage, setMoveMessage] = useState("");
  useEffect(() => {
    const first = occupiedCells[0];
    setSelectedSlotKey(first?.key || null);
    setSelectedMoldId(null);
    setSelectedProductId(null);
    setMovePanelOpen(false);
    setMovePreview(null);
    setMoveIdempotencyKey("");
    setMoveAttemptUncertain(false);
    setMoveMessage("");
  }, [rack.id, response?.total]);
  useEffect(() => {
    setMovePanelOpen(false);
    setMovePreview(null);
    setMoveIdempotencyKey("");
    setMoveAttemptUncertain(false);
    setMoveMessage("");
  }, [selectedMoldId]);
  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (moveAttemptUncertain) return;
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowLeft") onPrevious();
      if (event.key === "ArrowRight") onNext();
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [moveAttemptUncertain, onClose, onPrevious, onNext]);
  const selectedSlot = occupiedCells.find((cell) => cell.key === selectedSlotKey) || null;
  const selectedMold = response?.items.find((item) => item.id === selectedMoldId) || null;
  const selectedProduct = selectedMold?.products.find((product) => product.id === selectedProductId) || null;
  const selectedMoveRack = moveOptions.find((item) => item.rack_code === moveRackCode) || null;
  const selectedMoveLevel = selectedMoveRack?.levels.find((item) => String(item.level) === moveLevel) || null;
  const selectedMoveTarget = buildMoldLocationTarget(selectedMoveRack, moveLevel, moveGrid);
  const visibleSpines = buildMoldShelfSpines(selectedSlot?.items || response?.items || []);
  const unmatchedCount = rackView.unmatched_items.length + rackView.rack_only_items.length
    + rackView.levels.reduce((sum, level) => sum + level.level_only_items.length, 0);
  const levels = [...rackView.levels].reverse();

  const resetMovePreview = () => {
    setMovePreview(null);
    setMoveIdempotencyKey("");
    setMoveAttemptUncertain(false);
    setMoveMessage("");
  };

  const selectMoveRack = (rackCode: string, options = moveOptions) => {
    const option = options.find((item) => item.rack_code === rackCode) || options[0] || null;
    const guide = selectedMold?.location_guide;
    const preferredLevel = option?.rack_code === `R${String(guide?.rack || "").padStart(2, "0")}`
      ? Number(guide?.level || 0)
      : 0;
    const level = option?.levels.find((item) => item.level === preferredLevel) || option?.levels[0] || null;
    const preferredGrid = Number(guide?.grid || guide?.row || 0);
    const grid = level?.grids.includes(preferredGrid) ? preferredGrid : level?.grids[0] || 0;
    setMoveRackCode(option?.rack_code || "");
    setMoveLevel(level ? String(level.level) : "");
    setMoveGrid(grid ? String(grid) : "");
    resetMovePreview();
  };

  const openMoldMove = async () => {
    if (!selectedMold || !canMoveMolds || moveBusy || moveAttemptUncertain) return;
    setMovePanelOpen(true);
    setMoveMessage("");
    if (moveOptions.length) {
      const currentRackCode = selectedMold.location_guide?.rack
        ? `R${String(selectedMold.location_guide.rack).padStart(2, "0")}`
        : "";
      selectMoveRack(currentRackCode || moveOptions[0].rack_code);
      return;
    }
    setMoveBusy(true);
    try {
      const result = await requestJson<MoldLocationOptionsResponse>("/api/warehouse/molds/location-options");
      const options = result.racks || [];
      setMoveOptions(options);
      const currentRackCode = selectedMold.location_guide?.rack
        ? `R${String(selectedMold.location_guide.rack).padStart(2, "0")}`
        : "";
      selectMoveRack(currentRackCode || options[0]?.rack_code || "", options);
      if (!options.length) setMoveMessage("当前正式地图还没有可选的模具货架位置。请先完成层格校验和发布。");
    } catch (reason) {
      setMoveMessage(`读取正式模具位置失败：${(reason as Error).message}`);
    } finally {
      setMoveBusy(false);
    }
  };

  const previewMoldMove = async () => {
    if (!selectedMold || !selectedMoveTarget || moveBusy) return;
    setMoveBusy(true);
    setMoveMessage("");
    try {
      const preview = await mutateJson<MoldLocationMovePreview>(
        "/api/warehouse/molds/location-movement/preview",
        "POST",
        { mold_code: selectedMold.mold_code, target_location: selectedMoveTarget }
      );
      if (!preview) return;
      setMovePreview(preview);
      setMoveIdempotencyKey(operationKey("twin-mold-move"));
      setMoveMessage(preview.same_location
        ? "该模具已经在这个正式位置，无需移动。"
        : `目标位置已校验；${preview.co_located_count ? `当前同格还有 ${preview.co_located_count} 件模具。` : "当前没有同格模具。"}`
      );
    } catch (reason) {
      setMoveMessage(`目标位置不可用：${(reason as Error).message}`);
    } finally {
      setMoveBusy(false);
    }
  };

  const confirmMoldMove = async () => {
    if (!selectedMold || !movePreview || movePreview.same_location || !moveIdempotencyKey || moveBusy) return;
    if (!window.confirm(
      `请先确认模具实物已经搬动：\n\n模具：${selectedMold.mold_code} · ${selectedMold.mold_name}\n原位置：${selectedMold.rack_location}\n新位置：${movePreview.target_location}\n\n确认后将更新正式模具位置并追加不可变移位流水。`
    )) return;
    setMoveBusy(true);
    setMoveMessage("");
    try {
      const result = await mutateJson<MoldLocationMoveResponse>(
        "/api/warehouse/molds/location-movement/confirm",
        "POST",
        {
          mold_code: selectedMold.mold_code,
          target_location: movePreview.target_location,
          expected_version: movePreview.expected_version,
          idempotency_key: moveIdempotencyKey,
          source: "manual_input",
          note: "实测地图移货模式确认模具实物已搬动"
        }
      );
      if (!result) return;
      setMovePanelOpen(false);
      setMovePreview(null);
      setMoveIdempotencyKey("");
      setMoveAttemptUncertain(false);
      setMoveMessage(result.message);
      onMoldMoved(`${result.mold.mold_code} 已移动到 ${result.mold.rack_location}，正式位置和移位流水已同步。`);
    } catch (reason) {
      const requestError = reason as Error & { status?: number };
      if (requestError.status && requestError.status < 500) {
        setMovePreview(null);
        setMoveIdempotencyKey("");
        setMoveAttemptUncertain(false);
        setMoveMessage(`移动被拒绝：${requestError.message}。请刷新或重新预览目标位置。`);
      } else {
        setMoveAttemptUncertain(true);
        setMoveMessage(`移动结果暂不确定：${requestError.message}。请保持当前模具和目标不变，再点一次确认；系统会复用同一凭证核对。`);
      }
    } finally {
      setMoveBusy(false);
    }
  };

  return <section className="twin-rack-focus-panel twin-rack-stage twin-mold-rack-stage" role="region" aria-label={`${rack.rack_code} 模具资产正视图`}>
      <header>
        <div><small>实时模具货架 · {response?.rack.mold_rack_code || rack.mold_rack_code || rack.rack_code}</small><h2>{moldRackEmployeeName(rack)}</h2><p>{formatNumber(rack.width_mm)} × {formatNumber(rack.depth_mm)} × {formatNumber(rack.height_mm)} 毫米 · {rack.levels} 层 · 同区货架 {rackIndex + 1}/{rackCount}</p></div>
        <button type="button" disabled={moveAttemptUncertain} onClick={onClose}>{moveAttemptUncertain ? "请先核对移动结果" : "返回孪生地图"}</button>
      </header>
      {response?.rack.uses_legacy_bays && <div className="twin-mold-rack-legacy-note">当前发布地图仍沿用旧统一 {rack.bays || 1} 格结构；请在“区域规划”中补齐每层实际格数，发布前不会改动任何模具位置。</div>}
      <div className="twin-rack-content">
        <button type="button" className="twin-rack-switch previous" aria-label="上一个同区域货架" disabled={moveAttemptUncertain} onClick={onPrevious}>‹</button>
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
                    <button type="button" className="mold-rack-cell-summary" disabled={moveAttemptUncertain} onClick={() => { setSelectedSlotKey(key); setSelectedMoldId(null); setSelectedProductId(null); }}><b>第 {cell.grid} 格</b><strong>{cell.items.length ? `${cell.items.length} 件模具 · ${spines.length} 个产品书脊` : "空格"}</strong></button>
                    {spines.length ? <div className="mold-rack-book-spines">{spines.map((spine, index) => <button
                      type="button"
                      className={selectedMoldId === spine.mold_id && selectedProductId === spine.product_id ? "selected" : ""}
                      key={spine.key}
                      title={`${spine.code} · ${spine.name} · 模具 ${spine.mold_code}`}
                      disabled={moveAttemptUncertain}
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
          <small>实时模具台账</small>
          {loading && <><h3>正在读取正式模具台账…</h3><p>只读，不修改模具位置或绑定。</p></>}
          {!loading && error && <><h3>模具读取失败</h3><p className="error">{error}</p></>}
          {!loading && !error && selectedMold && <article className="twin-rack-product-label mold-label">
            <span>{selectedProduct ? "当前产品书脊" : "未绑定产品的模具"}</span><h3>{selectedProduct?.product_code || selectedMold.mold_code}</h3><strong>{selectedProduct?.product_name || selectedMold.mold_name}</strong>
            <dl>{selectedProduct && <div><dt>客户</dt><dd>{selectedProduct.customer_name || "客户待确认"}</dd></div>}<div><dt>关联模具</dt><dd>{selectedMold.mold_code} · {selectedMold.mold_name}</dd></div><div><dt>正式位置</dt><dd>{selectedMold.rack_location}</dd></div><div><dt>现场指引</dt><dd>{selectedMold.location_guide?.prompt || "位置指引待补充"}</dd></div><div><dt>关联产品</dt><dd>{selectedMold.product_count} 款</dd></div></dl>
            <div className="twin-mold-rack-selected-actions">
              <button type="button" className="twin-rack-detail-toggle" disabled={moveAttemptUncertain} onClick={() => { setSelectedMoldId(null); setSelectedProductId(null); }}>返回本格产品书脊</button>
              {canMoveMolds && <button type="button" className="move" disabled={moveBusy || moveAttemptUncertain} onClick={openMoldMove}>{movePanelOpen ? "重新选择目标位置" : "移动该模具"}</button>}
            </div>
            {movePanelOpen && <section className="twin-mold-move-panel">
              <div><b>移动 {selectedMold.mold_code}</b><small>只列出当前正式发布的模具货架层格；预览不会写入。</small></div>
              <label><span>目标货架</span><select value={moveRackCode} disabled={moveBusy || moveAttemptUncertain || !moveOptions.length} onChange={(event) => selectMoveRack(event.target.value)}>{moveOptions.map((option) => <option value={option.rack_code} key={option.rack_code}>{option.rack_code} · {option.name}</option>)}</select></label>
              {selectedMoveRack?.levels.length ? <label><span>目标层</span><select value={moveLevel} disabled={moveBusy || moveAttemptUncertain} onChange={(event) => {
                const value = event.target.value;
                const level = selectedMoveRack.levels.find((item) => String(item.level) === value) || null;
                setMoveLevel(value);
                setMoveGrid(level?.grids[0] ? String(level.grids[0]) : "");
                resetMovePreview();
              }}>{selectedMoveRack.levels.map((level) => <option value={level.level} key={level.level}>第 {level.level} 层</option>)}</select></label> : null}
              {selectedMoveLevel?.grids.length ? <label><span>目标格</span><select value={moveGrid} disabled={moveBusy || moveAttemptUncertain} onChange={(event) => { setMoveGrid(event.target.value); resetMovePreview(); }}>{selectedMoveLevel.grids.map((grid) => <option value={grid} key={grid}>第 {grid} 格</option>)}</select></label> : null}
              <div className="twin-mold-move-target"><span>目标正式位置</span><b>{selectedMoveTarget || "当前没有可用位置"}</b></div>
              {moveMessage && <p className={moveMessage.includes("失败") || moveMessage.includes("不可用") || moveMessage.includes("未完成") ? "error" : ""}>{moveMessage}</p>}
              <div className="twin-mold-move-actions">
                <button type="button" disabled={moveBusy || moveAttemptUncertain || !selectedMoveTarget} onClick={previewMoldMove}>{moveBusy ? "处理中…" : "预览并校验目标"}</button>
                <button type="button" className="confirm" disabled={moveBusy || !movePreview || movePreview.same_location} onClick={confirmMoldMove}>{moveAttemptUncertain ? "用原凭证核对移动结果" : "确认实物已搬动并保存"}</button>
              </div>
            </section>}
            <div className="twin-mold-product-links">{selectedMold.products.length ? selectedMold.products.map((product) => <button type="button" className={selectedProductId === product.id ? "selected" : ""} key={product.id} onClick={() => setSelectedProductId(product.id)}><b>{product.customer_name || "客户待确认"}</b><span>{product.product_code || "无存货编码"} · {product.product_name || "产品名称待补充"}</span></button>) : <p>当前模具尚未绑定产品。</p>}</div>
          </article>}
          {!loading && !error && !selectedMold && <>
            <h3>{selectedSlot ? `第 ${selectedSlot.level} 层 · 第 ${selectedSlot.grid} 格` : "当前货架模具"}</h3>
            <p>同一格可登记多件模具；货架按绑定产品显示为书脊。点击存货编码或名称，右侧查看产品、模具和正式位置。同格顺序只为阅读，不代表现场左右次序。</p>
            <strong>{selectedSlot ? `${selectedSlot.items.length} 件模具 · ${visibleSpines.length} 个书脊` : `${response?.total || 0} 件模具 · ${visibleSpines.length} 个书脊`}{unmatchedCount ? ` · ${unmatchedCount} 件未精确到当前格` : ""}</strong>
            <div className="twin-mold-rack-item-list">{visibleSpines.map((spine) => <button type="button" disabled={moveAttemptUncertain} key={spine.key} onClick={() => { setSelectedMoldId(spine.mold_id); setSelectedProductId(spine.product_id); }}><b>{spine.code}</b><span>{spine.name}</span><small>模具 {spine.mold_code} · {spine.rack_location}</small></button>)}</div>
            {response?.truncated && <p className="error">该货架超过 500 件，本页只显示前 500 件；请按模具编号查找其精确位置。</p>}
            {unmatchedCount > 0 && <p className="twin-mold-rack-warning">未精确到当前格的模具仍保留在台账中；调整层格不会自动搬动它们。</p>}
          </>}
        </aside>
        <button type="button" className="twin-rack-switch next" aria-label="下一个同区域货架" disabled={moveAttemptUncertain} onClick={onNext}>›</button>
      </div>
  </section>;
}

function WarehouseRackElevation({
  rack,
  area,
  locations,
  unboundLocationCount,
  canChooseProducts,
  rackIndex,
  rackCount,
  onPrevious,
  onNext,
  onChooseEmptyLocation,
  onClose
}: {
  rack: Rack;
  area?: AreaDistribution;
  locations: DashboardLocation[];
  unboundLocationCount: number;
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
  const rackCells = useMemo(() => {
    const grouped = new Map<string, DashboardLocation[]>();
    for (const location of locations) {
      if (location.map_rack_id !== rack.id) continue;
      const key = rackCellIdentityKey(location.map_rack_id, location.level_no, location.slot_no);
      if (!key) continue;
      grouped.set(key, [...(grouped.get(key) || []), location]);
    }
    return grouped;
  }, [locations, rack.id]);
  const items = useMemo(
    () => locations.flatMap((location) => rackLocationInventoryItems(location)),
    [locations]
  );
  const emptyLocationCount = useMemo(
    () => locations.filter((location) => rackLocationInventoryItems(location).length === 0).length,
    [locations]
  );
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
  return <section className="twin-rack-focus-panel twin-rack-stage" role="region" aria-label={`${rack.rack_code} 参数化正视图`}>
      <header>
      <div><small>仓储货架正视图</small><h2>{moldRackEmployeeName(rack)}</h2><p>{formatNumber(rack.width_mm)} × {formatNumber(rack.depth_mm)} × {formatNumber(rack.height_mm)} mm · {rack.levels} 层 · 同区货架 {rackIndex + 1}/{rackCount}</p></div>
        <button type="button" onClick={onClose}>返回孪生地图</button>
      </header>
      <div className="twin-rack-content">
        <button type="button" className="twin-rack-switch previous" aria-label="上一个同区域货架" onClick={onPrevious}>‹</button>
        <div className="twin-elevation-shell">
          <div className="twin-height-ruler"><b>{formatNumber(rack.height_mm)} mm</b></div>
          <div className="twin-elevation-frame">
            {levels.map((level) => {
              const cellCount = levelCellCounts[level - 1] || 0;
              return <div className="twin-elevation-level" key={level}>
              <span>第 {level} 层 · {cellCount ? `${cellCount} 格` : "尚未分格"}</span>
              <div className={cellCount ? "" : "unpartitioned"}>{cellCount === 0 ? <i className="twin-unpartitioned-cell">本层尚未分格</i> : Array.from({ length: cellCount }, (_, bay) => {
                const cellKey = rackCellIdentityKey(rack.id, level, bay + 1);
                const cellLocations = cellKey ? rackCells.get(cellKey) || [] : [];
                const cellItems = cellLocations.flatMap((location) => rackLocationInventoryItems(location));
                const location = cellLocations.length === 1 ? cellLocations[0] : null;
                const identityConflict = cellLocations.length > 1;
                let blockReason: string | null = null;
                if (location && cellItems.length === 0) {
                  const finishedBlock = stocktakeAddBlockReason(location, "finished");
                  const semiFinishedBlock = stocktakeAddBlockReason(location, "semi_finished");
                  const nextBlockReason = !canChooseProducts
                    ? "进入盘点调整后才可选择产品。"
                    : finishedBlock && semiFinishedBlock
                      ? finishedBlock
                      : null;
                  blockReason = nextBlockReason;
                }
                const cellSelected = cellItems.some((item) => item.lot_id === selectedItem?.lot_id);
                const cellTitle = identityConflict
                  ? `该层格关联 ${cellLocations.length} 个正式货位，请管理员处理身份冲突。`
                  : location?.location_name || "暂无已建空货位";
                return <section className={`mold-rack-cell ${cellItems.length ? "occupied" : "empty"} ${cellSelected ? "selected" : ""}`} key={cellKey || `${rack.id}-${level}-${bay + 1}`} title={cellTitle}>
                  <button
                    type="button"
                    className="mold-rack-cell-summary"
                    disabled={!cellItems.length && (identityConflict || !location || Boolean(blockReason))}
                    onClick={() => {
                      if (cellItems.length) {
                        setSelectedItem(cellItems[0]);
                        setDetailOpen(false);
                      } else if (location && !blockReason) {
                        onChooseEmptyLocation(location.location_id);
                      }
                    }}
                  ><b>第 {bay + 1} 格</b><strong>{identityConflict ? "货位身份冲突" : cellItems.length ? `${cellItems.length} 个批次` : location ? "正式空货位" : "未建正式货位"}</strong></button>
                  {cellItems.length ? <div className="mold-rack-book-spines">{cellItems.map((item, index) => <button
                    type="button"
                    className={selectedItem?.lot_id === item.lot_id ? "selected" : ""}
                    key={`${item.lot_id}-${item.location_code || "unknown"}`}
                    title={`${item.inventory_code || item.lot_number || `批次 ${item.lot_id}`} · ${item.product_name || "产品名称待补充"} · ${employeeCustomerName(item)} · ${formatNumber(inventoryLabelQuantity(item))} ${inventoryUnitLabel(item.unit)}`}
                    onClick={() => { setSelectedItem(item); setDetailOpen(false); }}
                  ><small>{index + 1}</small><b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b><span>{employeeCustomerName(item)} · {formatNumber(inventoryLabelQuantity(item))} {inventoryUnitLabel(item.unit)}</span></button>)}</div> : <span className="mold-rack-empty-spine">{identityConflict ? "请管理员确认唯一正式货位" : location ? blockReason || "＋ 为此货位选产品" : "暂无已建空货位"}</span>}
                </section>;
              })}</div>
            </div>})}
          </div>
          <div className="twin-width-ruler">正面宽度 {formatNumber(rack.width_mm)} mm</div>
        </div>
        <aside>
          <small>产品标签</small>
          {!selectedItem ? <><h3>点击货架上的产品或空货位</h3><p>每一格严格读取该货架、层号和格号；同格所有批次均保留并可逐个查看。</p><strong>{items.length} 个批次 · {emptyLocationCount} 个正式空货位</strong>{unboundLocationCount > 0 && <p className="twin-mold-rack-warning">本区域还有 {unboundLocationCount} 个有货旧货位未绑定货架层格，请先转入盘点待归位。</p>}{area?.quantities.map((item) => <div className="twin-quantity-row" key={item.key}><span>{item.label}</span><b>{formatNumber(item.available)} {inventoryUnitLabel(item.unit)}</b></div>)}</> : <article className="twin-rack-product-label">
            <span>当前产品标签</span>
            <h3>{selectedItem.inventory_code || selectedItem.lot_number || `批次 ${selectedItem.lot_id}`}</h3>
            <strong>{selectedItem.product_name || "产品名称待补充"}</strong>
            <dl><div><dt>客户</dt><dd>{selectedItem.customer_name || "待确认"}</dd></div><div><dt>产品数量</dt><dd>{formatNumber(inventoryLabelQuantity(selectedItem))} {inventoryUnitLabel(selectedItem.unit)}</dd></div></dl>
            <button type="button" className="twin-rack-detail-toggle" aria-expanded={detailOpen} onClick={() => setDetailOpen((value) => !value)}>{detailOpen ? "收起详情" : "查看详情"}</button>
            {detailOpen && <dl className="twin-rack-product-detail"><div><dt>可用数量</dt><dd>{formatNumber(selectedItem.available_quantity)} {inventoryUnitLabel(selectedItem.unit)}</dd></div><div><dt>已预占</dt><dd>{formatNumber(selectedItem.reserved_quantity)} {inventoryUnitLabel(selectedItem.unit)}</dd></div><div><dt>实际位置</dt><dd>{selectedItem.location_name || "位置名称待完善"}</dd></div><div><dt>存放方式</dt><dd>{selectedItem.pallet_code ? "已绑定实物栈板" : "地堆或散存"}</dd></div><div><dt>批次</dt><dd>{selectedItem.lot_number || "—"}</dd></div></dl>}
          </article>}
        </aside>
        <button type="button" className="twin-rack-switch next" aria-label="下一个同区域货架" onClick={onNext}>›</button>
      </div>
  </section>;
}

export function WarehouseTwinApp() {
  const query = useMemo(() => new URLSearchParams(window.location.search), []);
  const embedded = query.get("embedded") === "1";
  const traceReadOnly = query.get("readonly") === "1" && query.get("source") === "order_trace";
  const [floorCode, setFloorCode] = useState<WarehouseOperationalFloorCode>(() => {
    const requested = query.get("floor")?.toUpperCase();
    return isWarehouseOperationalFloorCode(requested) ? requested : "3F";
  });
  const [viewMode, setViewMode] = useState<ViewMode>(query.get("view") === "25d" ? "25d" : "2d");
  const [cameraPreset, setCameraPreset] = useState<CameraPreset>("fit");
  const [viewResetToken, setViewResetToken] = useState(0);
  const [layout, setLayout] = useState<Layout | null>(null);
  const [planningPublishedLayout, setPlanningPublishedLayout] = useState<Layout | null>(null);
  const [layoutStandardPallet, setLayoutStandardPallet] = useState<StandardPalletContract | null>(null);
  const [layoutDraftControl, setLayoutDraftControl] = useState<LayoutDraftControl | null>(null);
  const geometryApplyRequestRef = useRef<{ signature: string; operationKey: string } | null>(null);
  const rackApplyRequestRef = useRef<{ signature: string; operationKey: string } | null>(null);
  const [legacyRackBindingPreview, setLegacyRackBindingPreview] = useState<LegacyRackBindingPreview | null>(null);
  const [legacyRackBindingSelections, setLegacyRackBindingSelections] = useState<Record<string, string>>({});
  const [publishedFloorRevision, setPublishedFloorRevision] = useState("");
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
  const [pendingLotId, setPendingLotId] = useState<number | null>(() => {
    const requested = Number(query.get("lot_id") || 0);
    return requested > 0 ? requested : null;
  });
  const [traceFocusedLotId, setTraceFocusedLotId] = useState<number | null>(null);
  const [traceDeepLinkMessage, setTraceDeepLinkMessage] = useState(
    traceReadOnly ? "订单追溯只读定位：正在核对实际位置和成品批次…" : ""
  );
  const [areaInventorySearch, setAreaInventorySearch] = useState("");
  const [areaInventoryDetailsOpen, setAreaInventoryDetailsOpen] = useState(false);
  const [moldAreaResponse, setMoldAreaResponse] = useState<MoldAreaResponse | null>(null);
  const [moldAreaPage, setMoldAreaPage] = useState(1);
  const [moldAreaLoading, setMoldAreaLoading] = useState(false);
  const [moldAreaError, setMoldAreaError] = useState("");
  const [moldRackResponse, setMoldRackResponse] = useState<MoldRackResponse | null>(null);
  const [moldRackLoading, setMoldRackLoading] = useState(false);
  const [moldRackError, setMoldRackError] = useState("");
  const [moldRackRefreshToken, setMoldRackRefreshToken] = useState(0);
  const [layerPanelOpen, setLayerPanelOpen] = useState(false);
  const [searchPanelOpen, setSearchPanelOpen] = useState(true);
  const [mapMode, setMapMode] = useState<WarehouseMapMode>(() => {
    if (traceReadOnly) return "lookup";
    const requested = query.get("mode");
    return requested === "move" || requested === "planning" ? requested : "lookup";
  });
  const [productionPanelOpen, setProductionPanelOpen] = useState(false);
  const [pendingAreaCode, setPendingAreaCode] = useState<string | null>(() => query.get("area_code")?.trim().toUpperCase() || null);
  const [pendingMapFeatureId, setPendingMapFeatureId] = useState<string | null>(() => query.get("map_feature_id")?.trim() || null);
  const [pendingRackId, setPendingRackId] = useState<string | null>(() => query.get("rack_id")?.trim() || null);
  const [pendingAreaPolicyEdit, setPendingAreaPolicyEdit] = useState(
    !traceReadOnly && query.get("edit") === "area_policy"
  );
  const [pendingRackEdit, setPendingRackEdit] = useState(
    !traceReadOnly && query.get("edit") === "rack"
  );
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
  const [canStocktake, setCanStocktake] = useState(false);
  const [canViewProductionProjection, setCanViewProductionProjection] = useState(false);
  const [uiMode, setUiMode] = useState<"standard" | "large">("standard");
  const [locationEditMode, setLocationEditMode] = useState(false);
  const [areaPolicyEditMode, setAreaPolicyEditMode] = useState(false);
  const [locationDrafts, setLocationDrafts] = useState<Record<number, LocationLayoutGeometry>>({});
  const locationNudgeRef = useRef<{ id: string; x: number; y: number } | null>(null);
  const [keyboardLocationEditActive, setKeyboardLocationEditActive] = useState(false);
  useEffect(() => {
    if (mapMode !== "planning") { setKeyboardLocationEditActive(false); locationNudgeRef.current = null; }
  }, [mapMode]);
  const [rackDrafts, setRackDrafts] = useState<Record<string, RackDraft>>({});
  const [newRackFormOpen, setNewRackFormOpen] = useState(false);
  const [newRackSettings, setNewRackSettings] = useState({ width: "", depth: "", height: "", levels: "3", cells: "" });
  const [zonePolicyDrafts, setZonePolicyDrafts] = useState<Record<string, { allowed_inventory_types: InventoryUsage[]; storage_layout: StorageLayout }>>({});
  const [zoneGeometryDrafts, setZoneGeometryDrafts] = useState<Record<string, number[][]>>({});
  const zoneGeometryDraftsRef = useRef<Record<string, number[][]>>({});
  const replaceZoneGeometryDrafts = (
    updater: Record<string, number[][]> | ((current: Record<string, number[][]>) => Record<string, number[][]>)
  ) => {
    const next = typeof updater === "function"
      ? updater(zoneGeometryDraftsRef.current)
      : updater;
    zoneGeometryDraftsRef.current = next;
    setZoneGeometryDrafts(next);
  };
  const [layoutMapToolsOpen, setLayoutMapToolsOpen] = useState(false);
  const [mapHelpOpen, setMapHelpOpen] = useState(false);
  const [objectActions, setObjectActions] = useState<{
    entity: NonNullable<SelectedEntity>; clientX: number; clientY: number;
  } | null>(null);
  const objectActionsRef = useRef<HTMLDivElement>(null);
  const objectActionTriggerRef = useRef<HTMLElement | null>(null);
  const inspectorRef = useRef<HTMLElement>(null);
  const [layoutMapTool, setLayoutMapTool] = useState<"adjust" | "zone">("adjust");
  const [featureContextMenu, setFeatureContextMenu] = useState<{
    featureId: string;
    clientX: number;
    clientY: number;
  } | null>(null);
  const [layoutDrawPoints, setLayoutDrawPoints] = useState<number[][]>([]);
  const [floor4CalibrationMode, setFloor4CalibrationMode] = useState(false);
  const [floor4CalibrationPoints, setFloor4CalibrationPoints] = useState<number[][]>([]);
  const [floor4CalibrationApplied, setFloor4CalibrationApplied] = useState(false);
  const [floor4CalibrationOperationKey, setFloor4CalibrationOperationKey] = useState(() => operationKey("floor4-calibration"));
  const [spatialEditBusy, setSpatialEditBusy] = useState(false);
  const [locationEditBusy, setLocationEditBusy] = useState(false);
  const [locationEditMessage, setLocationEditMessage] = useState("");
  const [staleLayoutDraft, setStaleLayoutDraft] = useState(false);
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
  const locationLayoutOperationRef = useRef<{ signature: string; key: string } | null>(null);
  const [delayedDispatchOpen, setDelayedDispatchOpen] = useState(false);
  const [formalAreaOptions, setFormalAreaOptions] = useState<FormalWarehouseAreaOption[]>([]);
  const [selectedExistingAreaId, setSelectedExistingAreaId] = useState("");
  const [formalAreaOptionsError, setFormalAreaOptionsError] = useState("");
  const [floor1CandidatePlan, setFloor1CandidatePlan] = useState<Floor1FormalCandidatePlan | null>(null);
  const [floor1CandidateBusy, setFloor1CandidateBusy] = useState(false);
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
  const [moveAction, setMoveAction] = useState<"relocate" | "merge" | "stocktake" | "ground">(
    query.get("action") === "stocktake" ? "stocktake" : "relocate"
  );
  const [mergeSources, setMergeSources] = useState<PalletMergeCandidate[]>([]);
  const [mergeTarget, setMergeTarget] = useState<PalletMergeCandidate | null>(null);
  const [mergeCustomerId, setMergeCustomerId] = useState("");
  const [mergeKeyword, setMergeKeyword] = useState("");
  const [mergeBatchIdempotencyKey, setMergeBatchIdempotencyKey] = useState(() => operationKey("warehouse-pallet-merge-batch"));
  const [mergeBatchBusy, setMergeBatchBusy] = useState(false);
  const [locationDetailOpen, setLocationDetailOpen] = useState(false);
  const [locationItemsExpanded, setLocationItemsExpanded] = useState(false);
  const [stocktakeDrafts, setStocktakeDrafts] = useState<WarehouseStocktakeDraft[]>([]);
  const [stocktakeBatchIdempotencyKey, setStocktakeBatchIdempotencyKey] = useState(() => operationKey("warehouse-stocktake-batch"));
  const [stocktakeBatchBusy, setStocktakeBatchBusy] = useState(false);
  const [stocktakeRefreshRequired, setStocktakeRefreshRequired] = useState(false);
  const [stocktakeInventoryType, setStocktakeInventoryType] = useState<StocktakeInventoryType>("finished");
  const [stocktakeSourceKind, setStocktakeSourceKind] = useState<"existing_stocktake" | "partner_transfer">("existing_stocktake");
  const [stocktakeCustomerQuery, setStocktakeCustomerQuery] = useState("");
  const [stocktakeCustomers, setStocktakeCustomers] = useState<CustomerOption[]>([]);
  const [stocktakeCustomerId, setStocktakeCustomerId] = useState("");
  const [stocktakeProductQuery, setStocktakeProductQuery] = useState("");
  const [stocktakeProductCandidates, setStocktakeProductCandidates] = useState<ProductCandidate[]>([]);
  const [stocktakeProductId, setStocktakeProductId] = useState("");
  const [stocktakeAddQuantity, setStocktakeAddQuantity] = useState("");
  const [stocktakeSupplementConfirmed, setStocktakeSupplementConfirmed] = useState(false);
  const [stocktakeStockDate, setStocktakeStockDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [rackFocusId, setRackFocusId] = useState<string | null>(null);
  const [stocktakeLotId, setStocktakeLotId] = useState<number | null>(null);
  const [pendingQuery, setPendingQuery] = useState("");
  const [recountLotId, setRecountLotId] = useState<number | null>(null);
  const [pendingQuantity, setPendingQuantity] = useState("");
  const [pendingPlacementBusy, setPendingPlacementBusy] = useState(false);
  const [pendingRefreshRequired, setPendingRefreshRequired] = useState(false);
  const pendingPlacementRef = useRef<{ busy: boolean; signature: string; key: string }>({ busy: false, signature: "", key: "" });
  const [stocktakeDecreaseQuantity, setStocktakeDecreaseQuantity] = useState("");
  const [stocktakeLastResult, setStocktakeLastResult] = useState<StocktakeBatchResultItem[]>([]);
  const [groundOperation, setGroundOperation] = useState<"inbound" | "transfer">("inbound");
  const [groundCustomerQuery, setGroundCustomerQuery] = useState("");
  const [groundCustomers, setGroundCustomers] = useState<CustomerOption[]>([]);
  const [groundCustomerId, setGroundCustomerId] = useState("");
  const [groundProductQuery, setGroundProductQuery] = useState("");
  const [groundProducts, setGroundProducts] = useState<ProductCandidate[]>([]);
  const [groundProductId, setGroundProductId] = useState("");
  const [groundQuantity, setGroundQuantity] = useState("");
  const [groundCapacityQuantity, setGroundCapacityQuantity] = useState("");
  const [groundStockDate, setGroundStockDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [groundLargeFootprint, setGroundLargeFootprint] = useState(false);
  const [groundCandidates, setGroundCandidates] = useState<GroundStorageCandidatesResponse | null>(null);
  const [groundPrimaryLocationId, setGroundPrimaryLocationId] = useState<number | null>(null);
  const [groundSecondaryLocationId, setGroundSecondaryLocationId] = useState<number | null>(null);
  const [groundTransferSource, setGroundTransferSource] = useState<WarehouseMoveSource | null>(null);
  const [groundStorageBusy, setGroundStorageBusy] = useState(false);
  const [groundStorageMessage, setGroundStorageMessage] = useState("");
  const [groundStorageIdempotencyKey, setGroundStorageIdempotencyKey] = useState(() => operationKey("ground-storage"));
  const [dispatchIdleDays, setDispatchIdleDays] = useState(3);
  const layoutDrawKind = locationEditMode && layoutMapToolsOpen && layoutMapTool !== "adjust"
    ? layoutMapTool
    : null;

  useEffect(() => {
    setLayoutMapToolsOpen(false);
    setNewRackFormOpen(false);
    setKeyboardLocationEditActive(false);
    setLayoutMapTool("adjust");
    setLayoutDrawPoints([]);
    setFloor4CalibrationMode(false);
    setFloor4CalibrationPoints([]);
    setFeatureContextMenu(null);
  }, [floorCode, locationEditMode]);

  useEffect(() => {
    if (!featureContextMenu) return;
    const closeMenu = () => setFeatureContextMenu(null);
    const closeMenuOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeMenu();
    };
    window.addEventListener("pointerdown", closeMenu);
    window.addEventListener("keydown", closeMenuOnEscape);
    return () => {
      window.removeEventListener("pointerdown", closeMenu);
      window.removeEventListener("keydown", closeMenuOnEscape);
    };
  }, [featureContextMenu]);

  const refreshDashboard = useCallback(async () => {
    const params = new URLSearchParams({
      days: "30",
      dispatch_idle_days: String(dispatchIdleDays)
    });
    const value = await requestJson<TwinDashboard>(`/api/warehouse/twin-dashboard/overview?${params.toString()}`);
    setDashboard(value);
    return value;
  }, [dispatchIdleDays]);

  useEffect(() => {
    refreshDashboard().catch((reason: Error) => setError(reason.message));
    requestJson<AuthResponse>("/api/auth/me")
      .then((value) => {
        setCanEditLocations(!traceReadOnly && value.user.role === "admin");
        setCanExecuteWarehouse(!traceReadOnly && value.permissions.includes("warehouse.execute"));
        setCanStocktake(!traceReadOnly && value.permissions.includes("warehouse.stocktake.submit"));
        setCanViewProductionProjection(value.permissions.includes("warehouse.view"));
        setUiMode(value.user.ui_mode === "large" ? "large" : "standard");
      })
      .catch(() => {
        setCanEditLocations(false);
        setCanExecuteWarehouse(false);
        setCanStocktake(false);
        setCanViewProductionProjection(false);
      });
  }, [refreshDashboard, traceReadOnly]);

  useEffect(() => {
    if (viewMode === "25d") {
      setMapMode("lookup");
      setSearchPanelOpen(true);
      setLocationEditMode(false);
      setAreaPolicyEditMode(false);
      setRackDrafts({});
      replaceZoneGeometryDrafts({});
      setSwapSourceLocationId(null);
    }
  }, [viewMode]);

  useEffect(() => {
    if (mapMode !== "move") return;
    if (moveAction === "stocktake" && !canStocktake && canExecuteWarehouse) setMoveAction("relocate");
    if (moveAction !== "stocktake" && !canExecuteWarehouse && canStocktake) setMoveAction("stocktake");
  }, [mapMode, moveAction, canExecuteWarehouse, canStocktake]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    setSelected(null);
    setRackDrafts({});
    setZonePolicyDrafts({});
    replaceZoneGeometryDrafts({});
    setLayoutDraftControl(null);
    setPublishedFloorRevision("");
    setPlanningPublishedLayout(null);
    setMapMode((current) => traceReadOnly
      ? "lookup"
      : current === "move" || (current === "planning" && (pendingAreaPolicyEdit || pendingRackEdit)) ? current : "lookup");
    setSearchPanelOpen(true);
    setLocationEditMode(false);
    setAreaPolicyEditMode(false);
    setFloor1CandidatePlan(null);
    setLayoutStandardPallet(null);
    setFloor4CalibrationApplied(false);
    requestJson<TwinFloorResponse>(`/api/warehouse/twin-layout/floors/${floorCode}`)
      .then((raw) => {
        if (!active) return;
        const hydrated = hydrateLayout(raw);
        setLayout(hydrated);
        setPlanningPublishedLayout(hydrated);
        setLayoutStandardPallet(normalizeStandardPalletContract(raw.standard_pallet));
        setAssets(raw.assets || []);
        setFloor4CalibrationApplied(isFloor4Aligned(raw));
        setPublishedFloorRevision(raw.revision);
      })
      .catch((reason: Error) => active && setError(reason.message))
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [floorCode, traceReadOnly]);

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
      "/api/warehouse/location-candidates?inventory_type=finished&empty_only=true&pallet_storage_only=false&include_hierarchy=true&published_only=true"
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

  useEffect(() => {
    if (!layout || layout.floor_code !== floorCode || !pendingRackId) return;
    const target = layout.racks.find((rack) => rack.id === pendingRackId);
    if (target) {
      setSelected({ kind: "rack", id: target.id });
      cameraFocusSequenceRef.current += 1;
      setCameraFocusTarget({ entity: { kind: "rack", id: target.id }, token: cameraFocusSequenceRef.current, source: "search" });
    }
    setPendingRackId(null);
  }, [layout, floorCode, pendingRackId]);

  // Browsing any mode uses the operational layout. Drafts are visible only
  // while the user has explicitly opened a layout/slot/rack editing action.
  const planningPreviewActive = mapMode === "planning" && (layoutMapToolsOpen
    || Boolean(locationPointEditAreaCode) || keyboardLocationEditActive || advancedAreaMaintenanceOpen
    || newRackFormOpen || Object.keys(rackDrafts).length > 0);
  const displayBaseLayout = planningPublishedLayout || layout;
  const features = stableTwinFeatures(layout) as TwinFeature[];
  const planningPublishedFeatures = useMemo(
    () => stableTwinFeatures(planningPublishedLayout) as TwinFeature[],
    [planningPublishedLayout]
  );
  const locationProjectionFeatures = useMemo(
    () => mergePublishedFeatureGeometry(features, planningPublishedFeatures) as TwinFeature[],
    [features, planningPublishedFeatures]
  );
  const activeEditingFeatureId = planningPreviewActive
    ? (selected?.kind === "feature" ? selected.id : locationPointEditAreaCode
      ? features.find((feature) => feature.feature_kind === "zone" && feature.erp_area_code === locationPointEditAreaCode)?.id
      : null)
    : null;
  const activeRackPreviewId = planningPreviewActive && selected?.kind === "rack" ? selected.id : null;
  const activeObjectPreview = Boolean(activeEditingFeatureId || activeRackPreviewId);
  const planningVisibleFeatures = useMemo(() => {
    const published = filterPlanningPublishedFeatures(displayBaseLayout, layout) as TwinFeature[];
    if (!activeEditingFeatureId) return published;
    const edited = features.find((feature) => feature.id === activeEditingFeatureId);
    if (!edited || edited.feature_kind === "aisle") return published;
    const visibleEdited = zoneGeometryDrafts[edited.id]
      ? { ...edited, points: zoneGeometryDrafts[edited.id] }
      : edited;
    return published.some((feature) => feature.id === edited.id)
      ? published.map((feature) => feature.id === edited.id ? { ...feature, ...visibleEdited } : feature)
      : [...published, visibleEdited];
  }, [features, zoneGeometryDrafts, activeEditingFeatureId, displayBaseLayout, layout]);
  const locationCollisionStructures = planningPublishedLayout?.structures || layout?.structures || [];
  const locationCollisionPlacements = planningPublishedLayout?.placements || layout?.placements || [];
  const locationCollisionRacks = planningPublishedLayout?.racks || layout?.racks || [];
  const planningCollisionStructures = displayBaseLayout?.structures || [];
  const planningCollisionPlacements = displayBaseLayout?.placements || [];
  const planningCollisionRacks = useMemo(() => {
    const published = [...(displayBaseLayout?.racks || [])];
    const activeId = activeRackPreviewId;
    if (!activeId) return published;
    const edited = rackDrafts[activeId] || layout?.racks.find((rack) => rack.id === activeId);
    if (!edited) return published;
    return published.some((rack) => rack.id === activeId)
      ? published.map((rack) => rack.id === activeId ? edited : rack)
      : [...published, edited];
  }, [displayBaseLayout?.racks, rackDrafts, activeRackPreviewId, layout?.racks]);
  const standardPallet = useMemo(
    () => standardPalletContractsMatch(layoutStandardPallet, dashboard?.standard_pallet)
      ? normalizeStandardPalletContract(layoutStandardPallet)
      : null,
    [layoutStandardPallet, dashboard?.standard_pallet]
  );
  const standardPalletError = standardPalletDisplayIssue({
    loading,
    dashboardReady: Boolean(dashboard),
    requestedFloorCode: floorCode,
    layoutFloorCode: layout?.floor_code,
    layoutContract: layoutStandardPallet,
    dashboardContract: dashboard?.standard_pallet
  });
  const warehouseMoveModeActive = mapMode === "move" && moveAction === "relocate" && canExecuteWarehouse && viewMode === "2d";
  const currentFloor = dashboard?.floors.find((item) => item.floor_code === floorCode);
  const visualLocations = useMemo<DashboardLocation[]>(() => (dashboard?.locations || []).map((location) => {
    const projectedLocation = normalizeInventoryLocationProjection(location) as DashboardLocation;
    const zone = locationProjectionFeatures.find((feature) => feature.id === location.map_feature_id
      || (feature.feature_kind === "zone" && feature.erp_area_code === location.area_code));
    const activeSaved = planningPreviewActive ? features.filter((feature) => feature.id === activeEditingFeatureId).flatMap((feature) =>
      feature.ground_location_draft?.slots || []).find((slot) => slot.location_id === location.location_id) : undefined;
    const saved = activeSaved || zone?.ground_location_draft?.slots.find((slot) => slot.location_id === location.location_id);
    const bounds = zone ? pointsBoundsMm(zone.points) : null;
    const savedPosition = saved && bounds ? {
      location_id: saved.location_id, expected_version: saved.expected_version,
      left_pct: (saved.x_mm - (bounds.centerXmm - bounds.widthMm / 2)) / bounds.widthMm * 100,
      top_pct: ((bounds.centerYmm + bounds.heightMm / 2) - saved.y_mm - saved.depth_mm) / bounds.heightMm * 100,
      width_pct: saved.width_mm / bounds.widthMm * 100, height_pct: saved.depth_mm / bounds.heightMm * 100,
      z_index: Number(location.map_position?.z_index || 0)
    } : undefined;
    const draft = planningPreviewActive && zone?.id === activeEditingFeatureId
      ? locationDrafts[location.location_id] || savedPosition
      : savedPosition;
    return draft ? {
      ...projectedLocation,
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
    } : projectedLocation;
  }), [dashboard?.locations, locationDrafts, planningPreviewActive, activeEditingFeatureId, features, locationProjectionFeatures, planningPublishedLayout]);
  const currentFloorOccupiedLocations = useMemo(
    () => visualLocations.filter((location) => (
      location.floor_code === floorCode && location.occupancy_status === "occupied"
    )).length,
    [visualLocations, floorCode]
  );
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
    () => buildMappedLocationPallets(
      locationProjectionFeatures,
      visualLocations,
      floorCode,
      standardPallet,
      layout?.id,
      locationEditMode
    ),
    [locationProjectionFeatures, visualLocations, floorCode, standardPallet, layout?.id, locationEditMode]
  );
  // Moving the boundary never reinterprets existing location percentages.
  // Draft locations keep absolute positions until the atomic map application.
  const planningPreviewPallets = mappedLocationPallets;
  const previewOnlyLocationIds = useMemo(() => new Set<string>(), []);
  const planningCollisionPallets = useMemo(
    () => planningPreviewPallets.filter((item) => item.is_planning_location_slot),
    [planningPreviewPallets]
  );
  const operationalColumnConflicts = useMemo(
    () => layout ? findPalletColumnConflicts(
      mappedLocationPallets,
      locationCollisionStructures,
      locationProjectionFeatures,
      0
    ) : [],
    [mappedLocationPallets, locationCollisionStructures, locationProjectionFeatures, layout]
  );
  const planningGeometryConflicts = useMemo(
    () => layout ? findPalletPlanningConflicts(
      planningCollisionPallets,
      planningCollisionStructures,
      planningVisibleFeatures,
      0,
      planningCollisionPlacements,
      planningCollisionRacks
    ) : [],
    [locationEditMode, planningCollisionPallets, planningCollisionStructures, planningVisibleFeatures, planningCollisionPlacements, planningCollisionRacks, layout]
  );
  // The same footprint/obstacle rules color each mode's effective layout.
  // Operational write guards remain separate from these visual diagnostics.
  const displayedLocationConflicts = planningGeometryConflicts;
  const operationalColumnConflictIds = useMemo(
    () => new Set(operationalColumnConflicts.map((item) => item.pallet_id)),
    [operationalColumnConflicts]
  );
  const displayedLocationConflictIds = useMemo(
    () => new Set(displayedLocationConflicts.map((item) => item.pallet_id)),
    [displayedLocationConflicts]
  );
  const columnConflictLocationIds = useMemo(
    () => operationalColumnConflicts
      .map((item) => Number(item.pallet_id.replace("erp-location-", "")))
      .filter((locationId) => Number.isFinite(locationId) && locationId > 0),
    [operationalColumnConflicts]
  );
  const operationalColumnConflictCount = useMemo(
    () => uniquePalletConflictCount(operationalColumnConflicts),
    [operationalColumnConflicts]
  );
  const moveReservedTargetIds = useMemo(
    () => moveDrafts
      .filter((item) => item.source_key !== moveSource?.source_key)
      .map((item) => item.target_location_id),
    [moveDrafts, moveSource?.source_key]
  );
  const eligibleMoveCandidates = useMemo(
    () => moveSource?.operation === "pallet_move"
      ? moveCandidates.filter((item) => String(item.storage_type || "").toLowerCase() !== "rack")
      : moveCandidates,
    [moveCandidates, moveSource?.operation]
  );
  const mappedMoveTargets = useMemo(
    () => intersectMappedMoveTargets(eligibleMoveCandidates, visualLocations, moveReservedTargetIds, columnConflictLocationIds),
    [eligibleMoveCandidates, visualLocations, moveReservedTargetIds, columnConflictLocationIds]
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
  const moveTargetAreaNames = useMemo(() => {
    const names = new Map<string, string>();
    for (const item of moveCandidates) {
      if (item.floor_code !== moveTargetFloorCode) continue;
      const code = item.area_code || "未分区";
      if (!names.has(code)) names.set(code, employeeAreaName(item, { floorCode: item.floor_code }));
    }
    return names;
  }, [moveCandidates, moveTargetFloorCode]);
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
  const mergeSuggestions = useMemo(() => {
    const groups = new Map<string, { label: string; candidates: PalletMergeCandidate[]; total: number }>();
    for (const location of visualLocations) {
      for (const pallet of inventoryLocationPallets(location) as DashboardPallet[]) {
        const normalized = normalizePalletMergeCandidate(location, pallet);
        if (!normalized.candidate) continue;
        const physicalItems = pallet.items.filter((item) => inventoryLabelQuantity(item) > 0);
        const productKey = palletMergeSuggestionProductKey(physicalItems);
        if (!productKey) continue;
        const first = physicalItems[0];
        const groupKey = [
          normalized.candidate.customer_id,
          normalized.candidate.inventory_type,
          normalized.candidate.unit,
          normalized.candidate.inventory_status,
          productKey
        ].join("|");
        const current = groups.get(groupKey) || {
          label: `${first.inventory_code || first.product_name || "存货编码待补充"}${first.specification ? ` · ${first.specification}` : ""}`,
          candidates: [],
          total: 0
        };
        current.candidates.push(normalized.candidate);
        current.total += normalized.candidate.total_quantity;
        groups.set(groupKey, current);
      }
    }
    return [...groups.entries()]
      .filter(([, group]) => group.candidates.length >= 2)
      .map(([key, group]) => ({ key, ...group }))
      .sort((left, right) => right.candidates.length - left.candidates.length || left.label.localeCompare(right.label, "zh-CN", { numeric: true }));
  }, [visualLocations]);
  const mergeCustomerOptions = useMemo(() => {
    const customers = new Map<number, string>();
    for (const suggestion of mergeSuggestions) {
      for (const candidate of suggestion.candidates) {
        customers.set(candidate.customer_id, employeeCustomerName(candidate));
      }
    }
    return [...customers.entries()]
      .map(([id, name]) => ({ id, name }))
      .sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
  }, [mergeSuggestions]);
  const filteredMergeSuggestions = useMemo(
    () => mergeSuggestions.filter((suggestion) => palletMergeSuggestionMatchesFilter(suggestion, mergeCustomerId, mergeKeyword)),
    [mergeSuggestions, mergeCustomerId, mergeKeyword]
  );
  const unmatchedInventoryObservationCount = useMemo(
    () => visualLocations.reduce((total, location) => total + Number(location.unmatched_inventory_observation_count || 0), 0),
    [visualLocations]
  );
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
  const groundCandidatePallets = useMemo(() => {
    if (mapMode !== "move" || moveAction !== "ground" || !groundCandidates) return movePreviewPallets;
    const candidates = new Map(groundCandidates.items.map((item) => [item.location_id, item]));
    const colors: Record<GroundStorageCandidate["color"], string> = {
      green: "#16a34a",
      blue: "#2563eb",
      gray: "#64748b",
      red: "#dc2626"
    };
    return movePreviewPallets.map((pallet) => {
      const locationId = Number(pallet.id.replace("erp-location-", ""));
      const candidate = candidates.get(locationId);
      if (!candidate) return pallet;
      const chosen = locationId === groundPrimaryLocationId
        ? " · 已选主位置"
        : locationId === groundSecondaryLocationId
          ? " · 已选相邻位置"
          : "";
      return {
        ...pallet,
        name: `${candidate.location_name}${chosen}`,
        color: candidate.color === "red" || candidate.color === "gray" ? colors[candidate.color] : pallet.color,
        candidate_status_color: candidate.color === "red" || candidate.color === "gray" ? colors[candidate.color] : pallet.candidate_status_color,
        status_note: candidate.reason,
        visual_status: candidate.status === "same_product" ? "waiting" as const : "empty" as const
      };
    });
  }, [movePreviewPallets, mapMode, moveAction, groundCandidates, groundPrimaryLocationId, groundSecondaryLocationId]);
  const visualLayout = useMemo(
    () => layout ? {
      ...(displayBaseLayout || layout),
      features: planningVisibleFeatures.map((feature) => {
        const projected = feature;
        return projected.feature_kind === "zone"
          ? { ...projected, name: employeeAreaName(projected, { floorCode }) }
          : projected;
      }),
      racks: planningCollisionRacks,
      // The operational map renders only ERP inventory projections.  Historical
      // editor/demo pallets remain in the measured source for provenance, but can
      // never become a second pallet-size or inventory truth on this screen.
      pallets: mapMode === "planning" ? planningPreviewPallets : groundCandidatePallets,
      violations: [
        ...layout.violations,
        ...displayedLocationConflicts.map((item) => ({
          id: `location-geometry-${item.pallet_id}-${item.column_id}`,
          severity: "error" as const,
          rule_code: "LOCATION_GEOMETRY_CONFLICT",
          message: "货位越界，或与其他货位、柱子、设备、货架、禁放区冲突",
          entity_kind: "pallet",
          entity_id: item.pallet_id,
          related_kind: "feature",
          related_id: item.column_id
        }))
      ]
    } : null,
    [layout, displayBaseLayout, planningVisibleFeatures, planningCollisionRacks, groundCandidatePallets, planningPreviewPallets, mapMode, displayedLocationConflicts, floorCode]
  );
  const searchProductGroups = useMemo(
    () => groupSearchProducts(searchResponse?.items || []),
    [searchResponse?.items]
  );
  const pendingRelocationItems = (dashboard?.unlocated_inventory || []).filter((item) => item.pending_relocation && inventoryHasPhysicalQuantity(item));
  const matchingPendingItems = pendingRelocationItems.filter((item) =>
    !pendingQuery.trim() || [item.inventory_code, item.product_name, item.customer_name, item.lot_number].some((text) => String(text || "").toLowerCase().includes(pendingQuery.trim().toLowerCase())));
  const selectedPendingItem = pendingRelocationItems.find((item) => item.lot_id === recountLotId);
  const pendingProductExists = pendingRelocationItems.some((item) => String(item.customer_id) === stocktakeCustomerId && String(item.product_id) === stocktakeProductId);
  const unlocatedFinishedItems = (dashboard?.unlocated_inventory || []).filter((item) => !item.pending_relocation && inventoryHasPhysicalQuantity(item));
  const unlocatedFinishedCount = unlocatedFinishedItems.length;
  const focusedSearchProduct = useMemo(
    () => searchProductGroups.find((item) => item.key === focusedSearchProductKey) || null,
    [searchProductGroups, focusedSearchProductKey]
  );
  const searchHighlightItems = (focusedSearchProduct?.items || searchResponse?.items || [])
    .filter((item) => inventoryHasPhysicalQuantity(item));
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
        .filter((item) => item.floor_code === floorCode && item.location_id && item.position_status === "mapped")
        .map((item) => `erp-location-${item.location_id}`),
      ...(searchResponse?.resources || [])
        .filter((item) => item.floor_code === floorCode && item.location_id && item.map_status === "mapped")
        .map((item) => `erp-location-${item.location_id}`)
    ]
  )], [searchHighlightItems, searchResponse?.resources, floorCode]);
  const mergeHighlightPalletIds = useMemo(() => [...new Set(
    mergeSources
      .filter((item) => item.floor_code === floorCode)
      .map((item) => `erp-location-${item.location_id}`)
  )], [mergeSources, floorCode]);
  const mapHighlightPalletIds = useMemo(
    () => [...new Set([...searchHighlightPalletIds, ...mergeHighlightPalletIds])],
    [searchHighlightPalletIds, mergeHighlightPalletIds]
  );
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
      setSelected(entity);
      if (locationEditMode) {
        const current = rackDrafts[entity.id] || layout.racks.find((item) => item.id === entity.id);
        if (current) setRackDrafts((drafts) => ({ ...drafts, [entity.id]: rackDraft(current) }));
        setRackFocusId(null);
        setLocationEditMessage("已选中货架；可直接拖动位置，或在右侧选择方向。修改后点击“保存并应用货架”。");
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
        setWarehouseOperationMessage(`已选待送货物；来源保持不变，请切换 1F、3F 或 4F 后点区域和具体空货位。`);
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
      if (mapMode === "move" && moveAction === "ground" && groundCandidates) {
        const candidate = groundCandidates.items.find((item) => item.location_id === locationId);
        if (candidate) {
          setSelected(entity);
          if (!candidate.selectable) {
            setGroundStorageMessage(`${candidate.location_name}：${candidate.reason}`);
            return;
          }
          if (!groundLargeFootprint) {
            setGroundPrimaryLocationId(candidate.location_id);
            setGroundSecondaryLocationId(null);
          } else if (
            groundPrimaryLocationId
            && groundPrimaryLocationId !== candidate.location_id
            && groundCandidates.items.find((item) => item.location_id === groundPrimaryLocationId)?.adjacent_location_ids.includes(candidate.location_id)
            && candidate.status === "empty"
          ) {
            setGroundSecondaryLocationId(candidate.location_id);
          } else {
            setGroundPrimaryLocationId(candidate.location_id);
            setGroundSecondaryLocationId(null);
          }
          if (candidate.status === "same_product" && candidate.capacity_quantity) {
            setGroundCapacityQuantity(String(candidate.capacity_quantity));
          }
          setGroundStorageMessage(`${candidate.location_name}：${candidate.reason}${groundLargeFootprint ? "；大型货物请再点相邻绿色位置" : "；核对后直接保存"}`);
          return;
        }
      }
      if (mapMode === "move" && moveSource) {
        const target = mappedMoveTargets.find((item) => item.location_id === locationId);
        if (target && target.location_id !== moveSource.source_location_id) {
          setMoveTargetFloorCode(target.floor_code);
          setMoveTargetAreaCode(target.area_code || "未分区");
          setMoveDraftTargetLocationId(String(target.location_id));
          setWarehouseOperationMessage(`已回填目标：${target.floor_code} · ${employeeLocationName(target)}；尚未写入。`);
        }
      }
      setSelected(entity);
      return;
    }
    if (
      entity.kind === "feature"
      && layout?.features.some((item) => (
        item.id === entity.id
        && item.feature_kind === "zone"
      ))
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
          ? `已选 ${floorCode} · ${isMeasuredDispatchFeature(feature) ? "一楼成品合并暂存区" : employeeAreaName(feature, { floorCode })}；来源栈板仍保留，请继续点地图中的具体空货位。`
          : `当前区域没有可用空货位；来源栈板仍保留，可切换其他楼层或区域继续选择。`);
      }
      setSelected(entity);
      return;
    }
    setSelected(null);
  }, [layout, locationEditMode, advancedAreaMaintenanceOpen, locationPointEditAreaCode, rackDrafts, mapMode, moveAction, moveSource, mappedMoveTargets, dispatchStagingLocation, dispatchStagingPallets, floorCode, features, visualLocations, groundCandidates, groundLargeFootprint, groundPrimaryLocationId]);
  const selectedLayoutFeature = selected?.kind === "feature"
    ? features.find((item) => item.id === selected.id)
    : undefined;
  const selectedFeature = selectedLayoutFeature?.feature_kind === "zone"
    ? selectedLayoutFeature
    : undefined;
  const selectedPlacement = selected?.kind === "equipment" ? layout?.placements.find((item) => item.id === selected.id) : undefined;
  const selectedRack = selected?.kind === "rack" ? visualLayout?.racks.find((item) => item.id === selected.id) : undefined;
  const selectedLocation = selected?.kind === "pallet"
    ? visualLocations.find((item) => `erp-location-${item.location_id}` === selected.id)
    : undefined;
  const selectedLocationPlanningWarning = selectedLocation
    ? planningConflictWarning(displayedLocationConflicts, `erp-location-${selectedLocation.location_id}`)
    : "";
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
  const selectedLocationCompositeParentSummaries = useMemo(
    () => selectedLocationItems
      .map((item) => ({ item, summary: item.composite_parent_summary }))
      .filter((row): row is { item: InventoryItem; summary: NonNullable<InventoryItem["composite_parent_summary"]> } => Boolean(row.summary)),
    [selectedLocationItems]
  );
  const selectedLocationLookupItems = useMemo(
    () => selectedLocationItems.filter((item) => !item.composite_parent_group_key),
    [selectedLocationItems]
  );
  const selectedLocationTraceItems = useMemo(() => {
    if (!traceFocusedLotId) return selectedLocationLookupItems;
    const target = selectedLocationItems.find((item) => item.lot_id === traceFocusedLotId);
    if (!target) return selectedLocationLookupItems;
    return [target, ...selectedLocationLookupItems.filter((item) => item.lot_id !== traceFocusedLotId)];
  }, [selectedLocationItems, selectedLocationLookupItems, traceFocusedLotId]);
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
  const visibleSelectedLocationItems = mapMode !== "lookup"
    ? selectedLocationItems
    : locationItemsExpanded
      ? selectedLocationTraceItems
      : selectedLocationTraceItems.slice(0, 4);
  const selectedLocationHasColumnConflict = Boolean(
    selectedLocation && operationalColumnConflictIds.has(`erp-location-${selectedLocation.location_id}`)
  );
  const selectedLocationStocktakeBlockReason = stocktakeRefreshRequired
    ? "盘点已写入，请先刷新核对地图。"
    : !selectedLocation
    ? "请先在地图选择正式货位。"
    : locationDrafts[selectedLocation.location_id]
      ? "该货位仍有未发布的布局草稿，不能加入盘点草稿。"
      : selectedLocationHasColumnConflict
        ? "该货位与固定柱子冲突，不能加入盘点草稿。"
        : stocktakeLocationBlockReason(selectedLocation);
  const selectedLocationBaseReceivable = Boolean(selectedLocation && !selectedLocationStocktakeBlockReason);
  const selectedLocationFinishedAddBlockReason = selectedLocationStocktakeBlockReason
    || stocktakeAddBlockReason(selectedLocation, "finished");
  const selectedLocationSemiFinishedAddBlockReason = selectedLocationStocktakeBlockReason
    || stocktakeAddBlockReason(selectedLocation, "semi_finished");
  const selectedLocationCanReceiveStocktakeProduct = stocktakeInventoryType === "finished"
    ? !selectedLocationFinishedAddBlockReason
    : !selectedLocationSemiFinishedAddBlockReason;
  const selectedLocationAddBlockReason = stocktakeInventoryType === "finished"
    ? selectedLocationFinishedAddBlockReason
    : selectedLocationSemiFinishedAddBlockReason;
  const selectedStocktakeProduct = stocktakeProductCandidates.find(
    (item) => String(item.product_id) === stocktakeProductId
  );
  const selectedStocktakeCustomer = stocktakeCustomers.find(
    (item) => String(item.id) === stocktakeCustomerId
  );
  const selectedGroundCustomer = groundCustomers.find(
    (item) => String(item.id) === groundCustomerId
  );
  const selectedGroundProduct = groundProducts.find(
    (item) => String(item.product_id) === groundProductId
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
    ? (planningVisibleFeatures.find((feature) => feature.id === selectedAreaFeature.id)?.points || selectedAreaFeature.points)
    : [];
  const selectedAreaBoundary = selectedAreaBoundaryPoints.length
    ? pointsBoundsMm(selectedAreaBoundaryPoints) : null;
  const selectedAreaVisibleAreaMm2 = selectedAreaBoundaryPoints.length
    ? polygonAreaMm2(selectedAreaBoundaryPoints)
    : Number(selectedAreaFeature?.area_mm2 || 0);
  const selectedFeatureIsMeasuredDispatch = floorCode === "1F" && isMeasuredDispatchFeature(selectedFeature);
  const dispatchStagingItems = dispatchStagingLocation?.loose_items || [];
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
  const featureHasMappedGroundLocations = (feature: TwinFeature) => {
    const areaCode = featureAreaCode(feature);
    return visualLocations.some((location) => (
      location.floor_code === floorCode
      && location.is_active
      && location.storage_type === "ground"
      && location.position_status === "mapped"
      && Boolean(location.map_position)
      && (
        location.map_feature_id === feature.id
        || Boolean(areaCode && location.area_code === areaCode)
      )
    ));
  };
  const selectedAreaBoundaryLocked = Boolean(
    selectedAreaFeature && featureHasMappedGroundLocations(selectedAreaFeature)
  );
  const selectedAreaLocationIds = new Set(
    selectedAreaLocations.map((item) => `erp-location-${item.location_id}`)
  );
  const selectedAreaConflictCount = new Set(
    planningGeometryConflicts
      .filter((item) => selectedAreaLocationIds.has(item.pallet_id))
      .map((item) => item.pallet_id)
  ).size;
  const stocktakeAreaTargetLocations = selectedAreaLocations
    .filter((location) => (
      !locationDrafts[location.location_id]
      && !operationalColumnConflictIds.has(`erp-location-${location.location_id}`)
      && !stocktakeLocationBlockReason(location)
    ))
    .sort((left, right) => employeeLocationName(left).localeCompare(
      employeeLocationName(right), "zh-CN", { numeric: true }
    ));
  const stocktakeExistingLocations = useMemo<StocktakeExistingProductLocation[]>(
    () => stocktakeExistingProductLocations(visualLocations, {
      customerId: stocktakeCustomerId,
      productId: stocktakeProductId,
      inventoryType: stocktakeInventoryType,
      targetFloorCode: selectedLocation?.floor_code || floorCode,
      targetAreaCode: selectedLocation?.area_code || selectedAreaCode,
      targetLocationId: selectedLocation?.location_id
    }),
    [
      visualLocations,
      stocktakeCustomerId,
      stocktakeProductId,
      stocktakeInventoryType,
      selectedLocation?.floor_code,
      selectedLocation?.area_code,
      selectedLocation?.location_id,
      floorCode,
      selectedAreaCode
    ]
  );
  const stocktakeOutsideAreaLocations = stocktakeExistingLocations.filter(
    (item) => item.is_outside_target_area && !item.is_target_location
  );
  const stocktakeOutsideAreaAvailable = stocktakeOutsideAreaLocations.reduce(
    (total, item) => total + Number(item.available_quantity || 0), 0
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
  const locationPointEditPalletIds = useMemo(() => locationEditMode
    ? visualLocations
      .filter((item) => item.floor_code === floorCode
        && item.position_status === "mapped"
        && !previewOnlyLocationIds.has(`erp-location-${item.location_id}`)
        && (!locationPointEditAreaCode || item.area_code === locationPointEditAreaCode))
      .map((item) => `erp-location-${item.location_id}`)
    : EMPTY_CANVAS_IDS, [locationEditMode, visualLocations, floorCode, previewOnlyLocationIds, locationPointEditAreaCode]);
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
  const selectedPublishedMoldRack = selectedRackEditDraft
    && moldRackResponse?.rack.rack_id === selectedRackEditDraft.id
      ? moldRackResponse.rack
      : null;
  const selectedMoldRackUsage = moldRackResponse && moldRackResponse.rack.rack_id === selectedRackEditDraft?.id
    ? moldRackLevelUsage(moldRackResponse.items)
    : new Map<number, number>();
  const selectedMoldRackHighestUsedLevel = Math.max(0, ...selectedMoldRackUsage.keys());
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
      .replace(/^ZONE-(?:1F|3F|4F)-/i, "")
      .replace(/[^A-Z0-9-]+/gi, "-")
      .replace(/^-+|-+$/g, "")
      .toUpperCase()
      .slice(0, 30);
    setFormalAreaCodeDraft(selectedAreaFeature.erp_area_code || suggested);
    setFormalAreaNameDraft(employeeAreaName(selectedAreaFeature, { floorCode }) || suggested);
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
  }, [selectedAreaFeature?.id, selectedAreaFeature?.erp_area_code, selectedAreaFeature?.formal_area_name, selectedAreaFeature?.employee_area_name, selectedAreaFeature?.name, floorCode]);
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
        .replace(/^ZONE-(?:1F|3F|4F)-/i, "")
        .replace(/[^A-Z0-9-]+/gi, "-")
        .replace(/^-+|-+$/g, "")
        .toUpperCase()
        .slice(0, 30) || "";
      setFormalAreaCodeDraft(selectedAreaFeature?.erp_area_code || suggested);
      setFormalAreaNameDraft(employeeAreaName(selectedAreaFeature, { floorCode }) || suggested);
      setLocationEditMessage("已取消选用现有区域；请核对下方正式区域编号后再保存草稿。");
      return;
    }
    setFormalAreaCodeDraft(area.area_code);
    const readableAreaName = employeeAreaName(area, { floorCode: area.floor_code });
    setFormalAreaNameDraft(readableAreaName);
    setLocationEditMessage(`已选用现有区域 ${area.floor_code} · ${area.area_code} ${readableAreaName}；保存后仍是地图草稿，校验并发布才会建立绑定。`);
  };
  const focusedRack = rackFocusId ? layout?.racks.find((item) => item.id === rackFocusId) || null : null;
  const moldRackQueryRack = focusedRack?.mold_rack_code
    ? focusedRack
    : locationEditMode && selectedRack?.mold_rack_code
      ? selectedRack
      : null;
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
    if (!moldRackQueryRack?.mold_rack_code) {
      setMoldRackResponse(null);
      setMoldRackLoading(false);
      setMoldRackError("");
      return;
    }
    let active = true;
    setMoldRackResponse(null);
    setMoldRackLoading(true);
    setMoldRackError("");
    const params = new URLSearchParams({ floor_code: floorCode, rack_id: moldRackQueryRack.id });
    requestJson<MoldRackResponse>(`/api/warehouse/molds/by-map-rack?${params.toString()}`)
      .then((value) => { if (active) setMoldRackResponse(value); })
      .catch((reason: Error) => { if (active) setMoldRackError(reason.message); })
      .finally(() => { if (active) setMoldRackLoading(false); });
    return () => { active = false; };
  }, [moldRackQueryRack?.id, moldRackQueryRack?.mold_rack_code, floorCode, moldRackRefreshToken]);
  const focusedRackLocations = useMemo<DashboardLocation[]>(() => {
    if (!focusedRack) return [];
    return visualLocations
      .filter((location) => location.floor_code === floorCode
        && location.map_rack_id === focusedRack.id
        && location.address_kind === "rack_slot"
        && Number.isInteger(location.level_no)
        && Number.isInteger(location.slot_no)
        && location.storage_type === "rack"
        && location.is_active
        && location.position_status === "mapped"
        && !locationDrafts[location.location_id])
      .sort((left, right) => Number(left.level_no) - Number(right.level_no)
        || Number(left.slot_no) - Number(right.slot_no)
        || left.location_id - right.location_id);
  }, [focusedRack, visualLocations, floorCode, locationDrafts]);
  const unboundRackLocationCount = useMemo(() => {
    if (!focusedRackAreaCode) return 0;
    return visualLocations.filter((location) => location.floor_code === floorCode
      && location.area_code === focusedRackAreaCode
      && location.storage_type === "rack"
      && location.is_active
      && !location.map_rack_id
      && (location.occupancy_status === "occupied" || rackLocationInventoryItems(location).length > 0)).length;
  }, [focusedRackAreaCode, visualLocations, floorCode]);
  const selectedStocktakeItem = selectedLocationItems.find((item) => item.lot_id === stocktakeLotId) || null;
  const selectedStocktakeDecreaseBlockReason = selectedStocktakeItem
    ? selectedLocationStocktakeBlockReason || stocktakeDecreaseBlockReason(selectedStocktakeItem)
    : null;

  useEffect(() => {
    setLocationDetailOpen(false);
    setLocationItemsExpanded(false);
    const nextStocktakeInventoryType: StocktakeInventoryType = (
      selectedLocation?.warehouse_type === "semi_finished"
      || (selectedLocation?.warehouse_type === "shared" && selectedLocation.storage_type === "rack")
    ) ? "semi_finished" : "finished";
    setStocktakeInventoryType(nextStocktakeInventoryType);
    setStocktakeCustomerQuery("");
    setStocktakeCustomers([]);
    setStocktakeCustomerId("");
    setStocktakeProductQuery("");
    setStocktakeProductCandidates([]);
    setStocktakeProductId("");
    setStocktakeAddQuantity("");
    setStocktakeSupplementConfirmed(false);
    setStocktakeLotId(null);
    setStocktakeDecreaseQuantity("");
    setWarehouseOperationMessage("");
  }, [selected?.kind, selected?.id]);
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
    if (!dashboard || !layout || loading || layout.floor_code !== floorCode) return;
    const location = visualLocations.find(
      (item) => item.location_id === pendingLocationId && item.floor_code === floorCode
    );
    if (!location) {
      if (traceReadOnly) {
        setTraceDeepLinkMessage("无法定位：指定库位不存在、已停用，或当前账号无权查看。请返回订单追溯刷新后重试。");
      }
      setPendingLocationId(null);
      setPendingLotId(null);
      return;
    }
    const entity = { kind: "pallet" as const, id: `erp-location-${pendingLocationId}` };
    if (selected?.kind !== entity.kind || selected.id !== entity.id) {
      setSelected(entity);
      cameraFocusSequenceRef.current += 1;
      setCameraFocusTarget({ entity, token: cameraFocusSequenceRef.current, source: "search" });
      return;
    }
    if (pendingLotId !== null) {
      const targetLot = selectedLocationItems.find((item) => item.lot_id === pendingLotId);
      if (targetLot) {
        setTraceFocusedLotId(pendingLotId);
        setLocationItemsExpanded(true);
        setTraceDeepLinkMessage(
          `已定位 ${employeeLocationName(location)} · 批次 ${targetLot.lot_number || pendingLotId} · ${formatNumber(inventoryLabelQuantity(targetLot))} ${inventoryUnitLabel(targetLot.unit)}`
        );
      } else if (traceReadOnly) {
        setTraceDeepLinkMessage("已定位原库位，但指定成品批次已移位、清零，或当前账号无权查看。请返回订单追溯刷新后重试。");
      }
    } else if (traceReadOnly) {
      setTraceDeepLinkMessage(`已定位 ${employeeLocationName(location)}；当前页面只读，不会改变库存。`);
    }
    setPendingLocationId(null);
    setPendingLotId(null);
  }, [pendingLocationId, pendingLotId, floorCode, visualLocations, dashboard, layout, loading, selected, selectedLocationItems, traceReadOnly]);

  useEffect(() => {
    if (!traceReadOnly || !error) return;
    setTraceDeepLinkMessage(`无法读取仓库位置：${error}`);
  }, [traceReadOnly, error]);

  useEffect(() => {
    if (
      !canStocktake
      || mapMode !== "move"
      || moveAction !== "stocktake"
      || viewMode !== "2d"
      || !selectedLocationBaseReceivable
      || !selectedLocationCanReceiveStocktakeProduct
    ) {
      setStocktakeCustomers([]);
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ keyword: stocktakeCustomerQuery.trim(), page: "1", page_size: "50" });
      requestJson<CustomerOptionsResponse>(`/api/master/customers?${params.toString()}`)
        .then((value) => active && setStocktakeCustomers(value.items || []))
        .catch((reason: Error) => active && setWarehouseOperationMessage(reason.message));
    }, 220);
    return () => { active = false; window.clearTimeout(timer); };
  }, [stocktakeCustomerQuery, canStocktake, mapMode, moveAction, viewMode, selectedLocation?.location_id, selectedLocationBaseReceivable, selectedLocationCanReceiveStocktakeProduct]);

  useEffect(() => {
    const keyword = stocktakeProductQuery.trim();
    if (!canStocktake || mapMode !== "move" || moveAction !== "stocktake" || viewMode !== "2d" || !selectedLocationCanReceiveStocktakeProduct || !stocktakeCustomerId) {
      setStocktakeProductCandidates([]);
      setStocktakeProductId("");
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ q: keyword, customer_id: stocktakeCustomerId, limit: "50" });
      requestJson<ProductCandidatesResponse>(`/api/warehouse/floor3/product-candidates?${params.toString()}`)
        .then((value) => {
          if (!active) return;
          setStocktakeProductCandidates(value.items || []);
          setStocktakeProductId((current) => current && value.items.some((item) => String(item.product_id) === current) ? current : "");
        })
        .catch((reason: Error) => active && setWarehouseOperationMessage(reason.message));
    }, 300);
    return () => { active = false; window.clearTimeout(timer); };
  }, [stocktakeProductQuery, stocktakeCustomerId, canStocktake, mapMode, moveAction, viewMode, selectedLocation?.location_id, selectedLocationCanReceiveStocktakeProduct]);

  useEffect(() => {
    if (!canExecuteWarehouse || mapMode !== "move" || moveAction !== "ground" || viewMode !== "2d" || groundOperation !== "inbound") {
      setGroundCustomers([]);
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ keyword: groundCustomerQuery.trim(), page: "1", page_size: "50" });
      requestJson<CustomerOptionsResponse>(`/api/master/customers?${params.toString()}`)
        .then((value) => active && setGroundCustomers(value.items || []))
        .catch((reason: Error) => active && setGroundStorageMessage(reason.message));
    }, 220);
    return () => { active = false; window.clearTimeout(timer); };
  }, [groundCustomerQuery, groundOperation, canExecuteWarehouse, mapMode, moveAction, viewMode]);

  useEffect(() => {
    if (!canExecuteWarehouse || mapMode !== "move" || moveAction !== "ground" || viewMode !== "2d" || groundOperation !== "inbound" || !groundCustomerId) {
      setGroundProducts([]);
      if (groundOperation === "inbound") setGroundProductId("");
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ q: groundProductQuery.trim(), customer_id: groundCustomerId, limit: "50" });
      requestJson<ProductCandidatesResponse>(`/api/warehouse/floor3/product-candidates?${params.toString()}`)
        .then((value) => {
          if (!active) return;
          setGroundProducts(value.items || []);
          setGroundProductId((current) => current && value.items.some((item) => String(item.product_id) === current) ? current : "");
        })
        .catch((reason: Error) => active && setGroundStorageMessage(reason.message));
    }, 300);
    return () => { active = false; window.clearTimeout(timer); };
  }, [groundProductQuery, groundCustomerId, groundOperation, canExecuteWarehouse, mapMode, moveAction, viewMode]);

  useEffect(() => {
    setGroundCandidates(null);
    setGroundPrimaryLocationId(null);
    setGroundSecondaryLocationId(null);
    setGroundStorageMessage("");
    setGroundStorageIdempotencyKey(operationKey("ground-storage"));
  }, [floorCode, selectedAreaCode, groundOperation, groundCustomerId, groundProductId, groundTransferSource?.source_key, groundQuantity]);

  const loadGroundStorageCandidates = async () => {
    const customerId = groundOperation === "transfer" ? groundTransferSource?.customer_id : Number(groundCustomerId);
    const productId = groundOperation === "transfer" ? groundTransferSource?.product_id : Number(groundProductId);
    const quantity = Number(groundQuantity);
    if (!selectedAreaCode || !customerId || !productId || !Number.isInteger(quantity) || quantity <= 0) {
      setGroundStorageMessage("请先选区域、客户、产品并填写本次整数数量。");
      return;
    }
    setGroundStorageBusy(true);
    setGroundStorageMessage("");
    try {
      const params = new URLSearchParams({
        floor_code: floorCode,
        area_code: selectedAreaCode,
        customer_id: String(customerId),
        product_id: String(productId),
        incoming_quantity: String(quantity)
      });
      const response = await requestJson<GroundStorageCandidatesResponse>(`/api/warehouse/ground-storage/candidates?${params.toString()}`);
      setGroundCandidates(response);
      setGroundPrimaryLocationId(null);
      setGroundSecondaryLocationId(null);
      setGroundStorageMessage(`已显示 ${response.items.length} 个正式位置；请按颜色和文字点选地图。`);
    } catch (reason) {
      setGroundCandidates(null);
      setGroundStorageMessage((reason as Error).message);
    } finally {
      setGroundStorageBusy(false);
    }
  };

  const saveGroundStorage = async () => {
    const primary = groundCandidates?.items.find((item) => item.location_id === groundPrimaryLocationId);
    const secondary = groundCandidates?.items.find((item) => item.location_id === groundSecondaryLocationId);
    const quantity = Number(groundQuantity);
    const candidateCapacity = primary?.status === "same_product" ? primary.capacity_quantity : Number(groundCapacityQuantity);
    if (!primary || !primary.selectable || !Number.isInteger(quantity) || quantity <= 0 || !candidateCapacity || candidateCapacity < quantity) {
      setGroundStorageMessage("请点选可用位置，并填写不小于本次数量的位置容量。");
      return;
    }
    if (groundLargeFootprint && (!secondary || !primary.adjacent_location_ids.includes(secondary.location_id))) {
      setGroundStorageMessage("大型货物必须再点选一个相邻绿色位置。");
      return;
    }
    setGroundStorageBusy(true);
    setGroundStorageMessage("");
    try {
      const common = {
        location_id: primary.location_id,
        expected_layout_version: primary.layout_version,
        secondary_location_id: groundLargeFootprint ? secondary?.location_id : undefined,
        expected_secondary_layout_version: groundLargeFootprint ? secondary?.layout_version : undefined,
        quantity,
        capacity_quantity: Number(candidateCapacity),
        idempotency_key: groundStorageIdempotencyKey
      };
      let response: GroundStorageMutationResponse | null;
      if (groundOperation === "transfer") {
        if (!groundTransferSource?.lot_id || !groundTransferSource.expected_version) throw new Error("请先在地图有货位置选择一个正式成品批次。");
        response = await mutateJson<GroundStorageMutationResponse>(
          `/api/warehouse/ground-storage/lots/${groundTransferSource.lot_id}/transfer`,
          "POST",
          { ...common, expected_lot_version: groundTransferSource.expected_version }
        );
      } else {
        const customerId = Number(groundCustomerId);
        const productId = Number(groundProductId);
        if (!customerId || !productId) throw new Error("请选择客户和产品。");
        response = await mutateJson<GroundStorageMutationResponse>(
          "/api/warehouse/ground-storage/finished-inbound",
          "POST",
          { ...common, customer_id: customerId, product_id: productId, stock_date: groundStockDate }
        );
      }
      if (response) {
        await refreshDashboard();
        setGroundStorageMessage(`${response.location_name} 已保存 ${response.quantity} 只；批次和来源保持独立。`);
        setGroundStorageIdempotencyKey(operationKey("ground-storage"));
        setGroundCandidates(null);
        setGroundPrimaryLocationId(null);
        setGroundSecondaryLocationId(null);
        if (groundOperation === "transfer") setGroundTransferSource(null);
      }
    } catch (reason) {
      setGroundStorageMessage((reason as Error).message);
    } finally {
      setGroundStorageBusy(false);
    }
  };

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
      setProductionMessage(`${selectedProductionTask.order_number} 已人工定位；生产任务、数量和状态未修改`);
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
      setProductionMessage(`${selectedProductionTask.order_number} 已移回待定位；生产任务保持不变`);
    } catch (reason) {
      setProductionMessage((reason as Error).message);
    } finally {
      setProductionBusy(false);
    }
  };

  const moveLocationDraft = (palletId: string, xMm: number, yMm: number) => {
    if (!locationEditMode || layoutMapToolsOpen) return;
    const locationId = Number(palletId.replace("erp-location-", ""));
    const location = visualLocations.find((item) => item.location_id === locationId);
    if (locationPointEditAreaCode && location?.area_code !== locationPointEditAreaCode) {
      setLocationEditMessage(`当前只可拖动 ${locationPointEditAreaCode} 区货位；其他区域保持固定。`);
      return;
    }
    const zone = locationProjectionFeatures.find((item) => item.feature_kind === "zone" && item.erp_area_code === location?.area_code);
    if (!location || !zone || location.position_status !== "mapped") {
      setLocationEditMessage("该库位尚无已确认布局，不能用拖动伪造坐标。");
      return;
    }
    if (!advancedAreaMaintenanceOpen && !locationPointEditAreaCode) {
      setLocationPointEditAreaCode(location.area_code);
      setLayoutMapToolsOpen(false);
      setSwapSourceLocationId(null);
    }
    const geometry = locationLayoutGeometry(zone, location, xMm, yMm, { clampToZone: false });
    if (!geometry) {
      setLocationEditMessage("库位布局版本缺失，请刷新后重试。");
      return;
    }
    setLocationDrafts((current) => ({ ...current, [locationId]: geometry }));
    setSelected({ kind: "pallet", id: palletId });
    const currentPallet = mappedLocationPallets.find((item) => item.id === palletId);
    const prospectivePallets = currentPallet
      ? planningCollisionPallets.map((item) => item.id === palletId
        ? { ...item, x_mm: xMm, y_mm: yMm }
        : item)
      : [];
    const hitsColumn = currentPallet && layout
      ? findPalletPlanningConflicts(
        prospectivePallets,
        planningCollisionStructures,
        planningVisibleFeatures,
        0,
        planningCollisionPlacements,
        planningCollisionRacks
      ).some((item) => item.pallet_id === palletId)
      : false;
    setLocationEditMessage(hitsColumn
      ? `${location.location_code} 越界，或与其他货位、柱子、设备、货架、禁放区冲突，已标红；可以保存调整，再继续整理。`
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
      customer_id: item.customer_id || undefined,
      product_id: item.product_id || undefined,
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

  const prepareDelayedDispatchMove = async (candidate: DelayedDispatchCandidate) => {
    const sourceLocation = visualLocations.find((item) => item.location_id === candidate.source_location_id);
    if (!canExecuteWarehouse || !sourceLocation) {
      setWarehouseOperationMessage("当前账号不能执行仓库移货，或待送位置尚未加载；请刷新后重试。");
      return;
    }
    const pallet = (inventoryLocationPallets(sourceLocation) as DashboardPallet[])
      .find((item) => item.pallet_id === candidate.pallet_id);
    const source = pallet ? palletMoveSource(sourceLocation, pallet) : null;
    if (!source) {
      setWarehouseOperationMessage("这块待送栈板已变化，请刷新后按最新库存重新选择。");
      return;
    }
    if (mapMode !== "move") await enterWarehouseMoveMode();
    setMoveAction("relocate");
    chooseMoveSource(source);
    setFloorCode("3F");
    setMoveTargetFloorCode("3F");
    setMoveTargetAreaCode(candidate.recommended_area_code);
    setMoveDraftTargetLocationId("");
    setWarehouseOperationMessage(
      `已选择待整理货物；请先把实物栈板搬到三楼左区，再选择一个空货位加入草稿并确认提交。当前尚未改库存位置。`
    );
  };

  const focusDelayedDispatchCandidate = (candidate: DelayedDispatchCandidate) => {
    setSearchPanelOpen(false);
    setPendingAreaCode(null);
    setPendingLocationId(candidate.source_location_id);
    setFloorCode(candidate.source_floor_code);
    setWarehouseOperationMessage(
      `${candidate.source_location_name || "当前待送位置"} 已定位；紫色货位是这批货物的当前地图位置。`
    );
  };

  const queueMoveDraft = (source: WarehouseMoveSource, target: DashboardLocation, quantityOverride?: number) => {
    if (!target.map_position) {
      setWarehouseOperationMessage("目标货位布局版本缺失，请刷新地图后重试。");
      return false;
    }
    if (source.source_location_id === target.location_id) {
      setWarehouseOperationMessage("来源与目标不能是同一货位。");
      return false;
    }
    const quantity = source.operation === "lot_transfer"
      ? Number(quantityOverride ?? (moveQuantity || source.quantity))
      : undefined;
    if (source.operation === "lot_transfer" && (!Number.isFinite(quantity) || Number(quantity) <= 0 || Number(quantity) > Number(source.max_quantity || 0))) {
      setWarehouseOperationMessage(`移动数量必须大于 0，且不能超过可移动 ${formatNumber(source.max_quantity)} ${inventoryUnitLabel(source.unit)}。`);
      return false;
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
      return false;
    }
    setMoveDrafts(result.items);
    setMoveBatchIdempotencyKey(operationKey("warehouse-move-batch"));
    setMoveDraftTargetLocationId("");
    setWarehouseOperationMessage(`已加入页面草稿：${source.source_location_name} → ${target.location_name}；尚未写入，底部一次确认后才提交。`);
    return true;
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
      ? `已加入合并集合：${normalizedCandidate.location_name}；可跨楼层继续选择。`
      : `已移出合并集合：${normalizedCandidate.location_name}；正式库存未改变。`);
  };

  const chooseMergeTarget = (candidate: PalletMergeCandidate) => {
    if (!mergeSources.some((item) => item.pallet_id === candidate.pallet_id)) {
      setWarehouseOperationMessage("目标栈板必须从已选集合中明确指定。");
      return;
    }
    setMergeTarget(candidate);
    setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
    setWarehouseOperationMessage(`已选主货位：${candidate.floor_code} · ${candidate.location_name}；尚未写入。`);
  };

  const clearMergeDraft = () => {
    setMergeSources([]);
    setMergeTarget(null);
    setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
    setWarehouseOperationMessage("已清空多栈合并草稿，正式库存未改变。");
  };

  const confirmPalletMergeBatch = async () => {
    if (mergeSources.length < 2 || !mergeTarget || mergeBatchBusy) return;
    const target = mergeTarget;
    const submittedSources = mergeSources.filter((item) => item.pallet_id !== mergeTarget.pallet_id);
    const sourcePreview = submittedSources.map((item) => `${item.floor_code}/${item.location_name || "位置名称待完善"}`).join("\n");
    if (!window.confirm(`确认一次把 ${submittedSources.length} 个来源货位的全部库存批次并入主货位吗？\n\n来源：\n${sourcePreview}\n\n主货位：${mergeTarget.floor_code}/${mergeTarget.location_name || "位置名称待完善"}\n\n来源栈板将逻辑释放；库存数量、批次、预占和库龄不变。`)) return;
    setMergeBatchBusy(true);
    setWarehouseOperationMessage("");
    let mergeAcknowledged = false;
    try {
      await mutateJson(
        "/api/warehouse/pallets/merge-batches",
        "POST",
        buildPalletMergeBatchPayload(mergeBatchIdempotencyKey, mergeSources, mergeTarget)
      );
      mergeAcknowledged = true;
      setMergeSources([]);
      setMergeTarget(null);
      setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
      if (isWarehouseOperationalFloorCode(target.floor_code)) setFloorCode(target.floor_code);
      setSelected({ kind: "pallet", id: `erp-location-${target.location_id}` });
      await refreshDashboard();
      setWarehouseOperationMessage(`${submittedSources.length} 个来源货位已一次并入 ${target.location_name}；来源栈板已逻辑释放。`);
    } catch (reason) {
      const rawMessage = String((reason as Error)?.message || reason || "未知错误");
      const message = /failed to fetch|networkerror|load failed/i.test(rawMessage) ? "服务连接中断" : rawMessage;
      setWarehouseOperationMessage(mergeAcknowledged
        ? `多栈合并已完成，但地图刷新失败：${message}。请刷新页面核对；不要重复提交合并。`
        : message === "服务连接中断"
          ? "多栈合并提交未收到服务器回执：服务连接中断。来源、目标和同一幂等键已保留；服务恢复后先刷新页面核对，若草稿仍在可用同一幂等键重试，系统只会处理一次。"
          : `多栈合并失败：${message}。来源、目标和本次幂等键已保留，可核对后重试。`);
    } finally {
      setMergeBatchBusy(false);
    }
  };

  const confirmMoveDrafts = async () => {
    if (!moveDrafts.length || moveBatchBusy) return;
    setMoveBatchBusy(true);
    setWarehouseOperationMessage("");
    let moveAcknowledged = false;
    try {
      await mutateJson(
        "/api/warehouse/twin-operations/move-batches",
        "POST",
        buildMoveBatchPayload(moveBatchIdempotencyKey, moveDrafts)
      );
      moveAcknowledged = true;
      setMoveDrafts([]);
      setMoveSource(null);
      setMoveQuantity("");
      setMoveDraftTargetLocationId("");
      setMoveBatchIdempotencyKey(operationKey("warehouse-move-batch"));
      await refreshDashboard();
      setWarehouseOperationMessage("整批移货已成功；地图已刷新。 ");
    } catch (reason) {
      setWarehouseOperationMessage(moveAcknowledged
        ? `移货已完成，但地图刷新失败：${(reason as Error).message}。当前画面尚未核验，请刷新页面；不要重复提交移货。`
        : `整批提交失败：${(reason as Error).message}。页面草稿与本次幂等键已保留，可核对后重试。`);
    } finally {
      setMoveBatchBusy(false);
    }
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
    if (!layout || layoutMapToolsOpen || locationEditBusy) return;
    const drafts = Object.values(locationDrafts).filter((draft) => !locationPointEditAreaCode
      || dashboard?.locations.some((location) => location.location_id === draft.location_id && location.area_code === locationPointEditAreaCode));
    if (!drafts.length) return;
    setLocationEditBusy(true);
    const savedIds = new Set<number>();
    let workingLayout = layout;
    const expectedPublishedRevision = planningPublishedLayout?.source_sha256 || publishedFloorRevision;
    let savedFeature: TwinFeature | null = null;
    try {
      const grouped = new Map<string, LocationLayoutGeometry[]>();
      for (const draft of drafts) {
        const location = dashboard?.locations.find((item) => item.location_id === draft.location_id);
        if (!location?.area_code) throw new Error("货位身份缺失，请重读地图");
        grouped.set(location.area_code, [...(grouped.get(location.area_code) || []), draft]);
      }
      if (grouped.size !== 1) throw new Error("一次只能保存并应用一个区域的货位调整");
      for (const [areaCode, slots] of grouped) {
        const feature = workingLayout.features.find((item) => (item as TwinFeature).erp_area_code === areaCode && item.feature_kind === "zone") as TwinFeature | undefined;
        const origin = locationProjectionFeatures.find((item) => item.id === feature?.id);
        if (!feature || !origin) throw new Error("区域原坐标缺失，请重读地图");
        const bounds = pointsBoundsMm(origin.points);
        const requestBody = {
            expected_revision: workingLayout.source_sha256, expected_version: feature.version,
            points: feature.points,
            ground_locations: slots.map((slot) => ({
              location_id: slot.location_id, expected_version: slot.expected_version,
              x_mm: bounds.centerXmm - bounds.widthMm / 2 + bounds.widthMm * slot.left_pct / 100,
              y_mm: bounds.centerYmm + bounds.heightMm / 2 - bounds.heightMm * (slot.top_pct + slot.height_pct) / 100
            }))
          };
        const requestSignature = JSON.stringify({ areaCode, ...requestBody });
        if (locationLayoutOperationRef.current?.signature !== requestSignature) {
          locationLayoutOperationRef.current = {
            signature: requestSignature,
            key: operationKey("ground-layout-positions")
          };
        }
        const result = await mutateJson<LayoutMutationResponse<TwinFeature>>(
          `/api/warehouse/twin-layout/floors/${floorCode}/features/${feature.id}/geometry`, "PATCH", {
            ...requestBody,
            operation_key: `${locationLayoutOperationRef.current.key}-${areaCode}`,
          });
        if (!result) throw new Error("尚未取得保存回执，请重读核对");
        workingLayout = { ...workingLayout, source_sha256: result.revision,
          features: workingLayout.features.map((item) => item.id === result.item.id ? { ...item, ...result.item } : item) };
        savedFeature = { ...feature, ...result.item };
        setLayout(workingLayout);
        rememberServerDraft(result.revision);
        slots.forEach((slot) => savedIds.add(slot.location_id));
      }
      if (!savedFeature) throw new Error("没有取得当前区域的保存结果");
      await applySavedAreaGeometryRevision(savedFeature, workingLayout.source_sha256, expectedPublishedRevision);
      setLocationDrafts((current) => Object.fromEntries(Object.entries(current).filter(([id]) => !savedIds.has(Number(id)))));
      locationLayoutOperationRef.current = null;
      setLocationPointEditAreaCode(null);
      setKeyboardLocationEditActive(false);
      setLocationEditMessage(`已保存并应用 ${savedIds.size} 个货位调整；三种模式现在使用同一坐标。`);
    } catch (reason) {
      setLocationDrafts((current) => Object.fromEntries(Object.entries(current).filter(([id]) => !savedIds.has(Number(id)))));
      if (savedIds.size) {
        locationLayoutOperationRef.current = null;
        try { await refreshPlanningTwinFloor(); } catch { /* 原保存回执仍有效，保留准确状态提示。 */ }
      }
      const applicationWritten = Boolean((reason as Error & { applicationWritten?: boolean }).applicationWritten);
      setLocationEditMessage(applicationWritten
        ? `已应用 ${savedIds.size} 个货位调整，但地图回读失败：${(reason as Error).message}。请刷新核对，不要重复保存。`
        : savedIds.size
        ? `已保存 ${savedIds.size} 个货位但尚未应用：${(reason as Error).message}。当前保留编辑预览，请修正后再保存。`
        : `货位草稿未确认保存：${(reason as Error).message}。调整仍保留，请重读核对。`);
    } finally { setLocationEditBusy(false); }
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
    if (item.location_id && item.position_status === "mapped") {
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
    if (isWarehouseOperationalFloorCode(item.floor_code)) setFloorCode(item.floor_code);
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
    if (isWarehouseOperationalFloorCode(resource.floor_code)) {
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

  const selectStocktakeTargetLocation = (locationId: number) => {
    const target = visualLocations.find((item) => item.location_id === locationId);
    if (!target) {
      setWarehouseOperationMessage("该货位已不在当前地图中，请刷新后重新选择。");
      return;
    }
    if (target.warehouse_type === "semi_finished") setStocktakeInventoryType("semi_finished");
    if (target.warehouse_type === "finished") setStocktakeInventoryType("finished");
    setStocktakeSupplementConfirmed(false);
    setSelected({ kind: "pallet", id: `erp-location-${target.location_id}` });
    cameraFocusSequenceRef.current += 1;
    setCameraFocusTarget({
      entity: { kind: "pallet", id: `erp-location-${target.location_id}` },
      token: cameraFocusSequenceRef.current,
      source: "search"
    });
    setWarehouseOperationMessage(`已选择盘点目标：${employeeLocationName(target)}。先查客户和产品，系统会优先列出现有库存位置。`);
  };

  const focusStocktakeExistingLocation = (match: StocktakeExistingProductLocation) => {
    if (isWarehouseOperationalFloorCode(match.source_floor_code)) {
      setFloorCode(match.source_floor_code);
    }
    if (match.source_location_id) {
      setPendingLocationId(match.source_location_id);
    }
    setWarehouseOperationMessage(
      `已定位现有库存：${match.source_location_name} · 可用 ${formatNumber(match.available_quantity)} ${inventoryUnitLabel(match.unit)}。`
    );
  };

  const queueStocktakeExistingMove = (match: StocktakeExistingProductLocation) => {
    if (!canExecuteWarehouse || !selectedLocation) {
      setWarehouseOperationMessage("当前账号不能执行移货，或尚未选择目标货位。请联系有移货权限的管理员处理。");
      return;
    }
    if (selectedLocation.occupancy_status !== "empty") {
      setWarehouseOperationMessage("优先移入现有库存需要选择一个空货位作为目标；当前位置已有货物，请改选同区域空货位。");
      return;
    }
    const sourceLocation = visualLocations.find((item) => item.location_id === match.source_location_id);
    const sourceItem = sourceLocation
      ? inventoryLocationItems(sourceLocation).find((item) => item.lot_id === match.lot_id) as InventoryItem | undefined
      : undefined;
    const source = sourceLocation && sourceItem ? lotMoveSource(sourceLocation, sourceItem) : null;
    if (!source || source.operation !== "lot_transfer" || sourceItem?.inventory_type !== "finished") {
      setWarehouseOperationMessage("该现有库存目前只能定位查看，不能从盘点页直接移入；请先使用移动位置处理，不能用新增代替移货。");
      return;
    }
    const requested = Number(stocktakeAddQuantity);
    const available = Number(match.available_quantity || 0);
    const quantity = Number.isInteger(requested) && requested > 0
      ? Math.min(requested, available)
      : available;
    if (!queueMoveDraft(source, selectedLocation, quantity)) return;
    setMoveAction("relocate");
    setMoveSource(null);
    setMoveQuantity("");
    setWarehouseOperationMessage(
      `已优先加入现有库存移货草稿：${match.source_location_name} → ${employeeLocationName(selectedLocation)} · ${formatNumber(quantity)} ${inventoryUnitLabel(match.unit)}。请先提交移货，再回到盘点补录确实缺少的数量。`
    );
  };

  const placePendingInventory = async () => {
    if (pendingPlacementRef.current.busy || pendingRefreshRequired) return;
    if (!canEditLocations || !selectedLocation || !selectedPendingItem || selectedLocationFinishedAddBlockReason) return;
    const quantity = Number(pendingQuantity);
    if (!Number.isInteger(quantity) || quantity <= 0 || quantity > movableLotQuantity(selectedPendingItem)) {
      setWarehouseOperationMessage("请填写不超过该批待归位数量的正整数。"); return;
    }
    const payload = { location_id: selectedLocation.location_id, expected_layout_version: Number(selectedLocation.map_position?.version),
      expected_version: selectedPendingItem.version, quantity, confirmed: true };
    const signature = JSON.stringify([selectedPendingItem.lot_id, payload]);
    if (pendingPlacementRef.current.signature !== signature) pendingPlacementRef.current = { busy: false, signature, key: operationKey("pending-place") };
    pendingPlacementRef.current.busy = true; setPendingPlacementBusy(true);
    let written = false;
    try {
      await mutateJson(`/api/warehouse/twin-operations/pending-lots/${selectedPendingItem.lot_id}/place`, "POST", { ...payload, idempotency_key: pendingPlacementRef.current.key });
      written = true; setRecountLotId(null); setPendingQuantity("");
      await refreshDashboard();
      setWarehouseOperationMessage(`已归位到 ${employeeLocationName(selectedLocation)}：${quantity} ${inventoryUnitLabel(selectedPendingItem.unit)}，库存总数未增加。`);
    } catch (error) {
      if (written) setPendingRefreshRequired(true);
      setWarehouseOperationMessage(written ? "货物已归位，但地图刷新失败；请刷新核对，不要重复提交。" : `归位结果未确认：${(error as Error).message}`);
    } finally {
      pendingPlacementRef.current.busy = false; setPendingPlacementBusy(false);
    }
  };

  const queueStocktakeAddDraft = () => {
    if (!canEditLocations) {
      setWarehouseOperationMessage("盘点补录会增加正式库存，只能由管理员确认；普通盘点人员可先定位或移入现有库存。");
      return;
    }
    if ((stocktakeOutsideAreaLocations.length || pendingProductExists) && !stocktakeSupplementConfirmed) {
      setWarehouseOperationMessage("仓库其他区域已有该产品，请先移入现有库存；确认现存数量仍不足后，才能补录缺少部分。");
      return;
    }
    if (selectedLocationAddBlockReason) {
      setWarehouseOperationMessage(selectedLocationAddBlockReason);
      return;
    }
    if (!selectedLocation || !selectedStocktakeCustomer || !selectedStocktakeProduct) {
      setWarehouseOperationMessage("请先确认已有客户和已有产品。 ");
      return;
    }
    const quantity = Number(stocktakeAddQuantity);
    const draft: WarehouseStocktakeDraft = {
      client_item_id: operationKey("stocktake-add"), operation: "add",
      location_id: selectedLocation.location_id, location_code: selectedLocation.location_code,
      expected_layout_version: Number(selectedLocation.map_position?.version),
      location_name: selectedLocation.location_name, floor_code: selectedLocation.floor_code,
      area_code: selectedLocation.area_code, customer_id: selectedStocktakeCustomer.id,
      customer_name: selectedStocktakeCustomer.name, product_id: selectedStocktakeProduct.product_id,
      inventory_code: selectedStocktakeProduct.product_code || selectedStocktakeProduct.customer_material_code || String(selectedStocktakeProduct.product_id),
      product_name: selectedStocktakeProduct.product_name, inventory_type: stocktakeInventoryType,
      unit: stocktakeInventoryType === "finished" ? "boxes" : "sheets", quantity,
      stock_date: stocktakeStockDate, source_kind: stocktakeSourceKind
    };
    const result = upsertStocktakeDraft(stocktakeDrafts, draft);
    if (result.error) { setWarehouseOperationMessage(result.error); return; }
    setStocktakeDrafts(result.items);
    setStocktakeBatchIdempotencyKey(operationKey("warehouse-stocktake-batch"));
    setStocktakeAddQuantity("");
    setWarehouseOperationMessage(`已加入盘点新增草稿：${selectedLocation.location_name} · ${selectedStocktakeProduct.product_name}；正式库存尚未改变。`);
  };

  const queueStocktakeDecreaseDraft = () => {
    if (!selectedLocation || !selectedStocktakeItem?.version) return;
    const blockReason = selectedLocationStocktakeBlockReason || stocktakeDecreaseBlockReason(selectedStocktakeItem);
    if (blockReason) {
      setWarehouseOperationMessage(blockReason);
      return;
    }
    const quantity = Number(stocktakeDecreaseQuantity);
    const available = Number(selectedStocktakeItem.available_quantity || 0);
    const result = upsertStocktakeDraft(stocktakeDrafts, {
      client_item_id: operationKey("stocktake-decrease"), operation: "decrease",
      location_id: selectedLocation.location_id, location_code: selectedLocation.location_code,
      expected_layout_version: Number(selectedLocation.map_position?.version),
      location_name: selectedLocation.location_name, floor_code: selectedLocation.floor_code,
      area_code: selectedLocation.area_code, lot_id: selectedStocktakeItem.lot_id,
      expected_version: selectedStocktakeItem.version, customer_name: selectedStocktakeItem.customer_name || "客户待确认",
      inventory_code: selectedStocktakeItem.inventory_code || selectedStocktakeItem.lot_number || `批次 ${selectedStocktakeItem.lot_id}`,
      product_name: selectedStocktakeItem.product_name || "产品名称待补充", unit: selectedStocktakeItem.unit || "",
      quantity, available_quantity: available, quantity_before: Number(selectedStocktakeItem.quantity || available)
    });
    if (result.error) { setWarehouseOperationMessage(result.error); return; }
    setStocktakeDrafts(result.items);
    setStocktakeBatchIdempotencyKey(operationKey("warehouse-stocktake-batch"));
    setStocktakeDecreaseQuantity("");
    setWarehouseOperationMessage(`已加入盘点调减草稿：${selectedStocktakeItem.inventory_code || selectedStocktakeItem.lot_number} 调减 ${quantity} ${inventoryUnitLabel(selectedStocktakeItem.unit)}；正式库存尚未改变。`);
  };

  const removeStocktakeDraftItem = (clientItemId: string) => {
    setStocktakeDrafts((current) => removeStocktakeDraft(current, clientItemId));
    setStocktakeBatchIdempotencyKey(operationKey("warehouse-stocktake-batch"));
    setWarehouseOperationMessage("已撤销该条盘点草稿，正式库存未改变。");
  };

  const cancelStocktakeDrafts = () => {
    setStocktakeDrafts(clearStocktakeDrafts());
    setStocktakeBatchIdempotencyKey(operationKey("warehouse-stocktake-batch"));
    setWarehouseOperationMessage("已取消全部盘点草稿，没有发送任何库存请求。");
  };

  const confirmStocktakeDrafts = async () => {
    if (!stocktakeDrafts.length || stocktakeBatchBusy || stocktakeRefreshRequired) return;
    const preview = stocktakeDrafts.slice(0, 6).map((item) => `${item.operation === "add" ? "新增" : "调减"} · ${item.location_name} · ${item.inventory_code} · ${item.quantity} ${inventoryUnitLabel(item.unit)}`).join("\n");
    if (!window.confirm(`确认一次提交 ${stocktakeDrafts.length} 条盘点调整吗？\n${preview}${stocktakeDrafts.length > 6 ? "\n……" : ""}\n\n提交成功后才会写入正式库存流水；调减到零只隐藏空卡，不删除历史。`)) return;
    setStocktakeBatchBusy(true);
    setWarehouseOperationMessage("");
    let written = false;
    try {
      const result = await mutateJson(
        "/api/warehouse/twin-operations/stocktake-batches",
        "POST",
        buildStocktakeBatchPayload(stocktakeBatchIdempotencyKey, stocktakeDrafts)
      ) as StocktakeBatchResult | null;
      written = true;
      setStocktakeRefreshRequired(true);
      setStocktakeDrafts(clearStocktakeDrafts());
      setStocktakeLastResult((result?.items || []).filter((item) => item.operation === "add"));
      setStocktakeLotId(null);
      setStocktakeDecreaseQuantity("");
      setStocktakeBatchIdempotencyKey(operationKey("warehouse-stocktake-batch"));
      await refreshDashboard();
      setStocktakeRefreshRequired(false);
      setWarehouseOperationMessage("盘点调整已整批成功，地图已刷新；新增成品可立即打印位置和产品标签。");
    } catch (reason) {
      const message = (reason as Error).message;
      setWarehouseOperationMessage(written
        ? "盘点已写入，但地图刷新失败；已提交草稿已清除，请刷新核对，不要重复补录。"
        : `盘点结果未确认：${message}。解决方法：${stocktakeBlockResolution(message)} 页面草稿与本次幂等键已保留，可核对后重试。`);
    } finally {
      setStocktakeBatchBusy(false);
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
    setLayoutStandardPallet(normalizeStandardPalletContract(raw.standard_pallet));
    setAssets(raw.assets || []);
    setFloor4CalibrationApplied(isFloor4Aligned(raw));
  };

  const refreshPublishedTwinFloor = async () => {
    const raw = await requestJson<TwinFloorResponse>(`/api/warehouse/twin-layout/floors/${floorCode}`);
    showTwinFloor(raw);
    setPlanningPublishedLayout(hydrateLayout(raw));
    setPublishedFloorRevision(raw.revision);
    setLayoutDraftControl((current) => current?.published_revision === raw.revision ? current : null);
  };

  const refreshPlanningTwinFloor = async () => {
    const [raw, publishedRaw] = await Promise.all([
      requestJson<TwinFloorDraftResponse>(`/api/warehouse/twin-layout/floors/${floorCode}/draft`),
      requestJson<TwinFloorResponse>(`/api/warehouse/twin-layout/floors/${floorCode}`)
    ]);
    showTwinFloor(raw);
    setPlanningPublishedLayout(hydrateLayout(publishedRaw));
    setPublishedFloorRevision(publishedRaw.revision);
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
    if (traceReadOnly || !canEditLocations || spatialEditBusy) return;
    setSpatialEditBusy(true);
    setLocationEditMessage("");
    try {
      if (locationEditMode) {
        await refreshPublishedTwinFloor();
        setMapMode("lookup");
        setSearchPanelOpen(true);
        setLocationEditMode(false);
        setAreaPolicyEditMode(false);
        setAdvancedAreaMaintenanceOpen(false);
        setLocationPointEditAreaCode(null);
        setLocationDrafts({});
        setRackDrafts({});
        setZonePolicyDrafts({});
        replaceZoneGeometryDrafts({});
        setSwapSourceLocationId(null);
        setLocationEditMessage("已退出布局编辑；当前显示员工正在使用的已发布地图。");
        return;
      }
      const [raw, publishedRaw] = await Promise.all([
        requestJson<TwinFloorDraftResponse>(`/api/warehouse/twin-layout/floors/${floorCode}/draft`),
        requestJson<TwinFloorResponse>(`/api/warehouse/twin-layout/floors/${floorCode}`)
      ]);
      setStaleLayoutDraft(false);
      showTwinFloor(raw);
      setPlanningPublishedLayout(hydrateLayout(publishedRaw));
      setPublishedFloorRevision(publishedRaw.revision);
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
      replaceZoneGeometryDrafts({});
      setLocationEditMessage(raw.draft_control.has_draft
        ? "检测到已保存但尚未应用的调整；当前仍显示已应用位置，只有选中对应对象编辑时才显示该对象预览。"
        : raw.draft_control.has_other_floor_drafts
          ? `区域规划已开启；${(raw.draft_control.dirty_floor_codes || []).filter((code) => code !== floorCode).join("、") || "其他楼层"} 的修改会独立保留，不影响当前楼层完成并应用。`
          : ""
      );
    } catch (reason) {
      const message = (reason as Error).message;
      setStaleLayoutDraft(message.includes("当前草稿已过期"));
      setLocationEditMessage(`打开布局草稿失败：${message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const useMergeSuggestion = (candidates: PalletMergeCandidate[]) => {
    const prepared = candidates.map((candidate) => ({
      ...candidate,
      client_item_id: operationKey("pallet-merge-source")
    }));
    setMergeSources(prepared);
    setMergeTarget(null);
    setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
    setWarehouseOperationMessage(`已把 ${prepared.length} 块同存货编码、同规格栈板加入合并草稿；请明确选择一块主栈板后再一次确认。`);
  };

  const rebuildStaleLayoutDraft = async () => {
    if (!canEditLocations || spatialEditBusy || !publishedFloorRevision) return;
    if (!window.confirm("确认放弃过期草稿，并以当前正式地图重新开始区域规划？\n\n旧草稿只会作为审计哈希保留；正式地图、库存、栈板和产品都不会改变。")) return;
    setSpatialEditBusy(true);
    try {
      await mutateJson(
        `/api/warehouse/twin-layout/floors/${floorCode}/draft/rebuild-stale`,
        "POST",
        {
          expected_published_revision: publishedFloorRevision,
          operation_key: operationKey("twin-layout-rebuild-stale")
        }
      );
      const raw = await requestJson<TwinFloorDraftResponse>(`/api/warehouse/twin-layout/floors/${floorCode}/draft`);
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
      replaceZoneGeometryDrafts({});
      setStaleLayoutDraft(false);
      setLocationEditMessage("过期草稿已放弃；区域规划现已基于当前正式地图重新开启。库存和正式地图未改变。");
    } catch (reason) {
      setLocationEditMessage(`重建布局草稿失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const returnToLookupMode = async () => {
    if (spatialEditBusy) return;
    if (activeLocationDraftCount || Object.keys(zoneGeometryDraftsRef.current).length) {
      setLocationEditMessage("还有未保存的位置调整，请先保存或取消；当前编辑继续保留。");
      return;
    }
    setSpatialEditBusy(true);
    try {
      await refreshPublishedTwinFloor();
      setLocationEditMode(false);
      setAreaPolicyEditMode(false);
      setAdvancedAreaMaintenanceOpen(false);
      setLocationPointEditAreaCode(null);
      setRackDrafts({}); setZonePolicyDrafts({}); replaceZoneGeometryDrafts({});
      setLocationDrafts({}); setSwapSourceLocationId(null);
      setMapMode('lookup'); setSearchPanelOpen(true);
      setLocationEditMessage('已返回查货模式，当前只显示已发布地图。');
    } catch (reason) {
      setLocationEditMessage(`返回查货模式失败：${(reason as Error).message}`);
    } finally { setSpatialEditBusy(false); }
  };

  const enterWarehouseMoveMode = async () => {
    if (traceReadOnly || (!canExecuteWarehouse && !canStocktake) || spatialEditBusy) return false;
    if (activeLocationDraftCount || Object.keys(zoneGeometryDraftsRef.current).length) {
      setLocationEditMessage("还有未保存的位置调整，请先保存或取消；当前编辑继续保留。");
      return false;
    }
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
      replaceZoneGeometryDrafts({});
      setLocationDrafts({});
      setSwapSourceLocationId(null);
      setLocationEditMessage("");
      setMoveAction((current) => canExecuteWarehouse && current !== "stocktake" ? current : canStocktake ? "stocktake" : "relocate");
      setWarehouseOperationMessage(stocktakeDrafts.length
        ? `已回到移货 / 盘点，保留 ${stocktakeDrafts.length} 条盘点草稿；尚未写入。`
        : "移货 / 盘点已开启：每个操作先形成页面草稿，底部一次确认后才提交。"
      );
      return true;
    } catch (reason) {
      setWarehouseOperationMessage(`进入移货 / 盘点失败：${(reason as Error).message}`);
      return false;
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const openAutomaticMerge = async () => {
    if (traceReadOnly) return;
    if (mapMode !== "move") await enterWarehouseMoveMode();
    setMoveAction("merge");
    setMoveSource(null);
    setSearchPanelOpen(false);
    setWarehouseOperationMessage("已列出同客户、同存货编码、同规格的可合并栈板；请只勾选现场确实要合并的栈板，再指定主货位。未勾选的同款栈板保持原位。");
  };

  useEffect(() => {
    if (
      traceReadOnly
      || (!pendingAreaPolicyEdit && !pendingRackEdit)
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
        setAdvancedAreaMaintenanceOpen(pendingRackEdit);
        setLocationPointEditAreaCode(null);
        setRackDrafts({});
        setZonePolicyDrafts({});
        replaceZoneGeometryDrafts({});
        setPendingAreaPolicyEdit(false);
        setLocationEditMessage(pendingRackEdit
          ? "已打开实测地图货架编辑；修改名称、层数或格数后点击保存，标签与地图会直接同步更新。"
          : "已打开阻断区域设置；处理并发布后，请返回原页面重新检查。"
        );
      } catch (reason) {
        if (!active) return;
        const message = (reason as Error).message;
        setStaleLayoutDraft(message.includes("当前草稿已过期"));
        setLocationEditMessage(`打开阻断区域设置失败：${message}`);
        setPendingAreaPolicyEdit(false);
        setPendingRackEdit(false);
      } finally {
        areaPolicyDeepLinkStartedRef.current = false;
        if (active) setSpatialEditBusy(false);
      }
    };
    void openAreaPolicy();
    return () => { active = false; };
  }, [pendingAreaPolicyEdit, pendingRackEdit, canEditLocations, layout?.id, floorCode, traceReadOnly]);

  useEffect(() => {
    if (traceReadOnly || !pendingRackEdit || !locationEditMode || !selectedRack) return;
    setAdvancedAreaMaintenanceOpen(true);
    setAreaPolicyEditMode(true);
    setPendingRackEdit(false);
    setLocationEditMessage("已定位到实测地图货架；修改名称、层数或格数后点击保存，标签与地图会直接同步更新。");
  }, [pendingRackEdit, locationEditMode, selectedRack?.id, traceReadOnly]);

  const rememberServerDraft = (revision: string) => {
    setLegacyRackBindingPreview(null);
    setLegacyRackBindingSelections({});
    setLayoutDraftControl((current) => ({
      has_draft: true,
      has_other_floor_drafts: current?.has_other_floor_drafts,
      dirty_floor_codes: Array.from(new Set([...(current?.dirty_floor_codes || []), floorCode])),
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
        : `草稿校验通过${result.warnings.length ? `：${result.warnings.slice(0, 3).join("；")}` : ""}；现在可以发布。`
      );
    } catch (reason) {
      setLocationEditMessage(`校验草稿失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const prepareLegacyRackBindingConfirmation = async (revision: string) => {
    const preview = await requestJson<LegacyRackBindingPreview>(
      `/api/warehouse/twin-layout/floors/${floorCode}/draft/rack-cell-bindings/preview?expected_revision=${encodeURIComponent(revision)}`
    );
    const priorSelections = legacyRackBindingPreview?.fingerprint === preview.fingerprint
      ? legacyRackBindingSelections
      : {};
    const selections = Object.fromEntries(preview.groups.map((group) => [
      group.binding_key,
      priorSelections[group.binding_key] || ""
    ]));
    setLegacyRackBindingPreview(preview);
    setLegacyRackBindingSelections(selections);
    if (!preview.groups.length) return { request: {}, summary: "" };
    if (preview.unresolved_count > 0) {
      setLocationEditMessage(`有 ${preview.unresolved_count} 组旧货位超出当前货架层格或目标已占用；请先修正货架层数、格数或冲突。`);
      return null;
    }
    const missing = preview.groups.filter((group) => !selections[group.binding_key]);
    if (missing.length) {
      setLocationEditMessage(`请在当前区域下方核对 ${missing.length} 组旧货位对应的实际货架，核对后再次点击“完成并应用”。`);
      return null;
    }
    const targets = Object.values(selections);
    if (new Set(targets).size !== targets.length) {
      setLocationEditMessage("同一地图货架不能绑定两组旧货位，请重新选择。");
      return null;
    }
    const occupiedCount = preview.groups.reduce((total, group) => total + group.occupied_location_count, 0);
    const mappingSummary = preview.groups.map((group) => {
      const target = group.candidates.find((candidate) => candidate.map_rack_id === selections[group.binding_key]);
      return `${group.area_code} ${group.legacy_rack_code}架 → ${target?.rack_name || selections[group.binding_key]}`;
    }).join("；");
    return {
      request: {
        legacy_rack_binding_fingerprint: preview.fingerprint,
        legacy_rack_bindings: preview.groups.map((group) => ({
          binding_key: group.binding_key,
          map_rack_id: selections[group.binding_key]
        })),
        legacy_rack_bindings_confirmed: true
      },
      summary: `${mappingSummary}\n同时保留并绑定 ${preview.groups.reduce((total, group) => total + group.location_count, 0)} 个旧货位${occupiedCount ? `（${occupiedCount} 个货位有货）` : ""}；不移动或合并库存。`
    };
  };

  const publishLayoutDraft = async () => {
    if (!layout || layoutDraftControl?.status !== "validated") return;
    setSpatialEditBusy(true);
    let publicationAcknowledged = false;
    try {
      const bindingConfirmation = await prepareLegacyRackBindingConfirmation(layout.source_sha256);
      if (bindingConfirmation === null) return;
      const moldMoveWarnings = (layoutDraftControl.warnings || []).filter((warning) => warning.includes("件模具"));
      if (!window.confirm(`确认发布 ${floorCode} 已校验的仓库地图吗？只发布当前楼层，其他楼层草稿会保留；发布前会自动备份旧地图，库存数量不会改变。${bindingConfirmation.summary ? `\n\n${bindingConfirmation.summary}` : ""}${moldMoveWarnings.length ? `\n\n${moldMoveWarnings.slice(0, 3).join("；")}。自动归位会写入模具位置移动流水。` : ""}`)) return;
      const result = await mutateJson<LayoutDraftPublishResponse>(
        `/api/warehouse/twin-layout/floors/${floorCode}/draft/publish`,
        "POST",
        {
          expected_published_revision: layoutDraftControl.published_revision,
          expected_draft_revision: layout.source_sha256,
          operation_key: operationKey("layout-publish"),
          ...bindingConfirmation.request
        }
      );
      if (!result) return;
      publicationAcknowledged = true;
      await refreshPublishedTwinFloor();
      await refreshDashboard();
      setMapMode("lookup");
      setSearchPanelOpen(true);
      setLocationEditMode(false);
      setAreaPolicyEditMode(false);
      setRackDrafts({});
      setZonePolicyDrafts({});
      replaceZoneGeometryDrafts({});
      setLegacyRackBindingPreview(null);
      setLegacyRackBindingSelections({});
      setLocationEditMessage(`${floorCode} 仓库地图已发布；旧地图备份为 ${result.backup_name}，库存数量未改变。${result.bound_legacy_location_ids?.length ? ` 已保留并绑定 ${result.bound_legacy_location_ids.length} 个旧货位。` : ""}${result.mold_location_reassignment_count ? ` ${result.mold_location_reassignment_count} 件失效模具位置已自动归入首个可用格，并记录移动流水。` : ""}${result.remaining_draft_floor_codes?.length ? ` ${result.remaining_draft_floor_codes.join("、")} 草稿仍独立保留。` : ""}`);
    } catch (reason) {
      if (publicationAcknowledged) {
        setLayoutDraftControl(null);
        setLocationEditMessage(`布局已发布，但地图或仓库记录回读失败：${(reason as Error).message}。当前画面尚未核验，请刷新页面核对；不要重复发布。`);
      } else {
        setLocationEditMessage(`发布布局失败：${(reason as Error).message}`);
      }
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const applySelectedAreaGeometry = async () => {
    if (!layout || !selectedAreaFeature || spatialEditBusy || locationEditBusy) return;
    if (activeLocationDraftCount || Object.keys(zoneGeometryDrafts).length || Object.keys(rackDrafts).length || Object.keys(zonePolicyDrafts).length) {
      setLocationEditMessage("请先保存页面上尚未保存的调整，再应用本区域。");
      return;
    }
    setSpatialEditBusy(true);
    let written = false;
    try {
      const result = await applySavedAreaGeometryRevision(
        selectedAreaFeature,
        layout.source_sha256,
        planningPublishedLayout?.source_sha256 || publishedFloorRevision,
      );
      written = true;
      setMapMode("lookup"); setSearchPanelOpen(true); setLocationEditMode(false);
      setAreaPolicyEditMode(false); setAdvancedAreaMaintenanceOpen(false);
      setLocationPointEditAreaCode(null); setLayoutMapToolsOpen(false);
      setLocationEditMessage("本区域位置已应用，查货与仓库记录已刷新；其他区域、货架和用途草稿未发布。");
      geometryApplyRequestRef.current = null;
    } catch (reason) {
      written = written || Boolean((reason as Error & { applicationWritten?: boolean }).applicationWritten);
      setLocationEditMessage(written
        ? `本区域已应用，但地图或仓库记录回读未完成：${(reason as Error).message}。请刷新核对，不要重新摆位。`
        : `本区域尚未应用：${(reason as Error).message}。已保存的调整仍保留。`);
    } finally { setSpatialEditBusy(false); }
  };

  const previewAndPublishLayout = async () => {
    if (!layout) return;
    if (!layoutDraftControl?.has_draft) {
      setSpatialEditBusy(true);
      try {
        await refreshPublishedTwinFloor();
        await refreshDashboard();
        setMapMode("lookup");
        setSearchPanelOpen(true);
        setLocationEditMode(false);
        setAreaPolicyEditMode(false);
        setAdvancedAreaMaintenanceOpen(false);
        setLocationPointEditAreaCode(null);
        setLayoutMapToolsOpen(false);
        setLocationEditMessage("地图调整已完成；当前没有未应用修改，查货正在使用最新地图。");
      } catch (reason) {
        setLocationEditMessage(`地图没有待应用修改，但正式地图回读失败：${(reason as Error).message}。请刷新页面核对。`);
      } finally {
        setSpatialEditBusy(false);
      }
      return;
    }
    setSpatialEditBusy(true);
    let publicationAcknowledged = false;
    try {
      const validation = await mutateJson<LayoutDraftValidationResponse>(
        `/api/warehouse/twin-layout/floors/${floorCode}/draft/validate`,
        "POST",
        { expected_revision: layout.source_sha256 }
      );
      if (!validation) return;
      setLayoutDraftControl((current) => current ? {
        ...current,
        status: validation.status,
        draft_revision: validation.draft_revision,
        validated_at: validation.validated_at,
        blockers: validation.blockers,
        warnings: validation.warnings
      } : current);
      if (validation.blockers.length) {
        setLocationEditMessage(`地图未应用：${validation.blockers.slice(0, 3).join("；")}。修改仍保留，可继续调整后再次完成。`);
        return;
      }
      const bindingConfirmation = await prepareLegacyRackBindingConfirmation(validation.draft_revision);
      if (bindingConfirmation === null) return;
      const result = await mutateJson<LayoutDraftPublishResponse>(
        `/api/warehouse/twin-layout/floors/${floorCode}/draft/publish`,
        "POST",
        {
          expected_published_revision: layoutDraftControl.published_revision,
          expected_draft_revision: validation.draft_revision,
          operation_key: operationKey("layout-complete-apply"),
          ...bindingConfirmation.request
        }
      );
      if (!result) return;
      publicationAcknowledged = true;
      await refreshPublishedTwinFloor();
      await refreshDashboard();
      setMapMode("lookup");
      setSearchPanelOpen(true);
      setLocationEditMode(false);
      setAreaPolicyEditMode(false);
      setAdvancedAreaMaintenanceOpen(false);
      setLocationPointEditAreaCode(null);
      setLayoutMapToolsOpen(false);
      setRackDrafts({});
      setZonePolicyDrafts({});
      replaceZoneGeometryDrafts({});
      setLegacyRackBindingPreview(null);
      setLegacyRackBindingSelections({});
      setLocationEditMessage(`${floorCode} 地图调整已保存并应用；查货正在使用新地图，旧地图备份为 ${result.backup_name}，库存数量未改变。${result.bound_legacy_location_ids?.length ? ` 已保留并绑定 ${result.bound_legacy_location_ids.length} 个旧货位。` : ""}`);
    } catch (reason) {
      if (publicationAcknowledged) {
        setLayoutDraftControl(null);
        setLocationEditMessage(`布局已发布，但地图或仓库记录回读失败：${(reason as Error).message}。当前画面尚未核验，请刷新页面核对；不要重复发布。`);
      } else {
        setLocationEditMessage(`地图未应用：${(reason as Error).message}。修改仍保留，可继续调整后再次完成。`);
      }
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
    if (!window.confirm(`确认只放弃 ${floorCode} 的布局草稿吗？其他楼层草稿、已发布地图和库存不会改变。`)) return;
    setSpatialEditBusy(true);
    let discardAcknowledged = false;
    try {
      await mutateJson(
        `/api/warehouse/twin-layout/floors/${floorCode}/draft/discard`,
        "POST",
        { expected_revision: layout.source_sha256 }
      );
      discardAcknowledged = true;
      await refreshPublishedTwinFloor();
      setMapMode("lookup");
      setSearchPanelOpen(true);
      setLocationEditMode(false);
      setAreaPolicyEditMode(false);
      setRackDrafts({});
      setZonePolicyDrafts({});
      replaceZoneGeometryDrafts({});
      setLocationEditMessage(`${floorCode} 布局草稿已放弃；其他楼层草稿、已发布地图和库存均未改变。`);
    } catch (reason) {
      if (discardAcknowledged) setLayoutDraftControl(null);
      setLocationEditMessage(discardAcknowledged
        ? `草稿已放弃，但地图回读失败：${(reason as Error).message}。请切回查货重读；当前画面尚未核验。`
        : `放弃草稿失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const moveRackDraft = (rackId: string, xMm: number, yMm: number) => {
    if (!locationEditMode || layoutMapTool !== "adjust") return;
    const original = layout?.racks.find((item) => item.id === rackId);
    if (!original) return;
    const next = {
      ...(rackDrafts[rackId] || rackDraft(original)),
      x_mm: Math.round(xMm),
      y_mm: Math.round(yMm),
    };
    setRackDrafts((current) => ({ ...current, [rackId]: next }));
    setSelected({ kind: "rack", id: rackId });
    setLocationEditMessage("货架位置已调整；核对层数和格数后点击“保存并应用货架”。");
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
    level_cell_counts: rack.mold_rack_code
      ? rack.level_cell_counts.map((count, index) => moldRackBlockedLevels(rack).includes(index + 1) ? 0 : count)
      : rack.level_cell_counts,
    bays: rack.bays,
    access_side: rack.access_side,
    min_aisle_width_mm: rack.min_aisle_width_mm,
    rotation_deg: rack.rotation_deg,
    color: rack.color
  });

  const applySavedRackRevision = async (
    rack: Rack,
    draftRevision: string,
    publishedRevision: string,
  ) => {
    const fields = {
      expected_revision: draftRevision,
      expected_published_revision: publishedRevision,
      expected_version: rack.version,
    };
    const signature = JSON.stringify([floorCode, rack.id, fields]);
    if (rackApplyRequestRef.current?.signature !== signature) {
      rackApplyRequestRef.current = { signature, operationKey: operationKey("rack-save-apply") };
    }
    const result = await mutateJson<LayoutDraftPublishResponse>(
      `/api/warehouse/twin-layout/floors/${floorCode}/racks/${rack.id}/apply`,
      "POST", { ...fields, operation_key: rackApplyRequestRef.current.operationKey },
    );
    if (!result) throw new Error("尚未取得货架应用回执，请保留当前调整后刷新核对");
    try {
      await Promise.all([refreshPlanningTwinFloor(), refreshDashboard()]);
    } catch (reason) {
      const readbackError = reason instanceof Error ? reason : new Error(String(reason));
      (readbackError as Error & { applicationWritten?: boolean }).applicationWritten = true;
      throw readbackError;
    }
    rackApplyRequestRef.current = null;
    return result;
  };

  const saveRackDraftImmediately = async (rackId: string, draft: RackDraft) => {
    if (!layout || spatialEditBusy) return;
    const original = layout.racks.find((item) => item.id === rackId);
    if (!original) return;
    const expectedPublishedRevision = planningPublishedLayout?.source_sha256 || publishedFloorRevision;
    setSpatialEditBusy(true);
    let saved = false;
    try {
      const response = await mutateJson<LayoutMutationResponse<Rack>>(
        `/api/warehouse/twin-layout/floors/${floorCode}/racks/${rackId}`,
        "PATCH",
        {
          ...rackMutationPayload(draft),
          expected_revision: layout.source_sha256,
          expected_version: original.version,
          operation_key: operationKey("rack-update")
        }
      );
      if (!response) return;
      saved = true;
      setLayout((current) => current ? {
        ...current,
        source_sha256: response.revision,
        racks: current.racks.map((item) => item.id === response.item.id ? response.item : item)
      } : current);
      rememberServerDraft(response.revision);
      await applySavedRackRevision(response.item, response.revision, expectedPublishedRevision);
      setRackDrafts((current) => {
        const next = { ...current };
        delete next[rackId];
        return next;
      });
      setLocationEditMessage(`${response.item.name} 已保存并应用；三种模式使用同一位置，正式层格已按本货架设置同步。`);
    } catch (reason) {
      const applicationWritten = Boolean((reason as Error & { applicationWritten?: boolean }).applicationWritten);
      setLocationEditMessage(applicationWritten
        ? `货架已应用，但地图回读失败：${(reason as Error).message}。请刷新核对，不要重复保存。`
        : saved
          ? `货架已保存但尚未应用：${(reason as Error).message}。当前对象预览保留，请修正后再保存。`
          : `保存货架失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const saveSelectedRack = async () => {
    if (!selectedRack) return;
    await saveRackDraftImmediately(
      selectedRack.id,
      rackDrafts[selectedRack.id] || rackDraft(selectedRack)
    );
  };

  const numberSelectedAreaRacks = async () => {
    if (!layout || !selectedAreaFeature || spatialEditBusy) return;
    if (selectedAreaRacks.some((r) => rackDrafts[r.id] && JSON.stringify(rackMutationPayload(rackDrafts[r.id])) !== JSON.stringify(rackMutationPayload(rackDraft(layout.racks.find((original) => original.id === r.id)!))))) {
      setLocationEditMessage("请先保存或取消当前区域未保存的货架修改，再编号。");
      return;
    }
    setSpatialEditBusy(true);
    try {
      const response = await mutateJson<LayoutMutationResponse<{ racks: Rack[] }>>(
        `/api/warehouse/twin-layout/floors/${floorCode}/zones/${selectedAreaFeature.id}/number-racks`, "POST",
        { expected_revision: layout.source_sha256, operation_key: operationKey("rack-number-area") });
      if (!response) return;
      const renamed = new Map(response.item.racks.map((r) => [r.id, r]));
      setLayout((current) => current ? { ...current, source_sha256: response.revision, racks: current.racks.map((r) => renamed.get(r.id) || r) } : current);
      setRackDrafts((current) => Object.fromEntries(Object.entries(current).filter(([id]) => !renamed.has(id))));
      rememberServerDraft(response.revision);
      setLocationEditMessage(`本区域 ${renamed.size} 架已从左到右编号并保存草稿；应用前查货和货位不变。`);
    } catch (error) {
      setLocationEditMessage(`编号结果未确认：${(error as Error).message}。请重读草稿核对后再操作。`);
    } finally { setSpatialEditBusy(false); }
  };

  const addRackToSelectedArea = async () => {
    if (!layout || !selectedAreaFeature || spatialEditBusy) return;
    const width = Number(newRackSettings.width), depth = Number(newRackSettings.depth), height = Number(newRackSettings.height);
    const levels = Number(newRackSettings.levels), cells = Number(newRackSettings.cells);
    if (![width, depth, height, levels, cells].every((value) => Number.isInteger(value) && value > 0) || levels > 20 || cells > 50 || height < levels) {
      setLocationEditMessage("请填写实际长、宽、总高度和每层格数（正整数，尺寸单位为毫米）。");
      return;
    }
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
          width_mm: width,
          depth_mm: depth,
          height_mm: height,
          levels,
          level_heights_mm: Array.from({ length: levels - 1 }, (_, index) => Math.floor(height / levels) * (index + 1)),
          cargo_rows: 4,
          level_cell_counts: Array.from({ length: levels }, () => cells),
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
      setNewRackFormOpen(false);
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
          area_name: formalAreaNameDraft.trim() || employeeAreaName(selectedAreaFeature, { floorCode }),
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
    setSpatialEditBusy(true);
    setLocationEditMessage("正在确认并启用区域…");
    let areaAcknowledged = false;
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
          area_name: formalAreaNameDraft.trim() || employeeAreaName(selectedAreaFeature, { floorCode }),
          existing_area_id: existingAreaId,
          confirmed: true
        }
      );
      if (!result) return;
      areaAcknowledged = true;
      setPlanningPublishedRevision(result.published_revision);
      await Promise.all([refreshPlanningTwinFloor(), refreshDashboard()]);
      setZonePolicyDrafts({});
      replaceZoneGeometryDrafts({});
      setSelectedExistingAreaId("");
      setLocationEditMessage(
        `${result.message}；区域启用状态已写入。当前没有货物时库存数量仍显示 0。` +
        (result.advanced_draft_preserved ? "原有高级维护草稿已保留，没有随本次确认发布。" : "")
      );
    } catch (reason) {
      setLocationEditMessage(areaAcknowledged
        ? `区域已启用，但回读失败：${(reason as Error).message}。当前画面尚未核验，请刷新页面重读；不要重复确认。`
        : `区域未启用：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const applySavedAreaGeometryRevision = async (
    feature: TwinFeature,
    draftRevision: string,
    publishedRevision: string
  ) => {
    const fields = {
      expected_revision: draftRevision,
      expected_published_revision: publishedRevision,
      expected_version: feature.version,
    };
    const signature = JSON.stringify([floorCode, feature.id, fields]);
    if (geometryApplyRequestRef.current?.signature !== signature) {
      geometryApplyRequestRef.current = { signature, operationKey: operationKey("zone-geometry-save-apply") };
    }
    const result = await mutateJson<LayoutDraftPublishResponse>(
      `/api/warehouse/twin-layout/floors/${floorCode}/zones/${feature.id}/apply-geometry`,
      "POST", { ...fields, operation_key: geometryApplyRequestRef.current.operationKey },
    );
    if (!result) throw new Error("尚未取得应用回执，请保留当前调整后刷新核对");
    try {
      await Promise.all([refreshPlanningTwinFloor(), refreshDashboard()]);
    } catch (reason) {
      const readbackError = reason instanceof Error ? reason : new Error(String(reason));
      (readbackError as Error & { applicationWritten?: boolean }).applicationWritten = true;
      throw readbackError;
    }
    geometryApplyRequestRef.current = null;
    return result;
  };

  const saveLayoutFeatureGeometry = async (
    feature: TwinFeature,
    points: number[][]
  ) => {
    if (!layout || spatialEditBusy) return;
    const expectedPublishedRevision = planningPublishedLayout?.source_sha256 || publishedFloorRevision;
    setSpatialEditBusy(true);
    let saved = false;
    try {
      const response = await mutateJson<LayoutMutationResponse<TwinFeature>>(
        `/api/warehouse/twin-layout/floors/${floorCode}/features/${feature.id}/geometry`,
        "PATCH",
        {
          expected_revision: layout.source_sha256,
          expected_version: feature.version,
          operation_key: operationKey("feature-geometry"),
          points,
        }
      );
      if (!response) return;
      saved = true;
      setLayout((current) => current ? {
        ...current,
        source_sha256: response.revision,
        features: current.features.map((item) => (
          item.id === response.item.id ? { ...item, ...response.item } : item
        )),
      } : current);
      rememberServerDraft(response.revision);
      replaceZoneGeometryDrafts((current) => {
        const next = { ...current };
        delete next[feature.id];
        return next;
      });
      await applySavedAreaGeometryRevision(
        { ...feature, ...response.item }, response.revision, expectedPublishedRevision
      );
      setLocationEditMessage("区域位置与大小已保存并应用；查货、移货和规划现在使用同一坐标。");
    } catch (reason) {
      const applicationWritten = Boolean((reason as Error & { applicationWritten?: boolean }).applicationWritten);
      setLocationEditMessage(
        applicationWritten
          ? `区域已应用，但地图回读失败：${(reason as Error).message}。请刷新核对，不要重复保存。`
          : saved
          ? `区域调整已保存但尚未应用：${(reason as Error).message}。当前继续显示编辑预览，请修正红色冲突后再保存。`
          : `区域调整未保存：${(reason as Error).message}。页面调整仍保留，可修正后重试。`
      );
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const moveAreaBoundaryDraft = (id: string, deltaXmm: number, deltaYmm: number) => {
    if (!locationEditMode || !layoutMapToolsOpen || locationPointEditAreaCode || layoutMapTool !== "adjust") return;
    const feature = visualLayout?.features.find((item) => item.id === id);
    if (!feature || feature.feature_kind !== "zone") return;
    const points = translatePointsMm(feature.points, deltaXmm, deltaYmm);
    replaceZoneGeometryDrafts((current) => ({
      ...current,
      [id]: points,
    }));
    setLocationEditMessage("正在保存布局位置到管理员草稿…");
    void saveLayoutFeatureGeometry(feature as TwinFeature, points);
  };

  const saveSelectedZoneGeometry = async () => {
    if (!layout || !selectedAreaFeature) return;
    const points = zoneGeometryDraftsRef.current[selectedAreaFeature.id];
    if (!points) return;
    await saveLayoutFeatureGeometry(selectedAreaFeature, points);
  };

  const nudgeAreaBoundaryDraft = (id: string, deltaXmm: number, deltaYmm: number) => {
    if (spatialEditBusy || !layoutMapToolsOpen || layoutMapTool !== "adjust") return;
    const feature = features.find((item) => item.id === id && item.feature_kind === "zone");
    if (!feature) return;
    replaceZoneGeometryDrafts((current) => ({ ...current,
      [id]: translatePointsMm(current[id] || feature.points, deltaXmm, deltaYmm) }));
    setLocationEditMessage("区域位置未保存 · 松开方向键后保存草稿");
  };

  const nudgeLocationDraft = (id: string, dx: number, dy: number) => {
    if (!locationEditMode || layoutMapToolsOpen || locationEditBusy || spatialEditBusy) return;
    const pallet = mappedLocationPallets.find((item) => item.id === id && item.is_planning_location_slot);
    if (!pallet) return;
    const prior = locationNudgeRef.current?.id === id ? locationNudgeRef.current : { id, x: pallet.x_mm, y: pallet.y_mm };
    const next = { id, x: prior.x + dx, y: prior.y + dy };
    locationNudgeRef.current = next;
    setKeyboardLocationEditActive(true);
    moveLocationDraft(id, next.x, next.y);
  };

  const createLayoutFeature = async (points: number[][]) => {
    if (!layout || spatialEditBusy) return;
    setSpatialEditBusy(true);
    try {
      const response = await mutateJson<LayoutMutationResponse<TwinFeature>>(
        `/api/warehouse/twin-layout/floors/${floorCode}/features`,
        "POST",
        {
          expected_revision: layout.source_sha256,
          operation_key: operationKey("feature-create-zone"),
          feature_kind: "zone",
          points,
          width_mm: null,
          direction: null,
        }
      );
      if (!response) return;
      setLayout((current) => current ? {
        ...current,
        source_sha256: response.revision,
        features: [...current.features, response.item],
      } : current);
      rememberServerDraft(response.revision);
      setSelected({ kind: "feature", id: response.item.id });
      setLayoutDrawPoints([]);
      setLayoutMapTool("adjust");
      setLocationEditMessage(
        "新区域已保存为管理员草稿；区域外自动作为通道，可继续调整区域。"
      );
    } catch (reason) {
      setLocationEditMessage(`新增区域失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const deleteLayoutFeature = async (feature: TwinFeature | undefined) => {
    if (!layout || !feature || spatialEditBusy) return;
    if (feature.feature_kind !== "zone") return;
    const label = "区域";
    const archivesFormalArea = feature.formal_policy_status === "published";
    const confirmation = archivesFormalArea
      ? `确认归档正式区域“${feature.name || feature.feature_code}”吗？\n\n系统会再次检查库存、栈板、资产、任务和当前配置；只有完全空闲才会归档。稳定区域、货位和历史流水都会保留。`
      : `确认从规划草稿删除${label}“${feature.name || feature.feature_code}”吗？\n\n已绑定正式区域、仍有关联货架或已锁定的对象会被系统阻止；库存、库位和已发布地图不会改变。`;
    if (!window.confirm(
      confirmation
    )) return;
    setSpatialEditBusy(true);
    setFeatureContextMenu(null);
    let deletionWritten = false;
    try {
      const key = operationKey("feature-delete");
      const query = new URLSearchParams({
        expected_revision: layout.source_sha256,
        expected_version: String(feature.version),
        operation_key: key,
      });
      if (archivesFormalArea) {
        if (!feature.formal_policy_version || !feature.formal_published_map_revision) {
          throw new Error("正式区域版本不完整，请刷新地图后重试。");
        }
        query.set("expected_policy_version", String(feature.formal_policy_version));
        query.set("expected_published_revision", feature.formal_published_map_revision);
        const management = await requestJson<AreaLocationManagement>(
          `/api/warehouse/spatial-layout/floors/${encodeURIComponent(floorCode)}/areas/${encodeURIComponent(feature.erp_area_code || "")}/management`
        );
        if (management.ground_plan_id && management.ground_plan_version) {
          query.set("retire_ground_plan", "true");
          query.set("expected_ground_plan_version", String(management.ground_plan_version));
        }
      }
      const response = await mutateJson<LayoutMutationResponse<{
        id: string;
        feature_kind: "zone" | "aisle";
        deleted: boolean;
        draft_changed?: boolean;
      }>>(
        `/api/warehouse/twin-layout/floors/${floorCode}/features/${feature.id}?${query.toString()}`,
        "DELETE"
      );
      if (!response) return;
      deletionWritten = true;
      const draftChanged = response.item.draft_changed !== false;
      setLayout((current) => {
        const removed = removeZoneHierarchy(current, feature.id, feature.erp_area_code) as Layout | null;
        return removed ? {
          ...removed,
          source_sha256: draftChanged ? response.revision : removed.source_sha256,
        } : removed;
      });
      setPlanningPublishedLayout((current) => (
        removeZoneHierarchy(current, feature.id, feature.erp_area_code) as Layout | null
      ));
      if (draftChanged) rememberServerDraft(response.revision);
      replaceZoneGeometryDrafts((current) => {
        const next = { ...current };
        delete next[feature.id];
        return next;
      });
      setZonePolicyDrafts((current) => {
        const next = { ...current };
        delete next[feature.id];
        return next;
      });
      setSelected(null);
      if (archivesFormalArea) {
        await Promise.all([refreshPlanningTwinFloor(), refreshDashboard()]);
      }
      setLocationEditMessage(
        archivesFormalArea
          ? `${label}已受控归档；旧空货位已停用，原排位和历史流水均保留。该位置现在是通道，可直接新增区域。`
          : `${label}已从规划草稿删除；库存、正式库位和已发布地图均未改变。`
      );
    } catch (reason) {
      setLocationEditMessage(deletionWritten
        ? `${label}已删除，但地图或仓库记录回读未完成：${(reason as Error).message}。请刷新核对，不要重复删除。`
        : `删除${label}失败：${(reason as Error).message}`);
    } finally {
      setSpatialEditBusy(false);
    }
  };
  const deleteSelectedLayoutFeature = () => deleteLayoutFeature(selectedLayoutFeature);
  const openFeatureContextMenu = (featureId: string, clientX: number, clientY: number) => {
    if (!locationEditMode || !layoutMapToolsOpen || layoutMapTool !== "adjust") return;
    const feature = features.find((item) => item.id === featureId);
    if (!feature || feature.feature_kind !== "zone") return;
    setFeatureContextMenu({ featureId, clientX, clientY });
  };

  const beginFloor4Calibration = () => {
    if (floorCode !== "4F" || !locationEditMode || spatialEditBusy) return;
    setViewMode("2d");
    setLayers((current) => ({ ...current, structures: true }));
    setLayoutMapToolsOpen(false);
    setLayoutDrawPoints([]);
    replaceZoneGeometryDrafts({});
    setFloor4CalibrationPoints([]);
    setFloor4CalibrationOperationKey(operationKey("floor4-calibration"));
    setFloor4CalibrationMode(true);
    setLocationEditMessage("货梯标定 0/3：请点击货梯门口第一端。");
  };

  const cancelFloor4Calibration = () => {
    if (spatialEditBusy) return;
    setFloor4CalibrationMode(false);
    setFloor4CalibrationPoints([]);
    setLocationEditMessage("已取消货梯标定，4F 草稿没有改变。");
  };

  const submitFloor4Calibration = async (points: number[][]) => {
    if (!layout || floorCode !== "4F" || points.length !== 3 || spatialEditBusy) return;
    setSpatialEditBusy(true);
    setLocationEditMessage("货梯标定 3/3：正在核对门口、进深与朝向…");
    try {
      const response = await mutateJson<Floor4CalibrationMutationResponse>(
        "/api/warehouse/twin-layout/floors/4F/draft/calibrate-freight-elevator",
        "POST",
        {
          expected_revision: layout.source_sha256,
          operation_key: floor4CalibrationOperationKey,
          source_points: points,
          calibration_mode: "doorway_heading",
          confirmed: true
        }
      );
      if (!response) return;
      await refreshPlanningTwinFloor();
      setFloor4CalibrationApplied(true);
      setFloor4CalibrationMode(false);
      setFloor4CalibrationPoints([]);
      const measuredWidth = response.item.calibration.measured_door_width_mm;
      const measuredDepth = response.item.calibration.measured_depth_mm;
      setLocationEditMessage(
        measuredWidth && measuredDepth
          ? `货梯位置与朝向标定完成；四楼实测约 ${(measuredWidth / 1000).toFixed(2)} × ${(measuredDepth / 1000).toFixed(2)} m，已保存到 4F 草稿。`
          : "货梯位置与朝向标定完成，已保存到 4F 草稿。"
      );
    } catch (reason) {
      setLocationEditMessage(`货梯标定失败：${(reason as Error).message}。三点已保留供核对；请点“重新选点”后重选，或取消。`);
    } finally {
      setSpatialEditBusy(false);
    }
  };

  const handleLayoutDrawPoint = (xMm: number, yMm: number) => {
    if (floor4CalibrationMode) {
      if (spatialEditBusy || floor4CalibrationPoints.length >= 3) return;
      const next = [...floor4CalibrationPoints, [xMm, yMm]];
      setFloor4CalibrationPoints(next);
      const nextPrompt = next.length === 1
        ? "已记录门口第一端；请点击门口另一端。"
        : next.length === 2
          ? "已记录门口宽度；请点击货梯内侧后沿，最好点后沿中点。"
          : "门口两端与内侧方向已记录，正在提交…";
      setLocationEditMessage(`货梯标定 ${next.length}/3：${nextPrompt}`);
      if (next.length === 3) void submitFloor4Calibration(next);
      return;
    }
    if (!layoutDrawKind || spatialEditBusy) return;
    if (!layoutDrawPoints.length) {
      setLayoutDrawPoints([[xMm, yMm]]);
      setLocationEditMessage("已选第一个角点；再点对角位置即可生成区域草稿。");
      return;
    }
    const [startX, startY] = layoutDrawPoints[0];
    if (Math.hypot(xMm - startX, yMm - startY) < 100) {
      setLocationEditMessage("起点和终点太近，请重新选择明显不同的位置。");
      return;
    }
    if (Math.abs(xMm - startX) < 100 || Math.abs(yMm - startY) < 100) {
      setLocationEditMessage("区域的长和宽都必须大于100毫米，请重新选择对角位置。");
      return;
    }
    void createLayoutFeature([
      [startX, startY],
      [xMm, startY],
      [xMm, yMm],
      [startX, yMm],
    ]);
  };

  const toggleLayer = (key: keyof LayerVisibility) => setLayers((current) => ({ ...current, [key]: !current[key] }));
  const floorTitle = floorCode === "1F"
    ? "一楼生产车间"
    : floorCode === "3F"
      ? "三楼实测仓库"
      : "四楼成品仓库";
  const switchFocusedRack = (offset: number) => {
    if (!focusedAreaRacks.length) return;
    const nextIndex = (focusedRackIndex + offset + focusedAreaRacks.length) % focusedAreaRacks.length;
    setRackFocusId(focusedAreaRacks[nextIndex].id);
    setSelected({ kind: "rack", id: focusedAreaRacks[nextIndex].id });
  };
  const chooseRackEmptyLocation = (locationId: number) => {
    setRackFocusId(null);
    setViewMode("2d");
    setMoveAction("stocktake");
    setSelected({ kind: "pallet", id: `erp-location-${locationId}` });
    cameraFocusSequenceRef.current += 1;
    setCameraFocusTarget({ entity: { kind: "pallet", id: `erp-location-${locationId}` }, token: cameraFocusSequenceRef.current, source: "search" });
  };

  const switchWarehouseFloor = (nextFloorCode: WarehouseOperationalFloorCode) => {
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

  const closeObjectActions = (restoreFocus = true) => {
    setObjectActions(null);
    if (restoreFocus) {
      const trigger = objectActionTriggerRef.current;
      const target = trigger?.isConnected ? trigger : document.querySelector<HTMLElement>(".twin-map-pane canvas");
      if (target) { target.tabIndex = Math.max(0, target.tabIndex); target.focus({ preventScroll: true }); }
    }
  };
  const openObjectActions = (entity: NonNullable<SelectedEntity>, clientX: number, clientY: number, trigger?: HTMLElement) => {
    if (mapMode === "planning" || locationEditMode || spatialEditBusy || loading) return false;
    const supported = entity.kind === "feature"
      ? features.some((item) => item.id === entity.id && item.feature_kind === "zone")
      : entity.kind === "rack"
        ? Boolean(layout?.racks.some((item) => item.id === entity.id))
        : entity.kind === "pallet" && (
          visualLocations.some((item) => entity.id === `erp-location-${item.location_id}`)
          || dispatchStagingPallets.some((item) => entity.id === `erp-dispatch-pallet-${item.pallet_id}`)
        );
    if (!supported) return false;
    // Opening a menu selects the card only, never a move source or destination.
    objectActionTriggerRef.current = trigger || document.querySelector<HTMLElement>(".twin-map-pane canvas");
    setSelected(entity);
    setObjectActions({ entity, clientX, clientY });
    return true;
  };
  const runObjectAction = async (action: "view" | "relocate" | "stocktake") => {
    if (!objectActions || spatialEditBusy || loading || mapMode === "planning" || locationEditMode) return;
    const entity = objectActions.entity;
    if (action !== "view") {
      if (traceReadOnly || (action === "relocate" ? !canExecuteWarehouse : !canStocktake)) return;
      if (!await enterWarehouseMoveMode()) return;
      setMoveAction(action);
      setMoveSource(null);
      setMoveQuantity("");
      setMoveDraftTargetLocationId("");
      setWarehouseOperationMessage(action === "relocate"
        ? "请选择一块栈板或一个批次，再选择目标货位；尚未移货。"
        : "请核对具体货位和产品后填写盘点数量；尚未写入。");
    } else {
      setLocationDetailOpen(true);
      if (entity.kind === "rack") setRackFocusId(entity.id);
    }
    setSelected(entity);
    closeObjectActions(false);
    requestAnimationFrame(() => {
      inspectorRef.current?.focus({ preventScroll: true });
      inspectorRef.current?.scrollIntoView({ block: "nearest" });
    });
  };
  useLayoutEffect(() => {
    const menu = objectActionsRef.current;
    if (!objectActions || !menu) return;
    const bounds = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(objectActions.clientX, window.innerWidth - bounds.width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(objectActions.clientY, window.innerHeight - bounds.height - 8))}px`;
    menu.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
  }, [objectActions]);
  useEffect(() => {
    if (!objectActions) return;
    const dismiss = (event: PointerEvent) => {
      if (!objectActionsRef.current?.contains(event.target as Node)) closeObjectActions(false);
    };
    const close = () => closeObjectActions(false);
    document.addEventListener("pointerdown", dismiss);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      window.removeEventListener("resize", close);
    };
  }, [objectActions]);
  useEffect(() => { setObjectActions(null); }, [floorCode, mapMode, viewMode]);
  useEffect(() => {
    setObjectActions((current) => current?.entity.id === selected?.id && current?.entity.kind === selected?.kind ? current : null);
  }, [selected?.id, selected?.kind]);
  const objectActionsButton = selected && mapMode !== "planning" && !locationEditMode
    ? <button type="button" className="twin-object-actions-trigger" aria-haspopup="menu" aria-expanded={Boolean(objectActions)} disabled={spatialEditBusy || loading} onClick={(event) => {
      const bounds = event.currentTarget.getBoundingClientRect();
      openObjectActions(selected, bounds.left, bounds.bottom + 4, event.currentTarget);
    }}>操作</button> : null;

  useEffect(() => {
    if (!embedded || window.parent === window) return;
    window.parent.postMessage({
      source: "tianming-warehouse",
      type: "warehouse-state",
      floor_code: floorCode,
      active_lots: currentFloor?.active_lots || 0,
      occupied_locations: currentFloorOccupiedLocations,
      mapped_locations: mappedLocationPallets.length,
      unlocated_finished: unlocatedFinishedCount,
      unmatched_observations: unmatchedInventoryObservationCount,
      column_conflicts: operationalColumnConflictCount,
    }, window.location.origin);
  }, [
    embedded,
    floorCode,
    currentFloor?.active_lots,
    currentFloorOccupiedLocations,
    mappedLocationPallets.length,
    unlocatedFinishedCount,
    unmatchedInventoryObservationCount,
    operationalColumnConflictCount,
  ]);

  useEffect(() => {
    if (!embedded) return undefined;
    const receiveShellCommand = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.source !== window.parent) return;
      const payload = event.data || {};
      if (payload.source !== "tianming-erp-shell" || payload.type !== "warehouse-command") return;
      if (payload.command === "set-floor" && isWarehouseOperationalFloorCode(payload.floor_code)) {
        switchWarehouseFloor(payload.floor_code);
      }
    };
    window.addEventListener("message", receiveShellCommand);
    return () => window.removeEventListener("message", receiveShellCommand);
  }, [embedded, floorCode, locationPointEditAreaCode, mapMode, moveSource]);

  return <main className={`warehouse-twin-shell ${embedded ? "embedded-shell" : ""} ${uiMode === "large" ? "large-text" : ""} ${mapMode === "move" ? "move-mode" : ""}`}>
    {objectActions && <div ref={objectActionsRef} className="twin-object-actions-menu" role="menu" aria-label="所选对象操作" style={{ left: objectActions.clientX, top: objectActions.clientY }} onKeyDown={(event) => {
      if (event.key === "Escape") { event.preventDefault(); closeObjectActions(); return; }
      if (event.key === "Tab") { closeObjectActions(); return; }
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
      const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next]?.focus();
    }}>
      <button type="button" role="menuitem" onClick={() => void runObjectAction("view")}>查看详情</button>
      {!traceReadOnly && canExecuteWarehouse && <button type="button" role="menuitem" disabled={spatialEditBusy || loading} onClick={() => void runObjectAction("relocate")}>移货</button>}
      {!traceReadOnly && canStocktake && <button type="button" role="menuitem" disabled={spatialEditBusy || loading} onClick={() => void runObjectAction("stocktake")}>盘点</button>}
      <button type="button" role="menuitem" onClick={() => closeObjectActions()}>关闭</button>
    </div>}
    <header className={`twin-command-bar ${embedded ? "embedded" : ""}`}>
      <div className="twin-brand"><div><h1>仓库地图</h1></div><nav className="twin-floor-switch" aria-label="楼层切换">
        <button type="button" className={floorCode === "1F" ? "active" : ""} onClick={() => switchWarehouseFloor("1F")}><b>1F</b><span>生产车间</span></button>
        <button type="button" className={floorCode === "3F" ? "active" : ""} onClick={() => switchWarehouseFloor("3F")}><b>3F</b><span>成品仓库</span></button>
        <button type="button" className={floorCode === "4F" ? "active" : ""} onClick={() => switchWarehouseFloor("4F")}><b>4F</b><span>成品仓库</span></button>
      </nav>{selectedAreaCode && <div className="twin-header-area-summary"><small>{mapMode === "planning" && canEditLocations ? `当前规划区域 · ${selectedAreaCode}` : "当前区域"}</small><b>{employeeAreaName(selectedAreaFeature, { floorCode })}</b><span>{selectedAreaVisibleAreaMm2 ? `${(selectedAreaVisibleAreaMm2 / 1_000_000).toFixed(1)} m²` : "面积待确认"} · {selectedAreaLocationCount} 库位 · {selectedArea?.lot_count || 0} 批次</span></div>}<p>{floorCode === "4F" ? floor4CalibrationMode ? `${floorTitle} · 正在重新校正货梯位置与朝向` : floor4CalibrationApplied ? `${floorTitle} · 已与 3F 货梯对齐` : `${floorTitle} · 扫描规划 / 待现场标定，尚未启用正式作业` : `${floorTitle} · 正式仓库作业层`}</p></div>
      <div className="twin-command-status"><span className="live">{traceReadOnly ? "订单追溯 · 只读定位" : mapMode === "planning" ? locationPointEditAreaCode ? `区域规划 · ${locationPointEditAreaCode} 点位调整` : layoutMapToolsOpen ? "区域规划 · 调整地图" : advancedAreaMaintenanceOpen ? "区域规划 · 整理货位/货架" : "区域规划 · 核对区域" : mapMode === "move" ? moveAction === "ground" ? "地图点选成品存放" : moveAction === "stocktake" ? `盘点调整 · ${stocktakeDrafts.length} 条草稿` : moveAction === "merge" ? `移货 · 合并栈板 · ${mergeSources.length} 块已选` : `移货 · ${moveDrafts.length} 条页面草稿` : "查货模式 · 只读"}</span><b>{currentFloor?.active_lots || 0}</b><small>当前层有效批次</small></div>
      <a className="twin-ledger-link" href="/warehouse-ledger.html?tab=finished" target="_top">库存台账</a>
    </header>

    <section className="twin-toolbar">
      <div className="twin-operation-modes" role="tablist" aria-label="仓库地图操作模式">
        {mapMode === "planning" && keyboardLocationEditActive && <button type="button" disabled={locationEditBusy || activeLocationDraftCount > 0} onClick={() => { setKeyboardLocationEditActive(false); locationNudgeRef.current = null; setLocationEditMessage("货位调整已完成；当前显示已应用位置。"); }}>完成货位调整</button>}
        <button type="button" className={mapMode === "lookup" ? "active" : ""} onClick={returnToLookupMode}>查货</button>
        {!traceReadOnly && (canExecuteWarehouse || canStocktake) && <button type="button" className={mapMode === "move" ? "active" : ""} disabled={spatialEditBusy} onClick={enterWarehouseMoveMode}>移货 / 盘点</button>}
        {!traceReadOnly && canEditLocations && <button type="button" className={locationEditMode ? 'active' : ''} disabled={spatialEditBusy} onClick={toggleLayoutEditor}>{locationEditMode ? '返回查货' : '区域规划'}</button>}
        {canEditLocations && mapMode === "planning" && locationEditMode && <button type="button" className={layoutMapToolsOpen ? "active" : ""} disabled={spatialEditBusy || Boolean(locationPointEditAreaCode) || activeLocationDraftCount > 0} title={locationPointEditAreaCode || activeLocationDraftCount > 0 ? "请先保存或取消货位点位调整" : ""} onClick={() => {
          if (activeLocationDraftCount > 0) {
            setLocationEditMessage("有未保存的货位位置，请先保存或取消后再调整地图。");
            return;
          }
          if (layoutMapToolsOpen && Object.keys(zoneGeometryDraftsRef.current).length) {
            setLocationEditMessage("仍有未保存的地图调整，请选中该区域点击重试保存；当前调整保持在画面中。");
            return;
          }
          if (layoutMapToolsOpen) {
            void previewAndPublishLayout();
            return;
          }
          setFloor4CalibrationMode(false);
          setFloor4CalibrationPoints([]);
          setLayoutMapToolsOpen(true);
          setLayoutMapTool("adjust");
          setLayoutDrawPoints([]);
          setLocationEditMessage("已进入地图调整；区域移动时货位保持原位置。点击“完成并应用”会自动校验并立即更新查货地图。");
        }}>{layoutMapToolsOpen ? "完成并应用地图调整" : "调整地图"}</button>}
        {canEditLocations && floorCode === "4F" && mapMode === "planning" && locationEditMode && <>
          <button
            type="button"
            className={floor4CalibrationMode ? "active" : ""}
            disabled={spatialEditBusy || Boolean(locationPointEditAreaCode)}
            title={floor4CalibrationApplied && !floor4CalibrationMode ? "重新按门口两端和内侧后沿校正货梯位置和朝向" : "依次点选门口两端和货梯内侧后沿"}
            onClick={beginFloor4Calibration}
          >{floor4CalibrationMode ? floor4CalibrationPoints.length === 3 ? "重新选点" : `标定货梯 ${floor4CalibrationPoints.length}/3` : floor4CalibrationApplied ? "重新标定货梯/朝向" : "标定货梯/朝向"}</button>
          {floor4CalibrationMode && <button type="button" disabled={spatialEditBusy} onClick={cancelFloor4Calibration}>取消</button>}
        </>}
        {!traceReadOnly && canEditLocations && staleLayoutDraft && <button type="button" className="warning" disabled={spatialEditBusy} onClick={rebuildStaleLayoutDraft}>放弃旧草稿并重新规划</button>}
      </div>
      {traceReadOnly && <div className={`twin-deeplink-message ${traceDeepLinkMessage.includes("无法") || traceDeepLinkMessage.includes("已移位") ? "error" : ""}`} role="status" aria-live="polite">{traceDeepLinkMessage}</div>}
      {(canExecuteWarehouse || canStocktake) && mapMode === "move" && <div className="twin-toolbar-move-actions" role="tablist" aria-label="仓库地图操作类型">
        {canExecuteWarehouse && <button type="button" role="tab" aria-selected={moveAction === "relocate"} className={moveAction === "relocate" ? "active" : ""} onClick={() => { setMoveAction("relocate"); setWarehouseOperationMessage(moveDrafts.length ? `已切回移动位置；保留 ${moveDrafts.length} 条移货草稿。` : "已切回移动位置。"); }}>移动位置</button>}
        {canExecuteWarehouse && <button type="button" role="tab" aria-selected={moveAction === "ground"} className={moveAction === "ground" ? "active" : ""} onClick={() => { setMoveAction("ground"); setMoveSource(null); setGroundStorageMessage("先选择楼层和地堆区域，再选择入库产品或转位批次。"); }}>地图存放</button>}
        {P1_49C_ENABLED && canExecuteWarehouse && <button type="button" role="tab" aria-selected={moveAction === "merge"} className={moveAction === "merge" ? "active" : ""} onClick={openAutomaticMerge}>合并栈板</button>}
        {canStocktake && <button type="button" role="tab" aria-selected={moveAction === "stocktake"} className={moveAction === "stocktake" ? "active" : ""} onClick={() => { setMoveAction("stocktake"); setMoveSource(null); setWarehouseOperationMessage(stocktakeDrafts.length ? `已切到盘点调整；保留 ${stocktakeDrafts.length} 条草稿。` : "请选择正式货位进行盘点调整。"); }}>盘点调整</button>}
      </div>}
      <div className="twin-toolbar-view-tools" role="group" aria-label="地图显示工具">
      <button type="button" aria-expanded={mapHelpOpen} aria-controls="warehouse-map-help" onClick={() => setMapHelpOpen((value) => !value)}>帮助</button>
      <button type="button" className={`twin-layer-toggle ${layerPanelOpen ? "active" : ""}`} aria-expanded={layerPanelOpen} onClick={() => setLayerPanelOpen((value) => !value)}>图层</button>
      <div className="twin-segmented" aria-label="视图模式">
        <button type="button" className={viewMode === "2d" ? "active" : ""} onClick={() => setViewMode("2d")}>二维平面</button>
        <button type="button" className={viewMode === "25d" ? "active" : ""} disabled={locationEditMode || mapMode === "move"} title={mapMode === "move" ? "移货 / 盘点使用二维地图；页面草稿不会丢失" : locationEditMode ? "请先完成并应用，或取消本次修改" : ""} onClick={() => setViewMode("25d")}>等距视图</button>
      </div>
      {viewMode === "25d" && <div className="twin-camera-presets">
        <button type="button" onClick={() => setCameraPreset("north_east")}>东北</button>
        <button type="button" onClick={() => setCameraPreset("north_west")}>西北</button>
        <button type="button" onClick={() => setCameraPreset("south_east")}>东南</button>
        <button type="button" onClick={() => setCameraPreset("south_west")}>西南</button>
      </div>}
      <button type="button" className="twin-reset" onClick={() => { setCameraPreset("fit"); setViewResetToken((value) => value + 1); }}>全图复位</button>
      </div>
      {!traceReadOnly && !!dashboard?.delayed_dispatch_relocation?.candidate_count && <button type="button" className={`twin-delayed-toggle ${delayedDispatchOpen ? "active" : ""}`} aria-expanded={delayedDispatchOpen} onClick={() => setDelayedDispatchOpen((value) => !value)}>延期待送 {dashboard.delayed_dispatch_relocation.candidate_count}</button>}
      {mapMode === "planning" && floorCode === "1F" && viewMode === "2d" && canEditLocations && !locationEditMode && <button type="button" className={`twin-floor1-candidate-toggle ${floor1CandidatePlan ? "active" : ""}`} disabled={floor1CandidateBusy} onClick={previewFloor1FormalCandidates}>{floor1CandidateBusy ? "正在测算…" : "一楼区域自动生成"}</button>}
      {mapMode === "planning" && locationEditMode && (advancedAreaMaintenanceOpen || locationPointEditAreaCode) && <><button type="button" className="twin-save-location-layout" disabled={locationEditBusy || layoutMapToolsOpen || !activeLocationDraftCount} onClick={saveLocationDrafts}>{locationPointEditAreaCode ? "保存货位调整" : "保存货位调整"} {activeLocationDraftCount || ""}</button><button type="button" className="twin-cancel-location-layout" disabled={locationEditBusy || (advancedAreaMaintenanceOpen && !activeLocationDraftCount)} onClick={locationPointEditAreaCode ? cancelLocationPointEditing : () => { setLocationDrafts({}); setSwapSourceLocationId(null); setLocationEditMessage("已取消未保存的库位位置草稿。"); }}>{locationPointEditAreaCode ? "取消点位调整" : "取消位置草稿"}</button></>}
      {mapMode === "planning" && locationEditMode && advancedAreaMaintenanceOpen && <div className="twin-layout-draft-workflow">
        <span className={`status ${layoutDraftControl?.status || "none"}`}>{layoutDraftControl?.has_draft ? "有尚未应用的修改" : "当前修改已应用"}</span>
        <button type="button" className="publish" disabled={spatialEditBusy} onClick={previewAndPublishLayout}>完成并应用本层地图</button>
        <button type="button" disabled={spatialEditBusy} onClick={discardLayoutDraft}>{layoutDraftControl?.has_draft ? "取消未应用修改" : "返回查货"}</button>
      </div>}
      <div className="twin-toolbar-spacer" />
    </section>

    <section className={`twin-workspace ${layerPanelOpen ? "layers-open" : "layers-collapsed"} ${searchPanelOpen ? "context-open" : "context-collapsed"} ${locationEditMode ? "location-editing" : ""} ${mapMode === "move" && moveAction === "merge" ? "merge-active" : ""} ${focusedRack ? "rack-focused" : ""}`}>
      {layerPanelOpen && <aside className="twin-layer-rail">
        <div className="twin-rail-title"><b>图层</b></div>
        {([
          ["zones", "区域"], ["racks", "货架"], ["equipment", "设备"],
          ["structures", "原始墙柱"], ["customStructures", "补充墙柱门窗"], ["noGo", "禁放区"], ["pallets", "库位 / 栈板"], ["production", "生产投影"]
        ] as Array<[keyof LayerVisibility, string]>).map(([key, label]) => <button type="button" key={key} className={layers[key] ? "active" : ""} onClick={() => toggleLayer(key)}><i /><span>{label}</span></button>)}
      </aside>}

      {searchPanelOpen && <aside className="twin-context-rail">
        <header><h2>查货</h2></header>
        <section className="twin-global-search">
          <div className="twin-context-heading"><b>统一查货</b>{search && <button type="button" onClick={() => { setSearch(""); setSearchResponse(null); setSearchError(""); setFocusedSearchItem(null); setFocusedSearchProductKey(null); setFocusedResource(null); setCameraFocusTarget(null); setAreaInventorySearch(""); }}>清除</button>}</div>
          {pendingRelocationItems.length > 0 && <div className="twin-location-message">盘点待归位 {pendingRelocationItems.length} 批 · 点选货位后“添加货物”</div>}
          {unlocatedFinishedCount > 0 && <div className="twin-unlocated-finished-blocker">
            <div><b>待定位成品 {unlocatedFinishedCount} 批</b><span>账上有货，但没有已发布实测格位；不会借用其他区域坐标。</span></div>
            <div className="twin-unlocated-finished-list">
              {unlocatedFinishedItems.map((item) => <button type="button" key={item.lot_id} onClick={() => focusSearchItem(item)}>
                <b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b>
                <strong>{item.product_name || "产品名称待补充"}</strong>
                <span>{item.location_name || "尚未绑定正式位置"} · {item.unlocated_reason || "缺少已发布实测格位"}</span>
                <small>实存 {formatNumber(inventoryPhysicalQuantity(item))} {inventoryUnitLabel(item.unit)}{Number(item.reserved_quantity || 0) > 0 ? ` · 已预占 ${formatNumber(item.reserved_quantity)}` : ""}{Number(item.damaged_quantity || 0) > 0 ? ` · 质量冻结 ${formatNumber(item.damaged_quantity)}` : ""} · {item.lot_number || "批次待补充"}</small>
              </button>)}
            </div>
          </div>}
          <div className="twin-search-type-grid" role="tablist" aria-label="查货类型">
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
            {searchType !== "finished" && searchResponse.resources.slice(0, 60).map((item) => <button type="button" className={focusedResource?.resource_id === item.resource_id ? "selected" : ""} key={item.resource_id} onClick={() => focusLocateResource(item)}><b>{item.kind === "mold_area" || item.kind === "printing_plate_area" ? "功能区域" : item.primary_code || item.title}</b><strong>{item.title}</strong><span>{item.subtitle}</span><small>{item.floor_code === "TEXT" ? "文字位置" : item.floor_code} · {item.map_status === "mapped" ? "点击定位到地图" : "仅有文字位置"}</small></button>)}
            {searchType === "finished" && !searchProductGroups.length && <div className="twin-area-empty"><b>没有匹配的成品库存</b><span>可改用客户全称/简写、存货编码片段、产品名称或规格。</span></div>}
            {searchType !== "finished" && !searchResponse.resources.length && <div className="twin-area-empty"><b>没有匹配结果</b><span>请更换编码、名称或区域关键词。</span></div>}
          </div>}
          {focusedSearchProduct && <div className="twin-search-focus-note product-focus"><b>全部真实位置已选中</b><span>{focusedSearchProduct.customer_name} · {focusedSearchProduct.inventory_code} · 共 {formatNumber(focusedSearchProduct.total_quantity)} {inventoryUnitLabel(focusedSearchProduct.unit)} · {focusedSearchProduct.location_count} 个位置</span><div className="twin-search-floor-actions">{focusedSearchProduct.floor_summaries.map((floor) => <button type="button" key={floor.floor_code} disabled={!isWarehouseOperationalFloorCode(floor.floor_code)} className={floorCode === floor.floor_code ? "active" : ""} onClick={() => focusSearchFloor(focusedSearchProduct, floor.floor_code)}>{floor.floor_code === "UNLOCATED" ? "待定位" : floor.floor_code} · {formatNumber(floor.quantity)} {inventoryUnitLabel(focusedSearchProduct.unit)} · {floor.location_count}处</button>)}</div><div className="twin-search-location-list">{focusedSearchProduct.location_summaries.map((location) => <button type="button" key={location.key} disabled={!isWarehouseOperationalFloorCode(location.floor_code)} onClick={() => focusSearchLocation(focusedSearchProduct, location)}><b>{location.floor_code === "UNLOCATED" ? "待定位" : location.floor_code}</b><span>{location.location_name || "位置名称待完善"}</span><em>{formatNumber(location.quantity)} {inventoryUnitLabel(focusedSearchProduct.unit)} · {location.position_status === "mapped" ? "地图可定位" : "真实文字位置"}</em></button>)}</div></div>}
          {focusedResource && <div className="twin-search-focus-note"><b>{focusedResource.map_status === "mapped" ? "地图定位指引" : "文字定位指引"}</b><span>{focusedResource.prompt}</span></div>}
        </section>
      </aside>}

      <div className={`twin-stage ${focusedRack ? "rack-focused" : ""}`}>
        <div className="twin-map-pane">
          <div className="twin-stage-heading"><div><small>{floorCode} · 实测布局</small><b>{floor4CalibrationMode ? "四楼实测成品仓库（重新校正中）" : layout?.name || floorTitle}</b></div><span>{floor4CalibrationMode ? "按门口两端和内侧后沿点选 · 地图已锁定" : locationEditMode && layoutMapToolsOpen && layoutMapTool === "adjust" ? "地图调整：区域外自动为通道" : locationEditMode ? "货位摆放：拖货位；点空白选区域" : viewMode === "2d" ? "按住左键平移 · 滚轮缩放" : "左键平移 · 右键旋转 · 滚轮缩放"}</span></div>
          <div className="twin-effective-map-status" role="status">
            <b>{activeObjectPreview ? "当前对象编辑预览" : "已应用位置 · 三种模式统一"}</b>
            {layoutDraftControl?.has_draft && <span>有已保存但尚未应用的调整</span>}
            <span className="twin-location-legend"><i className="occupied" />有货 <i className="empty" />空位 <i className="located" />定位 <i className="selected" />选中 <i className="conflict" />冲突</span>
          </div>
          {loading && <div className="twin-loading">正在加载实测布局…</div>}
          {error && <div className="twin-error"><b>地图加载失败</b><span>{error}</span><button type="button" onClick={() => window.location.reload()}>重新加载</button></div>}
          {standardPalletError && <div className="twin-error"><b>栈板尺寸读取失败</b><span>{standardPalletError}</span><button type="button" onClick={() => window.location.reload()}>重新加载</button></div>}
          {visualLayout && !loading && !standardPalletError && <EditorCanvas
          layout={visualLayout}
          assets={assets}
          viewMode={viewMode}
          cameraPreset={cameraPreset}
          viewResetToken={viewResetToken}
          selected={selected}
          layers={layers}
          palletSnapEnabled={false}
          palletSnapThresholdMm={0}
          drawMode={layoutDrawKind}
          drawPoints={floor4CalibrationMode ? floor4CalibrationPoints : layoutDrawPoints}
          drawPointLabels={floor4CalibrationMode ? CALIBRATION_POINT_LABELS : EMPTY_CANVAS_IDS}
          calibrationMode={floor4CalibrationMode}
          measureMode={false}
          measurePoints={EMPTY_CANVAS_POINTS}
          onSelect={selectOperationalEntity}
          onMoveEquipment={noop}
          onMoveRack={moveRackDraft}
          onMovePallet={warehouseMoveModeActive ? draftMoveFromDrag : moveLocationDraft}
          onMoveFeature={moveAreaBoundaryDraft}
          onNudgeFeature={nudgeAreaBoundaryDraft}
          onFinishFeatureNudge={() => void saveSelectedZoneGeometry()}
          onNudgePallet={locationEditMode && !layoutMapToolsOpen ? nudgeLocationDraft : undefined}
          onFinishPalletNudge={() => { locationNudgeRef.current = null; void saveLocationDrafts(); }}
          aisleEditingEnabled={false}
          onFeatureContextMenu={locationEditMode && layoutMapToolsOpen && layoutMapTool === "adjust" ? openFeatureContextMenu : undefined}
          onEntityContextMenu={mapMode !== "planning" && viewMode === "2d" ? openObjectActions : undefined}
          onDropAsset={noop}
          onDropRack={noop}
          onDropPallet={noop}
          onDrawPoint={handleLayoutDrawPoint}
          onMeasurePoint={noop}
          productionProjections={productionProjection?.items || EMPTY_PRODUCTION_PROJECTIONS}
          highlightFeatureIds={searchHighlightFeatureIds}
          highlightedPalletIds={mapHighlightPalletIds}
          focusTarget={cameraFocusTarget}
          palletEditingOnly={locationEditMode || warehouseMoveModeActive}
          rackEditingEnabled={locationEditMode && Boolean(selectedRackEditDraft) && !spatialEditBusy}
          featureEditingEnabled={!spatialEditBusy && locationEditMode && layoutMapToolsOpen && !locationPointEditAreaCode && layoutMapTool === "adjust"}
          mapPanLocked={floor4CalibrationMode || (locationEditMode && layoutMapToolsOpen && layoutMapTool === "adjust")}
          allowPalletSelection={viewMode === "25d" || locationEditMode || canEditLocations || canExecuteWarehouse || canStocktake}
          draggablePalletIds={warehouseMoveModeActive ? movablePalletIds : layoutMapToolsOpen ? EMPTY_CANVAS_IDS : locationPointEditPalletIds}
          readOnly={!locationEditMode && !warehouseMoveModeActive}
          visualTheme="warehouse"
          showInternalCodes={mapMode === "planning" && canEditLocations}
          />}
          {featureContextMenu && (() => {
            const feature = features.find((item) => item.id === featureContextMenu.featureId);
            return feature ? <div
              className="twin-feature-context-menu"
              role="menu"
              aria-label={`${feature.name || feature.feature_code}区域操作`}
              style={{ left: featureContextMenu.clientX, top: featureContextMenu.clientY }}
              onPointerDown={(event) => event.stopPropagation()}
            >
              <button type="button" role="menuitem" disabled={spatialEditBusy} onClick={() => void deleteLayoutFeature(feature)}>删除区域</button>
              <small>{feature.formal_policy_status === "published" ? "仅空闲正式区域可归档" : "从当前规划草稿删除"}</small>
            </div> : null;
          })()}
          {focusedRack && <div className="twin-rack-map-callout"><span>地图所选</span><b>{moldRackEmployeeName(focusedRack)}</b>{objectActionsButton}</div>}
        </div>
        {focusedRack?.mold_rack_code ? <MoldRackElevation
          rack={focusedRack}
          response={moldRackResponse}
          loading={moldRackLoading}
          error={moldRackError}
          canMoveMolds={mapMode === "move" && canExecuteWarehouse}
          rackIndex={focusedRackIndex}
          rackCount={focusedAreaRacks.length || 1}
          onPrevious={() => switchFocusedRack(-1)}
          onNext={() => switchFocusedRack(1)}
          onMoldMoved={(message) => { setMoldRackRefreshToken((value) => value + 1); setWarehouseOperationMessage(message); }}
          onClose={() => setRackFocusId(null)}
        /> : focusedRack && <WarehouseRackElevation
          rack={focusedRack}
          area={focusedRackAreaCode ? areaStats.get(focusedRackAreaCode) : undefined}
          locations={focusedRackLocations}
          unboundLocationCount={unboundRackLocationCount}
          canChooseProducts={canStocktake && mapMode === "move" && moveAction === "stocktake"}
          rackIndex={focusedRackIndex}
          rackCount={focusedAreaRacks.length || 1}
          onPrevious={() => switchFocusedRack(-1)}
          onNext={() => switchFocusedRack(1)}
          onChooseEmptyLocation={chooseRackEmptyLocation}
          onClose={() => setRackFocusId(null)}
        />}
      </div>

      <aside className="twin-inspector" ref={inspectorRef} tabIndex={-1}>
        <div className="twin-location-readonly-note" role="status">
          <b>{!layout ? "地图尚未读取" : mapMode === "planning" ? activeObjectPreview ? "当前对象编辑预览" : "已应用地图" : "已发布地图"}</b>
          {mapMode === "planning" && (activeLocationDraftCount > 0 || Object.keys(zoneGeometryDrafts).length > 0 || Object.keys(rackDrafts).length > 0 || Object.keys(zonePolicyDrafts).length > 0) && <span>有未保存调整</span>}
          {mapMode === "planning" && previewOnlyLocationIds.size > 0 && <span>部分货位随区域草稿预览，真实库存位置未改变；处理区域草稿后才能调整这些货位。</span>}
        </div>
        {mapHelpOpen && <section id="warehouse-map-help" className="twin-location-readonly-note">
          <b>地图帮助</b>
          <p>区域和货位调整保存后会自动校验并只应用本次对象；成功后查货、移货和规划立即使用同一位置。冲突标红时调整仍会保存，但已应用地图保持不变。调整图形不改变库存数量或栈板绑定。</p>
          <p>正常画面始终显示已应用地图；只有选中正在修改的对象时显示该对象编辑预览。保存货架或完成地图调整后会直接应用。</p>
          <p>移货、盘点和合并中的选择先保留在页面，提交结果以各自的保存状态为准。权限限制、冲突和失败原因仍显示在对应操作处。</p>
          <button type="button" onClick={() => setMapHelpOpen(false)}>关闭帮助</button>
        </section>}
        {floor1CandidatePlan && <section className="twin-floor1-candidate-panel">
          <div className="twin-floor1-candidate-title"><div><small>1F · 实测地图候选</small><b>一次确认区域与库位</b></div><button type="button" disabled={floor1CandidateBusy} onClick={() => setFloor1CandidatePlan(null)}>关闭</button></div>
          <div className="twin-floor1-candidate-summary"><span><b>{floor1CandidatePlan.candidate_count}</b> 个区域</span><span><b>{floor1CandidatePlan.long_term_pallet_capacity}</b> 个长期栈板位</span><span><b>{floor1CandidatePlan.formal_location_count}</b> 个正式库存库位</span></div>
          <p>按 {floor1CandidatePlan.standard_pallet_mm.width}×{floor1CandidatePlan.standard_pallet_mm.depth}mm 标准栈板和已发布毫米坐标测算；区域外自动为通道，区域内仍避开设备、货架、柱子和禁放区。确认前不会写正式台账。</p>
          {floor1CandidatePlan.excluded_out_of_bounds_count > 0 && <p className="twin-floor1-candidate-warning">已排除 {floor1CandidatePlan.excluded_out_of_bounds_count} 个实测边界外台账记录：{floor1CandidatePlan.excluded_out_of_bounds.map((item) => item.feature_code).join("、")}。这些记录不显示、不计容量，也不会生成正式区域或库位。</p>}
          {floor1CandidatePlan.formal_state.archivable_legacy_area_count > 0 && <p className="twin-floor1-candidate-warning">将停用 {floor1CandidatePlan.formal_state.archivable_legacy_area_count} 个无库存、无栈板且未映射的重复空台账记录：{floor1CandidatePlan.formal_state.legacy_areas.filter((item) => item.archive_required).map((item) => item.area_code).join("、")}。真实实测区域不受影响。</p>}
          {floor1CandidatePlan.formal_state.blocking_items.length > 0 && <div className="twin-floor1-candidate-blockers"><b>当前不能确认</b>{floor1CandidatePlan.formal_state.blocking_items.map((item) => <article key={`${item.code}-${item.area_id || item.area_code || item.message}`}><div><span>{item.message}</span>{floor1CandidateBlockerDetail(item) && <small>{floor1CandidateBlockerDetail(item)}</small>}</div><a href={floor1CandidateBlockerHref(item)} target="_blank" rel="noreferrer">{item.action_label}</a></article>)}<button type="button" className="twin-floor1-candidate-recheck" disabled={floor1CandidateBusy} onClick={previewFloor1FormalCandidates}>{floor1CandidateBusy ? "正在重新检查…" : "处理完成，重新检查"}</button></div>}
          <div className="twin-floor1-candidate-list">{floor1CandidatePlan.candidates.map((item) => <article key={item.map_feature_id} className={item.long_term_capacity_eligible ? "eligible" : "excluded"}><div><b>{item.area_code}</b><span>{item.area_name}</span></div><strong>{item.planned_pallet_capacity ? `${item.planned_pallet_capacity} 个长期栈板位` : "不计长期容量"}</strong><small>{item.formal_location_count ? `生成 ${item.formal_location_count} 个正式库位 · ` : ""}{item.capacity_note}</small></article>)}</div>
          <button type="button" className="twin-primary-action" disabled={floor1CandidateBusy || floor1CandidatePlan.formal_state.blocking_conflicts.length > 0} onClick={confirmFloor1FormalCandidates}>{floor1CandidateBusy ? "正在确认…" : floor1CandidatePlan.formal_state.already_applied ? "已确认，无需重复生成" : "一次确认并启用"}</button>
          <small>确认只写区域、容量、位置与审计记录，不改变库存数量、栈板内容、订单或生产数据。</small>
        </section>}
        {floorCode === "1F" && canViewProductionProjection && <section className="twin-production-panel">
          <div className="twin-production-title"><div><small>生产周转</small><b>真实生产周转</b><span>{productionProjection?.items.length || 0} 待生产 · {productionProjection?.items.filter((item) => item.mapping && !item.mapping.target_missing).length || 0} 已定位</span></div><button type="button" aria-expanded={productionPanelOpen} onClick={() => setProductionPanelOpen((value) => !value)}>{productionPanelOpen ? "收起" : "展开"}</button></div>
          {productionPanelOpen && <div className="twin-production-details">
            <div className="twin-production-tools"><span>只读定位，不改数量和状态</span><button type="button" disabled={productionBusy || !layout} onClick={() => layout && refreshProduction(layout.id, true)}>刷新</button></div>
            {productionMessage && <div className="twin-production-message">{productionMessage}</div>}
            <input value={productionSearch} onChange={(event) => setProductionSearch(event.target.value)} placeholder="订单、客户或品号" />
            <div className="twin-production-list">
              {productionProjection && !visibleProductionTasks.length && <div className="twin-empty-note">当前没有待生产任务。</div>}
              {visibleProductionTasks.map((task) => <button type="button" key={task.source_task_id} className={productionTaskId === task.source_task_id ? "selected" : ""} onClick={() => chooseProductionTask(task)}><span><b>{task.order_number}</b><em>{task.mapping && !task.mapping.target_missing ? task.mapping.target_code : "待定位"}</em></span><strong>{task.customer_name}</strong><small>{task.product_code} · {task.product_name}</small><small>计划 {formatNumber(task.planned_quantity)} {task.production_quantity_unit === "pieces" ? "件" : "套"} · 交期 {task.delivery_date || "未填"}</small></button>)}
            </div>
              {selectedProductionTask && layout && mapMode === "planning" && <div className="twin-production-bind">
              <b>{selectedProductionTask.order_number} · 系统只读</b><small>{selectedProductionTask.customer_name} / {selectedProductionTask.product_name}</small>
              <label>定位对象<select value={productionTargetKind} onChange={(event) => { const kind = event.target.value as "pallet" | "zone"; setProductionTargetKind(kind); setProductionTargetId(kind === "pallet" ? layout.pallets[0]?.id || "" : layout.features.find((item) => item.feature_kind === "zone")?.id || ""); }}><option value="pallet">现有栈板</option><option value="zone">现有区域</option></select></label>
              <label>人工选择<select value={productionTargetId} onChange={(event) => setProductionTargetId(event.target.value)}>{productionTargetKind === "pallet" ? layout.pallets.map((item) => <option key={item.id} value={item.id}>{item.pallet_code} · {item.zone_code}</option>) : layout.features.filter((item) => item.feature_kind === "zone").map((item) => <option key={item.id} value={item.id}>{item.feature_code} · {employeeAreaName(item, { floorCode })}</option>)}</select></label>
              <button type="button" className="twin-primary-action" disabled={productionBusy || !productionTargetId} onClick={saveProductionMapping}>{selectedProductionTask.mapping ? "更新人工定位" : "确认投影到地图"}</button>
              {selectedProductionTask.mapping && <button type="button" className="twin-production-remove" disabled={productionBusy} onClick={removeProductionMapping}>移回待定位</button>}
              <small>只保存隔离地图中的任务与位置关系。</small>
            </div>}
          </div>}
        </section>}
        {locationEditMessage && <div className={`twin-location-message ${locationEditMessage.includes("失败") || locationEditMessage.includes("缺失") ? "error" : ""}`}><span>{locationEditMessage}</span>{(locationEditMessage.includes("先完成移货") || locationEditMessage.includes("移到其他已启用区域")) && <button type="button" onClick={() => { setLocationEditMode(false); setMapMode("move"); setMoveAction("relocate"); setWarehouseOperationMessage("请点选当前区域内的货物或实体栈板，再切换楼层并选择目标位置；提交前不会改动库存。"); }}>前往移货</button>}</div>}
          {mapMode === "planning" && locationEditMode && canEditLocations && layoutMapToolsOpen && <section className="twin-layout-map-tools">
          <div className="twin-layout-map-tools-top">
            <div className="twin-layout-map-tools-title"><div><small>区域规划</small><b>在地图直接调整</b></div><span>区域和货位保存后自动应用</span></div>
            <label><span>地图操作</span><select aria-label="地图操作" value={layoutMapTool} disabled={spatialEditBusy} onChange={(event) => {
              const next = event.target.value as typeof layoutMapTool;
              setLayoutMapTool(next);
              setFeatureContextMenu(null);
              setLayoutDrawPoints([]);
              setLocationEditMessage(next === "adjust"
                ? "拖动区域；区域外自动作为通道，保存后只应用当前区域。"
                : "在地图点两下：第一点和第二点确定新区域的两个对角。");
            }}><option value="adjust">调整布局</option><option value="zone">新增区域</option></select></label>
          </div>
          {layoutMapTool === "zone" && <p>{layoutDrawPoints.length ? "已确定第一点，请在地图点第二点完成。" : "点两下即可形成矩形区域；区域外自动保留为通道。"}</p>}
          {layoutMapTool === "adjust" && selectedAreaFeature && zoneGeometryDrafts[selectedAreaFeature.id] && <button type="button" disabled={spatialEditBusy} onClick={() => void saveSelectedZoneGeometry()}>重试保存区域调整</button>}
          {layoutMapTool === "adjust" && selectedLayoutFeature?.feature_kind === "zone" && <button type="button" className="twin-layout-delete" disabled={spatialEditBusy || (selectedLayoutFeature.is_locked && selectedLayoutFeature.formal_policy_status !== "published")} onClick={deleteSelectedLayoutFeature}>删除区域</button>}
          {layoutMapTool === "adjust" && selectedRackEditDraft && <label className="twin-rack-direction"><span>货架方向</span><select aria-label="货架方向" value={selectedRackEditDraft.rotation_deg % 180 === 0 ? 0 : 90} disabled={spatialEditBusy} onChange={(event) => {
            const next = { ...selectedRackEditDraft, rotation_deg: Number(event.target.value) as Rack["rotation_deg"] };
            setRackDrafts((current) => ({ ...current, [selectedRackEditDraft.id]: next }));
            setLocationEditMessage("货架方向已调整；核对后点击“保存并应用货架”。");
          }}><option value={0}>横向 0°</option><option value={90}>竖向 90°</option></select><small>按实际长宽投影，核对后统一保存。</small></label>}
          <small className="twin-layout-draft-note">区域、货位和当前货架保存后自动校验并只应用本对象；库存数量不变。</small>
        </section>}
        {(canExecuteWarehouse || canStocktake) && mapMode === "move" && <section className="twin-move-control-panel">
          <div className="twin-formal-operation-title"><b>{moveAction === "ground" ? "地图点选成品存放" : moveAction === "stocktake" ? "盘点调整草稿" : moveAction === "merge" ? "多栈合并草稿" : "移货页面草稿"}</b><span>楼层切换不丢页面草稿</span></div>
          {moveAction === "ground" ? <div className="twin-ground-storage-panel">
            <div className="twin-map-inbound-type" role="tablist" aria-label="地图存放业务类型"><button type="button" className={groundOperation === "inbound" ? "active" : ""} onClick={() => { setGroundOperation("inbound"); setGroundTransferSource(null); }}>成品入库</button><button type="button" className={groundOperation === "transfer" ? "active" : ""} onClick={() => setGroundOperation("transfer")}>库存转位</button></div>
            <p className="twin-map-pick-hint">流程：选择楼层 → 点击地堆区域 → 显示四色候选 → 点击实际位置 → 核对中文位置 → 保存一次。</p>
            {!selectedAreaCode && <small className="error">请先点击地图中的地堆区域。</small>}
            {groundOperation === "inbound" ? <>
              <label><span>1　查找已有客户</span><input value={groundCustomerQuery} onChange={(event) => { setGroundCustomerQuery(event.target.value); setGroundCustomerId(""); setGroundProductId(""); }} placeholder="客户全称、简称或编码" /></label>
              <label><span>确认客户</span><select value={groundCustomerId} onChange={(event) => { setGroundCustomerId(event.target.value); setGroundProductQuery(""); setGroundProductId(""); }}><option value="">请选择客户</option>{groundCustomers.map((item) => <option key={item.id} value={item.id}>{item.customer_code ? `${item.customer_code} · ` : ""}{item.name}</option>)}</select></label>
              <label><span>2　筛选已有产品</span><input value={groundProductQuery} disabled={!groundCustomerId} onChange={(event) => { setGroundProductQuery(event.target.value); setGroundProductId(""); }} placeholder={groundCustomerId ? "存货编码或产品名称" : "请先确认客户"} /></label>
              <label><span>确认产品</span><select value={groundProductId} disabled={!groundCustomerId} onChange={(event) => setGroundProductId(event.target.value)}><option value="">请选择产品</option>{groundProducts.map((item) => <option key={item.product_id} value={item.product_id}>{item.product_code || item.customer_material_code || item.product_id} · {item.product_name}</option>)}</select></label>
              {selectedGroundCustomer && selectedGroundProduct && <small className="twin-formal-selected">{selectedGroundCustomer.name} / {selectedGroundProduct.product_code || selectedGroundProduct.customer_material_code || "编码待补充"} / {selectedGroundProduct.product_name}</small>}
            </> : groundTransferSource ? <div className="twin-move-source-summary"><small>已选正式成品批次</small><b>{groundTransferSource.inventory_code} · {groundTransferSource.product_name}</b><span>{groundTransferSource.source_floor_code} / {groundTransferSource.source_location_name} · 最多 {groundTransferSource.max_quantity} {inventoryUnitLabel(groundTransferSource.unit)}</span><button type="button" onClick={() => setGroundTransferSource(null)}>重新选择批次</button></div> : <p>请先点地图有货位置，再在右侧产品卡中选择一个正式成品批次。</p>}
            <div className="twin-ground-layout-grid"><label><span>本次数量（只）</span><input type="number" min="1" step="1" max={groundOperation === "transfer" ? groundTransferSource?.max_quantity : undefined} value={groundQuantity} onChange={(event) => setGroundQuantity(event.target.value)} /></label><label><span>位置容量（只）</span><input type="number" min="1" step="1" value={groundCapacityQuantity} onChange={(event) => setGroundCapacityQuantity(event.target.value)} /></label>{groundOperation === "inbound" && <label><span>库存日期</span><input type="date" value={groundStockDate} onChange={(event) => setGroundStockDate(event.target.value)} /></label>}</div>
            <label className="twin-ground-large-toggle"><input type="checkbox" checked={groundLargeFootprint} onChange={(event) => { setGroundLargeFootprint(event.target.checked); setGroundSecondaryLocationId(null); }} /><span>大型货物，占用两个相邻位置（库存数量只记一次）</span></label>
            <button type="button" disabled={groundStorageBusy || !selectedAreaCode || !groundQuantity || (groundOperation === "inbound" ? !groundProductId : !groundTransferSource)} onClick={loadGroundStorageCandidates}>{groundStorageBusy ? "正在校验…" : "显示地图候选"}</button>
            {groundCandidates && <><div className="twin-ground-legend"><span>奶白：空位</span><span className="green">绿色：有货（兼容性见货位卡）</span><span className="gray">灰色：不可用/容量不足</span><span className="red">红色：冲突</span></div><div className="twin-ground-selection-summary"><b>{groundCandidates.items.find((item) => item.location_id === groundPrimaryLocationId)?.location_name || "尚未点选主位置"}</b>{groundLargeFootprint && <span>{groundCandidates.items.find((item) => item.location_id === groundSecondaryLocationId)?.location_name || "请再点相邻可用位置"}</span>}</div></>}
            {groundStorageMessage && <div className="twin-location-message">{groundStorageMessage}</div>}
            <div className="twin-ground-storage-actions"><button type="button" onClick={() => { setGroundCandidates(null); setGroundPrimaryLocationId(null); setGroundSecondaryLocationId(null); setGroundStorageMessage("已取消页面选择；库存零写入。"); }}>取消选择</button><button type="button" className="twin-primary-action" disabled={groundStorageBusy || !groundPrimaryLocationId || (groundLargeFootprint && !groundSecondaryLocationId)} onClick={saveGroundStorage}>{groundStorageBusy ? "正在保存…" : "保存到当前中文位置"}</button></div>
          </div> : moveAction === "stocktake" ? <p>盘点只在右侧所选正式货位形成新增或调减草稿；不拖动货物、不改变地图结构，底部一次确认整批提交。</p> : moveAction === "merge" ? <>
            <div className="twin-merge-filters">
              <label><span>客户</span><select aria-label="合并栈板客户筛选" value={mergeCustomerId} onChange={(event) => setMergeCustomerId(event.target.value)}><option value="">全部客户</option>{mergeCustomerOptions.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
              <label><span>存货条件</span><input value={mergeKeyword} onChange={(event) => setMergeKeyword(event.target.value)} placeholder="输入存货编码、名称、规格或货位" /></label>
              {(mergeCustomerId || mergeKeyword) && <button type="button" onClick={() => { setMergeCustomerId(""); setMergeKeyword(""); }}>清除筛选</button>}
            </div>
            {filteredMergeSuggestions.length > 0 && <div className="twin-merge-suggestions"><h3>可合并货位</h3>{filteredMergeSuggestions.slice(0, 40).map((suggestion) => <article key={suggestion.key}><div><strong>{suggestion.label}</strong><span>{employeeCustomerName(suggestion.candidates[0])} · 合计 {formatNumber(suggestion.total)} {inventoryUnitLabel(suggestion.candidates[0].unit)}</span><div className="twin-merge-checklist">{suggestion.candidates.map((item) => {
              const selected = mergeSources.some((source) => source.pallet_id === item.pallet_id);
              return <label key={item.pallet_id}><input type="checkbox" checked={selected} disabled={mergeBatchBusy} onChange={() => {
                const candidate = selected ? item : { ...item, client_item_id: operationKey("pallet-merge-source") };
                const result = togglePalletMergeSource(mergeSources, candidate);
                if (result.error) { setWarehouseOperationMessage(result.error); return; }
                setMergeSources(result.items);
                if (mergeTarget && !result.items.some((source) => source.pallet_id === mergeTarget.pallet_id)) setMergeTarget(null);
                setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
              }} /><span>{item.location_code || item.location_name}</span><b>{formatNumber(item.total_quantity)} {inventoryUnitLabel(item.unit)}</b></label>;
            })}</div></div><button type="button" disabled={mergeBatchBusy} onClick={() => useMergeSuggestion(suggestion.candidates)}>全选这组</button></article>)}</div>}
            {!filteredMergeSuggestions.length && <p className="twin-merge-empty">当前条件下没有至少两块可合并栈板。</p>}
            {mergeSources.length > 0 && <div className="twin-merge-selection-line" aria-label="已选合并货位">{mergeSources.map((item) => <button type="button" key={item.pallet_id} disabled={mergeBatchBusy} onClick={() => {
              const result = togglePalletMergeSource(mergeSources, item);
              setMergeSources(result.items);
              if (mergeTarget?.pallet_id === item.pallet_id) setMergeTarget(null);
              setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
              setWarehouseOperationMessage(`已移除来源 ${item.location_name}；正式库存未改变。`);
            }}><b>{item.location_code || item.location_name}</b><span>{formatNumber(item.total_quantity)} {inventoryUnitLabel(item.unit)}</span><em>×</em></button>)}</div>}
            {mergeSources.length >= 2 && <div className="twin-move-target-cascade">
              <div className="twin-formal-operation-title"><b>选择主货位</b></div>
              <div className="twin-merge-target-list" role="radiogroup" aria-label="主货位">{mergeTargetChoices.map((item) => <button type="button" role="radio" aria-checked={mergeTarget?.pallet_id === item.pallet_id} className={mergeTarget?.pallet_id === item.pallet_id ? "selected" : ""} key={item.pallet_id} onClick={() => chooseMergeTarget(item)}><b>{item.location_code || item.location_name}</b><span>{formatNumber(item.total_quantity)} {inventoryUnitLabel(item.unit)}</span></button>)}</div>
              {!mergeTargetChoices.length && <small className="error">已选集合中没有通过位置门禁的目标栈板；请移除不可作为目标的栈板后重选。</small>}
            </div>}
          </> : !moveSource ? <p>先点地图上的真实栈板；厂外待送区每个栈板图案都可单独选择，历史散存也可从右侧选择。</p> : <>
            <div className="twin-move-source-summary"><small>已选货物 · {moveSource.operation === "pallet_move" ? "整栈板" : "库存批次"}</small><b>{moveSource.inventory_code} · {moveSource.product_name}</b><span>{moveSource.source_floor_code} / {moveSource.source_location_name || "位置名称待完善"}</span><button type="button" onClick={() => { setMoveSource(null); setMoveQuantity(""); setMoveDraftTargetLocationId(""); setWarehouseOperationMessage("已清除本次页面选择；库存、入库来源和送货单均未改变。"); }}>重新选择货物</button></div>
            <div className="twin-move-target-cascade">
              <div className="twin-formal-operation-title"><b>在地图选择目标空货位</b><span>可切换 1F / 3F / 4F；来源不会丢失</span></div>
              {moveSource.operation === "lot_transfer" && <label><span>本次移动数量（可用＋预占，损坏不计）</span><input type="number" min="1" max={moveSource.max_quantity} step="1" value={moveQuantity} onChange={(event) => setMoveQuantity(event.target.value)} /></label>}
              <label><span>楼层（选择后同步切换地图）</span><select value={moveTargetFloorCode} onChange={(event) => {
                const nextFloor = event.target.value;
                if (isWarehouseOperationalFloorCode(nextFloor)) switchWarehouseFloor(nextFloor);
                else { setMoveTargetFloorCode(""); setMoveTargetAreaCode(""); setMoveDraftTargetLocationId(""); }
              }}><option value="">请选择楼层</option>{moveTargetFloors.map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
              <label><span>区域（也可直接点地图区域）</span><select value={moveTargetAreaCode} disabled={!moveTargetFloorCode} onChange={(event) => { setMoveTargetAreaCode(event.target.value); setMoveDraftTargetLocationId(""); }}><option value="">请选择区域</option>{moveTargetAreas.map((value) => <option value={value} key={value}>{moveTargetAreaNames.get(value) || "区域名称待完善"}</option>)}</select></label>
              <label><span>具体货位（也可直接点地图空位）</span><select value={moveDraftTargetLocationId} disabled={!moveTargetAreaCode} onChange={(event) => setMoveDraftTargetLocationId(event.target.value)}><option value="">请选择可用空货位</option>{moveTargetLocations.map((item) => <option value={item.location_id} key={item.location_id}>{employeeLocationName(item)}</option>)}</select></label>
              {moveCandidatesLoading && <small>正在读取可用空货位…</small>}
              {moveCandidatesError && <small className="error">空货位读取失败：{moveCandidatesError}</small>}
              {!moveCandidatesLoading && !moveCandidatesError && mappedMoveTargets.length === 0 && <small>当前没有可用空货位；请先确认目标区域已启用并设有空货位。</small>}
              <button type="button" className="twin-primary-action" disabled={!selectedMoveTarget || moveBatchBusy} onClick={addSelectedMoveDraft}>加入页面草稿</button>
            </div>
          </>}
        </section>}
        {locationEditMode && canEditLocations && selectedRackEditDraft && <section className="twin-rack-layout-editor">
          <div className="twin-layout-editor-title"><div><small>货架编辑 · {selectedRackEditDraft.rack_code}</small><b>{selectedRackEditDraft.name}</b></div><em>编辑中</em></div>
          <div className="twin-rack-primary-fields">
            <label className="twin-field-span-2"><span>货架名称</span><input value={selectedRackEditDraft.name} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { name: event.target.value })} /></label>
            <label><span>货架层数</span><input type="number" min="1" max="20" value={selectedRackEditDraft.levels} onChange={(event) => changeRackLevels(selectedRackEditDraft, Number(event.target.value))} /></label>
            <label><span>X 坐标 mm</span><input type="number" value={selectedRackEditDraft.x_mm} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { x_mm: Number(event.target.value) })} /></label>
            <label><span>Y 坐标 mm</span><input type="number" value={selectedRackEditDraft.y_mm} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { y_mm: Number(event.target.value) })} /></label>
            <label><span>长度 mm</span><input type="number" min="1" value={selectedRackEditDraft.width_mm} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { width_mm: Number(event.target.value) })} /></label>
            <label><span>宽度 mm</span><input type="number" min="1" value={selectedRackEditDraft.depth_mm} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { depth_mm: Number(event.target.value) })} /></label>
            <label><span>旋转角度</span><select value={selectedRackEditDraft.rotation_deg} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { rotation_deg: Number(event.target.value) as Rack["rotation_deg"] })}>{[0, 90, 180, 270].map((angle) => <option value={angle} key={angle}>{angle}°</option>)}</select></label>
            <label><span>总高度 mm</span><input type="number" min="1" value={selectedRackEditDraft.height_mm} onChange={(event) => changeRackTotalHeight(selectedRackEditDraft, Number(event.target.value))} /></label>
            <label><span>周边通行间距 mm</span><input type="number" min="0" value={selectedRackEditDraft.min_aisle_width_mm} onChange={(event) => updateRackDraft(selectedRackEditDraft.id, { min_aisle_width_mm: Number(event.target.value) })} /></label>
          </div>
          <div className="twin-rack-level-editor"><b>逐层设置（修改净高会自动合计总高度）</b>{selectedRackEditDraft.level_clear_heights_mm.map((height, index) => <div className="twin-rack-level-row" key={`${selectedRackEditDraft.id}-level-${index}`}><label><span>第 {index + 1} 层净高 mm</span><input type="number" min="1" value={height} onChange={(event) => changeRackLevelHeight(selectedRackEditDraft, index, Number(event.target.value))} /></label><label><span>第 {index + 1} 层格数</span><input type="number" min="0" max="50" value={selectedRackEditDraft.level_cell_counts[index]} onChange={(event) => changeRackLevelCellCount(selectedRackEditDraft, index, Number(event.target.value))} /></label></div>)}<small>格数填 0 表示本层不生成正式货位；保存并应用后，成品和半成品货架会同步为稳定层格，其他用途仍走原有专项台账。</small></div>
          <div className="twin-layout-editor-actions"><button type="button" className="primary" disabled={spatialEditBusy} onClick={saveSelectedRack}>保存并应用货架</button><button type="button" disabled={spatialEditBusy} onClick={() => setRackDrafts((current) => { const next = { ...current }; delete next[selectedRackEditDraft.id]; return next; })}>取消本次修改</button><button type="button" className="danger" disabled={spatialEditBusy || selectedRackEditDraft.is_locked} onClick={deleteSelectedRack}>删除货架</button></div>
          <p>保存时自动校验并只应用当前货架；成功后同步本货架正式层格，其他草稿和库存数量不改变。</p>
        </section>}
        {!traceReadOnly && delayedDispatchOpen && dashboard?.delayed_dispatch_relocation && <section className="twin-location-card twin-delayed-dispatch-board">
          <div className="twin-location-card-title"><div><small>三楼左区 · 延期待送整理</small><b>超过几天未送货</b></div><em className={dashboard.delayed_dispatch_relocation.candidate_count ? "occupied" : "empty"}>{dashboard.delayed_dispatch_relocation.candidate_count} 块</em></div>
          <label className="twin-delayed-days"><span>未送货天数</span><select value={dispatchIdleDays} onChange={(event) => setDispatchIdleDays(Number(event.target.value))}>{Array.from({ length: 30 }, (_, index) => index + 1).map((value) => <option value={value} key={value}>{value} 天</option>)}</select></label>
          <div className="twin-dispatch-summary"><b>{dashboard.delayed_dispatch_relocation.available_target_count}</b><span>个左区可用空栈板位</span><small>{dashboard.delayed_dispatch_relocation.policy.notice}</small></div>
          <div className="twin-dispatch-label-list">
            {dashboard.delayed_dispatch_relocation.items.map((candidate) => <article key={`delayed-dispatch-${candidate.pallet_id}`}>
              <button type="button" className="twin-delayed-focus" aria-label={`延期待送地图定位 ${candidate.product_names.join("、")}`} onClick={() => focusDelayedDispatchCandidate(candidate)}>
                <small>{candidate.order_number} · 已等待 {candidate.idle_days} 天</small>
                <b>{formatNumber(candidate.quantity)} {inventoryUnitLabel(candidate.unit)}</b>
                <strong>{candidate.product_names.join("、")}</strong>
                <span>{candidate.customer_name || "客户待确认"} · {candidate.source_location_name || "待送区"}</span>
              </button>
              {canExecuteWarehouse && <button type="button" className="twin-delayed-move" disabled={!candidate.can_plan_move} onClick={() => prepareDelayedDispatchMove(candidate)}>整理移货</button>}
            </article>)}
            {!dashboard.delayed_dispatch_relocation.candidate_count && <p>当前没有达到所选天数、且仍在一楼待送区的订单栈板。</p>}
          </div>
          {dashboard.delayed_dispatch_relocation.candidate_count > 0 && dashboard.delayed_dispatch_relocation.available_target_count === 0 && <p className="twin-dispatch-weather-note">三楼左区目前没有通过实测地图门禁的空栈板位，因此只提示、不允许形成移货草稿。</p>}
        </section>}
        {selectedDispatchPallet && dispatchStagingLocation && <section className="twin-location-card twin-dispatch-board">
          {objectActionsButton}
          {(() => {
            const summary = dashboardPalletSummary(selectedDispatchPallet);
            return <>
              <div className="twin-location-card-title"><div><small>厂外待送货物</small><b>{summary.product}</b></div><em className="occupied">待送货</em></div>
              <div className="twin-selection-summary"><span><small>产品</small><b>{summary.product}</b></span><span><small>客户</small><b>{summary.customer}</b></span><span><small>数量</small><b>{formatNumber(summary.quantity)} {inventoryUnitLabel(summary.unit)}</b></span></div>
              <p className="twin-dispatch-weather-note">地图上的这一块图案对应这一块真实栈板；位置点只表示它位于实测厂外待送区，不伪造未登记的区内坐标。</p>
              {canExecuteWarehouse && <button type="button" className="twin-primary-action" onClick={async () => {
                if (mapMode !== "move") await enterWarehouseMoveMode();
                chooseMoveSource(palletMoveSource(dispatchStagingLocation, selectedDispatchPallet));
              }}>移动这块栈板到 1F / 3F / 4F</button>}
            </>;
          })()}
        </section>}
        {selectedFeatureIsMeasuredDispatch && <section className="twin-location-card twin-dispatch-board">
          <div className="twin-location-card-title"><div><small>一楼成品待送</small><b>{selectedFeature?.name || "一楼成品待送区"}</b></div><em className="occupied">临时待装车</em></div>
          <p className="twin-dispatch-weather-note">只使用当前实测地图内已经确认的待送区域轮廓；不向图外补画或扩展区域。</p>
          <div className="twin-dispatch-summary"><b>{dispatchStagingPallets.length}</b><span>块真实待送栈板</span><small>点击栈板可实时查看产品、客户和数量；转走后该区数量同步减少。另有 {dispatchStagingItems.length} 条未绑定实体栈板的历史散存。</small></div>
          <div className="twin-dispatch-label-list">
            {dispatchStagingPallets.map((pallet) => {
              const summary = dashboardPalletSummary(pallet);
              return <button type="button" className={(mapMode === "move" && moveAction === "relocate" ? moveSource?.source_key === `pallet:${pallet.pallet_id}` : selectedDispatchPallet?.pallet_id === pallet.pallet_id) ? "selected" : ""} key={`dispatch-pallet-${pallet.pallet_id}`} onClick={() => {
                setSelected({ kind: "pallet", id: `erp-dispatch-pallet-${pallet.pallet_id}` });
                if (mapMode === "move" && moveAction === "relocate" && canExecuteWarehouse && dispatchStagingLocation) chooseMoveSource(palletMoveSource(dispatchStagingLocation, pallet));
              }}>
                <small>实物栈板 · {dispatchStagingLocation ? employeeLocationName(dispatchStagingLocation) : "待送区"}</small>
                <b>{formatNumber(summary.quantity)} {inventoryUnitLabel(summary.unit)}</b>
                <strong>{summary.product}</strong>
                <span>{summary.customer}</span>
              </button>;
            })}
            {dispatchStagingItems.map((item, itemIndex) => <button type="button" className={mapMode === "move" && moveAction === "relocate" && moveSource?.source_key === `lot:${item.lot_id}` ? "selected" : ""} key={item.lot_id || `dispatch-${itemIndex}`} onClick={() => {
              if (mapMode === "move" && moveAction === "relocate" && canExecuteWarehouse && dispatchStagingLocation) {
                chooseMoveSource(lotMoveSource(dispatchStagingLocation, item));
              }
            }}>
              <small>散存待送 · 未绑定实体栈板</small>
              <b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b>
              <strong>{item.product_name || "产品名称待补充"}</strong>
              <span>{item.customer_name || "客户待确认"}</span>
              <em>{formatNumber(inventoryLabelQuantity(item))} {inventoryUnitLabel(item.unit)}</em>
            </button>)}
            {dispatchStagingPallets.length === 0 && dispatchStagingItems.length === 0 && <p>当前待送区没有货物。</p>}
          </div>
          {mapMode === "move" && moveAction === "relocate" && canExecuteWarehouse && <p className="twin-dispatch-weather-note">点击上方栈板或散存标签后，可随意切换 1F / 3F / 4F 地图；来源不会丢失，再点区域和具体空货位即可移动，不会改变订单和后续送货关系。</p>}
          {warehouseOperationMessage && <div className="twin-location-message">{warehouseOperationMessage}</div>}
        </section>}
        {selectedLocation && <section className="twin-location-card">
          {objectActionsButton}
          {!traceReadOnly && canStocktake && !locationEditMode && <button type="button" className="twin-primary-action" disabled={loading || pendingPlacementBusy} onClick={() => { setMapMode("move"); setMoveAction("stocktake"); setSearchPanelOpen(true); }}>添加货物</button>}
          <div className="twin-location-card-title"><div><small>当前位置</small><b>{employeeLocationName(selectedLocation)}</b></div><em className={selectedLocation.occupancy_status}>{selectedLocation.occupancy_status === "occupied" ? "有货" : "空位"}</em></div>
          {selectedLocation.has_unmatched_inventory_observation && <div className="twin-unmatched-observation"><b>现场有货但系统未匹配 · 待管理员核对</b>{(selectedLocation.unmatched_inventory_observations || []).map((item) => <p key={item.id}><strong>{item.inventory_keyword}</strong>{item.customer_keyword ? ` · ${item.customer_keyword}` : ""}{item.reported_quantity ? ` · 约 ${item.reported_quantity}${item.reported_unit || ""}` : ""}<span>{item.reason}</span></p>)}</div>}
          {selectedLocation.has_location_discrepancy && <div className="twin-unmatched-observation"><b>现场位置与系统登记不符 · 持续标红</b>{(selectedLocation.location_discrepancies || []).map((item) => <p key={item.id}><strong>{item.lot.inventory_code || item.lot.lot_number || `批次 ${item.lot.lot_id}`}</strong>{item.reported_quantity ? ` · ${formatNumber(item.reported_quantity)} ${inventoryUnitLabel(item.lot.unit)}` : ""}<span>{item.reason}</span></p>)}</div>}
          <div className="twin-selection-summary"><span><small>货物</small><b>{selectedLocationItems.length} 条</b></span><span><small>客户</small><b>{selectedLocationCustomerLabel}</b></span><span><small>栈板</small><b>{selectedLocationPallets.length || 0} 块</b></span></div>
          {selectedLocationItems.length === 0 && <p className="twin-location-empty-primary">该位置当前没有货物</p>}
          {mapMode === "lookup" && selectedLocationCompositeParentSummaries.map(({ item, summary }) => <article className="twin-location-item twin-composite-parent-item" key={summary.group_key}>
            <div className="twin-location-item-code"><b>{summary.inventory_code || `组合父件 ${summary.order_item_id}`}</b><strong>{summary.available_set_quantity > 0 ? `${formatNumber(summary.available_set_quantity)} 套` : "待齐套"}</strong></div>
            <h4>{summary.product_name}</h4>
            <div className="twin-location-item-summary"><span>{item.customer_name || "客户待确认"} · {summary.order_number}</span></div>
            <small>按最短组件自动计算；底层 {summary.component_lot_count} 个正式批次仍独立追溯</small>
            <div className="twin-composite-component-lines">{summary.components.map((component) => <span key={component.snapshot_id}>{component.product_code || component.product_name || `组件 ${component.snapshot_id}`}：{formatNumber(component.available_piece_quantity)} 件 / 每套 {formatNumber(component.quantity_per_set)} 件{component.is_required ? "" : "（可选）"}</span>)}</div>
          </article>)}
          {visibleSelectedLocationItems.map((item, itemIndex) => {
            const stocktakeBlockReason = mapMode === "move" && moveAction === "stocktake"
              ? selectedLocationStocktakeBlockReason || stocktakeDecreaseBlockReason(item)
              : null;
            return <button type="button" className={`twin-location-item ${stocktakeLotId === item.lot_id ? "correction-selected" : ""} ${(traceFocusedLotId && traceFocusedLotId === item.lot_id) || (focusedSearchProductKey && searchProductKey(item) === focusedSearchProductKey) ? "warehouse-search-hit" : ""} ${stocktakeBlockReason ? "stocktake-ineligible" : ""}`} key={item.lot_id || `${item.inventory_code}-${itemIndex}`} disabled={Boolean(stocktakeBlockReason)} title={stocktakeBlockReason || ""} onClick={() => {
              if (mapMode === "move" && moveAction === "stocktake") {
                setStocktakeLotId(item.lot_id || null);
                setStocktakeDecreaseQuantity("");
              }
              setWarehouseOperationMessage("");
            }}>
              <div className="twin-location-item-code"><b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b><strong>{formatNumber(inventoryLabelQuantity(item))} {inventoryUnitLabel(item.unit)}</strong></div>
              <h4>{item.product_name || "产品名称待补充"}</h4>
              <div className="twin-location-item-summary"><span>{item.customer_name || "客户待确认"}</span></div>
              {stocktakeBlockReason && <>
                <small className="twin-stocktake-block-reason">不可盘点调减：{stocktakeBlockReason}</small>
                <small className="twin-stocktake-resolution">解决方法：{stocktakeBlockResolution(stocktakeBlockReason)}</small>
              </>}
            </button>;
          })}
          {mapMode === "lookup" && selectedLocationTraceItems.length > 4 && <button type="button" className="twin-detail-toggle" aria-expanded={locationItemsExpanded} onClick={() => setLocationItemsExpanded((current) => !current)}>{locationItemsExpanded ? "收起货物" : `查看全部 ${selectedLocationTraceItems.length} 条货物`}</button>}
          {displayedLocationConflictIds.has(`erp-location-${selectedLocation.location_id}`) && <p className="twin-location-column-warning">{locationEditMode ? "该货位越界，或与其他货位、柱子、设备、货架、禁放区冲突，可先保存调整，再拖到安全位置；应用前会核对冲突。" : selectedLocationPlanningWarning}</p>}
          <button type="button" className="twin-detail-toggle secondary" aria-expanded={locationDetailOpen} onClick={() => setLocationDetailOpen((current) => !current)}>{locationDetailOpen ? "收起位置与栈板详情" : "位置与栈板详情"}</button>
          {locationDetailOpen && <div className="twin-location-secondary">
            <h3>{selectedLocation.location_name}</h3>
            <dl>
              <div><dt>位置</dt><dd>{employeeLocationName(selectedLocation)}</dd></div>
              <div><dt>地图状态</dt><dd>{locationDrafts[selectedLocation.location_id] ? "未保存草稿" : selectedLocation.position_status === "mapped" ? "已确认布局" : "待布局"}</dd></div>
              <div><dt>点位方式</dt><dd>{selectedLocation.map_position?.source_type === "manual" && Number(selectedLocation.map_position.version) > 1 ? "手工固定" : selectedLocation.map_position?.source_type === "manual" ? "历史系统点位（可自动接管）" : "系统均匀排布"}</dd></div>
              <div><dt>占地语义</dt><dd>{selectedLocation.map_position?.layout_kind === "physical_pallet" ? "实体栈板占地" : selectedLocation.map_position?.layout_kind === "logical_anchor" ? "逻辑货位点（非实尺度）" : "历史尺寸待确认"}</dd></div>
              <div><dt>实物栈板</dt><dd>{selectedLocationPallets.length ? `${selectedLocationPallets.length} 块` : "当前无栈板"}</dd></div>
              <div><dt>库存明细</dt><dd>{selectedLocationItems.length} 条</dd></div>
            </dl>
          </div>}
          {viewMode === "25d" && <p className="twin-location-readonly-note">等距视图仅查看库位与货物标签；调整请切换二维平面。</p>}
          {warehouseOperationMessage && <div className="twin-location-message">{warehouseOperationMessage}</div>}
          {canExecuteWarehouse && mapMode === "move" && moveAction !== "stocktake" && moveAction !== "ground" && viewMode === "2d" && <section className="twin-move-source-panel">
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
                    : !pallet.version || pallet.move_eligible === false;
                  return <button type="button" aria-pressed={moveAction === "merge" ? mergeSelected : undefined} title={moveAction === "merge" ? mergeCandidate.error || mergeCompatibility.error || "" : pallet.move_block_reason || ""} className={moveAction === "merge" ? mergeSelected ? "selected merge-selected" : disabled ? "merge-ineligible" : "" : moveSource?.source_key === `pallet:${pallet.pallet_id}` ? "selected" : ""} disabled={disabled} key={`move-pallet-${pallet.pallet_id}`} onClick={() => moveAction === "merge" ? toggleMergeSource(selectedLocation, pallet) : chooseMoveSource(palletMoveSource(selectedLocation, pallet))}>
                    <b>{moveAction === "merge" ? mergeSelected ? "已选合并货物" : "加入合并集合" : "整栈移动"} · {employeeCustomerName(firstItem)}</b>
                    <span>{firstItem?.inventory_code || "存货编码待补充"} · {firstItem?.product_name || "产品名称待补充"}{productCount > 1 ? ` 等 ${productCount} 款` : ""}</span>
                    <small>{moveAction !== "merge" && pallet.move_eligible === false ? pallet.move_block_reason || "当前不能整板移货，请刷新核对" : mergeCandidate.candidate ? `${mergeCandidate.candidate.lot_count} 个正式批次 · 合计 ${formatNumber(mergeCandidate.candidate.total_quantity)} ${inventoryUnitLabel(mergeCandidate.candidate.unit)} · ${mergeCandidate.candidate.inventory_status === "frozen" ? "冻结" : "可用"}` : mergeCandidate.error || `${pallet.item_count ?? pallet.items.length} 条库存明细 · 合计 ${formatNumber(totalQuantity)} ${inventoryUnitLabel(firstItem?.unit)}`}</small>
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
          {canExecuteWarehouse && mapMode === "move" && moveAction === "ground" && groundOperation === "transfer" && viewMode === "2d" && <section className="twin-move-source-panel">
            <div className="twin-formal-operation-title"><b>选择要转位的成品批次</b><span>批次和来源保持独立</span></div>
            <div className="twin-move-lot-list">{selectedLocationItems.filter((item) => item.inventory_type === "finished").map((item) => {
              const source = lotMoveSource(selectedLocation, item);
              return <button type="button" className={groundTransferSource?.source_key === `lot:${item.lot_id}` ? "selected" : ""} disabled={!source?.customer_id || !source.product_id} key={`ground-transfer-${item.lot_id}`} onClick={() => {
                if (!source) return;
                setGroundTransferSource(source);
                setGroundQuantity(String(source.max_quantity || source.quantity || ""));
                setGroundCandidates(null);
                setGroundPrimaryLocationId(null);
                setGroundSecondaryLocationId(null);
                setGroundStorageIdempotencyKey(operationKey("ground-storage"));
                setGroundStorageMessage(`已选 ${source.inventory_code}；请点击目标地堆区域并显示候选。`);
              }}><b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b><span>{item.product_name || "产品名称待补充"}</span><small>可转位 {formatNumber(movableLotQuantity(item))} {inventoryUnitLabel(item.unit)}</small></button>;
            })}</div>
            {!selectedLocationItems.some((item) => item.inventory_type === "finished") && <p>当前位置没有可转位的正式成品批次。</p>}
          </section>}
          {canStocktake && mapMode === "move" && moveAction === "stocktake" && viewMode === "2d" && !locationEditMode && selectedLocationBaseReceivable && <section className="twin-formal-operation twin-stocktake-operation">
            <div className="twin-formal-operation-title"><b>正式货位盘点调整</b><span>{selectedLocation.floor_code} · {selectedLocation.area_code || "未分区"} · {selectedLocation.location_name}</span></div>
            {canEditLocations && <fieldset className="twin-pending-placement" disabled={pendingPlacementBusy}>
              <legend>优先添加待归位货物</legend>
              <label><span>查找待归位货物</span><input value={pendingQuery} onChange={(event) => setPendingQuery(event.target.value)} placeholder="存货编码、产品或客户" /></label>
              <div className="twin-search-result-list">{matchingPendingItems.slice(0, 20).map((item) => <button type="button" key={item.lot_id} aria-pressed={recountLotId === item.lot_id} disabled={item.status !== "active" || !item.version || Number(item.damaged_quantity || 0) > 0 || pendingRefreshRequired} onClick={() => { setRecountLotId(item.lot_id); setPendingQuantity(String(movableLotQuantity(item))); }}>
                <b>{item.inventory_code} · {item.product_name}</b><span>{employeeCustomerName(item)} · 待归位 {formatNumber(movableLotQuantity(item))} {inventoryUnitLabel(item.unit)}</span><small>{item.lot_number}{item.status !== "active" ? " · 当前冻结，暂不能归位" : ""}</small>
              </button>)}</div>
              {matchingPendingItems.length > 20 && <small>符合 {matchingPendingItems.length} 批，显示前20批，可继续输入缩小范围。</small>}
              {!matchingPendingItems.length && <p>待归位中没有匹配货物，可在下方补录实际新增库存。</p>}
              {selectedPendingItem && <div className="twin-correction-form"><b>{selectedPendingItem.product_name} → {employeeLocationName(selectedLocation)}</b><label><span>本次归位数量（{inventoryUnitLabel(selectedPendingItem.unit)}）</span><input type="number" min="1" max={movableLotQuantity(selectedPendingItem)} step="1" value={pendingQuantity} onChange={(event) => setPendingQuantity(event.target.value)} /></label>
                {selectedLocationFinishedAddBlockReason && <small>{selectedLocationFinishedAddBlockReason}</small>}
                <button type="button" className="twin-primary-action" disabled={pendingRefreshRequired || Boolean(selectedLocationFinishedAddBlockReason) || !Number.isInteger(Number(pendingQuantity)) || Number(pendingQuantity) <= 0 || Number(pendingQuantity) > movableLotQuantity(selectedPendingItem)} onClick={() => void placePendingInventory()}>{pendingPlacementBusy ? "正在归位…" : "确认归位"}</button>
                <button type="button" onClick={() => { setRecountLotId(null); setPendingQuantity(""); }}>取消选择</button></div>}
              {pendingRefreshRequired && <button type="button" onClick={() => void refreshDashboard().then(() => setPendingRefreshRequired(false)).catch((error) => setWarehouseOperationMessage(`刷新仍未完成：${error.message}`))}>刷新核对归位结果</button>}
            </fieldset>}
            <div className="twin-formal-divider"><span>待归位没有的货物：新增库存</span></div>
            <div className="twin-map-inbound-type" role="tablist" aria-label="盘点新增库存类型">
              <button type="button" className={stocktakeInventoryType === "finished" ? "active" : ""} disabled={Boolean(selectedLocationFinishedAddBlockReason)} title={selectedLocationFinishedAddBlockReason || ""} onClick={() => { setStocktakeInventoryType("finished"); setStocktakeCustomerId(""); setStocktakeProductId(""); setStocktakeSupplementConfirmed(false); }}>成品 · 固定单位箱</button>
              <button type="button" className={stocktakeInventoryType === "semi_finished" ? "active" : ""} disabled={Boolean(selectedLocationSemiFinishedAddBlockReason)} title={selectedLocationSemiFinishedAddBlockReason || ""} onClick={() => { setStocktakeInventoryType("semi_finished"); setStocktakeCustomerId(""); setStocktakeProductId(""); setStocktakeSupplementConfirmed(false); }}>半成品 · 固定单位张</button>
            </div>
            {selectedLocationAddBlockReason && <p className="twin-stocktake-block-reason">当前新增类型不可用：{selectedLocationAddBlockReason}</p>}
            <label><span>库存实际来源</span><select value={stocktakeSourceKind} onChange={(event) => setStocktakeSourceKind(event.target.value as typeof stocktakeSourceKind)}><option value="existing_stocktake">本厂现场盘点发现</option><option value="partner_transfer">合作纸箱厂搬入</option></select></label>
            <label><span>1　查找已有客户</span><input value={stocktakeCustomerQuery} onChange={(event) => { setStocktakeCustomerQuery(event.target.value); setStocktakeCustomerId(""); setStocktakeProductId(""); setStocktakeSupplementConfirmed(false); }} placeholder="客户全称、中文简称、缩写或客户编码" /></label>
            <label><span>确认已有客户</span><select value={stocktakeCustomerId} onChange={(event) => { setStocktakeCustomerId(event.target.value); setStocktakeProductQuery(""); setStocktakeProductId(""); setStocktakeSupplementConfirmed(false); }}><option value="">请选择客户</option>{stocktakeCustomers.map((item) => <option key={item.id} value={item.id}>{item.chinese_short_name ? `${item.chinese_short_name} · ` : ""}{item.customer_code ? `${item.customer_code} · ` : ""}{item.name}</option>)}</select></label>
            <label><span>2　筛选该客户已有产品</span><input value={stocktakeProductQuery} disabled={!stocktakeCustomerId} onChange={(event) => { setStocktakeProductQuery(event.target.value); setStocktakeProductId(""); setStocktakeSupplementConfirmed(false); }} placeholder={stocktakeCustomerId ? "存货编码、客户料号或产品名称；留空显示候选" : "请先确认客户"} /></label>
            <label><span>确认已有产品</span><select value={stocktakeProductId} disabled={!stocktakeCustomerId} onChange={(event) => { setStocktakeProductId(event.target.value); setStocktakeSupplementConfirmed(false); }}><option value="">请选择产品</option>{stocktakeProductCandidates.map((item) => <option key={item.product_id} value={item.product_id}>{item.product_code || item.customer_material_code || item.product_id} · {item.product_name}</option>)}</select></label>
            {selectedStocktakeProduct && <small className="twin-formal-selected">{selectedStocktakeProduct.customer_name} / {selectedStocktakeProduct.product_code || selectedStocktakeProduct.customer_material_code || "编码待补充"} / {selectedStocktakeProduct.product_name}</small>}
            {selectedStocktakeProduct && <div className="twin-stocktake-existing-panel">
              <div className="twin-formal-operation-title"><b>先核对仓库现有库存</b><span>{stocktakeOutsideAreaAvailable} {inventoryUnitLabel(stocktakeInventoryType === "finished" ? "boxes" : "sheets")} 在其他区域可用</span></div>
              {stocktakeOutsideAreaLocations.length ? <div className="twin-stocktake-existing-list">{stocktakeOutsideAreaLocations.map((match) => <article key={`stocktake-existing-${match.lot_id}`}>
                <button type="button" className="twin-stocktake-existing-focus" onClick={() => focusStocktakeExistingLocation(match)}><b>{match.source_location_name}</b><span>{match.inventory_code || selectedStocktakeProduct.product_name}</span><small>可用 {formatNumber(match.available_quantity)} {inventoryUnitLabel(match.unit)}</small></button>
                {match.inventory_type === "finished" && canExecuteWarehouse && <button type="button" className="twin-stocktake-existing-move" disabled={selectedLocation.occupancy_status !== "empty" || !match.version} onClick={() => queueStocktakeExistingMove(match)}>先移入这里</button>}
              </article>)}</div> : <p>其他区域没有该产品的可用库存，可以按现场实际缺少数量补录。</p>}
              {stocktakeOutsideAreaLocations.length > 0 && selectedLocation.occupancy_status !== "empty" && <small className="twin-stocktake-block-reason">当前位置已有货物。要优先移入现有库存，请先在本区域选择空货位。</small>}
              {stocktakeOutsideAreaLocations.length > 0 && !canExecuteWarehouse && <small className="twin-stocktake-block-reason">当前账号只能盘点，不能移货；请联系有移货权限的管理员先处理现有库存。</small>}
              {stocktakeOutsideAreaLocations.some((item) => item.inventory_type === "semi_finished") && <small className="twin-stocktake-resolution">半成品现有库存先按位置定位核对；当前盘点页不伪造半成品移货，需使用库存明细的正式转位流程。</small>}
            </div>}
            <div className="twin-formal-operation-grid"><label><span>本次需处理数量（{stocktakeInventoryType === "finished" ? "箱" : "张"}）</span><input type="number" min="1" step="1" value={stocktakeAddQuantity} onChange={(event) => { setStocktakeAddQuantity(event.target.value); setStocktakeSupplementConfirmed(false); }} /></label><label><span>库存日期</span><input type="date" value={stocktakeStockDate} onChange={(event) => setStocktakeStockDate(event.target.value)} /></label></div>
            {!canEditLocations && <p className="twin-stocktake-block-reason">盘点补录会增加正式库存，只能由管理员确认；你仍可定位现有库存并按权限移货或调减。</p>}
            {canEditLocations && (stocktakeOutsideAreaLocations.length > 0 || pendingProductExists) && !stocktakeSupplementConfirmed && <button type="button" className="twin-stocktake-supplement-toggle" onClick={() => setStocktakeSupplementConfirmed(true)}>现存数量仍不足，补录缺少部分</button>}
            {canEditLocations && ((!stocktakeOutsideAreaLocations.length && !pendingProductExists) || stocktakeSupplementConfirmed) && <button type="button" className="twin-primary-action" title={selectedLocationAddBlockReason || ""} disabled={!selectedStocktakeCustomer || !selectedStocktakeProduct || !stocktakeAddQuantity || !stocktakeStockDate || !selectedLocationCanReceiveStocktakeProduct} onClick={queueStocktakeAddDraft}>加入盘点补录草稿</button>}
            {canEditLocations && <small className="twin-stocktake-resolution">补录会建立独立的盘点库存批次，不挂到已有订单；库存总数和地图位置仍进入同一正式台账。</small>}
            {selectedLocationItems.length > 0 && <div className="twin-formal-divider"><span>调减当前真实批次</span></div>}
            {selectedStocktakeItem?.version ? <div className="twin-correction-form"><b>{selectedStocktakeItem.inventory_code || selectedStocktakeItem.lot_number} · 可用 {formatNumber(selectedStocktakeItem.available_quantity)} {inventoryUnitLabel(selectedStocktakeItem.unit)}</b><label><span>本次调减数量</span><input type="number" min="1" max={selectedStocktakeItem.available_quantity || undefined} step="1" value={stocktakeDecreaseQuantity} onChange={(event) => setStocktakeDecreaseQuantity(event.target.value)} /></label>{selectedStocktakeDecreaseBlockReason && <><small className="twin-stocktake-block-reason">不可盘点调减：{selectedStocktakeDecreaseBlockReason}</small><small className="twin-stocktake-resolution">解决方法：{stocktakeBlockResolution(selectedStocktakeDecreaseBlockReason)}</small></>}<button type="button" title={selectedStocktakeDecreaseBlockReason || ""} disabled={Boolean(selectedStocktakeDecreaseBlockReason) || !stocktakeDecreaseQuantity || Number(stocktakeDecreaseQuantity) > Number(selectedStocktakeItem.available_quantity || 0)} onClick={queueStocktakeDecreaseDraft}>加入盘点调减草稿</button><small>调减至零后地图只隐藏空卡；批次、流水和审计历史保留。</small></div> : selectedLocationItems.length > 0 && <p className="twin-correction-hint">点击上方一个可盘点调减的真实产品卡，再输入调减数量；不能大于当前可用量。</p>}
          </section>}
          {canStocktake && mapMode === "move" && moveAction === "stocktake" && viewMode === "2d" && !locationEditMode && !selectedLocationBaseReceivable && <div className="twin-location-readonly-note"><b>{selectedLocationStocktakeBlockReason || "该位置不能加入盘点草稿。"}</b><span>解决方法：{stocktakeBlockResolution(selectedLocationStocktakeBlockReason)}</span></div>}
          {locationEditMode && advancedAreaMaintenanceOpen && canEditLocations && <div className="twin-location-edit-actions">
            <button type="button" disabled={!selectedLocation.map_position || locationEditBusy} onClick={exchangeLocationDraft}>{swapSourceLocationId === null ? "设为交换起点" : swapSourceLocationId === selectedLocation.location_id ? "已选交换起点" : `与 ${visualLocations.find((item) => item.location_id === swapSourceLocationId)?.location_code || "起点"} 交换位置`}</button>
            <button type="button" className="danger" disabled={selectedLocation.occupancy_status !== "empty" || !selectedLocation.map_position || locationEditBusy} onClick={disableSelectedLocation}>停用空库位</button>
            <small>拖动、交换只改变地图位置，不改变库存数量、实体栈板或库位绑定。</small>
          </div>}
        </section>}
        {(selectedFeature || (locationEditMode && selectedRack)) && <section className="twin-inventory-card">
          {objectActionsButton}
          {selectedAreaFeature ? <>
            <div className="twin-inventory-title"><div><small>{locationEditMode && canEditLocations ? `当前规划区域 · ${selectedAreaCode || selectedAreaFeature.feature_code}` : "当前区域"}</small><b>{employeeAreaName(selectedAreaFeature, { floorCode })}</b></div><span>{selectedAreaActivationLabel}</span></div>
          {layoutMapToolsOpen && layoutMapTool === "adjust" && selectedAreaFeature && selectedAreaBoundary && <div className="twin-zone-geometry-editor">
            <div><b>{employeeAreaName(selectedAreaFeature, { floorCode })}</b><small>{selectedAreaBoundaryLocked ? "区域移动不带动货位；红色冲突可保存后继续整理。" : "方向键1mm；长按加速，松开保存。"}</small></div>
            <div className="twin-zone-geometry-grid">{([['中心 X', 'centerXmm'], ['中心 Y', 'centerYmm'], ['长', 'widthMm'], ['宽', 'heightMm']] as const).map(([label, key]) => <label key={key}><span>{label} mm</span><input type="number" disabled={spatialEditBusy} value={selectedAreaBoundary[key]} onChange={(event) => {
              const next = { ...selectedAreaBoundary, [key]: Number(event.target.value) };
              replaceZoneGeometryDrafts((current) => ({ ...current, [selectedAreaFeature.id]: resizeAndMovePointsMm(selectedAreaBoundaryPoints, next.centerXmm, next.centerYmm, next.widthMm, next.heightMm) }));
            }} onBlur={saveSelectedZoneGeometry} onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} /></label>)}</div>
          </div>}
            {selectedAreaBoundary && <div className="twin-selection-summary area"><span><small>区域长 × 宽</small><b>{formatNumber(selectedAreaBoundary.widthMm)} × {formatNumber(selectedAreaBoundary.heightMm)} mm</b></span><span><small>有效货位</small><b>{selectedAreaLocationCount} 个</b></span></div>}
            {locationEditMode && canEditLocations && <div className="twin-region-planning-actions">
              <button type="button" disabled={spatialEditBusy || Boolean(locationPointEditAreaCode)} onClick={() => { setLayoutMapToolsOpen(true); setLayoutMapTool("adjust"); setLocationEditMessage("区域尺寸可输入；方向键每次1mm，长按加速，松开保存。区域外自动作为通道。"); }}>调整区域尺寸</button>
              <button type="button" disabled={spatialEditBusy || Boolean(locationPointEditAreaCode) || !selectedAreaCreatesInventoryLocations} onClick={() => { setAdvancedAreaMaintenanceOpen(true); setLayoutMapToolsOpen(false); requestAnimationFrame(() => document.getElementById("twin-target-location-count")?.focus()); }}>调整货位数量</button>
            </div>}
            <div className="twin-selection-summary area"><span><small>区域状态</small><b>{selectedAreaActivationLabel}</b></span><span><small>最大容量</small><b>{selectedAreaCapacitySummary}</b></span><span><small>{selectedAreaIsMold ? "当前模具" : "当前库存"}</small><b>{selectedAreaIsMold ? `${moldAreaResponse?.total || 0} 件` : selectedAreaQuantitySummary === "0" ? "0 · 当前无货" : selectedAreaQuantitySummary}</b></span></div>
            {selectedFeature && canStocktake && mapMode === "move" && moveAction === "stocktake" && viewMode === "2d" && !locationEditMode && <div className="twin-stocktake-area-target">
              <div className="twin-formal-operation-title"><b>选择本区域盘点货位</b><span>{stocktakeAreaTargetLocations.length} 个当前可用</span></div>
              <select aria-label="盘点目标货位" defaultValue="" onChange={(event) => {
                const locationId = Number(event.target.value);
                if (locationId > 0) selectStocktakeTargetLocation(locationId);
              }}><option value="">请选择具体货位</option>{stocktakeAreaTargetLocations.map((location) => <option value={location.location_id} key={location.location_id}>{employeeLocationName(location)} · {location.occupancy_status === "empty" ? "空位" : "已有货物"}</option>)}</select>
              {!stocktakeAreaTargetLocations.length && <p className="twin-stocktake-block-reason">本区域没有可盘点货位。解决方法：请管理员先在区域规划中启用区域、发布货位并保存现场位置。</p>}
            </div>}
            {(areaInventorySearch || (selectedAreaIsMold ? (moldAreaResponse?.total || 0) > 5 : selectedInventory.length > 5)) && <label className="twin-area-filter twin-area-filter-prominent">
              <span>{selectedAreaIsMold ? "当前区域模具筛选" : "当前区域库存筛选"}</span>
              <input value={areaInventorySearch} onChange={(event) => { setAreaInventorySearch(event.target.value); setMoldAreaPage(1); }} placeholder={selectedAreaIsMold ? "模具编号、名称、客户或存货编码" : "存货编码、产品、客户、位置"} />
              <small>{selectedAreaIsMold ? `显示 ${moldAreaResponse?.items.length || 0} / ${moldAreaResponse?.total || 0} 件` : `显示 ${filteredSelectedInventory.length} / ${selectedInventory.length} 条`}</small>
            </label>}
            {!selectedAreaIsMold && focusedSearchProduct && focusedSearchProduct.items.some((item) => item.area_code === selectedAreaCode) && <div className="twin-search-focus-note product-focus"><b>已找到该产品</b><span>{focusedSearchProduct.inventory_code} · 本区域位置已高亮</span></div>}
            {locationEditMode && canEditLocations && <div className="twin-area-layout-summary"><div><b>区域货架</b><small>{selectedAreaRacks.length} 个货架 · {selectedAreaLocationCount} 个正式库位</small></div><button type="button" disabled={spatialEditBusy || !selectedAreaFeature || Boolean(locationPointEditAreaCode)} onClick={() => setNewRackFormOpen(!newRackFormOpen)}>＋ 添加货架</button></div>}
            {locationEditMode && canEditLocations && selectedAreaRacks.length > 0 && <div className="twin-area-rack-numbering">
              <button type="button" disabled={spatialEditBusy || Boolean(locationPointEditAreaCode) || selectedAreaRacks.some((r) => r.is_locked || Boolean(r.mold_rack_code))} onClick={numberSelectedAreaRacks}>按从左到右编号</button>
              <small>按标准平面从左到右，同列从上到下；只在主动点击时重新编号，完成并应用后生效。</small>
              <ol>{[...selectedAreaRacks].sort((a, b) => a.x_mm - b.x_mm || b.y_mm - a.y_mm || a.id.localeCompare(b.id)).map((r) => <li key={r.id}><button type="button" onClick={() => setSelected({ kind: "rack", id: r.id })}>{r.name}</button><span>{r.width_mm} × {r.depth_mm} × {r.height_mm} mm · {r.levels}层</span></li>)}</ol>
            </div>}
            {locationEditMode && canEditLocations && newRackFormOpen && <section className="twin-rack-layout-editor" aria-label="添加区域货架">
              <b>添加到 {employeeAreaName(selectedAreaFeature, { floorCode })}</b>
              <div className="twin-rack-primary-fields">
                <label><span>新架长度 mm</span><input type="number" min="1" value={newRackSettings.width} onChange={(e) => setNewRackSettings({ ...newRackSettings, width: e.target.value })} /></label>
                <label><span>新架宽度 mm</span><input type="number" min="1" value={newRackSettings.depth} onChange={(e) => setNewRackSettings({ ...newRackSettings, depth: e.target.value })} /></label>
                <label><span>新架总高度 mm</span><input type="number" min="1" value={newRackSettings.height} onChange={(e) => setNewRackSettings({ ...newRackSettings, height: e.target.value })} /></label>
                <label><span>新架层数</span><select value={newRackSettings.levels} onChange={(e) => setNewRackSettings({ ...newRackSettings, levels: e.target.value })}><option value="3">三层</option><option value="4">四层</option></select></label>
                <label><span>新架每层格数</span><input type="number" min="1" max="50" value={newRackSettings.cells} onChange={(e) => setNewRackSettings({ ...newRackSettings, cells: e.target.value })} /></label>
              </div>
              <div className="twin-layout-editor-actions"><button type="button" disabled={spatialEditBusy} onClick={addRackToSelectedArea}>添加货架</button><button type="button" disabled={spatialEditBusy} onClick={() => setNewRackFormOpen(false)}>取消添加</button></div>
              <small>加入后可拖动位置、逐层调整格数；应用前不生成正式货位。</small>
            </section>}
            {locationEditMode && canEditLocations && selectedAreaHasPublishedBinding && selectedAreaFeature.capacity_review_status === 'pending' && <div className="twin-location-readonly-note"><b>容量待复核</b><span>已启用区域可直接在下方填写最大栈板数并一次确认；需要独立台账时再进入高级维护。</span>{advancedAreaMaintenanceOpen && <a href={selectedAreaCapacityReviewUrl} target="_top">单独复核容量</a>}</div>}
            {locationEditMode && canEditLocations && !selectedAreaFeature.formal_area_id && selectedAreaFeature.formal_binding_status !== 'draft' && <div className="twin-location-readonly-note"><b>尚未绑定正式区域</b><span>直接使用下方简化表单确认用途、形式和容量，系统会自动建立绑定并启用。</span></div>}
            {locationEditMode && canEditLocations && selectedAreaFeature.formal_binding_status === 'draft' && selectedAreaFeature.formal_policy_status !== 'published' && <div className="twin-location-readonly-note"><b>区域设置尚未应用</b><span>请展开货位/货架设置，完成并应用本层地图；其他楼层修改不会一起应用。</span></div>}
            {locationEditMode && canEditLocations && selectedAreaFeature.capacity_review_status === 'confirmed' && selectedAreaFeature.capacity_eligible && <div className="twin-location-readonly-note"><b>现场确认最大 {selectedAreaFeature.confirmed_pallet_capacity || 0} 个栈板</b></div>}
            {locationEditMode && canEditLocations && selectedAreaFeature.capacity_review_status === 'confirmed' && !selectedAreaFeature.capacity_eligible && <div className="twin-location-readonly-note"><b>不计入长期容量</b></div>}
            {locationEditMode && canEditLocations && selectedAreaFeature.capacity_review_status === 'excluded' && <div className="twin-location-readonly-note"><b>不计入长期容量</b></div>}
            {locationEditMode && canEditLocations && selectedAreaFeature && <div className="twin-zone-simple-planner">
              <header><div><b>用途与容量</b></div>{selectedAreaHasPublishedBinding && <span>已启用，可更新</span>}</header>
              {!selectedAreaFeature.formal_area_id && formalAreaOptions.length > 0 && <label className="twin-zone-simple-existing"><span>已有区域（可选）</span><select value={selectedExistingAreaId} onChange={(event) => selectExistingFormalArea(event.target.value)}><option value="">按地图编号新建</option>{formalAreaOptions.map((area) => <option value={area.id} key={area.id}>{area.area_code} · {employeeAreaName(area, { floorCode: area.floor_code })}</option>)}</select></label>}
              <div className="twin-zone-primary-fields">
                <label className="twin-zone-name-field"><span>区域名称</span><input maxLength={100} value={formalAreaNameDraft} onChange={(event) => setFormalAreaNameDraft(event.target.value)} placeholder="例如 4F 新振成品区" /></label>
                <label><span>用途</span><select value={simpleAreaUsage} onChange={(event) => setSimpleAreaUsage(event.target.value as InventoryUsage)}><option value="finished">成品</option><option value="semi_finished">半成品</option><option value="raw_material">原材料</option><option value="mold">模具</option><option value="print_plate">印刷版</option><option value="temporary_turnover">临时周转</option></select></label>
                <label><span>形式</span><select value={simpleAreaLayout} onChange={(event) => setSimpleAreaLayout(event.target.value as Exclude<StorageLayout, "mixed">)}><option value="pallet_ground">栈板区</option><option value="rack">货架区</option></select></label>
                <label><span>{simpleAreaLayout === "pallet_ground" ? "栈板货位数" : "最大栈板数"}</span><input type="number" min="0" max="500" step="1" value={simpleAreaCapacity} onChange={(event) => setSimpleAreaCapacity(event.target.value)} /></label>
              </div>
              <div className="twin-zone-confirm-row"><button type="button" className="confirm" disabled={spatialEditBusy || !formalAreaCodeDraft.trim() || simpleAreaCapacity === ""} onClick={confirmSelectedAreaOnce}>{spatialEditBusy ? "保存中…" : "保存区域设置"}</button><p>{simpleAreaLayout === "pallet_ground" ? "同步空货位数量，不改库存；填 0 会停用全部空货位。" : "保存设置，不改库存；专项货架容量可填 0。"}</p></div>
              {selectedAreaHasPublishedBinding && selectedAreaCreatesInventoryLocations && <div className="twin-location-point-planner">
                <div><b>货位点位</b><small>{selectedAreaLocationCount} 个正式货位 · 只调整当前区域</small></div>
                {locationPointEditAreaCode === selectedAreaCode ? <div className="actions">
                  <button type="button" className="save" disabled={locationEditBusy || layoutMapToolsOpen || !locationPointDraftCount} onClick={saveLocationDrafts}>保存货位调整{locationPointDraftCount ? ` ${locationPointDraftCount}` : ""}</button>
                  <button type="button" disabled={locationEditBusy} onClick={cancelLocationPointEditing}>取消点位调整</button>
                </div> : null}
                {selectedAreaFeature.ground_location_draft?.base_revision === planningPublishedLayout?.source_sha256 && <div className="actions">
                  <button type="button" className="save" disabled={spatialEditBusy || locationEditBusy || activeLocationDraftCount > 0 || Object.keys(zoneGeometryDrafts).length > 0 || Object.keys(rackDrafts).length > 0 || Object.keys(zonePolicyDrafts).length > 0} onClick={applySelectedAreaGeometry}>{spatialEditBusy ? "正在保存并应用…" : "保存并应用当前区域"}</button>
                  <span>只应用本区域边界与货位位置</span>
                </div>}
                <p>{locationPointEditAreaCode === selectedAreaCode ? "奶白色为空货位，绿色为有货货位。红色冲突可先保存；系统会尝试应用，未通过时留在当前区域继续调整。库存数量与栈板绑定不变。" : "区域规划按已应用地图显示全部正式货位：奶白色为空货位，绿色为有货货位。系统不强制紧贴均匀排布；保存通过校验后，查货、移货和盘点立即使用同一位置。"}</p>
                {selectedAreaConflictCount > 0 && <p className="twin-location-column-warning">当前区域有 {selectedAreaConflictCount} 个正式货位越界，或与其他货位、柱子、设备、货架、禁放区冲突。可先保存调整，再逐个拖到安全位置；应用前会核对冲突。</p>}
              </div>}
              {legacyRackBindingPreview?.groups.length ? <div className="twin-location-readonly-note">
                <b>旧货位对应当前货架</b>
                <span>只按同一区域、层号和格号列出可选货架；选择后再次点“完成并应用”，保留原货位 ID、编号和库存。</span>
                {legacyRackBindingPreview.groups.map((group) => <label key={group.binding_key}>
                  <span>{group.area_code} · {group.legacy_rack_code}架 · {group.location_count} 格{group.occupied_location_count ? `（${group.occupied_location_count} 格有货）` : ""}</span>
                  <select value={legacyRackBindingSelections[group.binding_key] || ""} onChange={(event) => setLegacyRackBindingSelections((current) => ({ ...current, [group.binding_key]: event.target.value }))}>
                    <option value="">选择现场对应货架</option>
                    {group.candidates.map((candidate) => <option key={candidate.map_rack_id} value={candidate.map_rack_id}>{candidate.rack_name}{candidate.map_rack_id === group.suggested_map_rack_id ? "（建议）" : ""}</option>)}
                  </select>
                  {group.blocking_reason && <small>{group.blocking_reason}</small>}
                </label>)}
              </div> : null}
              <div className="twin-region-planning-actions">
                <button type="button" className={!advancedAreaMaintenanceOpen ? "active" : ""} disabled={Boolean(locationPointEditAreaCode)} onClick={() => { setAdvancedAreaMaintenanceOpen(false); setAreaPolicyEditMode(true); setLocationEditMessage("请核对当前区域名称、用途、形式和容量。"); }}>编辑</button>
                <button type="button" className={advancedAreaMaintenanceOpen ? "active" : ""} disabled={Boolean(locationPointEditAreaCode)} title={locationPointEditAreaCode ? "请先保存并固定或取消点位调整" : ""} onClick={() => { setAdvancedAreaMaintenanceOpen(true); setAreaPolicyEditMode(true); setLocationEditMessage("请选择货架或货位，按现场尺寸整理后保存并应用。"); }}>货位/货架</button>
              </div>
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
                  setLocationEditMessage(`已选择 ${rack.mold_rack_code || rack.rack_code}；可直接修改层数与每层格数，点击保存后直接应用。`);
                }}><b>{rack.mold_rack_code || rack.rack_code}</b><span>{moldRackEmployeeName(rack)}</span><small>{rack.levels} 层 · {counts.map((count, index) => blockedLevels.includes(index + 1) ? `第${index + 1}层 设备占用` : `第${index + 1}层 ${count} 格`).join(" / ")}</small></button>;
              })}</div>
              {selectedRackEditDraft?.mold_rack_code && selectedAreaMoldRacks.some((rack) => rack.id === selectedRackEditDraft.id) && <div className="twin-mold-rack-structure-editor">
                <div><b>{selectedRackEditDraft.mold_rack_code} · {selectedRackEditDraft.name}</b><small>点击保存会校验并直接应用本货架；不会改变模具台账数量。</small></div>
                <div className="twin-mold-rack-publish-state">
                  <div><span>当前正式层格</span><b>{selectedPublishedMoldRack ? `${selectedPublishedMoldRack.levels} 层 · ${selectedPublishedMoldRack.uses_legacy_bays ? `旧统一 ${selectedPublishedMoldRack.level_cell_counts[0] || 1} 格` : selectedPublishedMoldRack.level_cell_counts.map((count, index) => `第${index + 1}层 ${count} 格`).join(" / ")}` : moldRackLoading ? "正在读取…" : "读取失败"}</b></div>
                  <div><span>准备应用的层格</span><b>{selectedRackEditDraft.levels} 层 · {selectedRackEditDraft.level_cell_counts.map((count, index) => moldRackBlockedLevels(selectedRackEditDraft).includes(index + 1) ? `第${index + 1}层 设备占用` : `第${index + 1}层 ${count} 格`).join(" / ")}</b></div>
                  <em className={layoutDraftControl?.status || "none"}>{layoutDraftControl?.has_draft ? "有尚未应用的本层修改" : "本货架输入尚未保存"}</em>
                </div>
                {selectedMoldRackHighestUsedLevel > selectedRackEditDraft.levels && <p className="twin-mold-rack-structure-blocker">正式台账仍有模具放在第 {selectedMoldRackHighestUsedLevel} 层；保存时会自动校验，应用后这些模具将归入本货架首个可用格。</p>}
                <div className="twin-mold-rack-fields">
                  <label><span title="货架总层数（含设备占用层）">总层数</span><input type="number" min="1" max="20" value={selectedRackEditDraft.levels} onChange={(event) => changeRackLevels(selectedRackEditDraft, Number(event.target.value))} /><small>已用到第 {selectedMoldRackHighestUsedLevel || 0} 层</small></label>
                  {selectedRackEditDraft.level_cell_counts.map((count, index) => {
                    const level = index + 1;
                    const machineBlocked = moldRackBlockedLevels(selectedRackEditDraft).includes(level);
                    const usedCount = selectedMoldRackUsage.get(level) || 0;
                    return <label className={machineBlocked ? "blocked" : ""} key={`${selectedRackEditDraft.id}-simple-grid-${level}`}><span>第 {level} 层格数{machineBlocked ? "（占用）" : usedCount ? `（已有 ${usedCount}）` : ""}</span><input type="number" min="0" max="50" disabled={machineBlocked} value={machineBlocked ? 0 : count} onChange={(event) => changeRackLevelCellCount(selectedRackEditDraft, index, Number(event.target.value))} /></label>;
                  })}
                </div>
                <div className="twin-mold-rack-planner-actions">
                  <button type="button" className="primary" disabled={spatialEditBusy} title="保存并直接应用当前货架" onClick={saveSelectedRack}>保存并应用货架</button>
                  <button type="button" disabled={spatialEditBusy} onClick={() => setRackDrafts((current) => { const next = { ...current }; delete next[selectedRackEditDraft.id]; return next; })}>取消本次输入</button>
                </div>
                <p>减少已被正式模具使用的层或格时，应用会保留原有校验；失效位置统一归入本货架首个可用格并写移动流水，之后可逐件手动调整。增加格位不会自动平均分配现有模具。</p>
              </div>}
            </section>}
            {locationEditMode && advancedAreaMaintenanceOpen && areaPolicyEditMode && canEditLocations && selectedAreaFeature && selectedZonePolicy && <div className="twin-zone-policy-editor">
              <div><b>正式区域绑定</b><small>区域编号保存后不可与其他地图区域重复；发布前仍不会进入员工入库候选。</small></div>
              {!selectedAreaFeature.formal_area_id && <label><span>选用现有未绑定区域</span><select value={selectedExistingAreaId} onChange={(event) => selectExistingFormalArea(event.target.value)}><option value="">不选，按下方编号建立新区域</option>{formalAreaOptions.map((area) => <option value={area.id} key={area.id}>{area.floor_code} · {area.area_code} {employeeAreaName(area, { floorCode: area.floor_code })} · {area.capacity_review_status === "confirmed" ? `已确认 ${area.confirmed_pallet_capacity || 0} 栈板` : area.capacity_review_status === "excluded" ? "不计长期容量" : "容量待复核"}</option>)}</select>{formalAreaOptionsError && <small>现有区域读取失败：{formalAreaOptionsError}</small>} {!formalAreaOptionsError && formalAreaOptions.length === 0 && <small>当前楼层没有可选的未绑定区域；可使用下方新编号。</small>}</label>}
              <div className="twin-zone-policy-fields">
                <label><span>区域编号</span><input maxLength={30} disabled={Boolean(selectedExistingAreaId)} value={formalAreaCodeDraft} onChange={(event) => setFormalAreaCodeDraft(event.target.value.toUpperCase())} placeholder="例如 FIN-001" /></label>
                <label><span>区域名称</span><input maxLength={100} value={formalAreaNameDraft} onChange={(event) => setFormalAreaNameDraft(event.target.value)} placeholder="例如 右区C2 新振（主通道西侧）" /></label>
                <label><span>存储形式</span><select value={selectedZonePolicy.storage_layout} onChange={(event) => setZonePolicyDrafts((current) => ({ ...current, [selectedAreaFeature.id]: { ...selectedZonePolicy, storage_layout: event.target.value as StorageLayout } }))}><option value="rack">货架区</option><option value="pallet_ground">栈板地堆区</option><option value="mixed">货架＋栈板混合区</option></select></label>
              </div>
              <div><b>区域允许存放类型</b><small>可多选；只保存区域策略，不自动转换现有库存</small></div>
              <div className="twin-zone-policy-options">{([[
                "finished", "成品"
              ], ["semi_finished", "半成品"], ["raw_material", "原材料"], ["mold", "模具"], ["print_plate", "印刷版"], ["temporary_turnover", "临时周转"]] as Array<[InventoryUsage, string]>).map(([value, label]) => <label key={value}><input type="checkbox" checked={selectedZonePolicy.allowed_inventory_types.includes(value)} onChange={() => toggleAreaUsage(value)} /><span>{label}</span></label>)}</div>
              <button type="button" className="primary" disabled={spatialEditBusy || !formalAreaCodeDraft.trim() || !selectedZonePolicy.allowed_inventory_types.length} onClick={saveSelectedZonePolicy}>绑定正式区域并保存策略</button>
            </div>}
            {locationEditMode && advancedAreaMaintenanceOpen && canEditLocations && selectedAreaCode && selectedAreaCreatesInventoryLocations && selectedAreaHasFormalLedger && <div className="twin-location-create">
              <div><b>区域库位数量</b><small>当前 {selectedAreaLocationCount} 个{selectedAreaPendingLocationCount ? ` · ${selectedAreaPendingLocationCount} 个待布局` : ""}</small></div>
              <label><span>目标库位数</span><input id="twin-target-location-count" type="number" min="0" max="500" step="1" value={targetAreaLocationCount} onChange={(event) => setTargetAreaLocationCount(event.target.value)} /></label>
              <button type="button" disabled={locationEditBusy || targetAreaLocationCount === "" || Number(targetAreaLocationCount) === selectedAreaLocationCount} onClick={applyAreaLocationCount}>确认调整</button>
              <p>系统按区域自动生成内部唯一编码和员工可读名称；减少时只逻辑停用空库位，不删除历史身份。</p>
            </div>}
            {locationEditMode && advancedAreaMaintenanceOpen && canEditLocations && selectedAreaCode && !selectedAreaHasFormalLedger && <div className="twin-location-create"><p>区域策略仍是管理员草稿；请先校验并发布建立正式区域，再规划正式库位。</p></div>}
            {locationEditMode && advancedAreaMaintenanceOpen && canEditLocations && selectedAreaCode && selectedAreaHasFormalLedger && !selectedAreaCreatesInventoryLocations && <div className="twin-location-create"><p>该区域使用原料、模具、印版或临时周转台账，不生成成品/半成品库存库位；发布后按对应台账定位。</p></div>}
            {locationEditMode && advancedAreaMaintenanceOpen && canEditLocations && !selectedAreaCode && <div className="twin-location-create"><p>请先确认启用区域，之后才能生成可投入使用的库位。</p></div>}
            {selectedAreaIsMold ? <div className="twin-area-mold-list">
              {!locationEditMode && selectedAreaMoldRacks.length > 0 && <div className="twin-mold-rack-lookup-shortcuts"><b>打开模具货架正视图</b><div>{selectedAreaMoldRacks.map((rack) => <button type="button" key={rack.id} onClick={() => { setRackFocusId(rack.id); setSelected({ kind: "rack", id: rack.id }); }}>{rack.mold_rack_code || rack.rack_code}<small>{moldRackEmployeeName(rack)}</small></button>)}</div></div>}
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
                {!selectedInventory.length && !locationEditMode && <div className="twin-area-empty"><b>{selectedAreaHasPublishedBinding ? "区域已启用，当前没有货物" : selectedAreaActivationLabel === "待启用" ? "区域绑定仍待启用" : "区域尚未启用"}</b><span>{selectedAreaHasPublishedBinding ? `${selectedAreaCapacitySummary}；库存为 0 不代表区域未启用。` : "进入区域规划确认用途、形式和容量后即可启用；系统不会生成模拟货物。"}</span></div>}
                {selectedInventory.length > 0 && !filteredSelectedInventory.length && <div className="twin-area-empty"><b>本区域没有匹配结果</b><span>请更换存货编码、产品、客户或位置关键词。</span></div>}
                {visibleSelectedInventory.map((item) => <article className={`twin-area-lot ${focusedSearchProductKey && searchProductKey(item) === focusedSearchProductKey ? "search-hit product-search-hit" : focusedSearchItem?.lot_id === item.lot_id ? "search-hit" : ""}`} key={item.lot_id}>
                  <div><b>{item.inventory_code || item.lot_number || `批次 ${item.lot_id}`}</b><strong>{formatNumber(inventoryPhysicalQuantity(item))} {inventoryUnitLabel(item.unit)}</strong></div>
                  <strong>{item.product_name || "待补充库存名称"}</strong>
                  <span>{item.customer_name || "客户待确认"} · {item.location_name || "位置待补充"}</span>
                  {areaInventoryDetailsOpen && <div className="twin-area-lot-details"><span>{employeeAreaName(selectedAreaFeature, { floorCode })} · {item.location_name || "位置名称待完善"} · {item.pallet_code ? "实物栈板" : "地堆或散存"}</span><span>{item.lot_number || "批次待补充"} · {inventoryAgeLabel(item.age_days)}</span>{(item.reserved_quantity || 0) > 0 && <small>已预占 {formatNumber(item.reserved_quantity)} {inventoryUnitLabel(item.unit)}</small>}</div>}
                </article>)}
              </div>
              {filteredSelectedInventory.length > 0 && <button type="button" className="twin-detail-toggle" aria-expanded={areaInventoryDetailsOpen} onClick={() => setAreaInventoryDetailsOpen((current) => !current)}>{areaInventoryDetailsOpen ? "收起完整台账" : filteredSelectedInventory.length > 5 && !areaInventorySearch.trim() ? `查看全部 ${filteredSelectedInventory.length} 条库存` : "批次与预占详情"}</button>}
            </>}
          </> : <div className="twin-unmapped">该区域尚未建立空间策略。</div>}
        </section>}
        {selectedPlacement && <section className="twin-object-card twin-equipment-card"><span className="twin-object-kind">生产设备</span><h3>{selectedPlacement.name}</h3><dl><div><dt>长 × 宽</dt><dd>{formatNumber(selectedPlacement.width_mm)} × {formatNumber(selectedPlacement.depth_mm)} mm</dd></div><div><dt>高度</dt><dd>{formatNumber(selectedPlacement.height_mm)} mm</dd></div><div><dt>坐标</dt><dd>X {formatNumber(selectedPlacement.x_mm)} / Y {formatNumber(selectedPlacement.y_mm)}</dd></div></dl></section>}
      </aside>
    </section>
    {mapMode === "move" && moveAction !== "ground" && (canExecuteWarehouse || canStocktake) && <section className={`twin-move-draft-bar ${moveAction === "merge" ? "merge-mode" : moveAction === "stocktake" ? "stocktake-mode" : ""}`} aria-label={moveAction === "stocktake" ? "盘点调整页面草稿汇总" : moveAction === "merge" ? "多栈合并页面草稿汇总" : "移货页面草稿汇总"}>
      {moveAction === "stocktake" ? <>
        <div className="twin-move-draft-heading"><div><small>盘点草稿 · 尚未写入</small><b>{stocktakeDrafts.length ? `${stocktakeDrafts.length} 条待确认调整` : "尚无盘点草稿"}</b></div><span>{stocktakeDrafts.length ? "一次确认整批提交；失败后草稿和重试键都会保留。" : "在右侧选择正式货位，加入已有产品或调减真实批次。"}</span></div>
        <div className="twin-move-draft-list">{stocktakeDrafts.map((item) => <article key={item.client_item_id}>
          <div><b>{item.operation === "add" ? "盘点新增" : "盘点调减"} · {item.inventory_code}</b><span>{item.customer_name} · {item.product_name}</span></div>
          <strong>{item.floor_code} / {item.area_code || "未分区"} / {item.location_name}</strong>
          <small>{formatNumber(item.quantity)} {inventoryUnitLabel(item.unit)}{item.operation === "decrease" && item.quantity === item.quantity_before ? " · 调减至零后仅隐藏空卡" : ""}</small>
          <button type="button" disabled={stocktakeBatchBusy} onClick={() => removeStocktakeDraftItem(item.client_item_id)}>撤销</button>
        </article>)}</div>
        <div className="twin-move-draft-actions"><button type="button" disabled={!stocktakeDrafts.length || stocktakeBatchBusy} onClick={cancelStocktakeDrafts}>取消全部草稿</button><button type="button" className="confirm" disabled={!stocktakeDrafts.length || stocktakeBatchBusy || stocktakeRefreshRequired} onClick={confirmStocktakeDrafts}>{stocktakeBatchBusy ? "正在一次提交…" : `一次确认 ${stocktakeDrafts.length || ""} 条`}</button>{stocktakeRefreshRequired && <button type="button" disabled={stocktakeBatchBusy} onClick={() => void refreshDashboard().then(() => { setStocktakeRefreshRequired(false); setWarehouseOperationMessage("盘点已写入，地图已刷新核对。"); }).catch((error) => setWarehouseOperationMessage(`盘点已写入，刷新仍未完成：${error.message}`))}>刷新核对盘点结果</button>}</div>
        {stocktakeLastResult.length > 0 && <div className="twin-stocktake-label-results"><b>本次新增已入账，可贴标签</b>{stocktakeLastResult.map((item) => <span key={`stocktake-label-${item.lot_id}`}><button type="button" onClick={() => window.open(`/static/location-label.html?location_id=${encodeURIComponent(item.location_id)}`, "_blank", "noopener")}>打印位置标签</button>{item.inventory_type === "finished" && <button type="button" onClick={() => window.open(`/static/finished-goods-label.html?lot_id=${encodeURIComponent(item.lot_id)}&version=${encodeURIComponent(item.version_after)}`, "_blank", "noopener")}>打印产品标签</button>}</span>)}</div>}
      </> : moveAction === "merge" ? <>
        <div className="twin-move-draft-heading"><b>{mergeSources.length ? `已选 ${mergeSources.length} 块` : "请选择货位"}</b></div>
        <div className="twin-move-draft-list twin-merge-draft-line">{mergeSources.map((item) => <article className={mergeTarget?.pallet_id === item.pallet_id ? "merge-target" : ""} key={item.client_item_id || item.pallet_id}>
          <b>{item.location_code || item.location_name || "位置待确认"}{mergeTarget?.pallet_id === item.pallet_id && <i>主货位</i>}</b>
          <strong>{formatNumber(item.total_quantity)} {inventoryUnitLabel(item.unit)}</strong>
          <button type="button" disabled={mergeBatchBusy} onClick={() => {
            const result = togglePalletMergeSource(mergeSources, item);
            setMergeSources(result.items);
            if (mergeTarget?.pallet_id === item.pallet_id) setMergeTarget(null);
            setMergeBatchIdempotencyKey(operationKey("warehouse-pallet-merge-batch"));
          }}>撤销</button>
        </article>)}</div>
        <div className="twin-move-draft-actions"><button type="button" disabled={!mergeSources.length || mergeBatchBusy} onClick={clearMergeDraft}>清空草稿</button><button type="button" className="confirm" disabled={mergeSources.length < 2 || !mergeTarget || mergeBatchBusy} onClick={confirmPalletMergeBatch}>{mergeBatchBusy ? "正在一次提交…" : mergeTarget ? `一次确认 ${mergeSources.length - 1} 源 → 1 目标` : "请明确目标栈板"}</button></div>
      </> : <>
        <div className="twin-move-draft-heading"><div><small>移货草稿 · 尚未写入</small><b>{moveDrafts.length ? `${moveDrafts.length} 条待确认移货` : "尚无移货草稿"}</b></div><span>{moveDrafts.length ? "可继续跨楼层选择；失败后草稿与重试键都会保留。" : "拖动整栈货物，或在右侧按楼层、区域、具体货位加入。"}</span></div>
        <div className="twin-move-draft-list">
          {moveDrafts.map((item) => <article key={item.client_item_id}>
            <div><b>{item.inventory_code}</b><span>{item.customer_name} · {item.product_name}</span></div>
            <strong>{item.source_floor_code} / {item.source_location_name || "位置名称待完善"}<i>→</i>{item.target_floor_code} / {item.target_location_name || "位置名称待完善"}</strong>
            <small>{item.operation === "pallet_move" ? "整栈板" : `${formatNumber(item.quantity)} ${inventoryUnitLabel(item.unit)}`}</small>
            <button type="button" disabled={moveBatchBusy} onClick={() => removeMoveDraft(item.client_item_id)}>撤销</button>
          </article>)}
        </div>
        <div className="twin-move-draft-actions"><button type="button" disabled={!moveDrafts.length || moveBatchBusy} onClick={clearMoveDrafts}>清空草稿</button><button type="button" className="confirm" disabled={!moveDrafts.length || moveBatchBusy} onClick={confirmMoveDrafts}>{moveBatchBusy ? "正在整批提交…" : `提交 ${moveDrafts.length || ""} 条移货`}</button></div>
      </>}
    </section>}
  </main>;
}
