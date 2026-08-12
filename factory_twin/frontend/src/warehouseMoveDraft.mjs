function normalizedId(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function zoneBounds(points) {
  if (!Array.isArray(points) || points.length < 3) return null;
  const xs = points.map((point) => Number(point?.[0])).filter(Number.isFinite);
  const ys = points.map((point) => Number(point?.[1])).filter(Number.isFinite);
  if (!xs.length || !ys.length) return null;
  return {
    minX: Math.min(...xs),
    maxX: Math.max(...xs),
    minY: Math.min(...ys),
    maxY: Math.max(...ys)
  };
}

export function moveLocationBounds(features, location) {
  const position = location?.map_position;
  const areaCode = String(location?.area_code || "");
  if (!position || !areaCode) return null;
  const zone = features.find((feature) =>
    feature?.feature_kind === "zone"
    && String(feature?.erp_area_code || "") === areaCode
  );
  const bounds = zoneBounds(zone?.points);
  if (!bounds) return null;
  const width = Math.max(1, bounds.maxX - bounds.minX);
  const height = Math.max(1, bounds.maxY - bounds.minY);
  const left = bounds.minX + (Number(position.left_pct) / 100) * width;
  const right = left + (Number(position.width_pct) / 100) * width;
  const top = bounds.maxY - (Number(position.top_pct) / 100) * height;
  const bottom = top - (Number(position.height_pct) / 100) * height;
  if (![left, right, top, bottom].every(Number.isFinite)) return null;
  return { left: Math.min(left, right), right: Math.max(left, right), bottom: Math.min(bottom, top), top: Math.max(bottom, top) };
}

export function intersectMappedMoveTargets(candidates, dashboardLocations, reservedTargetIds = []) {
  const candidateIds = new Set(
    (candidates || [])
      .filter((item) => item?.is_empty !== false && item?.occupied !== true)
      .map((item) => normalizedId(item?.id))
      .filter(Boolean)
  );
  const reserved = new Set((reservedTargetIds || []).map(normalizedId).filter(Boolean));
  return (dashboardLocations || [])
    .filter((location) => candidateIds.has(normalizedId(location?.location_id)))
    .filter((location) => location?.is_active !== false)
    .filter((location) => location?.occupancy_status === "empty")
    .filter((location) => location?.position_status === "mapped" && location?.map_position)
    .filter((location) => !reserved.has(normalizedId(location?.location_id)))
    .sort((left, right) =>
      String(left.floor_code || "").localeCompare(String(right.floor_code || ""), "zh-CN", { numeric: true })
      || String(left.area_code || "").localeCompare(String(right.area_code || ""), "zh-CN", { numeric: true })
      || String(left.location_code || "").localeCompare(String(right.location_code || ""), "zh-CN", { numeric: true })
    );
}

export function mergeLocationInventoryItems(palletItems = [], looseItems = []) {
  const seenLotIds = new Set();
  return [...(palletItems || []), ...(looseItems || [])].filter((item) => {
    const lotId = normalizedId(item?.lot_id);
    if (lotId === null) return true;
    if (seenLotIds.has(lotId)) return false;
    seenLotIds.add(lotId);
    return true;
  });
}

export function resolveMoveDropTarget(features, locations, floorCode, xMm, yMm) {
  const hits = (locations || []).filter((location) => {
    if (location?.floor_code !== floorCode) return false;
    const bounds = moveLocationBounds(features, location);
    return bounds
      && Number(xMm) >= bounds.left
      && Number(xMm) <= bounds.right
      && Number(yMm) >= bounds.bottom
      && Number(yMm) <= bounds.top;
  });
  if (hits.length === 1) return { target: hits[0], error: null };
  if (hits.length > 1) return { target: null, error: "落点同时命中多个空货位，请改用右侧楼层、区域、货位选择。" };
  return { target: null, error: "请把货物拖到有权限且已发布的空货位内，或改用右侧三级选择。" };
}

export function upsertMoveDraft(drafts, draft) {
  const sourceKey = String(draft?.source_key || "");
  const targetId = normalizedId(draft?.target_location_id);
  if (!sourceKey || !targetId) return { items: drafts || [], error: "移货来源或目标货位无效。" };
  const collision = (drafts || []).find((item) =>
    item.source_key !== sourceKey && normalizedId(item.target_location_id) === targetId
  );
  if (collision) return { items: drafts || [], error: "该目标货位已被另一条页面草稿占用，请先撤销或改选空位。" };
  const currentIndex = (drafts || []).findIndex((item) => item.source_key === sourceKey);
  if (currentIndex < 0) return { items: [...(drafts || []), draft], error: null };
  const items = [...drafts];
  items[currentIndex] = { ...items[currentIndex], ...draft, client_item_id: items[currentIndex].client_item_id };
  return { items, error: null };
}

export function buildMoveBatchPayload(idempotencyKey, drafts) {
  return {
    idempotency_key: idempotencyKey,
    confirmed: true,
    items: (drafts || []).map((draft) => ({
      client_item_id: draft.client_item_id,
      operation: draft.operation,
      ...(draft.operation === "pallet_move" ? { pallet_id: draft.pallet_id } : { lot_id: draft.lot_id, quantity: draft.quantity }),
      expected_version: draft.expected_version,
      target_location_id: draft.target_location_id
    }))
  };
}
