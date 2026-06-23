# 天华超净主库正式迁移 Batch 3 执行报告

- 执行时间：2026-06-20 08:59:43（Asia/Shanghai）
- 项目目录：`D:\纸箱厂erp软件搭建`
- 本轮是否执行主库 apply：是
- 执行范围：仅 `batch_3`
- 是否执行 `batch_4`：否
- 是否执行 `batch_5`：否
- 是否执行全量迁移：否

## 1. 停机与安全门禁

- 已停止 `uvicorn app.main:app`。
- 8000、8002、8004 端口均无后端监听。
- 未发现剩余后端进程。
- SQLite `BEGIN EXCLUSIVE` 独占锁探针成功。
- 写连接执行 `PRAGMA foreign_keys=ON` 后返回 `1`。

## 2. Batch 3 时点备份

`D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_tianhua_batch_3_apply_20260620_085704.sqlite3`

| 项目 | 结果 |
|---|---|
| 主库/备份大小 | 209,182,720 / 209,182,720 字节 |
| 主库/备份 SHA-256 | `120535ADC2E3E9EFB80625283475F8D3B98E95FCD549DD43AE45BDD673373581` |
| 哈希一致 | 是 |
| 主库/备份 `integrity_check` | `ok` / `ok` |

## 3. Apply 前基线

| 项目 | 数量 |
|---|---:|
| `sales_orders` | 1,104 |
| `sales_order_items` | 1,113 |
| `legacy_ruida_orders` | 39,922 |
| `legacy_ruida_order_items` | 40,449 |
| `customers` | 132 |
| `products` | 3,316 |
| `users` | 2 |
| Batch 1 / 2 / 3 | 100 / 1,000 / 0 |
| Batch 4 / 5 | 0 / 0 |

Apply 前主库 SHA-256：

`120535ADC2E3E9EFB80625283475F8D3B98E95FCD549DD43AE45BDD673373581`

## 4. Batch 3 Dry-run

输出：

- `docs/migration_reports/FORMAL_SALES_SAMPLE_DRY-RUN_20260620_085721.md`
- `docs/migration_reports/FORMAL_SALES_SAMPLE_IDS_20260620_085721.csv`

| 项目 | 结果 |
|---|---:|
| 计划订单 | 5,000 |
| 计划明细 | 5,054 |
| 金额 | 8,590,604.37 |
| Approved prefix | 4,638 |
| 精确匹配 | 416 |
| 多明细订单 | 43 |
| Rejected 明细进入计划 | 0 |
| 费用项进入计划 | 0 |
| 未匹配明细进入计划 | 0 |
| 已迁移 Batch 3 订单 | 0 |

结果与不可变 manifest 和五批副本演练中的 Batch 3 完全一致。

## 5. Batch 3 Apply

命令仅使用：

- `--batch-id batch_3`
- 显式天华客户名称和已审核 review CSV
- 实时完整 `--expected-source-db-sha256`
- `--confirm-main-apply APPLY_TIANHUA_FORMAL_SALES`
- `--allow-main-sandbox`
- `--apply`

输出：

- `docs/migration_reports/FORMAL_SALES_SAMPLE_APPLY_20260620_085755.md`
- `docs/migration_reports/FORMAL_SALES_SAMPLE_IDS_20260620_085755.csv`

实际新增：

- 订单：5,000
- 明细：5,054
- 金额：8,590,604.37
- Prefix：4,638
- 精确：416

Apply 后 SHA-256：

`1BE0238B6C5B9AD8BAA48488AB2DD7B3283B1D56D40E5460CF18BD4C408EC1FF`

## 6. Apply 后校验

| 检查 | 结果 |
|---|---:|
| `sales_orders` | 6,104 |
| `sales_order_items` | 6,167 |
| Batch 3 订单/明细增量 | 5,000 / 5,054 |
| 订单/明细迁移台账合计 | 6,100 / 6,160 |
| Batch 1 / 2 / 3 | 100 / 1,000 / 5,000 |
| Batch 4 / 5 | 0 / 0 |
| `integrity_check` | `ok` |
| 验证连接 `foreign_keys` | 1 |
| `foreign_key_check` 异常 | 0 |
| 孤儿正式明细 | 0 |
| 无效 `product_id` | 0 |
| 重复订单/明细台账 | 0 / 0 |
| 重复 `order_number` | 0 |
| 数量小于等于 0 | 0 |
| Batch 3 逐单明细数差异 | 0 |
| Batch 3 源金额/明细金额差异 | 0 |
| Batch 3 源金额/订单金额差异 | 0 |
| 第 32、157 行款号迁移数 | 0 / 0 |
| 费用项迁移数 | 0 |

`sales_orders` / `sales_order_items` 仍未出现在 `sqlite_sequence` 中，
最大 ID 为 6,104 / 6,167，无 sequence 异常。

## 7. 幂等复跑

输出：

`docs/migration_reports/FORMAL_SALES_SAMPLE_DRY-RUN_20260620_085856.md`

- 计划订单：0
- 计划明细：0
- 已迁移订单：5,000
- 金额：0.00

Batch 3 幂等复跑归零。

## 8. 后端恢复与性能初查

- 已恢复 `uvicorn app.main:app`。
- 后端数据库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- 健康接口：订单 6,104、明细 6,167。
- `GET /api/orders?keyword=RUIDA-`：6,100 张。
- Batch 3 样例 `RUIDA-39948` 详情 GET：HTTP 200。
- 10 次中间页列表 GET：中位 16.09 ms，最大 57.56 ms；首次恢复后的
  列表请求为 81.01 ms，未出现超时。

## 9. 回滚方式

1. 停止后端并确认端口无监听。
2. 保留当前主库为失败现场证据。
3. 使用
   `data/backups/carton_erp_before_tianhua_batch_3_apply_20260620_085704.sqlite3`
   恢复 `data/carton_erp.sqlite3`。
4. 校验恢复后 SHA-256 为
   `120535ADC2E3E9EFB80625283475F8D3B98E95FCD549DD43AE45BDD673373581`，
   `integrity_check=ok`，正式表为 1,104/1,113。
5. 恢复后端并复核健康接口。

## 10. 下一步

本轮到此停止，不执行 Batch 4。下一最小任务是 Batch 3 只读 API、搜索、
分页、详情和页面性能验收；验收通过后再单独申请 Batch 4 授权。
