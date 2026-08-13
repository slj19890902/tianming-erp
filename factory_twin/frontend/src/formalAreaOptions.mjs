const EMPTY_TWIN_FEATURES = Object.freeze([]);

export function stableTwinFeatures(layout) {
  return Array.isArray(layout?.features) ? layout.features : EMPTY_TWIN_FEATURES;
}

export function clearFormalAreaOptions(current) {
  return Array.isArray(current) && current.length === 0 ? current : [];
}

export function formalAreaOptionsEffectEnabled({
  canEditLocations,
  locationEditMode,
  areaPolicyEditMode,
  hasSelectedFeature,
  formalAreaId
}) {
  return Boolean(
    canEditLocations
    && locationEditMode
    && areaPolicyEditMode
    && hasSelectedFeature
    && !formalAreaId
  );
}
