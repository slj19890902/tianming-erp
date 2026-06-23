# legacy_ruida_* 隔离副本 Apply 验证报告

## 执行声明

- 执行时间：2026-06-19 08:59:01 至 09:00:32 +08:00
- 当前项目根：`D:\纸箱厂erp软件搭建`
- 本轮是否修改主库：否
- 是否只修改副本库：是
- 是否写入 `sales_orders` / `sales_order_items`：否
- SQL Server 源库：`.\BOXERP / BoxDB20_REPRO`
- 主库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- 副本库：`D:\纸箱厂erp软件搭建\data\sandboxes\carton_erp_legacy_refresh_apply_test_20260619_085901.sqlite3`
- Apply 批次：`legacy-ruida-20260619_085956`
- 脚本报告：`docs/migration_reports/LEGACY_REFRESH_APPLY_20260619_085956.md`
- Apply 后 dry-run：`docs/migration_reports/LEGACY_REFRESH_DRY_RUN_20260619_090032.md`

## 副本创建验证

| 项目 | 主库 | 新建副本 |
|---|---|---|
| 文件大小 | 209,182,720 字节 | 209,182,720 字节 |
| 修改时间 UTC | 2026-06-15 09:32:39.6735849 | 2026-06-15 09:32:39.6735849 |
| SHA-256 | `9CCECF883828A9CDAD54B44443765082E00969784EEDCFDE8F64812B59F18ADA` | 同主库 |
| `integrity_check` | `ok` | `ok` |

复制后大小及 SHA-256 完全一致。

## Apply 前后数量

| 表 | Apply 前 | 新增 | Apply 后 |
|---|---:|---:|---:|
| `legacy_ruida_orders` | 39,766 | 156 | 39,922 |
| `legacy_ruida_order_items` | 40,293 | 156 | 40,449 |
| `legacy_ruida_customers` | 132 | 0 | 132 |
| `sales_orders` | 4 | 0 | 4 |
| `sales_order_items` | 7 | 0 | 7 |

Apply 命令显式指向副本，未传入主库写入所需的 `--allow-main-sandbox`：

```powershell
.\.venv\Scripts\python.exe .\scripts\migration\refresh_legacy_ruida_from_sqlserver.py `
  --target-sqlite .\data\sandboxes\carton_erp_legacy_refresh_apply_test_20260619_085901.sqlite3 `
  --apply `
  --confirm-apply APPLY_LEGACY_RUIDA_REFRESH
```

## 备份与事务

脚本写副本前自动创建并验证：

```text
data/backups/carton_erp_before_legacy_refresh_20260619_085956.sqlite3
```

- 大小：209,182,720 字节
- SHA-256：`9CCECF883828A9CDAD54B44443765082E00969784EEDCFDE8F64812B59F18ADA`
- `integrity_check`：`ok`
- 写入在单个事务内完成，实际新增数与计划数一致后提交。

## Apply 后校验

- 副本 `integrity_check`：`ok`
- `legacy_order_id`、`legacy_item_id`、`legacy_customer_id` 重复组：均为 0
- 明细找不到 legacy 订单：0
- 新增订单找不到 legacy 客户：0
- 新增订单 ID：42688–42843，共 156 条
- 新增明细 ID：43370–43527，共 156 条
- 新增明细数量合计：181,967
- 新增明细金额合计：437,688.30
- 新增行 `imported_at`：2026-06-19 08:59:56

对 156 条新增订单和 156 条新增明细的父键、客户、日期、数量、单价、金额、备注及源更新时间等关键字段执行 SQL Server 对照，字段差异均为 0。

## 幂等 dry-run 复查

Apply 后再次针对同一副本运行 dry-run：

| 数据 | 待新增 |
|---|---:|
| 订单 | 0 |
| 明细 | 0 |
| 客户 | 0 |

## 主库零变化验证

- SHA-256：`9CCECF883828A9CDAD54B44443765082E00969784EEDCFDE8F64812B59F18ADA`
- 文件大小：209,182,720 字节
- 修改时间 UTC：2026-06-15 09:32:39.6735849
- `integrity_check`：`ok`
- 表数量仍为 39,766 / 40,293 / 132 / 4 / 7

主库哈希、大小、修改时间及表数量均未变化。

## 是否可以进入“主库备份后 Apply”

技术验证结论：**可以进入主库 Apply 准备与授权评审，但本报告不授权直接执行。**

进入前仍须重新获得明确授权、停止 ERP、创建并验证主库新备份、再次 dry-run 为 156/156/0，并明确回滚文件。

## 风险点

- 主库 Apply 会真实改变原始层，必须单独授权。
- 新增明细的 `product_archive_id`、`supplier_id` 保持为空，未做猜测映射。
- legacy 金额使用 SQLite `REAL`；正式业务表迁移仍需 Decimal 规则。
- 脚本只追加缺失源 ID，不更新已有 legacy 行；历史修订同步需另行设计。
