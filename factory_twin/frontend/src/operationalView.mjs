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
  const sampledRows = [];
  for (let index = 0; index <= slices; index += 1) {
    const y = minY + ((maxY - minY) * index) / slices;
    const xs = horizontalWallIntersections(structures, y)
      .filter((value) => value >= minX - 1 && value <= maxX + 1);
    sampledRows.push(xs.length < 2 ? null : [Math.min(...xs), y, Math.max(...xs)]);
  }
  const maximumGapRows = Math.max(3, Math.floor(slices * 0.1));
  for (let index = 0; index < sampledRows.length;) {
    if (sampledRows[index]) {
      index += 1;
      continue;
    }
    const start = index;
    while (index < sampledRows.length && !sampledRows[index]) index += 1;
    const end = index - 1;
    const before = sampledRows[start - 1];
    const after = sampledRows[index];
    if (!before || !after || end - start + 1 > maximumGapRows) continue;
    for (let gapIndex = start; gapIndex <= end; gapIndex += 1) {
      const y = minY + ((maxY - minY) * gapIndex) / slices;
      sampledRows[gapIndex] = [
        Math.min(before[0], after[0]),
        y,
        Math.max(before[2], after[2])
      ];
    }
  }
  const rows = sampledRows.filter(Boolean);
  if (rows.length < Math.max(4, Math.floor(slices * 0.2))) return [];
  const jumpThreshold = Math.max(600, (maxX - minX) * 0.02);
  const returnTolerance = jumpThreshold * 0.35;
  const repairShortInwardExcursion = (sideIndex, inwardDirection) => {
    for (let index = 1; index < rows.length; index += 1) {
      const previous = rows[index - 1][sideIndex];
      const current = rows[index][sideIndex];
      if ((current - previous) * inwardDirection < jumpThreshold) continue;
      const limit = Math.min(rows.length - 1, index + maximumGapRows);
      let returnIndex = -1;
      for (let candidate = index + 1; candidate <= limit; candidate += 1) {
        const returned = (rows[candidate][sideIndex] - previous) * inwardDirection;
        if (returned <= returnTolerance) {
          returnIndex = candidate;
          break;
        }
      }
      if (returnIndex < 0) continue;
      const outside = inwardDirection > 0
        ? Math.min(previous, rows[returnIndex][sideIndex])
        : Math.max(previous, rows[returnIndex][sideIndex]);
      for (let repair = index; repair < returnIndex; repair += 1) {
        rows[repair][sideIndex] = outside;
      }
      index = returnIndex;
    }
  };
  repairShortInwardExcursion(0, 1);
  repairShortInwardExcursion(2, -1);
  const steppedRows = [rows[0]];
  for (let index = 1; index < rows.length; index += 1) {
    const previous = rows[index - 1];
    const current = rows[index];
    if (
      Math.abs(current[0] - previous[0]) >= jumpThreshold
      || Math.abs(current[2] - previous[2]) >= jumpThreshold
    ) {
      const transitionY = (previous[1] + current[1]) / 2;
      steppedRows.push(
        [previous[0], transitionY, previous[2]],
        [current[0], transitionY, current[2]]
      );
    }
    steppedRows.push(current);
  }
  return [
    ...steppedRows.map(([left, y]) => [left, y]),
    ...steppedRows.slice().reverse().map(([, y, right]) => [right, y])
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

export function shouldShowWarehousePalletVisual(pallet, hideRackLocationMarkers = false) {
  return !(hideRackLocationMarkers && pallet?.is_rack_location);
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
