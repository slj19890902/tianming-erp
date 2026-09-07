export interface InventoryProjectionItem {
  lot_id: number;
  product_id?: number | null;
  lot_number?: string;
  inventory_code?: string;
  product_name?: string;
  customer_name?: string;
  quantity?: number;
  available_quantity?: number;
  reserved_quantity?: number;
  damaged_quantity?: number;
  unit?: string;
  age_days?: number | null;
}

export interface InventoryProjectionPallet {
  pallet_id: number;
  pallet_code: string;
  version?: number;
  item_count?: number;
  items: InventoryProjectionItem[];
}

export interface InventoryProjectionLocation {
  location_id?: number;
  location_code: string;
  location_name: string;
  employee_location_name?: string | null;
  current_address_name?: string | null;
  floor_code: string;
  area_code: string | null;
  map_feature_id?: string | null;
  published_map_revision?: string | null;
  source_version?: string | null;
  occupancy_status?: "occupied" | "empty";
  position_status?: string;
  map_position?: {
    left_pct: number;
    top_pct: number;
    width_pct: number;
    height_pct: number;
    version?: number;
    z_index?: number;
  } | null;
  pallet: InventoryProjectionPallet | null;
  pallets?: InventoryProjectionPallet[];
  loose_items: InventoryProjectionItem[];
}

export interface AreaInventoryEntry extends InventoryProjectionItem {
  location_code: string;
  location_name: string;
  pallet_code: string | null;
}

export function expandAreaInventory(
  locations: InventoryProjectionLocation[],
  floorCode: string,
  areaCode: string | null
): AreaInventoryEntry[];

export function filterAreaInventory(
  items: AreaInventoryEntry[],
  keyword: string
): AreaInventoryEntry[];

export function inventoryAgeLabel(ageDays?: number | null): string;
export function inventoryAgeTone(ageDays?: number | null): "unknown" | "critical" | "warning" | "normal";
export function inventoryUnitLabel(unit?: string | null): string;
export function inventoryPhysicalQuantity(item?: {
  quantity?: number | null;
  available_quantity?: number | null;
  reserved_quantity?: number | null;
  damaged_quantity?: number | null;
} | null): number;

export function inventoryHasPhysicalQuantity(item?: {
  quantity?: number | null;
  available_quantity?: number | null;
  reserved_quantity?: number | null;
  damaged_quantity?: number | null;
} | null): boolean;

export function searchHighlightAreaCodes(
  items: Array<{ floor_code?: string; area_code?: string | null; position_status?: string }>,
  floorCode: string
): string[];

export function warehouseSearchProductKey(item: {
  product_id?: number | null;
  customer_id?: number | null;
  customer_name?: string | null;
  inventory_code?: string | null;
  product_name?: string | null;
  specification?: string | null;
  inventory_type?: string | null;
  unit?: string | null;
}): string;

export function warehouseSearchFloorSummaries(items: Array<{
  floor_code?: string | null;
  area_code?: string | null;
  location_id?: number | null;
  location_name?: string | null;
  quantity?: number | null;
  available_quantity?: number | null;
  reserved_quantity?: number | null;
  damaged_quantity?: number | null;
}>): Array<{ floor_code: string; quantity: number; location_count: number }>;

export function warehouseSearchLocationSummaries(items: Array<{
  floor_code?: string | null;
  area_code?: string | null;
  location_id?: number | null;
  location_name?: string | null;
  position_status?: string | null;
  quantity?: number | null;
  available_quantity?: number | null;
  reserved_quantity?: number | null;
  damaged_quantity?: number | null;
}>): Array<{
  key: string;
  floor_code: string;
  area_code: string | null;
  location_id: number | null;
  location_name: string;
  position_status: string;
  quantity: number;
}>;

export function inventoryLocationPallets(
  location?: InventoryProjectionLocation | null
): InventoryProjectionPallet[];

export function inventoryLocationItems(
  location?: InventoryProjectionLocation | null
): InventoryProjectionItem[];

