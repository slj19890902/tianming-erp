# P1-54 外购包材历史异常单条修复方案（待授权）

## 边界

- 本文件只是发布后的独立数据修复方案，不属于 `ll20v8x9z09` 结构迁移。
- 禁止批量推断或回填历史订单；未取得老板对下面精确单据的明确授权前，不执行任何正式库写入。
- 正式执行前必须停服、验证独占锁、创建并校验完整备份，确认正式库已升级到 `ll20v8x9z09`。

## 只读核对结果（2026-08-13）

唯一已确认的现场异常：

| 字段 | 只读值 |
|---|---|
| 订单 | `TM20260813001`（`sales_orders.id=9657`） |
| 订单状态 | `cancelled` |
| 外购采购单 | `EP-20260813-001`（`external_packaging_purchase_orders.id=2`） |
| 采购状态 | `confirmed` |
| 采购明细数 | 1 |
| 累计实收 | 0 |
| 撤回审计 | `operation_logs.id=6552`，`order.workflow_rollback` |
| 作废审计 | `operation_logs.id=6553`，`order.status_change` |

现有审计证据足以支持“该零实收采购随订单撤回/作废失效”；但 `ll20` 不自动把这一推断写入正式历史。

## 授权后建议写入

只允许新增一条 append-only 事实：

```sql
INSERT INTO external_packaging_purchase_cancellations (
    purchase_order_id,
    source,
    reason,
    cancelled_by
)
SELECT
    2,
    'authorized_data_repair',
    '老板授权修复现场异常：订单 TM20260813001 已撤回并作废，采购单 EP-20260813-001 零实收',
    NULL
WHERE EXISTS (
    SELECT 1
    FROM external_packaging_purchase_orders p
    JOIN external_packaging_purchase_batches b ON b.id = p.batch_id
    JOIN sales_orders o ON o.id = b.sales_order_id
    WHERE p.id = 2
      AND p.purchase_number = 'EP-20260813-001'
      AND o.id = 9657
      AND o.order_number = 'TM20260813001'
      AND o.status = 'cancelled'
      AND NOT EXISTS (
          SELECT 1
          FROM external_packaging_purchase_items pi
          JOIN external_packaging_receipt_items ri
            ON ri.purchase_item_id = pi.id
          WHERE pi.purchase_order_id = p.id
            AND ri.received_quantity > 0
      )
      AND EXISTS (
          SELECT 1 FROM operation_logs log
          WHERE log.entity_type = 'order'
            AND log.entity_id = o.id
            AND log.action_code = 'order.workflow_rollback'
      )
      AND EXISTS (
          SELECT 1 FROM operation_logs log
          WHERE log.entity_type = 'order'
            AND log.entity_id = o.id
            AND log.action_code = 'order.status_change'
      )
)
AND NOT EXISTS (
    SELECT 1
    FROM external_packaging_purchase_cancellations c
    WHERE c.purchase_order_id = 2
);
```

正式执行器必须断言 `changes() = 1`；为 0 或大于 1 均回滚并停止，不得放宽条件。

## 执行后只读验收

1. cancellation 表只有 `purchase_order_id=2` 的一条 `authorized_data_repair` 事实。
2. 外购包装待收列表不再出现 `EP-20260813-001`。
3. 订单详情显示“原外购包装采购已作废”，且不允许继续打印或收料。
4. `integrity_check=ok`、`foreign_key_check=0`，核心表计数除 cancellation `+1` 外不变。
5. 保存正式执行报告、备份路径与 SHA-256；不得删除原采购、采购明细或原审计日志。
