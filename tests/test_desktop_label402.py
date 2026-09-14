from test_semi_finished_lot_product_bindings import lot_db
from app.services.warehouse_twin_dashboard import _lot_business_fields


def test_sheet_flute_is_an_explicit_display_field(lot_db):
    db, data = lot_db
    lot = data['lot']
    before = (lot.quantity_available, lot.quantity_reserved, lot.version)
    result = _lot_business_fields(lot)
    assert result['flute_type'] == lot.semi_finished_detail.flute_type
    assert result['specification']
    assert result['material']
    assert before == (lot.quantity_available, lot.quantity_reserved, lot.version)
