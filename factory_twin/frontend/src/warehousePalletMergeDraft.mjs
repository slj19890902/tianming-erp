function positiveInteger(value) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function itemQuantity(item) {
  if (item?.quantity !== undefined && item?.quantity !== null) return Number(item.quantity);
  return Number(item?.available_quantity || 0)
    + Number(item?.reserved_quantity || 0)
    + Number(item?.damaged_quantity || 0);
}

function oneValue(items, getter) {
  const normalized = items.map(getter);
  if (normalized.some((value) => value === null || value === undefined || value === "")) return null;
  const values = [...new Set(normalized)];
  return values.length === 1 ? values[0] : null;
}

function targetLocationAllows(location, inventoryType) {
  if (location?.is_active === false || location?.position_status !== "mapped" || !location?.map_position) return false;
  if (location?.storage_type === "rack") return false;
  if (inventoryType === "finished") return ["finished", "shared"].includes(location?.warehouse_type);
  if (inventoryType === "semi_finished") return ["semi_finished", "shared"].includes(location?.warehouse_type);
  return false;
}

export function normalizePalletMergeCandidate(location, pallet) {
  const palletId = positiveInteger(pallet?.pallet_id);
  const expectedVersion = positiveInteger(pallet?.version);
  if (!palletId || !expectedVersion) return { candidate: null, error: "系统栈板缺少有效编号或版本。" };
  if (!positiveInteger(location?.location_id) || location?.position_status !== "mapped" || !location?.map_position) {
    return { candidate: null, error: "栈板不在已发布的真实地图位置。" };
  }
  if (location?.is_active === false || location?.storage_type === "rack") {
    return { candidate: null, error: "停用位置或货架位不允许整板合并。" };
  }

  const items = (Array.isArray(pallet?.items) ? pallet.items : []).filter((item) => itemQuantity(item) > 0);
  if (!items.length) return { candidate: null, error: "栈板没有可合并的剩余货物。" };
  if (items.some((item) => !positiveInteger(item?.lot_id) || !positiveInteger(item?.version))) {
    return { candidate: null, error: "栈板含未关联正式批次的快照，不能合并。" };
  }

  const customerId = oneValue(items, (item) => positiveInteger(item?.customer_id));
  if (!customerId) return { candidate: null, error: "栈板客户归属不唯一。" };
  const inventoryType = oneValue(items, (item) => ["finished", "semi_finished"].includes(item?.inventory_type) ? item.inventory_type : null);
  if (!inventoryType) return { candidate: null, error: "栈板库存类型不唯一。" };
  const unit = oneValue(items, (item) => String(item?.unit || "").trim() || null);
  if (!unit) return { candidate: null, error: "栈板原生单位不唯一。" };
  const inventoryStatus = oneValue(items, (item) => ["active", "frozen"].includes(item?.status) ? item.status : null);
  if (!inventoryStatus) return { candidate: null, error: "栈板冻结状态不唯一或不可确认。" };
  const qualityStatus = oneValue(items, (item) => Number(item?.damaged_quantity || 0) > 0 ? "damaged" : "usable");
  if (!qualityStatus) return { candidate: null, error: "栈板质量状态不唯一。" };
  if (qualityStatus !== "usable") return { candidate: null, error: "栈板含损坏数量，当前多栈合并必须保持质量状态兼容并保守阻止。" };

  const totalQuantity = items.reduce((total, item) => total + itemQuantity(item), 0);
  if (!Number.isFinite(totalQuantity) || totalQuantity <= 0) {
    return { candidate: null, error: "栈板数量无效。" };
  }
  const productKeys = new Set(items.map((item) => item?.product_id || item?.inventory_code || item?.lot_id));
  const candidate = {
    pallet_id: palletId,
    pallet_code: String(pallet?.pallet_code || `栈板 ${palletId}`),
    expected_version: expectedVersion,
    location_id: Number(location.location_id),
    location_code: String(location?.location_code || ""),
    location_name: String(location?.location_name || "实际位置待确认"),
    floor_code: String(location?.floor_code || ""),
    area_code: location?.area_code || null,
    customer_id: customerId,
    customer_name: String(items.find((item) => positiveInteger(item?.customer_id) === customerId)?.customer_name || "客户待确认"),
    inventory_type: inventoryType,
    unit,
    inventory_status: inventoryStatus,
    quality_status: qualityStatus,
    total_quantity: totalQuantity,
    lot_count: items.length,
    product_count: productKeys.size,
    target_eligible: targetLocationAllows(location, inventoryType)
  };
  return { candidate, error: null };
}

export function palletMergeCompatibility(left, right) {
  if (!left || !right) return { compatible: false, error: "栈板兼容信息缺失。" };
  if (left.customer_id !== right.customer_id) return { compatible: false, error: "只能合并同一客户的系统栈板。" };
  if (left.inventory_type !== right.inventory_type) return { compatible: false, error: "只能合并同一库存类型的系统栈板。" };
  if (left.unit !== right.unit) return { compatible: false, error: "只能合并原生单位相同的系统栈板。" };
  if (left.inventory_status !== right.inventory_status) return { compatible: false, error: "冻结状态不兼容，不能合并。" };
  if (left.quality_status !== right.quality_status) return { compatible: false, error: "质量状态不兼容，不能合并。" };
  return { compatible: true, error: null };
}

export function togglePalletMergeSource(sources, candidate) {
  const current = Array.isArray(sources) ? sources : [];
  const existing = current.find((item) => item.pallet_id === candidate?.pallet_id);
  if (existing) return { items: current.filter((item) => item.pallet_id !== candidate.pallet_id), error: null };
  if (!candidate) return { items: current, error: "该系统栈板不能作为合并来源。" };
  const mismatch = current.map((item) => palletMergeCompatibility(item, candidate)).find((result) => !result.compatible);
  if (mismatch) return { items: current, error: mismatch.error };
  return { items: [...current, candidate], error: null };
}

export function palletMergeTargetChoices(selected) {
  const current = Array.isArray(selected) ? selected : [];
  if (current.length < 2) return [];
  return current
    .filter((item) => item?.target_eligible)
    .filter((item) => current.every((other) => other.pallet_id === item.pallet_id || palletMergeCompatibility(other, item).compatible))
    .sort((left, right) => String(left.floor_code).localeCompare(String(right.floor_code), "zh-CN", { numeric: true })
      || String(left.area_code || "").localeCompare(String(right.area_code || ""), "zh-CN", { numeric: true })
      || String(left.location_code).localeCompare(String(right.location_code), "zh-CN", { numeric: true })
      || left.pallet_id - right.pallet_id);
}

export function buildPalletMergeBatchPayload(idempotencyKey, sources, target) {
  return {
    idempotency_key: idempotencyKey,
    confirmed: true,
    target_pallet_id: target.pallet_id,
    expected_target_version: target.expected_version,
    sources: (sources || []).filter((item) => item.pallet_id !== target.pallet_id).map((item) => ({
      client_item_id: item.client_item_id,
      pallet_id: item.pallet_id,
      expected_version: item.expected_version
    }))
  };
}
