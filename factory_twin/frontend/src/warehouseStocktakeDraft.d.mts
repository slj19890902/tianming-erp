export type StocktakeInventoryType = "finished" | "semi_finished";
export type StocktakeUnit = "boxes" | "sheets";

export interface StocktakeLocationProjection {
  location_code?: string | null;
  floor_code?: string | null;
  area_code?: string | null;
  warehouse_type?: string | null;
  storage_type?: string | null;
  is_active?: boolean;
  position_status?: string | null;
  source_version?: string | null;
}

export interface StocktakeDecreaseProjection {
  stocktake_decrease_eligible?: boolean;
  stocktake_decrease_block_reason?: string | null;
}

interface StocktakeDraftBase {
  client_item_id: string;
  location_id: number;
  location_code: string;
  location_name: string;
  floor_code: string;
  area_code: string | null;
  quantity: number;
  customer_name: string;
  inventory_code: string;
  product_name: string;
}

export interface StocktakeAddDraft extends StocktakeDraftBase {
  operation: "add";
  customer_id: number;
  product_id: number;
  inventory_type: StocktakeInventoryType;
  unit: StocktakeUnit;
  stock_date: string;
}

export interface StocktakeDecreaseDraft extends StocktakeDraftBase {
  operation: "decrease";
  lot_id: number;
  expected_version: number;
  unit: string;
  available_quantity: number;
  quantity_before: number;
}

export type WarehouseStocktakeDraft = StocktakeAddDraft | StocktakeDecreaseDraft;

export function stocktakeLocationBlockReason(location?: StocktakeLocationProjection | null): string | null;
export function stocktakeAddBlockReason(location: StocktakeLocationProjection | null | undefined, inventoryType: StocktakeInventoryType): string | null;
export function stocktakeDecreaseBlockReason(item?: StocktakeDecreaseProjection | null): string | null;
export function validateStocktakeDraft(draft: WarehouseStocktakeDraft): string | null;
export function upsertStocktakeDraft<T extends WarehouseStocktakeDraft>(drafts: T[], draft: T): { items: T[]; error: string | null };
export function removeStocktakeDraft<T extends WarehouseStocktakeDraft>(drafts: T[], clientItemId: string): T[];
export function clearStocktakeDrafts(): WarehouseStocktakeDraft[];
export function buildStocktakeBatchPayload(idempotencyKey: string, drafts: WarehouseStocktakeDraft[]): {
  idempotency_key: string;
  confirmed: true;
  items: Array<Record<string, string | number>>;
};
