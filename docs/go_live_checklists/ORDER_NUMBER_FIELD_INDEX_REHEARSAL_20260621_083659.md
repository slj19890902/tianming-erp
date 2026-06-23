# 订单主系统单号 / 明细系统单号 / 客户单号分组副本演练报告

执行时间：2026-06-21 08:36:59  
项目目录：`D:\纸箱厂erp软件搭建`

## 一、边界确认

1. 是否执行历史迁移：否
2. 是否修改历史订单：否
3. 是否修改 `legacy_*`：否
4. 是否直接修改主库结构：否
5. 主库路径：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
6. 本轮仅进行了：
   - 主库结构只读审计
   - 副本结构演练
   - 测试
   - 方案输出

## 二、现有结构审计结果

### 1. `sales_orders` 已有字段

- `id`
- `order_number`
- `customer_id`
- `customer_po`
- `order_date`
- `delivery_date`
- `status`
- `payment_status`
- `total_amount`
- `remark`
- `created_by`
- `created_at`
- `updated_at`

### 2. `sales_order_items` 已有字段

- `id`
- `order_id`
- `product_id`
- `quantity`
- `unit_price`
- `subtotal`
- `material_status`
- `material_received_at`
- `snapshot_product_name`
- `snapshot_spec`
- `snapshot_material`
- `created_at`
- `material_received_by`
- `delivered_quantity`
- `is_force_closed`
- `snapshot_product_code`
- `inventory_deducted_qty`
- `requisition_qty`
- `requisition_status`
- `special_process`
- `requisition_spec`
- `cardboard_len`
- `cardboard_width`
- `requisition_date`
- `supplier_delivery_time`
- `supplier_order_number`
- `requisition_remark`

### 3. 已存在 / 可复用字段

- `sales_orders.order_number`：可直接复用为主系统单号字段
- `sales_orders.customer_po`：可直接作为客户单号字段
- `sales_orders.order_date`：可直接用于主系统单号按日递增
- `sales_orders.status`：可继续承载作废状态控制
- `order_daily_sequences`：主系统单号日序列已存在，可复用

### 4. 当前缺失字段

- `sales_order_items.item_order_number`
- `sales_order_items.item_sequence`

### 5. 不建议复用的方向

- 不建议把明细系统单号塞进备注或产品快照字段
- 不建议用 `sales_order_items.id` 直接代替明细系统单号
- 不建议仅靠现存明细行 `MAX(id)` 或 `COUNT(*)` 推导 `-001/-002/-003`

### 6. 约束与索引现状

#### `sales_orders`

- 已有唯一约束：`order_number`
- 已有索引：
  - `customer_id`
  - `order_date`
  - `status`
- 缺失索引：
  - `customer_po`
  - `(customer_id, customer_po)` 组合索引

#### `sales_order_items`

- 已有索引：
  - `order_id`
  - `product_id`
  - 若干报料状态相关索引
- 缺失索引：
  - `item_order_number`
  - `snapshot_product_code`
  - `snapshot_product_name`

### 7. 删除逻辑审计

- 当前明细删除为物理删除
- 代码路径：`D:\纸箱厂erp软件搭建\app\api\orders.py`
  - 明细删除路由：`DELETE /items/{item_id}`
  - 当前实现：`db.delete(item)`
- 结论：若未来需要保证明细编号删除后不复用，仅靠主表现存明细无法可靠实现，必须配套序列字段或序列表

### 8. 当前订单创建 / 编辑代码路径

- 主单创建：`D:\纸箱厂erp软件搭建\app\api\orders.py`
- 明细编辑：`D:\纸箱厂erp软件搭建\app\api\orders.py`
- 模型定义：`D:\纸箱厂erp软件搭建\app\models\order.py`

## 三、字段与序列设计建议

### 1. 建议新增字段

#### `sales_order_items.item_order_number`

- 含义：明细系统单号
- 示例：`TM20260621001-001`
- 建议：必填、全库唯一
- 结论：建议新增

#### `sales_order_items.item_sequence`

- 含义：同一主系统单号下的明细递增序号
- 示例：`1 / 2 / 3`
- 用途：生成 `-001 / -002 / -003`
- 结论：建议新增

### 2. 建议复用现有主单日序列表

- 现有 `order_daily_sequences` 已可承担主系统单号 `TMYYYYMMDDNNN` 的日递增
- 不建议再新建第二套主单日序列表

### 3. 建议新增明细序列表

建议新增：

`order_item_number_sequences`

建议字段：

- `order_id`
- `last_item_seq`

用途：

1. 即使明细被物理删除，后续新增仍从更大序号继续递增
2. 保证 `item_order_number` 不复用
3. 降低并发生成子编号时的歧义

结论：建议新增

## 四、索引建议

### 建议新增

1. `sales_orders.customer_po`
   - 用途：客户单号搜索
   - 类型：普通索引

