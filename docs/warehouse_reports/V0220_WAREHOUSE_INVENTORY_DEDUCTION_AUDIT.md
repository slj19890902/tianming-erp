# v0.22.0 仓库成品/半成品库存抵扣审计报告

审计日期：2026-07-01  
当前分支：`feature/v0208-common-box-edit`  
审计基线：`09b05cd hotfix: restore Tianhua common boxes after quotation conversion`  
Alembic：单一 head `b19t6u7v8w18`

## 结论摘要

1. 当前生产系统没有库存主表、库存批次表、库存流水表、库存预占表或库位主数据表。
2. “仓库来料入库”当前不是库存系统。它把订单明细从“已报料、待收料”改成“已入库”，并记录操作日志。
3. 当前报料中的“库存抵扣”只是订单明细和报料单上的手工数字，没有库存来源、余额校验、预占、防重复抵扣或流水。
4. 正式库四处抵扣字段的非零记录均为 0，因此后续不需要迁移历史库存抵扣，但也不能把这些字段当成真实库存。
5. 成品库存抵扣应插在“订单明细保存后、生成报料前”；半成品库存抵扣应插在“计算纸板需求后、生成/合并供应商报料单前”。
6. 推荐新增统一库存批次父表、成品/半成品明细表、预占表、流水表和库位表。所有余额变化必须通过一个后端库存服务在同一事务中完成。
7. 第一版不能直接实施，必须先确认半成品库存的数量单位/开料产出、成品出库时点和通用成品匹配规则。

## 1. 当前相关模块现状

### 1.1 仓库来料入库

相关文件：

- `app/api/incoming.py`
- `static/index.html`
- `static/incoming.html`
- `app/models/order.py`
- `app/models/requisition.py`
- `tests/test_phase6_incoming.py`

当前能力：

- 老板端和手机端共用 `/api/incoming`。
- 待入库条件：
  - `sales_order_items.material_status = pending`
  - `requisition_status IN (已报料, 供应商已排单)`
- 支持单条入库、最多200条批量入库、最近24小时入库、历史入库和撤回。
- 入库时允许人工修改本次入库数量。
- 通过条件更新和 `rowcount` 防止同一订单明细重复确认入库。
- 批量入库使用保存点，允许部分成功并逐条返回原因。
- 入库写 `operation_logs`，撤回也写操作日志。
- 订单全部明细入库后，订单可能转为 `pending_delivery`。
- 已发货订单禁止撤回来料入库。

当前局限：

- 没有物理库存批次和余额。
- 没有部分入库流水；修改入库数量会覆盖 `requisition_qty`。
- 没有库位、供应商批次、质检、破损、报损、冻结或库存调拨。
- `operation_logs` 是操作审计，不是库存流水，不能重建库存余额。
- 入库对象仍是订单明细，不是可被后续订单复用的半成品库存。

### 1.2 报料管理

相关文件：

- `app/api/requisition.py`
- `app/models/requisition.py`
- `alembic/versions/b71c4a9e2d10_phase11_requisition_workflow.py`
- `static/index.html`

当前数量公式：

```text
每箱片数 = snapshot_pieces_per_box，缺失时按单拼1片、双拼2片
需求小片数 = 订单成品数量 × 每箱片数
开料数 = 一开一至一开五对应 1 至 5
采购张数 = ceil((需求小片数 - 手工库存抵扣小片数) / 开料数)
采购长 = 单片报料长
采购宽 = 单片报料宽 × 开料数
```

当前字段：

- `sales_order_items.inventory_deducted_qty`
- `sales_order_items.requisition_qty`
- `material_requisition_items.inventory_deducted_qty`
- `material_requisition_items.requisition_qty`

当前“库存抵扣”允许用户直接填写数字，后端只检查：

- 抵扣数不能小于0。
- 抵扣数不能超过需求小片数。

当前缺失：

- 不知道抵扣来自哪一批库存。
- 不检查库存是否存在或是否足够。
- 不检查楞型、尺寸、压线、材质差异。
- 不预占库存，同一数字可以被多个订单重复使用。
- 取消报料仅把抵扣数字清零，没有库存释放记录。

