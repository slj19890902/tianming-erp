"""Read-only employee projection of the existing quantity ledger."""
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased, selectinload
from app.models.customer import Customer
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.incoming_receipt import IncomingReceiptItem as ReceiptItem
from app.models.stock_replenishment import StockReplenishmentOrderItem as StockItem
from app.models.purchase_receipt import IncomingReceiptPurposeAllocation as PurposeAllocation
from app.models.warehouse_inventory import (
    InventoryLot, InventoryMovement, InventoryLotTransfer,
    FinishedGoodsInventoryDetail as Finished, SemiFinishedInventoryDetail as Semi,
    SemiFinishedLotAllowedProduct as Binding,
)
from app.services.asset_time_archive import _location_labels, _operator_names, _transfer_movement_key
from app.services.warehouse_display_units import lot_display_unit

LABELS = {
    'manual_in': '入库', 'adjust': '盘点 / 数量调整', 'freeze': '冻结', 'unfreeze': '解除冻结',
    'damage': '报损', 'scrap': '报废', 'transfer_to_general': '转为通用库存',
    'location_transfer': '移库', 'reserve': '订单占用', 'release_reserve': '解除订单占用',
    'consume': '出库 / 生产领用', 'reverse_consume': '撤销出库 / 退回',
    'return_in': '退货入库', 'return_reconsume': '撤销退货',
}

# A partial move creates a new lot, while the receipt continues to point at
# the original one. Only a recorded lot transfer can carry that provenance.
_lineage = select(InventoryLot.id.label('lot_id'),
                  InventoryLot.id.label('ancestor_id')).cte('movement_lot_lineage', recursive=True)
_lineage = _lineage.union(select(_lineage.c.lot_id,
    InventoryLotTransfer.source_lot_id).join(InventoryLotTransfer,
        InventoryLotTransfer.target_lot_id == _lineage.c.ancestor_id))

def customer_condition(customer_id):
    return or_(InventoryLot.finished_detail.has(Finished.owner_customer_id == customer_id),
               InventoryLot.semi_finished_detail.has(Semi.owner_customer_id == customer_id))


def _stock_source_query(*, keyword=None, lot_ids=None, visible_customer_ids=None):
    """A receipt's own lot and customer establish provenance, never usage."""
    root = aliased(InventoryLot)
    root_detail = aliased(Semi)
    query = select(InventoryLot.id, StockItem.product_code_snapshot,
                   StockItem.product_name_snapshot).join(
        Semi, Semi.inventory_lot_id == InventoryLot.id).join(
        _lineage, _lineage.c.lot_id == InventoryLot.id).join(
        root, root.id == _lineage.c.ancestor_id).join(
        root_detail, and_(root_detail.inventory_lot_id == root.id,
            root_detail.owner_customer_id == Semi.owner_customer_id)).join(
        ReceiptItem, and_(root.source_ref_type == 'stock_replenishment_receipt',
            ReceiptItem.id == root.source_ref_id,
            ReceiptItem.received_inventory_lot_id == root.id,
            InventoryLot.source_ref_type == root.source_ref_type,
            InventoryLot.source_ref_id == root.source_ref_id)).join(
        StockItem, and_(StockItem.id == ReceiptItem.stock_replenishment_item_id,
            StockItem.target_inventory_type == 'semi_finished',
            StockItem.customer_id == Semi.owner_customer_id)).where(
        InventoryLot.inventory_type == 'semi_finished',
        Semi.owner_customer_id.is_not(None),
        func.trim(StockItem.product_code_snapshot) != '')
    if keyword is not None:
        query = query.where(or_(StockItem.product_code_snapshot.contains(keyword, autoescape=True),
                                StockItem.product_name_snapshot.contains(keyword, autoescape=True)))
    if lot_ids is not None:
        query = query.where(InventoryLot.id.in_(lot_ids))
    if visible_customer_ids is not None:
        query = query.where(Semi.owner_customer_id.in_(visible_customer_ids))
    return query


