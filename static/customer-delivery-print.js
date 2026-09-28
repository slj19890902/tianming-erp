(function (global) {
  'use strict';
  const priceKeys = new Set(['unit_price', 'amount']);
  const numericKeys = new Set(['quantity', 'unit_price', 'amount']);
  function node(tag, text, cls) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = String(text ?? '');
    if (cls) el.className = cls;
    return el;
  }
  function visibleColumns(layout) {
    // Price mode changes values only; keep the customer's column layout intact.
    return layout.columns;
  }
  function decimal(value, places) {
    // Values are decimal strings from the server; BigInt avoids binary rounding.
    const match = String(value ?? '').match(/^(\d+)(?:\.(\d+))?$/);
    if (!match) return String(value ?? '');
    const fraction = (match[2] || '').padEnd(places + 1, '0');
    let scaled = BigInt(match[1] + fraction.slice(0, places));
    if (Number(fraction[places]) >= 5) scaled += 1n;
    const result = scaled.toString().padStart(places + 1, '0');
    return places ? result.slice(0, -places) + '.' + result.slice(-places) : result;
  }
  function valuesFor(row, columns, layout, shown) {
    return columns.map(col => {
      if (priceKeys.has(col.key) && !shown) return '';
      const value = row[col.key];
      return value === undefined || value === null ? '' : priceKeys.has(col.key)
        ? decimal(value, col.key === 'unit_price' ? layout.price_decimals : layout.amount_decimals)
        : col.key === 'specification' ? String(value).replace(/(?:mm|毫米)/gi, '').trim() : String(value);
    });
  }
  function makeSheet(data, columns, entries, pageNumber, pageCount) {
    const layout = data.print_template.layout;
    const sheet = node('main', undefined, 'cd-sheet');
    sheet.style.setProperty('--cd-font', `${layout.font_size_pt}pt`);
    const body = node('div', undefined, 'cd-body'); sheet.append(body);
    const header = node('header', undefined, 'cd-header');
    header.append(node('h1', data.sender.company_name), node('p', [data.sender.address, data.sender.phone].filter(Boolean).join('　')),
      node('h2', data.order_context ? `送货单（${data.order_context}）` : '送货单'));
    body.append(header);
    const meta = node('section', undefined, 'cd-meta');
    [`客户名称：${data.customer.name || ''}`, `送货日期：${String(data.delivery_date || '').slice(0,10)}`,
      `交货地点：${data.customer.address || ''}`, `送货单号：${data.delivery_number || ''}`,
      `联系人：${data.customer.contact_person || ''}　电话：${data.customer.phone || ''}`, `车牌号码：${data.vehicle_number || ''}`]
      .forEach(text => meta.append(node('div', text)));
    body.append(meta);
    const table = node('table', undefined, 'cd-table'), group = node('colgroup');
    const width = columns.reduce((sum, c) => sum + c.width, 0);
    columns.forEach(col => { const el = node('col'); el.style.width = `${100 * col.width / width}%`; group.append(el); });
    table.append(group);
    if (layout.show_headers) {
      const head = node('thead'), tr = node('tr');
      columns.forEach(col => {
        const label = col.key === 'specification'
          ? `${col.label.replace(/\s*[（(]?\s*(?:mm|毫米)\s*[）)]?/gi, '').trim()}（mm）` : col.label;
        tr.append(node('th', label));
      }); head.append(tr); table.append(head);
    }
    const tbody = node('tbody');
    entries.forEach(entry => {
      const tr = node('tr');
      if (entry.note) {
        tr.className = 'cd-note'; const td = node('td', entry.values[0]); td.colSpan = columns.length; tr.append(td);
      } else {
        columns.forEach((col, index) => {
          const td = node('td', entry.values[index], numericKeys.has(col.key) ? 'cd-numeric' : '');
          if (col.key === 'customer_po') {
            td.className = 'customer-order-cell';
            td.replaceChildren(node('span', entry.values[index], 'customer-order-text'));
          }
          if (entry.continued && index === 0) td.prepend(node('div', `第${entry.row.sequence}行续`, 'cd-continuation'));
          tr.append(td);
        });
      }
      tbody.append(tr);
    });
    table.append(tbody); body.append(table);
    const footer = node('footer', undefined, 'cd-footer');
    const pageQuantities = {};
    entries.filter(e => !e.continued && !e.note && e.row.pricing_included).forEach(e => {
      const unit=e.row.unit||'';pageQuantities[unit]=(pageQuantities[unit]||0)+Number(e.row.quantity||0);
    });
    const quantityText = quantities => Object.entries(quantities).map(([unit,quantity])=>`${quantity}${unit}`).join('、') || '0';
    footer.append(node('div', `本页数量：${quantityText(pageQuantities)}${pageNumber === pageCount ? `　整单数量：${quantityText(data.commercial_quantities || {'':data.commercial_quantity})}` : ''}`, 'cd-totals'));
    if (pageNumber === pageCount) {
      const total = node('div', data.price_display.shown
        ? `金额合计：${decimal(data.total_amount, layout.amount_decimals)} 元` : '金额合计：', 'cd-totals');
      // Reserve the same footer height without placing hidden amounts in the DOM.
      if (!data.price_display.shown) total.style.visibility = 'hidden';
      footer.append(total);
    }
    const sign = node('div', undefined, 'cd-sign');
    sign.append(node('span', '送货人：'), node('span', '收货单位（签章）：________________'));
    footer.append(sign, node('div', `${pageNumber}/${pageCount}页　白联存档　红联客户　黄联回单`, 'cd-page'));
    body.append(footer); return sheet;
  }
  function checkWidth(sheet) {
    if (typeof global.fitCustomerOrderNumbers === 'function') global.fitCustomerOrderNumbers(sheet);
    const body = sheet.querySelector('.cd-body,.print-safe-area');
    const bounds = body.getBoundingClientRect(), paper = sheet.getBoundingClientRect();
    const children = [body, ...body.querySelectorAll('header,section,table,th,td,footer,.cd-sign,.cd-page')];
    if (bounds.left < paper.left - 1 || bounds.right > paper.right + 1 || children.some(el => {
      if (!el.getClientRects().length) return false;
      const rect = el.getBoundingClientRect();
      return rect.left < bounds.left - 1 || rect.right > bounds.right + 1 || el.scrollWidth > el.clientWidth + 2;
    })) throw new Error('送货单内容超出正文安全宽度，请核对打印档案的正文宽度、偏移和模板列宽。');
  }
  function validate(container) {
    container.querySelectorAll('.cd-sheet,.sheet').forEach(sheet => {
      checkWidth(sheet);
      const body = sheet.querySelector('.cd-body,.print-safe-area');
      const paper = sheet.getBoundingClientRect(), bounds = body.getBoundingClientRect();
      if (body.scrollHeight > body.clientHeight + 1 || bounds.top < paper.top - 1 || bounds.bottom > paper.bottom + 1)
        throw new Error('送货单内容超出纸张高度，请核对纸型、上下偏移或模板字号。');
    });
  }
  function paginate(data, columns, container) {
    const pages = []; let current = [];
    const fits = entries => {
      const probe = makeSheet(data, columns, entries, 999, 999); container.append(probe);
      const body = probe.firstElementChild;
      try {
        checkWidth(probe);
        return body.scrollHeight <= body.clientHeight + 1;
      } finally { probe.remove(); }
    };
    if (!fits([])) throw new Error('抬头内容超过当前打印纸可用高度，请在模板中调整字号或联系管理员核对抬头。');
    function add(entry) {
      if (fits([...current, entry])) { current.push(entry); return; }
      if (current.length) { pages.push(current); current = []; }
      if (fits([entry])) { current.push(entry); return; }
      // Split a single oversized row by measured height, preserving every character.
      let remaining = entry.values.map(text => Array.from(text)), continued = !!entry.continued;
      while (remaining.some(chars => chars.length)) {
        const maxLength = Math.max(...remaining.map(chars => chars.length));
        let low = 1, high = maxLength, best = 0;
        const part = count => ({ ...entry, continued, values: remaining.map(chars => chars.slice(0,count).join('')) });
        while (low <= high) {
          const middle = Math.floor((low + high) / 2);
          if (fits([part(middle)])) { best = middle; low = middle + 1; } else high = middle - 1;
        }
        if (!best) throw new Error('当前模板没有可用明细行高度，请调整抬头或正文设置。');
        const fragment = part(best);
        remaining = remaining.map(chars => chars.slice(best));
        if (remaining.some(chars => chars.length)) pages.push([fragment]);
        else current = [fragment];
        continued = true;
      }
    }
    data.customer_document_rows.forEach(row => {
      add({ row, values: valuesFor(row, columns, data.print_template.layout, data.price_display.shown), continued: false });
      if (data.print_template.layout.show_remarks && row.remarks && !columns.some(col => col.key === 'remarks'))
        add({row, values:[`第${row.sequence}行备注：${row.remarks}`], note:true});
    });
    if (current.length || !pages.length) pages.push(current);
    return pages;
  }
  function render(data, container) {
    const columns = visibleColumns(data.print_template.layout, data.price_display.shown);
    container.hidden = false; container.style.visibility = 'hidden'; container.replaceChildren();
    try {
      const pages = paginate(data, columns, container);
      container.replaceChildren(...pages.map((entries,i) => makeSheet(data, columns, entries, i+1, pages.length)));
      validate(container);
      return pages.length;
    } finally { container.style.visibility = 'visible'; }
  }
  function controls(data, reload) {
    global.customerPrintData = data;
    document.getElementById('customerPrintControls')?.remove();
    document.getElementById('customerPrintWarnings')?.remove();
    if (data.print_template?.layout?.catalog_version !== 'delivery-print-v2') return;
    const bar = node('span', undefined, 'cd-print-controls'); bar.id = 'customerPrintControls';
    const select = node('select'); select.setAttribute('aria-label', '价格显示');
    [['false','不显示价格'],['true','显示价格']].forEach(([value,label]) => {
      const option = node('option', label); option.value = value;
      if (value === 'true' && !data.price_display.allowed) option.disabled = true;
      select.append(option);
    }); select.value = String(data.price_display.shown);
    const change = (key, value) => { const url = new URL(location.href); url.searchParams.set(key,value); history.replaceState(null,'',url); reload(); };
    select.onchange = () => change('show_prices',select.value); bar.append(select);
    if (data.print_template.layout.preset === 'yke') {
      const context = node('select'); context.setAttribute('aria-label','送货场景');
      [['','普通订单'],['海外订单','海外订单']].forEach(([value,label]) => {const o=node('option',label);o.value=value;context.append(o);});
      context.value=data.order_context; context.onchange=()=>change('order_context',context.value);bar.append(context);
    }
    document.querySelector('.toolbar').append(bar);
    const warnings = [...(data.document_warnings || [])];
    if (!data.price_display.shown) warnings.push('无价版已隐藏单价和所有金额，请同时核对手工备注中是否包含价格文字。');
    if (warnings.length) {const warning=node('div',warnings.join('；'),'cd-warning no-print');warning.id='customerPrintWarnings';document.querySelector('.toolbar').after(warning);}
  }
  const pendingPrints = new Map();
  function recordPrint() {
    const data = global.customerPrintData;
    if (data?.print_template?.layout?.catalog_version !== 'delivery-print-v2') return;
    const identity = JSON.stringify([data.id, data.document_hash, data.price_display.shown, data.order_context]);
    let attempt = pendingPrints.get(identity);
    if (!attempt) {
      attempt = {body: {idempotency_key:global.TmOperationKey.create(), document_hash:data.document_hash,
        show_prices:data.price_display.shown, order_context:data.order_context}, promise:null};
      pendingPrints.set(identity, attempt);
    }
    if (attempt.promise) return attempt.promise;
    // A lost response may already have committed. Retry that exact request/key;
    // a different document or price mode must never reuse its operation key.
    attempt.promise = (async () => {
      const response = await fetch(`/api/deliveries/${data.id}/customer-print-events`, {
        method:'POST', credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(attempt.body)});
      if (!response.ok) {const error=await response.json().catch(()=>({}));throw new Error(error.detail||`打印登记失败 HTTP ${response.status}`);}
      pendingPrints.delete(identity);
    })().finally(() => { attempt.promise = null; });
    return attempt.promise;
  }
  global.CustomerDeliveryPrint = { render, controls, visibleColumns, decimal, paginate, recordPrint, validate, checkWidth };
  if (typeof module !== 'undefined') module.exports = global.CustomerDeliveryPrint;
})(typeof window === 'undefined' ? globalThis : window);
