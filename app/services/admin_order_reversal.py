"""Administrator self-service, preview-bound, atomic order disposition.

Business records are reversed/voided, never erased along with their audit.
No order numbers, product codes or location IDs are special-cased.
"""
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select, inspect, update

from app.models.order import Order, OrderItem
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.production import ProductionCompletion
from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.models.audit import OperationLog
from app.services.bom_transactions import atomic_bom
from app.services.admin_order_reversal_scope import current_location_reversal

LABELS = {'withdraw': '撤回', 'keep_stock': '作废留库', 'delete_trial': '删除试验单'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def rows_snapshot(rows):
    return [{c.key: getattr(row, c.key) for c in inspect(type(row)).columns} for row in rows]


def access(db, user, order_ids):
    from app.api.deps import require_customer_access
    from app.api.orders import _order_group_key
    if user.role != 'admin':
        raise HTTPException(403, '仅管理员可执行订单关联撤销')
    rows = list(db.scalars(select(Order).where(Order.id.in_(order_ids)).order_by(Order.id)))
    if len(rows) != len(order_ids) or not rows:
        raise HTTPException(404, '订单不存在，请刷新列表')
    for row in rows:
        require_customer_access(row.customer_id, current_user=user, db=db)
    if len({_order_group_key(row) for row in rows}) != 1:
        raise HTTPException(409, '只能处理同一个客户订单组')
    return rows


def preview(db, *, user, order_ids, mode):
    from app.api.orders import (DeliveryItem, SupplierRequisitionOrderItem,
        SupplierRequisitionOrder, RequisitionItem, Requisition)
    from app.models.external_packaging_purchase import (ExternalPackagingPurchaseItem,
        ExternalPackagingPurchaseOrder, ExternalPackagingReceiptItem)
    from app.services.external_receipt_state import active_receipt_item
    from app.services.supplier_monthly_settlement import assert_receipt_item_not_in_confirmed_statement
    orders = access(db, user, order_ids)
    items = list(db.scalars(select(OrderItem).where(OrderItem.order_id.in_(order_ids)).order_by(OrderItem.id)))
    ids = [r.id for r in items]
    def own(model):
        return list(db.scalars(select(model).where(model.order_item_id.in_(ids)).order_by(model.id)))
    receipts, completions, assemblies = own(IncomingReceiptItem), own(ProductionCompletion), own(BomAssembly)
    reservations = own(InventoryReservation)
    inputs = list(db.scalars(select(BomAssemblyInput).where(BomAssemblyInput.conversion_id.in_([a.id for a in assemblies])).order_by(BomAssemblyInput.id)))
    lot_ids = {r.inventory_lot_id for r in completions + reservations}
    lot_ids.update(a.output_lot_id for a in assemblies)
    lot_ids.update(r.lot_id for r in inputs)
    lot_ids.update(r.surplus_inventory_lot_id for r in receipts)
    lots = list(db.scalars(select(InventoryLot).where(InventoryLot.id.in_([i for i in lot_ids if i])).order_by(InventoryLot.id)))
    suppliers = own(SupplierRequisitionOrderItem)
    supplier_headers = list(db.scalars(select(SupplierRequisitionOrder).where(SupplierRequisitionOrder.id.in_({r.supplier_order_id for r in suppliers})).order_by(SupplierRequisitionOrder.id)))
    req_items = own(RequisitionItem)
    reqs = list(db.scalars(select(Requisition).where(Requisition.id.in_({r.requisition_id for r in req_items})).order_by(Requisition.id)))
    external = list(db.scalars(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_id.in_(order_ids)).order_by(ExternalPackagingPurchaseItem.id)))
    external_headers = list(db.scalars(select(ExternalPackagingPurchaseOrder).where(ExternalPackagingPurchaseOrder.id.in_({r.purchase_order_id for r in external})).order_by(ExternalPackagingPurchaseOrder.id)))
    external_receipts = list(db.scalars(select(ExternalPackagingReceiptItem).where(ExternalPackagingReceiptItem.purchase_item_id.in_([r.id for r in external]), active_receipt_item()).order_by(ExternalPackagingReceiptItem.id)))
    external_lots = list(db.scalars(select(InventoryLot).where(InventoryLot.source_ref_type=='bom_external_receipt',
        InventoryLot.source_ref_id.in_([r.id for r in external_receipts]))))
    lots = sorted({lot.id:lot for lot in lots + external_lots}.values(),key=lambda lot:lot.id)
    deliveries = own(DeliveryItem)
    blockers = []
    if any(o.status in ('cancelled','dead','closed','archived') for o in orders):
        blockers.append('订单已经关闭或作废，请在历史订单查看处理结果。')
    if deliveries or any(int(i.delivered_quantity or 0) for i in items):
        blockers.append('已有送货记录：请到「送货」撤销发货并删除对应送货草稿后，再预览；已开票或收款须先冲销。')
    if mode != 'keep_stock':
        for row in receipts:
            if row.status == 'posted':
                try:
                    assert_receipt_item_not_in_confirmed_statement(db, row.id)
                except Exception as exc:
                    blockers.append(str(getattr(exc, 'detail', exc)))
    for header in supplier_headers:
        foreign = db.scalar(select(SupplierRequisitionOrderItem.id).where(
            SupplierRequisitionOrderItem.supplier_order_id == header.id,
            SupplierRequisitionOrderItem.order_item_id.not_in(ids)).limit(1))
        if foreign and mode != 'keep_stock' and header.status not in ('voided','cancelled','已作废'):
            blockers.append(f'报料单 {header.order_number} 合并了其他订单，请先在报料管理拆分目标订单；不会删除其他订单来源。')
    for row in external_receipts:
        foreign = db.scalar(select(ExternalPackagingReceiptItem.id).where(
            ExternalPackagingReceiptItem.receipt_id == row.receipt_id,
            ExternalPackagingReceiptItem.purchase_item_id.not_in([r.id for r in external])).limit(1))
        if foreign and mode != 'keep_stock':
            blockers.append(f'外购实收 {row.receipt_id} 包含其他订单，请先在外购收料处理该共享实收。')
    output_ids = {r.inventory_lot_id for r in completions} | {r.output_lot_id for r in assemblies}
    inventory = []
    for lot in lots:
        owned = (lot.id in output_ids or lot.id in {r.surplus_inventory_lot_id for r in receipts}
            or (lot.source_ref_type=='bom_external_receipt' and lot.source_ref_id in {r.id for r in external_receipts}))
        inventory.append(dict(id=lot.id, quantity=int(lot.quantity_available)+int(lot.quantity_reserved),
            reserved=int(lot.quantity_reserved), location_id=lot.warehouse_location_id,
            action='保留' if mode == 'keep_stock' or not owned else '撤销本单入库',
            product_code=(getattr(lot.finished_detail, 'inventory_code_snapshot', None) or f'批次{lot.id}')))
    state = [rows_snapshot(r) for r in (orders,items,receipts,completions,assemblies,inputs,reservations,lots,
        suppliers,supplier_headers,req_items,reqs,external,external_headers,external_receipts,deliveries)]
    return dict(mode=mode, label=LABELS[mode], order_ids=sorted(order_ids),
        orders=[r.order_number for r in orders], reviewed_hash=fingerprint([mode,state]),
        counts=dict(receipts=sum(r.status=='posted' for r in receipts),
            completions=sum(r.status=='posted' for r in completions),
            assemblies=sum(r.status=='posted' for r in assemblies), purchases=len(supplier_headers)+len(external_headers)),
        inventory=inventory, blockers=list(dict.fromkeys(blockers)),
        result='订单作废，实物库存及收料成本保留，释放预占。' if mode=='keep_stock' else
            ('撤销关联组套、完工、收料及报料，订单保留并回到未报料。' if mode=='withdraw' else
             '撤销本单模拟库存及关联单据，从当前订单列表移除；历史与审计保留。'))


