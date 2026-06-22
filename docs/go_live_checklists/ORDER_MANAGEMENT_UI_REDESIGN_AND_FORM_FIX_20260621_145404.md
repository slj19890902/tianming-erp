# 订单管理 UI 重构与表单修复

- 执行时间：2026-06-21 14:54:04
- 项目目录：`D:\纸箱厂erp软件搭建`
- 是否执行历史迁移：否
- 是否修改历史订单：否
- 是否修改 `legacy_*`：否
- 是否批量改库：否

## 本轮改动范围

- 前端：订单管理页面顶部区、筛选区、客户订单组表格、展开明细卡片、新建订单表单、订单组编辑表单、材质显示层清洗。
- 后端：新增订单头编辑接口；订单列表/详情返回 `display_material`；订单列表支持日期范围与状态筛选。
- 测试：更新前端结构断言，新增订单头编辑与材质显示层断言。

## 验收结论

1. 订单管理第一列已改为客户名称。
2. 客户单号列已单独收窄并保留分组语义。
3. 主系统单号默认不再作为列表独立列显示。
4. 编辑按钮已移动到最后“操作”列。
5. 展开区已改为浅色明细卡片，不再重复主表结构。
6. 展开区显示明细系统单号与主系统单号。
7. 新建订单按钮可打开右侧/弹层表单。
8. 新建订单表单包含客户单号 `customer_po`。
9. 订单组编辑表单已加入客户单号 `customer_po`，保存后按新的 `customer_id + customer_po` 重新分组。
10. 材质显示已清洗为显示层逻辑：如 `045 A113B` 显示为 `A113B`。
11. 改动只发生在 API 返回字段和前端显示层，未批量改动数据库原始材质值。

## 关键实现文件

- `D:\纸箱厂erp软件搭建\static\index.html`
- `D:\纸箱厂erp软件搭建\app\api\orders.py`
- `D:\纸箱厂erp软件搭建\tests\test_phase14_frontend.py`
- `D:\纸箱厂erp软件搭建\tests\test_phase5_orders.py`

## 只读数据库校验

- `sales_orders = 15594`
- `sales_order_items = 15660`
- `legacy_ruida_orders = 39922`
- `legacy_ruida_order_items = 40449`
- `PRAGMA integrity_check = ok`

说明：本轮未执行写库操作；以上为本轮结束时的只读读取结果。

## 测试结果

执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase5_orders.py tests\test_phase14_frontend.py tests\test_order_number_structure_rehearsal.py tests\scripts\test_apply_order_number_structure.py -q
```

结果：

- `31 passed`

## 页面级验证说明

- 已尝试使用内置浏览器做自动化只读验收。
- 当前浏览器运行时返回 `codex/sandbox-state-meta: missing field sandboxPolicy`，导致本轮无法完成自动截图级验收。
- 因此本轮页面确认依据为：
  - 前端结构测试
  - 后端接口测试
  - 本地代码静态核对

## 是否建议再次人工验收

建议。

建议人工重点再看一次：

1. 订单管理默认列表列顺序与行高。
2. 展开卡片的可读性。
3. 新建订单按钮打开与客户单号输入。
4. 订单组编辑后列表重分组效果。
5. 材质显示是否统一为清洗后的展示值。

