from datetime import date, datetime

import pytest

from app.api.warehouse import _lot_dict
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail


@pytest.mark.parametrize("source,role", [("subkit_receipt", "component"),
    ("subkit_conversion", "kit"), ("bom_assembly", "kit"), (None, None)])
def test_role_belongs_to_lot_not_map_layout(source, role):
    lot = InventoryLot(lot_number="TEST-ROLE", inventory_type="finished", warehouse_location_id=None,
        quantity_available=3, quantity_reserved=0, quantity_consumed=0, quantity_damaged=0,
        quantity_scrapped=0, unit="boxes", status="active", source_type="manual",
        source_ref_type=source, stock_date=date(2026, 9, 9), stock_date_accuracy="exact",
        last_movement_at=datetime(2026, 9, 9, 8))
    assert _lot_dict(lot)["subkit_role"] == role


def test_assembled_real_product_displays_sets_not_boxes():
    from app.services.warehouse_display_units import lot_display_unit
    lot = InventoryLot(unit="boxes", source_ref_type="bom_assembly")
    lot.finished_detail = FinishedGoodsInventoryDetail(product=Product(unit="套"))
    assert lot_display_unit(lot) == "套"
