const fields = {items:'明细',quantity:'数量',stock_date:'库存日期',customer_id:'客户',product_id:'产品',location_id:'货位',expected_layout_version:'货位版本',expected_version:'库存版本',inventory_type:'货物类型',unit:'单位',source_kind:'来源',stock_stage:'组装状态',idempotency_key:'提交凭证',client_item_id:'明细凭证',confirmed:'确认状态',initial_inventory_snapshot:'库存核对凭证'};
export function apiErrorMessage(body, status) {
  const detail = body && typeof body === 'object' ? body.detail : null;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map(error => {
      const loc = Array.isArray(error?.loc) ? error.loc : [];
      const index = loc.indexOf('items');
      const row = index >= 0 && Number.isInteger(loc[index+1]) ? `第${loc[index+1]+1}项·` : '';
      const field = fields[loc.at(-1)] || '提交内容';
      const type = error?.type || '';
      const reason = type === 'missing' ? '请填写完整' : type.startsWith('date') ? '请填写有效日期' : type.startsWith('int') ? '请填写整数' : type === 'greater_than' ? '必须大于0' : type === 'string_too_long' ? '内容超过允许长度' : type === 'literal_error' ? '选项无效，请重新选择' : typeof error?.msg === 'string' ? error.msg.replace(/^Value error, /, '') : '格式不正确';
      return `${row}${field}：${reason}`;
    }).join('；');
  }
  return status === 401 ? '登录状态已失效' : `请求失败（${status}）`;
}
