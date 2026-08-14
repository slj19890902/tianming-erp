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
  floor_code: string;
  area_code: string | null;
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
}>): Array<{ floor_code: string; quantity: number; location_count: number }>;

export function warehouseSearchLocationSummaries(items: Array<{
  floor_code?: string | null;
  area_code?: string | null;
  location_id?: number | null;
  location_name?: string | null;
  position_status?: string | null;
  quantity?: number | null;
  available_quantity?: number | null;
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

export function singleLocationPallet(
  location?: InventoryProjectionLocation | null
): InventoryProjectionPallet | null;

export function buildMeasuredDispatchPallets(
  features: Array<{ id: string; feature_kind: string; feature_code: string; subtype?: string | null; points: number[][] }>,
  dispatchLocation?: InventoryProjectionLocation | null,
  floorCode?: string,
  layoutId?: string
): import("./types").Pallet[];

export function buildMappedLocationPallets(
  features: Array<{ id: string; feature_kind: string; feature_code: string; erp_area_code?: string | null; points: number[][] }>,
  locations: InventoryProjectionLocation[],
  floorCode: string,
  layoutId?: string
): import("./types").Pallet[];

export function findPalletColumnConflicts(
  pallets: import("./types").Pallet[],
  structures?: import("./types").Structure[],
  features?: import("./types").LayoutFeature[],
  clearanceMm?: number
): Array<{ pallet_id: string; column_id: string }>;

export function locationLayoutGeometry(
  zone: { points: number[][] },
  location: InventoryProjectionLocation,
  xMm: number,
  yMm: number
): {
  location_id: number;
  expected_version: number;
  left_pct: number;
  top_pct: number;
  width_pct: number;
  height_pct: number;
  z_index: number;
} | null;
