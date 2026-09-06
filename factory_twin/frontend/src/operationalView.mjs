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
  return "#dcefe3";
}

export function warehouseZoneColor(visualTheme, fallback) {
  return visualTheme === "warehouse" ? "#60a5fa" : fallback;
}

function horizontalWallIntersections(structures, y) {
  const xs = [];
  for (const structure of structures || []) {
    if (!["wall", "exterior_wall"].includes(structure?.kind)) continue;
    const geometry = structure?.geometry || {};
    const points = geometry.points || [];
    if (geometry.type !== "polyline" || points.length < 2) continue;
    const edgeCount = geometry.closed ? points.length : points.length - 1;
    for (let index = 0; index < edgeCount; index += 1) {
      const [x1, y1] = points[index];
      const [x2, y2] = points[(index + 1) % points.length];
      if (![x1, y1, x2, y2].every(Number.isFinite)) continue;
      if (y1 === y2) {
        if (Math.abs(y - y1) < 0.001) xs.push(x1, x2);
        continue;
      }
      if ((y1 <= y && y < y2) || (y2 <= y && y < y1)) {
        xs.push(x1 + ((y - y1) * (x2 - x1)) / (y2 - y1));
      }
    }
  }
  return xs;
}

export function warehousePassageEnvelope(bounds, structures, sliceCount = 224) {
  const minY = Number(bounds?.min_y);
  const maxY = Number(bounds?.max_y);
  const minX = Number(bounds?.min_x);
  const maxX = Number(bounds?.max_x);
  if (![minX, minY, maxX, maxY].every(Number.isFinite) || maxX <= minX || maxY <= minY) return [];
  const slices = Math.max(16, Math.min(512, Math.round(sliceCount)));
  const rows = [];
  for (let index = 0; index <= slices; index += 1) {
    const y = minY + ((maxY - minY) * index) / slices;
    const xs = horizontalWallIntersections(structures, y)
      .filter((value) => value >= minX - 1 && value <= maxX + 1);
    if (xs.length < 2) continue;
    rows.push([Math.min(...xs), y, Math.max(...xs)]);
  }
  if (rows.length < Math.max(4, Math.floor(slices * 0.2))) return [];
  return [
    ...rows.map(([left, y]) => [left, y]),
    ...rows.slice().reverse().map(([, y, right]) => [right, y])
  ];
}

export function warehousePassageSurfaceStyle(visualTheme) {
  return visualTheme === "warehouse"
    ? { visible: true, color: "#dcefe3", elevationMm: 0 }
    : { visible: false, color: "#f1f5f9", elevationMm: -4 };
}

export function effectiveMapFeatures(visualTheme, features = []) {
  return visualTheme === "warehouse"
    ? features.filter((feature) => feature?.feature_kind !== "aisle")
    : [...features];
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
