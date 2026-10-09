import pytest
import os
import json
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, IntegrityError
from sqlalchemy.orm import Session

from tests.test_n035_stocktake_api import stocktake_api, _login, _logout, _submission_payload
from tests.test_mobile_stocktake_recovery_api import facts
from app.api import stocktake as api
from app.models.stocktake import StocktakeReview
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation


def prepare(client,ids,key,count=15):
    _login(client,'n035-workshop')
    submission=_submission_payload(client,ids['location'],key=f'submit-{key}',counts={ids['lot1']:count} if count is not None else None)
    submitted=client.post('/api/warehouse/stocktakes',json=submission)
    assert submitted.status_code==201
    _logout(client)
    _login(client,'n035-admin')
    return submitted.json()['id'],{'idempotency_key':key,'expected_actor_id':ids['admin']}


def resolve(client,order,action,body):
    return client.post(f'/api/warehouse/stocktakes/{order}/review-result',json={'action':action,'body':body})


def export_receipt(name,action,body,receipt):
    directory=os.environ.get('STOCKTAKE_REVIEW_EVIDENCE')
    if directory:
        target=Path(directory);target.mkdir(parents=True,exist_ok=True)
        (target/f'{name}-receipt.json').write_text(json.dumps({'action':action,'ownerId':body['expected_actor_id'],
            'body':body,'httpStatus':200,'receipt':receipt},ensure_ascii=False,indent=2),encoding='utf-8')


@pytest.mark.parametrize('action',['approve','reject'])
def test_first_review_receipt_is_complete_and_exact_readonly_result_matches(stocktake_api,action):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        order,body=prepare(client,ids,f'full-{action}')
        written=client.post(f'/api/warehouse/stocktakes/{order}/{action}',json=body)
        assert written.status_code==200
        receipt=written.json()
        assert len(receipt['reviews'])==1 and receipt['reviewed_by_name']
        assert receipt['request_action']==action and receipt['request_idempotency_key']==body['idempotency_key']
        assert receipt['current_actor_id']==ids['admin']
        review=receipt['matched_review']
        assert review['id']==receipt['reviews'][0]['id'] and review['reviewed_by']==ids['admin']
        assert review['reviewed_at']==receipt['reviewed_at'] and review['action']==action
        assert review['idempotency_key']==body['idempotency_key']
        assert 'no-store' in written.headers['cache-control']
        export_receipt(action,action,body,receipt)
        before=facts(factory)
        found=resolve(client,order,action,body)
        assert found.status_code==200 and found.json()['status']=='found'
        assert found.json()['order']==receipt and found.json()['matched_review']==review
        assert facts(factory)==before
        replay=client.post(f'/api/warehouse/stocktakes/{order}/{action}',json=body)
        assert replay.status_code==200 and replay.json()==receipt and facts(factory)==before


@pytest.mark.parametrize('action',['approve','reject'])
def test_actor_mismatch_rejects_before_review_and_keeps_unknown(stocktake_api,action):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        order,body=prepare(client,ids,f'actor-{action}')
        body['expected_actor_id']=ids['admin']+100
        before=facts(factory)
        for url,request in ((f'/api/warehouse/stocktakes/{order}/{action}',body),
                            (f'/api/warehouse/stocktakes/{order}/review-result',{'action':action,'body':body})):
            result=client.post(url,json=request)
            assert result.status_code==409 and result.json()['detail']['code']=='STOCKTAKE_ACTOR_MISMATCH'
            assert result.headers['x-stocktake-preserve']=='1' and result.headers['x-stocktake-actor-mismatch']=='1'
            assert 'x-stocktake-rejected' not in result.headers and facts(factory)==before


@pytest.mark.parametrize('action',['approve','reject'])
def test_unknown_key_is_observation_with_zero_business_writes(stocktake_api,action):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        order,body=prepare(client,ids,f'not-found-{action}')
        before=facts(factory)
        result=resolve(client,order,action,body)
        assert result.status_code==200
        data=result.json()
        assert data['status']=='not_found' and data['order'] is None and data['matched_review'] is None
        assert data['current_actor_id']==ids['admin'] and data['observed_at']
        assert not any(field in data for field in ('cancelled','can_continue','terminal_expired'))
        assert facts(factory)==before


@pytest.mark.parametrize('action',['approve','reject'])
def test_review_response_generation_failure_rolls_back_business_facts(stocktake_api,monkeypatch,action):
    app,factory,ids=stocktake_api
    with TestClient(app,raise_server_exceptions=False) as client:
        order,body=prepare(client,ids,f'receipt-failure-{action}')
        before=facts(factory)
        def fail(*args,**kwargs):
            raise OperationalError('receipt',{},Exception('database is locked'))
        monkeypatch.setattr(api,'_order_payload',fail)
        result=client.post(f'/api/warehouse/stocktakes/{order}/{action}',json=body)
        assert result.status_code==409 and result.headers['x-stocktake-preserve']=='1'
        assert facts(factory)==before


