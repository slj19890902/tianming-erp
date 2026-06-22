# 天华主库 Batch 5 正式迁移与最终总对账

- 执行时间：2026-06-20 13:52:53
- 本轮是否执行主库 apply：是
- 执行范围：仅 `batch_5`
- 是否全量滑批：否

## 1. 备份与 apply 前状态

- 主库：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- 备份：`D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_tianhua_batch_5_apply_20260620_134925.sqlite3`
- Apply 前主库 SHA-256：`9C7E6BF7CAE37E02DD573F328250A53D075FD8E578C28BC6AB204EB029C13BD1`
- 备份 SHA-256：`9C7E6BF7CAE37E02DD573F328250A53D075FD8E578C28BC6AB204EB029C13BD1`
- 主库与备份 SHA-256 是否一致：是
- Apply 前主库大小：`214,667,264`
- Apply 前 `integrity_check=ok`
- 锁探针：`BEGIN IMMEDIATE / ROLLBACK` 成功
- 写连接 `PRAGMA foreign_keys=1`

Apply 前关键数量：

- `sales_orders = 11104`
- `sales_order_items = 11169`
- `RUIDA- = 11100`
- `legacy_ruida_orders = 39922`
- `legacy_ruida_order_items = 40449`
- `customers = 132`
- `products = 3316`
- `users = 2`
- Batch 1 / 2 / 3 / 4 / 5 已迁移订单数 = `100 / 1000 / 5000 / 5000 / 0`

## 2. batch_5 dry-run 结果

命令包含：

- `--sqlite-path .\data\carton_erp.sqlite3`
- `--product-review-csv .\docs\migration_reports\PRODUCT_PREFIX_TIANHUA_REVIEW_WORKSHEET_APPROVED_EXCEPT_32_157_FIXED.csv`
- `--batch-manifest .\docs\migration_reports\TIANHUA_MAIN_BATCH_MANIFEST_20260619_175953.csv`
- `--batch-id batch_5`
- `--customer-name 苏州天华超净科技股份有限公司`

Dry-run 输出：

- 计划订单数：`4489`
- 计划明细数：`4489`
- Approved prefix 明细数：`4102`
- 精确匹配明细数：`387`
- 多明细订单数：`0`
- 单明细订单数：`4489`
- 金额合计：`4,918,380.12`
- Rejected style 命中：`70`
- 实际选入 rejected 明细：`0`
- 费用项选入：`0`
- 未匹配明细选入：`0`
- 已导入数：`0`
- 插入数：`0 / 0`

跳过原因：

- `product_unmatched_or_ambiguous = 8527`
- `rejected_product_review = 70`
- `suspected_fee_item = 1`

结论：dry-run 与 immutable manifest 的 `batch_5` 固定余量一致，未发现滑批。

## 3. batch_5 apply 结果

主库 apply 命令额外包含：

- `--expected-source-db-sha256 9C7E6BF7CAE37E02DD573F328250A53D075FD8E578C28BC6AB204EB029C13BD1`
- `--confirm-main-apply APPLY_TIANHUA_FORMAL_SALES`
- `--allow-main-sandbox`
- `--apply`

Apply 输出：

- 新增订单：`4489`
- 新增明细：`4489`
- 金额：`4,918,380.12`

Apply 前后正式表数量：

- `sales_orders: 11104 -> 15593`
- `sales_order_items: 11169 -> 15658`

Apply 后主库：

- SHA-256：`162450A396E1CC4A439618084501E915D181201BA8C3849D0836C0AF50ECDCAE`
- 大小：`217,415,680`
- 修改时间：`2026-06-20T13:50:59`

## 4. Apply 后单批校验

- `PRAGMA integrity_check = ok`
- `PRAGMA foreign_keys = 1`
- `PRAGMA foreign_key_check = 0`
- 孤儿正式明细：`0`
- 无效 `product_id`：`0`
- 重复订单台账：`0`
- 重复明细台账：`0`
- 重复 `order_number`：`0`
- 正式明细 `quantity <= 0`：`0`
- batch_5 每单明细数差异：`0`
- batch_5 每单金额差异：`0`
- batch_5 legacy 金额合计：`4,918,380.12`
- batch_5 sales 金额合计：`4,918,380.12`

排除项校验：

- 第 32 行 raw 款号 `21301021 / 中性内箱26*45THH10` 进入正式明细：`0`
- 第 157 行 raw 款号 `21302061 / 满衬板24*36` 进入正式明细：`0`
- 费用项命中：`0`
- 未匹配明细进入计划：`0`

说明：

- 存在 43 条已迁移明细与第 157 行同前缀码 `21302061` 的其他变体款号相关，但并非该 rejected raw 款号本身；该现象不构成“第 157 行 raw 款号进入正式明细”。

幂等复跑：

- batch_5 dry-run 复跑：`0 张订单 / 0 条明细`
- 全量剩余 dry-run（`--all`）：`0 张订单 / 0 条明细`

## 5. 最终全量迁移合计

最终正式表：

- `sales_orders = 15593`
- `sales_order_items = 15658`
- `RUIDA-` 历史订单 = `15589`

迁移台账：

- 订单台账：`15589`
- 明细台账：`15651`

五批订单数：

- Batch 1 / 2 / 3 / 4 / 5 = `100 / 1000 / 5000 / 5000 / 4489`

五批明细数：

- Batch 1 / 2 / 3 / 4 / 5 = `100 / 1006 / 5054 / 5002 / 4489`
- 合计：`15651`

与 5 批副本演练对照：

- 最终订单合计一致：`15589`
- 最终明细合计一致：`15651`
- 正式表总数一致：`15593 / 15658`
- 结论：与 5 批副本演练一致

## 6. 后端恢复验证

- 后端已恢复
- 健康检查：`ok`
- 后端数据库路径：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
- `orders_table = sales_orders`
- API 当前订单数：`15593`
- API 当前明细数：`15658`
- `GET /api/orders?keyword=RUIDA-`：`15589`
- 样例详情 `GET /api/orders/15593` 返回 `200`

## 7. 性能初步观察

- `/api/health` 恢复后正常
- `GET /api/orders?keyword=RUIDA-&page=1&page_size=1` 正常返回 `15589`
- 最终只读验收前，仍建议按 Batch 4 验收模板再做一次 API / 搜索 / 分页 / 详情 / 页面性能全量只读复核

## 8. 回滚方式

1. 停止后端服务
2. 保留当前失败现场库，不覆盖
3. 使用时点备份恢复：
   `data/backups/carton_erp_before_tianhua_batch_5_apply_20260620_134925.sqlite3`
4. 恢复后执行：
   - `PRAGMA integrity_check`
   - `PRAGMA foreign_key_check`
   - `sales_orders / sales_order_items / RUIDA-` 数量复核

## 9. 结论与下一步建议

- 本轮只执行了 `batch_5`
- 未发生全量滑批
- 最终全量迁移结果与 5 批副本演练一致
- 当前建议进入“最终只读验收”
- 本轮后不要宣布项目结束；仍需完成：
  1. 最终只读验收
  2. 最终备份归档