def _order_purpose_source_query(*, keyword=None, lot_ids=None, visible_customer_ids=None):
    """A frozen order-line identity survives a merged supplier purchase line."""
    root = aliased(InventoryLot)
    root_detail = aliased(Semi)
    query = select(InventoryLot.id, OrderItem.snapshot_product_code,
                   OrderItem.snapshot_product_name).join(
        Semi, Semi.inventory_lot_id == InventoryLot.id).join(
        _lineage, _lineage.c.lot_id == InventoryLot.id).join(
        root, root.id == _lineage.c.ancestor_id).join(
        root_detail, and_(root_detail.inventory_lot_id == root.id,
            root_detail.owner_customer_id == Semi.owner_customer_id)).join(
        PurposeAllocation, and_(PurposeAllocation.semi_finished_inventory_lot_id == root.id,
            PurposeAllocation.incoming_receipt_item_id == root.source_ref_id,
            root.source_ref_type == 'incoming_receipt_item',
            InventoryLot.source_ref_type == root.source_ref_type,
            InventoryLot.source_ref_id == root.source_ref_id,
            PurposeAllocation.customer_id == Semi.owner_customer_id)).join(
        ReceiptItem, ReceiptItem.id == PurposeAllocation.incoming_receipt_item_id).join(
        OrderItem, OrderItem.id == PurposeAllocation.source_order_item_id).join(
        Order, and_(Order.id == OrderItem.order_id, Order.customer_id == Semi.owner_customer_id)).where(
        InventoryLot.inventory_type == 'semi_finished',
        Semi.owner_customer_id.is_not(None),
        func.trim(OrderItem.snapshot_product_code) != '')
    if keyword is not None:
        query = query.where(or_(OrderItem.snapshot_product_code.contains(keyword, autoescape=True),
                                OrderItem.snapshot_product_name.contains(keyword, autoescape=True)))
    if lot_ids is not None:
        query = query.where(InventoryLot.id.in_(lot_ids))
    if visible_customer_ids is not None:
        query = query.where(Semi.owner_customer_id.in_(visible_customer_ids))
    return query


def source_identity_condition(keyword, visible_customer_ids=None):
    sources = (_stock_source_query(keyword=keyword, visible_customer_ids=visible_customer_ids),
               _order_purpose_source_query(keyword=keyword, visible_customer_ids=visible_customer_ids))
    return or_(*(InventoryLot.id.in_(source.with_only_columns(InventoryLot.id))
                 for source in sources))


def _stable_source_products(rows):
    facts = {(str(code).strip(), name, kind) for code, name, kind in rows
             if code and str(code).strip()}
    return [dict(code=code, name=name, kind=kind)
            for code, name, kind in sorted(facts, key=lambda row: (row[0].casefold(), row[1] or '', row[2]))]


def source_identities(db, lot_ids, visible_customer_ids=None):
    """Return stable, distinct source facts for this visible page."""
    if not lot_ids:
        return {}
    evidence = {}
    for kind, query in (
        ('stock_replenishment', _stock_source_query(lot_ids=lot_ids, visible_customer_ids=visible_customer_ids)),
        ('order_purchase_reserve', _order_purpose_source_query(lot_ids=lot_ids, visible_customer_ids=visible_customer_ids)),
    ):
        for lot_id, code, name in db.execute(query):
            evidence.setdefault(lot_id, []).append((code, name, kind))
    return {lot_id: products for lot_id, rows in evidence.items()
            if (products := _stable_source_products(rows))}

def keyword_condition(keyword, visible_customer_ids=None):
    def contains(column): return column.contains(keyword.strip(), autoescape=True)
    customer = or_(contains(Customer.name), contains(Customer.customer_code), contains(Customer.chinese_short_name))
    product = or_(contains(Product.product_code), contains(Product.product_name))
    if visible_customer_ids is not None:
        product = product & Product.customer_id.in_(visible_customer_ids)
    return or_(
        InventoryLot.finished_detail.has(or_(contains(Finished.inventory_code_snapshot), contains(Finished.product_name_snapshot),
            contains(Finished.owner_customer_name_snapshot), Finished.customer.has(customer))),
        InventoryLot.semi_finished_detail.has(or_(contains(Semi.internal_name), contains(Semi.owner_customer_name_snapshot), Semi.customer.has(customer))),
        InventoryLot.allowed_products.any(Binding.product.has(product)),
        source_identity_condition(keyword.strip(), visible_customer_ids),
    )

