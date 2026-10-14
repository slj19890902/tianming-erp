"""Resolve purchase ownership from stable IDs, including retained typed history.

No commits, inventory writes, or inference from product codes/order numbers.
Callers own authorization, status gates and the audit transaction.
"""
from sqlalchemy import func, or_, select

from app.models.incoming_receipt import IncomingReceiptItem
from app.models.procurement_source import ProcurementSourceLink
from app.models.requisition import RequisitionItem
from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem


def order_purchase_lines(db, item_ids, *, active_only=False):
    linked = select(ProcurementSourceLink.supplier_item_id).join(RequisitionItem,
        RequisitionItem.id == ProcurementSourceLink.material_requisition_item_id
    ).where(RequisitionItem.order_item_id.in_(item_ids))
    query = select(SupplierRequisitionOrderItem).where(or_(
        SupplierRequisitionOrderItem.order_item_id.in_(item_ids),
        SupplierRequisitionOrderItem.id.in_(linked)))
    if active_only:
        query = query.join(SupplierRequisitionOrder,
            SupplierRequisitionOrder.id == SupplierRequisitionOrderItem.supplier_order_id).where(
            SupplierRequisitionOrderItem.status == 'active',
            func.lower(func.trim(SupplierRequisitionOrder.status)).notin_(
                ('voided','cancelled','canceled','withdrawn','invalid','已作废','已取消','已撤回')))
    return list(db.scalars(query.order_by(SupplierRequisitionOrderItem.id)))


def line_source_links(db, line_ids):
    return list(db.scalars(select(ProcurementSourceLink).where(
        ProcurementSourceLink.supplier_item_id.in_(line_ids)
    ).order_by(ProcurementSourceLink.id)))


def line_receipt_clause(line, link=None):
    clauses = [IncomingReceiptItem.supplier_order_item_id == line.id]
    if link is not None:
        if link.material_requisition_item_id:
            clauses.append(IncomingReceiptItem.requisition_item_id == link.material_requisition_item_id)
        if link.stock_replenishment_item_id:
            clauses.append(IncomingReceiptItem.stock_replenishment_item_id == link.stock_replenishment_item_id)
    return or_(*clauses)


def order_receipts(db, item_ids, *, posted_only=False):
    lines = order_purchase_lines(db, item_ids)
    links = {r.supplier_item_id:r for r in line_source_links(db, [r.id for r in lines])}
    clause = or_(IncomingReceiptItem.order_item_id.in_(item_ids),
        *[line_receipt_clause(row, links.get(row.id)) for row in lines])
    query = select(IncomingReceiptItem).where(clause)
    if posted_only:
        query = query.where(IncomingReceiptItem.status == 'posted')
    return list(db.scalars(query.order_by(IncomingReceiptItem.id)))
