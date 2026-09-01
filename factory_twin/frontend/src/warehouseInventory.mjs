function normalized(value) {
  return String(value ?? "").trim().toLocaleLowerCase("zh-CN");
}

export function inventoryPhysicalQuantity(item) {
  const hasBreakdown = [item?.available_quantity, item?.reserved_quantity, item?.damaged_quantity]
    .some((value) => value !== undefined && value !== null);
  if (hasBreakdown) {
    return [item?.available_quantity, item?.reserved_quantity, item?.damaged_quantity]
      .map((value) => Number(value || 0))
      .filter(Number.isFinite)
      .reduce((sum, value) => sum + value, 0);
  }
  const physical = Number(item?.quantity || 0);
  return Number.isFinite(physical) ? physical : 0;
}

export function inventoryHasPhysicalQuantity(item) {
  const hasQuantityFact = [
    item?.quantity,
    item?.available_quantity,
    item?.reserved_quantity,
    item?.damaged_quantity
  ].some((value) => value !== undefined && value !== null);
  // Older projections may not carry quantity fields. Keep those visible until
  // the backend supplies a quantity fact; only an explicit non-positive fact
  // is safe to remove from the current warehouse picture.
  return !hasQuantityFact || inventoryPhysicalQuantity(item) > 0;
}

function zoneBounds(points) {
  const xs = points.map((point) => Number(point[0]));
  const ys = points.map((point) => Number(point[1]));
  return {
    minX: Math.min(...xs),
    maxX: Math.max(...xs),
    minY: Math.min(...ys),
    maxY: Math.max(...ys)
  };
}

function mappedLocationPoint(zone, location) {
  const position = location.map_position;
  if (!position) return null;
  const bounds = zoneBounds(zone.points);
  const width = Math.max(1, bounds.maxX - bounds.minX);
  const height = Math.max(1, bounds.maxY - bounds.minY);
  return [
    bounds.minX + ((Number(position.left_pct) + Number(position.width_pct) / 2) / 100) * width,
    bounds.maxY - ((Number(position.top_pct) + Number(position.height_pct) / 2) / 100) * height
  ];
}

export function locationLayoutGeometry(zone, location, xMm, yMm) {
  const position = location.map_position;
  if (!zone?.points?.length || !position || !location.location_id || !Number(position.version)) return null;
  const bounds = zoneBounds(zone.points);
  const width = Math.max(1, bounds.maxX - bounds.minX);
  const height = Math.max(1, bounds.maxY - bounds.minY);
  const widthPct = Number(position.width_pct);
  const heightPct = Number(position.height_pct);
  const left = ((Number(xMm) - bounds.minX) / width) * 100 - widthPct / 2;
  const top = ((bounds.maxY - Number(yMm)) / height) * 100 - heightPct / 2;
  const rounded = (value) => Number(value.toFixed(4));
  return {
    location_id: Number(location.location_id),
    expected_version: Number(position.version),
    left_pct: rounded(Math.max(0, Math.min(100 - widthPct, left))),
    top_pct: rounded(Math.max(0, Math.min(100 - heightPct, top))),
    width_pct: rounded(widthPct),
    height_pct: rounded(heightPct),
    z_index: Number(position.z_index || 0)
  };
}

export function searchHighlightAreaCodes(items, floorCode) {
  return [...new Set(items
    .filter((item) => item.floor_code === floorCode)
    .filter((item) => item.area_code && !["disabled", "unplaced", "unlocated"].includes(item.position_status || ""))
    .map((item) => String(item.area_code)))]
    .sort((left, right) => left.localeCompare(right, "zh-CN"));
}

export function warehouseSearchProductKey(item) {
  const customerIdentity = item.customer_id
    ? `customer:${item.customer_id}`
    : `customer-name:${item.customer_name || ""}`;
  const businessIdentity = item.product_id
    ? `product:${item.product_id}`
    : [item.inventory_code || "", item.product_name || "", item.specification || ""].join("::");
  return [
    customerIdentity,
    businessIdentity,
    item.inventory_type || "",
    item.unit || ""
  ].join("::").toLocaleLowerCase("zh-CN");
}

