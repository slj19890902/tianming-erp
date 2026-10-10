(function(global){
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const validId=value=>Number.isSafeInteger(Number(value))&&Number(value)>0;
  function dimensions(raw){
    const match=String(raw||'').trim().match(/^(\d+(?:\.\d+)?)\s*[×xX*＊]\s*(\d+(?:\.\d+)?)(?:\s*[×xX*＊]\s*(\d+(?:\.\d+)?))?(?:\s*(?:mm|毫米))?$/i);
    if(!match)return null;
    const length=Number(match[1]),width=Number(match[2]),height=match[3]?Number(match[3]):null;
    return length>0&&width>0&&length<=100000&&width<=100000&&(height===null||height>0&&height<=100000)?{length:String(length),width:String(width),...(height===null?{}:{height:String(height)})}:null;
  }
  function locationHref(row){
    if(!validId(row.location_id)||!['1F','3F','4F'].includes(String(row.floor_code||'')))return null;
    const query=new URLSearchParams({embedded:'1',tab:'map',view:'2d',mode:'lookup',readonly:'1',floor:String(row.floor_code),location_id:String(Number(row.location_id))});
    return '/warehouse.html?'+query;
  }
  function locations(body){
    if(!body||!Array.isArray(body.items)||!Number.isSafeInteger(body.total)||body.total<0||!Number.isSafeInteger(body.page)||body.page<1||typeof body.has_more!=='boolean')throw Error('货位查询回执不完整，请重试');
    return body.items.filter(row=>row&&validId(row.location_id)).map(row=>({
      id:Number(row.location_id),code:String(row.location_code||''),name:String(row.employee_location_name||row.location_name||row.short_location_label||'位置待核'),
      floor:String(row.floor_code||''),area:String(row.area_code||''),status:String(row.position_status||''),issue:String(row.map_issue||''),
      binding:row.default_binding&&typeof row.default_binding==='object'?row.default_binding:null,
      bindingVisibility:String(row.default_binding_visibility||''),href:locationHref(row)
    }));
  }
  global.TmMobileFieldSearch={dimensions,locations,locationHref,esc};
})(window);
