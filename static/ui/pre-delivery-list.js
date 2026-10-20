/* Planning display and A4 output share the same quantities. No persistence. */
(function(root) {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const quantity = row => {
    const value = row.final_delivery_qty;
    return value !== '' && value != null && Number.isSafeInteger(Number(value)) && Number(value) >= 0 ? Number(value) : null;
  };
  function remaining(rows, row) {
    if (row.stock_on_hand_qty == null || !Number.isFinite(Number(row.stock_on_hand_qty))) return null;
    let used = 0;
    for (const current of rows) {
      if (current === row || (row.stock_pool_key && current.stock_pool_key === row.stock_pool_key)) {
        const number = quantity(current);
        if (number == null) return null;
        used += number;
      }
      if (current === row) return Number(row.stock_on_hand_qty) - used;
    }
    return null;
  }
  const number = value => value == null ? '—' : String(value);
  function render(data) {
    const rows = data.items || [], e = escape;
    return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>预送货拿货清单</title><style>
    *{box-sizing:border-box}body{margin:0;background:#eef1f4;color:#17212b;font:14px "Microsoft YaHei",sans-serif}
    nav{padding:16px;text-align:center}button{padding:10px 18px;margin:0 6px;cursor:pointer}main{background:white;max-width:186mm;margin:0 auto;padding:12mm}
    h1{text-align:center;font-size:22px;margin:0 0 14px}.meta{display:flex;justify-content:space-between;gap:12px;margin-bottom:12px;font-size:12px}
    table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{border:1px solid #555;padding:7px 6px;vertical-align:top;overflow-wrap:anywhere;white-space:pre-wrap}th{background:#f0f2f4}td.num{text-align:right;font-variant-numeric:tabular-nums}.note{font-size:11px;line-height:1.6;margin-top:12px}
    @page{size:A4 portrait;margin:12mm}@media print{nav{display:none}body{background:white}main{max-width:none;margin:0;padding:0}thead{display:table-header-group}tr{break-inside:avoid}th{print-color-adjust:exact}}
    </style></head><body><nav><button onclick="window.print()">打印 / 另存为 PDF（A4）</button><button onclick="window.close()">关闭</button></nav><main>
    <h1>预送货拿货清单</h1><div class="meta"><span>批次：${e(data.batchNumber || '')}</span><span>预送日期：${e(data.preDeliveryDate || '')}</span></div>
    <table><colgroup><col style="width:24%"><col style="width:19%"><col style="width:29%"><col style="width:12%"><col style="width:16%"></colgroup><thead><tr><th>图号</th><th>存货编码</th><th>位置</th><th>数量</th><th>剩余库存数量</th></tr></thead><tbody>
    ${rows.map(row => `<tr><td>${e(row.customer_drawing_number || '—')}</td><td>${e(row.stock_code || '—')}</td><td>${e([...new Set((row.stock_locations || []).map(loc => loc.location_name || loc.location_code).filter(Boolean))].join('\n') || '暂无成品位置')}</td><td class="num">${e(number(quantity(row)))}</td><td class="num">${e(number(remaining(rows, row)))}</td></tr>`).join('')}
    </tbody></table><p class="note">数量为本次填写的建议送货数量。剩余库存＝现存成品（含预占）－本清单计划数量；同一产品多行按清单顺序扣减，负数表示缺口。<br>本清单供提前备货，未扣减实物库存；实际发货仍以系统库存核验为准。</p></main></body></html>`;
  }
  const api = {quantity, remaining, render};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PreDeliveryList = api;
})(typeof window === 'undefined' ? globalThis : window);
