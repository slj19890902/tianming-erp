"""Real HTTP recovery probes on synthetic stock, with append-only commands."""
from copy import deepcopy
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from stock_preparation_legacy_fixture import stock_replenishment_app, base_stock_replenishment_app
from test_stock_preparation_groups import prepare, plan, action_body
from app.api.stock_preparation import router, GroupAction
from app.models.stock_preparation import StockPreparationCommand as Command, StockPreparationJob as Job
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation
from app.models.audit import OperationLog

URL='/api/production/stock-preparation/group-actions'
RESOLVE='/api/production/stock-preparation/group-completion-result'

@pytest.fixture
def grouped(stock_replenishment_app):
    app,factory=stock_replenishment_app
    app.include_router(router,prefix='/api/production')
    with factory() as db:
        for action in ('UPDATE','DELETE'):
            db.execute(text(f"CREATE TRIGGER stock_preparation_commands_no_{action.lower()} BEFORE {action} ON stock_preparation_commands BEGIN SELECT RAISE(ABORT, '备库安排流水不可修改或删除'); END"))
        db.commit()
    return app,factory

def facts(factory):
    with factory() as db:
        result={m.__tablename__:[tuple(v) for v in db.execute(select(*m.__table__.columns).order_by(*m.__table__.primary_key.columns))] for m in (InventoryLot,Job,Command,InventoryMovement,InventoryReservation)}
        result['audit']=list(db.execute(select(OperationLog.id,OperationLog.details).where(OperationLog.event_category=='business').order_by(OperationLog.id)))
        return result

def prepared(client,app,factory,kind='semi',partial=False):
    parent=prepare(app,factory,client)
    planned,_=plan(client,parent,sets=5)
    assert planned.status_code==200,planned.text
    client._group_workspace_before=client.get('/api/production/stock-preparation?workspace=true&state=pending')
    body=action_body(client,parent,'recovery-group-dispose','dispose')
    body.update(disposition=kind,sets=5,expected_actor_id=1)
    for j in body['jobs']:
        j.update(location_id=7,layout_version=1,output_version=0,lot_id=None)
        if partial:j['actual_output']-=1
    return body

def resolve(client,body):
    return client.post(RESOLVE,json=dict(operation_key=body['operation_key'],original_request=body,expected_actor_id=1))

@pytest.mark.parametrize('kind',['semi','finished'])
def test_first_save_frozen_group_receipt_and_readonly_replay(grouped,kind):
    app,factory=grouped
    with TestClient(app) as client:
        body=prepared(client,app,factory,kind)
        first=client.post(URL,json=body)
        assert first.status_code==200,first.text
        proof=first.json().get('group_completion_receipt')
        assert isinstance(proof,dict),'first real successful HTTP lacks immutable group save receipt'
        assert proof['actor_id']==1 and proof['operation_key']==body['operation_key']
        assert {c['job_id'] for c in proof['children']}=={j['job_id'] for j in body['jobs']}
        after=facts(factory)
        assert client.post(URL,json=body).json()==first.json()
        recovered=resolve(client,body)
        assert recovered.status_code==200,recovered.text
        assert recovered.json()['status']=='completed' and recovered.json()['group_completion_receipt']==proof
        assert recovered.headers['cache-control']=='no-store'
        assert facts(factory)==after


def archive(name,client,body,first,recovered,replay=None,**extra):
    import os
    from pathlib import Path
    target=os.environ.get('GROUP_RECOVERY_EXPORT_DIR')
    if not target:return
    def packet(r):
        return dict(status=r.status_code,body=r.json() if r.headers.get('content-type','').startswith('application/json') else r.text,headers=dict(r.headers))
    workspace=client.get('/api/production/stock-preparation?workspace=true&state=stock')
    history=client.get('/api/production/stock-preparation/history/'+body['group_key'])
    data=dict(originalBody=body,actorId=1,operationKey=body['operation_key'],write=packet(first),resolve=packet(recovered),
              workspaceAfter=packet(workspace),historyTrace=packet(history),**extra)
    before=getattr(client,'_group_workspace_before',None)
    if before is not None:
        data['workspaceBefore']=packet(before)
        data['sourceRow']=next(r for r in before.json()['items'] if r.get('entry_type')=='group_job')
    if replay is not None:data['replay']=packet(replay)
    Path(target).mkdir(parents=True,exist_ok=True)
    Path(target,name+'.json').write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8')


