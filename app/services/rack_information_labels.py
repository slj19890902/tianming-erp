"""Read-only product labels for actual rack contents, not fixed-picking bindings."""
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from fastapi import HTTPException

from app.models.customer import Customer
from app.models.warehouse_inventory import InventoryLot


def rack_information_contents(db, location_id, require_customer):
    lots = db.scalars(select(InventoryLot).options(
        selectinload(InventoryLot.finished_detail),
    ).where(
        InventoryLot.warehouse_location_id == location_id,
        InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged > 0,
    ).order_by(InventoryLot.id)).all()
    contents = {}
    for lot in lots:
        detail = lot.finished_detail
        if lot.inventory_type != 'finished' or detail is None:
            raise HTTPException(409, '本格含半成品或身份不完整批次，请先核对产品身份后打印信息标签')
        require_customer(detail.owner_customer_id)
        customer = db.get(Customer, detail.owner_customer_id) if detail.owner_customer_id else None
        short_name = customer.chinese_short_name if customer else '通用库存'
        if not short_name:
            raise HTTPException(409, '请先补充客户中文简称后打印信息标签')
        specification = 'x'.join(format(value, 'g') for value in
                                 (detail.length_mm, detail.width_mm, detail.height_mm) if value is not None)
        item = dict(product_id=detail.product_id, customer_short_name=short_name,
                    inventory_code=detail.inventory_code_snapshot,
                    product_name=detail.product_name_snapshot, specification=specification)
        if not item['inventory_code'] or not item['product_name']:
            raise HTTPException(409, '本格产品编码或名称不完整，请先核对后打印信息标签')
        key = (detail.owner_customer_id, detail.product_id, item['inventory_code'], item['product_name'], specification)
        contents.setdefault(key, item)
    return list(contents.values())
