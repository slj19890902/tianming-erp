import type { LayoutFeature, Pallet } from "./types";

export interface PalletSnapGuide { axis: "x" | "y"; value: number }
export interface PalletSnapResult { x: number; y: number; snapped: boolean; guides: PalletSnapGuide[] }

export function palletRect(pallet: Pallet, x?: number, y?: number): { left: number; right: number; bottom: number; top: number };
export function snapPalletPosition(
  pallet: Pallet,
  proposedX: number,
  proposedY: number,
  pallets: Pallet[],
  zones: LayoutFeature[],
  threshold?: number,
  enabled?: boolean
): PalletSnapResult;
