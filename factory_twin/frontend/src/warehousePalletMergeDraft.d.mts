export interface PalletMergeInventoryItem {
  lot_id?: number | null;
  version?: number | null;
  product_id?: number | null;
  inventory_code?: string | null;
  product_name?: string | null;
  specification?: string | null;
  customer_id?: number | null;
  customer_name?: string | null;
  customer_short_name?: string | null;
  inventory_type?: string | null;
  unit?: string | null;
  status?: string | null;
  quantity?: number | null;
  available_quantity?: number | null;
  reserved_quantity?: number | null;
  damaged_quantity?: number | null;
}

export interface PalletMergePallet {
  pallet_id: number;
  pallet_code: string;
  version: number;
  items: PalletMergeInventoryItem[];
}

export interface PalletMergeLocation {
  location_id: number;
  location_code: string;
  location_name: string;
  floor_code: string;
  area_code: string | null;
  warehouse_type: string;
  storage_type: string;
  is_active: boolean;
  position_status: string;
  map_position: object | null;
}

export interface PalletMergeCandidate {
  client_item_id?: string;
  pallet_id: number;
  pallet_code: string;
  expected_version: number;
  location_id: number;
  location_code: string;
  location_name: string;
  floor_code: string;
  area_code: string | null;
  customer_id: number;
  customer_name: string;
  customer_short_name: string | null;
  inventory_code: string;
  product_name: string;
  specification: string | null;
  inventory_type: "finished" | "semi_finished";
  unit: string;
  inventory_status: "active" | "frozen";
  quality_status: "usable" | "damaged";
  total_quantity: number;
  lot_count: number;
  product_count: number;
  target_eligible: boolean;
}

export function palletMergeSuggestionProductKey(items: PalletMergeInventoryItem[]): string | null;
export function palletMergeSuggestionMatchesFilter(
  suggestion: { label?: string | null; candidates?: PalletMergeCandidate[] | null },
  customerId?: string | number | null,
  keyword?: string | null
): boolean;
export function normalizePalletMergeCandidate(location: PalletMergeLocation, pallet: PalletMergePallet): { candidate: PalletMergeCandidate | null; error: string | null };
export function palletMergeCompatibility(left: PalletMergeCandidate | null, right: PalletMergeCandidate | null): { compatible: boolean; error: string | null };
export function togglePalletMergeSource(sources: PalletMergeCandidate[], candidate: PalletMergeCandidate | null): { items: PalletMergeCandidate[]; error: string | null };
export function palletMergeTargetChoices(selected: PalletMergeCandidate[]): PalletMergeCandidate[];
export function buildPalletMergeBatchPayload(idempotencyKey: string, sources: PalletMergeCandidate[], target: PalletMergeCandidate): {
  idempotency_key: string;
  confirmed: true;
  target_pallet_id: number;
  expected_target_version: number;
  sources: Array<{ client_item_id?: string; pallet_id: number; expected_version: number }>;
};
