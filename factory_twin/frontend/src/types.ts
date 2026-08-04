export type ViewMode = "2d" | "25d";

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
}

export interface Structure {
  id: string;
  source_handle: string;
  kind: "exterior_wall" | "wall" | "column" | "door" | "unknown";
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
  bays: number;
  access_side: "north" | "south" | "east" | "west" | "both";
  min_aisle_width_mm: number;
  rotation_deg: number;
  color: string;
  source: "manual" | "ai";
  status: "candidate" | "confirmed";
  is_locked: boolean;
  version: number;
}

export interface LayoutFeature {
  id: string;
  layout_id: string;
  feature_code: string;
  name: string;
  feature_kind: "zone" | "aisle" | "no_go";
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
  version: number;
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
  zones: boolean;
  aisles: boolean;
  noGo: boolean;
  labels: boolean;
}

export type SelectedEntity = { kind: "equipment" | "rack" | "feature"; id: string } | null;

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
  features: LayoutFeature[];
  violations: Violation[];
  rule_defaults: Record<string, number>;
}
