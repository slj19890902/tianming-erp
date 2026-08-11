const FLOOR_ONE = "1F";

function featureMinimumX(feature) {
  const xs = (feature?.points || [])
    .map((point) => Number(point?.[0]))
    .filter(Number.isFinite);
  return xs.length ? Math.min(...xs) : Number.NEGATIVE_INFINITY;
}

function zoneInsideBounds(feature, bounds) {
  const points = feature?.points || [];
  if (points.length < 3) return false;
  const minX = Number(bounds?.min_x);
  const minY = Number(bounds?.min_y);
  const maxX = Number(bounds?.max_x);
  const maxY = Number(bounds?.max_y);
  if (![minX, minY, maxX, maxY].every(Number.isFinite)) return false;
  return points.every((point) => {
    const x = Number(point?.[0]);
    const y = Number(point?.[1]);
    return Number.isFinite(x) && Number.isFinite(y)
      && x >= minX && x <= maxX && y >= minY && y <= maxY;
  });
}

export function filterOperationalFeatures(floorCode, bounds, features) {
  if (String(floorCode || "").toUpperCase() !== FLOOR_ONE) return [...features];

  const minX = Number(bounds?.min_x || 0);
  const maxX = Number(bounds?.max_x || 0);
  const workshopSpanX = Math.max(0, maxX - minX);
  const workshopSouthEdgeX = maxX + Math.max(2500, workshopSpanX * 0.12);

  return features.filter((feature) => {
    if (feature?.feature_kind === "zone" && !zoneInsideBounds(feature, bounds)) return false;
    return !(
      feature?.feature_kind === "structure"
      && feature?.subtype === "custom_column"
      && featureMinimumX(feature) > workshopSouthEdgeX
    );
  });
}

export function warehouseFrustumDivisor(floorCode, visualTheme) {
  if (visualTheme !== "warehouse") return 1.8;
  return 2.65;
}

export function warehouseAisleColor(floorCode, visualTheme, fallback) {
  if (visualTheme !== "warehouse") return fallback;
  return "#16a34a";
}

export function aisleSurfaceStyle(visualTheme) {
  if (visualTheme !== "warehouse") {
    return { transparent: true, opacity: 0.34, depthWrite: false, heightMm: 18, elevationMm: 12 };
  }
  return { transparent: false, opacity: 1, depthWrite: true, heightMm: 16, elevationMm: 10 };
}

export function operationalEntitySelectable(visualTheme, entityKind, featureKind = null) {
  if (visualTheme !== "warehouse") return true;
  return entityKind === "equipment" || entityKind === "rack" || (entityKind === "feature" && featureKind === "zone");
}

export function wallSurfaceStyle(visualTheme, viewMode) {
  if (visualTheme !== "warehouse") {
    return {
      transparent: viewMode === "25d",
      opacity: viewMode === "25d" ? 0.78 : 0.94,
      depthWrite: true
    };
  }
  return {
    transparent: true,
    opacity: viewMode === "25d" ? 0.34 : 0.2,
    depthWrite: false
  };
}
