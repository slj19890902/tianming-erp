"""Reverse a graph receipt as one audited transaction, never delete its facts."""
import hashlib
import json
from sqlalchemy import select, update

from app.models.external_packaging_purchase import (
    ExternalPackagingReceipt, ExternalPackagingReceiptItem, ExternalPackagingReceiptReversal,
    ExternalPackagingPurchaseItem,
)
from app.models.order import Order, OrderItem
from app.models.supplier_settlement import SupplierMonthlyStatement, SupplierMonthlyStatementLine
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom
from app.services.bom_subkits import SubkitError
from app.services.bom_subkit_inventory import _only_reversed_graph_consumptions
from app.services.external_packaging_purchase import ExternalPurchaseContractError
from app.services.external_receipt_state import active_receipt_item
from app.services.multilevel_bom_external_identity import external_receipt_execution_contract
from app.services.multilevel_bom_external_costs import validated_external_lot_detail
from app.services.multilevel_bom_inventory import reverse_order_assembly
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_receipts import graph_material_receipts_closed, refresh_graph_main_task
from app.services.warehouse_inventory import _balances, _movement, release_finished_reservation


def _reverse_output(db, row, *, order_item_id, user, direct=False):
    if direct:
        from app.services.external_legacy_stock import record
        cutover = record(db, order_item_id)
        if cutover and cutover['receipt_item_id'] == row.id:
            raise SubkitError('该旧实收已确认历史发货及实物余货，不能整体撤销实收；请先单独核对旧单退货与余货')
    source = 'direct_external_receipt' if direct else 'bom_external_receipt'
    lots = list(db.scalars(select(InventoryLot).where(InventoryLot.source_ref_type == source,
        InventoryLot.source_ref_id == row.id)))
    if not row.converted_finished_quantity:
        if lots:
            raise SubkitError('无整件实收却有库存，不能撤销')
        return
    if not lots:
        raise SubkitError('实收库存身份不完整，请先核对来源')
    # A transfer retains the same receipt provenance; prove its complete family.
    from app.services.production_reversal_transfers import transfer_family, reverse_transferred_completion
    from app.models.warehouse_inventory import InventoryLotTransfer
    child_ids = set(db.scalars(select(InventoryLotTransfer.target_lot_id).where(
        InventoryLotTransfer.target_lot_id.in_([lot.id for lot in lots]),
        InventoryLotTransfer.source_lot_id != InventoryLotTransfer.target_lot_id)))
    roots = [lot for lot in lots if lot.id not in child_ids]
    if len(roots) != 1:
        raise SubkitError('实收库存来源链不完整，不能按数量猜测撤销')
    lot = roots[0]
    family_ids, transfers = transfer_family(db, [lot.id])
    if family_ids != {entry.id for entry in lots}:
        raise SubkitError('实收移库批次来源不一致')
    if direct:
        item = db.get(OrderItem, order_item_id)
        order = db.get(Order, item.order_id)
        detail = json.loads(lot.cost_snapshot_detail_json or '{}')
        if (lot.finished_detail is None or lot.finished_detail.product_id != item.product_id
                or lot.finished_detail.owner_customer_id != order.customer_id
                or detail.get('external_receipt_item_id') != row.id
                or detail.get('external_purchase_item_id') != row.purchase_item_id):
            raise SubkitError('外购成品收料库存身份不一致')
    else:
        validated_external_lot_detail(db, lot)
    if transfers:
        from types import SimpleNamespace
        from app.services.production_workflow import ProductionWorkflowError
        output = SimpleNamespace(id=row.id, inventory_lot_id=lot.id,
            stock_quantity=row.converted_finished_quantity, order_item_id=order_item_id)
        try:
            reverse_transferred_completion(db, completion=output, operator_id=user.id,
                reason='撤销外购实收', source_ref_type=source)
        except ProductionWorkflowError as error:
            raise SubkitError(str(error)) from error
        return
    reservation = db.scalar(select(InventoryReservation).where(
        InventoryReservation.idempotency_key == (f'direct-external-reserve:{row.id}' if direct
            else f'bom-external-pick:{row.receipt_id}:{lot.id}')))
    if reservation is None and not direct:
        waiting = list(db.scalars(select(InventoryReservation).where(
            InventoryReservation.inventory_lot_id == lot.id,
            InventoryReservation.order_item_id == order_item_id,
            InventoryReservation.idempotency_key.startswith(f'bom-wait-assembly:{lot.id}:'),
            InventoryReservation.status.in_(('active', 'partial')))))
        if len(waiting) == 1:
            reservation = waiting[0]
    released = False
    if reservation is not None:
        movement = db.scalar(select(InventoryMovement).where(
            InventoryMovement.idempotency_key == reservation.idempotency_key))
        if (reservation.inventory_lot_id != lot.id or reservation.order_item_id != order_item_id
                or reservation.consumed_stock_quantity or reservation.released_stock_quantity
                or movement is None or movement.movement_type != 'reserve'
                or movement.inventory_lot_id != lot.id or movement.reservation_id != reservation.id
                or movement.quantity != reservation.reserved_stock_quantity
                or not _only_reversed_graph_consumptions(db, lot, ignored_reserve_id=movement.id)):
            raise SubkitError('外购自动预占已有后续使用，请先还原后续操作')
        release_finished_reservation(db, reservation_id=reservation.id, operator_id=user.id,
            release_reason='撤销外购实收自动预占', idempotency_key=f'bom-external-unreserve:{row.id}',
            allow_downstream=True, allow_production_reversal=True)
        db.refresh(lot)
        released = True
    if (lot.status != 'active' or lot.quantity_available != row.converted_finished_quantity
            or lot.quantity_reserved or lot.quantity_consumed or lot.quantity_damaged or lot.quantity_scrapped
            or (lot.version != 1 and not released and not _only_reversed_graph_consumptions(db, lot))):
        raise SubkitError('外购库存已移库、盘点或使用，请先还原后续操作')
    before = _balances(lot)
    result = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id, InventoryLot.version == lot.version).values(
        quantity_available=0, quantity_consumed=row.converted_finished_quantity, status='closed', version=lot.version+1))
    if result.rowcount != 1:
        raise SubkitError('外购库存已变化，请刷新重试')
    db.refresh(lot)
    _movement(db, lot=lot, movement_type='consume', quantity=row.converted_finished_quantity, before=before,
        operator_id=user.id, reason='撤销外购实收库存', idempotency_key=f'bom-external-reverse:{row.id}')