export function normalizeInventoryLocationProjection(
  location?: InventoryProjectionLocation | null
): InventoryProjectionLocation | null | undefined;

export function singleLocationPallet(
  location?: InventoryProjectionLocation | null
): InventoryProjectionPallet | null;

export function employeeLocationName(
  location?: {
    employee_location_name?: string | null;
    current_address_name?: string | null;
    location_name?: string | null;
  } | null
): string;

export function employeeAreaName(
  area?: {
    employee_area_name?: string | null;
    formal_area_name?: string | null;
    area_name?: string | null;
    area_code?: string | null;
    erp_area_code?: string | null;
    floor_code?: string | null;
    floor_number?: number | null;
    warehouse_floor?: number | null;
    name?: string | null;
  } | null,
  context?: { floorCode?: string | null; floorNumber?: number | null }
): string;

export interface StandardPalletContract {
  contract_version: "standard-pallet-v1";
  width_mm: number;
  depth_mm: number;
  height_mm: number;
}

export function normalizeStandardPalletContract(
  value?: Partial<StandardPalletContract> | null
): StandardPalletContract | null;

export function standardPalletContractsMatch(
  left?: Partial<StandardPalletContract> | null,
  right?: Partial<StandardPalletContract> | null
): boolean;

export function standardPalletDisplayIssue(input: {
  loading: boolean;
  dashboardReady?: boolean;
  requestedFloorCode: string;
  layoutFloorCode?: string | null;
  layoutContract?: Partial<StandardPalletContract> | null;
  dashboardContract?: Partial<StandardPalletContract> | null;
}): string;

export function buildMeasuredDispatchPallets(
  features: Array<{ id: string; feature_kind: string; feature_code: string; subtype?: string | null; points: number[][] }>,
  dispatchLocation?: InventoryProjectionLocation | null,
  floorCode?: string,
  standardPallet?: StandardPalletContract | null,
  layoutId?: string
): import("./types").Pallet[];

export function buildMappedLocationPallets(
  features: Array<{ id: string; feature_kind: string; feature_code: string; erp_area_code?: string | null; points: number[][] }>,
  locations: InventoryProjectionLocation[],
  floorCode: string,
  standardPallet?: StandardPalletContract | null,
  layoutId?: string,
  renderEmptyPlanningSlots?: boolean
): import("./types").Pallet[];

export function mergePublishedFeatureGeometry<T extends {
  id?: string | null;
  points?: number[][];
}>(
  activeFeatures?: T[],
  publishedFeatures?: T[]
): T[];

export function findPalletColumnConflicts(
  pallets: import("./types").Pallet[],
  structures?: import("./types").Structure[],
  features?: import("./types").LayoutFeature[],
  clearanceMm?: number
): Array<{ pallet_id: string; column_id: string }>;

export function findPalletPlanningConflicts(
  pallets: import("./types").Pallet[],
  structures?: import("./types").Structure[],
  features?: import("./types").LayoutFeature[],
  clearanceMm?: number,
  placements?: import("./types").Placement[],
  racks?: import("./types").Rack[]
): Array<{ pallet_id: string; column_id: string }>;

export function planningConflictWarning(
  conflicts?: Array<{ pallet_id: string; column_id: string }>,
  palletId?: string
): string;

export function uniquePalletConflictCount(
  conflicts?: Array<{ pallet_id: string; column_id: string }>
): number;

export function zoneLayoutFrame(points: number[][]): {
  anchor: number[];
  right: number[];
  down: number[];
  width: number;
  height: number;
  rotation_deg: number;
} | null;

export function locationLayoutGeometry(
  zone: { points: number[][] },
  location: InventoryProjectionLocation,
  xMm: number,
  yMm: number,
  options?: { clampToZone?: boolean }
): {
  location_id: number;
  expected_version: number;
  left_pct: number;
  top_pct: number;
  width_pct: number;
  height_pct: number;
  z_index: number;
} | null;