export function warehouseSearchFloorSummaries(items) {
  const floors = new Map();
  for (const item of items || []) {
    const floorCode = item.floor_code || "UNLOCATED";
    const row = floors.get(floorCode) || {
      floor_code: floorCode,
      quantity: 0,
      location_count: 0,
      location_keys: new Set()
    };
    row.quantity += inventoryPhysicalQuantity(item);
    row.location_keys.add(item.location_id || `${item.area_code || "TEXT"}:${item.location_name || "待定位"}`);
    row.location_count = row.location_keys.size;
    floors.set(floorCode, row);
  }
  return [...floors.values()]
    .map(({ location_keys: _locationKeys, ...row }) => row)
    .sort((left, right) => String(left.floor_code).localeCompare(String(right.floor_code), "zh-CN", { numeric: true }));
}

export function warehouseSearchLocationSummaries(items) {
  const locations = new Map();
  for (const item of items || []) {
    const key = item.location_id
      ? `location:${item.location_id}`
      : `${item.floor_code || "UNLOCATED"}:${item.area_code || "TEXT"}:${item.location_name || "待定位"}`;
    const row = locations.get(key) || {
      key,
      floor_code: item.floor_code || "UNLOCATED",
      area_code: item.area_code || null,
      location_id: item.location_id || null,
      location_name: item.location_name || "位置待确认",
      position_status: item.position_status || "unlocated",
      quantity: 0
    };
    row.quantity += inventoryPhysicalQuantity(item);
    locations.set(key, row);
  }
  return [...locations.values()].sort((left, right) =>
    String(left.floor_code).localeCompare(String(right.floor_code), "zh-CN", { numeric: true })
    || String(left.area_code || "").localeCompare(String(right.area_code || ""), "zh-CN", { numeric: true })
    || String(left.location_name).localeCompare(String(right.location_name), "zh-CN", { numeric: true })
  );
}

export function inventoryLocationPallets(location) {
  const listed = Array.isArray(location?.pallets) ? location.pallets : [];
  const candidates = listed.length ? listed : location?.pallet ? [location.pallet] : [];
  const seen = new Set();
  return candidates
    .map((pallet) => {
      if (!Array.isArray(pallet?.items)) return pallet;
      const items = pallet.items.filter((item) => inventoryHasPhysicalQuantity(item));
      if (!items.length) return null;
      if (items.length === pallet.items.length) return pallet;
      return {
        ...pallet,
        items,
        item_count: items.length,
        visible_item_count: items.length
      };
    })
    .filter(Boolean)
    .filter((pallet, index) => {
      const palletId = Number(pallet?.pallet_id);
      const identity = Number.isFinite(palletId) && palletId > 0 ? `id:${palletId}` : `legacy:${index}`;
      if (seen.has(identity)) return false;
      seen.add(identity);
      return true;
    })
    .sort((left, right) => {
      const leftId = Number(left.pallet_id);
      const rightId = Number(right.pallet_id);
      if (Number.isFinite(leftId) && Number.isFinite(rightId)) return leftId - rightId;
      if (Number.isFinite(leftId)) return -1;
      if (Number.isFinite(rightId)) return 1;
      return String(left.pallet_code || "").localeCompare(String(right.pallet_code || ""), "zh-CN", { numeric: true });
    });
}

export function inventoryLocationItems(location) {
  const seenLotIds = new Set();
  return [
    ...inventoryLocationPallets(location).flatMap((pallet) => pallet.items || []),
    ...(location?.loose_items || []).filter((item) => inventoryHasPhysicalQuantity(item))
  ].filter((item) => {
    const lotId = Number(item?.lot_id);
    if (!Number.isFinite(lotId) || lotId <= 0) return true;
    if (seenLotIds.has(lotId)) return false;
    seenLotIds.add(lotId);
    return true;
  });
}

export function normalizeInventoryLocationProjection(location) {
  if (!location) return location;
  const pallets = inventoryLocationPallets(location);
  const looseItems = (location.loose_items || [])
    .filter((item) => inventoryHasPhysicalQuantity(item));
  const occupied = pallets.length > 0 || looseItems.length > 0;
  return {
    ...location,
    pallets,
    pallet: pallets.length === 1 ? pallets[0] : null,
    loose_items: looseItems,
    occupancy_status: occupied ? "occupied" : "empty"
  };
}

export function singleLocationPallet(location) {
  const pallets = inventoryLocationPallets(location);
  return pallets.length === 1 ? pallets[0] : null;
}