def reverse_graph_external_receipt(db, *, receipt_id, idempotency_key, reason, user, visible_customer_ids):
    reason = reason.strip()
    if not reason or len(reason) > 500 or not idempotency_key.strip() or len(idempotency_key) > 120:
        raise ExternalPurchaseContractError('撤销原因或请求编号无效', status_code=422)
    fingerprint = hashlib.sha256(json.dumps({'receipt_id':receipt_id, 'reason':reason}, sort_keys=True).encode()).hexdigest()
    with atomic_bom(db):
        receipt = db.get(ExternalPackagingReceipt, receipt_id)
        if receipt is None:
            raise ExternalPurchaseContractError('实收记录不存在', status_code=404)
        rows = list(db.scalars(select(ExternalPackagingReceiptItem).where(ExternalPackagingReceiptItem.receipt_id == receipt.id)))
        items = {}
        graphs = {}
        current_items = set()
        direct_items = set()
        for row in rows:
            purchase = db.get(ExternalPackagingPurchaseItem, row.purchase_item_id)
            item = db.get(OrderItem, purchase.sales_order_item_id) if purchase and purchase.sales_order_item_id else None
            order = db.get(Order, item.order_id) if item else None
            if order is None or (visible_customer_ids is not None and order.customer_id not in visible_customer_ids):
                raise ExternalPurchaseContractError('无权撤销该客户实收或订单来源不完整', status_code=403)
            if purchase.purchase_order_id != receipt.purchase_order_id or purchase.sales_order_id != order.id:
                raise SubkitError('外购实收订单身份不一致')
            from app.services.direct_external_finished import eligible, conversions
            if eligible(db, item):
                # Also rejects receipt-only historical rows; never manufactures missing stock.
                conversions(db, [(purchase, 0)], customer_id=order.customer_id)
                items[item.id] = item
                direct_items.add(item.id)
                continue
            link, graph = external_receipt_execution_contract(db, row.id)
            if link is None or link.order_item_id != item.id or graph is None or graph.graph.customer_id != order.customer_id:
                raise SubkitError('该实收不是完整真实BOM来源，不能按组套撤销')
            if item.id in graphs and {s.id for s in graphs[item.id].snapshots} != {s.id for s in graph.snapshots}:
                raise SubkitError('同次外购实收混用了不同BOM版本，须核实原来源')
            current = read_compiled_order_bom(db, item.id)
            execution_source = next(s.id for s in graph.snapshots if s.component_product_id == link.product_id)
            if execution_source in {s.id for s in current.snapshots}:
                current_items.add(item.id)
            items[item.id] = item
            graphs[item.id] = graph
        if not rows:
            raise SubkitError('实收明细缺失，不能撤销')
        existing = db.get(ExternalPackagingReceiptReversal, receipt_id)
        if existing:
            if existing.idempotency_key != idempotency_key or existing.request_fingerprint != fingerprint:
                raise SubkitError('该实收已撤销，请刷新后查看')
            return existing, False
        if db.scalar(select(ExternalPackagingReceiptReversal.receipt_id).where(ExternalPackagingReceiptReversal.idempotency_key == idempotency_key)):
            raise SubkitError('撤销请求编号已用于其他内容')
        for row in rows:
            if db.scalar(select(ExternalPackagingReceiptItem.id).where(
                    ExternalPackagingReceiptItem.purchase_item_id == row.purchase_item_id,
                    ExternalPackagingReceiptItem.id > row.id, active_receipt_item()).limit(1)):
                raise SubkitError('同采购行还有后续实收，请从最后一次实收开始撤销')
        statement = db.scalar(select(SupplierMonthlyStatement.id).join(SupplierMonthlyStatementLine,
            SupplierMonthlyStatementLine.statement_id == SupplierMonthlyStatement.id).where(
                SupplierMonthlyStatementLine.external_receipt_item_id.in_([r.id for r in rows]),
                SupplierMonthlyStatement.status != 'voided').limit(1))
        if statement:
            raise SubkitError('实收已进入供应商月结，请先作废相关月结记录')
        for oid, graph in graphs.items():
            own_rows = [r for r in rows if db.get(ExternalPackagingPurchaseItem, r.purchase_item_id).sales_order_item_id == oid]
            if any(r.converted_finished_quantity for r in own_rows) and any(n.source == 'assembled' for n in graph.graph.nodes):
                reverse_order_assembly(db, order_item_id=oid, operation_key=f'bom-external-receipt:{receipt.id}:{oid}',
                    operator_id=user.id, source_snapshot_id=graph.snapshots[0].id)
            for row in own_rows:
                _reverse_output(db, row, order_item_id=oid, user=user)
        for row in rows:
            oid = db.get(ExternalPackagingPurchaseItem, row.purchase_item_id).sales_order_item_id
            if oid in direct_items:
                _reverse_output(db, row, order_item_id=oid, user=user, direct=True)
        reversal = ExternalPackagingReceiptReversal(receipt_id=receipt.id, idempotency_key=idempotency_key,
            request_fingerprint=fingerprint, reason=reason, reversed_by=user.id)
        db.add(reversal)
        db.flush()
        for item in items.values():
            if item.id not in current_items:
                # Historical receipts carry no implicit credit for the new
                # rule. Reversing them must not rewrite its task or status.
                continue
            if not graph_material_receipts_closed(db, item):
                item.material_status = 'pending'
                if item.requisition_status == '已入库':
                    item.requisition_status = '已报料'
                item.material_received_at = item.material_received_by = None
            refresh_graph_main_task(db, item, create_if_missing=False)
        append_audit_event(db, event_category='business', result='success', source='web',
            module_code='external_packaging_receiving', action_code='external_packaging.receipt.reverse',
            resource='ExternalPackagingReceipt', actor=user, entity_type='external_packaging_receipt',
            entity_id=receipt.id, description='撤销外购BOM实收',
            details={'reason':reason, 'receipt_item_ids':[r.id for r in rows], 'order_item_ids':sorted(items), 'original_facts_preserved':True})
        db.flush()
        return reversal, True
