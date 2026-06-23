# 瑞达原始层到正式销售表字段映射与100条试迁移评审

## 执行声明

- 项目根：`D:\纸箱厂erp软件搭建`
- 本轮是否修改主库：否
- 是否写入主库正式业务表：否
- 是否修改正式业务表结构：否
- 是否对副本执行100条试迁移：是
- 副本：`data/sandboxes/carton_erp_formal_mapping_test_20260619_091639.sqlite3`
- 样本清单：`docs/migration_reports/FORMAL_SALES_SAMPLE_IDS_20260619_091652.csv`
- Apply报告：`docs/migration_reports/FORMAL_SALES_SAMPLE_APPLY_20260619_091652.md`
- 幂等复查：`docs/migration_reports/FORMAL_SALES_SAMPLE_DRY-RUN_20260619_091700.md`

## 表结构结论

- `legacy_ruida_orders.customer_id` 已全部映射到正式 `customers.id`，无需重复创建客户。
- `legacy_ruida_order_items.product_archive_id` 覆盖较高，但正式明细要求 `products.id`，不能直接使用档案 ID。
- 正式表没有 `legacy_order_id` / `legacy_item_id` 字段，本轮不改表结构。
- 副本使用独立迁移台账 `migration_ruida_sales_order_map`、`migration_ruida_sales_item_map` 保证幂等；该方案需在全量迁移前评审是否转为正式迁移追踪字段或保留台账。

## 订单字段映射

| 原始字段/规则 | 正式字段 | 处理 |
|---|---|---|
| `legacy_order_id` | `order_number` | 生成 `RUIDA-<legacy_order_id>`，稳定且唯一 |
| `customer_id` | `customer_id` | 直接映射；为空则整单跳过 |
| 首个非空明细 `customer_order_no`，回退 `po` | `customer_po` | 清洗空白，长度由正式表限制 |
| 明细最早 `order_date` | `order_date` | 解析斜杠/ISO文本为日期 |
| 明细最晚非空 `delivery_date` | `delivery_date` | 无值允许为空 |
| 固定值 | `status` | `pending_production` |
| 固定值 | `payment_status` | `unpaid` |
| 明细 `amount` 按订单求和 | `total_amount` | Decimal量化到2位 |
| `remark` + 源ID标识 | `remark` | 前缀 `[瑞达历史订单 <ID>]` |
| 无可靠来源 | `created_by` | `NULL` |
| 系统生成 | `created_at` | SQLite默认当前时间 |

`approved/approved_at/approved_by/image_path/source_updated_at/source_json` 暂不迁入正式订单；它们继续保留在原始层。

## 明细字段映射

| 原始字段/规则 | 正式字段 | 处理 |
|---|---|---|
| 迁移后的订单ID | `order_id` | 通过订单台账关联 |
| 客户 + `style_no` 精确匹配产品编码 | `product_id` | 必须唯一匹配，否则整单跳过 |
| `order_quantity` | `quantity` | 必须大于0 |
| `unit_price` | `unit_price` | Decimal量化到4位，禁止负数 |
| `amount` | `subtotal` | 使用源金额，Decimal量化到2位 |
| 固定值 | `material_status` | `pending` |
| `products.product_name` | `snapshot_product_name` | 产品快照 |
| `length_mm×width_mm×height_mm` | `snapshot_spec` | 生成 `L×W×Hmm` |
| `material` | `snapshot_material` | 保留瑞达原始材质文本 |
| `products.product_code` | `snapshot_product_code` | 产品编码快照 |
| 固定值 | `delivered_quantity` | 0 |
| 固定值 | `is_force_closed` | 0 |
| 固定值 | `inventory_deducted_qty` | 0 |
| 固定值 | `requisition_status` | `未报料` |
| 固定值 | `special_process` | `无` |

其余采购、供应商、印刷、模切、唛头和工艺字段暂不迁入正式订单明细，继续保留在原始层，后续按业务需要映射。

## 客户匹配策略

