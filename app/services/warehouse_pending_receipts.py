"""Read-only delivery projection, deliberately separate from factory stock."""
from sqlalchemy import exists, or_, select

from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import ReturnReceipt
from app.models.order import OrderItem
from app.models.product import Product


def pending_receipt_search(db, *, keyword, visible_customer_ids):
    query = (select(DeliveryItem, Delivery, Customer, OrderItem, Product)
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .join(Customer, Customer.id == Delivery.customer_id)
        .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .outerjoin(Product, Product.id == DeliveryItem.product_id)
        .where(Delivery.status == "dispatched", DeliveryItem.is_current.is_(True),
            ~exists(select(ReturnReceipt.id).where(
                ReturnReceipt.delivery_id == Delivery.id,
                ReturnReceipt.status == "confirmed"))))
    if visible_customer_ids is not None:
        query = query.where(Delivery.customer_id.in_(visible_customer_ids))
    fields = [Delivery.delivery_number, Customer.name, Customer.chinese_short_name,
              Customer.customer_code, DeliveryItem.product_code_snapshot,
              DeliveryItem.product_name_snapshot, DeliveryItem.specification_snapshot,
              OrderItem.snapshot_product_code, OrderItem.snapshot_product_name, OrderItem.snapshot_spec]
    query = query.where(or_(*(field.contains(keyword, autoescape=True) for field in fields)))
    return [{
        "delivery_item_id": item.id, "delivery_id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "customer_name": customer.chinese_short_name or customer.name,
        "inventory_code": item.product_code_snapshot or (order.snapshot_product_code if order else None),
        "product_name": item.product_name_snapshot or (order.snapshot_product_name if order else None),
        "specification": item.specification_snapshot or (order.snapshot_spec if order else None),
        "quantity": item.delivered_quantity,
        "unit": item.unit_snapshot or (product.unit if product else "个"),
        "status": "dispatched_waiting_receipt",
        "location_name": "已发出" + (f" · 车辆 {delivery.vehicle_number}" if delivery.vehicle_number else ""),
        "counts_as_factory_stock": False,
    } for item, delivery, customer, order, product in db.execute(query.order_by(Delivery.id.desc(), DeliveryItem.id))]
