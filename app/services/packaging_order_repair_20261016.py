"""Finite repair of the two owner-confirmed order 9790 delivery lines.

Preview and apply have no HTTP route.  The caller owns commit, backup and
operator authentication.  Every expected identity and quantity is rechecked
inside the write transaction; stale facts abort the entire correction.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.delivery import Delivery, DeliveryItem
from app.models.external_packaging_purchase import ExternalPackagingPurchaseCancellation
from app.models.finance import FinanceIdempotencyRecord, ReturnReceipt, ReturnReceiptItem
from app.models.order import Order, OrderItem
from app.models.unordered_delivery_order_attribution import UnorderedDeliveryOrderAttribution
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot, InventoryReservation, UnorderedFinishedDeliveryAllocation,
)
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom
from app.services.warehouse_inventory import (
    WarehouseInventoryError, mutate_lot, release_finished_reservation,
)


ACTION = "packaging_order_20261016_repair"
RECEIPT_KEY = "packaging-order-9790-20261016"
LINES = ((1096, 100, 10628, 3719, 924, 100), (1097, 101, 10629, 3744, 1473, 100))


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise WarehouseInventoryError(reason, 409)


def _facts(db: Session, actor: User) -> dict:
    _require(actor is not None and actor.is_active and actor.role == "admin", "仅正式管理员可执行本次历史更正")
    order = db.get(Order, 9790)
    delivery = db.get(Delivery, 155)
    _require(order is not None and order.order_number == "TM20261009004" and order.customer_id == 137,
             "目标订单身份已变化")
    _require(delivery is not None and delivery.customer_id == 137 and delivery.status == "dispatched"
             and delivery.delivery_number == "YG-20261015-001", "目标送货单身份或状态已变化")
    _require(delivery.version == 4, "送货单版本已变化，请重新核账")
    line_facts = []
    for line_id, allocation_id, item_id, product_id, source_lot_id, quantity in LINES:
        line = db.get(DeliveryItem, line_id)
        allocation = db.get(UnorderedFinishedDeliveryAllocation, allocation_id)
        item = db.get(OrderItem, item_id)
        _require(line is not None and line.delivery_id == delivery.id and line.is_current
                 and line.source_type == "unordered_finished" and line.product_id == product_id
                 and line.delivered_quantity == quantity and line.customer_po_snapshot == "2026109",
                 f"送货明细 {line_id} 的冻结来源已变化")
        _require(allocation is not None and allocation.delivery_item_id == line_id
                 and allocation.inventory_lot_id == source_lot_id and allocation.status == "dispatched"
                 and allocation.consumed_quantity == quantity and allocation.restored_quantity == 0
                 and allocation.consume_movement_id is not None,
                 f"送货明细 {line_id} 的实际出库事实已变化")
        _require(item is not None and item.order_id == order.id and item.product_id == product_id
                 and item.quantity == quantity and item.delivered_quantity == 0 and not item.is_force_closed
                 and item.requisition_status == "未报料", f"订单明细 {item_id} 已变化")
        _require(db.scalar(select(UnorderedDeliveryOrderAttribution.id).where(
                 UnorderedDeliveryOrderAttribution.delivery_item_id == line_id)) is None,
                 f"送货明细 {line_id} 已关联订单")
        _require(db.scalar(select(DeliveryItem.id).where(
                 DeliveryItem.order_item_id == item_id, DeliveryItem.is_current.is_(True))) is None,
                 f"订单明细 {item_id} 已有其他送货")
        _require(db.scalar(select(ReturnReceiptItem.id)
                 .join(ReturnReceipt, ReturnReceipt.id == ReturnReceiptItem.return_receipt_id)
                 .where(ReturnReceiptItem.delivery_item_id == line_id,
                        ReturnReceipt.status != "cancelled")) is None,
                 f"送货明细 {line_id} 已有有效回单事实，请重新核对")
        line_facts.append(dict(delivery_item_id=line_id, allocation_id=allocation_id,
            order_item_id=item_id, product_id=product_id, source_lot_id=source_lot_id,
            quantity=quantity, delivery_unit_price=str(line.unit_price_snapshot),
            order_unit_price=str(item.unit_price)))
    lot_rows = []
    for lot_id, expected in (
        (923, (2700, 100, 0, 8)),
        (924, (2700, 0, 100, 8)),
        (1473, (3500, 100, 100, 3)),
    ):
        lot = db.get(InventoryLot, lot_id)
        _require(lot is not None and lot.status == "active" and
                 (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed, lot.version) == expected,
                 f"库存批次 {lot_id} 已变化，请重新核账")
        lot_rows.append(dict(id=lot_id, available=lot.quantity_available,
                             reserved=lot.quantity_reserved, consumed=lot.quantity_consumed,
                             version=lot.version))
    reservations = []
    for reserve_id, item_id, lot_id in ((1067, 10628, 923), (1068, 10629, 1473)):
        reserve = db.get(InventoryReservation, reserve_id)
        _require(reserve is not None and reserve.order_item_id == item_id
                 and reserve.inventory_lot_id == lot_id and reserve.status == "active"
                 and reserve.reserved_stock_quantity == 100
                 and reserve.consumed_stock_quantity == 0 and reserve.released_stock_quantity == 0,
                 f"预占 {reserve_id} 已变化，请重新核账")
        reservations.append(dict(id=reserve_id, item_id=item_id, lot_id=lot_id))
    _require(all(db.get(ExternalPackagingPurchaseCancellation, cid) is not None
                 for cid in (10, 11)), "已撤销的包材采购事实已变化")
    facts = dict(order_id=order.id, delivery_id=delivery.id,
        delivery_version=delivery.version, lines=line_facts, lots=lot_rows,
        reservations=reservations, purchase_cancellation_ids=[10, 11],
        physical_confirmation="老板确认原80020549实物2800，出库100后剩2700；两款各100属于本单")
    facts["reviewed_hash"] = hashlib.sha256(json.dumps(
        facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return facts


def preview(db: Session, *, actor: User) -> dict:
    return _facts(db, actor)


def apply(db: Session, *, actor: User, reviewed_hash: str) -> dict:
    with atomic_bom(db):
        existing = db.scalar(select(FinanceIdempotencyRecord).where(
            FinanceIdempotencyRecord.idempotency_key == RECEIPT_KEY))
        if existing is not None:
            result = json.loads(existing.response_json)
            _require(existing.request_hash == reviewed_hash,
                     "本次历史更正已提交，申请与原核对快照不一致")
            return {**result, "replayed": True}
        facts = _facts(db, actor)
        _require(facts["reviewed_hash"] == reviewed_hash,
                 "正式业务事实已变化，请重新预览本次更正")
        for reserve_id in (1067, 1068):
            release_finished_reservation(
                db, reservation_id=reserve_id, operator_id=actor.id,
                release_reason="已核实的无订单送货归属本订单，释放后来重复预占",
                idempotency_key=f"{RECEIPT_KEY}:release:{reserve_id}",
            )
        lot = db.get(InventoryLot, 923)
        _require(lot.quantity_available == 2800 and lot.quantity_reserved == 0,
                 "重复盘点批次余额变化，本次更正取消")
        mutate_lot(db, lot_id=923, operation="adjust", expected_version=lot.version,
                   operator_id=actor.id, quantity=-2800,
                   reason="重复盘点登记更正：原始实物总量2800，已由批次924出库100，批次923不代表额外实物",
                   idempotency_key=f"{RECEIPT_KEY}:duplicate-stocktake")
        for line in facts["lines"]:
            db.add(UnorderedDeliveryOrderAttribution(
                delivery_item_id=line["delivery_item_id"],
                order_item_id=line["order_item_id"], quantity=line["quantity"],
                reason="老板确认该无订单送货实际履行TM20261009004；原送货、出库及单价保持",
                actor_user_id=actor.id,
                operation_key=f"{RECEIPT_KEY}:attribute:{line['delivery_item_id']}",
            ))
            db.get(OrderItem, line["order_item_id"]).delivered_quantity = line["quantity"]
        db.flush()
        from app.api.deliveries import _refresh_order_status
        _refresh_order_status(db, 9790)
        _require(db.get(InventoryLot, 923).quantity_available == 0
                 and db.get(InventoryLot, 924).quantity_available == 2700
                 and db.get(InventoryLot, 1473).quantity_available == 3600,
                 "更正后库存数量不符，已整体回滚")
        result = dict(order_id=9790, delivery_id=155,
                      delivery_item_ids=[1096, 1097], order_item_ids=[10628, 10629],
                      duplicate_lot_id=923, duplicate_quantity_corrected=2800,
                      target_stock=dict(product_80020549=2700, product_82021188=3600),
                      purchase_gap=0, replayed=False)
        db.add(FinanceIdempotencyRecord(
            idempotency_key=RECEIPT_KEY, request_hash=reviewed_hash,
            action=ACTION, actor_user_id=actor.id, resource_type="sales_order",
            resource_id=9790, response_json=json.dumps(result, ensure_ascii=False, sort_keys=True),
        ))
        append_audit_event(db, event_category="business", result="success",
            source="script", module_code="orders", action_code=ACTION,
            resource="sales_order", actor=actor, entity_type="sales_order",
            entity_id=9790, customer_id=137, request_id=RECEIPT_KEY,
            description="修复重复盘点与已发货订单关联，原送货和价格不变",
            details=dict(reviewed_hash=reviewed_hash, before=facts, after=result,
                         price_difference_82021188=str(
                             (Decimal("3.292") - Decimal("3.077")) * Decimal(100)),
                         finance_policy="保留已发送货单价，不自动改对账或发票"))
        db.flush()
        return result
