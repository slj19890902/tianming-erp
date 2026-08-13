export interface FormalAreaOptionsEffectState {
  canEditLocations: boolean;
  locationEditMode: boolean;
  areaPolicyEditMode: boolean;
  hasSelectedFeature: boolean;
  formalAreaId?: number | null;
}

export function stableTwinFeatures<T extends { features?: unknown[] }>(
  layout: T | null | undefined
): unknown[];

export function clearFormalAreaOptions<T>(current: T[]): T[];

export function formalAreaOptionsEffectEnabled(
  state: FormalAreaOptionsEffectState
): boolean;
