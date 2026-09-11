"""Extra liner consumption accompanying a physical parent-carton delivery."""
from decimal import Decimal, ROUND_HALF_UP
import json
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.bom_subkit import OrderSubkit, SubkitConversion, SubkitDeliveryAllocation
from app.models.delivery import DeliveryItem
from app.models.warehouse_inventory import InventoryLot
from app.services.bom_subkits import SubkitError
from app.services.warehouse_inventory import _balances, _movement, inventory_fifo_sort_key


def _lots(db, order_item_id):
    return list(db.scalars(select(InventoryLot).join(SubkitConversion, SubkitConversion.id == InventoryLot.source_ref_id).where(
        InventoryLot.source_ref_type == "subkit_conversion",
        SubkitConversion.order_item_id == order_item_id, SubkitConversion.status == "posted",
        InventoryLot.status == "active", InventoryLot.quantity_available > 0)))


def limit_by_subkit_stock(db: Session, quantities: dict[int, int]) -> dict[int, int]:
    result = dict(quantities)
    if not result:
        return result
    for group in db.scalars(select(OrderSubkit).where(OrderSubkit.order_item_id.in_(result))):
        available = sum(lot.quantity_available for lot in _lots(db, group.order_item_id))
        result[group.order_item_id] = min(result[group.order_item_id], available // group.kits_per_parent)
    return result


def consume_delivery_subkits(db: Session, *, delivery_item_id: int, operator_id: int, operation_key: str):
    line = db.get(DeliveryItem, delivery_item_id)
    group = db.get(OrderSubkit, line.order_item_id)
    if group is None:
        return
    existing = list(db.scalars(select(SubkitDeliveryAllocation).where(
        SubkitDeliveryAllocation.delivery_item_id == line.id, SubkitDeliveryAllocation.reversed.is_(False))))
    needed = int(line.delivered_quantity) * group.kits_per_parent
    if existing:
        if sum(r.quantity for r in existing) != needed or any(r.operation_key != operation_key for r in existing):
            raise SubkitError("该送货的子套件扣减记录已存在，请刷新")
        return
    from app.services.bom_transactions import atomic_bom
    with atomic_bom(db):
        lots = sorted(_lots(db, line.order_item_id), key=inventory_fifo_sort_key)
        if sum(lot.quantity_available for lot in lots) < needed:
            raise SubkitError("内衬套件数量不足，不能只送内盒")
        for lot in lots:
            take = min(needed, lot.quantity_available)
            if not take:
                break
            version = lot.version
            before = _balances(lot)
            from app.services.fixed_shelf_staging import staging_owner
            owner = staging_owner(db, lot.id)
            if owner and owner != line.id:
                raise SubkitError("内衬已为其他送货单集货")
            from app.services.bom_subkit_costs import delivery_cost
            cost, cost_detail = delivery_cost(db, lot, take)
            changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id,
                InventoryLot.version == version, InventoryLot.quantity_available >= take).values(
                quantity_available=InventoryLot.quantity_available - take,
                quantity_consumed=InventoryLot.quantity_consumed + take, version=InventoryLot.version + 1))
            if changed.rowcount != 1:
                raise SubkitError("内衬库存已变化，请刷新")
            db.refresh(lot)
            movement = _movement(db, lot=lot, movement_type="consume", quantity=take,
                before=before, operator_id=operator_id, reason="子套件随父件送货",
                idempotency_key=f"{operation_key}:subkit:{lot.id}", related_order_item_id=line.order_item_id,
                related_delivery_id=line.delivery_id)
            db.flush()
            if lot.estimated_unit_cost_snapshot is None:
                raise SubkitError("内衬成本来源不完整")
            db.add(SubkitDeliveryAllocation(delivery_item_id=line.id, lot_id=lot.id, quantity=take,
                total_cost=cost, cost_detail_json=json.dumps(cost_detail),
                operation_key=operation_key, consume_movement_id=movement.id, reversed=False))
            needed -= take
        db.flush()


def reverse_delivery_subkits(db: Session, *, delivery_item_id: int, operator_id: int, operation_key: str):
    from app.services.bom_transactions import atomic_bom
    with atomic_bom(db):
        for allocation in db.scalars(select(SubkitDeliveryAllocation).where(
            SubkitDeliveryAllocation.delivery_item_id == delivery_item_id,
            SubkitDeliveryAllocation.reversed.is_(False))):
            lot = db.get(InventoryLot, allocation.lot_id)
            if lot is None or lot.quantity_consumed < allocation.quantity:
                raise SubkitError("内衬送货消耗记录不完整")
            before = _balances(lot)
            changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id,
                InventoryLot.version == lot.version, InventoryLot.quantity_consumed >= allocation.quantity).values(
                quantity_available=InventoryLot.quantity_available + allocation.quantity,
                quantity_consumed=InventoryLot.quantity_consumed - allocation.quantity, version=InventoryLot.version + 1))
            if changed.rowcount != 1:
                raise SubkitError("内衬库存已变化，请刷新")
            db.refresh(lot)
            _movement(db, lot=lot, movement_type="reverse_consume", quantity=allocation.quantity,
                before=before, operator_id=operator_id, reason="撤销父件送货恢复内衬",
                idempotency_key=f"{operation_key}:subkit:{allocation.id}", reversal_of_movement_id=allocation.consume_movement_id)
            allocation.reversed = True
        db.flush()
