import pytest
import copy
import hashlib
from fastapi.testclient import TestClient
from sqlalchemy import select, func, text
from sqlalchemy.exc import OperationalError, IntegrityError

from tests.test_n035_stocktake_api import stocktake_api, _login, _submission_payload
from app.api import stocktake as api
from app.models.stocktake import StocktakeOrder, StocktakeReview
from app.models.audit import OperationLog
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.models.warehouse_inventory import WarehouseLocation
from app.models.user import User


def write_url(action):
    return '/api/warehouse/stocktakes/confirm' if action=='confirm' else '/api/warehouse/stocktakes'


def resolve(client, action, body):
    return client.post('/api/warehouse/stocktakes/resolve',json={'action':action,'body':body})


def assert_echo(order, body, action, actor):
    assert type(order['id']) is int and order['id']>0
    assert order['order_number'] and order['stocktake_number']==order['order_number']
    assert order['request_action']==action and order['request_idempotency_key']==body['idempotency_key']
    assert order['current_actor_id']==actor
    expected=('mobile-confirm-'+hashlib.sha256(body['idempotency_key'].encode()).hexdigest()
              if action=='confirm' else body['idempotency_key'])
    assert order['idempotency_key']==expected
    for field in ('location_id','location_layout_version','location_address_version','location_position_status','published_map_revision'):
        assert order[field]==body[field]
    original={row['inventory_lot_id']:row for row in body['items']}
    assert len(order['items'])==len(original)
    for row in order['items']:
        source=original[row['inventory_lot_id']]
        assert row['counted_quantity']==source['counted_quantity']
        assert row['lot_version_snapshot']==source['expected_version']
        assert row['quantity_available_snapshot']==source['expected_available']
        assert row['quantity_reserved_snapshot']==source['expected_reserved']


def facts(factory):
    with factory() as db:
        return {
            'lots': [(r.id,r.version,r.warehouse_location_id,r.quantity_available,r.quantity_reserved)
                     for r in db.scalars(select(InventoryLot).order_by(InventoryLot.id))],
            'orders': [(r.id,r.status,r.idempotency_key,r.submitted_by,r.reviewed_by)
                       for r in db.scalars(select(StocktakeOrder).order_by(StocktakeOrder.id))],
            'reviews': [(r.id,r.order_id,r.idempotency_key,r.reviewed_by)
                        for r in db.scalars(select(StocktakeReview).order_by(StocktakeReview.id))],
            'movements': [(r.id,r.inventory_lot_id,r.quantity,r.operator_id,r.idempotency_key)
                          for r in db.scalars(select(InventoryMovement).order_by(InventoryMovement.id))],
            'business_audits': db.scalar(select(func.count(OperationLog.id)).where(OperationLog.action.like('STOCKTAKE_%'))),
        }


def test_confirm_response_construction_failure_rolls_back_all_facts(stocktake_api, monkeypatch):
    app,factory,ids=stocktake_api
    def fail(*args,**kwargs):
        raise OperationalError('response read',{},Exception('database is locked'))
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key='confirm-response-failure',counts={ids['lot1']:15})
        before=facts(factory)
        monkeypatch.setattr(api,'_order_payload',fail)
        result=client.post('/api/warehouse/stocktakes/confirm',json=body)
        assert result.status_code==409
        assert facts(factory)==before


def test_expected_actor_mismatch_rejects_before_confirm_write(stocktake_api):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key='confirm-wrong-actor',counts={ids['lot1']:15})
        before=facts(factory)
        result=client.post('/api/warehouse/stocktakes/confirm',json={**body,'expected_actor_id':ids['admin']+100})
        assert result.status_code==409
        assert result.json()['detail']['code']=='STOCKTAKE_ACTOR_MISMATCH'
        assert result.headers['X-Stocktake-Preserve']=='1'
        assert facts(factory)==before


def test_exact_read_only_resolve_returns_confirmed_order(stocktake_api):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key='confirm-resolve',counts={ids['lot1']:15})
        saved=client.post('/api/warehouse/stocktakes/confirm',json=body)
        assert saved.status_code==201
        before=facts(factory)
        result=client.post('/api/warehouse/stocktakes/resolve',json={'action':'confirm','body':body})
        assert result.status_code==200
        assert result.json()['status']=='found' and result.json()['order']['id']==saved.json()['id']
        assert facts(factory)==before


