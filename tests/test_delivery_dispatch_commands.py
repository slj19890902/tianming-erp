"""Actual HTTP commands against disposable stock and delivery facts."""
import copy
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func,select,event

from tests.test_phase7_deliveries import delivery_api_app,_create_payload,_login,_seed_historical_finished_delivery_inventory
from tests.test_n036_delivery_pick import pick_app,_create_task,_login as pick_login
from app.api import deliveries
from app.models.delivery import Delivery,DeliveryPickTask
from app.models.finance import FinanceIdempotencyRecord
from app.models.order import OrderItem
from app.models.warehouse_inventory import InventoryLot,InventoryMovement


def state(factory):
    with factory() as db:
        return dict(deliveries=[(r.id,r.status,r.version,r.total_quantity) for r in db.scalars(select(Delivery).order_by(Delivery.id))],
            lots=[(r.id,r.quantity_available,r.quantity_reserved,r.quantity_consumed,r.version) for r in db.scalars(select(InventoryLot).order_by(InventoryLot.id))],
            movements=db.scalar(select(func.count()).select_from(InventoryMovement)),
            records=db.scalar(select(func.count()).select_from(FinanceIdempotencyRecord)),
            order_items=[(r.id,r.delivered_quantity) for r in db.scalars(select(OrderItem).order_by(OrderItem.id))],
            tasks=[(r.id,r.status,r.applied_by) for r in db.scalars(select(DeliveryPickTask).order_by(DeliveryPickTask.id))])


def seed(client,factory):
    _seed_historical_finished_delivery_inventory(factory,1,2)
    created=client.post('/api/deliveries',json=dict(_create_payload(),idempotency_key='dispatch-test-seed'))
    assert created.status_code==201,created.text
    return created.json()


def command(client,did,key='dispatch-command-first',pick_action='none'):
    response=client.get(f'/api/deliveries/{did}/dispatch-snapshot',params={'pick_action':pick_action})
    assert response.status_code==200,response.text
    value=response.json()
    return dict(expected_actor_id=value['current_actor_id'],idempotency_key=key,expected_version=value['snapshot']['delivery']['version'],snapshot_hash=value['snapshot_hash'],snapshot=value['snapshot'],confirm_pick_exception=pick_action=='apply')


def resolve(client,did,body):
    return client.post(f"/api/deliveries/{did}/dispatch-results/{body['idempotency_key']}/resolve",json=dict(expected_actor_id=body['expected_actor_id'],original_request=body))


def test_external_legacy_put_rejected_without_inventory_effect(delivery_api_app):
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');created=seed(c,factory);before=state(factory)
        old=c.put(f"/api/deliveries/{created['id']}/dispatch")
        assert old.status_code in (400,409,422),old.text
        assert state(factory)==before
        assert old.headers['cache-control']=='no-store'


def test_complete_replay_cancel_and_old_version_are_distinct(delivery_api_app):
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id'];body=command(c,did)
        first=c.put(f'/api/deliveries/{did}/dispatch',json=body);assert first.status_code==200,first.text
        receipt=first.json()['dispatch_receipt'];assert receipt['original_version']==1 and receipt['completed_version']==2
        assert len(receipt['final_items'])==2 and sum(r['physical_quantity'] for r in receipt['movements'])==70
        after=state(factory);replay=c.put(f'/api/deliveries/{did}/dispatch',json=body)
        assert replay.json()==first.json() and state(factory)==after
        cancel=c.put(f'/api/deliveries/{did}/cancel');assert cancel.status_code==200,cancel.text
        assert cancel.json()['version']==3
        restored=state(factory);old=c.put(f'/api/deliveries/{did}/dispatch',json=body)
        assert old.json()==first.json() and state(factory)==restored
        stale=copy.deepcopy(body);stale['idempotency_key']='dispatch-command-stale'
        rejected=c.put(f'/api/deliveries/{did}/dispatch',json=stale);assert rejected.status_code==409
        assert state(factory)==restored
        recovered=resolve(c,did,body);assert recovered.json()['status']=='completed'
        assert recovered.json()['dispatch_receipt']==receipt and recovered.json()['current']['status']=='pending'
        fresh=command(c,did,'dispatch-command-new');new=c.put(f'/api/deliveries/{did}/dispatch',json=fresh)
        assert new.status_code==200 and new.json()['dispatch_receipt']['completed_version']==4


