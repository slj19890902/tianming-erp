(function (root) {
  'use strict';
  function groups(rows, keyField='receipt_group_key') {
    const byKey = new Map();
    for (const row of rows || []) {
      const key = row[keyField] || String(row.stable_id || row.item_id || row.receipt_item_id || row.history_key);
      if (!byKey.has(key)) byKey.set(key, {key, rows:[], first:row});
      byKey.get(key).rows.push(row);
    }
    return [...byKey.values()];
  }
  function distribute(rows, quantity) {
    if (!Number.isSafeInteger(quantity) || quantity < 1) throw new Error('实收总数必须是大于 0 的整数');
    const weights = rows.map(row => Math.max(0, Number(row.remaining_quantity ?? row.incoming_quantity ?? 0)));
    const total = weights.reduce((a,b) => a+b, 0);
    if (!Number.isSafeInteger(total) || total < 1) throw new Error('待收数量已变化，请刷新后重试');
    // BigInt keeps a large integer receipt total exact, including its remainder.
    const allocations = weights.map((weight, index) => {
      const numerator = BigInt(quantity) * BigInt(weight);
      return {index, count:Number(numerator/BigInt(total)), remainder:numerator%BigInt(total)};
    });
    let extra = quantity - allocations.reduce((sum,row) => sum+row.count, 0);
    const order = [...allocations].sort((a,b) => a.remainder === b.remainder ? a.index-b.index : a.remainder>b.remainder ? -1:1);
    for (const row of order) if (extra-- > 0) row.count++;
    return allocations.map(row => row.count);
  }
  const methods = {
    reportedDisplayGroups() { return groups(this.requisitionItems, 'purchase_group_key'); },
    reportedGroupSelected(group) { return group.rows.every(row => this.reportedItemSelected(row)); },
    selectReportedGroup(group, checked) { group.rows.forEach(row => this.toggleReportedItemSelection(row,checked)); },
    bomDraftGroups(group) {
      const result=new Map();
      for (const [index,row] of (group.bom_lines || []).entries()) {
        const key=row.cardboard_len && row.cardboard_width && row.material_display ? JSON.stringify([
          row.customer_id || row.customer_name,row.material_id || row.material_display,row.flute_type,
          row.cardboard_len,row.cardboard_width,row.crease_type,row.crease_left_mm,row.crease_middle_mm,row.crease_right_mm,row.supplier_edge_type
        ]) : 'single:'+index;
        if (!result.has(key)) result.set(key,{key,first:row,rows:[]});
        result.get(key).rows.push(row);
      }
      return [...result.values()];
    },
    setBomDraftTotal(group, value) {
      try {
        const counts=distribute(group.rows.map(row => ({remaining_quantity:row.purchase_total_sheet_qty})),Number(value));
        if (counts.some(n => n<1)) throw new Error('总数不足以分配到全部产品，请展开明细调整');
        group.rows.forEach((row,index) => {row.purchase_total_sheet_qty=counts[index];this.onPurchasePurposeTotalChanged(row);});
      } catch(error) {this.showToast(error.message,true);}
    },
    incomingDisplayGroups() {
      if (this.incomingTab !== 'pending') return this.incomingRowsForTab().map((row,index) => ({key:row.history_key || row.receipt_item_id || index,first:row,rows:[row]}));
      return groups(this.incomingPending);
    },
    incomingGroupQuantity(group, field) {
      return group.rows.reduce((sum,row) => sum+Number(row[field] ?? (field==='planned_quantity' ? row.requisition_qty : 0) ?? 0),0);
    },
    incomingGroupSelected(group) { return group.rows.every(row => !!this.incomingSelected[row.item_id]); },
    selectIncomingGroup(group, checked) {
      group.rows.forEach(row => { if (this.canReceiveIncoming(row)) this.incomingSelected[row.item_id]=checked; });
    },
    toggleIncomingGroup(group) { this.incomingExpandedGroups[group.key]=!this.incomingExpandedGroups[group.key]; },
    setIncomingGroupQuantity(group, value) {
      try {
        const counts=distribute(group.rows, Number(value));
        group.rows.forEach((row,index) => {
          row.incoming_quantity=counts[index];
          this.onIncomingQuantityChanged(row);
          if (Number(row.cumulative_received_quantity || 0)+counts[index] < Number(row.planned_quantity || row.requisition_qty || 0)) row.resolution_action='await_supplier';
        });
      } catch(error) { this.showToast(error.message,true); }
    },
    async receiveIncomingGroup(group) {
      const rows=group.rows.filter(row => Number(row.incoming_quantity)>0);
      if (!rows.length) return this.showToast('请填写本次实收总数',true);
      return this.batchReceiveIncoming(rows);
    },
  };
  root.TianmingIncomingGroups={groups,distribute,methods};
  if (typeof module==='object' && module.exports) module.exports=root.TianmingIncomingGroups;
})(typeof window==='undefined' ? globalThis : window);