def execute(db, *, user, order_ids, mode, reviewed_hash, operation_key, trial_confirmed, reason):
    from app.api import orders as old
    from app.services import audit_log
    from app.services.incoming_receipts import revert_receipt_item
    from app.services.multilevel_bom_inventory import reverse_order_assembly
    from app.services.production_workflow import reverse_production_completion
    from app.services.external_packaging_purchase_lifecycle import cancel_unreceived_external_purchases
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingReceiptItem
    from app.services.external_receipt_state import active_receipt_item
    from app.services.multilevel_bom_external_reversal import reverse_graph_external_receipt
    from app.core.time_contract import beijing_now_naive
    if mode == 'delete_trial' and not trial_confirmed:
        raise HTTPException(409, '请确认本订单是纯模拟，没有真实到货、送货或收付款。真实到货请选择「作废留库」。')
    payload_hash = fingerprint([sorted(order_ids),mode,reviewed_hash,trial_confirmed,reason])
    ref = 'admin-order:' + operation_key
    with atomic_bom(db):
        orders = access(db, user, order_ids)
        # Claim the same order rows without firing their on-update timestamp
        # before comparing the reviewed snapshot.
        for oid in sorted(order_ids):
            db.execute(update(Order).where(Order.id==oid).values(status=Order.status, updated_at=Order.updated_at))
        db.expire_all()
        replay = db.scalar(select(OperationLog).where(OperationLog.action_code=='order.admin_disposition', OperationLog.object_ref==ref))
        if replay:
            fact = json.loads(replay.details)
            if fact['payload_hash'] != payload_hash or replay.user_id != user.id:
                raise HTTPException(409, '该操作编号已用于其他内容，请重新预览')
            return fact['response']
        plan = preview(db, user=user, order_ids=order_ids, mode=mode)
        if plan['reviewed_hash'] != reviewed_hash:
            raise HTTPException(409, '订单或库存已变化，请重新预览后确认')
        if plan['blockers']:
            raise HTTPException(409, '；'.join(plan['blockers']))
        ids = [i.id for o in orders for i in o.items]
        with current_location_reversal(ids):
            if mode != 'keep_stock':
                assemblies = list(db.scalars(select(BomAssembly).where(BomAssembly.order_item_id.in_(ids), BomAssembly.status=='posted').order_by(BomAssembly.id.desc())))
                handled = set()
                for row in assemblies:
                    operation = json.loads(row.cost_detail_json or '{}').get('graph_operation', {})
                    key = operation.get('key')
                    if not key:
                        raise HTTPException(409, f'组套记录{row.id}缺少来源，请在生产记录中先撤销该组套')
                    if (row.order_item_id,key) not in handled:
                        reverse_order_assembly(db, order_item_id=row.order_item_id, operation_key=key,
                            operator_id=user.id, source_snapshot_id=(operation.get('source_ids') or [None])[0])
                        handled.add((row.order_item_id,key))
                manual = list(db.scalars(select(ProductionCompletion).where(ProductionCompletion.order_item_id.in_(ids),
                    ProductionCompletion.status=='posted', ProductionCompletion.origin=='manual').order_by(ProductionCompletion.id.desc())))
                for row in manual:
                    reverse_production_completion(db, completion_id=row.id, operator_id=user.id, reason=reason)
                ext_ids = list(db.scalars(select(ExternalPackagingReceiptItem.receipt_id).join(ExternalPackagingPurchaseItem,
                    ExternalPackagingPurchaseItem.id==ExternalPackagingReceiptItem.purchase_item_id).where(
                    ExternalPackagingPurchaseItem.sales_order_id.in_(order_ids), active_receipt_item()).order_by(ExternalPackagingReceiptItem.id.desc())))
                for rid in dict.fromkeys(ext_ids):
                    reverse_graph_external_receipt(db, receipt_id=rid, idempotency_key=f'{operation_key}:ext:{rid}',
                        reason=reason, user=user, visible_customer_ids={o.customer_id for o in orders})
                receipts = list(db.scalars(select(IncomingReceiptItem).where(IncomingReceiptItem.order_item_id.in_(ids),
                    IncomingReceiptItem.status=='posted').order_by(IncomingReceiptItem.id.desc())))
                for row in receipts:
                    revert_receipt_item(db, user=user, receipt_item_id=row.id, reason=reason,
                        idempotency_key=f'{operation_key}:receipt:{row.id}', audit_context=None)
                old._rollback_supplier_requisition_items(db, order_item_ids=ids)
                for order in orders:
                    cancel_unreceived_external_purchases(db, order_id=order.id, source='order_workflow_rollback', reason=reason, cancelled_by=user.id)
                req_ids = set(db.scalars(select(old.RequisitionItem.requisition_id).where(old.RequisitionItem.order_item_id.in_(ids))))
                from app.models.product_bom import RequisitionItemBomSource
                target_req_ids = list(db.scalars(select(old.RequisitionItem.id).where(old.RequisitionItem.order_item_id.in_(ids))))
                db.execute(update(old.RequisitionItem).where(old.RequisitionItem.id.in_(target_req_ids)).values(status='已取消'))
                db.execute(update(RequisitionItemBomSource).where(RequisitionItemBomSource.requisition_item_id.in_(target_req_ids)).values(active_guard=None))
                for rid in req_ids:
                    foreign = db.scalar(select(old.RequisitionItem.id).where(old.RequisitionItem.requisition_id==rid,
                        old.RequisitionItem.order_item_id.not_in(ids)).limit(1))
                    if not foreign:
                        db.get(old.Requisition, rid).status = '已作废'
            if mode == 'keep_stock':
                from app.services.warehouse_inventory import release_finished_reservation
                for reservation in db.scalars(select(InventoryReservation).where(
                    InventoryReservation.order_item_id.in_(ids), InventoryReservation.reservation_type=='finished_order',
                    InventoryReservation.reserved_stock_quantity > InventoryReservation.consumed_stock_quantity + InventoryReservation.released_stock_quantity)):
                    release_finished_reservation(db, reservation_id=reservation.id, operator_id=user.id,
                        release_reason=reason, idempotency_key=f'{operation_key}-keep-{reservation.id}',
                        allow_downstream=True, allow_production_reversal=True)
            old._release_order_reservations(db, order_item_ids=ids, operator_id=user.id,
                reason=reason, idempotency_prefix=operation_key, allow_downstream=True)
            old._invalidate_requisition_holds_before_item_delete(db, item_ids=ids, user=user, request=None)
            old._unlink_predelivery_order_bindings(db, order_ids=order_ids, reason=reason)
            for order in orders:
                if mode == 'withdraw':
                    for item in order.items:
                        item.is_force_closed = False
                        item.material_status = 'pending'
                        item.material_received_at = item.material_received_by = None
                        item.inventory_deducted_qty = 0
                        item.requisition_qty = None
                        item.requisition_status = '未报料'
                        item.requisition_date = item.supplier_order_number = None
                    order.status = 'pending_production'
                else:
                    order.status = 'cancelled'
                order.updated_at = beijing_now_naive()
            response = dict(ok=True, mode=mode, order_ids=sorted(order_ids), message=plan['result'])
            audit_log.append_audit_event(db, actor=user, event_category='business', result='success', source='web',
                resource='Order', module_code='orders', action_code='order.admin_disposition',
                entity_type='order', entity_id=orders[0].id, object_ref=ref, customer_id=orders[0].customer_id,
                description=LABELS[mode], details=dict(payload_hash=payload_hash, reason=reason,
                    trial_confirmed=trial_confirmed, response=response, counts=plan['counts'], inventory=plan['inventory']))
            db.flush()
            return response