export function employeeLocationName(location) {
  const name = String(
    location?.employee_location_name
      || location?.current_address_name
      || location?.location_name
      || ""
  ).trim();
  return name || "位置名称待完善";
}

const LEGACY_V11_RIGHT_AREA_CODES = new Set([
  "A1", "A2", "AB1", "AB2", "B1", "B2", "C1", "C2", "CD1", "D1", "D2",
  "DE1", "E1", "E2", "E3", "E4", "F1", "F12", "F2", "F3", "F34", "F4"
]);

function isFloorThree(value) {
  const normalized = String(value ?? "").trim().toUpperCase();
  return normalized === "3" || normalized === "3F" || normalized === "三楼";
}

function isGenericLegacyAreaName(name, areaCode) {
  const compact = String(name || "").replace(/\s+/g, "").toUpperCase();
  return compact === `${areaCode}区` || compact === `三楼${areaCode}区`;
}

export function employeeAreaName(area, context = {}) {
  const projected = String(area?.employee_area_name || "").trim();
  if (projected) return projected;
  const code = String(area?.area_code || area?.erp_area_code || "").trim().toUpperCase();
  const floor = context.floorCode ?? context.floorNumber ?? area?.floor_code ?? area?.floor_number ?? area?.warehouse_floor;
  const formalName = String(area?.formal_area_name || area?.area_name || "").trim();
  const usesRightDefault = isFloorThree(floor) && LEGACY_V11_RIGHT_AREA_CODES.has(code);
  if (usesRightDefault) {
    const rightLabel = `右区${code}`;
    if (!formalName || isGenericLegacyAreaName(formalName, code)) return rightLabel;
    if (formalName.replace(/\s+/g, "").includes(rightLabel)) return formalName;
    const escapedCode = code.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const description = formalName
      .replace(new RegExp(`^(?:三楼\\s*)?(?:${escapedCode}(?:\\s*区)?\\s*)?`, "i"), "")
      .replace(/^[\s·-]+|[\s·-]+$/g, "");
    return description ? `${rightLabel}·${description}` : rightLabel;
  }
  if (formalName) return formalName;
  return formalName || String(area?.name || "").trim() || code || "区域名称待完善";
}

export function normalizeStandardPalletContract(value) {
  if (!value || typeof value !== "object") return null;
  const contractVersion = String(value.contract_version || "").trim();
  const widthMm = Number(value.width_mm);
  const depthMm = Number(value.depth_mm);
  const heightMm = Number(value.height_mm);
  if (
    contractVersion !== "standard-pallet-v1"
    || !Number.isFinite(widthMm)
    || !Number.isFinite(depthMm)
    || !Number.isFinite(heightMm)
    || widthMm <= 0
    || depthMm <= 0
    || heightMm <= 0
  ) return null;
  return {
    contract_version: contractVersion,
    width_mm: widthMm,
    depth_mm: depthMm,
    height_mm: heightMm
  };
}

export function standardPalletContractsMatch(left, right) {
  const normalizedLeft = normalizeStandardPalletContract(left);
  const normalizedRight = normalizeStandardPalletContract(right);
  return Boolean(
    normalizedLeft
    && normalizedRight
    && normalizedLeft.contract_version === normalizedRight.contract_version
    && normalizedLeft.width_mm === normalizedRight.width_mm
    && normalizedLeft.depth_mm === normalizedRight.depth_mm
    && normalizedLeft.height_mm === normalizedRight.height_mm
  );
}

export function standardPalletDisplayIssue({
  loading,
  dashboardReady = true,
  requestedFloorCode,
  layoutFloorCode,
  layoutContract,
  dashboardContract
}) {
  if (loading || !dashboardReady || !layoutFloorCode || layoutFloorCode !== requestedFloorCode) return "";
  return standardPalletContractsMatch(layoutContract, dashboardContract)
    ? ""
    : "标准栈板尺寸合同缺失或前后端不一致，系统已停止绘制实体栈板；请刷新或联系管理员。";
}

export function buildMeasuredDispatchPallets(
  _features,
  _dispatchLocation,
  _floorCode,
  _standardPallet,
  _layoutId = "erp-twin"
) {
  // F1-DISPATCH-01 has ledger identity but no measured geometry.  Borrowing a
  // FIN polygon would falsely tell operators that the stock has been moved.
  // The overview's unlocated_inventory blocker is the only valid projection.
  return [];
}

