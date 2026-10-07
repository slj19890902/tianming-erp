export interface ShelfReadingItem {
  stock_date?: string | null; stock_date_accuracy?: string;
  lot_id: number; product_id?: number | null; customer_id?: number | null;
  inventory_code?: string | null; product_name?: string | null; box_style?: string | null;
  is_bom_component?: boolean | null;
  inventory_type?: string; unit?: string; specification?: string | null; material?: string | null;
  location_id?: number | null; location_code?: string | null; quantity?: number;
  available_quantity?: number; reserved_quantity?: number; damaged_quantity?: number;
  composite_parent_group_key?: string | null;
}
export function shelfStockDates(items: ShelfReadingItem[]): {first: string | null; latest: string | null; incomplete: boolean; approximate: boolean};
export function groupShelfProducts<T extends ShelfReadingItem>(items: T[]): Array<{
  key: string; item: T; items: T[]; physical: number; available: number; reserved: number; damaged: number;
}>;
export function filterShelfMolds<T extends {id: number; mold_name: string; chinese_abbreviation?: string;
  products?: Array<{customer_name?: string | null; product_code?: string | null; product_name?: string | null; customer_drawing_number?: string | null; customer_drawing_display?: string | null}>}>(items: T[], query?: string): T[];
