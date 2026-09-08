export interface ShelfReadingItem {
  lot_id: number; product_id?: number | null; customer_id?: number | null;
  inventory_type?: string; unit?: string; specification?: string | null; material?: string | null;
  location_id?: number; location_code?: string | null; quantity?: number;
  available_quantity?: number; reserved_quantity?: number; damaged_quantity?: number;
  composite_parent_group_key?: string | null;
}
export function groupShelfProducts<T extends ShelfReadingItem>(items: T[]): Array<{
  key: string; item: T; items: T[]; physical: number; available: number; reserved: number; damaged: number;
}>;
export function filterShelfMolds<T extends {id: number; mold_name: string; chinese_abbreviation?: string;
  products?: Array<{customer_name?: string | null; product_code?: string | null; product_name?: string | null}>}>(items: T[], query?: string): T[];
