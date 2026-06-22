# legacy_ruida_* 幂等增量刷新方案评审

## 执行声明

- 方案时间：2026-06-19 08:31:21 +08:00
- 当前真实项目根目录：`D:\纸箱厂erp软件搭建`
- 本轮是否只读：是
- 是否修改数据库：否
- 是否执行导入：否
- 是否运行 `--apply`：否
- 源库：`.\BOXERP / BoxDB20_REPRO`
- 目标库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- 依据：`docs/migration_reports/DRY_RUN_DIFF_20260619_081040.md`
- 脚本：`scripts/migration/refresh_legacy_ruida_from_sqlserver.py`

## 评审结论

采用“按瑞达源主键只追加缺失记录，已有 legacy 记录只报告、不自动更新”的保守策略。三张源表均有稳定整数 `ID`，三张 legacy 表均已有对应源 ID 的 `UNIQUE NOT NULL` 约束。当前差异是连续新增批次，重叠记录主要映射字段一致，没有必要扩大为全表更新。

## 表映射与幂等键

| SQL Server 源表 | SQLite 目标表 | 幂等匹配键 | 策略 |
|---|---|---|---|
| `Orders` | `legacy_ruida_orders` | `Orders.ID` ↔ `legacy_order_id` | 追加源端独有 ID |
| `OrderXLs` | `legacy_ruida_order_items` | `OrderXLs.ID` ↔ `legacy_item_id` | 追加源端独有 ID |
| `Customers` | `legacy_ruida_customers` | `Customers.ID` ↔ `legacy_customer_id` | 当前无差异，默认跳过 |

### 无可靠主键时

当前不需要组合键。若未来发现源 ID 缺失或不稳定，必须停止自动写入并输出冲突报告。仅供人工匹配的候选组合：

- 订单：`客户_FK + opTime + 审核时间 + PO`；`电脑单号` 全空，不可使用。
- 明细：`Order_FK + 客户单号 + 款号 + 长宽高 + 下单日期 + 数量 + 单价`。
- 客户：优先 `编码`；人工候选为 `全称 + 电话 + 地址`。全称有重名，不能单独使用。

组合字段不得替代源 ID 自动落库。

## 增量识别与预计数量

```text
待新增 = SQL Server 源 ID 集合 - SQLite legacy 源 ID 集合
目标多余 = SQLite legacy 源 ID 集合 - SQL Server 源 ID 集合
```

| 数据 | 源数量 | 目标数量 | 预计新增 | 目标多余 |
|---|---:|---:|---:|---:|
| 订单 | 39,922 | 39,766 | 156 | 0 |
| 明细 | 40,449 | 40,293 | 156 | 0 |
| 客户 | 132 | 132 | 0 | 0 |

- 订单缺失 ID：`42688–42843`。
- 明细缺失 ID：`43370–43527`。
- 客户默认不刷新；只有发现缺失客户并显式使用 `--refresh-customers` 才允许追加。

## 已有数据与防重复策略

- 仅追加缺失 ID；已有 ID只报告，不执行 `UPDATE`。
- 不删除 SQLite 独有行。
- 插入使用 `ON CONFLICT(<legacy key>) DO NOTHING`。
- 集合差、SQLite 唯一约束和冲突忽略形成三重防重复。
- 若未来需要更新已有行，必须另行设计字段级覆盖和审计规则。

## 批次记录

不新增数据库批次表。每次运行使用批次 ID `legacy-ruida-YYYYMMDD_HHMMSS`，完整日志写入：

```text
docs/migration_reports/LEGACY_REFRESH_<MODE>_<timestamp>.md
```

报告记录模式、源目标、刷新前后文件状态、SHA-256、完整性、表数量、源/目标差集、计划及实际插入数、备份路径和哈希。新增行使用同一批次时间写入 `imported_at`。

## 安全执行流程

