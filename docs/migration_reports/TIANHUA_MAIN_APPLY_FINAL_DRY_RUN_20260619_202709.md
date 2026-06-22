# 天华超净主库正式迁移前最终 Dry-run 与备份复核

- 执行时间：2026-06-19 20:27:09（Asia/Shanghai）
- 当前项目根目录：`D:\纸箱厂erp软件搭建`
- 本轮是否执行主库 apply：否
- 本轮是否写入正式订单：否
- 本轮是否修改产品/客户：否
- 本轮是否删除数据库：否

## 1. 后端与主库确认

- 当前后端进程：`uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`
- 健康检查：正常
- 后端报告数据库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- 后端订单模型：`sales_orders / sales_order_items`
- API 健康检查数量：订单 4、明细 7
- 未读取 `tm_phase3_dev.sqlite3`。

## 2. 主库迁移前基线

| 项目 | 结果 |
|---|---:|
| 主库路径 | `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3` |
| 文件大小 | 209,182,720 字节 |
| 修改时间 UTC | `2026-06-19T01:07:42.3186118Z` |
| SHA-256 | `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77` |
| `PRAGMA integrity_check` | `ok` |
| `sales_orders` | 4 |
| `sales_order_items` | 7 |
| `legacy_ruida_orders` | 39,922 |
| `legacy_ruida_order_items` | 40,449 |
| `customers` | 132 |
| `products` | 3,316 |
| `users` | 2 |
| `sqlite_sequence` 行数 | 12 |
| 当前只读连接 `PRAGMA foreign_keys` | 0 |
| `PRAGMA foreign_key_check` 异常数 | 0 |

主库 SHA-256 与数据库路径统一报告一致，无变化。

## 3. 备份复核

正式迁移前字节级备份：

`D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_tianhua_sales_apply_20260619_202543.sqlite3`

| 项目 | 结果 |
|---|---:|
| 文件大小 | 209,182,720 字节 |
| 修改时间 UTC | `2026-06-19T01:07:42.3186118Z` |
| SHA-256 | `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77` |
| `PRAGMA integrity_check` | `ok` |
| 与主库 SHA-256 一致 | 是 |
| 复制期间主库哈希变化 | 否 |

另保留 SQLite Backup API 在线逻辑备份：

`data/backups/carton_erp_before_tianhua_sales_apply_20260619_202414.sqlite3`

其大小一致且 `integrity_check=ok`；因 Backup API 重建 SQLite 页，物理 SHA-256 为
`CC0224B4B1856A7DEDDC4B01E99BB9DC41E2F94F2C0E4EE376E7BF1A6BE8B8AB`，
不作为“字节级回滚基线”，但可作为第二份逻辑恢复证据。

## 4. 输入文件

- 批次 Manifest：
  `docs/migration_reports/TIANHUA_MAIN_BATCH_MANIFEST_20260619_175953.csv`
- Manifest SHA-256：
  `91D4340513DAFC2C039261167CCD9CB85FFB4F9EDB3BA5F7C02AAFDD87041FF3`
- 人工审核文件：
  `docs/migration_reports/PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_APPROVED_EXCEPT_32_157_FIXED.csv`
- 审核文件 SHA-256：
  `6A4C966AD497273533D04E69FF82DB2F421AA5392970B5A0AE61B9318EBAE8A7`

Manifest 共 15,589 个唯一订单 ID，无跨批重复。

## 5. 五批主库 Dry-run

所有命令均未传入 `--apply`，脚本以 SQLite `mode=ro` 和 `PRAGMA query_only=ON`
打开主库；五批 `inserted.orders/items` 均为 `0/0`。

| 批次 | 订单 | 明细 | 金额 | Prefix | 精确 | 多明细订单 |
|---|---:|---:|---:|---:|---:|---:|
| `batch_1` | 100 | 100 | 689,972.39 | 80 | 20 | 0 |
| `batch_2` | 1,000 | 1,006 | 1,230,739.83 | 822 | 184 | 5 |
| `batch_3` | 5,000 | 5,054 | 8,590,604.37 | 4,638 | 416 | 43 |
| `batch_4` | 5,000 | 5,002 | 16,331,793.23 | 4,533 | 469 | 2 |
| `batch_5` | 4,489 | 4,489 | 4,918,380.12 | 4,102 | 387 | 0 |
| **合计** | **15,589** | **15,651** | **31,761,489.94** | **14,175** | **1,476** | **50** |

结果与五批副本演练及 Manifest 完全一致。

每批共同检查结果：

