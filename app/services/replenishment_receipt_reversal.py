"""Reverse one intact paperboard receipt after its real downstream work is undone.

The calling incoming API owns the transaction and reversal replay record. Frozen
receipt, price, purchase and production facts remain as the historical source.
"""
from sqlalchemy import or_, select, update

from app.core.time_contract import utc_now_naive
from app.models.customer import Customer
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.procurement_source import ProcurementSourceLink
from app.models.stock_preparation import StockPreparationJob
from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
from app.models.warehouse_inventory import (
    InventoryLot, InventoryLotTransfer, InventoryMovement, InventoryPalletItem, InventoryReservation,
)
from app.services.audit_log import append_audit_event
from app.services.warehouse_inventory import WarehouseInventoryError, mutate_lot


def lock_stock_receipt_source(db, *, receipt_id, user):
    """Serialize receipt reversal with purchase withdrawal and new receipts."""
    from app.api.deps import require_customer_access
    from app.services.incoming_receipts import IncomingReceiptError

    def fail(message):
        raise IncomingReceiptError(message, 409)

    if user.role != 'admin':
        raise IncomingReceiptError('仅管理员可撤销补库收料', 403)
    receipt_item = db.get(IncomingReceiptItem, receipt_id)
    if receipt_item is None or receipt_item.stock_replenishment_item_id is None:
        fail('补库来料实收来源不存在')
    stock_id = receipt_item.stock_replenishment_item_id
    item = db.get(StockReplenishmentOrderItem, stock_id)
    if item is None:
        fail('补库收料来源不存在，禁止撤销')
    from app.api.requisition import _require_stock_replenishment_order_access
    source_order = db.get(StockReplenishmentOrder, item.replenishment_order_id)
    if source_order is None:
        fail('补库来源单不存在，禁止撤销')
    _require_stock_replenishment_order_access(db, source_order, user)
    if item.customer_id is not None:
        require_customer_access(item.customer_id, user, db)
    # Match the purchase/source lock order used by purchase withdrawal. Receipt
    # rows store the typed stock source, often without a direct supplier item FK.
    link = db.scalar(select(ProcurementSourceLink).where(
        ProcurementSourceLink.stock_replenishment_item_id == stock_id,
        ProcurementSourceLink.status == 'active'))
    if link is not None:
        supplier_item = db.get(SupplierRequisitionOrderItem, link.supplier_item_id)
        if supplier_item is None or supplier_item.status != 'active':
            fail('关联采购行已变化，请刷新后重试')
        claim = db.execute(update(SupplierRequisitionOrder).where(
            SupplierRequisitionOrder.id == supplier_item.supplier_order_id,
            SupplierRequisitionOrder.status == 'confirmed').values(status=SupplierRequisitionOrder.status))
        if claim.rowcount != 1:
            fail('关联采购单已变化，请刷新后重试')
    claimed = db.execute(update(StockReplenishmentOrder).where(
        StockReplenishmentOrder.id == item.replenishment_order_id,
        StockReplenishmentOrder.status.in_(('confirmed', 'partially_stocked', 'stocked'))
    ).values(status=StockReplenishmentOrder.status))
    if claimed.rowcount != 1:
        fail('补库来源状态已变化，请刷新后重试')
    db.expire_all()
    receipt_item = db.get(IncomingReceiptItem, receipt_id)
    item = db.get(StockReplenishmentOrderItem, stock_id)
    order = db.get(StockReplenishmentOrder, item.replenishment_order_id)
    _require_stock_replenishment_order_access(db, order, user)
    return receipt_item, item, order


