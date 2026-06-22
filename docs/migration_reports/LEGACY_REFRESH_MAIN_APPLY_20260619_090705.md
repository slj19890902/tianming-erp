# legacy_ruida_* 主库增量刷新 Apply 报告

## 执行声明

- 执行时间：2026-06-19 09:07:05 至 09:08:13 +08:00
- 项目根目录：`D:\纸箱厂erp软件搭建`
- 是否修改主库：是
- 修改范围：仅 `legacy_ruida_orders`、`legacy_ruida_order_items`、`legacy_ruida_customers`
- 是否修改正式业务表：否
- 是否修改 SQL Server：否
- 是否执行正式表迁移：否
- SQL Server 源库：`.\BOXERP / BoxDB20_REPRO`
- SQLite 主库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- Apply 批次：`legacy-ruida-20260619_090738`
- 脚本报告：`docs/migration_reports/LEGACY_REFRESH_APPLY_20260619_090738.md`
- Apply 后 dry-run：`docs/migration_reports/LEGACY_REFRESH_DRY_RUN_20260619_090813.md`

## 服务与目录门禁

- `Get-Location` 确认为 `D:\纸箱厂erp软件搭建`。
- 检测到项目 uvicorn 后端监听 8002。
- Apply 前已停止 PID 30472/13696，并确认进程退出、8002 端口无监听。
- 前端 Vite 未停止，因为其不访问 SQLite。

## 备份

用户要求的主备份：

```text
D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_legacy_refresh_apply_20260619_090705.sqlite3
```

| 项目 | 结果 |
|---|---|
| 文件大小 | 209,182,720 字节 |
| 修改时间 UTC | 2026-06-15 09:32:39.6735849 |
| SHA-256 | `9CCECF883828A9CDAD54B44443765082E00969784EEDCFDE8F64812B59F18ADA` |
| `integrity_check` | `ok` |
| 与 Apply 前主库大小/哈希 | 一致 |

脚本另创建事务前自动备份：

```text
D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_legacy_refresh_20260619_090738.sqlite3
```

该备份大小、SHA-256 和完整性同样验证通过。

## Apply 前状态

| 项目 | 结果 |
|---|---|
| 主库大小 | 209,182,720 字节 |
| 修改时间 UTC | 2026-06-15 09:32:39.6735849 |
| SHA-256 | `9CCECF883828A9CDAD54B44443765082E00969784EEDCFDE8F64812B59F18ADA` |
| `integrity_check` | `ok` |

| 表 | Apply 前 |
|---|---:|
| `legacy_ruida_orders` | 39,766 |
| `legacy_ruida_order_items` | 40,293 |
| `legacy_ruida_customers` | 132 |
| `sales_orders` | 4 |
| `sales_order_items` | 7 |

Apply 前显式 `--sqlite-path` dry-run 为订单 156、明细 156、客户 0。

## Apply 命令与结果

```powershell
.\.venv\Scripts\python.exe .\scripts\migration\refresh_legacy_ruida_from_sqlserver.py `
  --sqlite-path .\data\carton_erp.sqlite3 `
  --apply `
  --confirm-apply APPLY_LEGACY_RUIDA_REFRESH `
  --allow-main-sandbox
```

实际新增：

- 订单：156
- 明细：156
- 客户：0

## Apply 后状态

| 表 | Apply 前 | Apply 后 | 变化 |
|---|---:|---:|---:|
| `legacy_ruida_orders` | 39,766 | 39,922 | +156 |
| `legacy_ruida_order_items` | 40,293 | 40,449 | +156 |
| `legacy_ruida_customers` | 132 | 132 | 0 |
| `sales_orders` | 4 | 4 | 0 |
| `sales_order_items` | 7 | 7 | 0 |

- `PRAGMA integrity_check`：`ok`
- Apply 后 SHA-256：`6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`
- `legacy_order_id` 重复组：0
- `legacy_item_id` 重复组：0
- `legacy_customer_id` 重复组：0
- 孤儿 legacy 明细：0
- 新增订单 ID：42688–42843
- 新增明细 ID：43370–43527
- 新增明细数量合计：181,967
- 新增明细金额合计：437,688.30
- 156 条新增订单、156 条新增明细关键字段与 SQL Server 对照差异：0

## Dry-run 复查

Apply 后再次显式指定主库运行 dry-run：

| 数据 | 待新增 |
|---|---:|
| 订单 | 0 |
| 明细 | 0 |
| 客户 | 0 |

幂等刷新验证通过。

## 正式业务表保护

- `sales_orders`：Apply 前后均为 4。
- `sales_order_items`：Apply 前后均为 7。
- 本轮未执行正式业务表导入，未修改正式业务表结构。

## 风险点

- 主库已真实更新，回滚会撤销本批次 156+156 条原始层增量。
- legacy 金额字段仍使用 SQLite `REAL`；正式表迁移必须另行设计 Decimal 精度规则。
- 新增明细的正式产品/供应商映射未处理，这是原始层预期行为。
- 脚本只追加缺失源 ID，不同步已有 legacy 行的源端历史修订。
- 本报告不授权继续执行 `legacy_ruida_*` 到正式业务表的迁移。

## 回滚方式

如需回滚：

1. 停止 ERP 后端及其他 SQLite 访问进程。
2. 保留当前主库为带时间戳的证据副本，不删除。
3. 复核备份 SHA-256 为 `9CCECF883828A9CDAD54B44443765082E00969784EEDCFDE8F64812B59F18ADA`。
4. 将 `carton_erp_before_legacy_refresh_apply_20260619_090705.sqlite3` 恢复到 `data/carton_erp.sqlite3`。
5. 重新执行 `integrity_check` 和五张表数量核对。
6. 未经再次授权，不执行回滚。

## 下一步建议

下一最小任务仅为：设计 `legacy_ruida_*` 到 `sales_orders / sales_order_items` 的正式表字段映射和 100 条隔离副本试迁移方案，不执行正式表写入。
