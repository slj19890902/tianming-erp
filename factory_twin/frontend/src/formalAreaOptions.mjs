const EMPTY_TWIN_FEATURES = Object.freeze([]);

export function stableTwinFeatures(layout) {
  return Array.isArray(layout?.features) ? layout.features : EMPTY_TWIN_FEATURES;
}

export function filterPlanningPublishedFeatures(publishedLayout, _draftLayout) {
  // Lookup, planning and move must share the same applied region set.  A saved
  // draft may preview only the object currently being edited; it must not hide
  // published regions from the other modes before that draft is applied.
  return stableTwinFeatures(publishedLayout).filter(
    (feature) => feature?.feature_kind !== "aisle"
  );
}

export function removeZoneHierarchy(layout, featureId, areaCode) {
  if (!layout) return layout;
  const normalizedFeatureId = String(featureId || "").trim();
  const normalizedAreaCode = String(areaCode || "").trim().toUpperCase();
  const featureCodes = new Set(
    stableTwinFeatures(layout)
      .filter((feature) => String(feature?.id || "") === normalizedFeatureId)
      .flatMap((feature) => [feature?.feature_code, feature?.erp_area_code])
      .map((value) => String(value || "").trim().toUpperCase())
      .filter(Boolean)
  );
  if (normalizedAreaCode) featureCodes.add(normalizedAreaCode);
  return {
    ...layout,
    features: stableTwinFeatures(layout).filter(
      (feature) => String(feature?.id || "") !== normalizedFeatureId
    ),
    racks: (layout.racks || []).filter((rack) => (
      String(rack?.area_feature_id || "") !== normalizedFeatureId
      && !featureCodes.has(String(rack?.area_code || "").trim().toUpperCase())
    )),
    pallets: (layout.pallets || []).filter((pallet) => (
      String(pallet?.zone_id || "") !== normalizedFeatureId
      && !featureCodes.has(String(pallet?.zone_code || "").trim().toUpperCase())
    ))
  };
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
