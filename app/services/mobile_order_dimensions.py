"""Read-only, directional dimensional lookup of outstanding order lines."""
from datetime import date
from decimal import Decimal
import re
from sqlalchemy import select, or_
from app.models.order import Order, OrderItem
from app.models.customer import Customer
from app.models.product import Product
from app.services.order_status_policy import order_item_forward_fulfillment_sql_conditions, order_item_remaining_quantity


def parse_dimensions(text, maximum=3):
    normalized = re.sub(r"\s+", "", str(text or "").lower()).replace("毫米", "mm")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:[x×*](\d+(?:\.\d+)?))?(?:[x×*](\d+(?:\.\d+)?))?(mm|cm)?", normalized)
    if not match:
        raise ValueError("请输入尺寸，例如 800×600；单位默认 mm，也支持 cm")
    values = [Decimal(v) * (10 if match[4] == "cm" else 1) for v in match.groups()[:3] if v]
    if len(values) > maximum or any(v <= 0 or v > 100000 for v in values):
        raise ValueError("尺寸数量或范围不正确")
    return values


def candidates(item, product, domain):
    if domain == "board":
        return [("订单报料", [item.cardboard_len, item.cardboard_width]),
                ("订单报料快照", [item.snapshot_report_length_mm, item.snapshot_report_width_mm]),
                ("常用箱报料", [product.report_length_mm, product.report_width_mm] if product else []),
                ("常用箱默认纸板", [product.default_cardboard_length, product.default_cardboard_width] if product else [])]
    try:
        frozen = parse_dimensions(item.snapshot_spec)
    except ValueError:
        frozen = []
    return [("订单规格", frozen), ("常用箱尺寸", [product.length_mm, product.width_mm, product.height_mm] if product else [])]


def best_match(item, product, domain, dimensions, tolerance, axis):
    matches = []
    for source, values in candidates(item, product, domain):
        indexes = ([0, 1] if axis == "any" else [1] if axis == "width" else [0]) if domain == "board" and len(dimensions) == 1 else [0]
        for first in indexes:
            selected = values[first:first + len(dimensions)]
            if len(selected) != len(dimensions) or any(v is None or v <= 0 for v in selected):
                continue
            differences = [Decimal(v) - requested for v, requested in zip(selected, dimensions)]
            if any(abs(v) > tolerance for v in differences):
                continue
            distance = sum(abs(v) for v in differences)
            labels = ["长", "宽", "高"][first:first + len(dimensions)]
            matches.append({"source": source, "distance": float(distance),
                "level": "精确" if distance == 0 else "接近" if max(abs(v) for v in differences) <= 5 else "扩大范围",
                "dimensions_mm": [float(v) if v is not None else None for v in values],
                "differences": [{"dimension": label, "mm": float(v)} for label, v in zip(labels, differences)]})
    return min(matches, key=lambda m: m["distance"], default=None)


def outstanding_query(visible_ids):
    stmt = select(OrderItem, Order, Customer, Product).join(Order, Order.id == OrderItem.order_id).join(Customer, Customer.id == Order.customer_id).outerjoin(Product, Product.id == OrderItem.product_id).where(*order_item_forward_fulfillment_sql_conditions(
        order_status_column=Order.status, ordered_quantity_column=OrderItem.quantity,
        delivered_quantity_column=OrderItem.delivered_quantity, is_force_closed_column=OrderItem.is_force_closed))
    if visible_ids is not None:
        stmt = stmt.where(Order.customer_id.in_(visible_ids))
    return stmt


def line_payload(item, order, customer, product):
    return {"order_id": order.id, "order_item_id": item.id, "order_number": order.order_number,
        "customer_po": order.customer_po, "item_order_number": item.item_order_number,
        "customer_name": customer.chinese_short_name or customer.name,
        "product_code": item.snapshot_product_code, "product_name": item.snapshot_product_name,
        "specification": item.snapshot_spec, "common_specification": "×".join(str(v) for v in (product.length_mm, product.width_mm, product.height_mm) if v is not None) if product else None,
        "quantity": int(item.quantity or 0), "delivered_quantity": int(item.delivered_quantity or 0),
        "remaining_quantity": order_item_remaining_quantity(ordered_quantity=item.quantity, delivered_quantity=item.delivered_quantity),
        "unit": product.unit if product else "只", "order_date": order.order_date.isoformat(),
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None, "status": order.status}


def find_order_dimensions(db, *, visible_ids, domain, text, customer, tolerance, axis, page, page_size):
    dimensions = parse_dimensions(text, 2 if domain == "board" else 3)
    stmt = outstanding_query(visible_ids)
    if customer:
        pattern = "%" + customer.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(or_(*(column.ilike(pattern, escape="\\") for column in (Customer.name, Customer.chinese_short_name, Customer.customer_code))))
    matches = []
    # Stream the scoped active lines; frozen textual specifications cannot be
    # treated as numeric SQL columns. Paginate only after exact dimensional ranking.
    for item, order, client, product in db.execute(stmt.execution_options(yield_per=200)):
        match = best_match(item, product, domain, dimensions, tolerance, axis)
        if match:
            matches.append({**line_payload(item, order, client, product), "dimension_match": match})
    matches.sort(key=lambda row: (row["dimension_match"]["distance"], -row["remaining_quantity"], -date.fromisoformat(row["order_date"]).toordinal(), row["order_item_id"]))
    total = len(matches)
    page = min(page, max(1, (total + page_size - 1) // page_size))
    return {"id": "orders", "label": "未送齐订单", "total": total, "page": page, "page_size": page_size,
            "items": matches[(page - 1) * page_size:page * page_size]}