def test_snapshot_drift_and_actor_conflict_preserve_original(delivery_api_app):
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id'];body=command(c,did)
        altered=copy.deepcopy(body);altered['expected_actor_id']=True
        assert c.put(f'/api/deliveries/{did}/dispatch',json=altered).status_code==422
        mismatch=copy.deepcopy(body);mismatch['expected_actor_id']+=100
        response=c.put(f'/api/deliveries/{did}/dispatch',json=mismatch)
        assert response.status_code==409 and response.headers.get('x-delivery-dispatch-actor-mismatch')=='1'
        edit=_create_payload();edit.pop('customer_id');edit.update(expected_version=1,idempotency_key='dispatch-edit-drift');edit['items'][0]['delivered_quantity']=25
        assert c.put(f'/api/deliveries/{did}',json=edit).status_code==200
        before=state(factory);stale=c.put(f'/api/deliveries/{did}/dispatch',json=body)
        assert stale.status_code==409 and state(factory)==before
        found=resolve(c,did,body);assert found.json()['status']=='not_recorded'


@pytest.mark.parametrize('phase',['prepare','dispatch','receipt'])
def test_parent_failure_rolls_back_every_phase(phase,delivery_api_app,monkeypatch):
    from app.services import delivery_dispatch_commands as service
    app,factory=delivery_api_app
    with TestClient(app,raise_server_exceptions=False) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id'];body=command(c,did);before=state(factory)
        target={'prepare':'_prepare_legacy_accompany','dispatch':'_dispatch_delivery'}
        if phase=='receipt':monkeypatch.setattr(service,'build_receipt',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('synthetic receipt failure')))
        else:
            name=target[phase];real=getattr(deliveries,name)
            def failed(*args,**kwargs):real(*args,**kwargs);raise RuntimeError('synthetic after '+phase)
            monkeypatch.setattr(deliveries,name,failed)
        response=c.put(f'/api/deliveries/{did}/dispatch',json=body)
        assert response.status_code==500 and state(factory)==before
        assert response.headers.get('x-delivery-dispatch-preserve')=='1'
        assert resolve(c,did,body).json()['status']=='not_recorded'


def test_committed_ack_loss_and_readonly_result(delivery_api_app,monkeypatch):
    from app.services import delivery_dispatch_commands as service
    app,factory=delivery_api_app
    with TestClient(app,raise_server_exceptions=False) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id'];body=command(c,did)
        real=service.commit_command
        def ack_loss(db):real(db);raise RuntimeError('synthetic committed ack loss')
        with monkeypatch.context() as patch:patch.setattr(service,'commit_command',ack_loss);failed=c.put(f'/api/deliveries/{did}/dispatch',json=body)
        assert failed.status_code==500
        before=state(factory);statements=[]
        def sql(conn,cursor,statement,*args):statements.append(statement.lstrip().split()[0].upper())
        event.listen(factory.kw['bind'],'before_cursor_execute',sql)
        try:
            found=resolve(c,did,body)
            changed=copy.deepcopy(body);changed['confirm_pick_exception']=True
            conflict=resolve(c,did,changed)
        finally:event.remove(factory.kw['bind'],'before_cursor_execute',sql)
        assert found.status_code==200 and found.json()['status']=='completed'
        assert conflict.status_code==409 and not {'INSERT','UPDATE','DELETE'} & set(statements)
        assert state(factory)==before


def test_atomic_pick_apply_failure_restores_original_task(pick_app,monkeypatch):
    app,factory,ids,_=pick_app
    with TestClient(app,raise_server_exceptions=False) as c:
        pick_login(c,'admin');_seed_historical_finished_delivery_inventory(factory,*ids['order_items']);task=_create_task(c,ids['delivery'])
        pick_login(c,'delivery_picker')
        for index,row in enumerate(task['items']):
            assert c.put(f"/api/delivery-picks/{task['id']}/items/{row['id']}",json=dict(pick_status='partial' if index==0 else 'picked',picked_quantity=80 if index==0 else 50)).status_code==200
        assert c.post(f"/api/delivery-picks/{task['id']}/submit").status_code==200
        pick_login(c,'admin');body=command(c,ids['delivery'],pick_action='apply');before=state(factory)
        with monkeypatch.context() as p:p.setattr(deliveries,'_prepare_legacy_accompany',lambda *a: (_ for _ in ()).throw(RuntimeError('synthetic after applied')));failed=c.put(f"/api/deliveries/{ids['delivery']}/dispatch",json=body)
        assert failed.status_code==500 and state(factory)==before
        saved=c.put(f"/api/deliveries/{ids['delivery']}/dispatch",json=body);assert saved.status_code==200,saved.text
        receipt=saved.json()['dispatch_receipt'];assert receipt['stages']['pick_applied'] and sum(r['customer_quantity'] for r in receipt['final_items'])==130
        after=state(factory);assert c.put(f"/api/deliveries/{ids['delivery']}/dispatch",json=body).json()==saved.json()
        assert state(factory)==after