@pytest.mark.parametrize('action',['submit','confirm'])
def test_write_and_resolve_echo_preserve_old_signature_and_body(stocktake_api,action):
    app,factory,ids=stocktake_api
    account='n035-workshop' if action=='submit' else 'n035-admin'
    actor=ids['workshop'] if action=='submit' else ids['admin']
    with TestClient(app) as client:
        _login(client,account)
        body=_submission_payload(client,ids['location'],key=f'echo-{action}',counts={ids['lot1']:15})
        saved=client.post(write_url(action),json=body)
        assert saved.status_code==201 and 'no-store' in saved.headers['cache-control']
        assert_echo(saved.json(),body,action,actor)
        stored=facts(factory)
        # expected_actor is transport ownership, excluded from old signatures.
        body['expected_actor_id']=actor
        replay=client.post(write_url(action),json=body)
        assert replay.status_code==201 and replay.json()['id']==saved.json()['id']
        result=resolve(client,action,body)
        assert result.status_code==200 and result.json()['status']=='found'
        assert 'no-store' in result.headers['cache-control']
        assert result.json()['observed_at'] and result.json()['current_actor_id']==actor
        assert_echo(result.json()['order'],body,action,actor)
        assert facts(factory)==stored


@pytest.mark.parametrize('action',['submit','confirm'])
def test_not_found_has_no_business_writes_or_cancellation_claim(stocktake_api,action):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key=f'missing-{action}')
        before=facts(factory)
        result=resolve(client,action,body)
        assert result.status_code==200
        assert result.json()['status']=='not_found' and result.json()['order'] is None
        assert result.json()['request_action']==action and result.json()['request_idempotency_key']==body['idempotency_key']
        assert 'can_continue' not in result.json() and 'cancelled' not in result.json()
        assert facts(factory)==before


@pytest.mark.parametrize('action',['submit','confirm'])
def test_completed_resolve_ignores_current_stock_and_location_drift(stocktake_api,action):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key=f'old-{action}',counts={ids['lot1']:15})
        saved=client.post(write_url(action),json=body)
        assert saved.status_code==201
        with factory() as db:
            lot=db.get(InventoryLot,ids['lot1'])
            lot.version+=1
            lot.warehouse_location_id=ids['other_location']
            location=db.get(WarehouseLocation,ids['location'])
            location.address_version+=1
            location.is_active=False
            db.commit()
        before=facts(factory)
        result=resolve(client,action,body)
        assert result.status_code==200 and result.json()['status']=='found'
        assert_echo(result.json()['order'],body,action,ids['admin'])
        assert facts(factory)==before


def test_resolve_business_conflicts_and_duplicate_lines_do_not_collapse(stocktake_api):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key='resolve-conflicts',counts={ids['lot1']:15})
        assert client.post(write_url('confirm'),json=body).status_code==201
        before=facts(factory)
        changed=[]
        for field in ('counted_quantity','expected_version','expected_available','expected_reserved'):
            item=copy.deepcopy(body)
            item['items'][0][field]+=1
            changed.append(item)
        for field,value in [('location_id',ids['other_location']),('location_layout_version',1),
                            ('location_address_version',body['location_address_version']+1),
                            ('location_position_status','mapped'),('published_map_revision','changed')]:
            changed.append({**body,field:value})
        duplicate=copy.deepcopy(body)
        duplicate['items'].append(dict(duplicate['items'][0]))
        changed.append(duplicate)
        for item in changed:
            result=resolve(client,'confirm',item)
            assert result.status_code==409 and result.json()['detail']['code']=='IDEMPOTENCY_CONFLICT'
            assert result.headers['X-Stocktake-Preserve']=='1'
            assert facts(factory)==before
        metadata=copy.deepcopy(body)
        metadata['items'].reverse()
        for item in metadata['items']:
            item['client_line_id']='new-metadata-'+str(item['inventory_lot_id'])
        assert resolve(client,'confirm',metadata).json()['status']=='found'
        assert facts(factory)==before


@pytest.mark.parametrize('value',[True,'1',0])
def test_actor_validation_is_strict_before_any_write(stocktake_api,value):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key='strict-actor')
        before=facts(factory)
        body['expected_actor_id']=value
        assert client.post(write_url('confirm'),json=body).status_code==422
        assert resolve(client,'confirm',body).status_code==422
        assert facts(factory)==before


