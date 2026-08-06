import type { Bounds, ReferenceOverlayConfig } from "./types";

export const LEGACY_BASE: Readonly<{ scale_x: number; scale_y: number; offset_x_mm: number; offset_y_mm: number }>;
export function createDefaultReferenceOverlay(sharedCoordinates?: boolean): ReferenceOverlayConfig;
export function normalizeReferenceOverlayDraft(saved: unknown, sharedCoordinates?: boolean): ReferenceOverlayConfig;
export function referenceOverlayAnchor(bounds: Bounds): { source_x_mm: number; source_y_mm: number };
export function transformReferencePoint(x: number, y: number, bounds: Bounds, config: ReferenceOverlayConfig): [number, number];
