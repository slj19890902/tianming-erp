# Phase 1 主库只读基线记录

记录时间：2026-06-27（factory PC）
性质：只读。未修改正式代码、配置、数据库或历史数据。

## Git
- 分支：`factory-current-baseline`
- 提交：`c61dc61d513105c0d9b53099d575fa91f74b1dbf`
- 工作树：仅 `docs/product_planning/`（未跟踪规划文档）

## 正式库
- 路径：`data/carton_erp.sqlite3`
- 大小：222,134,272 bytes（211.8 MB）
- mtime：2026-06-26T19:07:48
- SHA-256：`02d06b01410844b4c4f55d75a0b4e6afb09d4537a29c357fe23984392010fad0`
- integrity_check：`ok`
- foreign_key_check：0 violations
- alembic_version：`s57m1p9q6r39`

## 关键表行数（2026-06-27 实测）
| 表 | 行数 |
|---|---|
| customers | 132 |
| products | 3316 |
| materials | 46 |
| sales_orders | 15594 |
| sales_order_items | 15660 |
| material_requisitions | 2 |
| material_requisition_items | 3 |
| supplier_requisition_orders | 0 |
| supplier_requisition_order_items | 0 |
| sales_deliveries | 4 |
| sales_delivery_items | 7 |
| finance_return_receipts | 4 |
| finance_return_receipt_items | 7 |
| finance_statements | 3 |
| finance_statement_items | 6 |
| finance_invoices | 0 |
| finance_settlement_records | 3 |
| users | 4 |
| operation_logs | 172 |

## 送货现状（Phase 1 直接相关）
- `sales_deliveries.status` 分布：`dispatched` × 4（当前无 pending）。
- `sales_deliveries` 列：id, delivery_number, customer_id, delivery_date, vehicle_number,
  status(VARCHAR(20) default 'pending'), total_quantity, created_by,
  dispatched_by, dispatched_at, created_at, printed_by, printed_at。
- `sales_delivery_items` 列：id, delivery_id, order_item_id, delivered_quantity, remarks, created_at。
- 现有 4 张送货单均关联回单（finance_return_receipts=4），意味着真实库**已有回单门禁场景**可供测试。

## 隔离副本
- 位置：会话 scratchpad `.../sandbox/carton_erp.sqlite3`
- SHA-256 与正式库一致（已校验 MATCH）。
- 用途：后续 alembic 迁移演练，不影响正式库。

## 回滚说明
本轮无任何写操作，无需回滚。正式库 SHA-256 未变即为未被触碰的证据。
