# v0.22.0-b 成品库存预占抵扣报料实施报告

日期：2026-07-02

## 完成内容

- 成品库存候选严格匹配订单明细 product_id。
- 客户专用库存只允许用于所属客户；通用库存必须人工确认。
- 人工预占、幂等提交、版本校验、库存余额条件更新。
- 预占和释放均写不可变库存流水。
- 订单显示订单数量、成品库存抵扣和需生产数量。
- 报料按剩余需生产数量和每箱片数计算。
- 全额预占不生成报料。
- 删除订单、删除明细、取消订单和流程撤回释放 active 预占。
- 仓库页面展示预占、消耗和关联订单记录。
- 全额预占订单允许进入送货选择，但不执行库存 consumed。

## 边界

- 未修改 `sales_order_items.quantity`。
- 未修改送货数量和对账数量口径。
- 未接半成品库存抵扣。
- 未接送货 consumed 出库。
- 未新增数据库迁移。
- 未批量修改历史订单、报料、送货或对账数据。
- 未恢复 stash，未处理天华预送货新功能和历史采购单导入。

## API

- `GET /api/warehouse/finished/candidates`
- `POST /api/warehouse/finished/reservations`
- `GET /api/warehouse/reservations`
- `POST /api/warehouse/reservations/{id}/release`

## 计算口径

```text
已预占成品库存 = active finished_order reservations 合计
需生产数量 = max(订单数量 - 已预占成品库存, 0)
需求小片数 = 需生产数量 × 每箱片数
采购张数 = ceil(需求小片数 / 开料数)
```

## 测试

- 成品候选、预占、释放、报料、删除/取消、全额预占和 UI 专项：12 passed。
- 订单、报料、供应商采购单、送货、仓库基础及前端回归：125 passed。
- 报价、PDF 订单导入、来料、财务、天华预送货冻结和启动脚本回归：65 passed。
- 前端 JavaScript 语法检查通过。