@pytest.mark.parametrize('kind,partial,sets',[('semi',False,5),('semi',True,5),('finished',False,5),('finished',False,2)])
def test_quantities_frozen_recipe_and_replay_conflicts(grouped,kind,partial,sets):
    from app.models.product_bom import ProductBomComponent
    app,factory=grouped
    with TestClient(app) as client:
        body=prepared(client,app,factory,kind,partial);body['sets']=sets
        with factory() as db:
            for edge in db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==body['parent_id'])):
                edge.quantity_per_set=99
            db.commit()
        first=client.post(URL,json=body);assert first.status_code==200,first.text
        proof=first.json()['group_completion_receipt'];assert sorted(c['input_quantity'] for c in proof['children'])==[15,20]
        assert sorted(c['actual_output'] for c in proof['children'])==([14,19] if partial else [15,20])
        assert sorted(c['per_set'] for c in proof['recipe']['children'])==[3,4]
        if kind=='finished':
            assert proof['assembly']['sets']==sets and proof['assembly']['output_stock_quantity']==sets
            assert sum(c['assembly_consumed_quantity'] for c in proof['children'])==sets*7
            assert sum(c['remaining_stock_quantity'] for c in proof['children'])==35-sets*7
        else:
            assert proof['assembly'] is None and all(c['assembly_consumed_quantity']==0 for c in proof['children'])
        after=facts(factory);replay=client.post(URL,json=body);assert replay.json()==first.json()
        changed=deepcopy(body);changed['jobs'][0]['actual_output']+=1
        assert client.post(URL,json=changed).status_code==409
        assert resolve(client,changed).status_code==409
        reverse=deepcopy(body);reverse['jobs'].reverse()
        assert client.post(URL,json=reverse).status_code==409
        stale=deepcopy(body);stale['operation_key']='recovery-group-stale-version'
        rejected=client.post(URL,json=stale)
        assert rejected.status_code==409 and rejected.headers['x-production-group-rejected']=='1'
        assert facts(factory)==after
        recovered=resolve(client,body);assert recovered.json()['group_completion_receipt']==proof
        archive(kind+('-partial' if partial else '-sets-'+str(sets)),client,body,first,recovered,replay)


