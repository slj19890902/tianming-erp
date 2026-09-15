from types import SimpleNamespace
import pytest
from sqlalchemy import select
from test_n036_delivery_pick import pick_app
from app.api import deliveries as api


@pytest.mark.parametrize('root,quantity', [(True,300),(False,30)])
def test_parent_inventory_is_never_picked_twice(pick_app, monkeypatch, root, quantity):
    _, factory, ids, _ = pick_app
    with factory() as db:
        oi=db.get(api.OrderItem,ids['order_items'][0])
        oi.composite_fulfillment_mode_snapshot='parent_delivery'
        item=SimpleNamespace(id=21,delivery_item_id=None,order_item_id=oi.id,original_quantity=quantity,
            product_code_snapshot='SAME',product_name_snapshot='成套',specification_snapshot='10x20')
        source=dict(source_type='component_stock' if root else 'finished', reservation_id=7,
            lot_id=None,component_snapshot_id=13 if root else None,
            quantity_to_pick_stock=quantity,quantity_to_pick_requirement=quantity)
        monkeypatch.setattr(api,'_inventory_sources_for_order_item',lambda *a,**k:[source])
        monkeypatch.setattr(api,'_pick_parent_finished_sources',lambda *a,**k:[source])
        components=[dict(component_snapshot_id=13,is_graph_root=True,unit='套',
            planned_delivery_quantity=quantity)] if root else []
        lines,_=api._pick_item_location_plan(db,item=item,component_lines=components)
        assert len(lines)==1 and lines[0]['pick_quantity']==quantity
        assert lines[0]['unit']==('套' if root else '个')
        assert lines[0]['source_type']=='finished_inventory'
        db.rollback()


def test_short_reservation_is_missing_not_fictitious_production_stock(pick_app, monkeypatch):
    _,factory,ids,_=pick_app
    with factory() as db:
        item=SimpleNamespace(id=21,delivery_item_id=None,order_item_id=ids['order_items'][0],original_quantity=20,
            product_code_snapshot='151',product_name_snapshot='纸箱',specification_snapshot='')
        monkeypatch.setattr(api,'_inventory_sources_for_order_item',lambda *a,**k:[dict(
            source_type='finished',reservation_id=7,quantity_to_pick_stock=19,quantity_to_pick_requirement=19)])
        lines,complete=api._pick_item_location_plan(db,item=item,component_lines=[])
        assert [(l['source_type'],l['pick_quantity']) for l in lines]==[('finished_inventory',19),('unassigned',1)]
        assert not complete and lines[1]['requires_attention']


def test_parent_receipt_ledger_does_not_project_old_consumable_children(pick_app,monkeypatch):
    _,factory,ids,_=pick_app
    with factory() as db:
        di=db.scalar(select(api.DeliveryItem).where(api.DeliveryItem.delivery_id==ids['delivery']))
        item=SimpleNamespace(delivery_item_id=di.id,order_item_id=di.order_item_id,original_quantity=30)
        monkeypatch.setattr(api,'is_composite_order_item',lambda *a:True)
        monkeypatch.setattr(api,'_uses_composite_inventory',lambda *a:False)
        monkeypatch.setattr(api,'_delivery_component_lines',lambda *a,**k:pytest.fail('must use parent ledger'))
        assert api._pick_item_component_lines(db,item)==[]


def test_explicit_unstocked_direct_fact_is_preserved(pick_app):
    _,factory,ids,_=pick_app
    with factory() as db:
        oi=db.get(api.OrderItem,ids['order_items'][0])
        item=SimpleNamespace(id=21,delivery_item_id=None,order_item_id=oi.id,original_quantity=20,
            product_code_snapshot='151',product_name_snapshot='纸箱',specification_snapshot='')
        context=dict(delivery_items={},order_items={oi.id:oi},composite_order_item_ids=set(),
            inventory_source_order_item_ids=set(),unstocked_direct_quantities={oi.id:15})
        lines,complete=api._pick_item_location_plan(db,item=item,component_lines=[],read_context=context)
        assert [(l['source_type'],l['pick_quantity']) for l in lines]==[('production_direct',15),('unassigned',5)]
        assert not complete
