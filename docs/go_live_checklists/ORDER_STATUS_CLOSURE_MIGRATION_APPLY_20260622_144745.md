# 订单状态约束迁移应用报告（P0）

- 日期：2026-06-22
- 任务：应用 `h48d9f6c1e32_order_status_closure`，修复管理员标记 `dead/closed/archived` 等订单状态时触发 SQLite CHECK 约束失败的问题。
- 本轮仅做订单状态约束迁移，未做 UI 优化、未重构代码、未修改历史订单内容、未改 `legacy_ruida_*` / `migration_*` / `snapshot_*` / `RUIDA-*`。

## 1. 结果

- 迁移成功，已写库（仅表结构约束）。无异常。
- 正式库 alembic 版本：`g37c8e5b0d21` → `h48d9f6c1e32`。

## 2. 备份信息

- 路径：`data/backups/carton_erp_BEFORE_ORDER_STATUS_CLOSURE_20260622_144745.sqlite3`
- 大小：`219447296` bytes
- SHA-256：`b1f597bf3172ade5cdee847fccf7f1ed9ead0642ed445a2430d3a3eb348a95e3`（与迁移前主库逐字节一致）
- 备份 `integrity_check = ok`，`foreign_key_check = 0`
- 备份 `sales_orders = 9549`，`sales_order_items = 9615`，`alembic_version = g37c8e5b0d21`

## 3. 迁移前后对比

| 项目 | 迁移前 | 迁移后 |
|---|---|---|
| alembic_version | g37c8e5b0d21 | h48d9f6c1e32 |
| sales_orders | 9549 | 9549 |
| sales_order_items | 9615 | 9615 |
| integrity_check | ok | ok |
| foreign_key_check | 0 | 0 |
| status CHECK | 6 状态 | 14 状态 |

- 迁移前约束：`status IN ('pending_production','production','pending_delivery','partially_delivered','delivered','cancelled')`
- 迁移后约束：`status IN ('pending_confirmation','pending_production','production','pending_delivery','partially_delivered','pending_reconciliation','pending_invoice','pending_payment','delivered','completed','archived','closed','dead','cancelled')`
- 已确认新增包含：`dead`、`closed`、`archived`、`completed`、`pending_confirmation`、`pending_reconciliation`
- 订单状态分布未变（无历史行被改）：`pending_production=9543`、`delivered=4`、`cancelled=1`、`pending_delivery=1`

## 4. 功能验证

- 测试订单：`id=15594`，`TM20260621001`（既有受控/已作废测试单，非 RUIDA-）
- 原始状态：`cancelled`
- 通过现有端点逻辑 `app.api.orders.update_order_status` 标记为 `dead`：成功，无 `CHECK constraint failed`
- 提交后确认 `status=dead`；`operation_logs` 新增 1 条（action=STATUS，before=cancelled，after=dead）
- 已恢复原始状态 `cancelled`，备注与明细 `is_force_closed` 标志均还原一致

## 5. 测试结果

- 命令：`python -X utf8 -m pytest tests/test_phase5_orders.py tests/test_phase14_frontend.py tests/test_phase8_finance.py tests/test_phase7_deliveries.py -q`
- 结果：`75 passed`，0 失败

## 6. 回滚方案

如后续发现问题：
1. 停止 8000 服务（`taskkill /PID <pid> /F`）。
2. 用备份覆盖正式库：将 `data/backups/carton_erp_BEFORE_ORDER_STATUS_CLOSURE_20260622_144745.sqlite3` 复制回 `data/carton_erp.sqlite3`（覆盖前可先另存当前库）。
3. 校验：`integrity_check=ok`、`foreign_key_check=0`、`alembic_version=g37c8e5b0d21`、`sales_orders=9549`、`sales_order_items=9615`。
4. 重启服务。
（迁移 `downgrade()` 为 no-op，回滚以备份恢复为准，不用 `alembic downgrade`。）

## 7. 运行态

- 已重启服务，`http://127.0.0.1:8000/api/health` 返回 `9549/9615` 正常。
