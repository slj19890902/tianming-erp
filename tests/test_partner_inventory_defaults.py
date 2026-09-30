from app.services.partner_product_defaults import product_defaults
from app.services import receipt_putaway as service
from test_fixed_shelf import setup, incoming
from test_p1_123_warehouse_region_rack_labels import rack_factory


def test_explicit_process_and_flute_preserved():
    x=product_defaults(customer_name='研光',layer_count=5,flute_type='BE',box_style='A1/0201 普通开槽箱',production_process='粘贴,开槽')
    assert 'flute_type' not in x and 'production_process' not in x
    assert x['production_label_enabled'] and x['production_label_units_per_label']==5
    y=product_defaults(customer_name='其他客户',layer_count=5,flute_type=None,box_style='A1',production_process='开槽')
    assert y=={'flute_type':'AB','production_process':'开槽,打钉'}


def test_confirmed_stocktake_tracks_new_rack_and_is_idempotent(setup):
    factory,pid,cid,ids=setup
    with factory() as db:
        first=incoming(db,pid,cid,ids[0])
        service.remember_stocktake(db,first,1)
        second=incoming(db,pid,cid,ids[1],key='second')
        service.remember_stocktake(db,second,1,follow_position=True)
        assert service.info(db,pid)['location_id']==ids[1]
        version=service.info(db,pid)['version']
        service.remember_stocktake(db,second,1,follow_position=True)
        assert service.info(db,pid)['version']==version
        assert first.quantity_available==125 and second.quantity_available==125
        db.rollback()
