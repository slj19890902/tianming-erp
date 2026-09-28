"""Delete an unproduced order using older production surplus, private copies only."""
import pytest
from sqlalchemy import select, func
from app.models.order import OrderItem
from app.models.production import ProductionCompletion
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, InventoryMovement
from app.services.warehouse_inventory import release_finished_reservation, WarehouseInventoryError
from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_bom_other_products_acceptance import factory_http


def balance(db):
    lot=db.get(InventoryLot,919)
    return (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed,lot.version)


def test_real_unproduced_yl132_line_deletion_only_releases_its_two_surplus_units(factory_http):
    client,db=factory_http
    original=balance(db)
    completion=db.get(ProductionCompletion,244)
    old=db.get(InventoryReservation,704)
    protected=(completion.order_item_id,completion.status,old.reserved_stock_quantity,old.consumed_stock_quantity,old.released_stock_quantity)
    result=client.delete('/api/orders/items/10465')
    assert result.status_code==204,result.text
    db.expire_all()
    assert db.get(OrderItem,10465) is None
    assert balance(db)==(original[0]+2,original[1]-2,original[2],original[3]+1)
    reserve=db.get(InventoryReservation,810)
    assert reserve.released_stock_quantity==2 and reserve.status=='released'
    completion=db.get(ProductionCompletion,244);old=db.get(InventoryReservation,704)
    assert protected==(completion.order_item_id,completion.status,old.reserved_stock_quantity,old.consumed_stock_quantity,old.released_stock_quantity)
    assert db.get(OrderItem,10147).delivered_quantity==300
    again=client.delete('/api/orders/items/10465')
    assert again.status_code==404
    db.expire_all()
    assert balance(db)[0]==original[0]+2
    client.cookies.clear()
    assert client.delete('/api/orders/items/10147').status_code==401


def test_surplus_release_replays_but_original_completion_reservation_stays_protected(factory_copy):
    db=factory_copy
    args=dict(reservation_id=810,operator_id=1,release_reason='隔离回归取消抵扣',idempotency_key='isolated-yl132-release')
    before=balance(db)
    result=release_finished_reservation(db,**args)
    db.commit()
    assert result.released_stock_quantity==2
    assert release_finished_reservation(db,**args).id==810
    db.commit()
    assert balance(db)==(before[0]+2,before[1]-2,before[2],before[3]+1)
    with pytest.raises(WarehouseInventoryError):
        release_finished_reservation(db,**{**args,'reservation_id':704})
    db.rollback()
    with pytest.raises(WarehouseInventoryError,match='生产完工自动预占'):
        release_finished_reservation(db,**{**args,'reservation_id':704,'idempotency_key':'original-protected'})
    db.rollback()


def test_delete_audit_failure_rolls_back_release_and_line(factory_http,monkeypatch):
    from app.api import orders
    client,db=factory_http
    before=balance(db)
    movement_count=db.scalar(select(func.count()).select_from(InventoryMovement))
    def fail(*args,**kwargs):
        raise RuntimeError('isolated delete audit failure')
    monkeypatch.setattr(orders,'_append_order_audit',fail)
    with pytest.raises(RuntimeError,match='isolated delete audit failure'):
        client.delete('/api/orders/items/10465')
    db.expire_all()
    assert db.get(OrderItem,10465) is not None and balance(db)==before
    assert db.get(InventoryReservation,810).released_stock_quantity==0
    assert db.scalar(select(func.count()).select_from(InventoryMovement))==movement_count


def test_unresolved_production_source_remains_blocked(factory_copy):
    db=factory_copy
    lot=db.get(InventoryLot,919)
    lot.source_ref_id=999999999
    db.commit()
    before=balance(db)
    with pytest.raises(WarehouseInventoryError,match='缺少可核对的生产来源'):
        release_finished_reservation(db,reservation_id=810,operator_id=1,
            release_reason=None,idempotency_key='unresolved-source')
    db.rollback()
    assert balance(db)==before
