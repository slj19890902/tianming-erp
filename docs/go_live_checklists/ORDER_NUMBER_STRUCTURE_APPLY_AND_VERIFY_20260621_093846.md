# 订单主系统单号 / 明细系统单号主库实施与验证报告

执行时间：2026-06-21 09:38:46  
项目目录：`D:\纸箱厂erp软件搭建`

## 一、边界确认

1. 是否确认目录：是
2. 是否执行历史迁移：否
3. 是否修改历史订单：否
4. 是否修改 `legacy_*`：否
5. 本轮仅执行：
   - 主库时点备份
   - 主库结构迁移
   - 后端 / 前端切换新编号与分组逻辑
   - 1 张受控测试订单写入验证

## 二、主库备份

- 备份路径：`D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_order_number_structure_apply_20260621_093327.sqlite3`
- 备份前主库 SHA-256：`a7bdf3ceff3e42f0e0f0c5e3d456475b4d876ca52a625359adead898c49b48cf`
- 备份 SHA-256：`a7bdf3ceff3e42f0e0f0c5e3d456475b4d876ca52a625359adead898c49b48cf`
- 主库 / 备份哈希是否一致：是
- 备份完整性：`PRAGMA integrity_check = ok`
- 备份前数量：
  - `sales_orders = 15593`
  - `sales_order_items = 15658`
  - 历史订单数量 = `15589`
  - `migration_ruida_sales_order_map = 15589`
  - `migration_ruida_sales_item_map = 15651`

## 三、主库结构迁移内容

### 新增字段

- `sales_order_items.item_order_number`
- `sales_order_items.item_sequence`

### 新增序列表

- `order_item_number_sequences`

### 复用序列表

- `order_daily_sequences`

### 新增索引

- `ix_sales_orders_customer_po`
- `ix_sales_orders_customer_po_group`
- `ux_sales_order_items_item_order_number`
- `ix_sales_order_items_snapshot_product_code`
- `ix_sales_order_items_snapshot_product_name`

### 迁移纪律

- 使用事务执行
- 结构已存在则跳过
- 未回填历史明细 `item_order_number`
- 历史数据允许新字段为空
- 迁移后未改动历史迁移台账和 `legacy_*`

## 四、编号规则已启用

### 主系统单号

- 格式：`TMYYYYMMDDNNN`
- 示例：`TM20260621001`
- 同日递增
- 保存后不复用
- 删除 / 作废后不复用

### 明细系统单号

- 格式：`TMYYYYMMDDNNN-001`
- 示例：
  - `TM20260621001-001`
  - `TM20260621001-002`
  - `TM20260621001-003`
- 同主单递增
- 全库唯一
- 保存后不复用

## 五、订单管理与新建订单切换结果

1. 订单管理默认分组规则已切换为 `customer_id + customer_po`
2. 不同客户相同 `customer_po` 不合并
3. 空 `customer_po` 不强行合并
4. 默认第一列显示客户单号
5. 第二列显示编辑按钮
6. 系统单号默认隐藏
7. 展开后显示：
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
8. 新建订单已增加主单号和明细单号预览

## 六、受控真实写单验证

### 测试订单

- 订单 ID：`15594`
- 客户：`苏州华舜五金工具制造有限公司`
- 客户单号：`TEST-ORDER-NUMBER-VERIFY-20260621`
- 主系统单号：`TM20260621001`
- 保存后状态：初始 `pending_production`，验证后按业务规则改为 `cancelled`

### 测试明细系统单号

- `TM20260621001-001`
- `TM20260621001-002`
- `TM20260621001-003`

### 搜索与分组验证

- 搜索客户单号：命中 1 张
- 搜索主系统单号：命中 1 张
- 搜索明细系统单号：命中 1 张
- 搜索产品编码：命中 1 张
- 详情返回明细数：3
- 分组键：`14::TEST-ORDER-NUMBER-VERIFY-20260621`

## 七、删除 / 作废后不复用验证

1. 测试订单作废后，下一个主系统单号预览为 `TM20260621002`
2. 未复用已作废主单号 `TM20260621001`
3. 当前测试订单的下一条明细序号预览为 `4`
4. 对应下一条明细系统单号将是 `TM20260621001-004`
5. 结论：主单号和明细号均已验证“不复用”

## 八、主库数量变化说明

### 迁移后主库数量

- `sales_orders = 15594`
- `sales_order_items = 15661`
- 历史订单数量 = `15589`
- `migration_ruida_sales_order_map = 15589`
- `migration_ruida_sales_item_map = 15651`

### 变化来源

- 本轮新增仅来自 1 张受控测试订单和 3 条测试明细
- 历史订单数量未变化
- 迁移台账未变化
- `legacy_*` 未变化

## 九、完整性与外键结果

- 主库当前 SHA-256：`e0ef6a7947051520574bdda061a826e1d81f75b9c8ed10ec2c5538ab97f2fb58`
- `PRAGMA integrity_check = ok`
- 应用实际连接 `PRAGMA foreign_keys = 1`
- `PRAGMA foreign_key_check = 0`

## 十、后端恢复结果

- 后端已恢复
- 健康接口：`GET /api/health`
- 当前健康结果：
  - 数据库路径：`D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`
  - `orders_count = 15594`
  - `order_items_count = 15661`

## 十一、测试结果

执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase5_orders.py tests\test_phase14_frontend.py tests\test_order_number_structure_rehearsal.py tests\scripts\test_apply_order_number_structure.py -q
```

结果：

- `27 passed`

覆盖：

1. 主系统单号格式 / 递增
2. 明细系统单号格式 / 递增
3. 删除 / 作废后不复用
4. `item_order_number` 唯一
5. `item_sequence` 正确
6. `customer_id + customer_po` 分组
7. 不同客户相同客户单号不合并
8. 空客户单号不合并
9. 搜索客户名称 / 客户单号 / 主系统单号 / 明细系统单号 / 产品编码 / 产品名称
10. 默认列顺序和系统单号隐藏
11. 普通页面 / 文档不暴露旧系统名称

## 十二、结论

1. 主库结构迁移完成
2. 后端新编号规则已启用
3. 订单管理新分组规则已启用
4. 受控真实写单验证通过
5. 主库数量变化仅来自受控测试订单
6. 建议下一步重新进入综合人工试用验收，重点验收：
   - 新建订单预览
   - 客户单号分组展开
   - 搜索客户单号 / 主系统单号 / 明细系统单号
   - 测试订单作废后的日常显示
