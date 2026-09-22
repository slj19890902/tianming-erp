"""Placement is a projection of live staging stock, not completion history."""
from sqlalchemy import select, func, or_, exists
from app.models.warehouse_inventory import (
    InventoryLot, FinishedGoodsInventoryDetail, WarehouseLocation, WarehouseArea,
    WarehouseFloor, InventoryReservation, InventoryLotTransfer,
)
from app.models.receipt_putaway import ReceiptStagingArea
from app.models.customer import Customer
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.services.warehouse_inventory import WarehouseInventoryError, transfer_finished_lot_between_locations
from app.services.receipt_putaway import is_staging_location
from app.services.warehouse_location_address import employee_location_name


def page(db, *, allowed_customer_ids, page=1, page_size=20, customer_id=None,
         order_keyword=None, product_code=None, product_name=None,
         completed_date_from=None, completed_date_to=None, placement_state='pending', count_only=False):
    physical = InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged
    staging = exists(select(ReceiptStagingArea.area_id).join(WarehouseArea,
        WarehouseArea.id == ReceiptStagingArea.area_id).join(WarehouseFloor,
        WarehouseFloor.id == WarehouseArea.floor_id).where(
        WarehouseArea.area_code == WarehouseLocation.area_code,
        WarehouseFloor.floor_number == WarehouseLocation.warehouse_floor))
    query = select(InventoryLot, FinishedGoodsInventoryDetail, Customer, Product, WarehouseLocation).join(
        FinishedGoodsInventoryDetail, FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id).join(
        Customer, Customer.id == FinishedGoodsInventoryDetail.owner_customer_id).join(
        Product, Product.id == FinishedGoodsInventoryDetail.product_id).join(
        WarehouseLocation, WarehouseLocation.id == InventoryLot.warehouse_location_id).where(
        InventoryLot.inventory_type == 'finished', InventoryLot.status.in_(['active','frozen']),
        physical > 0)
    if placement_state == 'pending':
        query = query.where(staging)
    elif placement_state == 'placed':
        query = query.where(~staging)
    if allowed_customer_ids is not None:
        query = query.where(Customer.id.in_(allowed_customer_ids))
    if customer_id is not None:
        query = query.where(Customer.id == customer_id)
    for term, columns in ((product_code, (Product.product_code, Product.customer_material_code, FinishedGoodsInventoryDetail.inventory_code_snapshot)),
                          (product_name, (Product.product_name, FinishedGoodsInventoryDetail.product_name_snapshot))):
        if term and term.strip():
            query = query.where(or_(*(c.contains(term.strip()) for c in columns)))
    if completed_date_from:
        query = query.where(InventoryLot.stock_date >= completed_date_from)
    if completed_date_to:
        query = query.where(InventoryLot.stock_date <= completed_date_to)
    if order_keyword and order_keyword.strip():
        term=order_keyword.strip()
        query=query.where(exists(select(InventoryReservation.id).join(OrderItem,
            OrderItem.id==InventoryReservation.order_item_id).join(Order,Order.id==OrderItem.order_id).where(
            InventoryReservation.inventory_lot_id==InventoryLot.id,
            or_(Order.customer_po.contains(term),Order.order_number.contains(term),OrderItem.item_order_number.contains(term)))))
    total=db.scalar(select(func.count()).select_from(query.subquery())) or 0
    if count_only:
        return int(total)
    rows=db.execute(query.order_by(InventoryLot.stock_date.desc(),InventoryLot.id.desc()).offset((page-1)*page_size).limit(page_size)).all()
    result=[]
    for lot,detail,customer,product,location in rows:
        orders=list(db.execute(select(Order.customer_po,Order.order_number).join(OrderItem,OrderItem.order_id==Order.id).join(
            InventoryReservation,InventoryReservation.order_item_id==OrderItem.id).where(
            InventoryReservation.inventory_lot_id==lot.id,
            InventoryReservation.status.notin_(['cancelled','released','consumed']),
            InventoryReservation.reserved_stock_quantity > InventoryReservation.consumed_stock_quantity + InventoryReservation.released_stock_quantity).distinct().order_by(Order.id)))
        pending = is_staging_location(db, location)
        block='库存已冻结' if lot.status!='active' else ('含报损或报废数量，请先处理' if lot.quantity_damaged or lot.quantity_scrapped else '')
        result.append(dict(id=f'lot:{lot.id}',inventory_lot_id=lot.id,inventory_version=lot.version,
            customer_id=customer.id,customer_short_name=customer.chinese_short_name or customer.name,
            customer_order_number=' / '.join(dict.fromkeys(o.customer_po for o in orders if o.customer_po)),
            order_number=' / '.join(dict.fromkeys(o.order_number for o in orders)),
            product_code=detail.inventory_code_snapshot or product.customer_material_code or product.product_code,
            product_name=detail.product_name_snapshot or product.product_name,output_unit=product.unit or '只',
            quantity=lot.quantity_available+lot.quantity_reserved+lot.quantity_damaged,
            available_quantity=lot.quantity_available,reserved_quantity=lot.quantity_reserved,
            placement_pending=pending,
            movable_quantity=lot.quantity_available+lot.quantity_reserved,can_place=pending and not block,placement_block=block,
            warehouse_location_id=location.id,warehouse_location_name=employee_location_name(location),
            warehouse_location_code=location.location_code,
            receipt_placement={'label':'待归位' if pending else '已定位','color':'orange' if pending else 'green'}))
    return result,int(total)


def transfer(db, *, lot_id, expected_version, quantity, location_id, layout_version, operator_id, idempotency_key):
    lot=db.get(InventoryLot,lot_id)
    if lot is None or lot.finished_detail is None:
        raise WarehouseInventoryError('成品库存不存在',404)
    replay=db.scalar(select(InventoryLotTransfer.id).where(InventoryLotTransfer.idempotency_key==idempotency_key))
    if not replay and not is_staging_location(db,db.get(WarehouseLocation,lot.warehouse_location_id)):
        raise WarehouseInventoryError('该批次已不在待入库区，请刷新后操作',409)
    return transfer_finished_lot_between_locations(db,lot_id=lot_id,expected_version=expected_version,
        quantity=quantity,location_id=location_id,expected_target_layout_version=layout_version,
        operator_id=operator_id,idempotency_key=idempotency_key)
