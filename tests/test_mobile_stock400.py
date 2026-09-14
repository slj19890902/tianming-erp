from test_semi_finished_lot_product_bindings import lot_db
from test_material_candidates import prepare
from app.api.mobile_erp import _mobile_goods_payload
from app.services.material_candidates import candidate_items


def test_sheet_material_is_not_product_code(lot_db):
    db, data, lot, user = prepare(lot_db)
    payload = _mobile_goods_payload(lot)
    assert payload['product_code'] is None
    assert payload['material_code'] == lot.semi_finished_detail.material_code_snapshot
    assert payload['flute_type'] == lot.semi_finished_detail.flute_type
    assert payload['specification']
    assert payload['quantity_total'] == lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged


def test_candidate_box_style_retains_customer_scope(lot_db):
    db, data, lot, user = prepare(lot_db)
    data['products'][0].box_style = '衬板'
    data['products'][1].box_style = 'A1'
    db.commit()
    items = candidate_items(db, lot, {data['customer'].id})
    liners = [item for item in items if item['box_style'] == '衬板']
    assert [item['product_id'] for item in liners] == [data['products'][0].id]
    assert all(item['customer_id'] == data['customer'].id for item in items)
