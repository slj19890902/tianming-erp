"""Read-only employee projection of the existing quantity ledger."""
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload
from app.models.customer import Customer
from app.models.product import Product
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

def customer_condition(customer_id):
    return or_(InventoryLot.finished_detail.has(Finished.owner_customer_id == customer_id),
               InventoryLot.semi_finished_detail.has(Semi.owner_customer_id == customer_id))

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
        result.append(item)
    return result