- `selected_rejected_items=0`，第 32、157 行对应 rejected 款号未进入计划。
- `selected_fee_items=0`，费用项未进入计划。
- `selected_unmatched_items=0`，无未匹配产品明细进入计划。
- `already_imported=0`，当前主库尚未迁移这 5 批。
- 跳过原因总口径保持：产品未匹配/不唯一 8,527 张、人工拒绝 70 张、费用项 1 张。
- 产品映射均引用 `products` 中现有产品，且批准映射检查同一客户。
- Manifest 订单均可在当前候选集中找到，无滑动补单。

逐批输出：

- `FORMAL_SALES_SAMPLE_DRY-RUN_20260619_202558.md`
- `FORMAL_SALES_SAMPLE_DRY-RUN_20260619_202608.md`
- `FORMAL_SALES_SAMPLE_DRY-RUN_20260619_202610.md`
- `FORMAL_SALES_SAMPLE_DRY-RUN_20260619_202614.md`
- `FORMAL_SALES_SAMPLE_DRY-RUN_20260619_202617.md`

## 6. AUTOINCREMENT / sqlite_sequence 风险

- `sales_orders.id` 与 `sales_order_items.id` 均为 SQLite `INTEGER PRIMARY KEY`。
- 两表 DDL 未使用 `AUTOINCREMENT`，因此 `sqlite_sequence` 中没有对应记录。
- 当前最大 ID：`sales_orders=4`、`sales_order_items=7`。
- 按当前连续插入预计迁移后最大 ID 约为：
  - `sales_orders=15,593`
  - `sales_order_items=15,658`
- 因不存在两表 sequence，“预计最大 ID 低于 sequence”不适用。
- SQLite 会按当前最大 ROWID + 1 自动分配，不影响后续前端新开单。
- 风险评级：低。正式迁移后应检查 `MAX(id)`、重复主键和新增订单自动取号；不建议为这两表手工写 `sqlite_sequence`。

## 7. 外键约束风险

主库表定义：

- `sales_orders.customer_id → customers.id`
- `sales_orders.created_by → users.id`
- `sales_order_items.order_id → sales_orders.id`
- `sales_order_items.product_id → products.id`

现有数据 `PRAGMA foreign_key_check` 异常数为 0。

迁移脚本：

- Dry-run 通过产品表验证 product ID，并要求产品属于同一客户。
- 无产品匹配、人工 rejected、费用项或非法金额/数量时整单跳过。
- Apply 分支在 `BEGIN IMMEDIATE` 前执行 `PRAGMA foreign_keys=ON`。
- 新明细 `order_id` 取刚插入订单的 `lastrowid`。
- Apply 使用事务，异常 rollback。

风险评级：低但需执行时确认。SQLite 外键开关是连接级设置；正式 apply 前应在同一连接明确验证
`PRAGMA foreign_keys=1`，每批后执行 `PRAGMA foreign_key_check` 并要求为 0。现有脚本会开启外键，
但未显式断言开启结果，也未把 `foreign_key_check` 结果写入报告；建议将其列为现场强制验收项，
本轮不修改脚本。

## 8. 停机与恢复方案

本轮未停机，仅完成方案复核。正式 apply 获得单独授权后：

1. 记录并停止 `uvicorn app.main:app` 父子进程。
2. 确认 8000 端口无监听，且不存在其他连接主库的 Python/SQLite 进程。
3. 再次记录主库 SHA-256；必须等于授权命令中的 expected hash。
4. 在停机状态重新创建一份迁移时点备份，并校验 SHA-256 与 `integrity_check`。
5. 每批先 dry-run，再按 manifest 固定 batch apply；任一批失败立即停止。
6. 每批验收数量、金额、外键、孤儿、产品 ID、rejected/费用项和幂等复跑。
7. 全部通过后重启 `start_erp.bat`，验证 `/api/health`、登录、订单查询和对账页面。

回滚：

1. 保持后端停止。
2. 将失败现场库另存为证据，禁止覆盖。
3. 使用字节级备份恢复到 `data/carton_erp.sqlite3`。
4. 校验恢复后 SHA-256、`integrity_check`、4/7 基线和 API 健康。
5. 未定位原因前不继续下一批。

## 9. Readiness 结论

当前具备“申请主库 apply 授权”的条件，但不等于已获得执行授权。

申请授权后的执行前置条件：

1. 明确授权仅执行 `batch_1` 的 100 张订单。
2. 正式停机并确认无数据库占用。
3. 停机后重做时点备份及哈希校验。
4. apply 命令必须包含 manifest、batch ID、主库双重确认短语和实时 expected hash。
5. 同一连接确认 `PRAGMA foreign_keys=1`，批后 `foreign_key_check=0`。

本轮没有执行任何主库 apply，也没有写入历史订单。
