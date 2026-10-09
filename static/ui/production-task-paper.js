/* Presentation only: source cards and their concurrency fingerprints stay intact. */
(function (root) {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const uniq = values => [...new Set(values.filter(v => v !== null && v !== undefined && String(v).trim()))];
  const num = value => Number(value || 0).toLocaleString('zh-CN');
  const size = (length, width) => length && width ? `${length}×${width} mm` : '尺寸待核对';
  const noPrint = value => !value || /^(无|否|无印刷|无需印刷|不印刷|不印)$/.test(String(value).trim());

  function groups(cards) {
    const result = [], lookup = new Map();
    for (const [index, card] of cards.entries()) {
      const parts = card.components || [];
      const specs = uniq(parts.map(c => JSON.stringify([c.report_length_mm, c.report_width_mm, c.material_code, c.flute_type, c.layer_count, c.crease_display])));
      // Actual receipts retain individual batch/location facts, even if sizes match.
      const canMerge = !!card.paper_group_id && specs.length === 1 && card.paper_phase !== 'actual_receipt';
      const key = canMerge ? JSON.stringify([card.paper_group_id, card.source_type || 'order', card.customer_id, card.customer_name, card.paper_phase, specs[0]]) : `single:${index}`;
      let group = lookup.get(key);
      if (!group) { group = {cards:[], key}; lookup.set(key, group); result.push(group); }
      group.cards.push(card);
    }
    return result;
  }

  function operations(c, card) {
    const result = [], add = (label, values, internal = false) => {
      const text = uniq(values).join(' · ');
      if (text) result.push({label, text, internal});
    };
    const snap = c.sheet_cutting_snapshot;
    const route = c.production_route || {};
    const steps = (route.steps || []).map(s => s.code);
    if (snap && Number(snap.cutting_factor) > 1 && !route.already_cut) {
      add('分切', [`长${snap.length_parts}×宽${snap.width_parts}`, `每片 ${size(snap.theoretical_length_mm, snap.theoretical_width_mm)}`]);
    } else if (!snap && c.cutting_mode && !['一开一','一开1'].includes(c.cutting_mode)) {
      add('开料', [c.cutting_mode, '按原工艺核对分切 / 模数']);
    }
    const die = snap ? snap.is_die_cut : c.needs_die_cut || steps.includes('die_cutting') || c.layout_kind === 'die_cut';
    if (die) {
      add('模切', [snap ? `${snap.mold_count}模` : '模数待核对', c.mold_code || c.mold_name || '模具待核对'], true);
      add('模具位置', [c.mold_location_display || c.mold_location || '位置待核对', c.mold_is_active === false ? '模具已停用' : null], true);
    }
    if (c.box_type_code === 'a1_0201' || card.box_type_code === 'a1_0201' || steps.includes('slotting')) {
      add('开槽', [`压线 ${c.crease_display || '待核对'}`]);
    } else if (steps.includes('creasing')) add('压线', [c.crease_display || '待核对']);
    const colors = (c.printing_colors || []).filter(v => !noPrint(v));
    const content = c.print_content || c.printing_instruction;
    if (colors.length || !noPrint(content) || !noPrint(c.printing_situation)) {
      add('印刷', [...colors, !noPrint(content) ? content : c.printing_situation]);
      for (const p of c.printing_plates || []) add('印版', [p.color_name, p.plate_code, p.plate_name, p.current_location], true);
      const setup = [c.plate_alignment_value_mm != null ? `对版 ${c.plate_alignment_value_mm} mm` : null,
        c.plate_mount_value_mm != null ? `挂版 ${c.plate_mount_value_mm} mm` : null];
      if (c.machine_set_length_mm || c.machine_set_width_mm || c.machine_set_height_mm)
        setup.push(`机设 ${[c.machine_set_length_mm,c.machine_set_width_mm,c.machine_set_height_mm].map(v=>v ?? '待核对').join('×')} mm`);
      add('印刷设定', setup, true);
    }
    const joining = c.joining_method || card.joining_method || '';
    if (/钉/.test(joining)) add('结合', ['打钉']);
    if (/粘|贴/.test(joining)) add('结合', ['粘贴']);
    for (const s of route.steps || []) if (['clearing','corner_cutting','punching','folding','laminating','binding'].includes(s.code)) add(s.label, ['按工艺执行']);
    return result;
  }

  function atoms(group) {
    const rows = [], blocks = [], reservations = new Set();
    const multiple = group.cards.reduce((n,c) => n + (c.components || []).length, 0) > 1;
    for (const card of group.cards) {
      for (const c of card.components || [card]) {
        const code = c.product_code || card.product_code || '编码待核对';
        const unit = c.finished_unit || card.output_unit || '只';
        const label = c.component_label && !['整片','整箱','整体','本体'].includes(c.component_label) ? `（${c.component_label}）` : '';
        const name = `${c.product_name || card.product_name || ''}${label}`;
        const quantities = [c.customer_order_quantity ?? card.customer_order_quantity,
          c.finished_deduction_quantity ?? card.stock_deduction_quantity,
          c.planned_finished_quantity ?? card.planned_finished_quantity].map(v=>Number(v || 0));
        const rowKey = JSON.stringify([code, name, unit]);
        const previousRow = rows.find(row => row.rowKey === rowKey);
        if (previousRow) {
          previousRow.quantities = previousRow.quantities.map((v,i)=>v+quantities[i]);
          previousRow.cells = [code,name,...previousRow.quantities.map(v=>`${num(v)}${unit}`)];
        } else rows.push({kind:'row',rowKey,quantities,cells:[code,name,...quantities.map(v=>`${num(v)}${unit}`)]});
        if (c.quantity_per_set) blocks.push({kind:'block', label:code, text:`每套 ${c.quantity_per_set}片 · ${num(c.order_set_quantity)}套`, internal:false});
        const prefix = multiple ? `${code}${label} ` : '';
        for (const op of operations(c, card)) blocks.push({kind:'block', ...op, label:prefix+op.label});
        let finishedLocations = false;
        for (const pick of c.inventory_pick_lines || []) {
          if (pick.kind === 'finished') finishedLocations = true;
          if (reservations.has(pick.reservation_id)) continue;
          reservations.add(pick.reservation_id);
          const label = `${code} ${pick.kind === 'finished' ? '成品抵扣' : '库存领料'}`;
          if (pick.quantity > 0) blocks.push({kind:'block', label, text:`${num(pick.quantity)}${pick.unit || '片'} · ${pick.location_name || '位置待核对'}${pick.location_issue ? '（位置需核对）' : ''}`, internal:true});
          if (pick.consumed_quantity > 0) blocks.push({kind:'block', label, text:`已领用 ${num(pick.consumed_quantity)}${pick.unit || '片'}`, internal:true});
        }
        if (!finishedLocations && Number(c.finished_deduction_quantity ?? card.stock_deduction_quantity) > 0)
          blocks.push({kind:'block', label:code+' 成品位置', text:'待核对当前抵扣批次', internal:true});
      }
      if (card.paper_phase === 'actual_receipt') blocks.push({kind:'block', label:'本批实收', text:`${num(card.received_sheet_quantity)}张 · 最多生产 ${num(card.production_capacity_quantity)}${card.output_unit || '只'} · ${card.employee_location_name || card.current_address_name || card.warehouse_location || '位置待核对'} · ${card.inventory_lot_number || card.receipt_number || ''}`, internal:true});
      for (const r of card.fulfillment_reminders || []) blocks.push({kind:'block', label:'交付要求', text:r.content, internal:true});
      for (const warning of card.review_messages || []) blocks.push({kind:'block', label:'请核对', text:warning, internal:true});
      // Free-form special instructions are retained in full, apart from redundant generated cutting text.
      const notes = uniq((card.components || []).flatMap(c => c.production_notes || []).concat(card.production_steps || []));
      for (const rawNote of notes) {
        const note = String(rawNote).replace(/无需结合[，、；; /]*/g, '').trim();
        if (!note) continue;
        if (['无需结合','无印刷','不印刷','一开一','打钉','粘贴','模切','开槽','印刷'].includes(note) || (card.components || []).some(c => c.cutting_work_instruction === note)) continue;
        blocks.push({kind:'block', label:'工艺要求', text:note, internal:false});
      }
    }
    const seen = new Set();
    return rows.concat(blocks.filter(b => { const key = JSON.stringify(b); if (seen.has(key)) return false; seen.add(key); return true; }));
  }

  function header(group) {
    const cards = group.cards, first = cards[0];
    const orders = uniq(cards.flatMap(c => (c.customer_pos || []).length ? c.customer_pos : c.order_numbers || []));
    const sizes = uniq(cards.flatMap(c => (c.components || []).map(p => size(p.report_length_mm, p.report_width_mm))));
    const materials = uniq(cards.flatMap(c => (c.components || []).map(p => [p.material_code,p.flute_type].filter(Boolean).join(' / '))));
    const qty = cards.reduce((n,c) => n+Number(c.requisition_quantity || 0), 0);
    const qr = cards.length === 1 && first.product_qr?.qr_data_url ? `<img src="${esc(first.product_qr.qr_data_url)}" alt="扫码查看当前产品资料">` : '';
    const title = first.source_type === 'stock_replenishment' ? '补库任务单' : first.paper_phase === 'actual_receipt' ? '生产任务单 · 实收' : '生产任务单';
    return `<header class="paper-head"><div><div class="paper-title">${title} <span data-part></span></div><h1>${esc(first.customer_name || '客户待核对')}</h1><div class="paper-orders">订单 ${esc(orders.join('、') || '待核对')}</div></div>${qr}</header><div class="paper-board"><strong>纸板 ${esc(sizes.join(' / '))}</strong><b>${num(qty)}张</b><span>${esc(materials.join('、'))}</span></div>`;
  }
  function body(atoms) {
    let html = '', inTable = false;
    for (const a of atoms) {
      if (a.kind === 'row') {
        if (!inTable) { html += '<table class="paper-products"><colgroup><col style="width:23%"><col style="width:32%"><col style="width:15%"><col style="width:15%"><col style="width:15%"></colgroup><thead><tr><th>存货编码</th><th>产品名称</th><th>订单数</th><th>成品抵扣</th><th>需生产数</th></tr></thead><tbody>'; inTable = true; }
        html += `<tr>${a.cells.map(v => `<td>${esc(v)}</td>`).join('')}</tr>`;
      } else {
        if (inTable) { html += '</tbody></table>'; inTable = false; }
        html += `<div class="paper-operation${a.internal ? ' internal-only' : ''}"><b>${esc(a.label)}</b><span>${esc(a.text)}</span></div>`;
      }
    }
    return html + (inTable ? '</tbody></table>' : '');
  }
  function render(host, cards) {
    host.innerHTML = '';
    const measurePage = document.createElement('article'); measurePage.className = 'page batch-page paper-v2';
    const measure = document.createElement('section'); measure.className = 'task-card paper-half';
    measurePage.append(measure); host.append(measurePage);
    const pages = []; let pending = null;
    for (const group of groups(cards)) {
      const head = header(group), sections = []; let content = [];
      const footer = `<footer class="paper-foot"><span>${esc(uniq(group.cards.map(c => c.supplier_order_number)).join('、'))}</span><span class="paper-page-index"></span></footer>`;
      const set = values => { measure.innerHTML = head + body(values) + footer; };
      for (const atom of atoms(group)) {
        set([...content, atom]);
        if (measure.scrollHeight > measure.clientHeight + 1 || measure.scrollWidth > measure.clientWidth + 1) {
          if (content.length) { sections.push(content); content = []; set([atom]); }
          if (measure.scrollHeight > measure.clientHeight + 1 || measure.scrollWidth > measure.clientWidth + 1)
            throw new Error(`任务内容过长：${atom.label || atom.cells?.[0] || '客户 / 订单'}，请精简该项后打印`);
        }
        content.push(atom);
      }
      if (content.length) sections.push(content);
      const htmls = sections.map((values, i) => `<section class="task-card paper-half">${head.replace('data-part>', `data-part>${sections.length>1 ? `${i+1}/${sections.length}${i ? ' 续页' : ''}` : ''}`)}${body(values)}${footer.replace('class="paper-page-index">', `class="paper-page-index">${i+1}/${sections.length}`)}</section>`);
      if (htmls.length > 1) {
        if (pending) { pages.push([pending]); pending = null; }
        for (let i=0;i<htmls.length;i+=2) pages.push(htmls.slice(i,i+2));
      } else if (htmls.length) {
        if (pending) { pages.push([pending,htmls[0]]); pending = null; } else pending=htmls[0];
      }
    }
    if (pending) pages.push([pending]);
    host.innerHTML = pages.map(p => `<article class="page batch-page paper-v2">${p.join('')}${p.length===1 ? '<section class="task-card paper-half blank"></section>' : ''}</article>`).join('');
    return pages;
  }
  const api = {groups, operations, atoms, header, body, render};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.ProductionTaskPaper = api;
})(typeof window !== 'undefined' ? window : globalThis);
