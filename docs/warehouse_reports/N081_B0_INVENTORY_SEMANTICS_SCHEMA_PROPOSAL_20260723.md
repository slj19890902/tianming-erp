# N081-B0：库存来源、归属、日期可信度与半成品口径方案

日期：2026-07-23

开发基线：
`codex/p0-production-surplus-rebase-20260723@f11d616f419b0bbf9ea47bfa68b41cfd9aedb53e`

## 1. 目标和边界

本阶段只冻结首次盘点必须使用的数据语义，不创建第二套库存余额，也不提前
实现 N080-B 的最低库存、备库生产、无订单直接出库或欠送。

数量权威继续是：

```text
inventory_lots
→ inventory_movements
```

盘点草稿、导入原文、地图和栈板内容均不得成为独立余额。

## 2. 复用的既有事实

- 库存取得来源继续使用 `inventory_lots.source_type`。首次盘点新建库存固定
  使用 `stocktake`；客户备库/通用库存不是来源类型。
- 成品归属继续使用
  `finished_goods_inventory_details.is_general + owner_customer_id`：
  `is_general=true` 表示通用，否则必须有客户。
- 半成品归属继续使用
  `semi_finished_inventory_details.owner_customer_id`：空值表示通用，非空表示
  客户专用。
- 库存类型继续使用 `inventory_lots.inventory_type=finished/semi_finished`。
- 半成品片料类型继续使用
  `raw_board/net_sheet/creased_sheet`，不新建原材料数量账。
- N035 继续只处理已有正式成品批次的复盘；账外库存不得伪装成 N035 调整。

## 3. 本阶段新增字段

在 `inventory_lots` 增加：

```text
stock_date_accuracy
  exact | estimated | unknown

stock_date_original_text
  盘点表或现场记录中的日期原文，可空
```

规则：

- 历史批次统一回填 `unknown`，不根据现有 `stock_date` 猜测可信度。
- 新业务默认 `exact`；估算或不明日期必须显式传入对应可信度。
- `unknown` 仍可把盘点确认日写入技术字段 `stock_date`，但库龄接口和页面不得
  将其展示为精确库龄。
- 人工修改正式入库日期时，可信度重置为 `exact` 并保存 ISO 日期原文。

## 4. 迁移与回退

计划 revision：

```text
ci65v8x9z54
→ cj66v8x9z55
```

SQLite 迁移使用原地 `ADD COLUMN`，不为两列元数据重建 219MB 的
`inventory_lots` 表。新增列先带 `exact` 默认值和枚举约束，再在停服迁移窗口
把所有迁移前既有行统一改为 `unknown`；迁移期间不会并发创建新库存。

降级采用 fail-closed：

- 所有批次仍为迁移回填状态 `unknown`；
- 所有 `stock_date_original_text` 仍为空；

两项同时满足才允许删除字段。只要已经产生新的精确、估算或日期原文事实，
就必须恢复升级前完整备份，不能静默丢字段。

## 5. 后续 B1/B2 契约

- B1 使用 P0-B 私有上传，只支持真实 `.xlsx` 与 UTF-8/GB18030 CSV。
- 查看使用 `warehouse.stocktake.view`；上传、编辑和 dry-run 使用
  `warehouse.stocktake.submit`；B2 试盘确认复用管理员专属
  `warehouse.stocktake.review`。
- B1 只形成草稿和 dry-run，不写正式库存。
- B2 只在隔离副本确认 3～5 个成品栈板和 2～3 个半成品栈板；单区域、
  不超过 100 行、事务全成全败、重复确认幂等。
- B3 工厂分区域正式入账不属于本轮家庭开发授权。

## 6. B0 实施结论

- 手工成品/半成品入库明确保存 `exact + YYYY-MM-DD`。
- 估算日期和不明日期保留原文；不明日期在库存列表与库存洞察中显示
  “日期不明”，`age_days=null`，不进入精确库龄告警。
- 现有库存编辑只有在日期实际改变时才把可信度改为 `exact`；仅改数量、归属
  或库位不会把旧的 `unknown` 偷换成精确日期。
- `cj66v8x9z55` 在 219,447,296 字节工厂副本的再次复制件完成
  `ch64 → ci65 → cj66 → ci65 → cj66`。110 条历史库存均为 `unknown`，
  核心表计数与库存余额汇总不变，完整性为 `ok`、外键异常 0。
