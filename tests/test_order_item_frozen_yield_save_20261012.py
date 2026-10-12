"""Actual endpoint path with synthetic frozen facts, never production data."""
from copy import deepcopy
import pytest
from fastapi import HTTPException
from tests.test_finished_goods_inventory_reservation import reservation_db, add_lot

@pytest.mark.parametrize('mode,accepted,reserved',[
    ('一开一',True,True),('一开二',False,True),('一开二',False,False)])
def test_frozen_yield_save_preserves_snapshot_and_reserved_stock(reservation_db,mode,accepted,reserved):
    from app.api.warehouse import create_finished_reservation,FinishedReservationPayload
    from app.api.orders import update_order_item,OrderItemUpdate
    db,facts=reservation_db
    item=facts['item'];product=facts['product']
    settings=dict(schema_version=2,whole=dict(length_parts=1,width_parts=1,mold_count=1,is_die_cut=False))
    item.sheet_cutting_settings_snapshot=deepcopy(settings)
    product.sheet_cutting_settings=deepcopy(settings)
    product.box_style='A1普通箱'
    product.base_report_length_mm=None
    product.base_report_width_mm=None
    item.special_process='一开一'
    lot=add_lot(db,facts,quantity=80,key='frozen-yield-save')
    db.commit()
    if reserved:
        create_finished_reservation(FinishedReservationPayload(order_item_id=item.id,inventory_lot_id=lot.id,
            quantity=20,expected_version=lot.version,idempotency_key='frozen-yield-save-reserve'),db=db,user=facts['admin'])
    before=(item.quantity,deepcopy(item.sheet_cutting_settings_snapshot),lot.quantity_available,lot.quantity_reserved)
    payload=OrderItemUpdate(quantity=item.quantity,unit_price=item.unit_price,special_process=mode,
        product_code=item.snapshot_product_code,product_name=item.snapshot_product_name,
        specification=item.snapshot_spec,material=item.snapshot_material)
    if accepted:
        result=update_order_item(item.id,payload,db=db,user=facts['admin'])
        assert result['quantity']==before[0]
    else:
        with pytest.raises(HTTPException) as raised:update_order_item(item.id,payload,db=db,user=facts['admin'])
        assert raised.value.status_code==409
        assert ('库存预占' if reserved else '独立开料订单') in raised.value.detail
        db.rollback()
    db.refresh(item);db.refresh(lot)
    assert (item.quantity,item.sheet_cutting_settings_snapshot,lot.quantity_available,lot.quantity_reserved)==before
    assert item.special_process=='一开一'
