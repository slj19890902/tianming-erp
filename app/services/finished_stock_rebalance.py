"""Reattach a released order reservation to real, unreserved compatible stock.

The old lot, the stocktake adjustment, and all prior completion facts remain
unchanged. The replacement is an ordinary reservation with its own movement.
"""
from __future__ import annotations

from hashlib import sha256
from sqlalchemy import select

from app.models.order import Order, OrderItem
from app.models.warehouse_inventory import FinishedGoodsInventoryDetail, InventoryLot
from app.services.finished_stock_identity import matches_lot_stock_identity, order_product_basis
from app.services.warehouse_inventory import WarehouseInventoryError, reserve_finished_inventory


def reserve_released_finished(
    db, *, order_item_id: int, quantity: int, operator_id: int,
    operation_key: str, reason: str, exclude_lot_ids=(),
) -> int:
    """Reserve at most quantity; caller decides whether a shortfall is allowed."""
    if reason not in {"stocktake_rebalance", "dispatch_rebalance"}:
        raise ValueError("invalid reservation recovery reason")
    if quantity <= 0:
        return 0
    row = db.execute(select(OrderItem, Order).join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)).one_or_none()
    if row is None:
        return 0
    item, order = row
    if item.supply_mode_snapshot == "external_purchase":
        # Purchase ratios require an explicit, frozen physical-unit transfer.
        return 0
    expected_basis = order_product_basis(db, item.id, item.product_id)
    excluded = set(exclude_lot_ids)
    candidates = db.scalars(select(InventoryLot)
        .join(FinishedGoodsInventoryDetail,
              FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id)
        .where(FinishedGoodsInventoryDetail.product_id == item.product_id,
               FinishedGoodsInventoryDetail.owner_customer_id == order.customer_id,
               FinishedGoodsInventoryDetail.is_general.is_(False),
               InventoryLot.inventory_type == "finished",
               InventoryLot.status == "active",
               InventoryLot.quantity_available > 0)
        .order_by(InventoryLot.stock_date, InventoryLot.id)
        .with_for_update()).all()
    reserved = 0
    for lot in candidates:
        if reserved >= quantity:
            break
        if lot.id in excluded or not matches_lot_stock_identity(lot, expected_basis):
            continue
        if lot.unit != "boxes":
            continue
        amount = min(quantity - reserved, int(lot.quantity_available))
        try:
            reserve_finished_inventory(
                db, order_item_id=item.id, inventory_lot_id=lot.id,
                quantity=amount, expected_version=lot.version,
                operator_id=operator_id,
                idempotency_key="rebalanced:" + sha256(
                    f"{operation_key}:lot:{lot.id}".encode()).hexdigest(),
                warning_acknowledged_codes=[], recovery_reason=reason,
            )
        except WarehouseInventoryError as error:
            # A blocked candidate may be skipped. Every other failure, in
            # particular a concurrent update or a broken ledger, must abort.
            ineligible = (
                "该库存批次当前不可预占",
                "未组装本体仅能抵扣本体生产",
                "该内衬已保留给原订单",
                "该批次已集货待送",
                "库存产品与订单产品不一致",
                "客户专用库存不能用于其他客户订单",
            )
            if error.status_code != 409 or not any(
                str(error).startswith(message) for message in ineligible
            ):
                raise
            continue
        reserved += amount
    return reserved
