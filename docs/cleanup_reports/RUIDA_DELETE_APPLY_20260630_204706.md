# RUIDA DELETE APPLY

- 生成时间：2026-06-30 20:47:11
- 删除清单：`D:\纸箱厂erp软件搭建\docs\cleanup_reports\RUIDA_DELETE_LIST_20260630_204706.xlsx`
- 备份路径：`D:\纸箱厂erp软件搭建\data\backups\carton_erp_20260630_204709_728363_BEFORE_DELETE_RUIDA_ORDERS.sqlite3`
- RUIDA 订单：9535
- RUIDA 订单明细：9597
- 天华导入引用保留并解除绑定：168
- 天华草稿引用：0
- integrity_check：ok
- foreign_key_check：0

## 关联影响

- sales_delivery_items: 0
- material_requisition_items: 0
- supplier_requisition_order_items: 0
- tianhua_pre_delivery_import_items: 168
- tianhua_pre_delivery_draft_items: 0
- migration_ruida_sales_order_map: 9535
- migration_ruida_sales_item_map: 9597

## 核心表数量

- sales_orders: 9540 -> 5
- sales_order_items: 9631 -> 34
- sales_deliveries: 0 -> 0
- sales_delivery_items: 0 -> 0
- finance_return_receipts: 0 -> 0
- finance_return_receipt_items: 0 -> 0
- finance_statements: 0 -> 0
- finance_statement_items: 0 -> 0
- material_requisitions: 12 -> 12
- material_requisition_items: 46 -> 46
- supplier_requisition_orders: 3 -> 3
- supplier_requisition_order_items: 6 -> 6
- tianhua_pre_delivery_import_batches: 7 -> 7
- tianhua_pre_delivery_import_items: 210 -> 210
- tianhua_pre_delivery_drafts: 0 -> 0
- tianhua_pre_delivery_draft_items: 0 -> 0

## 安全结论

- 删除条件固定为 sales_orders.order_number 包含 RUIDA。
- 非 RUIDA 订单数量在 apply 前后必须一致。
- 天华预送货四张独立表不会执行 DELETE。
- 不修改正式库存数量，不生成送货、回单或对账记录。