### 1.3 合并报料

当前合并键包括：

```text
供应商
material_id
层数
楞型
单片报料长
单片报料宽
每箱片数
拼箱方式
压线类型
三段压线尺寸
```

优点：

- 不同楞型不会错误合并。
- 不同单片尺寸、拼箱方式和压线不会错误合并。

风险：

- 合并建议中的库存抵扣仍是一个无来源的手工数字。
- 前端把抵扣按成员依次分摊，但没有库存批次、预占或并发校验。
- 合并建议只返回成员数大于1的组；单条报料走另一套批次路径。

### 1.4 供应商采购报料单打印

相关表：

- `supplier_requisition_orders`
- `supplier_requisition_order_items`

打印数量：

- 显示 `requisition_qty`，即本次采购张数。
- 不显示订单数量、送货数量、对账数量或内部成本。
- 材质显示为业务明细的“材质代码 / 楞型”。

当前作废逻辑：

- 报料单状态改为 `voided`。
- 关联订单明细退回“未报料”。
- 抵扣数字和采购张数清空。

后续接库存后，作废必须同步处理预占，不能只清空数字。

### 1.5 订单与常用箱

订单数量来源：

- `sales_order_items.quantity`，是客户成品订单数量。

常用箱提供并在下单时固化：

- 客户、存货编码、产品名称、箱型、长宽高。
- `material_id`、材质代码、供应商、层数、楞型。
- 单片报料长宽、压线、拼箱方式、每箱片数、舌头。

重要边界：

- 订单保存后使用快照，不应被后续常用箱修改静默覆盖。
- 成品/半成品库存抵扣不得修改 `sales_order_items.quantity`。
- 当前订单撤回会删除报料关联并把 `inventory_deducted_qty` 清零；接入真实库存后必须先释放有效预占。

### 1.6 材质与楞型

- 材质字典保存供应商、层数、材质代码、结构、克重和价格。
- 材质字典不绑定楞型。
- 楞型来自常用箱和订单业务明细。
- 三层允许 A/B/E，五层允许 AB/BE。
- 半成品库存匹配必须使用库存批次楞型和订单明细楞型，不能从材质字典回填。

### 1.7 当前数量口径

| 数量 | 当前来源/计算 | 库存模块接入后的边界 |
|---|---|---|
| 客户订单数量 | `sales_order_items.quantity` | 永远不改 |
| 需求小片数 | 订单数量 × 每箱片数 | 应先减成品抵扣后的需生产数量再计算 |
| 采购张数 | `ceil((需求小片数 - 手工抵扣数) / 开料数)` | 手工抵扣数改为有效半成品预占折算值 |
| 来料入库数量 | 默认采购张数，允许仓库人工修改 | 应记录收货流水，不能覆盖原采购需求 |
| 已送数量 | `sales_order_items.delivered_quantity` 累计 | 仍按实际发货更新 |
| 送货单数量 | `sales_delivery_items.delivered_quantity` | 不因库存来源变化而改变 |
| 回单实收数量 | `finance_return_receipt_items.actual_received_quantity` | 不变 |
| 对账数量 | 回单明细的实际收货数量快照 | 不变 |

`tests/test_phase5_requisition_wms.py` 使用的是旧 `phase1_postgres` 原型应用，不是当前 `app.main` 生产链路，不能视为当前已有库存能力。

## 2. 当前数据库表结构梳理

### 2.1 已有库存相关表

正式库没有表名包含以下关键词的业务表：

```text
inventory
stock
warehouse
movement
reservation
```

当前最接近库存的内容：

| 位置 | 用途 | 是否真实库存 |
|---|---|---|
| `sales_order_items.inventory_deducted_qty` | 报料时手工填写抵扣小片数 | 否 |
| `material_requisition_items.inventory_deducted_qty` | 报料快照 | 否 |
| `supplier_requisition_orders.stock_deduction_qty` | 供应商报料单汇总快照 | 否 |
| `supplier_requisition_order_items.stock_deduction_qty` | 供应商报料单成员快照 | 否 |
| `operation_logs` | 操作审计 | 否 |