2. `sales_orders(customer_id, customer_po)`
   - 用途：默认分组与组合筛选
   - 类型：普通组合索引

3. `sales_order_items.item_order_number`
   - 用途：明细系统单号精确定位
   - 类型：唯一索引

4. `sales_order_items.snapshot_product_code`
   - 用途：按存货编码搜索
   - 类型：普通索引

5. `sales_order_items.snapshot_product_name`
   - 用途：按产品名称搜索
   - 类型：普通索引

### 已有且可继续使用

- `sales_orders.customer_id`
- `sales_orders.order_date`
- `sales_orders.order_number`
- `sales_order_items.order_id`

### 性能评估结论

- 当前约 1.5 万正式订单规模下，上述索引是合理的
- 写入开销可接受
- 仍建议先在副本验证，再单独授权主库结构迁移

## 五、副本结构演练

副本路径：  
`D:\纸箱厂erp软件搭建\data\sandboxes\carton_erp_order_number_structure_rehearsal_20260621_083313.sqlite3`

### 1. 副本新增字段

- `sales_order_items.item_order_number`
- `sales_order_items.item_sequence`

### 2. 副本新增索引

- `ix_sales_orders_customer_po`
- `ix_sales_orders_customer_po_group`
- `ux_sales_order_items_item_order_number`
- `ix_sales_order_items_snapshot_product_code`
- `ix_sales_order_items_snapshot_product_name`

### 3. 副本新增序列表

- `order_item_number_sequences`

### 4. 主系统单号生成结果

- `TM20260621001`
- `TM20260621002`
- `TM20260621003`
- `TM20260621004`

### 5. 明细系统单号生成结果

以 `TM20260621001` 为例：

- `TM20260621001-001`
- `TM20260621001-002`
- `TM20260621001-003`

删除 `-002` 后继续新增一条：

- `TM20260621001-004`

### 6. 编号删除后不复用验证

- 主系统单号：通过
- 明细系统单号：通过
- 结论：借助现有主单日序列 + 新增明细序列表，可保证编号不复用

### 7. 分组规则验证

验证口径：

- 同客户 + 同客户单号：合并
- 不同客户 + 同客户单号：不合并
- 空客户单号：单独显示

副本验证结果：

- `group:3::PO-SAME-001`：两张订单被正确归为一组
- `group:5::PO-SAME-001`：未与客户 3 混组
- `single:TM20260621004`：空客户单号未被强行合并

结论：通过

### 8. 搜索验证

已验证搜索项：

- 客户名称
- 客户单号
- 主系统单号
- 明细系统单号
- 日期
- 产品编码
- 产品名称

验证结果：

- 搜索命中后仍按 `customer_id + customer_po` 分组
- 空客户单号结果仍按单独主系统单号显示
- 未出现不同客户相同客户单号混组

结论：通过

### 9. 副本完整性

- `PRAGMA integrity_check = ok`
- `PRAGMA foreign_key_check = 0`

### 10. 历史数据未受影响

- 历史迁移订单数量未被本次副本演练改写
- `legacy_*` 原始层未被修改

## 六、订单管理实现同步结论

后续正式实现建议按以下口径落地：

1. 默认第一列：客户单号
2. 第二列：编辑按钮
3. 系统单号默认隐藏
4. 默认分组键：`customer_id + customer_po`
5. 空 `customer_po` 不合并
6. 展开后显示：
   - 明细系统单号
   - 主系统单号
   - 产品名称
   - 存货编码
   - 规格
   - 材质
   - 数量
   - 单价
   - 金额
   - 状态
7. 搜索结果仍按 `customer_id + customer_po` 分组

## 七、测试结果

测试文件：  
`D:\纸箱厂erp软件搭建\tests\test_order_number_structure_rehearsal.py`

执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_order_number_structure_rehearsal.py -q
```

结果：

- `3 passed`

覆盖内容：

1. 主系统单号格式与递增
2. 明细系统单号格式与递增
3. 删除后不复用
4. 分组规则
5. 搜索规则
6. 副本结构字段 / 索引 / 序列表
7. 主库未被改动

## 八、主库状态结论

1. 主库结构未改动
2. 主库数据未改动
3. 历史订单未改动
4. `legacy_*` 未改动

## 九、需要单独授权的事项

以下事项仍需你后续单独授权：

1. 主库结构迁移
2. 主库新增字段 `item_order_number`
3. 主库新增字段 `item_sequence`
4. 主库新增表 `order_item_number_sequences`
5. 主库新增相关索引
6. 后端正式切换到 `TMYYYYMMDDNNN` / `TMYYYYMMDDNNN-001` 规则
7. 订单管理正式改成默认按 `customer_id + customer_po` 分组
8. 如需主库真实写入测试订单，也需单独授权

## 十、下一步建议

建议下一步分两段执行：

1. 先授权“主库结构迁移草案 + 副本 API / 页面实现”
2. 再授权“主库正式结构迁移 + 小范围真实写单验证”