def test_actor_guard_and_historical_actor_remain_separate(stocktake_api):
    app,factory,ids=stocktake_api
    with TestClient(app) as admin,TestClient(app) as employee:
        _login(admin,'n035-admin')
        _login(employee,'n035-workshop')
        body=_submission_payload(employee,ids['location'],key='historical-employee')
        saved=employee.post(write_url('submit'),json=body)
        assert saved.status_code==201
        before=facts(factory)
        # New authenticated actor may read old business receipt, without
        # inventing historical ownership or replaying another actor's write.
        result=resolve(admin,'submit',{**body,'expected_actor_id':ids['admin']})
        assert result.status_code==200 and result.json()['current_actor_id']==ids['admin']
        assert result.json()['order']['submitted_by']==ids['workshop']
        rejected=resolve(admin,'submit',{**body,'expected_actor_id':ids['workshop']})
        assert rejected.status_code==409 and rejected.headers['X-Stocktake-Actor-Mismatch']=='1'
        wrong=employee.post(write_url('submit'),json={**body,'expected_actor_id':ids['admin']})
        assert wrong.status_code==409 and wrong.headers['X-Stocktake-Preserve']=='1'
        assert facts(factory)==before


@pytest.mark.parametrize('review_action',['approve','reject'])
def test_employee_resolution_returns_actual_reviewed_state(stocktake_api,review_action):
    app,factory,ids=stocktake_api
    with TestClient(app) as employee,TestClient(app) as admin:
        _login(employee,'n035-workshop')
        _login(admin,'n035-admin')
        body=_submission_payload(employee,ids['location'],key=f'employee-{review_action}')
        order=employee.post(write_url('submit'),json=body).json()
        reviewed=admin.post(f"/api/warehouse/stocktakes/{order['id']}/{review_action}",
                            json={'idempotency_key':f'review-{review_action}'})
        assert reviewed.status_code==200
        before=facts(factory)
        result=resolve(employee,'submit',body)
        assert result.status_code==200 and result.json()['status']=='found'
        assert result.json()['order']['status']==('approved' if review_action=='approve' else 'rejected')
        assert facts(factory)==before


@pytest.mark.parametrize('account,action',[('n035-workshop','confirm'),('n035-restricted','submit')])
def test_resolve_keeps_current_permission_and_full_customer_scope(stocktake_api,account,action):
    app,factory,ids=stocktake_api
    with TestClient(app) as admin,TestClient(app) as denied:
        _login(admin,'n035-admin')
        _login(denied,account)
        body=_submission_payload(admin,ids['location'],key='denied-resolve')
        before=facts(factory)
        assert resolve(denied,action,body).status_code==403
        assert facts(factory)==before
    if account=='n035-workshop':
        with factory() as db:
            assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.action_code=='permission.denied'))>0


@pytest.mark.parametrize('damage',['key','details','actor'])
def test_damaged_confirmation_review_is_conflict_not_found(stocktake_api,damage):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key='damaged-confirm',counts={ids['lot1']:15})
        assert client.post(write_url('confirm'),json=body).status_code==201
        with factory() as db:
            # Deliberately corrupt only this isolated synthetic DB; ordinary
            # tests retain the real immutable-review trigger throughout.
            db.execute(text('DROP TRIGGER trg_stocktake_reviews_immutable_update'))
            review=db.scalar(select(StocktakeReview))
            if damage=='key':
                review.idempotency_key='another-command'
            elif damage=='details':
                review.details_json='broken-json-structure'
            else:
                review.reviewed_by=ids['workshop']
            db.commit()
        before=facts(factory)
        result=resolve(client,'confirm',body)
        assert result.status_code==409 and result.json()['detail']['code']=='IDEMPOTENCY_CONFLICT'
        assert facts(factory)==before


def test_no_difference_confirmation_has_valid_receipt_without_adjustment(stocktake_api):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key='no-difference')
        assert client.post(write_url('confirm'),json=body).status_code==201
        before=facts(factory)
        assert before['movements']==[]
        result=resolve(client,'confirm',body)
        assert result.status_code==200 and result.json()['status']=='found'
        assert all(item['adjustment_movement_id'] is None for item in result.json()['order']['items'])
        assert facts(factory)==before


def test_employee_response_failure_rolls_back_submission(stocktake_api,monkeypatch):
    app,factory,ids=stocktake_api
    def fail(*args,**kwargs):
        raise RuntimeError('receipt-construction')
    with TestClient(app) as client:
        _login(client,'n035-workshop')
        body=_submission_payload(client,ids['location'],key='employee-response-failure')
        before=facts(factory)
        monkeypatch.setattr(api,'_order_payload',fail)
        with pytest.raises(RuntimeError,match='receipt-construction'):
            client.post(write_url('submit'),json=body)
        assert facts(factory)==before