正式库核对结果：

```text
sales_order_items.inventory_deducted_qty：非零0条，合计0
material_requisition_items.inventory_deducted_qty：非零0条，合计0
supplier_requisition_orders.stock_deduction_qty：非零0条，合计0
supplier_requisition_order_items.stock_deduction_qty：非零0条，合计0
```

### 2.2 订单相关表

- `sales_orders`
- `sales_order_items`
- `order_item_number_sequences`

库存接入时重点关联：

- `sales_order_items.id`
- `sales_order_items.product_id`
- `sales_order_items.quantity`
- 订单快照材质、楞型、报料尺寸和压线字段

不建议把真实库存余额直接塞进订单表。

### 2.3 报料相关表

- `material_requisitions`
- `material_requisition_items`
- `supplier_requisition_orders`
- `supplier_requisition_order_items`

目前有两套报料记录：

1. 普通报料批次 `material_requisitions`。
2. 合并后的供应商采购单 `supplier_requisition_orders`。

后续库存服务必须同时明确两者职责，避免一套扣库存、另一套没有扣库存。

### 2.4 常用箱相关表

- `products`
- `materials`
- `product_drawings`

`products` 是成品库存选择产品和生成快照的主数据来源，不是库存余额表。

### 2.5 建议新增的表

推荐采用“统一库存批次父表 + 类型明细”的结构，避免流水和预占使用无法建立外键的多态 `inventory_type + inventory_id`。

#### 2.5.1 `warehouse_locations`

```text
id
location_code unique
location_name
warehouse_type finished / semi_finished / shared
is_active
remarks
created_at
updated_at
```

索引：

- 唯一索引 `location_code`。
- `warehouse_type, is_active`。

#### 2.5.2 `inventory_lots`

```text
id
lot_number unique
inventory_type finished / semi_finished
warehouse_location_id
quantity_available
quantity_reserved
quantity_consumed
quantity_damaged
quantity_scrapped
unit boxes / sheets
status active / frozen / closed
source_type manual / production_surplus / purchase_surplus / stocktake / transfer
source_ref_type
source_ref_id
stock_date
last_movement_at
version
remarks
created_by
created_at
updated_at
```

约束：

- 所有数量不得为负。
- `status=frozen` 时禁止新预占。
- `quantity_available + quantity_reserved` 表示当前仍在库数量。
- `version` 用于乐观并发控制。

说明：

- `available/reserved/consumed/damaged/scrapped` 更适合作为数量桶或流水类型，不适合作为整个批次唯一状态，因为一个批次可能部分可用、部分预占。

索引：

- `inventory_type, status`
- `warehouse_location_id, status`
- `stock_date`
- `last_movement_at`

#### 2.5.3 `finished_goods_inventory_details`

```text
inventory_lot_id PK/FK
owner_customer_id nullable
is_general
product_id
inventory_code_snapshot
product_name_snapshot
box_type_snapshot
length_mm
width_mm
height_mm
material_code_snapshot
flute_type_snapshot
```

约束：

- 非通用库存必须有 `owner_customer_id`。
- 通用库存必须由管理员明确转换，写流水和操作日志。

索引：

- `owner_customer_id, product_id`
- `product_id`
- `is_general`
- `inventory_code_snapshot`

#### 2.5.4 `semi_finished_inventory_details`

```text
inventory_lot_id PK/FK
supplier_name
owner_customer_id nullable
material_code_snapshot
layer_count
flute_type
board_length_mm
board_width_mm
sheet_type raw_board / net_sheet / creased_sheet
crease_type
crease_left_mm
crease_middle_mm
crease_right_mm
```

索引：

- `flute_type, board_length_mm, board_width_mm`
- `sheet_type, flute_type`
- `owner_customer_id`
- `material_code_snapshot`

#### 2.5.5 `inventory_reservations`

