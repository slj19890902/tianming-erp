"""Read-only last-five dispatched shipment reference in frozen physical units."""
from sqlalchemy import select, func

from app.models.delivery import Delivery, DeliveryItem
from app.models.order import OrderItem
from app.services.delivery_quantities import (
    QuantityContractError, for_item,
)


def stock_reference(db, *, customer_id, product_id, product_code, quantity_unit, remaining_quantity):
    result = dict(status='unknown', quantity_unit=quantity_unit, min_quantity=None,
        max_quantity=None, sample_count=0, samples=[], reason='暂无参考')
    identity = func.coalesce(DeliveryItem.product_id, OrderItem.product_id)
    code = func.coalesce(DeliveryItem.product_code_snapshot, OrderItem.snapshot_product_code)
    conditions = (Delivery.customer_id == customer_id, Delivery.status == 'dispatched',
        DeliveryItem.is_current.is_(True), identity == product_id, code == product_code)
    # Limit documents before loading lines: repeated product rows form one sample.
    documents = db.execute(select(Delivery.id, Delivery.delivery_number, Delivery.delivery_date)
        .join(DeliveryItem, DeliveryItem.delivery_id == Delivery.id)
        .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .where(*conditions).group_by(Delivery.id, Delivery.delivery_number, Delivery.delivery_date)
        .order_by(Delivery.delivery_date.desc(), Delivery.id.desc()).limit(5)).all()
    if not documents or not quantity_unit:
        return result
    totals = {row.id: 0 for row in documents}
    lines = db.execute(select(DeliveryItem, OrderItem)
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .where(*conditions, Delivery.id.in_(totals))).all()
    try:
        for line, order_item in lines:
            frozen = for_item(line)
            if frozen is not None:
                if (frozen['customer_id'] != customer_id or frozen['product_id'] != product_id
                        or frozen['physical_unit'] != quantity_unit):
                    raise QuantityContractError('历史送货单位或产品身份不一致')
                quantity = frozen['physical_quantity']
            elif order_item is not None and order_item.supply_mode_snapshot == 'external_purchase':
                # An order ratio cannot prove which physical accounting basis an
                # older dispatched delivery used. Never repair it during a read.
                raise QuantityContractError('外购历史送货缺少冻结实物数量快照')
            else:
                frozen_unit = line.unit_snapshot or (order_item.sales_unit_snapshot if order_item else None)
                if frozen_unit != quantity_unit:
                    raise QuantityContractError('历史送货缺少可靠的单位换算')
                quantity = line.delivered_quantity
            totals[line.delivery_id] += quantity
    except QuantityContractError:
        result['reason'] = '历史送货单位或冻结数量待核对'
        return result
    samples = [dict(delivery_id=row.id, delivery_number=row.delivery_number,
        delivery_date=row.delivery_date.isoformat(), quantity=totals[row.id]) for row in documents]
    low, high = min(totals.values()), max(totals.values())
    result.update(status='green' if remaining_quantity >= high else 'yellow' if remaining_quantity >= low else 'red',
        min_quantity=low, max_quantity=high, sample_count=len(samples), samples=samples, reason=None)
    return result
