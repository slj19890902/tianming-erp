import type { InventoryProjectionItem, InventoryProjectionLocation } from "./warehouseInventory.mjs";
export interface IntakePaint { bucket: string; color: string; unknown: boolean; hasUnknown?: boolean; empty: boolean; dimmed?: boolean }
export const INTAKE_AGE_BANDS: ReadonlyArray<{bucket: string; label: string; color: string; max: number}>;
export const UNKNOWN_INTAKE_COLOR: string;
export function intakeAgePaint(ageDays: unknown): IntakePaint;
export function oldestIntakePaint(items: InventoryProjectionItem[]): IntakePaint;
export function warehouseIntakePaints(locations: InventoryProjectionLocation[], selectedItems?: InventoryProjectionItem[] | null): Record<number, IntakePaint>;
export function rackIntakePaint(rackId: string, locations: InventoryProjectionLocation[], paints: Record<number, IntakePaint>): IntakePaint;
export function intakeOutlineState(state?: { selected?: boolean; search?: boolean; hover?: boolean }): { color: string; width: number };
