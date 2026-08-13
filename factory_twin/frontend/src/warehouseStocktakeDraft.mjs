function positiveInteger(value) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function draftIdentity(draft) {
  if (draft?.operation === "add") {
    return [
      "add",
      positiveInteger(draft.location_id),
      positiveInteger(draft.customer_id),
      positiveInteger(draft.product_id),
      draft.inventory_type,
      draft.unit
    ].join(":");
  }
  if (draft?.operation === "decrease") return `decrease:${positiveInteger(draft.lot_id) || ""}`;
  return "";
}

function normalized(value) {
  return String(value || "").trim().toUpperCase();
}

export function stocktakeLocationBlockReason(location) {
  if (!location) return "请先在地图选择正式货位。";
  if (!location.is_active) return "该货位已停用，不能加入盘点草稿。";
  if (location.position_status !== "mapped") return "该货位尚未发布到正式地图，不能加入盘点草稿。";
  if (!positiveInteger(location.map_position?.version)) return "该货位缺少当前地图版本，请刷新后重试。";
  const floorCode = String(location.floor_code || "");
  const sourceVersion = String(location.source_version || "");
  const supportedSource = sourceVersion === "TWIN_V1"
    ? ["1F", "3F"].includes(floorCode)
    : sourceVersion === "V11" && floorCode === "3F";
  if (!supportedSource) {
    return "该货位缺少受支持的正式地图来源，仅允许 TWIN_V1 一楼/三楼或 V11 三楼库位。";
  }
  if (!normalized(location.area_code) || !normalized(location.location_code)) {
    return "该货位缺少正式区域或库位编码，不能加入盘点草稿。";
  }
  if (
    normalized(location.area_code) === "DISPATCH"
    || ["F1-DISPATCH-01", "1F-DISPATCH-01"].includes(normalized(location.location_code))
  ) {
    return "一楼待送区不能通过盘点新增或调减库存。";
  }
  return null;
}

export function stocktakeAddBlockReason(location, inventoryType) {
  const locationError = stocktakeLocationBlockReason(location);
  if (locationError) return locationError;
  const allowedWarehouseTypes = inventoryType === "finished"
    ? ["finished", "shared"]
    : inventoryType === "semi_finished"
      ? ["semi_finished", "shared"]
      : [];
  if (!allowedWarehouseTypes.includes(String(location.warehouse_type || ""))) {
    return "所选货位类型与当前库存类型不匹配。";
  }
  const storageType = String(location.storage_type || "").trim().toLowerCase();
  if (inventoryType === "finished" && storageType === "rack") {
    return "本轮成品盘点新增尚不支持货架位，请选择正式地面位。";
  }
  if (inventoryType === "finished" && !["ground", "temporary_aisle"].includes(storageType)) {
    return "本轮成品盘点新增只支持正式地面位。";
  }
  if (inventoryType === "semi_finished" && !["ground", "rack", "temporary_aisle"].includes(storageType)) {
    return "该半成品货位的存储方式尚不支持盘点新增。";
  }
  return null;
}

export function stocktakeDecreaseBlockReason(item) {
  if (!item) return "请先选择一个真实库存批次。";
  if (item.stocktake_decrease_eligible === true) return null;
  return String(item.stocktake_decrease_block_reason || "").trim()
    || "该批次未通过盘点调减资格校验，请刷新后核对库存状态。";
}

export function validateStocktakeDraft(draft) {
  if (!draft?.client_item_id || !String(draft.client_item_id).trim()) return "盘点草稿缺少页面明细编号。";
  if (!positiveInteger(draft.location_id)) return "请选择已发布的正式货位。";
  if (!positiveInteger(draft.expected_layout_version)) return "盘点草稿缺少当前地图版本。";
  if (!positiveInteger(draft.quantity)) return "盘点数量必须是正整数。";
  if (draft.operation === "add") {
    if (!positiveInteger(draft.customer_id) || !positiveInteger(draft.product_id)) return "盘点新增必须选择已有客户和已有产品。";
    if (!['finished', 'semi_finished'].includes(draft.inventory_type)) return "盘点新增库存类型无效。";
    const expectedUnit = draft.inventory_type === "finished" ? "boxes" : "sheets";
    if (draft.unit !== expectedUnit) return `该库存类型的固定单位必须是 ${expectedUnit}。`;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(draft.stock_date || ""))) return "请选择有效库存日期。";
    return null;
  }
  if (draft.operation === "decrease") {
    if (!positiveInteger(draft.lot_id) || !positiveInteger(draft.expected_version)) return "盘点调减必须来自带版本的真实库存批次。";
    if (Number.isFinite(Number(draft.available_quantity)) && Number(draft.quantity) > Number(draft.available_quantity)) {
      return "盘点调减数量不能大于当前可用数量。";
    }
    return null;
  }
  return "盘点草稿操作类型无效。";
}

export function upsertStocktakeDraft(drafts, draft) {
  const error = validateStocktakeDraft(draft);
  if (error) return { items: drafts || [], error };
  const identity = draftIdentity(draft);
  if (draft.operation === "add") {
    const conflictingAdd = (drafts || []).find((item) => (
      item.operation === "add"
      && positiveInteger(item.location_id) === positiveInteger(draft.location_id)
      && draftIdentity(item) !== identity
    ));
    if (conflictingAdd) {
      return {
        items: drafts || [],
        error: "同一货位不能在一个盘点批次中新增不同客户、产品、库存类型或单位。"
      };
    }
  }
  const currentIndex = (drafts || []).findIndex((item) => draftIdentity(item) === identity);
  if (currentIndex < 0) return { items: [...(drafts || []), draft], error: null };
  const items = [...drafts];
  items[currentIndex] = { ...items[currentIndex], ...draft, client_item_id: items[currentIndex].client_item_id };
  return { items, error: null };
}

export function removeStocktakeDraft(drafts, clientItemId) {
  return (drafts || []).filter((item) => item.client_item_id !== clientItemId);
}

export function clearStocktakeDrafts() {
  return [];
}

export function buildStocktakeBatchPayload(idempotencyKey, drafts) {
  if (!String(idempotencyKey || "").trim()) throw new Error("盘点批次缺少幂等键。");
  if (!Array.isArray(drafts) || drafts.length === 0) throw new Error("盘点批次没有草稿明细。");
  if (drafts.length > 50) throw new Error("一次最多提交 50 条盘点明细。");
  for (const draft of drafts) {
    const error = validateStocktakeDraft(draft);
    if (error) throw new Error(error);
  }
  return {
    idempotency_key: idempotencyKey,
    confirmed: true,
    items: drafts.map((draft) => draft.operation === "add" ? {
      client_item_id: draft.client_item_id,
      operation: "add",
      location_id: draft.location_id,
      expected_layout_version: draft.expected_layout_version,
      customer_id: draft.customer_id,
      product_id: draft.product_id,
      inventory_type: draft.inventory_type,
      unit: draft.unit,
      quantity: draft.quantity,
      stock_date: draft.stock_date
    } : {
      client_item_id: draft.client_item_id,
      operation: "decrease",
      location_id: draft.location_id,
      expected_layout_version: draft.expected_layout_version,
      lot_id: draft.lot_id,
      expected_version: draft.expected_version,
      quantity: draft.quantity
    })
  };
}
