# 瑞达历史数据迁移计划

## 目标

安全完成：

```text
BoxDB20_REPRO
→ legacy_ruida_* 原始层
→ 100 条正式表试迁移
→ 全量正式表迁移
```

本计划默认 dry-run，任何提交操作必须单独授权并先备份。

## 阶段 0：环境复核

- 读取 `docs/CODEX_HANDOFF.md` 和 `AGENTS.md`。
- 确认 SQL Server 实例、`BoxDB20_REPRO` 状态和只读连接。
- 确认 `data/carton_erp.sqlite3` 文件位置、大小和哈希。
- 禁止使用 `erp.db` 作为订单源。

## 阶段 1：原始层差异统计

- 比较 SQL Server `Orders` 与 `legacy_ruida_orders` 的主键集合。
- 比较 `OrderXLs` 与 `legacy_ruida_order_items` 的主键集合。
- 比较 `Customers` 与 `legacy_ruida_customers`。
- 统计新增、缺失、重复、字段变化、孤儿明细。
- 输出报告到 `docs/migration_reports/`。
- 本阶段禁止写数据库。

预期重点差异：

- SQL Server `Orders` 约 39,922，SQLite 原始层约 39,766。
- SQL Server `OrderXLs` 约 40,449，SQLite 原始层约 40,293。

## 阶段 2：刷新原始层

- 先复制 `data/carton_erp.sqlite3` 到带时间戳的备份文件。
- 在数据库副本中测试 UPSERT。
- 使用瑞达原始 ID 作为幂等键。
- 保留 `source_json` 和源更新时间。
- 验证行数、重复键、孤儿记录和哈希。
- 未通过验证不得替换主沙盘。

## 阶段 3：正式表映射设计

- 定义 `Orders` → `sales_orders` 映射。
- 定义 `OrderXLs` → `sales_order_items` 映射。
- 明确客户、产品、订单号、状态、日期、数量、金额映射。
- 金额使用 Decimal，禁止 float 直接落库。
- 无法映射的客户或产品进入冲突报告，不得静默丢弃。
- 明确与现有 4 个正式订单的去重策略。

## 阶段 4：100 条试迁移

- 仅对数据库副本执行。
- 选择有客户、有明细、有金额的稳定样本。
- 导入 100 个订单主表及关联明细。
- 验证主外键、订单总额、明细合计、日期和中文字段。
- 输出逐项验收报告。

## 阶段 5：全量迁移

只有在 100 条测试全部通过并获得明确授权后执行：

- 再次备份。
- 全量幂等导入。
- 核对源目标数量和金额。
- 执行完整性检查。
- 保留回滚文件和迁移报告。

## N034 Phase A：复合产品 BOM

- worktree：`D:\tm-worktrees\erp-composite-bom-n034`；branch：`feature/composite-bom-n034`。
- 迁移链：`bd57v8x9z47`（N034 Phase A）接在 `bc56v8x9z46` 后。
- 新增三张表：`product_bom_components`、`sales_order_item_bom_components`、`requisition_item_bom_sources`；`products` 新增 `is_composite`、`is_internal_component` 两个标记。
- `sales_order_item_bom_components` 保存历史快照，快照字段不可变；其 `product_bom_component_id` 在模板删除时允许 `SET NULL`，不得借此修改快照内容。
- downgrade 必须 fail-closed：三张 N034 表任一有事实行，或任意 Product 的 `is_composite` / `is_internal_component` 为真，即拒绝降级；只有三表均为空且两个标记均无真值时才可降级。
- 隔离副本 `D:\tm-uat\composite_bom_n034_20260718_130112\carton_erp_uat.sqlite3` 已完成 `head -> base -> head`，最终 `integrity_check=ok`、`foreign_key_check=0`；测试 `144 passed`；UAT 端口 `18068`。
- 正式库未写入；本阶段未 commit、未 push。

