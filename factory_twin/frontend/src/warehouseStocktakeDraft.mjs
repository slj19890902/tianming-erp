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
      draft.unit,
      draft.source_kind,
      draft.stock_stage || "complete"
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
  const supportedSource = ["TWIN_V1", "CURRENT_MAP"].includes(sourceVersion)
    ? ["1F", "3F", "4F"].includes(floorCode)
    : sourceVersion === "V11" && floorCode === "3F";
  if (!supportedSource) {
    return "该货位尚未接入可盘点的正式地图。";
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
  const allowedWarehouseTypes = ["finished", "semi_finished", "raw_material"].includes(inventoryType)
    ? ["finished", "semi_finished", "shared"] : [];
  if (!allowedWarehouseTypes.includes(String(location.warehouse_type || ""))) {
    return "该货位不是可盘点的正式库存位置。";
  }
  const storageType = String(location.storage_type || "").trim().toLowerCase();
  if (inventoryType === "finished" && !["ground", "rack", "temporary_aisle"].includes(storageType)) {
    return "该成品货位的存储方式尚不支持盘点新增。";
  }
  if (["semi_finished", "raw_material"].includes(inventoryType) && !["ground", "rack", "temporary_aisle"].includes(storageType)) {
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

export function stocktakeBlockResolution(reason) {
  const message = String(reason || "").trim();
  if (!message) return "刷新地图后重新选择该货位；仍有提示时请联系管理员核对正式库存。";
  if (/预占|待送分配|业务任务/.test(message)) {
    return "先在对应订单或送货任务中释放占用，再刷新地图重新盘点。";
  }
  if (/冻结/.test(message)) {
    return "先由管理员完成冻结库存复核和解冻；冻结期间不能直接调减。";
  }
  if (/生产完工|生产任务|采购用途/.test(message)) {
    return "该库存仍有业务来源约束，请从对应生产、来料或任务单处理，不能用盘点绕过。";
  }
  if (/尚未发布|正式地图|地图位置|地图版本|正式区域|正式楼层|正式货位|位置不可用|已停用/.test(message)) {
    return "请管理员在区域规划中完成区域启用、货位发布和位置保存，然后刷新地图再盘点。";
  }
  if (/未完成盘点|盘点任务/.test(message)) {
    return "先完成或撤销已有盘点单，再刷新地图重新操作。";
  }
  if (/报损|报废/.test(message)) {
    return "先在库存明细中处理报损或报废数量，再对剩余可用库存盘点。";
  }
  if (/单位|类型/.test(message)) {
    return "请改选与货位用途、库存类型和单位一致的正式货物；不要直接改写数量。";
  }
  if (/成本|材质报价|展开纸板|人工费用/.test(message)) {
    return "请在常用箱核对提示的产品或子件开料尺寸、每张产出及供应商报价；组套父件不用补纸板资料。修正后保留草稿重试，无需解除库存约束。";
  }
  return "刷新地图并核对最新库存；仍无法处理时请管理员从库存明细核对对应业务来源。";
}

function locationInventoryItems(location) {
  const palletItems = Array.isArray(location?.pallets)
    ? location.pallets.flatMap((pallet) => Array.isArray(pallet?.items) ? pallet.items : [])
    : Array.isArray(location?.pallet?.items)
      ? location.pallet.items
      : [];
  const looseItems = Array.isArray(location?.loose_items) ? location.loose_items : [];
  return [...palletItems, ...looseItems];
}

export function stocktakeExistingProductLocations(
  locations,
  { customerId, productId, inventoryType, targetFloorCode, targetAreaCode, targetLocationId } = {}
) {
  const customer = positiveInteger(customerId);
  const product = positiveInteger(productId);
  if (!customer || !product || !["finished", "semi_finished", "raw_material"].includes(inventoryType)) return [];
  const targetArea = normalized(targetAreaCode);
  const targetFloor = normalized(targetFloorCode);
  const targetLocation = positiveInteger(targetLocationId);
  const matches = [];
  for (const location of Array.isArray(locations) ? locations : []) {
    for (const item of locationInventoryItems(location)) {
      const allowedProductIds = Array.isArray(item?.allowed_product_ids)
        ? item.allowed_product_ids.map(positiveInteger).filter(Boolean)
        : [];
      const productMatches = positiveInteger(item?.product_id) === product
        || allowedProductIds.includes(product);
      if (
        positiveInteger(item?.customer_id) !== customer
        || !productMatches
        || (item?.inventory_usage || item?.inventory_type) !== inventoryType
        || Number(item?.available_quantity || 0) <= 0
      ) continue;
      matches.push({
        ...item,
        source_location_id: positiveInteger(location.location_id),
        source_floor_code: String(location.floor_code || ""),
        source_area_code: location.area_code || null,
        source_location_code: location.location_code || "",
        source_location_name: location.employee_location_name || location.location_name || "位置名称待完善",
        source_layout_version: positiveInteger(location.map_position?.version),
        is_target_location: positiveInteger(location.location_id) === targetLocation,
        is_outside_target_area: Boolean(targetArea) && (
          normalized(location.area_code) !== targetArea
          || Boolean(targetFloor) && normalized(location.floor_code) !== targetFloor
        )
      });
    }
  }
  return matches.sort((left, right) => (
    Number(right.is_outside_target_area) - Number(left.is_outside_target_area)
    || String(left.source_floor_code).localeCompare(String(right.source_floor_code), "zh-CN", { numeric: true })
    || String(left.source_location_name).localeCompare(String(right.source_location_name), "zh-CN", { numeric: true })
    || Number(left.lot_id || 0) - Number(right.lot_id || 0)
  ));
}

export function validateStocktakeDraft(draft) {
  if (!draft?.client_item_id || !String(draft.client_item_id).trim()) return "盘点草稿缺少页面明细编号。";
  if (!positiveInteger(draft.location_id)) return "请选择已发布的正式货位。";
  if (!positiveInteger(draft.expected_layout_version)) return "盘点草稿缺少当前地图版本。";
  if (!positiveInteger(draft.quantity)) return "盘点数量必须是正整数。";
  if (draft.operation === "add") {
    if (!positiveInteger(draft.customer_id) || !positiveInteger(draft.product_id)) return "盘点新增必须选择已有客户和已有产品。";
    if (!['finished', 'semi_finished', 'raw_material'].includes(draft.inventory_type)) return "盘点新增库存类型无效。";
    if (draft.source_kind && !['existing_stocktake', 'partner_transfer'].includes(draft.source_kind)) return "请选择成品库存的实际来源。";
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
      stock_date: draft.stock_date,
      source_kind: draft.source_kind || "existing_stocktake",
      stock_stage: draft.stock_stage || "complete"
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
