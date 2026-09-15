(function (root) {
  'use strict';
  const value = x => typeof x === 'string' || typeof x === 'number' ? String(x).trim() : '';
  function parts(row) {
    row = row || {};
    return {
      customer: value(row.customer_po) || value(row.customer_order_number) || value(row.customer_order_no),
      erp: value(row.order_number) || value(row.order_no) || value(row.order_number_snapshot) || value(row.item_order_number),
    };
  }
  const escape = x => String(x).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function html(row, options) {
    const p = parts(row), secondary = options?.secondary !== false;
    return `<span class="order-reference"><span class="${p.customer ? 'customer-po' : 'customer-po-missing'}">${escape(p.customer || '未填写客户单号')}</span>${secondary && p.erp && p.erp !== p.customer ? `<small class="erp-order-no">ERP ${escape(p.erp)}</small>` : ''}</span>`;
  }
  function node(row) {
    const container = document.createElement('span');
    container.innerHTML = html(row);
    return container.firstElementChild;
  }
  const component = {
    props:['row','secondary'], computed:{reference(){return parts(this.row);}},
    template:`<span class="order-reference"><span :class="reference.customer ? 'customer-po' : 'customer-po-missing'">{{ reference.customer || '未填写客户单号' }}</span><small v-if="secondary !== false && reference.erp && reference.erp !== reference.customer" class="erp-order-no">ERP {{ reference.erp }}</small></span>`,
  };
  root.TMOrderReference = {parts,html,node,component};
})(globalThis);
