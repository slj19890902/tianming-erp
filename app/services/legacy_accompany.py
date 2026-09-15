"""Automatic remaining-only handoff for receipt-auto legacy accompanying goods.

Caller owns commit/rollback and delivery permissions. Historical dispatches are
never reconstructed from today's stock. Production snapshots remain untouched.
"""
import hashlib
import json
from dataclasses import replace

from sqlalchemy import select, update

from app.models.multilevel_bom import LegacyAccompanyContract, OrderBomGraph, ProductBomProfile
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, FinishedGoodsInventoryDetail
from app.services.warehouse_inventory import WarehouseInventoryError, _balances, _movement, _number
from app.core.time_contract import utc_now_naive


def contract(db, item_id):
    row = db.get(LegacyAccompanyContract, item_id)
    if row is None:
        return None
    if hashlib.sha256(row.document_json.encode()).hexdigest() != row.content_hash:
        raise WarehouseInventoryError('随货配套冻结记录校验失败', 409)
    doc = json.loads(row.document_json)
    for part in doc['parts']:
        snapshot = db.get(SalesOrderItemBomComponent, part['snapshot_id'])
        if snapshot is None or snapshot.sales_order_item_id != item_id or snapshot.component_product_id != part['product_id']:
            raise WarehouseInventoryError('随货配套来源已失效', 409)
    return doc


def frozen_demands(db, item_id, demands):
    doc = contract(db, item_id)
    if doc is None:
        return None
    by_id = {d.snapshot_id: d for d in demands}
    return [replace(by_id[p['snapshot_id']], quantity_per_set=p['per_set'],
        effective_sets=doc['quantity']-doc['delivered_before'],
        required_piece_quantity=(doc['quantity']-doc['delivered_before'])*p['per_set'],
        delivered_before_cutover=doc['delivered_before'], unit=p['unit'],
        is_required=True, show_on_delivery=False, frozen_delivery_mode='parent_delivery') for p in doc['parts']]


def _eligible_lot(db, lot, snapshot, customer_id):
    from app.services.fixed_shelf_staging import staging_owner
    from app.services.bom_subkits import require_free_subkit_stock, SubkitError
    from app.services.finished_stock_identity import matching_component_basis
    detail = lot.finished_detail
    if (lot.status != 'active' or lot.inventory_type != 'finished' or detail is None
            or detail.is_general or detail.owner_customer_id != customer_id
            or detail.product_id != snapshot.component_product_id
            or lot.unit != 'boxes' or staging_owner(db, lot.id)):
        return False
    try:
        require_free_subkit_stock(db, lot)
    except SubkitError:
        return False
    # Preserve the existing flat-order identity contract; no cross-product or
    # generic stock substitution. Graph orders never enter this adapter.
    return matching_component_basis(db, snapshot, lot)