export function buildMappedLocationPallets(
  features,
  locations,
  floorCode,
  standardPallet,
  layoutId = "erp-twin",
  renderEmptyPlanningSlots = false
) {
  const standard = normalizeStandardPalletContract(standardPallet);
  if (!standard) return [];
  const measuredZones = features.filter(
    (feature) => feature.feature_kind === "zone" && feature.id && feature.points?.length >= 3
  );
  const zoneById = new Map(measuredZones.map((feature) => [String(feature.id), feature]));
  const grouped = new Map();
  for (const location of locations) {
    if (location.floor_code !== floorCode) continue;
    if (location.position_status !== "mapped") continue;
    if (!Number(location.map_position?.version)) continue;
    const mapFeatureId = String(location.map_feature_id || "").trim();
    let zone = mapFeatureId ? zoneById.get(mapFeatureId) : null;
    if (!zone && String(location.source_version || "").trim().toUpperCase() === "V11") {
      const areaCode = String(location.area_code || "").trim().toUpperCase();
      zone = areaCode
        ? measuredZones.find(
          (feature) => String(feature.erp_area_code || "").trim().toUpperCase() === areaCode
        ) || null
        : null;
    }
    if (!zone) continue;
    const key = String(zone.id);
    const group = grouped.get(key) || { zone, locations: [] };
    group.locations.push(normalizeInventoryLocationProjection(location));
    grouped.set(key, group);
  }
  const pallets = [];
  for (const [, { zone, locations: areaLocations }] of [...grouped.entries()].sort(([left], [right]) => left.localeCompare(right, "zh-CN"))) {
    const ordered = [...areaLocations].sort((left, right) => String(left.location_code).localeCompare(String(right.location_code), "zh-CN", { numeric: true }));
    const positions = ordered.map((location) => mappedLocationPoint(zone, location));
    const xs = zone.points.map((point) => Number(point[0]));
    const ys = zone.points.map((point) => Number(point[1]));
    ordered.forEach((location, index) => {
      const readableLocationName = employeeLocationName(location);
      const occupied = location.occupancy_status === "occupied";
      const hasUnmatchedObservation = Boolean(location.has_unmatched_inventory_observation);
      const hasLocationDiscrepancy = Boolean(location.has_location_discrepancy);
      const hasRedInventoryIssue = hasUnmatchedObservation || hasLocationDiscrepancy;
      const unmatchedObservationCount = Number(location.unmatched_inventory_observation_count || 0)
        + Number(location.location_discrepancy_count || 0);
      const locationPallets = inventoryLocationPallets(location);
      const actualPalletCode = locationPallets.length === 1 ? locationPallets[0].pallet_code : null;
      const palletSummary = locationPallets.length > 1 ? `${locationPallets.length} 块系统栈板` : null;
      const position = location.map_position;
      const mappedWidthMm = position ? (Number(position.width_pct) / 100) * (Math.max(...xs) - Math.min(...xs)) : 0;
      const mappedDepthMm = position ? (Number(position.height_pct) / 100) * (Math.max(...ys) - Math.min(...ys)) : 0;
      const rotation = mappedWidthMm > 0 && mappedDepthMm > 0 && Math.abs(mappedWidthMm - mappedDepthMm) > 50
        ? (mappedWidthMm < mappedDepthMm ? 90 : 0)
        : Math.max(...ys) - Math.min(...ys) > Math.max(...xs) - Math.min(...xs) ? 90 : 0;
      const isEmptyGroundLocation = !occupied && (
        position?.layout_kind === "physical_pallet"
        || location.storage_type === "ground"
      );
      const isPlanningLocationSlot = renderEmptyPlanningSlots && isEmptyGroundLocation;
      const isLogicalAnchor = locationPallets.length !== 1;
      // The measured rectangle remains authoritative for the location centre and
      // orientation.  A physical pallet never inherits or scales to that legacy
      // rectangle: rotation may swap axes, while the one backend contract owns size.
      const renderedWidthMm = isLogicalAnchor ? 0 : standard.width_mm;
      const renderedDepthMm = isLogicalAnchor ? 0 : standard.depth_mm;
      pallets.push({
        id: `erp-location-${location.location_id}`,
        layout_id: layoutId,
        pallet_code: location.location_code,
        name: actualPalletCode
          ? `${readableLocationName} · ${actualPalletCode}`
          : palletSummary
            ? `${readableLocationName} · ${palletSummary}`
            : readableLocationName,
        zone_id: zone.id,
        zone_code: zone.feature_code,
        x_mm: positions[index][0],
        y_mm: positions[index][1],
        z_mm: 0,
        width_mm: renderedWidthMm,
        depth_mm: renderedDepthMm,
        height_mm: isLogicalAnchor ? 0 : standard.height_mm,
        rotation_deg: rotation,
        color: hasRedInventoryIssue ? "#b91c1c" : occupied ? "#2563eb" : "#16a34a",
        candidate_status_color: hasRedInventoryIssue ? "#b91c1c" : occupied ? "#2563eb" : "#16a34a",
        visual_status: occupied ? "waiting" : "empty",
        status_note: `${hasRedInventoryIssue ? `现场库存待核对 · ${unmatchedObservationCount || 1} 条红色异常 · ` : ""}${actualPalletCode
          ? `ERP正式库位 · ${actualPalletCode}`
          : palletSummary
            ? `ERP正式共享位置 · ${palletSummary} · 请在右侧逐块选择`
            : "ERP正式空库位"}${isLogicalAnchor ? " · 逻辑位置标记（非实物占地）" : ""}`,
        visual_kind: isLogicalAnchor ? "location_anchor" : "physical_pallet",
        display_label: readableLocationName,
        operational_group_id: `location:${location.location_id}`,
        is_logical_anchor: isLogicalAnchor,
        is_planning_location_slot: isPlanningLocationSlot,
        planning_slot_width_mm: isPlanningLocationSlot ? standard.width_mm : undefined,
        planning_slot_depth_mm: isPlanningLocationSlot ? standard.depth_mm : undefined,
        is_simulated: false,
        version: 1,
        snapped: false
      });
    });
  }
  return pallets;
}

