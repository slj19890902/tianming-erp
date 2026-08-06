import type { Rack } from "./types";

export interface RackFrontSlot {
  id: string;
  tier: number;
  column: number;
  isGround: boolean;
  label: string;
  sampleSku: string;
  quantity: number;
  unit: string;
}

export interface RackFrontTier {
  tier: number;
  isGround: boolean;
  title: string;
  heightMm: number;
  slots: RackFrontSlot[];
}

export function buildRackFrontSlots(rack: Rack): RackFrontTier[];
