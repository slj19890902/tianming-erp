function normalized(value) {
  return String(value ?? "").trim().toLocaleLowerCase("zh-CN");
}

function pointInPolygon(x, y, points) {
  let inside = false;
  for (let index = 0, previous = points.length - 1; index < points.length; previous = index++) {
    const [xi, yi] = points[index];
    const [xj, yj] = points[previous];
    const crosses = yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi || 1) + xi;
    if (crosses) inside = !inside;
  }
  return inside;
}

function stableZonePoints(points, count) {
  if (!count || points.length < 3) return [];
  const xs = points.map((point) => Number(point[0]));
  const ys = points.map((point) => Number(point[1]));
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const width = Math.max(1, maxX - minX);
  const height = Math.max(1, maxY - minY);
  const marginX = Math.min(600, width * 0.08);
  const marginY = Math.min(500, height * 0.08);
  const candidates = [];
  const seen = new Set();
  for (let density = 2; density <= 8 && candidates.length < count; density += 1) {
    const columns = Math.max(1, Math.ceil(Math.sqrt(count * (width / height)) * density));
    const rows = Math.max(1, Math.ceil((count * density * density) / columns));
    for (let row = 0; row < rows; row += 1) {
      for (let column = 0; column < columns; column += 1) {
        const x = minX + marginX + ((column + 0.5) * Math.max(1, width - marginX * 2)) / columns;
        const y = minY + marginY + ((row + 0.5) * Math.max(1, height - marginY * 2)) / rows;
        const key = `${Math.round(x)}:${Math.round(y)}`;
        if (!seen.has(key) && pointInPolygon(x, y, points)) {
          seen.add(key);
          candidates.push([x, y]);
        }
      }
    }
  }
  if (!candidates.length) {
    const centroid = points.reduce((sum, point) => [sum[0] + Number(point[0]), sum[1] + Number(point[1])], [0, 0]);
    candidates.push([centroid[0] / points.length, centroid[1] / points.length]);
  }
  if (candidates.length <= count) return Array.from({ length: count }, (_, index) => candidates[index % candidates.length]);
  return Array.from({ length: count }, (_, index) => candidates[Math.floor((index * candidates.length) / count)]);
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
    row.quantity += Number(item.quantity ?? item.available_quantity ?? 0);
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
    row.quantity += Number(item.quantity ?? item.available_quantity ?? 0);
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
    ...(location?.loose_items || [])
  ].filter((item) => {
    const lotId = Number(item?.lot_id);
    if (!Number.isFinite(lotId) || lotId <= 0) return true;
    if (seenLotIds.has(lotId)) return false;
    seenLotIds.add(lotId);
    return true;
  });
}

export function singleLocationPallet(location) {
  const pallets = inventoryLocationPallets(location);
  return pallets.length === 1 ? pallets[0] : null;
}

