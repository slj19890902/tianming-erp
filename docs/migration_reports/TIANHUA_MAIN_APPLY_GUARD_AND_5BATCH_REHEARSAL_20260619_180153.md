# 天华主库迁移保护与5批副本演练报告

- 执行时间：2026-06-19 18:01:53 +08:00
- 本轮是否修改主库：否
- 是否执行主库正式表迁移：否
- 是否执行主库 apply：否
- 主库 SHA-256 前后：`6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`

## 后端真实数据库路径

运行中的后端：

```text
uvicorn phase1_postgres.main:app --host 0.0.0.0 --port 8002
```

数据库模块 `phase1_postgres/database.py` 只读取：

1. `TM_ERP_DATABASE_URL`
2. `TARGET_DB_URL`
3. 默认值 `sqlite+pysqlite:///./data/tm_phase3_dev.sqlite3`

当前 `.env` 未设置前两个变量，只设置了：

```text
ERP_DATABASE_PATH=D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3
```

该变量未被数据库模块读取。因此运行中后端实际连接：

`D:\纸箱厂erp软件搭建\data\tm_phase3_dev.sqlite3`

该路径不等于拟迁移主库：

`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`

API显示6张订单的原因：

- `tm_phase3_dev.sqlite3.orders=6`
- `carton_erp.sqlite3.sales_orders=4`
- 两个文件路径和表结构均不同。

项目中还存在 `tm_erp_test.sqlite3`、浏览器/API测试库和历史备份库。当前错连根因是相对默认路径生效，而 `.env` 中的 `ERP_DATABASE_PATH` 对该后端无效。

修复建议：

- 不要直接把后端URL改向 `carton_erp.sqlite3`；当前后端ORM使用 `orders/order_items`，迁移目标使用 `sales_orders/sales_order_items`，表结构不兼容。
- 必须先明确“正式运行后端应使用哪套模型和数据库”，再单独设计应用切换或数据整合方案。
- 启动脚本应显式设置并记录 `TM_ERP_DATABASE_URL`，启动后提供只读数据库路径诊断接口或日志。

## 迁移脚本安全改造

脚本：`scripts/migration/migrate_legacy_ruida_to_sales_orders.py`

新增：

- `--batch-manifest`
- `--batch-id`
- `--generate-batch-manifest`
- `--expected-source-db-sha256`
- `--allow-main-sandbox`
- `--confirm-main-apply`

默认仍为dry-run；只有显式 `--apply` 才写入。

主库apply必须同时满足：

1. `--apply`
2. `--allow-main-sandbox`
3. `--confirm-main-apply APPLY_TIANHUA_FORMAL_SALES`
4. `--expected-source-db-sha256` 等于执行时主库实际哈希
5. `--batch-manifest`
6. `--batch-id`

任一条件缺失或哈希不一致立即停止。副本apply仍使用原确认短语。

脚本当前SHA-256：

`495EBAE4AB0773644ED80D5655DDBD2A2F21E8A3B34A19C94848DCD8714A0DDB`

## 不可变批次 Manifest

路径：

`docs/migration_reports/TIANHUA_MAIN_BATCH_MANIFEST_20260619_175953.csv`

SHA-256：

`91D4340513DAFC2C039261167CCD9CB85FFB4F9EDB3BA5F7C02AAFDD87041FF3`

字段包括批次ID、订单ID、客户、计划明细数、金额、匹配来源、批内顺序、审核CSV及状态。文件已设置为拒绝覆盖；迁移按清单固定ID选择，已迁移订单只变为本批pending=0，不会滑动补入其他订单。

| 批次 | 订单 | 明细 | 金额 |
|---|---:|---:|---:|
| batch_1 | 100 | 100 | 689,972.39 |
| batch_2 | 1,000 | 1,006 | 1,230,739.83 |
| batch_3 | 5,000 | 5,054 | 8,590,604.37 |
| batch_4 | 5,000 | 5,002 | 16,331,793.23 |
| batch_5 | 4,489 | 4,489 | 4,918,380.12 |
| 合计 | 15,589 | 15,651 | 31,761,489.94 |

## 5批连续副本演练

副本：

`data/sandboxes/carton_erp_tianhua_5batch_rehearsal_20260619_180021.sqlite3`

每批均执行：

1. dry-run
2. apply
3. 同批次dry-run复跑
4. 数量和 `integrity_check`

每批复跑计划订单/明细均为0/0。全部批次完成后：

- `sales_orders=15,593`（基线4 + 15,589）
- `sales_order_items=15,658`（基线7 + 15,651）
- 迁移订单台账15,589
- 迁移明细台账15,651
- `integrity_check=ok`
- 孤儿明细0
- 无效产品0
- 重复订单/明细台账0
- 逐单明细数差异0
- 逐单金额差异0
- 其他客户迁移0
- 费用项迁移0
- Manifest订单与台账差集双向均为0
- 全范围复跑dry-run为0/0

## 测试结果

- 迁移测试：25项通过。
- 覆盖默认dry-run、主库确认短语、expected hash、manifest/batch必填、哈希不一致、rejected排除、固定清单不滑动、副本授权和幂等选择。

## 主库保护结果

- 主库未修改。
- SHA-256、文件大小和修改时间未变化。
- 主库正式表仍为4/7，产品3,316。
- 本轮所有apply仅作用于新副本。

## 是否仍有阻塞点

仍有一个关键阻塞：

- 运行中的ERP后端使用 `tm_phase3_dev.sqlite3` 的 `orders/order_items` 模型，而本迁移目标是 `carton_erp.sqlite3` 的 `sales_orders/sales_order_items`。在应用数据库和表模型未统一前，迁移目标无法通过当前前端/API验收。

主库apply保护、不可变分批和5批副本连续演练已完成，但这不构成主库迁移授权。

## 下一步建议

下一最小任务应只读设计“运行后端数据库与迁移目标库统一方案”，明确采用哪套表模型、如何切换配置、如何验证前端订单和对账页面。该架构阻塞解决并再次评审前，不执行主库正式表迁移。
