# 天华超净主库正式迁移 Batch 2 执行报告

- 执行时间：2026-06-20 07:48:06（Asia/Shanghai）
- 项目目录：`D:\纸箱厂erp软件搭建`
- 本轮是否执行主库 apply：是
- 执行范围：仅 `batch_2`
- 是否执行 `batch_3`：否
- 是否执行 `batch_4` / `batch_5` / 全量迁移：否

## 1. 停机与锁检查

- 已停止 `uvicorn app.main:app` 父子进程。
- 8000、8002、8004 均无监听。
- 未发现剩余后端进程。
- SQLite `BEGIN EXCLUSIVE` 锁探针成功。
- 写连接执行 `PRAGMA foreign_keys=ON` 后返回 `1`。

## 2. Batch 2 时点备份

`D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_tianhua_batch_2_apply_20260620_074551.sqlite3`

| 项目 | 结果 |
|---|---|
| 文件大小 | 209,182,720 字节 |
| 修改时间 UTC | `2026-06-19T12:32:46.3713850Z` |
| 主库 SHA-256 | `2EBFB2F9A0BE2B7F50A08E114B44532C25B7B497505C3CDAB73ACFE66A67EFD3` |
| 备份 SHA-256 | `2EBFB2F9A0BE2B7F50A08E114B44532C25B7B497505C3CDAB73ACFE66A67EFD3` |
| 哈希一致 | 是 |
| 主库 / 备份 `integrity_check` | `ok` / `ok` |

## 3. Apply 前基线

| 项目 | 数量 |
|---|---:|
| `sales_orders` | 104 |
| `sales_order_items` | 107 |
| `legacy_ruida_orders` | 39,922 |
| `legacy_ruida_order_items` | 40,449 |
| `customers` | 132 |
| `products` | 3,316 |
| `users` | 2 |
| Batch 1 已迁移订单 | 100 |
| Batch 2 已迁移订单 | 0 |
| Batch 3 已迁移订单 | 0 |

## 4. Batch 2 Dry-run

输出：

- `docs/migration_reports/FORMAL_SALES_SAMPLE_DRY-RUN_20260620_074606.md`
- `docs/migration_reports/FORMAL_SALES_SAMPLE_IDS_20260620_074606.csv`

| 项目 | 结果 |
|---|---:|
| 计划订单 | 1,000 |
| 计划明细 | 1,006 |
| 金额 | 1,230,739.83 |
| Approved prefix | 822 |
| 精确匹配 | 184 |
| 多明细订单 | 5 |
| Rejected 明细进入计划 | 0 |
| 费用项进入计划 | 0 |
| 未匹配明细进入计划 | 0 |
| 已迁移 Batch 2 订单 | 0 |

结果与五批副本演练中的 Batch 2 完全一致。

## 5. Batch 2 Apply

Apply 前 SHA-256：

`2EBFB2F9A0BE2B7F50A08E114B44532C25B7B497505C3CDAB73ACFE66A67EFD3`

命令仅使用：

- `--batch-id batch_2`
- `--expected-source-db-sha256` 完整实时哈希
- `--confirm-main-apply APPLY_TIANHUA_FORMAL_SALES`
- `--allow-main-sandbox`
- `--apply`

输出：

- `docs/migration_reports/FORMAL_SALES_SAMPLE_APPLY_20260620_074631.md`
- `docs/migration_reports/FORMAL_SALES_SAMPLE_IDS_20260620_074631.csv`

实际新增：

- 订单：1,000
- 明细：1,006
- 金额：1,230,739.83
- Prefix：822
- 精确：184

Apply 后 SHA-256：

`120535ADC2E3E9EFB80625283475F8D3B98E95FCD549DD43AE45BDD673373581`

## 6. Apply 后校验

| 检查 | 结果 |
|---|---:|
| `sales_orders` | 1,104 |
| `sales_order_items` | 1,113 |
| Batch 2 订单增量 | 1,000 |
| Batch 2 明细增量 | 1,006 |
| 订单迁移台账合计 | 1,100 |
| 明细迁移台账合计 | 1,106 |
| Batch 1 已迁移订单 | 100 |
| Batch 2 已迁移订单 | 1,000 |
| Batch 3 / 4 / 5 已迁移订单 | 0 / 0 / 0 |
| `integrity_check` | `ok` |
| 验证连接 `foreign_keys` | 1 |
| `foreign_key_check` 异常 | 0 |
| 孤儿正式明细 | 0 |
| 无效 `product_id` | 0 |
| 重复订单台账 | 0 |
| 重复明细台账 | 0 |
| Batch 2 逐单明细/金额差异 | 0 |
| Rejected 款号进入正式明细 | 0 |
| 费用项进入正式明细 | 0 |

`sales_orders` / `sales_order_items` 仍为非 AUTOINCREMENT 的
`INTEGER PRIMARY KEY`；`sqlite_sequence` 未新增两表记录，最大 ID 为
1,104 / 1,113，无异常。

## 7. 幂等复跑

输出：

`docs/migration_reports/FORMAL_SALES_SAMPLE_DRY-RUN_20260620_074714.md`

- 计划订单：0
- 计划明细：0
- 已迁移订单：1,000
- 金额：0.00

Batch 2 幂等复跑归零。

## 8. 后端恢复

- 已恢复 `uvicorn app.main:app`。
- 后端数据库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- 健康接口：订单 1,104、明细 1,113。
- 前端首页 GET：HTTP 200。
- `GET /api/orders?keyword=RUIDA-` 返回 1,100 张历史订单。
- 样例订单详情 GET 返回 200。

## 9. 回滚方式

1. 停止后端并确认端口无监听。
2. 保留当前主库为失败现场证据。
3. 用
   `data/backups/carton_erp_before_tianhua_batch_2_apply_20260620_074551.sqlite3`
   恢复 `data/carton_erp.sqlite3`。
4. 校验恢复后 SHA-256 为
   `2EBFB2F9A0BE2B7F50A08E114B44532C25B7B497505C3CDAB73ACFE66A67EFD3`，
   `integrity_check=ok`，正式表为 104/107。
5. 恢复后端并复核健康接口。

## 10. 下一步

本轮到此停止，不执行 Batch 3。下一最小任务应先只读验收 Batch 2 的 API、
搜索、分页、详情和页面加载性能；通过后再单独申请 Batch 3 授权。
