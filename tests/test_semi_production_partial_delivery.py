"""An unfinished task must not hide real finished goods or expose raw sheets."""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_n029_production_integration import n029_delivery_app, _login


def seed_partial(factory, ids, *, credit=3, status='pending'):
    from app.models import OrderItem, ProductionTask, InventoryLot, FinishedGoodsInventoryDetail, InventoryReservation
    with factory() as db:
        item=db.get(OrderItem, ids['task_pending'])
        item.material_status='pending'
        task=db.scalar(select(ProductionTask).where(ProductionTask.order_item_id==item.id))
        task.status=status
        task.planned_quantity=100-credit if status=='pending' else 0
        task.finished_coverage_snapshot=credit
        task.readiness_basis='semi_finished_inventory' if status=='pending' else None
        lot=InventoryLot(lot_number='PARTIAL-FINISHED',inventory_type='finished',
            warehouse_location_id=ids['return_location'],quantity_available=0,quantity_reserved=credit,
            quantity_consumed=0,quantity_damaged=0,quantity_scrapped=0,unit='boxes',status='active',
            source_type='stocktake',stock_date=date.today(),last_movement_at=datetime.now(),version=1)
        db.add(lot);db.flush()
        db.add(FinishedGoodsInventoryDetail(inventory_lot_id=lot.id,owner_customer_id=ids['customer'],
            product_id=item.product_id,is_general=False,inventory_code_snapshot=item.snapshot_product_code,
            product_name_snapshot=item.snapshot_product_name,owner_customer_name_snapshot="测试客户",box_type_snapshot="普通箱"))
        reservation=InventoryReservation(reservation_number='PARTIAL-FINISHED-RS',inventory_lot_id=lot.id,
            reservation_type='finished_order',order_id=item.order_id,order_item_id=item.id,
            reserved_stock_quantity=credit,credited_requirement_quantity=credit,status='active',
            idempotency_key='PARTIAL-FINISHED-RS')
        db.add(reservation);db.flush();db.commit()
        return lot.id,reservation.id,task.id


@pytest.mark.parametrize('status',['pending','waiting_material'])
def test_partial_finished_consistent_in_scalar_query_and_picker(n029_delivery_app,status):
    from app.models import OrderItem
    from app.api.deliveries import _delivery_remaining_quantity,_pending_query,_PendingDeliveryReadContext
    app,factory,ids=n029_delivery_app
    seed_partial(factory,ids,status=status)
    with factory() as db:
        item=db.get(OrderItem,ids['task_pending'])
        assert _delivery_remaining_quantity(db,item)==3
        rows=list(db.execute(_pending_query(db=db,order_item_id=item.id)))
        assert len(rows)==1
        assert _PendingDeliveryReadContext(db,rows).remaining_quantity(db,item)==3
        assert not list(db.execute(_pending_query(db=db,customer_ids=set(),order_item_id=item.id)))
    with TestClient(app) as client:
        _login(client)
        response=client.get('/api/deliveries/pending_items')
        assert response.status_code==200,response.text
        row=next(r for r in response.json()['items'] if r['order_item_id']==ids['task_pending'])
        assert row['deliverable_quantity']==3
        for quantity,expected in [(4,409),(3,201)]:
            response=client.post('/api/deliveries',json={'customer_id':ids['customer'],
                'idempotency_key':'partial-finished-'+str(quantity),
                'items':[{'order_item_id':ids['task_pending'],'delivered_quantity':quantity}]})
            assert response.status_code==expected,response.text


@pytest.mark.parametrize('invalid',['consumed','released','cancelled','frozen','semi_finished','semi_order'])
def test_unavailable_stock_never_releases_unfinished_task(n029_delivery_app,invalid):
    from app.models import OrderItem, InventoryLot,InventoryReservation
    from app.api.deliveries import _delivery_remaining_quantity,_pending_query
    _app,factory,ids=n029_delivery_app
    lotid,rid,_=seed_partial(factory,ids)
    with factory() as db:
        r=db.get(InventoryReservation,rid);lot=db.get(InventoryLot,lotid)
        if invalid in {'consumed','released'}:
            setattr(r,invalid+'_stock_quantity',3);setattr(r,invalid+'_requirement_quantity',3);r.status=invalid;lot.quantity_reserved=0
        elif invalid=='cancelled':r.status='cancelled'
        elif invalid=='frozen':lot.status='frozen'
        elif invalid=='semi_finished':lot.inventory_type='semi_finished'
        else:
            # A component reservation is not a finished parent delivery credit.
            r.reservation_type='semi_order'
        db.flush()
        item=db.get(OrderItem,ids['task_pending'])
        assert _delivery_remaining_quantity(db,item)==0
        assert not list(db.execute(_pending_query(db=db,order_item_id=item.id)))


