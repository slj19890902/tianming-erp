export interface MoldRackViewItem {
  id: number;
  mold_code: string;
  mold_name: string;
  rack_location: string;
  location_guide?: {
    kind?: string | null;
    level?: number | null;
    grid?: number | null;
    row?: number | null;
  } | null;
  products?: Array<{
    id: number;
    customer_name?: string | null;
    product_code?: string | null;
    product_name?: string | null;
  }>;
}

export interface MoldShelfSpine {
  key: string;
  mold_id: number;
  product_id: number | null;
  code: string;
  name: string;
  customer_name: string | null;
  mold_code: string;
  mold_name: string;
  rack_location: string;
}

export interface MoldRackCell<T extends MoldRackViewItem> {
  grid: number;
  items: T[];
}

export interface MoldRackLevel<T extends MoldRackViewItem> {
  level: number;
  cell_count: number;
  blocked: boolean;
  cells: Array<MoldRackCell<T>>;
  level_only_items: T[];
}

export function buildMoldRackView<T extends MoldRackViewItem>(
  rack: { levels: number; bays?: number; level_cell_counts?: number[] },
  items: T[],
  blockedLevels?: number[]
): {
  levels: Array<MoldRackLevel<T>>;
  rack_only_items: T[];
  unmatched_items: T[];
  total_items: number;
};

export function moldRacksForArea<
  TFeature extends { id?: string; feature_code?: string; erp_area_code?: string | null },
  TRack extends { area_feature_id?: string | null; area_code?: string | null; mold_rack_code?: string | null }
>(feature: TFeature | null | undefined, racks: TRack[]): TRack[];

export function buildMoldShelfSpines<T extends MoldRackViewItem>(items: T[]): MoldShelfSpine[];