def prepare_order(db, item_id, actor):
    from app.services.bom_accompany import legacy_accompany_preview
    from app.services.composite_bom_workflow import _stock_reservations, _remaining_reservation_quantity, _delivered_component_quantity
    from app.services.production_workflow import lock_order_rows_for_production_transition
    from app.services.audit_log import append_audit_event
    item = db.get(OrderItem, item_id)
    if item is None or db.get(OrderBomGraph, item_id) is not None:
        return False
    doc = contract(db, item_id)
    preview = [] if doc else legacy_accompany_preview(db, item, 1)
    if doc is None and not preview:
        return False
    if actor is None or not actor.is_active:
        raise WarehouseInventoryError('请登录后准备随货配套', 403)
    if doc is None and actor.role != 'admin':
        raise WarehouseInventoryError('旧单首次配套由管理员打开送货操作后自动准备', 403)
    order = db.get(Order, item.order_id)
    from app.api.deps import require_customer_access
    require_customer_access(order.customer_id, actor, db)
    lock_order_rows_for_production_transition(db, [order.id])
    db.refresh(item)
    from app.services.order_status_policy import order_item_forward_block_reason, order_item_forward_block_message
    reason = order_item_forward_block_reason(order_status=order.status, ordered_quantity=item.quantity,
        delivered_quantity=item.delivered_quantity, is_force_closed=item.is_force_closed)
    if reason:
        raise WarehouseInventoryError(order_item_forward_block_message(reason, action='准备随货配套', order_status=order.status), 409)
    if int(item.quantity or 0) <= int(item.delivered_quantity or 0):
        return bool(doc)
    if doc and (item.quantity != doc['quantity'] or item.delivered_quantity < doc['delivered_before']):
        raise WarehouseInventoryError('订单数量或历史已送边界已变化，不能沿用原配套预占', 409)
    if doc is None:
        from app.services.composite_bom_workflow import effective_component_demands
        effective = {d.snapshot_id:d for d in effective_component_demands(db,item_id)}
        root = db.get(ProductBomProfile, item.product_id)
        if root and root.source in ('assembled', 'separate'):
            raise WarehouseInventoryError('组装或子件交货订单不能按普通随货配套转换', 409)
        parts = []
        for p in preview:
            snapshot = db.get(SalesOrderItemBomComponent, p['component_snapshot_id'])
            if effective[snapshot.id].required_piece_quantity != item.quantity * int(snapshot.quantity_per_set):
                raise WarehouseInventoryError('该旧单有专用配套数量调整，需先核对实际配套数量，不能按模板覆盖', 409)
            product = db.get(Product, snapshot.component_product_id)
            profile = db.get(ProductBomProfile, product.id)
            if profile and profile.source in ('assembled', 'separate'):
                raise WarehouseInventoryError('成套子件必须使用已组装库存，不能自动转换旧子件账', 409)
            if product.deleted_at is not None or not product.is_active:
                raise WarehouseInventoryError('随货子件已停用，请核实产品', 409)
            parts.append(dict(snapshot_id=snapshot.id, product_id=product.id,
                per_set=int(snapshot.quantity_per_set), unit=p['unit'],
                consumed_before=_delivered_component_quantity(db, snapshot.id)))
        doc = dict(schema=1, order_item_id=item.id, customer_id=order.customer_id,
            quantity=item.quantity, delivered_before=int(item.delivered_quantity or 0), parts=parts)
    # Preflight all parts before touching quantities. Nested transaction also
    # protects callers which catch a domain error and keep working.
    with db.begin_nested():
        if db.get(LegacyAccompanyContract, item_id) is None:
            document = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            db.add(LegacyAccompanyContract(order_item_id=item_id, document_json=document,
                content_hash=hashlib.sha256(document.encode()).hexdigest(), created_by=actor.id))
            db.flush()
            append_audit_event(db, actor=actor, event_category='business', result='success', source='api',
                module_code='deliveries', action_code='delivery.legacy_accompany.frozen',
                resource='sales_order_items', entity_id=item_id, customer_id=order.customer_id,
                description='自动冻结旧单剩余随货配套，不补扣历史', details=doc)
        for part in doc['parts']:
            snapshot = db.get(SalesOrderItemBomComponent, part['snapshot_id'])
            target = (doc['quantity'] - doc['delivered_before']) * part['per_set']
            reserved = sum(_remaining_reservation_quantity(r) for r in _stock_reservations(db, snapshot.id))
            need = max(target - _delivered_component_quantity(db, snapshot.id) - reserved, 0)
            if not need:
                continue
            lots = list(db.scalars(select(InventoryLot).join(FinishedGoodsInventoryDetail).where(
                FinishedGoodsInventoryDetail.product_id == part['product_id'],
                FinishedGoodsInventoryDetail.owner_customer_id == order.customer_id,
                InventoryLot.quantity_available > 0).order_by(InventoryLot.stock_date, InventoryLot.id)))
            lots = [lot for lot in lots if _eligible_lot(db, lot, snapshot, order.customer_id)]
            available = sum(l.quantity_available for l in lots)
            if available < need:
                raise WarehouseInventoryError(f"随货配套 {snapshot.snapshot_component_product_name} 需预占{need}{part['unit']}，可用{available}，缺{need-available}；请先补足该产品库存", 409)
            for lot in lots:
                quantity = min(need, lot.quantity_available)
                if not quantity:
                    continue
                from app.services.warehouse_inventory import _claim_inventory_restore_destination
                _claim_inventory_restore_destination(db, lot.warehouse_location_id)
                before = _balances(lot)
                version = lot.version
                now = utc_now_naive()
                changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id,
                    InventoryLot.version == version, InventoryLot.quantity_available >= quantity).values(
                    quantity_available=InventoryLot.quantity_available-quantity,
                    quantity_reserved=InventoryLot.quantity_reserved+quantity,
                    version=InventoryLot.version+1, last_movement_at=now))
                if changed.rowcount != 1:
                    raise WarehouseInventoryError('配套库存已变化，请重试', 409)
                key = f'legacy-accompany:{item_id}:{snapshot.id}:{lot.id}:{version}'
                reservation = InventoryReservation(reservation_number=_number('BRS'), inventory_lot_id=lot.id,
                    reservation_type='finished_order', order_id=order.id, order_item_id=item_id,
                    sales_order_item_bom_component_id=snapshot.id, reserved_stock_quantity=quantity,
                    credited_requirement_quantity=quantity, yield_factor=1, status='active',
                    reserved_by=actor.id, reserved_at=now, idempotency_key=key)
                db.add(reservation); db.flush(); db.refresh(lot)
                _movement(db, lot=lot, movement_type='reserve', quantity=quantity, before=before,
                    operator_id=actor.id, reason='旧单剩余随货自动预占，不补扣历史', idempotency_key=key,
                    reservation_id=reservation.id, related_order_id=order.id, related_order_item_id=item_id)
                need -= quantity
                if not need:
                    break
        db.flush()
    return True


def prepare_delivery(db, delivery, actor):
    from app.models.delivery import DeliveryItem
    if delivery.status != 'pending' or delivery.is_historical_backfill:
        return
    for item_id in db.scalars(select(DeliveryItem.order_item_id).where(
            DeliveryItem.delivery_id == delivery.id, DeliveryItem.is_current.is_(True),
            DeliveryItem.order_item_id.is_not(None)).distinct()):
        prepare_order(db, item_id, actor)