```text
id
reservation_number unique
inventory_lot_id FK
reservation_type finished_order / semi_requisition
order_id
order_item_id
requisition_item_id nullable
reserved_stock_quantity
credited_requirement_quantity
yield_factor nullable
status active / released / consumed / cancelled
warning_codes
warning_acknowledged_by
reserved_by
reserved_at
released_by
released_at
consumed_by
consumed_at
release_reason
idempotency_key unique
created_at
updated_at
```

说明：

- 成品：`reserved_stock_quantity` 和抵扣成品数量一致。
- 半成品：必须同时记录实际占用库存张数和抵扣的需求小片数，避免“一开几”单位混淆。

索引：

- `inventory_lot_id, status`
- `order_item_id, reservation_type, status`
- `requisition_item_id`
- 部分唯一索引：同一 `idempotency_key` 只能成功一次。

#### 2.5.6 `inventory_movements`

```text
id
movement_number unique
inventory_lot_id FK
movement_type manual_in / reserve / release_reserve / consume / damage / scrap / adjust / freeze / unfreeze / transfer_to_general
quantity
unit
before_available
after_available
before_reserved
after_reserved
reservation_id nullable
related_order_id nullable
related_order_item_id nullable
related_requisition_id nullable
related_supplier_order_id nullable
related_delivery_id nullable
reversal_of_movement_id nullable
reason
remarks
operator_id
idempotency_key unique
created_at
```

索引：

- `inventory_lot_id, created_at`
- `reservation_id`
- `related_order_item_id`
- `related_supplier_order_id`
- `related_delivery_id`
- `movement_type, created_at`

流水只能新增，不能编辑或删除。撤销通过反向流水完成。

## 3. 成品仓设计建议

### 3.1 表结构建议

使用：

- `warehouse_locations`
- `inventory_lots`
- `finished_goods_inventory_details`
- `inventory_reservations`
- `inventory_movements`

不建议把客户名称、产品名称当作外键；应保存正式 FK，同时保留必要快照用于历史追溯。

### 3.2 API 建议

```text
GET    /api/warehouse/finished
POST   /api/warehouse/finished/manual-in
GET    /api/warehouse/finished/{lot_id}
POST   /api/warehouse/finished/{lot_id}/adjust
POST   /api/warehouse/finished/{lot_id}/freeze
POST   /api/warehouse/finished/{lot_id}/unfreeze
POST   /api/warehouse/finished/{lot_id}/damage
POST   /api/warehouse/finished/{lot_id}/scrap
POST   /api/warehouse/finished/{lot_id}/transfer-to-general
GET    /api/warehouse/finished/candidates?order_item_id=...
POST   /api/warehouse/finished/reservations
POST   /api/warehouse/reservations/{id}/release
GET    /api/warehouse/movements
```

### 3.3 页面建议

仓库管理增加：

```text
成品仓
├── 库存列表
├── 手工入库
├── 订单抵扣候选
├── 预占记录
├── 库存流水
└── 长期未动提醒
```

列表至少显示客户、存货编码、产品、规格、材质/楞型、可用、预占、库位、入库日期、最近变动和状态。

### 3.4 抵扣订单流程

```text
订单明细保存
→ 系统只读查询同客户/同产品的可用成品候选
→ 用户输入抵扣数量并确认
→ 后端重新检查订单、产品、库存余额和版本
→ 同事务条件扣减 available、增加 reserved
→ 写 reservation
→ 写 movement
→ 报料服务按订单数量减已确认成品预占计算需生产数量
```

计算：

```text
需生产成品数 = max(订单数量 - 有效成品预占数, 0)
需求小片数 = 需生产成品数 × 每箱片数
```

送货数量和对账数量不变。

### 3.5 取消抵扣流程

```text
检查 reservation.status=active
→ 条件减少 reserved、增加 available
→ reservation 改为 released
→ 新增 release_reserve 流水
→ 重新计算需生产和待报料数量
```

不得删除原预占或原流水。

### 3.6 风险点

