"""Bounded, customer-scoped BOM receipt history; no procurement prices."""
from sqlalchemy import select, func, or_

from app.models.external_packaging_purchase import (
    ExternalPackagingReceipt as Receipt, ExternalPackagingReceiptItem as Row,
    ExternalPackagingPurchaseOrder as Purchase, ExternalPackagingPurchaseBatch as Batch,
    ExternalPackagingPurchaseItem as Item, ExternalPackagingReceiptReversal as Reversal,
)
from app.models.multilevel_bom import OrderBomExternalComponent as Link
from app.models.order import Order
from app.models.customer import Customer


def list_bom_receipts(db, *, visible_customer_ids, page, page_size, keyword=None):
    linked = select(Row.id).join(Item, Item.id == Row.purchase_item_id).join(
        Link, Link.external_component_id == Item.order_component_id).where(
        Row.receipt_id == Receipt.id, Link.order_item_id == Item.sales_order_item_id).exists()
    wrong_owner = select(Row.id).join(Item, Item.id == Row.purchase_item_id).where(
        Row.receipt_id == Receipt.id, or_(Item.purchase_order_id != Receipt.purchase_order_id,
            Item.sales_order_id.is_(None), Item.sales_order_id != Order.id)).correlate(Receipt, Order).exists()
    query = select(Receipt, Purchase, Order, Customer, Reversal.receipt_id).join(
        Purchase, Purchase.id == Receipt.purchase_order_id).join(Batch, Batch.id == Purchase.batch_id).join(
        Order, Order.id == Batch.sales_order_id).join(Customer, Customer.id == Order.customer_id).outerjoin(
        Reversal, Reversal.receipt_id == Receipt.id).where(linked, ~wrong_owner)
    if visible_customer_ids is not None:
        query = query.where(Order.customer_id.in_(visible_customer_ids))
    if keyword and keyword.strip():
        value = keyword.strip()
        query = query.where(or_(*(column.contains(value, autoescape=True) for column in (
            Receipt.receipt_number, Purchase.purchase_number, Purchase.supplier_name_snapshot,
            Order.order_number, Customer.name))))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    headers = db.execute(query.order_by(Receipt.id.desc()).offset((page-1)*page_size).limit(page_size)).all()
    ids = [row[0].id for row in headers]
    details = {}
    if ids:
        rows = db.execute(select(Row, Item.product_name_snapshot, Link.external_component_id).join(
            Item, Item.id == Row.purchase_item_id).outerjoin(Link,
            (Link.external_component_id == Item.order_component_id) & (Link.order_item_id == Item.sales_order_item_id)
        ).where(Row.receipt_id.in_(ids)).order_by(Row.id)).all()
        for row, name, link in rows:
            details.setdefault(row.receipt_id, []).append({"product_name": name,
                "quantity": format(row.received_quantity.normalize(), "f"), "unit": row.purchase_unit_snapshot,
                "bom": link is not None})
    return {"total": total, "page": page, "page_size": page_size, "items": [
        {"id": receipt.id, "receipt_number": receipt.receipt_number,
         "purchase_number": purchase.purchase_number, "supplier_name": purchase.supplier_name_snapshot,
         "order_number": order.order_number, "customer_name": customer.name,
         "received_at": receipt.received_at.isoformat() + "Z" if receipt.received_at else None,
         "reversed": reversed_id is not None, "items": details.get(receipt.id, []),
         "supports_reversal": bool(details.get(receipt.id)) and all(r["bom"] for r in details[receipt.id])}
        for receipt, purchase, order, customer, reversed_id in headers]}
