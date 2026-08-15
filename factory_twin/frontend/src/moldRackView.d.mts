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