1. 当前送货门禁要求 `material_status=received`。全量使用成品库存时不会经过来料入库，因此后续必须新增明确的可送货数量计算，不能简单把库存抵扣伪装成来料入库。
2. 部分成品库存 + 部分新生产时，必须定义送货时优先消耗成品库存还是新生产数量。
3. 通用成品库存如何匹配客户产品尚未确认，不能仅按名称模糊匹配。
4. 当前没有生产完成确认节点，剩余生产数量何时变成可送货需要单独设计。

## 4. 半成品仓设计建议

### 4.1 表结构建议

使用：

- `warehouse_locations`
- `inventory_lots`
- `semi_finished_inventory_details`
- `inventory_reservations`
- `inventory_movements`

### 4.2 API 建议

```text
GET    /api/warehouse/semi-finished
POST   /api/warehouse/semi-finished/manual-in
GET    /api/warehouse/semi-finished/{lot_id}
GET    /api/warehouse/semi-finished/candidates?order_item_id=...
POST   /api/warehouse/semi-finished/reservations
POST   /api/warehouse/semi-finished/{lot_id}/adjust
POST   /api/warehouse/semi-finished/{lot_id}/freeze
POST   /api/warehouse/semi-finished/{lot_id}/damage
POST   /api/warehouse/semi-finished/{lot_id}/scrap
POST   /api/warehouse/reservations/{id}/release
```

### 4.3 页面建议

```text
半成品仓
├── 片料库存
├── 手工入库/盘点
├── 待报料抵扣候选
├── 预占记录
├── 库存流水
└── 长期未动提醒
```

候选列表必须同时显示库存批次和订单需求，不能只显示“可抵扣”结果。

### 4.4 抵扣报料流程

```text
先扣除成品库存，得到需生产数量
→ 计算需求小片数
→ 查询半成品候选
→ 人工确认库存批次、物理张数和抵扣需求小片数
→ 同事务预占库存并写流水
→ 采购张数按剩余需求重新计算
→ 合并报料和采购单只使用剩余采购张数
```

### 4.5 候选推荐规则

硬性条件：

```text
库存状态 active
quantity_available > 0
库存楞型 = 订单楞型
库存长 >= 订单单片需求长
库存宽 >= 订单单片需求宽
```

第一版不自动旋转长宽，除非用户确认允许90度旋转。

软性警告：

```text
材质代码不同：黄色“材质不同，请人工确认”
库存为已压线片：黄色“库存片料已有压线，请确认压线位置”
客户专用库存用于其他客户：默认禁止；管理员明确转通用后才允许
```

### 4.6 已压线片料处理

- 毛料、净片可以候选有压线需求。
- 已压线片不能候选毛料/净料需求。
- 已压线片候选有压线需求时必须人工确认警告。
- 第一版不自动判断三段压线尺寸完全一致。

### 4.7 材质不同但可候选的处理

- 材质不同不作为硬拦截。
- 用户必须勾选“已确认材质差异”。
- 后端必须收到警告确认码并再次校验。
- 抵扣不修改订单材质快照，也不修改材质字典。

### 4.8 风险点

1. 半成品库存数量单位尚未确定：是供应商送来的大张纸板，还是已开好的小片。
2. 如果库存是一开三的大张纸板，需要明确每张能抵扣几片以及余料口径，不能直接用一个整数相减。
3. 当前来料入库会覆盖 `requisition_qty`，无法表达“订100、到80、余20”。
4. 当前供应商报料单和普通报料批次是两套记录，库存预占必须由统一服务接入。
5. 当前没有生产领料/退料节点，半成品何时从 reserved 转 consumed 尚需确认。

## 5. 库存预占 / 冻结 / 消耗 / 报损设计

### 5.1 状态流转

批次状态：

```text
active → frozen → active
active/frozen → closed
```

数量流转：

```text
入库：available 增加
预占：available 减少，reserved 增加
释放：reserved 减少，available 增加
消耗：reserved 减少，consumed 增加
报损：available 减少，damaged/scrapped 增加
盘点调整：available 增减
```

### 5.2 库存流水

每次变动必须在同一数据库事务中完成：

