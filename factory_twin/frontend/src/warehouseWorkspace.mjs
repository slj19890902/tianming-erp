export function moveLocationState(location, source, eligibleIds, targetId) {
  const blocked = reason => ({kind:'blocked',selectable:false,label:'不可选',reason});
  if (!location?.location_id) return blocked('货位身份不完整，请刷新核对');
  if (location.location_id === source?.source_location_id) return {kind:'source',selectable:false,label:'来源',reason:'来源与目标不能相同'};
  if (source?.operation === 'pallet_move' && (location.occupancy_status !== 'empty' || location.map_rack_id || location.address_kind === 'rack_slot'))
    return blocked('整栈板只能移到空的地面货位；移入货架或有货位请选单个产品');
  if (!eligibleIds.includes(location.location_id)) return blocked('当前货位不满足此次移货条件，请核对容量、状态或刷新重选');
  const occupied=location.occupancy_status !== 'empty';
  return {kind:location.location_id===Number(targetId)?'target':occupied?'occupied':'empty',selectable:true,label:occupied?'有货':'空位',reason:''};
}
export function areaSortKey(name) {
  const text=String(name||'');
  const match=text.match(/([A-Z]+)\s*(\d+)?/i);
  return match ? `${match[1].toUpperCase()}${match[2]||'0'} ${text}` : `ZZZ ${text}`;
}
