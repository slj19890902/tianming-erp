"""Read-only catalog fallback. Catalog records never become inventory lots."""
from sqlalchemy import exists, select

from app.models.customer import Customer
from app.models.product import Product
from app.models.warehouse_inventory import FinishedGoodsInventoryDetail, InventoryLot


def _normalized(value):
    return ("".join(str(value or "").split()).casefold().replace("x", "×")
            .replace("*", "×").replace("毫米", "").replace("mm", ""))


def catalog_without_stock(db, *, keyword, visible_customer_ids, after_product_id=0, page_size=100):
    physical_stock = exists(select(InventoryLot.id).join(
        FinishedGoodsInventoryDetail,
        FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id).where(
            FinishedGoodsInventoryDetail.product_id == Product.id,
            FinishedGoodsInventoryDetail.owner_customer_id == Product.customer_id,
            InventoryLot.status.in_(("active", "frozen")),
            InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged > 0))
    query = select(Product, Customer).join(Customer, Customer.id == Product.customer_id).where(
        Product.deleted_at.is_(None), ~physical_stock)
    if visible_customer_ids is not None:
        query = query.where(Product.customer_id.in_(visible_customer_ids))
    cursor, matches = after_product_id or 0, []
    needle = _normalized(keyword)
    while len(matches) <= page_size:
        batch = db.execute(query.where(Product.id > cursor).order_by(Product.id).limit(200)).all()
        if not batch:
            break
        for product, customer in batch:
            cursor = product.id
            specification = product.external_packaging_specification_summary or "×".join(
                format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")
                for value in (product.length_mm, product.width_mm, product.height_mm) if value is not None)
            if needle not in _normalized(" ".join(str(value or "") for value in (
                product.product_code, product.customer_material_code, product.product_name,
                customer.name, customer.chinese_short_name, customer.customer_code, specification))):
                continue
            matches.append({"product_id": product.id, "customer_name": customer.chinese_short_name or customer.name,
                "inventory_code": product.customer_material_code or product.product_code,
                "product_name": product.product_name, "specification": specification,
                "is_active": product.is_active, "stock_status": "no_stock"})
            if len(matches) > page_size:
                break
        if len(batch) < 200:
            break
    rows = matches[:page_size]
    has_more = len(matches) > page_size
    return {"items": rows, "has_more": has_more,
            "next_after_product_id": rows[-1]["product_id"] if has_more else None}
