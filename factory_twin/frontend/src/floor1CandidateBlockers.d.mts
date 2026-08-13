export interface Floor1CandidateBlockingItem {
  code: "partial_candidate_state" | "legacy_area_policy" | "legacy_area_inventory";
  message: string;
  action_kind: "open_area_planning" | "open_inventory_move";
  action_label: string;
  area_id?: number | null;
  area_code?: string | null;
  map_feature_id?: string | null;
  location_id?: number | null;
  live_lot_count: number;
  current_pallet_count: number;
}

export function floor1CandidateBlockerHref(item: Floor1CandidateBlockingItem): string;
export function floor1CandidateBlockerDetail(item: Floor1CandidateBlockingItem): string;