function palletBounds(pallet, clearanceMm = 0) {
  const quarterTurns = Math.round((Number(pallet.rotation_deg || 0) % 180) / 90);
  const swapAxes = Math.abs(quarterTurns) % 2 === 1;
  const width = swapAxes ? Number(pallet.depth_mm || 0) : Number(pallet.width_mm || 0);
  const depth = swapAxes ? Number(pallet.width_mm || 0) : Number(pallet.depth_mm || 0);
  return {
    minX: Number(pallet.x_mm) - width / 2 - clearanceMm,
    maxX: Number(pallet.x_mm) + width / 2 + clearanceMm,
    minY: Number(pallet.y_mm) - depth / 2 - clearanceMm,
    maxY: Number(pallet.y_mm) + depth / 2 + clearanceMm
  };
}

function segmentBounds(start, end, widthMm) {
  const x1 = Number(start?.[0]);
  const y1 = Number(start?.[1]);
  const x2 = Number(end?.[0]);
  const y2 = Number(end?.[1]);
  if (![x1, y1, x2, y2].every(Number.isFinite)) return null;
  const length = Math.hypot(x2 - x1, y2 - y1);
  if (length < 1) return null;
  const halfWidth = Math.max(0, Number(widthMm || 0)) / 2;
  const normalX = (-(y2 - y1) / length) * halfWidth;
  const normalY = ((x2 - x1) / length) * halfWidth;
  const corners = [
    [x1 + normalX, y1 + normalY],
    [x1 - normalX, y1 - normalY],
    [x2 + normalX, y2 + normalY],
    [x2 - normalX, y2 - normalY]
  ];
  return {
    minX: Math.min(...corners.map((point) => point[0])),
    maxX: Math.max(...corners.map((point) => point[0])),
    minY: Math.min(...corners.map((point) => point[1])),
    maxY: Math.max(...corners.map((point) => point[1]))
  };
}

function boundsOverlap(left, right) {
  return left.minX < right.maxX && left.maxX > right.minX && left.minY < right.maxY && left.maxY > right.minY;
}

