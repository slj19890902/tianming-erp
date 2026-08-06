export type EquipmentVisualKind = "printing" | "die_cutter" | "forming" | "conveyor" | "generic";

export function classifyEquipment(name?: string, category?: string): EquipmentVisualKind;
export function distanceMm(first?: number[], second?: number[]): number;
export function formatDistanceMm(value: number): string;
export function niceScaleLengthMm(targetMm: number): number;
export function accessDirectionVectors(accessSide: string): number[][];