def test_activation_and_permission_denial_never_dispatch(delivery_api_app,monkeypatch):
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id'];body=command(c,did);before=state(factory)
        from app.core.config import load_settings
        app.state.erp_settings=load_settings()
        with monkeypatch.context() as patch:
            patch.setenv('ERP_ENVIRONMENT','production')
            blocked=c.put(f'/api/deliveries/{did}/dispatch',json=body)
            assert blocked.status_code==503 and blocked.headers['cache-control']=='no-store'
        assert state(factory)==before
        _login(c,'finance')
        denied=c.put(f'/api/deliveries/{did}/dispatch',json=body)
        assert denied.status_code==403 and denied.headers.get('x-delivery-dispatch-preserve')=='1'
        assert state(factory)==before


def test_current_and_original_customer_scope_and_corrupt_proof(delivery_api_app):
    from app.models.user import User
    from app.models.order import Order
    from app.models.access_control import UserPermissionOverride,UserCustomerScope
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id'];body=command(c,did)
        saved=c.put(f'/api/deliveries/{did}/dispatch',json=body);assert saved.status_code==200
        with factory() as db:
            actor=db.get(User,body['expected_actor_id']);actor.role='sales';actor.customer_access_mode='selected'
            db.add(UserPermissionOverride(user_id=actor.id,permission_code='deliveries.execute',is_allowed=True))
            db.add(UserCustomerScope(user_id=actor.id,customer_id=1));db.commit()
        _login(c,'admin')
        found=resolve(c,did,body);assert found.status_code==200 and found.json()['status']=='completed'
        with factory() as db:
            db.get(Delivery,did).customer_id=2;db.commit()
        before=state(factory);denied=resolve(c,did,body)
        assert denied.status_code==403 and state(factory)==before
        with factory() as db:
            db.get(Delivery,did).customer_id=1;db.get(Order,1).customer_id=2;db.commit()
        assert resolve(c,did,body).status_code==403
        with factory() as db:
            db.get(Order,1).customer_id=1
            record=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key==body['idempotency_key']))
            damaged=json.loads(record.response_json);damaged['dispatch_receipt']['final_items'][0]['physical_quantity']+=1
            record.response_json=json.dumps(damaged);db.commit()
        before=state(factory);trace=resolve(c,did,body)
        assert trace.status_code==200 and trace.json()['status']=='trace' and trace.json()['dispatch_receipt'] is None
        assert c.put(f'/api/deliveries/{did}/dispatch',json=body).status_code==409
        assert state(factory)==before


def test_snapshot_pick_read_is_zero_dml(pick_app):
    app,factory,ids,_=pick_app
    with TestClient(app) as c:
        pick_login(c,'admin');_create_task(c,ids['delivery'])
        before=state(factory);statements=[]
        def sql(conn,cursor,statement,*args):statements.append(statement.lstrip().split()[0].upper())
        event.listen(factory.kw['bind'],'before_cursor_execute',sql)
        try:
            body=command(c,ids['delivery']);found=resolve(c,ids['delivery'],body)
        finally:event.remove(factory.kw['bind'],'before_cursor_execute',sql)
        assert found.status_code==200 and found.json()['status']=='not_recorded'
        assert not {'INSERT','UPDATE','DELETE'} & set(statements) and state(factory)==before



def close_request(client,did,body):
    return client.post(f"/api/deliveries/{did}/dispatch-results/{body['idempotency_key']}/close",json=dict(expected_actor_id=body['expected_actor_id'],original_request=body))


def test_execute_wins_then_close_keeps_original_completion(delivery_api_app):
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id'];body=command(c,did)
        write=c.put(f'/api/deliveries/{did}/dispatch',json=body);assert write.status_code==200
        before=state(factory);closed=close_request(c,did,body)
        assert closed.status_code==200 and closed.json()==write.json() and state(factory)==before
        assert resolve(c,did,body).json()['status']=='completed'


