import type { Bounds, LayoutFeature, Pallet } from "./types";

export function filterOperationalFeatures(
  floorCode: string,
  bounds: Bounds,
  features: LayoutFeature[]
): LayoutFeature[];

export function warehouseFrustumDivisor(
  floorCode: string,
  visualTheme: "editor" | "warehouse"
): number;

export function warehouseAisleColor(
  floorCode: string,
  visualTheme: "editor" | "warehouse",
  fallback: string
): string;

export function warehouseZoneColor(
  visualTheme: "editor" | "warehouse",
  fallback: string
): string;

export function warehousePassageEnvelope(
  bounds: Bounds,
  structures: Array<{
    kind?: string;
    geometry?: { type?: string; closed?: boolean; points?: number[][] };
  }>,
  sliceCount?: number
): number[][];

export function warehousePassageSurfaceStyle(
  visualTheme: "editor" | "warehouse"
): {
  visible: boolean;
  color: string;
  elevationMm: number;
};

export function effectiveMapFeatures(
  visualTheme: "editor" | "warehouse",
  features?: LayoutFeature[]
): LayoutFeature[];

export function aisleSurfaceStyle(visualTheme: "editor" | "warehouse"): {
  transparent: boolean;
  opacity: number;
  depthWrite: boolean;
  heightMm: number;
  elevationMm: number;
};

export function operationalEntitySelectable(
  visualTheme: "editor" | "warehouse",
  entityKind: "structure" | "feature" | "equipment" | "rack" | "pallet",
  featureKind?: "zone" | "aisle" | "no_go" | "structure" | null
): boolean;

export function shouldShowWarehousePalletVisual(
  pallet: Pallet,
  hideRackLocationMarkers?: boolean
): boolean;

export function wallSurfaceStyle(
  visualTheme: "editor" | "warehouse",
  viewMode: "2d" | "25d"
): {
  transparent: boolean;
  opacity: number;
  depthWrite: boolean;
};