1. 优先使用 `legacy_ruida_orders.customer_id` 和明细 `customer_id`。
2. 当前订单客户覆盖率为100%，因此不创建新客户。
3. 禁止仅按客户名称匹配，因为存在重名。
4. customer_id为空或订单与明细客户不一致时整单进入跳过报告。

## 产品、规格和材质策略

1. 产品仅按同一 `customer_id` 下：
   - `style_no = products.product_code`，或
   - `style_no = products.customer_material_code`
2. 必须唯一匹配；0个或多个匹配均整单跳过，不自动创建占位产品。
3. 当前40,449条明细中可唯一匹配10,145条；完整候选订单9,846个。
4. 规格由原始长宽高生成快照，不覆盖产品主数据。
5. 材质保留到 `snapshot_material`；本轮不强制映射 `materials.id`，避免材质文本清洗误配。

## 日期、金额和状态规则

- 日期支持 `YYYY/M/D H:mm:ss`、`YYYY-MM-DD HH:mm:ss`、`YYYY-MM-DD`，统一写ISO日期。
- 订单日期取明细最早下单日，交货日期取最晚交货日。
- 单价使用Decimal四舍五入到4位，金额到2位。
- 订单总额以源明细金额合计为准；不以浮点数计算。
- 默认状态为待生产、未付款、待报料、材料未到、已送数量0。

## 幂等设计

- 订单第一道防重：确定性 `order_number = RUIDA-<legacy_order_id>`，正式表已有唯一约束。
- 订单台账：`migration_ruida_sales_order_map(legacy_order_id PRIMARY KEY, sales_order_id UNIQUE)`。
- 明细台账：`migration_ruida_sales_item_map(legacy_item_id PRIMARY KEY, sales_order_item_id UNIQUE, legacy_order_id)`。
- 先写订单，再用实际 `sales_order_id` 写全部明细；整个样本单事务提交，失败全部回滚。
- 正式表当前没有源ID字段。全量迁移前建议二选一：
  1. 保留独立迁移台账作为审计和幂等依据（推荐，避免改业务表）；
  2. 经正式数据库变更评审后新增 nullable 源字段及唯一索引。

## 100条样本规则

- 模式：`complete_orders`
- 从最新 `legacy_order_id` 向前选择。
- 必须有客户、有明细、日期有效、数量大于0、金额非负。
- 所有明细必须唯一匹配正式产品；任一明细失败则整单跳过。
- 固定选择前100个完整候选，不因已迁移而滑动到下一批，保证复跑能验证同一批幂等。

## 副本测试结果

| 项目 | 测试前 | 测试后 | 变化 |
|---|---:|---:|---:|
| `sales_orders` | 4 | 104 | +100 |
| `sales_order_items` | 7 | 107 | +100 |
| `legacy_ruida_orders` | 39,922 | 39,922 | 0 |
| `legacy_ruida_order_items` | 40,449 | 40,449 | 0 |

- 样本订单：100；实际明细：100。
- 样本金额：165,528.97。
- 台账订单/明细：100/100。
- `integrity_check = ok`。
- 孤儿明细、无效产品外键、负数量/金额：均为0。
- Apply后再次dry-run：计划订单0、明细0，已导入100。
- 主库SHA-256保持 `6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77`。

## 风险点

- 产品唯一匹配覆盖不足，不能直接全量导入；约30,304条明细需产品映射补全或冲突处理。
- 样本恰好均为单明细订单，后续应增加多明细专项样本验证。
- `subtotal`使用源金额，可能与`quantity × unit_price`存在历史舍入差异。
- 历史状态无法完整还原，本方案默认进入待生产流程；全量导入前需决定历史订单是否应以只读/已完成状态呈现。
- 台账表目前只存在副本，不构成主库结构授权。

## 回滚方式

本轮只写副本。回滚方式为停止使用并保留副本作为证据；不删除文件。主库无需回滚。

## 下一步建议

下一最小任务：只读统计产品未匹配原因，并设计产品映射补全规则；同时选取包含多明细订单的第二批专项样本。未经新授权不得写主库正式表。
