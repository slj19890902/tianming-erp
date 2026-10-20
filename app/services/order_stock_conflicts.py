"""Explain excluded parent stock without making it eligible or converting units."""
import json

from sqlalchemy import select

from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail, InventoryLot, WarehouseLocation,
)
from app.services.finished_stock_identity import (
    product_basis, matches_component_stock_identity, stock_identity_error,
)
from app.services.order_parent_stock import physical_bom_parent


def parent_stock_conflicts(db, product):
    if not physical_bom_parent(db, product):
        return []
    expected = product_basis(product)
    rows = db.execute(select(InventoryLot, WarehouseLocation)
        .join(FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id)
        .join(WarehouseLocation, WarehouseLocation.id == InventoryLot.warehouse_location_id)
        .where(InventoryLot.inventory_type == "finished", InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            FinishedGoodsInventoryDetail.product_id == product.id,
            FinishedGoodsInventoryDetail.owner_customer_id == product.customer_id,
            FinishedGoodsInventoryDetail.is_general.is_(False))).all()
    result = []
    for lot, location in rows:
        if matches_component_stock_identity(lot, expected):
            continue
        try:
            basis = json.loads(lot.finished_detail.physical_basis_json or "{}")
        except (ValueError, TypeError):
            basis = {}
        result.append(dict(lot_id=lot.id, quantity=lot.quantity_available,
            unit=basis.get("unit") if isinstance(basis, dict) else None,
            location=location.location_name or location.location_code,
            reason=stock_identity_error(lot, expected)))
    return result