def reverse_stock_receipt(db, *, receipt_item, user, reason, idempotency_key, audit_context=None):
    from app.api.deps import require_customer_access
    from app.services.incoming_receipts import IncomingReceiptError
    from app.services.supplier_monthly_settlement import assert_receipt_item_not_in_confirmed_statement, SupplierSettlementError

    def fail(message):
        raise IncomingReceiptError(message, 409)

    receipt_id = receipt_item.id
    receipt_item, item, order = lock_stock_receipt_source(db, receipt_id=receipt_id, user=user)
    stock_id = item.id
    if receipt_item is None or receipt_item.status != 'posted':
        fail('该补库来料实收记录已经撤销')
    if item.customer_id is not None:
        require_customer_access(item.customer_id, user, db)
    try:
        assert_receipt_item_not_in_confirmed_statement(db, receipt_id)
    except SupplierSettlementError as error:
        raise IncomingReceiptError(error.message, error.status_code, code=error.code) from error
    from app.models.raw_purchase_plan import RawPurchasePlan
    if db.scalar(select(RawPurchasePlan.id).where(RawPurchasePlan.stock_item_id == stock_id)):
        fail('这笔原片已绑定订单分配方案，请先处理订单片料来源，不能按独立补库收料撤销')
    latest = db.scalar(select(IncomingReceiptItem.id).where(
        IncomingReceiptItem.stock_replenishment_item_id == stock_id,
        IncomingReceiptItem.status == 'posted').order_by(IncomingReceiptItem.id.desc()).limit(1))
    if latest != receipt_id:
        fail('同一补库来源有更晚的实收记录，请先撤销最新一笔')
    lot = db.get(InventoryLot, receipt_item.received_inventory_lot_id) if receipt_item.received_inventory_lot_id else None
    if (lot is None or lot.inventory_type != 'semi_finished' or lot.source_type != 'replenishment'
            or lot.source_ref_type != 'stock_replenishment_receipt' or lot.source_ref_id != receipt_id):
        fail('本次收料原批次身份不完整或不一致，禁止按产品编码猜批次撤销')
    if lot.warehouse_location_id is None:
        fail('本次收料库存缺少实际位置，请先核对')
    try:
        from app.services.warehouse_inventory import _claim_inventory_restore_destination
        _claim_inventory_restore_destination(db, lot.warehouse_location_id)
    except WarehouseInventoryError as error:
        raise IncomingReceiptError(str(error), error.status_code) from error
    # Hold the lot row while inspecting reservations, jobs and placement. A
    # stale production/warehouse writer loses its version claim after reversal.
    claimed = db.execute(update(InventoryLot).where(
        InventoryLot.id == lot.id, InventoryLot.version == lot.version,
        InventoryLot.status == 'active').values(version=InventoryLot.version))
    if claimed.rowcount != 1:
        fail('收料库存版本或状态已变化，请刷新后重试')
    db.refresh(lot)
    jobs = list(db.scalars(select(StockPreparationJob).where(StockPreparationJob.receipt_item_id == receipt_id)))
    if any(job.status == 'pending' for job in jobs):
        fail('仍有待生产安排，请先到生产与成品→待生产，取消这笔补库的生产安排')
    if any(job.status == 'completed' for job in jobs):
        fail('这笔补库已加工完工，请先到完工历史撤销本次加工/组套')
    for job in jobs:
        output = db.get(InventoryLot, job.output_lot_id) if job.output_lot_id else None
        if output and (output.status != 'closed' or any((output.quantity_available, output.quantity_reserved, output.quantity_consumed, output.quantity_damaged, output.quantity_scrapped))):
            fail('这笔补库的加工产出尚未完整撤销，请先到完工历史核对后续使用')
    if (any((lot.quantity_reserved, lot.quantity_consumed, lot.quantity_damaged, lot.quantity_scrapped))
            or db.scalar(select(InventoryReservation.id).where(
                InventoryReservation.inventory_lot_id == lot.id,
                InventoryReservation.status.in_(('active', 'consumed'))).limit(1))):
        fail('本次收料库存仍被预占、生产使用或损坏报废，请先撤销对应下游业务')
    if lot.quantity_available != receipt_item.received_quantity:
        fail('本次实收原批次数量已变化或拆分，请先核对真实库存流转，不能扩大撤销范围')
    if db.scalar(select(InventoryLotTransfer.id).where(or_(
            InventoryLotTransfer.source_lot_id == lot.id, InventoryLotTransfer.target_lot_id == lot.id)).limit(1)):
        fail('本次收料已拆批移库或并入其他批次，请先按库存流转核对来源，不能撤销混用库存')
    movements = list(db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id == lot.id)))
    initial = [m for m in movements if m.movement_type == 'manual_in']
    if (len(initial) != 1 or initial[0].quantity != receipt_item.received_quantity
            or any(m.movement_type not in {'manual_in', 'reserve', 'release_reserve', 'consume', 'reverse_consume'} for m in movements)):
        fail('本次实收库存已有其他调整或来源流水，请先核对，禁止普通收料撤销')
    pallet = lot.pallet_item.pallet if lot.pallet_item else None
    if pallet:
        if not pallet.is_current or pallet.status != 'active' or pallet.location_id != lot.warehouse_location_id:
            fail('本批栈板位置已变化，请先核对实际存放位置')
        other = db.scalar(select(InventoryLot.id).join(InventoryPalletItem, InventoryPalletItem.inventory_lot_id == InventoryLot.id).where(
            InventoryPalletItem.pallet_id == pallet.id, InventoryLot.id != lot.id,
            InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged > 0).limit(1))
        if other:
            fail('本批栈板包含其他实物，请先核对并分离，不能撤销混用库存')
    before = dict(stocked_quantity=item.stocked_quantity, stock_order_status=order.status,
                  inventory_lot_id=lot.id, quantity=lot.quantity_available, version=lot.version,
                  location_id=lot.warehouse_location_id)
    try:
        mutate_lot(db, lot_id=lot.id, operation='adjust', expected_version=lot.version,
                   operator_id=user.id, quantity=-receipt_item.received_quantity,
                   reason=reason, idempotency_key=f'incoming-stock-revert:{receipt_id}')
        lot.status = 'closed'
        db.flush()
        if pallet:
            from app.services.floor3_locations import clear_pallet, Floor3LocationError
            try:
                clear_pallet(db, pallet_id=pallet.id, expected_version=pallet.version,
                             remarks='撤销补库收料', operator_id=user.id)
            except Floor3LocationError as error:
                raise IncomingReceiptError(str(error), error.status_code) from error
    except WarehouseInventoryError as error:
        raise IncomingReceiptError(str(error), error.status_code) from error
    now = utc_now_naive()
    receipt_item.status = 'reversed'
    receipt_item.reversal_reason, receipt_item.reversed_by, receipt_item.reversed_at = reason, user.id, now
    db.flush()
    if all(row.status == 'reversed' for row in receipt_item.receipt.items):
        receipt_item.receipt.status = 'reversed'
        receipt_item.receipt.reversal_reason = reason
        receipt_item.receipt.reversed_by, receipt_item.receipt.reversed_at = user.id, now
    remaining = list(db.scalars(select(IncomingReceiptItem).where(
        IncomingReceiptItem.stock_replenishment_item_id == stock_id,
        IncomingReceiptItem.status == 'posted').order_by(IncomingReceiptItem.id)))
    item.stocked_quantity = min(item.quantity, sum(row.received_quantity for row in remaining))
    item.inventory_lot_id = remaining[-1].received_inventory_lot_id if remaining else None
    item.inventory_lot = db.get(InventoryLot, item.inventory_lot_id) if item.inventory_lot_id else None
    item.stocked_at = remaining[-1].receipt.received_at if remaining else None
    if remaining and remaining[-1].resolution_action == 'await_supplier':
        remaining[-1].resolution_status = 'pending'
        remaining[-1].resolved_by = remaining[-1].resolved_at = None
    from app.services.replenishment_receipt_progress import refresh_order_progress
    refresh_order_progress(db, order, user.id)
    if not any(row.stocked_quantity for row in order.items):
        order.status, order.stocked_by, order.stocked_at = 'confirmed', None, None
    customer = db.get(Customer, item.customer_id) if item.customer_id else None
    append_audit_event(db, request=(audit_context or {}).get('request'), actor=user,
        event_category='business', result='success', source='web', module_code='incoming',
        action_code='incoming.stock_replenishment.revert', legacy_action='REVERT_MATERIAL',
        resource='IncomingReceiptItem', entity_type='stock_replenishment_item', entity_id=item.id,
        customer_id=item.customer_id, customer_name=customer.name if customer else None,
        description='管理员逐笔撤销补库实收并反冲原批次，保留来源历史',
        details=dict(before=before, incoming_receipt_id=receipt_item.receipt_id,
            incoming_receipt_item_id=receipt_id, stock_replenishment_order_id=order.id,
            stock_replenishment_item_id=stock_id, idempotency_key=idempotency_key,
            after=dict(stocked_quantity=item.stocked_quantity, stock_order_status=order.status,
                       inventory_lot_id=item.inventory_lot_id, receipt_status=receipt_item.status,
                       lot_status=lot.status, quantity=lot.quantity_available, version=lot.version)))
    db.flush()
    return receipt_item
