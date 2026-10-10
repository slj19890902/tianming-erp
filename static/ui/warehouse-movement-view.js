(function(root){
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number=value=>Number.isFinite(Number(value))?Number(value).toLocaleString('zh-CN'):'—';
  const unit=value=>({pcs:'只',pieces:'片',sheets:'张',sets:'套',boxes:'只'}[value]||value||'');
  function row(item,time){
    const u=esc(unit(item.display_unit||item.unit));
    const delta=Number(item.physical_delta||0);
    const place=item.from_location&&item.to_location
      ?`${esc(item.from_location)}<br><b>→ ${esc(item.to_location)}</b>`
      :`<span class="muted">当前：</span>${esc(item.current_location||'位置待核')}`;
    const detail=`<details class="movement-detail"><summary>明细</summary><div>可用 ${number(item.before_available)} → ${number(item.after_available)} ${u}</div><div>订单占用 ${number(item.before_reserved)} → ${number(item.after_reserved)} ${u}</div><div>报损 ${number(item.before_damaged)} → ${number(item.after_damaged)} ${u}</div><div>${esc(item.quantity_scope)}</div><div>批次 ${esc(item.lot_number)}</div><div>记录 ${esc(item.movement_number)}</div></details>`;
    const sheet=item.inventory_type==='semi_finished';
    const sources=sheet
      ?(Array.isArray(item.source_products)?item.source_products:[item.source_product_code?{code:item.source_product_code,name:item.source_product_name}:null]).filter(source=>source&&source.code)
      :[];
    const sourceCodes=[...new Set(sources.map(source=>source.code))];
    const sourceNames=[...new Set(sources.map(source=>source.name).filter(Boolean))];
    const code=sheet
      ?(sources.length
        ?`<span class="muted">报料来源：</span><strong class="movement-code">${sourceCodes.map(esc).join(' / ')}</strong>`
        :`<strong class="movement-code">${item.inventory_code?`适用款号：${esc(item.inventory_code)}`:'未指定产品'}</strong>`)
      :`<strong class="movement-code">${esc(item.inventory_code||'未绑定编码')}</strong>`;
    const binding=sheet&&sources.length&&item.inventory_code
      ?`<div class="muted">适用款号：${esc(item.inventory_code)}</div>`:'';
    const sourceName=sheet&&sourceNames.length
      ?`<div>${sourceNames.map(esc).join(' / ')}</div>`:'';
    const productName=item.product_name&&!sourceNames.includes(item.product_name)
      ?`<div>${esc(item.product_name)}</div>`:'';
    return `<tr><td>${esc(time(item.created_at))}<br><span class="muted">${esc(item.operator_name)}</span></td><td><b>${esc(item.customer_name)}</b><br>${code}${sourceName}${productName}${binding}</td><td>${esc(item.operation_label)}${item.transfer_quantity!=null?`<div>${number(item.transfer_quantity)} ${u}</div>`:''}</td><td title="${esc(item.quantity_scope)}"><b>${number(item.before_physical)} → ${number(item.after_physical)} ${u}</b><div class="${delta<0?'movement-decrease':delta>0?'movement-increase':'muted'}">${delta===0?'实存不变':`${delta>0?'+':''}${number(delta)} ${u}`}</div></td><td>${place}</td><td>${esc(item.reason_display||'历史未记录')}${detail}</td></tr>`;
  }
  root.WarehouseMovementView={row,escape:esc};
})(typeof window==='undefined'?globalThis:window);
