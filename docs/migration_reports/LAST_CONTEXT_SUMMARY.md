# 最后上下文摘要

日期：2026-06-19

## 本线程已完成

- 只读探查 `Z:\sata1-18015598002\BoxERP\erp.db`。
- 确认其完整性为 `ok`，但订单和订单明细均为 0。
- 从 `erp_audit` 确认瑞达 SQL Server 历史数据约 4 万订单。
- 找到并部署 SQL Server 2022 Express 独立实例 `BOXERP`。
- SQL Server 服务曾确认显示 `Running`。
- 将 BAK 复制到 SQL Server 可访问目录，并验证源与副本 SHA-256 一致。
- 执行 `RESTORE HEADERONLY`、`FILELISTONLY`、`VERIFYONLY`，验证成功。
- 隔离目标数据库为 `BoxDB20_REPRO`；本线程曾观察其为 `ONLINE`。
- 只读盘点还原库：
  - `Orders`：39,922
  - `OrderXLs`：40,449
  - `CaiGouDanDetails`：31,961
  - `OrderXLs_common`：3,407
  - `Customers`：132
- 只读盘点 `data/carton_erp.sqlite3`：
  - `legacy_ruida_orders`：39,766
  - `legacy_ruida_order_items`：40,293
  - `legacy_ruida_customers`：132
  - `sales_orders`：4
  - `sales_order_items`：7

## 未完成

- 未执行 SQL Server → `legacy_ruida_*` 的最新差异同步。
- 未刷新任何原始层数据。
- 未设计完成正式订单字段映射。
- 未向 `sales_orders` / `sales_order_items` 导入历史订单。
- 未执行 100 条测试迁移。
- 未执行全量迁移。

## 当前结论

最新 BAK 比现有原始层多约 156 个订单主表记录和 156 个订单明细记录。下一步必须先做只读差异统计，不能直接写正式业务表。

## 本次交接收尾声明

本次收尾仅创建交接文档：

- 没有修改业务代码。
- 没有修改任何数据库。
- 没有执行正式导入。
- 没有继续执行 SQL Server 还原或迁移。

