from copy import deepcopy

import pytest

from tests.test_pre_delivery_current_match_r24 import facts
from app.services.pre_delivery_readiness import refresh_excel_readiness


@pytest.mark.parametrize('selection', ['bound_without_stock', 'foreign', 'multi'])
def test_quantity_preview_clears_stale_positions_without_rebinding_or_saving(facts, selection):
    db, batch, item, row = facts
    row['pick_locations'] = [{'location_id':925, 'location_name':'陈旧虚构位置', 'quantity':20}]
    row['source_payload'] = {}
    before = deepcopy(row)
    payload = {'items':[row], 'draft':None}
    change = {'item_id':1, 'row_no':1, 'final_delivery_qty':5, 'order_item_id':item.id, 'allocations':[]}
    if selection == 'foreign':
        change['order_item_id'] = 99999
    elif selection == 'multi':
        change['allocations'] = [{'order_item_id':item.id, 'quantity':2}, {'order_item_id':99999, 'quantity':3}]
    refresh_excel_readiness(db, batch, payload, {1:change})
    assert row['pick_locations'] == []
    assert all(row[key] == before[key] for key in ('order_item_id','product_id','selected','final_delivery_qty','source_row','source_no'))
    assert not db.dirty and not db.new and not db.deleted
    if selection in {'foreign','multi'}:
        assert row['source_payload']['shortage_diagnostic']['pending_review']
