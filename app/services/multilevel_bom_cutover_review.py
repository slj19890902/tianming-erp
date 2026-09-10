"""Read-only, exact review of legacy order sources before atomic conversion.

Produces detached current material rows for the remaining commercial quantity.
It never releases reservations, changes old snapshots or persists a graph.
The eventual writer must recreate and compare this manifest under its lock.
"""
from dataclasses import dataclass
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace

from sqlalchemy import select

from app.models.order import Order, OrderItem
from app.models.multilevel_bom import OrderBomGraph, OrderBomExecutionCutover
from app.models.product_bom import SalesOrderItemBomComponent, SalesOrderItemBomDemandAdjustment, BomComponentDirectDeliveryAllocation
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.models.warehouse_inventory import DeliveryInventoryAllocation
from app.models.delivery import Delivery, DeliveryItem
from app.models.production import ProductionTask, ProductionCompletion
from app.services.multilevel_bom_compile import compile_master_order_bom
from app.services.multilevel_bom_compile import CompiledMasterBom
from app.services.multilevel_bom_plan import BomPlanError
from app.services.multilevel_bom_snapshot import dump_graph


@dataclass(frozen=True)
class LegacyCutoverReview:
    compiled: CompiledMasterBom
    history: tuple
    document: str
    checksum: str


def _value(value):
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _row(row):
    return {column.key: _value(getattr(row, column.key)) for column in row.__table__.columns}


def review_legacy_cutover(db, *, order_item_id, customer_id):
    if db.new or db.dirty or db.deleted:
        raise BomPlanError("请先保存或撤销未提交修改，再核对转换")
    db.expire_all()  # Never approve an old identity-map image of changed rows.
    # A read-only review must not implicitly flush unrelated caller edits.
    with db.no_autoflush:
        item = db.get(OrderItem, order_item_id)
        order = db.get(Order, item.order_id) if item else None
        if item is None or order is None or order.customer_id != customer_id:
            raise BomPlanError("转换订单不存在或客户不一致")
        if (item.is_force_closed or item.quantity <= 0 or not 0 <= (item.delivered_quantity or 0) < item.quantity
                or order.status in {"cancelled", "closed", "dead", "completed", "archived", "delivered"}):
            raise BomPlanError("只可转换尚未完成的订单剩余数量")
        if db.get(OrderBomGraph, item.id) is not None or db.get(OrderBomExecutionCutover, item.id) is not None:
            raise BomPlanError("订单已有真实BOM或转换记录，不能覆盖")
        history = tuple(db.scalars(select(SalesOrderItemBomComponent).where(
            SalesOrderItemBomComponent.sales_order_item_id == item.id).order_by(SalesOrderItemBomComponent.id)))
        if not history or any(not 1 <= (row.snapshot_schema_version or 0) <= 4
                or row.order_set_quantity != item.quantity or row.quantity_per_set <= 0
                or row.required_piece_quantity != row.order_set_quantity * row.quantity_per_set for row in history):
            raise BomPlanError("旧BOM来源数量或版本不完整，不能转换")
        remaining = item.quantity - (item.delivered_quantity or 0)
        detached_item = SimpleNamespace(**{column.key: getattr(item, column.key) for column in OrderItem.__table__.columns})
        detached_item.quantity = remaining
        compiled = compile_master_order_bom(db, detached_item)
        # The compiler returns detached rows. Allocate new display positions and
        # leave the disposable template FK empty to avoid old-source uniqueness
        # collisions. Real product IDs/versions stay frozen in the graph.
        offset = max(row.display_order for row in history)
        for position, row in enumerate(compiled.snapshots, offset + 1):
            row.display_order = position
            row.product_bom_component_id = None
        reservations = tuple(db.scalars(select(InventoryReservation).where(
            InventoryReservation.order_item_id == item.id).order_by(InventoryReservation.id)))
        lot_ids = {row.inventory_lot_id for row in reservations}
        lots = tuple(db.scalars(select(InventoryLot).where(InventoryLot.id.in_(lot_ids)).order_by(InventoryLot.id))) if lot_ids else ()
        if len(lots) != len(lot_ids):
            raise BomPlanError("旧预占缺少实际库存来源")
        remaining_by_lot = defaultdict(int)
        for reservation in reservations:
            if reservation.reservation_type == "finished_order":
                remaining_by_lot[reservation.inventory_lot_id] += (
                    reservation.reserved_stock_quantity - reservation.consumed_stock_quantity - reservation.released_stock_quantity)
        from app.services.bom_subkit_costs import source_cost
        costs = []
        for lot in lots:
            take = remaining_by_lot[lot.id]
            if take < 0 or take > lot.quantity_reserved:
                raise BomPlanError("旧预占数量与库存余额不一致")
            if take:
                amount, lineage = source_cost(db, lot, take)
                costs.append({"lot_id": lot.id, "quantity": take, "amount": _value(amount), "lineage": lineage})
        deliveries = db.execute(select(DeliveryItem, Delivery).join(Delivery, Delivery.id == DeliveryItem.delivery_id)
            .where(DeliveryItem.order_item_id == item.id).order_by(DeliveryItem.id)).all()
        def facts(model, condition):
            return [_row(row) for row in db.scalars(select(model).where(condition).order_by(model.id))]
        line_ids = [line.id for line, _ in deliveries]
        payload = {"schema": 1, "order": _row(order), "item": _row(item), "remaining_quantity": remaining,
            "graph": json.loads(dump_graph(compiled.graph)), "history": [_row(row) for row in history],
            "current": [_row(row) for row in compiled.snapshots],
            "reservations": [_row(row) for row in reservations], "lots": [_row(row) for row in lots],
            "remaining_finished_costs": costs,
            "adjustments": facts(SalesOrderItemBomDemandAdjustment,
                SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id.in_([row.id for row in history])),
            "tasks": facts(ProductionTask, ProductionTask.order_item_id == item.id),
            "completions": facts(ProductionCompletion, ProductionCompletion.order_item_id == item.id),
            "direct_allocations": facts(BomComponentDirectDeliveryAllocation,
                BomComponentDirectDeliveryAllocation.delivery_item_id.in_(line_ids)),
            "stock_allocations": facts(DeliveryInventoryAllocation, DeliveryInventoryAllocation.delivery_item_id.in_(line_ids)),
            "deliveries": [{"line": _row(line), "document": _row(delivery)} for line, delivery in deliveries]}
        document = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return LegacyCutoverReview(compiled, history, document, hashlib.sha256(document.encode("utf-8")).hexdigest())
