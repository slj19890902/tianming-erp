"""Product provenance through receipt/job FKs and real lot moves, never code guesses.

This relation is for finding physical stock. It grants no right to consume it.
"""
from sqlalchemy import select, union
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.stock_replenishment import StockReplenishmentOrderItem as Stock, StockReplenishmentOrder as StockOrder
from app.models.incoming_receipt import IncomingReceiptItem as Receipt
from app.models.purchase_receipt import IncomingReceiptPurposeAllocation as Purpose
from app.models.stock_preparation import StockPreparationJob as Job
from app.models.warehouse_inventory import InventoryLot as Lot, InventoryLotTransfer as Move, SemiFinishedInventoryDetail as Semi


def source_relation():
    roots = []
    for field in (Stock.product_id, Stock.reference_product_id):
        roots.append(select(field.label('product_id'), Stock.customer_id.label('customer_id'),
            Receipt.received_inventory_lot_id.label('lot_id')).select_from(Receipt).join(Stock,
                Stock.id == Receipt.stock_replenishment_item_id).join(StockOrder).where(
                    field.is_not(None), Receipt.status == 'posted', StockOrder.status != 'voided'))
        roots.append(select(field, Stock.customer_id, Stock.inventory_lot_id).join(StockOrder).where(
            field.is_not(None), StockOrder.status.notin_(('draft', 'voided'))))
    roots.append(select(OrderItem.product_id, Order.customer_id, Receipt.received_inventory_lot_id)
        .select_from(Receipt).join(OrderItem, OrderItem.id == Receipt.order_item_id).join(Order)
        .where(Receipt.status == 'posted'))
    roots.append(select(OrderItem.product_id, Order.customer_id, Purpose.semi_finished_inventory_lot_id)
        .select_from(Purpose).join(OrderItem, OrderItem.id == Purpose.source_order_item_id).join(Order)
        .join(Receipt, Receipt.id == Purpose.incoming_receipt_item_id).where(
            Receipt.status == 'posted', Purpose.customer_id == Order.customer_id))
    roots.append(select(Job.product_id, Stock.customer_id, Job.output_lot_id).select_from(Job).join(
        Receipt, Receipt.id == Job.receipt_item_id).join(Stock, Stock.id == Receipt.stock_replenishment_item_id)
        .join(StockOrder).join(Product, Product.id == Job.product_id).join(Lot, Lot.id == Job.output_lot_id).where(
            Job.status == 'completed', Receipt.status == 'posted', StockOrder.status.notin_(('draft','voided')),
            Product.customer_id == Stock.customer_id, Lot.source_ref_type == 'stock_preparation', Lot.source_ref_id == Job.id))
    root = union(*roots).subquery('inventory_product_source_roots')
    lineage = select(root.c.product_id, root.c.customer_id, Lot.id.label('lot_id'),
        Lot.source_ref_type, Lot.source_ref_id).join(Lot, Lot.id == root.c.lot_id).join(
            Semi, Semi.inventory_lot_id == Lot.id).join(Product, Product.id == root.c.product_id).where(
                Lot.inventory_type == 'semi_finished', Semi.owner_customer_id == root.c.customer_id,
                Product.customer_id == root.c.customer_id).cte('inventory_product_source_lineage', recursive=True)
    return lineage.union(select(lineage.c.product_id, lineage.c.customer_id, Lot.id,
        Lot.source_ref_type, Lot.source_ref_id).select_from(lineage).join(Move, Move.source_lot_id == lineage.c.lot_id)
        .join(Lot, Lot.id == Move.target_lot_id).join(Semi, Semi.inventory_lot_id == Lot.id).where(
            Lot.inventory_type == 'semi_finished', Semi.owner_customer_id == lineage.c.customer_id,
            Lot.source_ref_type.is_not_distinct_from(lineage.c.source_ref_type),
            Lot.source_ref_id.is_not_distinct_from(lineage.c.source_ref_id)))


def source_products(db, lot_ids, visible_customer_ids=None):
    if not lot_ids:
        return {}
    relation = source_relation()
    query = select(relation.c.lot_id, Product).join(Product, Product.id == relation.c.product_id).where(
        relation.c.lot_id.in_(lot_ids))
    if visible_customer_ids is not None:
        query = query.where(relation.c.customer_id.in_(visible_customer_ids))
    result = {}
    for lot_id, product in db.execute(query):
        result.setdefault(lot_id, []).append(dict(product_id=product.id, code=product.product_code,
            customer_material_code=product.customer_material_code, name=product.product_name, kind='document_source'))
    return result
