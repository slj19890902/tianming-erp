# P1-125 订单与完工历史容量结构基准

## 边界

- 运行日期：2026-08-29。
- 运行位置：家庭端独立 worktree 的自动生成临时 SQLite 数据库。
- 数据规模：10,000 / 50,000 / 100,000 行；每个规模运行后临时库自动删除。
- 未连接、未迁移、未写入工厂正式数据库或家庭端旧沙盘。
- 本报告验证查询结构和索引增长特性，不替代工厂真实数据库副本上的端到端响应时间验收。

## 命令

```powershell
python scripts/performance/benchmark_history_capacity.py --sizes 10000 50000 100000
```

每个查询预热后执行 12 次，记录中位耗时。数值受机器、SQLite 缓存和临时盘影响，只用于比较同机增长趋势。

## 最终结果

| 生成行数 | 最近订单 50 条 | 订单精确存货编码 | 完工历史中点游标 51 条 | 完工精确存货编码 | 完工历史中点 OFFSET 51 条 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 0.1319 ms | 0.2893 ms | 0.1798 ms | 0.2704 ms | 0.1376 ms |
| 50,000 | 0.0884 ms | 0.3813 ms | 0.0776 ms | 0.2974 ms | 0.3853 ms |
| 100,000 | 0.0811 ms | 0.3001 ms | 0.1904 ms | 0.2758 ms | 0.6122 ms |

结果说明：

- 最近订单、订单/完工精确存货编码和完工游标在 10 倍数据量下保持同一亚毫秒量级，没有随全部历史量近似线性增长。
- 同一临时库中，中点 OFFSET 从 10,000 行的 0.1376 ms 增至 100,000 行的 0.6122 ms；游标仍保持 0.2 ms 以内。
- 这也是生产完工页面从 OFFSET 切换为 `(completed_at, id)` 游标的原因。

## 查询计划证据

- 最近订单：`SEARCH sales_orders USING COVERING INDEX ix_sales_orders_created_at_id (created_at>?)`。
- 订单快照存货编码：使用 `ix_sales_order_items_snapshot_product_code`。
- 产品主数据编码：先使用 `ix_products_product_code` 或 `ix_products_customer_material_code` 找产品，再使用既有 `ix_sales_order_items_product_id` 找订单明细。
- 完工游标：`SEARCH production_completions USING COVERING INDEX ix_production_completions_completed_at_id (completed_at<?)`。
- 完工精确编码：先按上述订单明细索引得到 `order_item_id`，再使用 `ix_production_completions_order_item_id` 定位完工事实。

初版精确编码 SQL 直接从 `sales_order_items JOIN products` 起查，查询计划会扫描全部订单明细；100,000 行中位耗时曾为 13.9858 ms。改为“产品编码索引 → 产品 ID → 订单明细 product_id 索引”的两段查询后，本次最终运行订单查询为 0.3001 ms、完工查询为 0.2758 ms。初版游标使用 `OR` 条件时计划为索引扫描，100,000 行中位曾为 1.7271 ms；改为 SQLite/PostgreSQL 均支持的 `(completed_at, id) < (?, ?)` 行值比较后，本次最终运行为 0.1904 ms。

## 自动回归

- API 测试固定游标稳定排序、无重复/漏行、客户范围、精确编码与非法游标 422。
- migration 测试固定临时库升级、降级、再升级、索引存在性、`integrity_check=ok`、外键异常 0 和关键查询计划命中。
- 订单测试固定旧未完工始终可见、最近 10 天完成、条件化旧历史、精确快照/主数据编码以及先分页后投影。

## 工厂发布前仍需完成

1. 取得正式迁移授权后，在工厂最新 `gs54v8x9z43` 数据库副本执行 `gs54v8x9z43 → gt55v8x9z44 → gs54v8x9z43 → gt55v8x9z44` 往返。
2. 记录副本升级前后核心业务表计数、SHA-256、完整性、外键和查询计划。
3. 用正式规模与真实数据分布测量订单首屏、最近完成、精确存货编码和完工历史前后页。
4. 完成 1920×1080 与大字模式人工页面验收后，才可申请正式发布和正式数据库迁移。
