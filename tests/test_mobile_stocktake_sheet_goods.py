from test_semi_finished_lot_product_bindings import lot_db
from test_material_candidates import prepare
from app.api.mobile_erp import _mobile_goods_payload


def test_mobile_goods_uses_real_material_snapshot_for_semi_and_raw(lot_db):
    db, data, lot, user = prepare(lot_db)
    detail=lot.semi_finished_detail
    detail.material_code_snapshot="A1B"
    detail.internal_name="模切待印刷片"
    result=_mobile_goods_payload(lot)
    assert result["product_code"]=="A1B"
    assert result["product_name"]=="模切待印刷片"
    assert result["unit"]=="张"
    detail.internal_name=""
    detail.sheet_type="raw_board"
    assert _mobile_goods_payload(lot)["product_name"]=="原材料纸板"
