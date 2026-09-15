from types import SimpleNamespace as NS
from app.services.historical_sales_contract import _unit
from app.models.warehouse_inventory import InventoryMovement, InventoryLot


def test_historical_unit_uses_exact_movement_not_current_master():
    allocation=NS(id=1,consume_movement_id=2,consumed_quantity=5)
    movement=NS(id=2,inventory_lot_id=3,movement_type='consume',quantity=5,unit='张')
    lot=NS(id=3,unit='只',finished_detail=NS(product_id=4))
    db=NS(scalars=lambda query:NS(all=lambda:[allocation]),get=lambda model,key:movement if model is InventoryMovement else lot)
    item=NS(id=1,product_id=4,unit_snapshot=None,source_type='unordered_finished',delivered_quantity=5)
    unit,evidence=_unit(db,item,None)
    assert unit=='张'
    assert evidence['sources'][0]['movement_id']==2
    movement.quantity=4
    assert _unit(db,item,None)[0] is None
    movement.quantity=5;lot.finished_detail.product_id=9
    assert _unit(db,item,None)[0] is None


def test_historical_unit_rejects_quantity_conversion():
    allocation=NS(id=1,consume_movement_id=2,consumed_stock_quantity=10,credited_requirement_quantity=5)
    movement=NS(id=2,inventory_lot_id=3,movement_type='consume',quantity=10,unit='片')
    lot=NS(id=3,finished_detail=NS(product_id=4))
    db=NS(scalars=lambda query:NS(all=lambda:[allocation]),get=lambda model,key:movement if model is InventoryMovement else lot)
    item=NS(id=1,product_id=4,unit_snapshot=None,source_type='order',delivered_quantity=5)
    assert _unit(db,item,None)[0] is None


def test_historical_write_role_guard_is_before_queries():
    import pytest
    from app.services.historical_sales_contract import adopt
    with pytest.raises(PermissionError):
        adopt(None,months=['2026-09'],actor=NS(is_active=True,role='warehouse'),expected_preview='',batch_id='x',reason='x')