function dispatchPalletItemQuantity(item) {
  if (item?.quantity !== undefined && item?.quantity !== null) return Math.max(0, Number(item.quantity) || 0);
  return Math.max(
    0,
    Number(item?.available_quantity || 0)
      + Number(item?.reserved_quantity || 0)
  );
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

export function buildMeasuredDispatchPallets(
  features,
  dispatchLocation,
  floorCode,
  standardPallet,
  layoutId = "erp-twin"
) {
  const standard = normalizeStandardPalletContract(standardPallet);
  if (!standard) return [];
  if (floorCode !== "1F" || dispatchLocation?.location_code !== "F1-DISPATCH-01") return [];
  const zones = (features || [])
    .filter((feature) => feature.feature_kind === "zone"
      && String(feature.subtype || "").toLowerCase() === "finished_wait_delivery"
      && feature.points?.length >= 3)
    .sort((left, right) => String(left.feature_code).localeCompare(String(right.feature_code), "zh-CN", { numeric: true }));
  const sourcePallets = inventoryLocationPallets(dispatchLocation);
  if (!zones.length || !sourcePallets.length) return [];

  const zonePallets = zones.map(() => []);
  sourcePallets.forEach((pallet, index) => zonePallets[index % zones.length].push(pallet));
  return zones.flatMap((zone, zoneIndex) => {
    const pallets = zonePallets[zoneIndex];
    const points = stableZonePoints(zone.points, pallets.length);
    return pallets.map((pallet, palletIndex) => {
      const items = Array.isArray(pallet.items) ? pallet.items : [];
      const productNames = [...new Set(items.map((item) => item.product_name).filter(Boolean))];
      const customerNames = [...new Set(items.map((item) => item.customer_name).filter(Boolean))];
      const totalQuantity = items.reduce((sum, item) => sum + dispatchPalletItemQuantity(item), 0);
      const unit = items.find((item) => item.unit)?.unit || "boxes";
      const productLabel = productNames.length === 1
        ? productNames[0]
        : productNames.length > 1 ? `${productNames[0]} 等 ${productNames.length} 款` : "产品名称待补充";
      const customerLabel = customerNames.length === 1
        ? customerNames[0]
        : customerNames.length > 1 ? `${customerNames.length} 个客户` : "客户待确认";
      return {
        id: `erp-dispatch-pallet-${pallet.pallet_id}`,
        layout_id: layoutId,
        pallet_code: pallet.pallet_code,
        name: `${productLabel} · ${totalQuantity.toLocaleString("zh-CN")} ${inventoryUnitLabel(unit)}`,
        zone_id: zone.id,
        zone_code: zone.feature_code,
        x_mm: points[palletIndex][0],
        y_mm: points[palletIndex][1],
        z_mm: 0,
        width_mm: standard.width_mm,
        depth_mm: standard.depth_mm,
        height_mm: standard.height_mm,
        rotation_deg: 0,
        color: "#ea580c",
        visual_status: "waiting",
        status_note: `真实待送栈板 · ${customerLabel} · ${pallet.pallet_code}`,
        is_simulated: false,
        version: Number(pallet.version || 1),
        snapped: false
      };
    });
  });
}

export function buildMappedLocationPallets(
  features,
  locations,
  floorCode,
  standardPallet,
  layoutId = "erp-twin"
) {
  const standard = normalizeStandardPalletContract(standardPallet);
  if (!standard) return [];
  const zoneByArea = new Map(
    features
      .filter((feature) => feature.feature_kind === "zone" && feature.erp_area_code && feature.points?.length >= 3)
      .map((feature) => [String(feature.erp_area_code), feature])
  );
  const grouped = new Map();
  for (const location of locations) {
    if (location.floor_code !== floorCode || !location.area_code) continue;
    if (["disabled", "unplaced", "unlocated"].includes(location.position_status || "")) continue;
    if (!zoneByArea.has(String(location.area_code))) continue;
    const key = String(location.area_code);
    grouped.set(key, [...(grouped.get(key) || []), location]);
  }
  const pallets = [];
  for (const [areaCode, areaLocations] of [...grouped.entries()].sort(([left], [right]) => left.localeCompare(right, "zh-CN"))) {
    const zone = zoneByArea.get(areaCode);
    const ordered = [...areaLocations].sort((left, right) => String(left.location_code).localeCompare(String(right.location_code), "zh-CN", { numeric: true }));
    const fallbackPositions = stableZonePoints(zone.points, ordered.length);
    const positions = ordered.map((location, index) => mappedLocationPoint(zone, location) || fallbackPositions[index]);
    const xs = zone.points.map((point) => Number(point[0]));
    const ys = zone.points.map((point) => Number(point[1]));
    ordered.forEach((location, index) => {
      const occupied = location.occupancy_status === "occupied";
      const locationPallets = inventoryLocationPallets(location);
      const actualPalletCode = locationPallets.length === 1 ? locationPallets[0].pallet_code : null;
      const palletSummary = locationPallets.length > 1 ? `${locationPallets.length} 块系统栈板` : null;
      const position = location.map_position;
      const mappedWidthMm = position ? (Number(position.width_pct) / 100) * (Math.max(...xs) - Math.min(...xs)) : 0;
      const mappedDepthMm = position ? (Number(position.height_pct) / 100) * (Math.max(...ys) - Math.min(...ys)) : 0;
      const rotation = mappedWidthMm > 0 && mappedDepthMm > 0 && Math.abs(mappedWidthMm - mappedDepthMm) > 50
        ? (mappedWidthMm < mappedDepthMm ? 90 : 0)
        : Math.max(...ys) - Math.min(...ys) > Math.max(...xs) - Math.min(...xs) ? 90 : 0;
      const hasMappedFootprint = mappedWidthMm > 0 && mappedDepthMm > 0;
      const representsPhysicalPallet = hasMappedFootprint && (
        (mappedWidthMm >= 1080 && mappedWidthMm <= 1320 && mappedDepthMm >= 900 && mappedDepthMm <= 1100)
        || (mappedWidthMm >= 900 && mappedWidthMm <= 1100 && mappedDepthMm >= 1080 && mappedDepthMm <= 1320)
      );
      const isLogicalAnchor = position?.layout_kind === "logical_anchor"
        || (position?.layout_kind !== "physical_pallet" && hasMappedFootprint && !representsPhysicalPallet);
      // The measured rectangle remains authoritative for the location centre and
      // orientation.  A physical pallet never inherits or scales to that legacy
      // rectangle: rotation may swap axes, while the one backend contract owns size.
      const renderedWidthMm = isLogicalAnchor
        ? (hasMappedFootprint ? Math.min(mappedWidthMm, 400) : 400)
        : standard.width_mm;
      const renderedDepthMm = isLogicalAnchor
        ? (hasMappedFootprint ? Math.min(mappedDepthMm, 400) : 400)
        : standard.depth_mm;
      pallets.push({
        id: `erp-location-${location.location_id}`,
        layout_id: layoutId,
        pallet_code: location.location_code,
        name: actualPalletCode
          ? `${location.location_name} · ${actualPalletCode}`
          : palletSummary
            ? `${location.location_name} · ${palletSummary}`
            : location.location_name,
        zone_id: zone.id,
        zone_code: zone.feature_code,
        x_mm: positions[index][0],
        y_mm: positions[index][1],
        z_mm: 0,
        width_mm: renderedWidthMm,
        depth_mm: renderedDepthMm,
        height_mm: isLogicalAnchor ? 90 : standard.height_mm,
        rotation_deg: rotation,
        color: occupied ? "#0f766e" : "#a16207",
        visual_status: occupied ? "waiting" : "empty",
        status_note: `${actualPalletCode
          ? `ERP正式库位 · ${actualPalletCode}`
          : palletSummary
            ? `ERP正式共享位置 · ${palletSummary} · 请在右侧逐块选择`
            : "ERP正式空库位"}${isLogicalAnchor ? " · 逻辑点位（非实尺度栈板占地）" : ""}`,
        is_logical_anchor: isLogicalAnchor,
        is_simulated: true,
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