1. 条件更新库存余额。
2. 新增或更新预占状态。
3. 新增库存流水。
4. 必要时新增 `operation_logs` 便于管理员审计。

禁止直接修改库存主表数量。

### 5.3 并发与重复抵扣防护

SQLite 下推荐：

```sql
UPDATE inventory_lots
SET quantity_available = quantity_available - :qty,
    quantity_reserved = quantity_reserved + :qty,
    version = version + 1
WHERE id = :id
  AND status = 'active'
  AND quantity_available >= :qty
  AND version = :expected_version;
```

必须检查 `rowcount=1`。同时要求：

- 每次确认带唯一 `idempotency_key`。
- `inventory_reservations.idempotency_key` 唯一。
- 前端按钮提交后立即禁用，但不能只依赖前端。
- 后端在事务内重新计算候选条件，不能信任旧页面结果。

### 5.4 回滚与撤销

- 取消订单、订单撤回、取消报料前必须查询活动预占。
- 活动预占必须先释放并写反向流水。
- 已消耗库存不能直接释放，应走退库或调整流程并填写原因。
- 流水不删除；使用 `reversal_of_movement_id` 关联原流水。
- 供应商报料单作废时，半成品预占是释放还是保留待重新报料，需要业务确认。

## 6. 页面改造范围

新增：

- 仓库管理总入口。
- 成品仓管理。
- 半成品仓管理。
- 库存批次详情。
- 预占记录。
- 库存流水。
- 长期未动库存提醒。

修改：

- 订单详情/编辑：显示订单数量、成品库存抵扣、需生产数量。
- 待报料：显示成品抵扣、半成品抵扣、剩余采购数量和候选入口。
- 合并报料：取消自由填写无来源抵扣数，改为读取已确认预占。
- 供应商采购单：继续只打印剩余采购张数。
- 来料入库：是否把采购余量转为半成品库存必须是明确人工操作。
- 首页：增加非阻塞的长期未动库存汇总。

## 7. API 改造范围

新增 `/api/warehouse/*` 库存接口和统一库存领域服务。

需要修改：

- `app/api/requisition.py`
  - 需求计算读取有效成品/半成品预占。
  - 禁止客户端直接提交无来源的抵扣数。
  - 报料取消和供应商报料单作废接入预占释放策略。
- `app/api/orders.py`
  - 订单撤回和删除前处理库存预占。
- `app/api/incoming.py`
  - 后续支持采购余量人工转半成品库存。
- `app/api/deliveries.py`
  - 成品库存参与可送货数量和消耗流水。
- `app/api/dashboard.py`
  - 增加库存提醒，不改变现有待办口径。

## 8. Alembic 迁移规划

本轮未创建迁移。

后续建议一个独立 revision，顺序：

1. 创建 `warehouse_locations`。
2. 创建 `inventory_lots`。
3. 创建成品和半成品详情表。
4. 创建 `inventory_reservations`。
5. 创建 `inventory_movements`。
6. 创建检查约束、唯一键、普通索引和必要的部分唯一索引。
7. 不回填历史库存，不修改历史订单、报料、送货、对账。
8. 迁移前必须 SQLite Backup API 备份并校验 SHA-256、完整性和外键。

现有 `inventory_deducted_qty` 和 `stock_deduction_qty` 暂时保留为历史兼容字段；新流程不应继续把它们作为库存事实来源。

## 9. 测试计划

至少覆盖：

1. 成品手工入库和流水。
2. 成品预占、取消预占、消耗。
3. 成品抵扣后订单数量不变。
4. 成品抵扣后需生产和报料数量减少。
5. 成品抵扣后送货、对账数量口径不变。
6. 半成品手工入库和流水。
7. 楞型相同、尺寸足够才进入候选。
8. 楞型不同不能候选。
9. 尺寸不足不能候选。
10. 材质不同黄色提醒和后端确认码。
11. 已压线片料对净料/毛料需求硬拦截。
12. 已压线片料对压线需求黄色提醒。
13. 半成品抵扣后采购张数减少。
14. 供应商采购单只打印剩余采购张数。
15. 冻结、解冻、破损、报损、盘点调整均写流水。
16. 同一库存并发预占只能一个成功。
17. 重复提交 idempotency key 不重复扣库存。
18. 订单撤回、取消报料释放预占。
19. 已消耗库存不能直接释放。
20. 1年、1年半、2年未动提醒。
21. 回归订单、报料、入库、送货、对账。