def movement_load_options():
    lot = selectinload(InventoryMovement.lot)
    return (lot.selectinload(InventoryLot.finished_detail).selectinload(Finished.customer),
            lot.selectinload(InventoryLot.finished_detail).selectinload(Finished.product),
            lot.selectinload(InventoryLot.semi_finished_detail).selectinload(Semi.customer),
            lot.selectinload(InventoryLot.allowed_products).selectinload(Binding.product))

def enrich_movements(db, rows, serialize, *, visible_customer_ids=None):
    if not rows: return []
    lot_ids = {row.inventory_lot_id for row in rows}
    sources = source_identities(db, lot_ids, visible_customer_ids)
    transfers = db.scalars(select(InventoryLotTransfer).where(or_(
        InventoryLotTransfer.source_lot_id.in_(lot_ids), InventoryLotTransfer.target_lot_id.in_(lot_ids)))).all()
    transfer_by_key = { _transfer_movement_key(t.idempotency_key, side): t for t in transfers for side in ('source', 'target') }
    locations = _location_labels(db, {row.lot.warehouse_location_id for row in rows} |
        {value for t in transfers for value in (t.source_location_id, t.target_location_id)})
    operators = _operator_names(db, {row.operator_id for row in rows if row.operator_id})
    result=[]
    for row in rows:
        item=serialize(row, include_sensitive_details=visible_customer_ids is None)
        lot=row.lot
        detail=lot.finished_detail or lot.semi_finished_detail
        customer=detail.customer if detail else None
        code=lot.finished_detail.inventory_code_snapshot if lot.finished_detail else None
        name=lot.finished_detail.product_name_snapshot if lot.finished_detail else (lot.semi_finished_detail.internal_name if lot.semi_finished_detail else None)
        if not code and lot.semi_finished_detail:
            products=[binding.product for binding in lot.allowed_products if binding.product and
                      (visible_customer_ids is None or binding.product.customer_id in visible_customer_ids)]
            code=' / '.join(dict.fromkeys(p.product_code for p in products)) or None
            name=name or '片料'
        before=sum(getattr(row,'before_'+key) for key in ('available','reserved','damaged'))
        after=sum(getattr(row,'after_'+key) for key in ('available','reserved','damaged'))
        label=LABELS.get(row.movement_type,'库存变更')
        transfer=transfer_by_key.get(row.idempotency_key)
        if transfer:
            if transfer.source_lot_id!=transfer.target_lot_id:
                label='移库转出' if row.inventory_lot_id==transfer.source_lot_id else '移库转入'
        item.update(customer_id=getattr(detail,'owner_customer_id',None),
            customer_name=(customer.chinese_short_name or customer.customer_code or customer.name) if customer else getattr(detail,'owner_customer_name_snapshot',None) or '通用 / 未指定客户',
            inventory_code=code,product_name=name or '产品资料待完善',
            display_unit=lot_display_unit(lot),operation_label=label,
            before_physical=before,after_physical=after,physical_delta=after-before,
            quantity_scope='本次涉及库存，非全仓合计',
            reason_display=row.reason if visible_customer_ids is None and row.reason else label,
            operator_name=operators.get(row.operator_id,'系统 / 历史未记录'),
            current_location=locations.get(lot.warehouse_location_id,'位置待核'),
            from_location=locations.get(transfer.source_location_id) if transfer else None,
            to_location=locations.get(transfer.target_location_id) if transfer else None,
            transfer_quantity=transfer.quantity if transfer else None)
        source_products = sources.get(lot.id, [])
        single_source = source_products[0] if len(source_products) == 1 else None
        item.update(source_products=source_products,
                    source_product_code=single_source['code'] if single_source else None,
                    source_product_name=single_source['name'] if single_source else None,
                    source_kind=single_source['kind'] if single_source else None)
        result.append(item)
    return result