@pytest.mark.parametrize('action',['approve','reject'])
@pytest.mark.parametrize('committed',[False,True])
def test_review_commit_failure_and_ack_loss_are_recovered_without_guessing(stocktake_api,monkeypatch,action,committed):
    app,factory,ids=stocktake_api
    original=Session.commit
    with TestClient(app) as client:
        order,body=prepare(client,ids,f'commit-{action}-{committed}')
        before=facts(factory)
        def fail(db):
            if committed:original(db)
            raise OperationalError('commit',{},Exception('database is locked'))
        monkeypatch.setattr(Session,'commit',fail)
        result=client.post(f'/api/warehouse/stocktakes/{order}/{action}',json=body)
        monkeypatch.setattr(Session,'commit',original)
        assert result.status_code==409 and result.headers['x-stocktake-preserve']=='1'
        after=facts(factory)
        found=resolve(client,order,action,body)
        assert found.status_code==200 and found.json()['status']==('found' if committed else 'not_found')
        assert facts(factory)==after
        if not committed:assert after==before
        replay=client.post(f'/api/warehouse/stocktakes/{order}/{action}',json=body)
        assert replay.status_code==200
        if committed:assert facts(factory)==after


@pytest.mark.parametrize('actor',[True,'1',0])
def test_review_actor_is_strict_in_write_and_resolver(stocktake_api,actor):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        order,body=prepare(client,ids,f'strict-{type(actor).__name__}-{actor}')
        body['expected_actor_id']=actor
        before=facts(factory)
        assert client.post(f'/api/warehouse/stocktakes/{order}/approve',json=body).status_code==422
        assert resolve(client,order,'approve',body).status_code==422
        assert facts(factory)==before


@pytest.mark.parametrize('action',['approve','reject'])
def test_review_exact_signature_and_current_permissions_precede_history(stocktake_api,action):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        order,body=prepare(client,ids,f'signature-{action}')
        body['reason']='  核对  '
        assert client.post(f'/api/warehouse/stocktakes/{order}/{action}',json=body).status_code==200
        before=facts(factory)
        for request_action,request_body in ((action,{**body,'reason':'不同'}),('reject' if action=='approve' else 'approve',body)):
            response=resolve(client,order,request_action,request_body)
            assert response.status_code==409 and response.headers['x-stocktake-preserve']=='1'
            assert facts(factory)==before
        _logout(client);_login(client,'n035-workshop')
        assert resolve(client,order,action,body).status_code==403
        assert facts(factory)==before
        with factory() as db:
            from app.models.audit import OperationLog
            assert db.scalar(select(OperationLog).where(OperationLog.action_code=='permission.denied')) is not None
        with factory() as db:
            from app.models.access_control import UserPermissionOverride
            db.add(UserPermissionOverride(user_id=ids['restricted'],permission_code='warehouse.stocktake.review',is_allowed=True))
            db.commit()
        _logout(client);_login(client,'n035-restricted')
        assert resolve(client,order,action,{**body,'expected_actor_id':ids['restricted']}).status_code==403
        assert facts(factory)==before


@pytest.mark.parametrize('action',['approve','reject'])
def test_completed_review_uses_history_when_current_stock_or_location_changed(stocktake_api,action):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        order,body=prepare(client,ids,f'changed-{action}')
        saved=client.post(f'/api/warehouse/stocktakes/{order}/{action}',json=body).json()
        with factory() as db:
            db.get(WarehouseLocation,ids['location']).is_active=False
            lot=db.get(InventoryLot,ids['lot1']);lot.version+=1;lot.quantity_available+=2
            db.commit()
        before=facts(factory)
        result=resolve(client,order,action,body)
        assert result.status_code==200 and result.json()['status']=='found'
        assert result.json()['matched_review']==saved['matched_review'] and facts(factory)==before


@pytest.mark.parametrize('action',['approve','reject'])
@pytest.mark.parametrize('damage',['actor','details','transition'])
def test_corrupted_review_cannot_be_presented_as_found(stocktake_api,action,damage):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        order,body=prepare(client,ids,f'corrupt-{action}-{damage}')
        assert client.post(f'/api/warehouse/stocktakes/{order}/{action}',json=body).status_code==200
        with factory() as db:
            db.execute(text('DROP TRIGGER trg_stocktake_reviews_immutable_update'))
            review=db.scalar(select(StocktakeReview))
            if damage=='actor':review.reviewed_by=ids['workshop']
            elif damage=='details':review.details_json={'damaged':True}
            else:review.from_status='draft'
            # Preserve the real SQL check constraint by changing only a valid
            # source status? The model allows submitted only: bypass solely
            # the isolated corruption fixture's CHECK validation if required.
            if damage=='transition':db.execute(text('PRAGMA ignore_check_constraints=ON'))
            db.commit()
        before=facts(factory)
        result=resolve(client,order,action,body)
        assert result.status_code==409 and result.headers['x-stocktake-preserve']=='1'
        assert facts(factory)==before