def test_close_wins_then_late_execute_never_consumes(delivery_api_app):
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id'];body=command(c,did)
        edit=_create_payload();edit.pop('customer_id');edit.update(expected_version=1,idempotency_key='close-after-stale-edit');edit['items'][0]['delivered_quantity']=25
        assert c.put(f'/api/deliveries/{did}',json=edit).status_code==200
        before=state(factory);closed=close_request(c,did,body)
        assert closed.status_code==200 and closed.json()['result']=='closed'
        closure=closed.json()['closure_receipt'];assert closure['request']['expected_version']==1 and closure['closed_at']
        after=state(factory);assert after['lots']==before['lots'] and after['deliveries']==before['deliveries'] and after['records']==before['records']+1
        assert c.put(f'/api/deliveries/{did}/dispatch',json=body).json()==closed.json()
        assert close_request(c,did,body).json()==closed.json() and state(factory)==after
        found=resolve(c,did,body);assert found.json()['status']=='closed' and found.json()['closure_receipt']==closure and found.json()['dispatch_receipt'] is None
        changed=copy.deepcopy(body);changed['confirm_pick_exception']=True
        assert close_request(c,did,changed).status_code==409 and state(factory)==after


def test_close_failure_and_committed_ack_loss_then_new_confirmation(delivery_api_app,monkeypatch):
    from app.services import delivery_dispatch_commands as service
    app,factory=delivery_api_app
    with TestClient(app,raise_server_exceptions=False) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id'];body=command(c,did);before=state(factory)
        real_audit=deliveries._write_audit
        def fail_audit(*a,**kw):
            if kw.get('action')=='CLOSE_DISPATCH_REQUEST':raise RuntimeError('synthetic close audit failure')
            return real_audit(*a,**kw)
        with monkeypatch.context() as patch:patch.setattr(deliveries,'_write_audit',fail_audit);failed=close_request(c,did,body)
        assert failed.status_code==500 and state(factory)==before and resolve(c,did,body).json()['status']=='not_recorded'
        _login(c,'finance');denied=close_request(c,did,body);assert denied.status_code==403 and state(factory)==before
        _login(c,'admin');real=service.commit_command
        def ack_loss(db):real(db);raise RuntimeError('synthetic close committed ack loss')
        with monkeypatch.context() as patch:patch.setattr(service,'commit_command',ack_loss);failed=close_request(c,did,body)
        assert failed.status_code==500
        after=state(factory);assert after['lots']==before['lots'] and after['deliveries']==before['deliveries']
        found=resolve(c,did,body);assert found.json()['status']=='closed'
        assert close_request(c,did,body).json()['closure_receipt']==found.json()['closure_receipt'] and state(factory)==after
        fresh=command(c,did,'command-after-closed');saved=c.put(f'/api/deliveries/{did}/dispatch',json=fresh)
        assert saved.status_code==200 and saved.json()['result']=='completed'



def test_missing_consumption_evidence_is_not_completion(delivery_api_app):
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id'];body=command(c,did)
        saved=c.put(f'/api/deliveries/{did}/dispatch',json=body);assert saved.status_code==200
        with factory() as db:
            record=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key==body['idempotency_key']))
            damaged=json.loads(record.response_json);damaged['dispatch_receipt']['movements']=[];damaged['dispatch_receipt']['direct_component_allocations']=[]
            record.response_json=json.dumps(damaged);db.commit()
        before=state(factory);found=resolve(c,did,body)
        assert found.status_code==200 and found.json()['status']=='trace' and found.json()['dispatch_receipt'] is None
        assert state(factory)==before


def test_replay_checks_frozen_item_customer_after_current_scope_changes(delivery_api_app):
    from app.models.order import Order
    from app.models.product import Product
    from app.models.warehouse_inventory import FinishedGoodsInventoryDetail
    from app.models.user import User
    from app.models.access_control import UserPermissionOverride,UserCustomerScope
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id']
        with factory() as db:
            db.get(Order,1).customer_id=2;db.get(Product,1).customer_id=2
            for detail in db.scalars(select(FinishedGoodsInventoryDetail).where(FinishedGoodsInventoryDetail.product_id==1)):
                detail.owner_customer_id=2
            db.commit()
        body=command(c,did,'frozen-item-scope-command')
        assert {r['customer_id'] for r in body['snapshot']['items']}=={1,2}
        saved=c.put(f'/api/deliveries/{did}/dispatch',json=body);assert saved.status_code==200,saved.text
        with factory() as db:
            db.get(Order,1).customer_id=1
            db.get(Product,1).customer_id=1
            actor=db.get(User,body['expected_actor_id']);actor.role='sales';actor.customer_access_mode='selected'
            db.add(UserPermissionOverride(user_id=actor.id,permission_code='deliveries.execute',is_allowed=True))
            db.add(UserCustomerScope(user_id=actor.id,customer_id=1));db.commit()
        _login(c,'admin');before=state(factory)
        for response in (resolve(c,did,body),c.put(f'/api/deliveries/{did}/dispatch',json=body),close_request(c,did,body)):
            assert response.status_code==403 and response.headers['x-delivery-dispatch-preserve']=='1'
        assert state(factory)==before
