# 天华超净 Batch 4 主库正式迁移

- 执行时间：2026-06-20 13:05:27（Asia/Shanghai）
- 项目目录：`D:\纸箱厂erp软件搭建`
- 本轮是否执行主库 apply：是
- 执行范围：仅 `batch_4`
- 是否执行 `batch_5`：否
- 是否执行全量迁移：否

## 1. 执行前门禁

| 检查项 | 结果 |
|---|---|
| 当前目录 | `D:\纸箱厂erp软件搭建` |
| 后端停机 | 已停止 `uvicorn app.main:app` |
| 监听端口 | 8000 / 8002 / 8004 均已关闭 |
| SQLite 独占锁探针 | 通过 |
| 写连接 `PRAGMA foreign_keys` | `1` |
| 主库完整性 | `ok` |
| 主库 `foreign_key_check` | `0` |

## 2. 时点备份

| 项目 | 值 |
|---|---|
| 备份文件 | `data/backups/carton_erp_before_tianhua_batch_4_apply_20260620_130056.sqlite3` |
| 主库 SHA-256 | `1BE0238B6C5B9AD8BAA48488AB2DD7B3283B1D56D40E5460CF18BD4C408EC1FF` |
| 备份 SHA-256 | `1BE0238B6C5B9AD8BAA48488AB2DD7B3283B1D56D40E5460CF18BD4C408EC1FF` |
| 主库与备份 SHA-256 | 一致 |
| 主库大小 | `211,509,248` 字节 |
| 备份大小 | `211,509,248` 字节 |
| 备份完整性 | `ok` |

## 3. Apply 前主库状态

| 项目 | 值 |
|---|---:|
| `sales_orders` | 6,104 |
| `sales_order_items` | 6,167 |
| `legacy_ruida_orders` | 39,922 |
| `legacy_ruida_order_items` | 40,449 |
| `customers` | 132 |
| `products` | 3,316 |
| `users` | 2 |
| `RUIDA-` 历史订单 | 6,100 |
| Batch 1 / 2 / 3 / 4 / 5 | 100 / 1,000 / 5,000 / 0 / 0 |

`sqlite_sequence` 中不存在 `sales_orders` 或 `sales_order_items` 条目。

## 4. Batch 4 Dry-run

| 指标 | 结果 |
|---|---:|
| 计划订单数 | 5,000 |
| 计划明细数 | 5,002 |
| 金额合计 | 16,331,793.23 |
| Approved prefix 明细数 | 4,533 |
| 精确匹配明细数 | 469 |
| 多明细订单数 | 2 |
| 单明细订单数 | 4,998 |
| `already_imported` | 0 |
| 未匹配明细进入计划 | 0 |
| Rejected 款号进入计划 | 0 |
| 费用项进入计划 | 0 |

Dry-run 与 immutable manifest 及 5 批副本演练的 `5,000 / 5,002 / 16,331,793.23` 一致。

## 5. Batch 4 Apply

| 指标 | 结果 |
|---|---:|
| 新增订单数 | 5,000 |
| 新增明细数 | 5,002 |
| Apply 后主库 SHA-256 | `9C7E6BF7CAE37E02DD573F328250A53D075FD8E578C28BC6AB204EB029C13BD1` |
| 正式表变化 | `6,104/6,167 -> 11,104/11,169` |

## 6. Apply 后校验

| 检查项 | 结果 |
|---|---|
| `PRAGMA integrity_check` | `ok` |
| `PRAGMA foreign_keys` | `1` |
| `PRAGMA foreign_key_check` | `0` |
| `RUIDA-` 历史订单 | 11,100 |
| Batch 1 / 2 / 3 / 4 / 5 | 100 / 1,000 / 5,000 / 5,000 / 0 |
| 无孤儿正式明细 | 是 |
| 无无效 `product_id` | 是 |
| 无重复订单台账 | 是 |
| 无重复明细台账 | 是 |
| 无重复 `order_number` | 是 |
| 无数量小于等于 0 的正式明细 | 是 |
| Batch 4 逐单明细数差异 | 0 |
| Batch 4 逐单金额差异 | 0 |
| Rejected 第 32、157 行款号进入正式明细 | 0 |
| 费用项进入正式明细 | 0 |
| `sqlite_sequence` 异常 | 无 |

## 7. 幂等复跑

| 指标 | 结果 |
|---|---:|
| 计划订单数 | 0 |
| 计划明细数 | 0 |
| `already_imported` | 5,000 |

结论：`batch_4` 幂等复跑归零。

## 8. 后端恢复与只读验证

后端已恢复为：

`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`

| 检查项 | 结果 |
|---|---|
| `GET /api/health` | 200 |
| 健康接口订单/明细 | 11,104 / 11,169 |
| `GET /api/orders?keyword=RUIDA-` | 11,100 |
| 样例详情 `GET /api/orders/6105` | 200 |

性能初步观察：

- `GET /api/health` 约 `5.50 ms`
- `GET /api/orders?keyword=RUIDA-` 约 `24.49 ms`
- `GET /api/orders/6105` 约 `10.26 ms`

## 9. 回滚方式

1. 停止后端服务并确认 SQLite 无占用。
2. 保留当前失败现场库，不覆盖。
3. 将 `data/backups/carton_erp_before_tianhua_batch_4_apply_20260620_130056.sqlite3` 恢复为 `data/carton_erp.sqlite3`。
4. 恢复后执行 `PRAGMA integrity_check`、`PRAGMA foreign_key_check` 和正式表数量复核。
5. 未重新完成 dry-run 前，不得继续 `batch_5`。

## 10. 下一步建议

1. 先做 Batch 4 主库只读验收，重点看 API、搜索、分页、详情和页面性能。
2. 未经新的明确授权，不执行 `batch_5`。
