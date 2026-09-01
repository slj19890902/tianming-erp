export type ViewMode = "2d" | "25d";
export type CameraPreset = "fit" | "north_east" | "north_west" | "south_east" | "south_west";

export interface Bounds {
  min_x: number;
  min_y: number;
  max_x: number;
  max_y: number;
}

export interface Geometry {
  type: "polyline" | "circle" | "insert";
  closed?: boolean;
  points?: number[][];
  x_mm?: number;
  y_mm?: number;
  radius_mm?: number;
  rotation_deg?: number;
  block_name?: string;
  opening_width_mm?: number;
}

export interface Structure {
  id: string;
  source_handle: string;
  kind: "exterior_wall" | "wall" | "column" | "door" | "window" | "unknown";
  layer: string;
  locked: boolean;
  source_readonly: boolean;
  column_code?: string;
  geometry: Geometry;
}

export interface AssetTemplate {
  id: string;
  name: string;
  category: string;
  render_type: "box25d" | "png";
  image_url: string | null;
  color: string;
  default_width_mm: number;
  default_depth_mm: number;
  default_height_mm: number;
}

export interface Placement {
  id: string;
  layout_id: string;
  template_id: string;
  name: string;
  x_mm: number;
  y_mm: number;
  z_mm: number;
  width_mm: number;
  depth_mm: number;
  height_mm: number;
  rotation_deg: number;
  is_confirmed: boolean;
  is_locked: boolean;
  version: number;
}

export interface Rack {
  id: string;
  layout_id: string;
  rack_code: string;
  name: string;
  x_mm: number;
  y_mm: number;
  z_mm: number;
  width_mm: number;
  depth_mm: number;
  height_mm: number;
  levels: number;
  level_heights_mm: number[];
  cargo_rows: number;
  level_cell_counts?: number[];
  cell_plan_status?: "pending_admin_configuration" | "configured";
  bays: number;
  access_side: "north" | "south" | "east" | "west" | "both";
  min_aisle_width_mm: number;
  rotation_deg: number;
  color: string;
  source: "manual" | "ai";
  status: "candidate" | "confirmed";
  is_locked: boolean;
  version: number;
  area_feature_id?: string;
  area_code?: string;
  mold_rack_code?: string;
}

export interface Pallet {
  id: string;
  layout_id: string;
  pallet_code: string;
  name: string;
  zone_id: string;
  zone_code: string;
  x_mm: number;
  y_mm: number;
  z_mm: number;
  width_mm: number;
  depth_mm: number;
  height_mm: number;
  rotation_deg: number;
  color: string;
  visual_status: "empty" | "waiting" | "in_process" | "completed" | "abnormal";
  status_note: string;
  candidate_status_color?: string;
  visual_kind?: "location_anchor" | "physical_pallet";
  display_label?: string;
  operational_group_id?: string;
  is_logical_anchor?: boolean;
  is_planning_location_slot?: boolean;
  planning_slot_width_mm?: number;
  planning_slot_depth_mm?: number;
  is_simulated: boolean;
  version: number;
  snapped: boolean;
}

export interface ProductionProjectionMapping {
  id: string;
  source_task_id: number;
  source_type: "erp_production_task";
  target_kind: "pallet" | "zone";
  target_id: string;
  target_code: string | null;
  target_name: string | null;
  target_missing: boolean;
  confirmed_at: string;
  version: number;
}

export interface ProductionTaskProjection {
  source_task_id: number;
  source_version: number;
  order_id: number;
  order_number: string;
  customer_name: string;
  product_code: string;
  product_name: string;
  specification: string;
  status: "pending";
  planned_quantity: number;
  production_quantity_unit: "sets" | "pieces";
  task_updated_at: string | null;
  delivery_date: string | null;
  source_state: "current";
  mapping: ProductionProjectionMapping | null;
}

export interface StaleProductionProjectionMapping extends ProductionProjectionMapping {
  source_state: "not_pending_or_missing";
}

export interface ProductionProjectionResponse {
  available: boolean;
  source_label: string;
  source_read_only: true;
  fetched_at: string | null;
  error: string | null;
  layout_id: string;
  floor_code: string;
  items: ProductionTaskProjection[];
  stale_mappings: StaleProductionProjectionMapping[];
}

export interface LayoutFeature {
  id: string;
  layout_id: string;
  feature_code: string;
  name: string;
  feature_kind: "zone" | "aisle" | "no_go" | "structure";
  subtype: string;
  points: number[][];
  width_mm: number | null;
  direction: "one_way" | "two_way" | "none" | null;
  no_stacking: boolean;
  storage_mode: "floor" | "rack" | "overhead";
  elevation_mm: number;
  storage_height_mm: number;
  color: string;
  area_mm2: number;
  source: "manual" | "ai";
  status: "candidate" | "confirmed";
  is_locked: boolean;
  version: number;
  allowed_inventory_types?: Array<"finished" | "semi_finished" | "raw_material" | "mold" | "print_plate" | "temporary_turnover">;
  storage_layout?: "rack" | "pallet_ground" | "mixed";
}

export interface Violation {
  id: string;
  severity: "error" | "warning";
  rule_code: string;
  message: string;
  entity_kind: string;
  entity_id: string;
  related_kind: string | null;
  related_id: string | null;
}

export interface LayerVisibility {
  structures: boolean;
  equipment: boolean;
  racks: boolean;
  pallets: boolean;
  zones: boolean;
  aisles: boolean;
  noGo: boolean;
  customStructures: boolean;
  labels: boolean;
  production: boolean;
}

export interface ReferenceOverlayConfig {
  enabled: boolean;
  shared_coordinates?: boolean;
  offset_x_mm: number;
  offset_y_mm: number;
  scale_x: number;
  scale_y: number;
  mirror_x: boolean;
  mirror_y: boolean;
  rotation_deg: number;
  opacity: number;
}

export type SelectedEntity = { kind: "equipment" | "rack" | "pallet" | "feature" | "structure"; id: string } | null;

export interface LayoutSummary {
  id: string;
  name: string;
  floor_code: string;
  source_name: string;
  source_sha256: string;
  source_units: string;
  bounds_mm: Bounds;
  created_at: string;
  updated_at: string;
}

export interface Layout extends LayoutSummary {
  structures: Structure[];
  warnings: string[];
  placements: Placement[];
  racks: Rack[];
  pallets: Pallet[];
  features: LayoutFeature[];
  violations: Violation[];
  rule_defaults: Record<string, number>;
  metadata?: {
    calibration?: {
      status?: string;
      applied?: boolean;
    };
  };
  calibration?: {
    status?: string;
    applied?: boolean;
  };
}