def test_finished_credit_is_remaining_and_partial_dispatch_keeps_task(n029_delivery_app):
    from app.models import OrderItem,InventoryReservation,InventoryLot,ProductionTask
    from app.api.deliveries import _delivery_remaining_quantity
    _app,factory,ids=n029_delivery_app
    lotid,rid,tid=seed_partial(factory,ids,credit=8)
    with factory() as db:
        r=db.get(InventoryReservation,rid);lot=db.get(InventoryLot,lotid);item=db.get(OrderItem,ids['task_pending'])
        r.consumed_stock_quantity=3;r.consumed_requirement_quantity=3;r.released_stock_quantity=2;r.released_requirement_quantity=2;r.status='partial'
        lot.quantity_reserved=3;lot.quantity_consumed=3;lot.quantity_available=2
        item.delivered_quantity=3
        db.flush()
        assert _delivery_remaining_quantity(db,item)==3
        assert db.get(ProductionTask,tid).status=='pending'
        assert db.get(ProductionTask,tid).planned_quantity==92


def test_partial_delivery_dispatch_replay_and_cancel_keep_production_open(n029_delivery_app):
    from app.models import OrderItem,Order,InventoryLot,ProductionTask
    from app.api.deliveries import _delivery_remaining_quantity
    app,factory,ids=n029_delivery_app
    lotid,_rid,tid=seed_partial(factory,ids)
    with TestClient(app) as client:
        _login(client)
        payload={'customer_id':ids['customer'],'idempotency_key':'partial-delivery-dispatch',
            'items':[{'order_item_id':ids['task_pending'],'delivered_quantity':3}]}
        created=client.post('/api/deliveries',json=payload)
        assert created.status_code==201,created.text
        did=created.json()['id']
        replay=client.post('/api/deliveries',json=payload)
        assert replay.status_code in (200,201) and replay.json()['id']==did,replay.text
        preview=client.get(f'/api/deliveries/{did}/dispatch-snapshot')
        assert preview.status_code==200,preview.text
        packet=preview.json()
        command={'expected_actor_id':packet['current_actor_id'],'idempotency_key':'partial-stock-dispatch',
            'expected_version':packet['snapshot']['delivery']['version'],'snapshot_hash':packet['snapshot_hash'],
            'snapshot':packet['snapshot'],'confirm_pick_exception':False}
        for _ in range(2):
            result=client.put(f'/api/deliveries/{did}/dispatch',json=command)
            assert result.status_code==200,result.text
        with factory() as db:
            item=db.get(OrderItem,ids['task_pending']);lot=db.get(InventoryLot,lotid)
            assert item.delivered_quantity==3 and item.quantity==100
            assert db.get(Order,item.order_id).status=='partially_delivered'
            assert (lot.quantity_reserved,lot.quantity_consumed)==(0,3)
            assert _delivery_remaining_quantity(db,item)==0
            assert db.get(ProductionTask,tid).status=='pending'
            assert db.get(ProductionTask,tid).planned_quantity==97
        cancelled=client.put(f'/api/deliveries/{did}/cancel')
        assert cancelled.status_code==200,cancelled.text
    with factory() as db:
        item=db.get(OrderItem,ids['task_pending']);lot=db.get(InventoryLot,lotid)
        assert item.delivered_quantity==0 and (lot.quantity_reserved,lot.quantity_consumed)==(3,0)
        assert _delivery_remaining_quantity(db,item)==3
        assert db.get(ProductionTask,tid).planned_quantity==97


def test_partial_finished_credit_keeps_customer_unit_denominator(n029_delivery_app):
    from app.models import OrderItem,InventoryReservation
    from app.api.deliveries import _delivery_remaining_quantity,_pending_query,_PendingDeliveryReadContext
    _app,factory,ids=n029_delivery_app
    _lotid,rid,_tid=seed_partial(factory,ids)
    with factory() as db:
        db.get(InventoryReservation,rid).requirement_quantity_denominator=2
        db.flush()
        item=db.get(OrderItem,ids['task_pending'])
        assert _delivery_remaining_quantity(db,item)==1
        rows=list(db.execute(_pending_query(db=db,order_item_id=item.id)))
        assert _PendingDeliveryReadContext(db,rows).remaining_quantity(db,item)==1


def test_closed_item_and_wrong_customer_cannot_use_partial_stock(n029_delivery_app):
    from app.models import OrderItem
    from app.api.deliveries import _pending_query
    app,factory,ids=n029_delivery_app
    seed_partial(factory,ids)
    with factory() as db:
        assert not list(db.execute(_pending_query(db=db,customer_ids={ids['customer']+100},order_item_id=ids['task_pending'])))
        db.get(OrderItem,ids['task_pending']).is_force_closed=True
        db.commit()
        assert not list(db.execute(_pending_query(db=db,order_item_id=ids['task_pending'])))
    with TestClient(app) as client:
        _login(client)
        result=client.post('/api/deliveries',json={'customer_id':ids['customer'],
            'items':[{'order_item_id':ids['task_pending'],'delivered_quantity':1}]})
        assert result.status_code in (400,409),result.text
