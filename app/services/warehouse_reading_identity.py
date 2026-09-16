"""Read-only display identity; never coalesce lots, reservations or BOM facts."""
from sqlalchemy import select, or_
from sqlalchemy.orm import object_session

from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
from app.models.order import OrderItem


def shelf_merge_identity(lot):
    from app.services.bom_inventory_contract import is_body_lot
    if is_body_lot(lot):
        return {"box_style": getattr(lot.finished_detail, "box_type_snapshot", None),
                "is_bom_component": True, "inventory_stage": "body"}
    detail = lot.finished_detail
    if detail is None:
        return {"box_style": None, "is_bom_component": None}
    result = {"box_style": getattr(detail, "box_type_snapshot", None), "is_bom_component": None}
    db = object_session(lot)
    if db is None:
        return result
    # Include historical frozen child identities even if the current template changed.
    # Root snapshots in multi-level graphs are not children merely because they
    # share the legacy snapshot table.
    with db.no_autoflush:
        child = db.scalar(select(or_(
            select(ProductBomComponent.id).where(
                ProductBomComponent.component_product_id == detail.product_id).exists(),
            select(SalesOrderItemBomComponent.id).join(OrderItem,
                OrderItem.id == SalesOrderItemBomComponent.sales_order_item_id).where(
                SalesOrderItemBomComponent.component_product_id == detail.product_id,
                OrderItem.product_id != detail.product_id).exists(),
        )))
        product = detail.product
        result["is_bom_component"] = bool(child or (product and product.is_internal_component)) if product else None
    return result
