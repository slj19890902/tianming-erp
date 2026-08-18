# P1-73A 已报料逐明细能力审计

## 结论

- 审计基线：`70d5dc87362beed05725ccb8b7d9b22e189ad91c`，`v0.22.130`。
- Alembic 唯一 head：`tt28v8x9z17`。
- `/api/requisition/reported-documents` 目前按正式单据分页，供应商报料、补库、旧报料和组合报料各一张单据一行；供应商正式明细已有稳定 `SupplierRequisitionOrderItem.id`。
- 正式明细表当前没有行级 `status`、`version`、`voided_at`、`voided_by`，有效覆盖计算只排除整张 `SupplierRequisitionOrder.status=voided`。因此不能用现模型安全表达“同单只撤销一行”，P1-73C 必须新增最小迁移。
- 来料事实已用 `IncomingReceiptItem.supplier_order_item_id` 精确关联正式明细，可据此计算有效实收并在事务内阻断；来料目标当前只接受 `SupplierRequisitionOrder.status=confirmed`，P1-73C 还必须增加明细行有效状态门禁。
- 任务打印已经以 `SupplierRequisitionOrderItem.id` 为任务卡稳定身份；P1-73B/D 应复用该身份，不按尺寸、客户或存货编码重新分组。

## 来源边界

- 首版逐明细撤销只支持 `source_type=supplier_order` 的正式供应商报料明细。
- `stock_replenishment`、`legacy_material_requisition`、`composite_bom_requisition` 和外购包材继续使用既有受控撤销/作废路径，不因新紧凑列表被强行套入明细撤销。
- 现有整单作废接口继续保留在采购单详情；P1-73C 不复用其“遍历整单并重置全部订单项”的实现。

## 金样本

匿名金样本位于 `tests/fixtures/p1_73_reported_item_golden_cases.json`，覆盖：

1. 同一采购单三行撤销中间行；
2. 同一订单明细分两次部分报料；
3. A3 盖/底独立覆盖；
4. 部分收料行禁止撤销；
5. 最后一条有效行撤销后采购单全部已撤销。

每个样本都固定撤销前后有效行、有效覆盖、剩余需求和采购单状态；后续 B/C/D 行为测试不得改变这些数量口径。

## 数据边界

本阶段只读审计代码和规则文件，仅新增匿名 JSON、测试和报告；没有连接、复制、迁移或写入正式数据库，也没有刷新 `legacy_ruida_*`。
