// Resolve the actual pallet owning this lot; never use the location's first pallet.
export function unlocatedPalletChoice(item, locations) {
  const location = locations.find(row => row.location_id === item.location_id);
  if (!location || !item.lot_id) return { error: "来源库存位置已变化，请刷新核对。" };
  const pallets = location.pallets?.length ? location.pallets : location.pallet ? [location.pallet] : [];
  const matches = pallets.filter(pallet => pallet.items?.some(row => row.lot_id === item.lot_id));
  if (matches.length !== 1) return { error: "未找到唯一的当前栈板，请管理员核对库存位置。" };
  const pallet = matches[0];
  if (!pallet.pallet_id || !pallet.version || pallet.move_eligible !== true) {
    return { error: pallet.move_block_reason || "当前栈板不符合移货条件，请核对库存。" };
  }
  return { location, pallet };
}
