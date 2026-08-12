import type { LayoutFeature } from "./types";

export interface MoveCandidate {
  id: number;
  location_code: string;
  location_name: string;
  warehouse_floor?: number | null;
  floor_code: string | null;
  floor_name?: string | null;
  area_code: string | null;
  area_name?: string | null;
  storage_type?: string;
  occupied?: boolean;
  is_empty?: boolean;
}

export interface MoveDashboardLocation {
  location_id: number;
  location_code: string;
  location_name: string;
  floor_code: string;
  area_code: string | null;
  is_active?: boolean;
  occupancy_status: "occupied" | "empty";
  position_status: string;
  map_position: {
    left_pct: number;
    top_pct: number;
    width_pct: number;
    height_pct: number;
  } | null;
}

export interface WarehouseMoveDraft {
  client_item_id: string;
  source_key: string;
  operation: "pallet_move" | "lot_transfer";
  pallet_id?: number;
  lot_id?: number;
  expected_version: number;
  quantity?: number;
  source_location_id: number;
  source_floor_code: string;
  source_area_code: string | null;
  source_location_code: string;
  source_location_name: string;
  target_location_id: number;
  target_floor_code: string;
  target_area_code: string | null;
  target_location_code: string;
  target_location_name: string;
  inventory_code: string;
  product_name: string;
  customer_name: string;
  unit: string;
}

export function moveLocationBounds(features: LayoutFeature[], location: MoveDashboardLocation): { left: number; right: number; bottom: number; top: number } | null;
export function intersectMappedMoveTargets<T extends MoveDashboardLocation>(candidates: MoveCandidate[], dashboardLocations: T[], reservedTargetIds?: number[]): T[];
export function mergeLocationInventoryItems<T extends { lot_id?: number | null }>(palletItems?: T[], looseItems?: T[]): T[];
export function resolveMoveDropTarget<T extends MoveDashboardLocation>(features: LayoutFeature[], locations: T[], floorCode: string, xMm: number, yMm: number): { target: T | null; error: string | null };
export function upsertMoveDraft<T extends WarehouseMoveDraft>(drafts: T[], draft: T): { items: T[]; error: string | null };
export function buildMoveBatchPayload(idempotencyKey: string, drafts: WarehouseMoveDraft[]): {
  idempotency_key: string;
  confirmed: true;
  items: Array<Record<string, string | number | undefined>>;
};