def test_multiple_receipts_same_component_finished_subset_inputs(grouped,monkeypatch):
    app,factory=grouped
    with TestClient(app) as client:
        real=client.put;split=[]
        def receive(url,**kwargs):
            if url.startswith('/api/incoming/receive/sr') and not split:
                split.append(url);payload=deepcopy(kwargs['json']);payload.update(received_quantity=4,idempotency_key=payload['idempotency_key']+'-first',resolution_action='await_supplier')
                part=real(url,json=payload);assert part.status_code==200,part.text
                payload.update(received_quantity=26,idempotency_key=payload['idempotency_key']+'-rest');payload.pop('resolution_action')
                return real(url,json=payload)
            return real(url,**kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(client,'put',receive);parent=prepare(app,factory,client)
        planned,_=plan(client,parent);assert planned.status_code==200,planned.text
        client._group_workspace_before=client.get('/api/production/stock-preparation?workspace=true&state=pending')
        body=action_body(client,parent,'recovery-multiple-receipts','dispose')
        body.update(disposition='finished',sets=1,expected_actor_id=1)
        for row in body['jobs']:row.update(location_id=7,layout_version=1,lot_id=None)
        assert len(body['jobs'])==3
        body['jobs'].reverse()
        first=client.post(URL,json=body);assert first.status_code==200,first.text
        proof=first.json()['group_completion_receipt'];children=proof['children']
        assert len(children)==3 and len({c['product_id'] for c in children})==2
        assert len({c['receipt_item_id'] for c in children})==3
        assert len(proof['assembly']['inputs'])==2
        assert sum(c['assembly_consumed_quantity']==0 for c in children)==1
        assert sum(c['remaining_stock_quantity'] for c in children)==28
        after=facts(factory);replay=client.post(URL,json=body);assert replay.json()==first.json()
        recovered=resolve(client,body);assert recovered.status_code==200,recovered.text;assert facts(factory)==after
        archive('finished-multi-receipt-subset',client,body,first,recovered,replay)


@pytest.mark.parametrize('fault',['builder','second_audit'])
def test_precommit_failure_rolls_back_all_children_and_assembly(grouped,monkeypatch,fault):
    from app.services import stock_preparation as service,stock_preparation_group_recovery as recovery
    app,factory=grouped
    with TestClient(app,raise_server_exceptions=False) as client:
        body=prepared(client,app,factory,'finished');before=facts(factory)
        real=service.append_audit_event;calls=[]
        def audit(*args,**kwargs):
            if kwargs.get('action_code')=='stock_preparation.complete':
                calls.append(1)
                if len(calls)==2:raise RuntimeError('synthetic child audit failure')
            return real(*args,**kwargs)
        def builder(*args):raise RuntimeError('synthetic proof failure')
        with monkeypatch.context() as patch:
            if fault=='builder':patch.setattr(recovery,'build_receipt',builder)
            else:patch.setattr(service,'append_audit_event',audit)
            failed=client.post(URL,json=body)
        assert failed.status_code==500 and 'x-production-group-rejected' not in failed.headers
        assert facts(factory)==before
        recovered=resolve(client,body);assert recovered.json()['status']=='not_recorded'
        assert facts(factory)==before
        assert client.post(URL,json=body).status_code==200


def test_real_commit_ack_loss_resolves_without_new_write(grouped,monkeypatch):
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import OperationalError
    app,factory=grouped
    with TestClient(app,raise_server_exceptions=False) as client:
        body=prepared(client,app,factory,'finished');body['sets']=2;real=Session.commit
        def lost(db):real(db);raise OperationalError('synthetic ack loss',{},Exception('lost'))
        with monkeypatch.context() as patch:
            patch.setattr(Session,'commit',lost);first=client.post(URL,json=body)
        assert first.status_code==500 and 'x-production-group-rejected' not in first.headers
        after=facts(factory)
        recovered=resolve(client,body);assert recovered.status_code==200 and recovered.json()['status']=='completed'
        assert facts(factory)==after
        replay=client.post(URL,json=body);assert replay.status_code==200 and replay.json()['group_completion_receipt']==recovered.json()['group_completion_receipt']
        assert facts(factory)==after
        workspace=client.get('/api/production/stock-preparation?workspace=true&state=stock')
        assert any(r.get('entry_type')=='group_stock' for r in workspace.json()['items'])
        archive('committed-ack-loss',client,body,first,recovered,replay)


@pytest.mark.parametrize('value',[True,'1',0,2])
def test_actor_hint_strict_and_mismatch_preserves(grouped,value):
    app,factory=grouped
    with TestClient(app) as client:
        body=prepared(client,app,factory);before=facts(factory);body['expected_actor_id']=value
        first=client.post(URL,json=body);readonly=resolve(client,body)
        assert first.status_code==(409 if value==2 else 422)
        assert readonly.status_code==first.status_code
        if value==2:
            assert first.headers['x-production-group-actor-mismatch']=='1'
            assert 'x-production-group-rejected' not in first.headers
        assert facts(factory)==before


def test_resolve_readonly_after_current_write_eligibility_changed(grouped,monkeypatch):
    from app.services import stock_preparation as service
    from app.models.product import Product
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.warehouse_inventory import WarehouseLocation
    from sqlalchemy.orm import Session
    app,factory=grouped
    with TestClient(app) as client:
        body=prepared(client,app,factory)
        with factory() as db:
            for row in body['jobs']:
                receipt=db.get(IncomingReceiptItem,db.get(Job,row['job_id']).receipt_item_id)
                db.get(StockReplenishmentOrderItem,receipt.stock_replenishment_item_id).customer_id=None
            db.get(WarehouseLocation,7).location_name=''
            db.commit()
        first=client.post(URL,json=body);assert first.status_code==200,first.text
        proof=first.json()['group_completion_receipt']
        assert all(c['source_customer_id'] is None and c['location_name']=='' for c in proof['children'])
        with factory() as db:
            db.get(Product,body['parent_id']).is_active=False
            db.get(WarehouseLocation,7).warehouse_floor=None
            for row in body['jobs']:
                job=db.get(Job,row['job_id']);job.status='cancelled'
                db.get(IncomingReceiptItem,job.receipt_item_id).status='reversed'
            db.commit()
        after=facts(factory)
        def no_write(*args,**kwargs):raise AssertionError('readonly must not flush/commit or call write source')
        original_flush=Session.flush
        def no_business_flush(db,*args,**kwargs):
            assert not db.new and not db.dirty and not db.deleted
            return original_flush(db,*args,**kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(service,'source',no_write);patch.setattr(Session,'flush',no_business_flush);patch.setattr(Session,'commit',no_write)
            recovered=resolve(client,body)
        assert recovered.status_code==200,recovered.text
        assert recovered.json()['group_completion_receipt']==proof and facts(factory)==after
        archive('nullable-and-changed-eligibility',client,body,first,recovered)


def test_old_no_proof_default_builder_and_append_only_trigger(grouped):
    from app.services.stock_preparation_groups import mutate_group,encode
    from app.services.bom_transactions import atomic_bom
    from app.services.stock_preparation_group_recovery import normalized_payload
    from app.models.user import User
    from sqlalchemy.exc import IntegrityError
    app,factory=grouped
    with TestClient(app) as client:
        body=prepared(client,app,factory,'finished')
        with factory() as db:
            with atomic_bom(db):old=mutate_group(db,normalized_payload(GroupAction.model_validate(body)),db.get(User,1))
            db.commit()
            for statement in ('UPDATE stock_preparation_commands SET result_json=result_json','DELETE FROM stock_preparation_commands'):
                with pytest.raises(IntegrityError):db.execute(text(statement))
                db.rollback()
        after=facts(factory);recovered=resolve(client,body)
        assert recovered.status_code==200 and recovered.json()['status']=='legacy_trace'
        assert recovered.json()['group_completion_receipt'] is None and recovered.json()['result']==old
        replay=client.post(URL,json=body);assert replay.status_code==200 and replay.json()['proof_status']=='legacy_trace'
        assert facts(factory)==after
        archive('legacy-no-proof',client,body,replay,recovered,replay)


def test_inflight_not_recorded_then_original_post_completes(grouped,monkeypatch):
    from app.services import stock_preparation_disposition as disposition
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    app,factory=grouped;entered=Event();release=Event();real=disposition.semi_output;calls=[]
    def pause(*args,**kwargs):
        result=real(*args,**kwargs);calls.append(1)
        if len(calls)==1:entered.set();assert release.wait(10)
        return result
    with TestClient(app) as client:
        body=prepared(client,app,factory);monkeypatch.setattr(disposition,'semi_output',pause)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(client.post,URL,json=body)
            try:
                assert entered.wait(10);during=resolve(client,body)
                assert during.status_code==200 and during.json()==dict(status='not_recorded',operation_key=body['operation_key'],current_actor_id=1,group_completion_receipt=None,result=None,trace_url=None)
            finally:release.set()
            first=future.result(timeout=10)
        assert first.status_code==200,first.text
        after=facts(factory);recovered=resolve(client,body);assert recovered.json()['status']=='completed' and facts(factory)==after
        archive('inflight-not-recorded',client,body,first,recovered,duringNotRecorded=dict(status=during.status_code,body=during.json()))


@pytest.mark.parametrize('corruption',['json','null','duplicate','assembly'])
def test_corrupt_parent_insert_is_not_a_completed_result(grouped,corruption):
    from app.services.stock_preparation_group_recovery import normalized_payload
    from app.services.stock_preparation_groups import encode
    app,factory=grouped
    with TestClient(app) as client:
        body=prepared(client,app,factory,'finished')
        result=dict(action='dispose',group_key=body['group_key'],disposition='finished',job_ids=[j['job_id'] for j in body['jobs']])
        if corruption=='duplicate':result['job_ids'].append(result['job_ids'][0])
        elif corruption=='null':result['group_completion_receipt']=None
        elif corruption=='assembly':result['assembly']=dict(action='assemble',group_key=body['group_key'],sets=99)
        with factory() as db:
            anchor=db.get(Job,body['jobs'][0]['job_id']).receipt_item_id
            db.add(Command(operation_key=body['operation_key'],receipt_item_id=anchor,actor_id=1,
                request_json=encode(normalized_payload(GroupAction.model_validate(body))),result_json='{' if corruption=='json' else encode(result)))
            db.commit()
        before=facts(factory);found=resolve(client,body)
        assert found.status_code==409 and found.headers['x-production-group-preserve']=='1'
        assert facts(factory)==before


def test_cookie_actor_switch_and_role_denial_do_not_write(grouped):
    from app.models.user import User
    from app.core.security import hash_password
    app,factory=grouped
    with TestClient(app) as client:
        body=prepared(client,app,factory)
        with factory() as db:
            user=User(username='group-recovery-boss',password_hash=hash_password('SyntheticRolePass123!'),role='boss',real_name='synthetic',display_name='synthetic',must_change_password=False)
            db.add(user);db.commit();other_id=user.id
        client.post('/api/auth/login',json=dict(username='group-recovery-boss',password='SyntheticRolePass123!'))
        before=facts(factory)
        for url,request in ((URL,body),(RESOLVE,dict(operation_key=body['operation_key'],original_request=body,expected_actor_id=other_id))):
            result=client.post(url,json=request)
            assert result.status_code==409 and result.headers['x-production-group-actor-mismatch']=='1'
        assert facts(factory)==before
        with factory() as db:db.get(User,other_id).role='sales';db.commit()
        assert resolve(client,body).status_code==403
        assert facts(factory)==before
        with factory() as db:assert db.scalar(select(OperationLog).where(OperationLog.event_category=='security')) is not None
    with TestClient(app) as anonymous:
        result=resolve(anonymous,body)
        assert result.status_code==401 and result.headers['cache-control']=='no-store'


def test_original_and_current_customer_scope_both_gate_receipt(grouped,monkeypatch):
    from app.api import stock_preparation as api
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from fastapi import HTTPException
    app,factory=grouped
    with TestClient(app) as client:
        body=prepared(client,app,factory);first=client.post(URL,json=body);assert first.status_code==200
        with factory() as db:
            for row in body['jobs']:
                receipt=db.get(IncomingReceiptItem,db.get(Job,row['job_id']).receipt_item_id)
                db.get(StockReplenishmentOrderItem,receipt.stock_replenishment_item_id).customer_id=None
            db.commit()
        calls=[];real=api.require_customer_access
        def check(customer,*args):calls.append(customer);return real(customer,*args)
        monkeypatch.setattr(api,'require_customer_access',check)
        found=resolve(client,body);assert found.status_code==200,found.text
        # Every live nullable source and every original frozen source is checked.
        assert calls.count(None)==6 and calls.count(1)==9
        before=facts(factory)
        def deny(customer,*args):
            if customer is None:raise HTTPException(403,'synthetic current source scope denied')
            return real(customer,*args)
        monkeypatch.setattr(api,'require_customer_access',deny)
        rejected=resolve(client,body)
        assert rejected.status_code==403 and rejected.headers['x-production-group-preserve']=='1' and facts(factory)==before


def test_resolve_wrong_outer_key_and_duplicate_jobs_are_conflicts(grouped):
    app,factory=grouped
    with TestClient(app) as client:
        body=prepared(client,app,factory);before=facts(factory)
        request=dict(operation_key='different-operation-key',original_request=body,expected_actor_id=1)
        assert client.post(RESOLVE,json=request).status_code==409
        duplicate=deepcopy(body);duplicate['jobs'].append(duplicate['jobs'][0])
        assert resolve(client,duplicate).status_code==409
        assert facts(factory)==before