export function findPalletColumnConflicts(pallets, structures = [], features = [], clearanceMm = 0) {
  const columnBounds = [];
  for (const structure of structures) {
    if (structure.kind !== "column") continue;
    const geometry = structure.geometry || {};
    if (geometry.type === "circle" && Number.isFinite(Number(geometry.x_mm)) && Number.isFinite(Number(geometry.y_mm)) && Number(geometry.radius_mm) > 0) {
      const radius = Number(geometry.radius_mm);
      columnBounds.push({
        column_id: structure.id,
        minX: Number(geometry.x_mm) - radius,
        maxX: Number(geometry.x_mm) + radius,
        minY: Number(geometry.y_mm) - radius,
        maxY: Number(geometry.y_mm) + radius
      });
    } else if (geometry.type === "polyline" && geometry.points?.length >= 3) {
      const xs = geometry.points.map((point) => Number(point[0])).filter(Number.isFinite);
      const ys = geometry.points.map((point) => Number(point[1])).filter(Number.isFinite);
      if (xs.length && ys.length) columnBounds.push({ column_id: structure.id, minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) });
    }
  }
  for (const feature of features) {
    if (feature.feature_kind !== "structure" || feature.subtype !== "custom_column") continue;
    for (let index = 0; index < (feature.points?.length || 0) - 1; index += 1) {
      const bounds = segmentBounds(feature.points[index], feature.points[index + 1], feature.width_mm);
      if (bounds) columnBounds.push({ column_id: feature.id, ...bounds });
    }
  }
  const conflicts = [];
  const seen = new Set();
  for (const pallet of pallets) {
    if (pallet?.visual_kind === "location_anchor" || pallet?.is_logical_anchor) continue;
    const candidate = palletBounds(pallet, clearanceMm);
    for (const column of columnBounds) {
      if (!boundsOverlap(candidate, column)) continue;
      const key = `${pallet.id}:${column.column_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      conflicts.push({ pallet_id: pallet.id, column_id: column.column_id });
    }
  }
  return conflicts;
}

export function expandAreaInventory(locations, floorCode, areaCode) {
  if (!areaCode) return [];
  const seenLotIds = new Set();
  return locations
    .filter((location) => location.floor_code === floorCode && location.area_code === areaCode)
    .flatMap((location) => [
      ...inventoryLocationPallets(location).flatMap((pallet) => (pallet.items || []).map((item) => ({
        ...item,
        location_code: location.location_code,
        location_name: location.location_name,
        pallet_code: pallet.pallet_code || null
      }))),
      ...(location.loose_items || []).map((item) => ({
        ...item,
        location_code: location.location_code,
        location_name: location.location_name,
        pallet_code: null
      }))
    ])
    .filter((item) => {
      const lotId = Number(item?.lot_id);
      if (!Number.isFinite(lotId) || lotId <= 0) return true;
      if (seenLotIds.has(lotId)) return false;
      seenLotIds.add(lotId);
      return true;
    })
    .sort((left, right) => {
      const leftAge = Number.isFinite(left.age_days) ? left.age_days : -1;
      const rightAge = Number.isFinite(right.age_days) ? right.age_days : -1;
      if (leftAge !== rightAge) return rightAge - leftAge;
      return normalized(left.inventory_code || left.lot_number).localeCompare(
        normalized(right.inventory_code || right.lot_number),
        "zh-CN"
      );
    });
}

export function filterAreaInventory(items, keyword) {
  const needle = normalized(keyword);
  if (!needle) return [...items];
  return items.filter((item) => normalized([
    item.inventory_code,
    item.product_name,
    item.customer_name,
    item.lot_number,
    item.location_code,
    item.location_name,
    item.pallet_code
  ].join(" ")).includes(needle));
}

export function inventoryAgeLabel(ageDays) {
  if (!Number.isFinite(ageDays)) return "库龄待确认";
  if (ageDays <= 0) return "今日入库";
  return `库龄 ${Math.floor(ageDays)} 天`;
}

export function inventoryAgeTone(ageDays) {
  if (!Number.isFinite(ageDays)) return "unknown";
  if (ageDays > 180) return "critical";
  if (ageDays > 90) return "warning";
  return "normal";
}

export function inventoryUnitLabel(unit) {
  const value = normalized(unit);
  const labels = {
    box: "只",
    boxes: "只",
    sheet: "张",
    sheets: "张",
    piece: "件",
    pieces: "件",
    set: "套",
    sets: "套",
    pcs: "件",
    set: "套",
    sets: "套"
  };
  return labels[value] || String(unit ?? "");
}
