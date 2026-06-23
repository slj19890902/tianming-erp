# ERP 正式使用层历史订单显示优化与旧系统名称隐藏实施报告

执行时间：2026-06-20 18:16:07

## 本轮边界

- 未执行任何历史迁移 apply
- 未修改主库历史订单号
- 未批量修改主库状态
- 未修改 `legacy_ruida_*` 原始层
- 主库正式数据保持：
  - `sales_orders = 15593`
  - `sales_order_items = 15658`
  - 历史迁移订单数 = `15589`

## 实施目标

本轮只处理显示层、普通业务 API、前端页面、日常用户文档和副本演练，目标是让新 ERP 日常使用层不再出现旧系统名称，并统一把历史订单展示为 `TMYYYYMMDD-####`。

## 已完成实现

### 1. 普通业务 API 显示层

已改造以下接口的历史订单显示逻辑：

- `app/api/orders.py`
- `app/api/incoming.py`
- `app/api/requisition.py`
- `app/api/finance.py`
- `app/api/deliveries.py`

处理规则：

- 历史订单对普通 API 返回 `display_order_number`
- 对历史订单，普通返回字段 `order_number` 也已改为 TM 展示编号
- 增加 `display_order_number`
- 用户可见备注等文本经过清洗，不再暴露旧系统名称
- 搜索支持 `TM` 编号、客户名、客户单号等普通业务检索

### 2. 前端页面显示层

已改造：

- `static/index.html`

处理结果：

- 订单列表、详情、报料、送货等页面统一显示 TM 历史订单号
- 搜索占位符改为“搜索订单号、TM编号、客户单号”
- 用户可见页面已移除旧系统名称

### 3. 通用历史订单显示服务

新增：

- `app/services/history_orders.py`

用途：

- 识别历史迁移订单
- 生成稳定 TM 展示编号
- 清洗用户可见文本
- 为多个 API 复用相同显示规则

### 4. 用户文档清理

已清理以下正式使用文档中的旧系统名称：

- `docs/go_live_checklists/MANUAL_ACCEPTANCE_CHECKLIST.md`
- `docs/go_live_checklists/DAILY_OPERATION_GUIDE.md`
- `docs/go_live_checklists/GO_LIVE_READINESS_SUMMARY.md`

并复核以下文件当前无旧系统名称暴露：

- `docs/go_live_checklists/PASSWORD_AND_ACCOUNT_SECURITY.md`
- `docs/go_live_checklists/RBAC_PERMISSION_CHECK.md`
- `docs/go_live_checklists/BACKUP_AND_RESTORE_GUIDE.md`

## 测试结果

本轮已通过定向测试：

- `tests/test_phase5_orders.py`
- `tests/test_phase6_incoming.py`
- `tests/test_phase8_finance.py`
- `tests/test_phase11_requisition.py`
- `tests/test_phase14_frontend.py`
- `tests/migration/test_rehearse_history_display_and_status.py`

汇总结果：

```text
54 passed
```

## 副本演练

已执行仅副本的真实改号演练：

- 副本库：
  `data/sandboxes/carton_erp_history_display_status_rehearsal_20260620_181354.sqlite3`
- 映射清单：
  `docs/migration_reports/HISTORY_ORDER_TM_RENUMBER_MAPPING_20260620_181354.csv`
- 演练报告：
  `docs/migration_reports/HISTORY_ORDER_TM_RENUMBER_REHEARSAL_20260620_181354.md`

演练结果：

- 历史订单总数：`15589`
- TM 编号总数：`15589`
- 剩余旧前缀订单：`0`
- 重复订单号：`0`
- `integrity_check = ok`
- `foreign_key_check = 0`
- 主库未修改

## 主库确认

- 本轮没有对主库执行批量真实改号
- 主库历史订单显示仍通过 API / 前端展示层隐藏旧前缀
- 内部旧表名、迁移台账、迁移脚本和历史迁移报告仍可保留旧技术对象名，供开发与审计追溯

## 结论

1. 新 ERP 日常页面已不再显示旧系统名称。
2. 普通业务 API 已不再向前端返回旧系统名称和旧前缀展示值。
3. 给实际使用人员看的正式文档已清理旧系统名称。
4. 旧名称目前仅保留在内部技术对象名、迁移审计层和既有历史迁移资料中。
5. 主库未做任何批量真实改号。
6. 副本 TM 改号演练已通过。

## 下一步建议

1. 保持主库真实订单号不动，先以显示层方案进入正式使用。
2. 如果后续需要主库真实改号，必须单独授权，并先复用本轮副本演练脚本。
3. 后续新增正式用户文档与页面功能时，继续执行“旧系统名称不得出现在日常使用层”的规则。
