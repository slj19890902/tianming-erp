"""Reviewed correction of unplaced purchases retained by cancelled orders.

Service entry point for the authorized integration owner; no automatic startup
repair or public request bypass. It never reopens an order or changes quantities.
"""
import json
import re

from fastapi import HTTPException
from sqlalchemy import select, update

from app.models.audit import OperationLog
from app.models.order import Order
from app.models.requisition import RequisitionItem
from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
from app.models.production import ProductionCompletion
from app.models.multilevel_bom import BomAssembly
from app.models.warehouse_inventory import InventoryReservation
from app.services.procurement_sources import order_purchase_lines, line_source_links, order_receipts
from app.services.admin_order_reversal import access, fingerprint, rows_snapshot, _void_order_purchase_lines
from app.services.bom_transactions import atomic_bom


def preview(db, *, user, order_ids, supplier_item_ids, supplier_not_sent_confirmed=False):
    if type(supplier_not_sent_confirmed) is not bool:
        raise HTTPException(422, '未下单事实必须明确确认为布尔值')
    orders = access(db, user, order_ids)
    if not supplier_item_ids or len(set(supplier_item_ids)) != len(supplier_item_ids):
        raise HTTPException(422, '专项更正必须指定唯一采购明细编号')
    item_ids = [i.id for o in orders for i in o.items]
    own = {r.id:r for r in order_purchase_lines(db, item_ids)}
    if not set(supplier_item_ids) <= own.keys():
        raise HTTPException(409, '选中采购来源不属于本订单，拒绝跨单更正')
    rows = [own[i] for i in sorted(supplier_item_ids)]
    links = line_source_links(db, supplier_item_ids)
    sources = list(db.scalars(select(RequisitionItem).where(RequisitionItem.id.in_(
        [l.material_requisition_item_id for l in links if l.material_requisition_item_id])).order_by(RequisitionItem.id)))
    headers = list(db.scalars(select(SupplierRequisitionOrder).where(
        SupplierRequisitionOrder.id.in_({r.supplier_order_id for r in rows})).order_by(SupplierRequisitionOrder.id)))
    shared = list(db.scalars(select(SupplierRequisitionOrderItem).where(
        SupplierRequisitionOrderItem.supplier_order_id.in_([h.id for h in headers])).order_by(SupplierRequisitionOrderItem.id)))
    receipts = order_receipts(db, item_ids)
    completions = list(db.scalars(select(ProductionCompletion).where(ProductionCompletion.order_item_id.in_(item_ids))))
    assemblies = list(db.scalars(select(BomAssembly).where(BomAssembly.order_item_id.in_(item_ids))))
    reservations = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id.in_(item_ids))))
    blockers = []
    if not supplier_not_sent_confirmed:
        blockers.append('必须确认选中采购明细从未向供应商下单；系统不猜测对外承诺事实')
    if any(o.status != 'cancelled' for o in orders):
        blockers.append('该专项仅处理已取消订单的遗留采购；不能恢复或代替正常订单撤回')
    if any(r.status != 'active' for r in rows) or any(l.status != 'active' for l in links):
        blockers.append('选中采购来源已变化，请重新核对版本及历史')
    if any(h.status not in ('confirmed','draft','pending') for h in headers):
        blockers.append('采购单状态不允许未下单更正，请先处理采购承诺或下游')
    if len(links) != len(rows) or len(sources) != len(rows) or any(l.stock_replenishment_item_id for l in links):
        blockers.append('专项需要完整且独立的订单报料来源；不处理补库或缺失来源')
    if any(s.status not in ('已取消','已作废') for s in sources):
        blockers.append('内部报料尚未取消，请走正常关联撤回')
    if any(f.status == 'posted' for f in receipts + completions + assemblies):
        blockers.append('存在有效实收、完工或组套，请先撤销对应实物事实')
    if any(int(r.consumed_stock_quantity or 0) or
        int(r.reserved_stock_quantity or 0) > int(r.released_stock_quantity or 0) for r in reservations):
        blockers.append('订单仍有库存占用或耗用，请先核对下游，专项不调整库存')
    state = [rows_snapshot(r) for r in (orders,rows,links,sources,headers,shared,receipts,completions,assemblies,reservations)]
    return dict(order_ids=sorted(order_ids), supplier_item_ids=sorted(supplier_item_ids),
        supplier_not_sent_confirmed=bool(supplier_not_sent_confirmed),
        reviewed_hash=fingerprint([supplier_not_sent_confirmed,state]),
        executable=not blockers, blockers=blockers,
        before=rows_snapshot(rows), source_links=rows_snapshot(links),
        unchanged_shared_item_ids=[r.id for r in shared if r.id not in supplier_item_ids],
        result='仅撤销选中遗留采购行及其来源链接，重算采购单；订单和内部报料保留已取消状态。')


def execute(db, *, user, order_ids, supplier_item_ids, supplier_not_sent_confirmed,
            reviewed_hash, operation_key, reason):
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', operation_key) or not reason.strip() or len(reason) > 300:
        raise HTTPException(422, '需要有效更正编号及明确事实依据')
    payload_hash = fingerprint([sorted(order_ids),sorted(supplier_item_ids),supplier_not_sent_confirmed,reviewed_hash,reason])
    ref = 'cancelled-procurement:' + operation_key
    with atomic_bom(db):
        orders = access(db, user, order_ids)
        for oid in sorted(order_ids):
            db.execute(update(Order).where(Order.id == oid).values(status=Order.status,updated_at=Order.updated_at))
        db.expire_all()
        replay = db.scalar(select(OperationLog).where(OperationLog.action_code == 'procurement.cancelled_order_correction', OperationLog.object_ref == ref))
        if replay:
            fact = json.loads(replay.details)
            if replay.user_id != user.id or fact['payload_hash'] != payload_hash:
                raise HTTPException(409, '更正编号已用于其他内容或操作人')
            return fact['response']
        plan = preview(db, user=user, order_ids=order_ids, supplier_item_ids=supplier_item_ids,
            supplier_not_sent_confirmed=supplier_not_sent_confirmed)
        if plan['reviewed_hash'] != reviewed_hash:
            raise HTTPException(409, '采购来源、版本或下游已变化，请重新预览')
        if plan['blockers']:
            raise HTTPException(409, '；'.join(plan['blockers']))
        _void_order_purchase_lines(db, user=user, item_ids=[i.id for o in orders for i in o.items],
            supplier_item_ids=supplier_item_ids,operation_key=operation_key,reason=reason)
        response = dict(ok=True, order_ids=sorted(order_ids), supplier_item_ids=sorted(supplier_item_ids),message=plan['result'])
        from app.services.audit_log import append_audit_event
        append_audit_event(db, actor=user,event_category='business',result='success',source='web',resource='Order',
            module_code='orders',action_code='procurement.cancelled_order_correction',entity_type='order',
            entity_id=orders[0].id,object_ref=ref,customer_id=orders[0].customer_id,description='已取消订单未下单采购专项更正',
            details=dict(payload_hash=payload_hash,reason=reason,reviewed_hash=reviewed_hash,
                supplier_not_sent_confirmed=True,before=plan['before'],source_links=plan['source_links'],
                unchanged_shared_item_ids=plan['unchanged_shared_item_ids'],response=response))
        db.flush()
        return response