def test_employee_integrity_replay_keeps_same_echo(stocktake_api,monkeypatch):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        _login(client,'n035-workshop')
        body=_submission_payload(client,ids['location'],key='employee-integrity-replay')
        saved=client.post(write_url('submit'),json=body)
        assert saved.status_code==201
        before=facts(factory)
        def fail(*args,**kwargs):
            raise IntegrityError('simulated concurrent unique claim',{},Exception('unique'))
        monkeypatch.setattr(api.stocktake_service,'create_stocktake',fail)
        replay=client.post(write_url('submit'),json=body)
        assert replay.status_code==201 and 'no-store' in replay.headers['cache-control']
        assert_echo(replay.json(),body,'submit',ids['workshop'])
        assert facts(factory)==before


def test_resolve_does_not_autoflush_pending_session_changes(stocktake_api):
    from app.api.deps import get_db
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key='no-autoflush')
        original=app.dependency_overrides[get_db]
        def dirty_session():
            with factory(autoflush=False) as db:
                user=db.get(User,ids['admin'])
                user.real_name='尚未写入的调用方字段'
                yield db
                assert user.real_name=='尚未写入的调用方字段'
        app.dependency_overrides[get_db]=dirty_session
        try:
            assert resolve(client,'submit',body).json()['status']=='not_found'
        finally:
            app.dependency_overrides[get_db]=original
        with factory() as db:
            assert db.get(User,ids['admin']).real_name!='尚未写入的调用方字段'


def test_concurrent_same_key_confirm_has_one_business_effect(stocktake_api):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key='concurrent-confirm',counts={ids['lot1']:15})
    ready=Barrier(2)
    def send():
        with TestClient(app) as client:
            _login(client,'n035-admin')
            ready.wait(timeout=10)
            return client.post(write_url('confirm'),json=body)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses=list(executor.map(lambda _:send(),range(2)))
    assert all(response.status_code==201 for response in responses),[response.text for response in responses]
    assert responses[0].json()['id']==responses[1].json()['id']
    after=facts(factory)
    assert len(after['orders'])==len(after['reviews'])==len(after['movements'])==1
    assert after['business_audits']==2


@pytest.mark.parametrize('after_commit',[False,True],ids=['commit-failed','commit-result-unknown'])
def test_commit_failure_or_lost_ack_is_observed_by_resolve(stocktake_api,monkeypatch,after_commit):
    from sqlalchemy.orm import Session
    app,factory,ids=stocktake_api
    original=Session.commit
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key='commit-outcome',counts={ids['lot1']:15})
        before=facts(factory)
        def fail(db):
            if any(isinstance(row,StocktakeOrder) for row in db.identity_map.values()):
                if after_commit:
                    original(db)
                raise OperationalError('commit outcome',{},Exception('database is locked'))
            return original(db)
        monkeypatch.setattr(Session,'commit',fail)
        result=client.post(write_url('confirm'),json=body)
        assert result.status_code==409 and result.headers['X-Stocktake-Preserve']=='1'
        monkeypatch.setattr(Session,'commit',original)
        observed=resolve(client,'confirm',body)
        assert observed.status_code==200
        if after_commit:
            assert observed.json()['status']=='found'
            stored=facts(factory)
            assert len(stored['orders'])==len(stored['reviews'])==len(stored['movements'])==1
            replay=client.post(write_url('confirm'),json=body)
            assert replay.status_code==201 and facts(factory)==stored
        else:
            assert observed.json()['status']=='not_found' and facts(factory)==before


def test_shortfall_confirmation_resolves_real_release_and_adjustment(stocktake_api):
    from tests.test_mobile_stocktake_confirm import _seed_count_reservations
    app,factory,ids=stocktake_api
    _seed_count_reservations(factory,ids)
    with TestClient(app) as client:
        _login(client,'n035-admin')
        body=_submission_payload(client,ids['location'],key='shortfall-resolve',counts={ids['lot1']:9})
        saved=client.post(write_url('confirm'),json=body)
        assert saved.status_code==201,saved.text
        before=facts(factory)
        result=resolve(client,'confirm',body)
        assert result.status_code==200 and result.json()['status']=='found',result.text
        assert result.json()['order']['id']==saved.json()['id']
        assert facts(factory)==before
