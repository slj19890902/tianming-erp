# 天华超净主库正式迁移 Batch 1 执行报告

- 执行时间：2026-06-19 20:35:11（Asia/Shanghai）
- 项目目录：`D:\纸箱厂erp软件搭建`
- 本轮是否执行主库 apply：是
- 执行范围：仅 `batch_1`
- 是否执行 `batch_2`：否
- 是否执行其他批次或全量迁移：否

## 1. 停机与锁检查

- 已停止 `uvicorn app.main:app` 父子进程。
- 8000、8002 端口均无监听。
- 未发现其他 uvicorn 后端进程。
- SQLite `BEGIN EXCLUSIVE` 锁探针成功。
- 写连接执行 `PRAGMA foreign_keys=ON` 后返回 `1`。

## 2. Apply 前时点备份

备份：

`D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_tianhua_batch_1_apply_20260619_203207.sqlite3`

| 项目 | 主库 / 备份 |
|---|---|
| 文件大小 | 209,182,720 字节 |
| 修改时间 UTC | `2026-06-19T01:07:42.3186118Z` |
| SHA-256 | `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77` |
| `integrity_check` | `ok` |

主库复制前后哈希不变，备份哈希与主库一致。

## 3. Apply 前主库基线

| 表 / 检查 | 结果 |
|---|---:|
| `sales_orders` | 4 |
| `sales_order_items` | 7 |
| `legacy_ruida_orders` | 39,922 |
| `legacy_ruida_order_items` | 40,449 |
| `customers` | 132 |
| `products` | 3,316 |
| `users` | 2 |
| `sqlite_sequence` 行数 | 12 |
| 迁移台账表 | 不存在 |
| `integrity_check` | `ok` |

## 4. Batch 1 Dry-run

输出：

- `docs/migration_reports/FORMAL_SALES_SAMPLE_DRY-RUN_20260619_203223.md`
- `docs/migration_reports/FORMAL_SALES_SAMPLE_IDS_20260619_203223.csv`

结果：

| 项目 | 数量 |
|---|---:|
| 计划订单 | 100 |
| 计划明细 | 100 |
| 金额 | 689,972.39 |
| Approved prefix 映射 | 80 |
| 精确匹配 | 20 |
| Rejected 明细进入计划 | 0 |
| 费用项进入计划 | 0 |
| 未匹配明细进入计划 | 0 |
| 已迁移订单 | 0 |

结果与最终 dry-run 报告的 `batch_1` 完全一致。

## 5. Batch 1 Apply

Apply 前完整 SHA-256：

`6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`

命令使用：

- `--batch-id batch_1`
- `--expected-source-db-sha256`（完整实时哈希）
- `--confirm-main-apply APPLY_TIANHUA_FORMAL_SALES`
- `--allow-main-sandbox`
- `--apply`

脚本在事务前执行 `PRAGMA foreign_keys=ON`，异常时 rollback。

Apply 输出：

- `docs/migration_reports/FORMAL_SALES_SAMPLE_APPLY_20260619_203246.md`
- `docs/migration_reports/FORMAL_SALES_SAMPLE_IDS_20260619_203246.csv`

实际新增：

- 订单：100
- 明细：100
- 金额：689,972.39
- Prefix：80
- 精确：20

Apply 后主库 SHA-256：

`2EBFB2F9A0BE2B7F50A08E114B44532C25B7B497505C3CDAB73ACFE66A67EFD3`

## 6. Apply 后校验

| 检查 | 结果 |
|---|---:|
| `sales_orders` | 104 |
| `sales_order_items` | 107 |
| 订单增量 | 100 |
| 明细增量 | 100 |
| `legacy_ruida_orders` | 39,922 |
| `legacy_ruida_order_items` | 40,449 |
| `customers` | 132 |
| `products` | 3,316 |
| `users` | 2 |
| 订单迁移台账 | 100 |
| 明细迁移台账 | 100 |
| `integrity_check` | `ok` |
| 验证连接 `foreign_keys` | 1 |
| `foreign_key_check` 异常 | 0 |
| 孤儿正式明细 | 0 |
| 无效 `product_id` | 0 |
| 重复订单台账 | 0 |
| 重复明细台账 | 0 |
| 逐单明细数差异 | 0 |
| 逐单金额差异 | 0 |
| Batch 1 Manifest 缺失订单 | 0 |
| Batch 2 已迁移订单 | 0 |
| 第 32、157 行 rejected 款号进入正式明细 | 0 |
| 费用项进入正式明细 | 0 |

`sales_orders` 和 `sales_order_items` 为 `INTEGER PRIMARY KEY`、非
`AUTOINCREMENT`，`sqlite_sequence` 未新增这两表记录；最大 ID 分别为
104、107，无异常。

## 7. 幂等复跑

输出：

`docs/migration_reports/FORMAL_SALES_SAMPLE_DRY-RUN_20260619_203431.md`

- 计划订单：0
- 计划明细：0
- 已迁移订单：100
- 金额：0.00

Batch 1 幂等复跑归零。

## 8. 后端恢复

- 已恢复 `uvicorn app.main:app`。
- 监听端口：8000。
- 健康检查：正常。
- 后端数据库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- 健康接口：`sales_orders=104`、`sales_order_items=107`
- 前端首页只读访问：HTTP 200。

## 9. 回滚方式

若后续确认 Batch 1 需要整体撤销：

1. 停止后端并确认 8000/8002 无监听。
2. 将当前失败/待调查主库另存为证据，不覆盖。
3. 使用
   `data/backups/carton_erp_before_tianhua_batch_1_apply_20260619_203207.sqlite3`
   恢复 `data/carton_erp.sqlite3`。
4. 校验恢复后 SHA-256 为
   `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`，
   `integrity_check=ok`，正式表恢复为 4/7。
5. 恢复后端并复核健康接口。

## 10. 下一步

本轮到此停止，不执行 `batch_2`。下一最小任务应仅只读验收 Batch 1
在订单列表、订单详情和后续流程页面的显示与查询结果；验收通过后再单独申请
`batch_2` 授权。
