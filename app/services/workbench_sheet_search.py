"""Read-only physical sheet discovery; selection never allocates inventory."""
from sqlalchemy import exists, func, or_, select

from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, SemiFinishedInventoryDetail, SemiFinishedLotAllowedProduct
from app.services.warehouse_inventory import SEMI_FINISHED_FLUTES_BY_LAYER


def flute_layers(flute):
    return next((layer for layer, values in SEMI_FINISHED_FLUTES_BY_LAYER.items()
                 if flute in values), None)


def find_sheets(db, scope, *, length, width, flute_type, page, page_size):
    from app.api.mobile_erp import _inventory_group, _lot_load_options
    from app.services.location_candidates import load_warehouse_location_projection_contexts
    from app.services.warehouse_goods import goods_profile

    query = select(InventoryLot).join(SemiFinishedInventoryDetail).where(
        InventoryLot.inventory_type == "semi_finished", InventoryLot.status == "active",
        or_(InventoryLot.quantity_available > 0, InventoryLot.quantity_reserved > 0),
        SemiFinishedInventoryDetail.board_length_mm == length,
        SemiFinishedInventoryDetail.board_width_mm == width)
    if flute_type:
        query = query.where(SemiFinishedInventoryDetail.flute_type == flute_type)
    if scope is not None:
        allowed = exists(select(SemiFinishedLotAllowedProduct.product_id).join(
            Product, Product.id == SemiFinishedLotAllowedProduct.product_id).where(
            SemiFinishedLotAllowedProduct.inventory_lot_id == InventoryLot.id,
            Product.customer_id.in_(scope), Product.deleted_at.is_(None),
            Product.purged_at.is_(None), Product.is_active.is_(True)))
        query = query.where(or_(SemiFinishedInventoryDetail.owner_customer_id.in_(scope), allowed))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    lots = list(db.scalars(query.options(*_lot_load_options()).order_by(InventoryLot.id)
                          .offset((page - 1) * page_size).limit(page_size)))
    contexts = load_warehouse_location_projection_contexts(db, [lot.location for lot in lots if lot.location])
    positions = {row["lot_id"]: row for row in _inventory_group(
        lots, unit="张", projection_contexts=contexts)["positions"]}
    items = []
    for lot in lots:
        detail, position = lot.semi_finished_detail, positions[lot.id]
        profile = goods_profile(db, lot) or {}
        own = scope is None or detail.owner_customer_id in scope
        items.append({
            "lot_id": lot.id,
            "name": (detail.internal_name or "库存片料") if own else "已授权共用片料",
            "length": detail.board_length_mm, "width": detail.board_width_mm,
            "flute_type": detail.flute_type, "layer_count": detail.layer_count,
            "material_code": detail.material_code_snapshot,
            "actual": position["quantity_total"], "available": position["quantity_available"],
            "reserved": position["quantity_reserved"],
            "unit": "片" if profile.get("output_piece") is True else "张",
            "location_label": position.get("employee_location_name") or "位置待核对",
            "reverse_source": {"lot_id": lot.id, "length": detail.board_length_mm,
                               "width": detail.board_width_mm, "flute_type": detail.flute_type},
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "has_more": page * page_size < total, "source": "inventory_sheets"}
