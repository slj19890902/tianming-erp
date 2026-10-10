(function(root) {
  'use strict';
  const columns = ['客户简称','存货编码','客户类别','订单成品数量','模具位置','产品尺寸 / 组件','压线尺寸 / 模具报料尺寸、模数','报料宽','报料长','报料数量','材质楞型','备注'];
  const show = value => value === null || value === undefined || value === '' ? '待核对' : String(value);
  const esc = value => show(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  function rows(cards, customerSafe=false) {
    const result = [], picked = new Set();
    for (const card of cards || []) {
      if (card.paper_phase === 'actual_receipt') throw new Error('此任务有分批或实收差异，请使用完整生产任务单核对实收版本，不能套用计划报料条单');
      for (const c of card.components || []) {
      const snap = c.sheet_cutting_snapshot, die = snap ? snap.is_die_cut : c.needs_die_cut || c.layout_kind==='die_cut';
      const count = c.strip_finished_quantity ?? c.order_set_quantity ?? null;
      const replenishment = card.source_type==='stock_replenishment' || card.stock_replenishment_item_id;
      const quantity = count === null ? '成品数量待核对' : `${count}${c.order_set_quantity ? '套' : c.finished_unit || ''}${replenishment ? '（补库）' : ''}`;
      const crease = die ? (snap ? `${snap.theoretical_width_mm}×${snap.theoretical_length_mm} mm / ${snap.mold_count}模` : '模具尺寸 / 模数待核对') : c.crease_display || '未维护';
      const common = [card.customer_name, c.product_code || card.product_code, c.customer_category ?? card.customer_category,
        quantity, customerSafe ? '内部资料' : c.mold_location_display || c.mold_location || (die ? '待核对' : '—'),
        [c.component_label, c.specification || (card.specifications || []).join(' / ')].filter(Boolean).join('：'), crease];
      const notes = [replenishment ? '补库' : '', ...(card.review_messages || []), c.cutting_work_instruction || ''].filter(Boolean);
      if (Number(c.requisition_quantity)>0) result.push([...common,
        snap?.supplier_width_mm ?? c.report_width_mm, snap?.supplier_length_mm ?? c.report_length_mm,
        `${c.requisition_quantity} ${c.requisition_unit || '张'}`, [c.material_code,c.flute_type].filter(Boolean).join(' / '),
        ['订纸',...notes].join('；')]);
      for (const pick of c.inventory_pick_lines || []) {
        if (pick.kind==='finished' || !(pick.quantity>0) || picked.has(pick.reservation_id)) continue;
        picked.add(pick.reservation_id);
        result.push([...common, pick.report_width_mm, pick.report_length_mm, `${pick.quantity} ${pick.unit || '单位待核对'}`,
          [pick.material_code,pick.flute_type].filter(Boolean).join(' / '),
          `库存材料做；${customerSafe ? '内部领料位置' : pick.location_name || '位置待核对'}；批次 ${pick.lot_number || '待核对'}${pick.location_issue ? '；'+pick.location_issue : ''}`]);
      }
      }
    }
    return result;
  }
  function render(host, cards, customerSafe=false) {
    const data = rows(cards,customerSafe), pages=[];
    for (let i=0;i<data.length;i+=5) pages.push(data.slice(i,i+5));
    host.innerHTML = pages.map((page,index)=>`<section class="material-strip-page"><h1>简易报料生产任务单</h1><p>每行一个报料尺寸 / 来源 · 单位 mm · 同款成品数量为同一任务参考，不跨行相加 · 第 ${index+1}/${pages.length} 页</p><table><colgroup>${[7,9,5,8,11,13,12,5,5,7,8,10].map(w=>`<col style="width:${w}%">`).join('')}</colgroup><thead><tr>${columns.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${page.map(row=>`<tr>${row.map(c=>`<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></section>`).join('');
    return pages;
  }
  root.MaterialStripPrint = {rows,render,columns};
  if(typeof module!=='undefined') module.exports=root.MaterialStripPrint;
})(typeof window!=='undefined'?window:globalThis);