1. 确认项目根为 `D:\纸箱厂erp软件搭建`。
2. 默认 dry-run，复核预计新增 156/156/0。
3. apply 前停止 ERP 和其他 SQLite 写入进程。
4. 首次 apply 只能针对主沙盘复制出的隔离副本。
5. 记录目标文件大小、修改时间、SHA-256、`integrity_check` 和各表数量。
6. 自动备份为：

```text
data/backups/carton_erp_before_legacy_refresh_YYYYMMDD_HHMMSS.sqlite3
```

7. 验证备份大小、SHA-256 和 `integrity_check`。
8. 使用单个 `BEGIN IMMEDIATE` 事务，顺序为客户（仅需要时）→订单→明细。
9. 任意异常、插入数不符或 `quick_check` 失败时 rollback。
10. 提交后重新执行完整 `integrity_check` 和各表数量统计并生成报告。
11. 任何校验失败都停止，不进入正式业务表迁移。

## 写入门禁

进入写入路径必须同时满足：

- 显式 `--apply`。
- 精确传入 `--confirm-apply APPLY_LEGACY_RUIDA_REFRESH`。
- 目标 SQLite 文件存在。
- SQL Server 数据库必须是 `BoxDB20_REPRO`，且三张源表存在。
- 三张目标表存在，源和目标幂等键无重复。
- 写 `data/carton_erp.sqlite3` 还必须传入 `--allow-main-sandbox`。
- 客户有新增时必须单独传入 `--refresh-customers`。

本轮没有运行任何包含 `--apply` 的命令。

## 执行命令

### Dry-run

```powershell
Set-Location "D:\纸箱厂erp软件搭建"
.\.venv\Scripts\python.exe .\scripts\migration\refresh_legacy_ruida_from_sqlserver.py
```

### Apply（仅隔离副本，仍需再次明确授权）

```powershell
.\.venv\Scripts\python.exe .\scripts\migration\refresh_legacy_ruida_from_sqlserver.py `
  --target-sqlite .\data\work\carton_erp_legacy_refresh_test.sqlite3 `
  --apply `
  --confirm-apply APPLY_LEGACY_RUIDA_REFRESH
```

主沙盘 apply 还需额外加入 `--allow-main-sandbox`；本报告不授权执行。

## 回滚方案

1. 停止 ERP 和所有访问目标 SQLite 的进程。
2. 保留失败目标库并改名为带时间戳的证据文件。
3. 复核备份报告中的 SHA-256。
4. 将对应备份复制回原目标路径。
5. 对恢复文件重新计算 SHA-256，执行 `PRAGMA integrity_check`。
6. 复核三张 legacy 表及 `sales_orders`、`sales_order_items` 数量。
7. 校验未通过前不得启动正式表迁移。

## 风险点

- SQL Server `datetime` 与 SQLite 文本日期格式、精度不同；脚本按现有 legacy 格式保存到秒。
- 原始层 `REAL` 存在浮点精度风险；正式表迁移仍必须使用 Decimal 规则。
- `电脑单号` 全空、`客户单号` 大量重复，均不能用作幂等键。
- 客户全称存在重名，只能按源 ID 关联。
- 新增明细的 `product_archive_id`、`supplier_id` 不自动猜测，保留为空；原始字段保存在映射列和 `source_json`。
- 主沙盘 apply 风险高，必须先通过隔离副本验证。

## 执行前检查清单

- [ ] 用户再次明确授权 apply。
- [ ] ERP 和其他 SQLite 写入进程已停止。
- [ ] 目标是已核验的隔离副本。
- [ ] dry-run 预计新增数量已复核。
- [ ] 目标文件状态、哈希、完整性和表数量已记录。
- [ ] 备份路径为空且可写，备份验证通过。
- [ ] 源库为 `BoxDB20_REPRO`，源和目标幂等键无重复。
- [ ] 回滚目标和备份文件已明确。

## 下一步建议

下一最小任务是：复制 `data/carton_erp.sqlite3` 为隔离测试库，只对该副本执行一次授权 apply，核验新增 156 个订单、156 条明细、0 个客户；不要触碰正式业务表。