@pytest.mark.parametrize('scenario',['no-difference','shortfall'])
def test_approve_recovery_preserves_real_balance_and_release_facts(stocktake_api,scenario):
    app,factory,ids=stocktake_api
    if scenario=='shortfall':
        from tests.test_mobile_stocktake_confirm import _seed_count_reservations
        _seed_count_reservations(factory,ids)
    with TestClient(app) as client:
        order,body=prepare(client,ids,f'balance-{scenario}',9 if scenario=='shortfall' else None)
        written=client.post(f'/api/warehouse/stocktakes/{order}/approve',json=body)
        assert written.status_code==200
        after=facts(factory)
        result=resolve(client,order,'approve',body)
        assert result.status_code==200 and result.json()['order']==written.json()
        export_receipt(scenario,'approve',body,written.json())
        assert facts(factory)==after
        if scenario=='no-difference':assert after['movements']==[]
        else:
            assert len(after['movements'])==3
            assert after['lots'][0][3:]==(0,9)


@pytest.mark.parametrize('action',['approve','reject'])
def test_review_integrity_error_replay_returns_same_complete_contract(stocktake_api,monkeypatch,action):
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        order,body=prepare(client,ids,f'integrity-{action}')
        saved=client.post(f'/api/warehouse/stocktakes/{order}/{action}',json=body)
        before=facts(factory)
        def fail(*args,**kwargs):raise IntegrityError('synthetic race',{},Exception('duplicate'))
        monkeypatch.setattr(api.stocktake_service,f'{action}_stocktake',fail)
        replay=client.post(f'/api/warehouse/stocktakes/{order}/{action}',json=body)
        assert replay.status_code==200 and replay.json()==saved.json()
        assert 'no-store' in replay.headers['cache-control'] and facts(factory)==before


def test_review_resolve_does_not_autoflush_or_commit_unrelated_dirty_fields(stocktake_api,monkeypatch):
    from app.models.user import User
    from sqlalchemy import event
    app,factory,ids=stocktake_api
    original=api.stocktake_service.resolve_review_replay
    writes=[]
    def dirty(db,**kwargs):
        db.get(User,ids['workshop']).real_name='not committed'
        return original(db,**kwargs)
    with TestClient(app) as client:
        order,body=prepare(client,ids,'zero-autoflush')
        listener=lambda *args:writes.append(True)
        event.listen(Session,'before_flush',listener)
        try:
            monkeypatch.setattr(api.stocktake_service,'resolve_review_replay',dirty)
            result=resolve(client,order,'approve',body)
            assert result.status_code==200 and not writes
        finally:
            event.remove(Session,'before_flush',listener)
        with factory() as db:assert db.get(User,ids['workshop']).real_name=='盘点车间'


@pytest.mark.parametrize('action',['approve','reject'])
def test_concurrent_same_review_key_preserves_one_effect_and_can_be_resolved(stocktake_api,monkeypatch,action):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    app,factory,ids=stocktake_api
    original=api.stocktake_service._review_replay
    barrier=threading.Barrier(2)
    def overlap(db,**kwargs):
        result=original(db,**kwargs)
        if result is None:barrier.wait(timeout=10)
        return result
    with TestClient(app) as first,TestClient(app) as second:
        order,body=prepare(first,ids,f'concurrent-{action}')
        _login(second,'n035-admin')
        monkeypatch.setattr(api.stocktake_service,'_review_replay',overlap)
        url=f'/api/warehouse/stocktakes/{order}/{action}'
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses=list(executor.map(lambda client:client.post(url,json=body),[first,second]))
        monkeypatch.setattr(api.stocktake_service,'_review_replay',original)
        assert all(result.status_code in (200,409) for result in responses)
        assert any(result.status_code==200 for result in responses)
        before=facts(factory)
        assert len(before['reviews'])==1 and len(before['movements'])==(1 if action=='approve' else 0)
        found=resolve(first,order,action,body)
        assert found.status_code==200 and found.json()['status']=='found'
        assert first.post(url,json=body).status_code==200 and facts(factory)==before


def test_review_resolver_current_actor_is_independent_of_historical_reviewer(stocktake_api):
    from app.models.user import User
    from app.core.security import hash_password
    from tests.test_n035_stocktake_api import PASSWORD
    app,factory,ids=stocktake_api
    with TestClient(app) as client:
        order,body=prepare(client,ids,'historical-actor')
        assert client.post(f'/api/warehouse/stocktakes/{order}/approve',json=body).status_code==200
        with factory() as db:
            other=User(username='review-other-admin',role='admin',real_name='另一管理员',
                       password_hash=hash_password(PASSWORD),must_change_password=False)
            db.add(other);db.commit();actor=other.id
        _logout(client);_login(client,'review-other-admin')
        body['expected_actor_id']=actor
        before=facts(factory)
        result=resolve(client,order,'approve',body)
        assert result.status_code==200 and result.json()['current_actor_id']==actor
        assert result.json()['matched_review']['reviewed_by']==ids['admin'] and facts(factory)==before
