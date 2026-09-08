import type { Rack } from "./types";
import type { PalletSnapResult } from "./palletSnap.mjs";
export function snapRackPosition(rack: Rack, x: number, y: number, racks: Rack[], threshold?: number, enabled?: boolean): PalletSnapResult;
