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

export function filterPlanningPublishedFeatures<T>(
  publishedLayout: { features?: T[] } | null | undefined,
  draftLayout: { features?: T[] } | null | undefined
): T[];

export function removeZoneHierarchy<T>(
  layout: T | null | undefined,
  featureId: string,
  areaCode?: string | null
): T | null | undefined;

export function clearFormalAreaOptions<T>(current: T[]): T[];

export function formalAreaOptionsEffectEnabled(
  state: FormalAreaOptionsEffectState
): boolean;
