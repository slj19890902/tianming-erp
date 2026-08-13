export function floor1CandidateBlockerHref(item) {
  const actionKind = String(item?.action_kind || "");
  const params = new URLSearchParams({
    floor: "1F",
    view: "2d",
    mode: actionKind === "open_inventory_move" ? "move" : "planning"
  });
  const locationId = Number(item?.location_id || 0);
  if (locationId > 0) params.set("location_id", String(locationId));
  const areaCode = String(item?.area_code || "").trim();
  if (areaCode) params.set("area_code", areaCode);
  const mapFeatureId = String(item?.map_feature_id || "").trim();
  if (mapFeatureId) params.set("map_feature_id", mapFeatureId);
  if (actionKind === "open_area_planning") params.set("edit", "area_policy");
  return `/warehouse.html?${params.toString()}`;
}

export function floor1CandidateBlockerDetail(item) {
  if (item?.action_kind !== "open_inventory_move") return "";
  const lots = Number(item?.live_lot_count || 0);
  const pallets = Number(item?.current_pallet_count || 0);
  return `待处理 ${lots} 个库存批次 · ${pallets} 个实体栈板`;
}
