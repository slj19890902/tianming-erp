"""A customer's model caption is not a conflicting measured die-cut size."""
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.test_stock_replenishment_flow import stock_replenishment_app
from tests.test_order_replenishment_supply import seed


@pytest.mark.parametrize('change', ['none', 'custom_spec', 'mold', 'count', 'material', 'corrupt_history', 'master_later'])
def test_frozen_unmeasured_die_supply_covers_only_proven_order(stock_replenishment_app, change):
    from app.models.product import Product
    from app.models.mold_tool import MoldTool
    from app.models.order import OrderItem
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.api.requisition import _current_requisition_summary
    from app.services.order_replenishment_supply import summary
    _, factory = stock_replenishment_app
    with factory() as db:
        product = db.get(Product, 1)
        product.length_mm = product.width_mm = product.height_mm = None
        product.box_category = 'die_cut'
        product.box_style = '模切内盒'
        product.product_name = 'CUSTOMER-MODEL'
        product.crease_type = '毛片'
        product.crease_left_mm = product.crease_middle_mm = product.crease_right_mm = None
        mold = MoldTool(mold_code='DIE-A', mold_name='Die A', rack_location='R01')
        db.add(mold); db.flush()
        product.mold_tool_id = mold.id
        product.sheet_cutting_settings = {'schema_version': 2, 'whole': {
            'is_die_cut': True, 'mold_count': 1, 'length_parts': 1, 'width_parts': 1}}
        db.commit()
    source_id, ids = seed(factory, quantities=(80, 80), sheets=100)
    with factory() as db:
        source = db.get(StockReplenishmentOrderItem, source_id)
        captured = json.loads(source.production_snapshot_json)
        fields = captured['fields'].copy()
        if change == 'mold': fields['mold_tool_id'] += 999
        if change == 'count':
            fields['sheet_cutting_settings']['whole']['mold_count'] = 2
        raw = json.dumps(fields, ensure_ascii=False, sort_keys=True)
        db.add(MasterDataObjectVersion(object_type='product', object_id=1, version=1,
            action='update', source='test', snapshot_json=raw,
            snapshot_sha256='0'*64 if change=='corrupt_history' else hashlib.sha256(raw.encode()).hexdigest(),
            change_set_id=str(uuid4()), created_at=source.created_at-timedelta(seconds=1)))
        for iid in ids:
            item = db.get(OrderItem, iid)
            item.snapshot_spec = 'DIFFERENT CUSTOM SPEC' if change == 'custom_spec' else 'CUSTOMER-MODEL'
            if change == 'material': item.snapshot_material = 'OTHER'
        if change == 'master_later':
            product=db.get(Product,1)
            product.product_name='NEW NAME';product.length_mm=999;product.width_mm=888
        db.commit()
    with factory() as db:
        results=[_current_requisition_summary(db,db.get(OrderItem,iid)) for iid in ids]
        expected=[80,20] if change in ('none','master_later') else [0,0]
        assert [r['replenishment_covered_piece_qty'] for r in results] == expected
        assert [r['requisition_qty'] for r in results] == [80-x for x in expected]
        assert sum(summary(db,iid)['physical_output_piece_quantity'] for iid in ids)==0
        assert not db.new and not db.dirty and not db.deleted