## 10. 分阶段实施计划

### 阶段 A：库存基础设施

- 新增表、模型、库存领域服务和迁移。
- 只做管理员手工入库、列表、冻结、报损、流水。
- 不接订单和报料。

### 阶段 B：成品仓抵扣

- 候选、人工预占、释放。
- 接入需生产/需报料计算。
- 处理全量成品库存订单的送货门禁。

### 阶段 C：半成品仓抵扣

- 候选规则、黄色提醒、人工预占。
- 接入单条报料、合并报料和采购单打印。

### 阶段 D：消费、撤销和提醒

- 送货/生产消耗。
- 订单撤回、报料作废、退库和反向流水。
- 长期未动提醒和库存盘点。

每个阶段单独迁移、测试、提交和现场验收。

## 11. 禁止事项与高风险点

绝对不能改：

- `sales_order_items.quantity` 客户订单数量。
- 送货数量和超送校验口径。
- 回单实际收货数量。
- 对账、开票和收款数量/金额口径。
- 材质字典不绑定楞型的规则。
- 历史订单和历史报料快照。
- 供应商采购单不显示内部库存成本和财务信息。

禁止：

- 自动抵扣库存。
- 只在前端显示抵扣而不写后端预占。
- 直接改库存余额而不写流水。
- 用 `operation_logs` 代替库存流水。
- 把不同楞型互相抵扣。
- 批量回填历史库存。
- 在没有幂等和条件更新的情况下上线。

## 12. 需要用户确认的问题

正式开发前必须确认：

1. 半成品库存数量单位：供应商送来的大张纸板，还是开好后的单片？一开三的大张库存每张如何换算抵扣小片数？
2. 半成品尺寸是否允许长宽旋转90度匹配？第一版建议不允许。
3. 成品库存预占后何时转为已消耗：生成送货单、确认发货还是仓库手工出库？
4. 一个订单同时有成品库存和新生产成品时，送货默认先消耗哪部分？
5. 通用成品库存的匹配条件：必须同产品，还是允许尺寸、材质、楞型完全一致但产品编码不同？
6. 半成品已压线片料由谁确认压线位置可用：管理员、仓库还是业务员？
7. 供应商报料单作废时，半成品预占应自动释放，还是保留给重新报料？
8. 来料入库后是否自动形成半成品库存？第一版建议不自动，必须人工确认“转库存数量”。
9. 库位先使用自由文本还是建立库位主数据？本报告建议建立主数据。
10. 当前报料页面的自由填写“库存抵扣”上线后是否彻底取消？建议取消，改为只能选择真实库存预占。

## 审计范围

重点审计文件：

- `app/api/incoming.py`
- `app/api/requisition.py`
- `app/api/orders.py`
- `app/api/deliveries.py`
- `app/api/finance.py`
- `app/api/dashboard.py`
- `app/api/products.py`
- `app/models/order.py`
- `app/models/requisition.py`
- `app/models/supplier_requisition_order.py`
- `app/models/delivery.py`
- `app/models/finance.py`
- `app/models/product.py`
- `app/models/material.py`
- `app/models/audit.py`
- `static/index.html`
- `static/incoming.html`
- `alembic/versions/b71c4a9e2d10_phase11_requisition_workflow.py`
- `alembic/versions/s57m1p9q6r39_v0192b_supplier_requisition_orders.py`
- 相关订单、报料、入库、送货、财务测试。

## 本轮执行声明

- 未新增库存业务代码。
- 未新增模型或数据库表。
- 未新增 Alembic 迁移。
- 未写正式数据库。
- 未修改历史订单、报料、送货或对账。
- 未恢复 stash。
- 未处理天华预送货或历史采购单导入。
