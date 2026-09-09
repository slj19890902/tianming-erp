"""Selection/quantity contracts for an explicitly recorded execution cutover.

Not an authorization to create or infer a cutover from delivered quantities.
Historical document lookups still use their original direct snapshot IDs.
"""
from dataclasses import dataclass

from sqlalchemy import exists, or_

from app.models.multilevel_bom import OrderBomExecutionCutover, OrderBomCutoverSource
from app.models.product_bom import SalesOrderItemBomComponent
from app.services.multilevel_bom_plan import BomPlanError


def current_snapshot_predicate(snapshot=SalesOrderItemBomComponent):
    cutover = exists().where(OrderBomExecutionCutover.order_item_id == snapshot.sales_order_item_id)
    current = exists().where(OrderBomCutoverSource.snapshot_id == snapshot.id,
        OrderBomCutoverSource.order_item_id == snapshot.sales_order_item_id,
        OrderBomCutoverSource.role == "current")
    return or_(~cutover, current)


@dataclass(frozen=True)
class ExecutionWindow:
    commercial_quantity: int
    delivered_before: int
    execution_quantity: int
    delivered_since: int
    remaining_quantity: int


def execution_window(*, order_quantity, delivered_quantity, cutover=None):
    if (type(order_quantity) is not int or order_quantity <= 0
            or type(delivered_quantity) is not int or delivered_quantity < 0):
        raise BomPlanError("订单或已送数量无效")
    baseline = 0
    if cutover is not None:
        if (type(cutover.order_quantity) is not int or cutover.order_quantity != order_quantity
                or type(cutover.delivered_before) is not int
                or not 0 <= cutover.delivered_before < order_quantity):
            raise BomPlanError("订单数量与执行转换边界不一致")
        baseline = cutover.delivered_before
        if delivered_quantity < baseline:
            raise BomPlanError("不能跨越BOM转换边界撤销旧送货，请先核对历史转换")
    # Authorized excess delivery is possible; do not make a negative demand.
    return ExecutionWindow(order_quantity, baseline, order_quantity - baseline,
        delivered_quantity - baseline, max(order_quantity - delivered_quantity, 0))
