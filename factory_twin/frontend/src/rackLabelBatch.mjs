// Use one authorized, published location per rack; never group racks by display name.
export function areaRackLabelBatch(racks, locations, floorCode) {
  const ordered=[...racks].sort((a,b)=>a.x_mm-b.x_mm || b.y_mm-a.y_mm || a.id.localeCompare(b.id));
  const ids=[], missing=[];
  for (const rack of ordered) {
    const candidates=locations.filter(l=>l.is_active && l.floor_code===floorCode && l.map_rack_id===rack.id && Number.isInteger(l.location_id) && l.location_id>0);
    candidates.sort((a,b)=>(a.level_no||0)-(b.level_no||0)||(a.slot_no||0)-(b.slot_no||0)||a.location_id-b.location_id);
    if(!candidates.length)missing.push(rack.name||rack.id);else ids.push(candidates[0].location_id);
  }
  const error=missing.length?`以下货架没有可打印正式货位：${missing.join('、')}`:ids.length>500?'区域货架超过500个，请分区打印':!ids.length?'本区域没有可打印货架':'';
  return {count:ids.length,error,url:error?'':`/static/shelf-label.html?content=rack&location_ids=${ids.join(',')}`};
}
