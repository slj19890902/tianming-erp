"""Standalone assembly HTTP receipts, isolated synthetic ledger and real triggers."""
from copy import deepcopy
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from test_stock_preparation_group_recovery_api import grouped, prepared, facts
from stock_preparation_legacy_fixture import stock_replenishment_app, base_stock_replenishment_app
from test_stock_preparation_groups import prepare, plan, action_body
from app.models.stock_preparation import StockPreparationCommand as Command, StockPreparationJob as Job
from app.models.warehouse_inventory import InventoryLot, InventoryMovement

URL='/api/production/stock-preparation/group-actions'
RESOLVE='/api/production/stock-preparation/group-assembly-result'

def setup(client,app,factory,multi=False):
    if not multi:dispose=prepared(client,app,factory,'semi')
    else:
        real=client.put;split=[]
        def put(url,**kwargs):
            if url.startswith('/api/incoming/receive/sr') and not split:
                split.append(url);p=deepcopy(kwargs['json']);p.update(received_quantity=4,resolution_action='await_supplier',idempotency_key=p['idempotency_key']+'-first')
                assert real(url,json=p).status_code==200
                p.update(received_quantity=26,idempotency_key=p['idempotency_key']+'-rest');p.pop('resolution_action')
                return real(url,json=p)
            return real(url,**kwargs)
        with pytest.MonkeyPatch.context() as patch:patch.setattr(client,'put',put);pid=prepare(app,factory,client)
        planned,_=plan(client,pid);assert planned.status_code==200,planned.text
        dispose=action_body(client,pid,'assembly-recovery-dispose','dispose');dispose.update(disposition='semi',sets=5,expected_actor_id=1)
        for j in dispose['jobs']:j.update(location_id=7,layout_version=1)
    saved=client.post(URL,json=dispose);assert saved.status_code==200,saved.text
    workspace=client.get('/api/production/stock-preparation?workspace=true&state=stock')
    row=next(r for r in workspace.json()['items'] if r['entry_type']=='group_stock')
    body=dict(action='assemble',disposition='finished',parent_id=row['task']['group']['recipe']['parent_id'],sets=2,
        basis_hash='',group_key=row['task']['group']['key'],location_id=7,layout_version=1,confirm_overproduction=False,
        jobs=[dict(job_id=j['id'],job_version=j['version'],lot_version=j['lot_version'],actual_output=j['expected_output'],
                   output_version=j['output_version'] or 0,location_id=7,layout_version=1) for j in row['task']['jobs']],
        operation_key='assembly-recovery-save',expected_actor_id=1)
    client._assembly_workspace_before=workspace;client._assembly_source_row=row
    return body

def resolve(client,body):
    return client.post(RESOLVE,json=dict(operation_key=body['operation_key'],original_request=body,expected_actor_id=1))

def packet(r):return dict(status=r.status_code,body=r.json() if r.headers.get('content-type','').startswith('application/json') else r.text,headers=dict(r.headers))

def archive(name,client,body,first,recovered,replay=None,**extra):
    import os
    from pathlib import Path
    target=os.environ.get('ASSEMBLY_RECOVERY_EXPORT_DIR')
    if not target:return
    data=dict(originalBody=body,actorId=1,operationKey=body['operation_key'],write=packet(first),resolve=packet(recovered),
        workspaceBefore=packet(client._assembly_workspace_before),sourceRow=client._assembly_source_row,
        workspaceAfter=packet(client.get('/api/production/stock-preparation?workspace=true&state=stock')),
        historyTrace=packet(client.get('/api/production/stock-preparation/history/'+body['group_key'])),**extra)
    if replay is not None:data['replay']=packet(replay)
    Path(target).mkdir(parents=True,exist_ok=True)
    Path(target,name+'.json').write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

@pytest.mark.parametrize('sets,multi',[(2,False),(5,False),(1,True)])
def test_real_first_and_readonly_frozen_receipt(grouped,sets,multi):
    app,factory=grouped
    with TestClient(app) as client:
        body=setup(client,app,factory,multi);body['sets']=sets
        first=client.post(URL,json=body);assert first.status_code==200,first.text
        proof=first.json().get('assembly_completion_receipt')
        assert isinstance(proof,dict),'real successful assemble lacks frozen complete receipt'
        assert len(proof['members'])==len(body['jobs']) and sum(i['quantity'] for i in proof['inputs'])==7*sets
        assert sum(m['remaining_stock_quantity'] for m in proof['members'])==35-7*sets
        if multi:
            assert len(proof['members'])==3 and len(proof['inputs'])==2
            unused=next(m for m in proof['members'] if not m['consumed_quantity'])
            assert unused['balance_basis']=='locked_end_balance'
        saved=facts(factory);replay=client.post(URL,json=body);assert replay.json()==first.json()
        recovered=resolve(client,body);assert recovered.status_code==200,recovered.text
        assert recovered.json()['status']=='completed' and recovered.json()['assembly_completion_receipt']==proof
        assert recovered.headers['cache-control']=='no-store' and facts(factory)==saved
        archive('normal-'+str(sets)+('-multi' if multi else ''),client,body,first,recovered,replay)


def test_second_partial_uses_this_movement_and_depleted_take_zero_member(grouped):
    app,factory=grouped
    with TestClient(app) as client:
        body=setup(client,app,factory,True);body['sets']=2
        first=client.post(URL,json=body);assert first.status_code==200,first.text
        current=client.get('/api/production/stock-preparation?workspace=true&state=stock')
        row=next(r for r in current.json()['items'] if r['entry_type']=='group_stock')
        second=deepcopy(body);second['operation_key']='assembly-recovery-second'
        for item in second['jobs']:
            job=next(j for j in row['task']['jobs'] if j['id']==item['job_id']);item['output_version']=job['output_version']
        client._assembly_workspace_before=current;client._assembly_source_row=row
        saved=client.post(URL,json=second);assert saved.status_code==200,saved.text
        proof=saved.json()['assembly_completion_receipt']
        assert sum(m['remaining_stock_quantity'] for m in proof['members'])==7
        assert sorted((i['before_available'],i['quantity'],i['after_available']) for i in proof['inputs'])==[(9,6,3),(12,8,4)]
        zero=next(m for m in proof['members'] if not m['consumed_quantity'])
        assert zero['remaining_stock_quantity']==0 and zero['balance_basis']=='locked_end_balance'
        assert next(j for j in row['task']['jobs'] if j['id']==zero['job_id'])['output_locations']==[]
        after=facts(factory);recovered=resolve(client,second);assert recovered.json()['assembly_completion_receipt']==proof
        assert resolve(client,body).json()['assembly_completion_receipt']==first.json()['assembly_completion_receipt']
        replay=client.post(URL,json=second);assert replay.json()==saved.json() and facts(factory)==after
        archive('second-partial-depleted-member',client,second,saved,recovered,replay,firstOriginalBody=body,firstWrite=packet(first))


def test_ignored_fields_defaults_and_signature_remain_compatible(grouped):
    from app.api.stock_preparation import GroupAction
    from app.services.stock_preparation_groups import encode
    from app.services.stock_preparation_assembly_recovery import normalized_payload
    from app.models.product_bom import ProductBomComponent
    app,factory=grouped
    with TestClient(app) as client:
        body=setup(client,app,factory);body['disposition']='semi';body['jobs'].reverse()
        for row in body['jobs']:row.update(actual_output=0,lot_version=999,location_id=None,layout_version=None,lot_id=None)
        with factory() as db:
            for edge in db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==body['parent_id'])):edge.quantity_per_set=99
            db.commit()
        first=client.post(URL,json=body);assert first.status_code==200,first.text
        proof=first.json()['assembly_completion_receipt'];assert proof['sets']==2 and sorted(c['per_set'] for c in proof['recipe']['children'])==[3,4]
        with factory() as db:assert db.get(Command,body['operation_key']).request_json==encode(normalized_payload(GroupAction.model_validate(body)))
        after=facts(factory)
        for field in ('actual_output','lot_version','location_id'):
            changed=deepcopy(body);changed['jobs'][0][field]=1
            assert client.post(URL,json=changed).status_code==409 and resolve(client,changed).status_code==409
        reverse=deepcopy(body);reverse['jobs'].reverse();assert resolve(client,reverse).status_code==409
        assert facts(factory)==after
        recovered=resolve(client,body);replay=client.post(URL,json=body);assert replay.json()==first.json()
        archive('ignored-fields-compatible',client,body,first,recovered,replay)


@pytest.mark.parametrize('fault',['builder','enroll','cost'])
def test_same_transaction_failures_and_fresh_rejected(grouped,monkeypatch,fault):
    from app.services import stock_preparation_assembly_recovery as recovery,shared_bom_stock
    app,factory=grouped;observed=[]
    with TestClient(app,raise_server_exceptions=False) as client:
        body=setup(client,app,factory)
        if fault=='cost':
            with factory() as db:
                last=db.scalar(select(Job).order_by(Job.id.desc()));db.get(InventoryLot,last.output_lot_id).estimated_unit_cost_snapshot=None;db.commit()
        before=facts(factory)
        def builder(*args):raise RuntimeError('synthetic builder fault')
        def enroll(db,lot,**kwargs):
            command=db.get(Command,body['operation_key']);assert command is not None
            assert json.loads(command.result_json)['assembly_completion_receipt']['output_lot_id']==lot.id
            observed.append(lot.id);raise RuntimeError('synthetic post-final-command enrolment fault')
        with monkeypatch.context() as patch:
            if fault=='builder':patch.setattr(recovery,'build_receipt',builder)
            elif fault=='enroll':patch.setattr(shared_bom_stock,'enroll_completed_bom',enroll)
            failed=client.post(URL,json=body)
        assert failed.status_code==(409 if fault=='cost' else 500) and facts(factory)==before
        if fault=='cost':
            assert failed.headers.get('x-production-assembly-rejected')=='1'
            assert 'x-production-assembly-preserve' not in failed.headers
        else:
            assert failed.headers.get('x-production-assembly-preserve')=='1'
            assert 'x-production-assembly-rejected' not in failed.headers
        if fault=='enroll':assert observed
        missing=resolve(client,body);assert missing.json()['status']=='not_recorded' and facts(factory)==before
        archive('rollback-'+fault,client,body,failed,missing,beforeCounts={k:len(v) for k,v in before.items()},afterCounts={k:len(v) for k,v in facts(factory).items()})


def test_real_commit_ack_loss_readonly_no_business_dml(grouped,monkeypatch):
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import OperationalError
    from sqlalchemy import event
    app,factory=grouped
    with TestClient(app,raise_server_exceptions=False) as client:
        body=setup(client,app,factory);real=Session.commit
        def lose(db):real(db);raise OperationalError('synthetic true commit ack loss',{},Exception('lost'))
        with monkeypatch.context() as patch:patch.setattr(Session,'commit',lose);lost=client.post(URL,json=body)
        assert lost.status_code==500 and 'x-production-assembly-rejected' not in lost.headers
        saved=facts(factory);sql=[]
        with factory() as db:engine=db.bind
        def observe(conn,cursor,statement,*args):sql.append(statement.strip().split()[0].upper())
        def no_commit(*args):raise AssertionError('readonly must not commit')
        event.listen(engine,'before_cursor_execute',observe)
        try:
            with monkeypatch.context() as patch:patch.setattr(Session,'commit',no_commit);recovered=resolve(client,body)
        finally:event.remove(engine,'before_cursor_execute',observe)
        assert recovered.status_code==200 and recovered.json()['status']=='completed'
        assert not {'INSERT','UPDATE','DELETE'}.intersection(sql) and facts(factory)==saved
        replay=client.post(URL,json=body);assert replay.status_code==200 and facts(factory)==saved
        archive('committed-ack-loss',client,body,lost,recovered,replay,readonlySqlKinds=sql)


def test_inflight_not_recorded_then_original_completes(grouped,monkeypatch):
    from app.services import stock_preparation_disposition as disposition
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    app,factory=grouped;entered=Event();release=Event();real=disposition.manual_finished_in
    def pause(*args,**kwargs):
        output=real(*args,**kwargs);entered.set();assert release.wait(10);return output
    with TestClient(app) as client:
        body=setup(client,app,factory);monkeypatch.setattr(disposition,'manual_finished_in',pause)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(client.post,URL,json=body)
            try:
                assert entered.wait(10);during=resolve(client,body)
                assert during.status_code==200 and during.json()==dict(status='not_recorded',operation_key=body['operation_key'],current_actor_id=1,assembly_completion_receipt=None,result=None,trace_url=None)
            finally:release.set()
            first=future.result(timeout=10)
        assert first.status_code==200,first.text
        after=facts(factory);recovered=resolve(client,body);assert recovered.json()['status']=='completed' and facts(factory)==after
        archive('inflight-not-recorded',client,body,first,recovered,duringNotRecorded=packet(during))


def test_legacy_default_builder_and_append_only(grouped):
    from app.services.stock_preparation_groups import mutate_group
    from app.services.stock_preparation_assembly_recovery import normalized_payload
    from app.services.bom_transactions import atomic_bom
    from app.api.stock_preparation import GroupAction
    from app.models.user import User
    from sqlalchemy.exc import IntegrityError
    app,factory=grouped
    with TestClient(app) as client:
        body=setup(client,app,factory)
        with factory() as db:
            with atomic_bom(db):old=mutate_group(db,normalized_payload(GroupAction.model_validate(body)),db.get(User,1))
            db.commit()
            for query in ('UPDATE stock_preparation_commands SET result_json=result_json','DELETE FROM stock_preparation_commands'):
                with pytest.raises(IntegrityError):db.execute(text(query))
                db.rollback()
        saved=facts(factory);found=resolve(client,body);assert found.status_code==200,found.text
        assert found.json()['status']=='legacy_trace' and found.json()['assembly_completion_receipt'] is None and found.json()['result']==old
        replay=client.post(URL,json=body);assert replay.json()['proof_status']=='legacy_trace' and facts(factory)==saved
        archive('legacy-no-proof',client,body,replay,found,replay)


def test_nullable_and_changed_eligibility_resolve_and_replay(grouped,monkeypatch):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.product import Product
    from app.models.warehouse_inventory import WarehouseLocation
    from app.services import stock_preparation as prep
    app,factory=grouped
    with TestClient(app) as client:
        body=setup(client,app,factory)
        with factory() as db:
            for item in body['jobs']:
                job=db.get(Job,item['job_id']);receipt=db.get(IncomingReceiptItem,job.receipt_item_id)
                db.get(StockReplenishmentOrderItem,receipt.stock_replenishment_item_id).customer_id=None
            db.get(WarehouseLocation,7).location_name='';db.commit()
        first=client.post(URL,json=body);assert first.status_code==200,first.text
        proof=first.json()['assembly_completion_receipt'];assert all(m['source_customer_id'] is None for m in proof['members'])
        assert proof['location_name']==''
        with factory() as db:
            db.get(Product,body['parent_id']).is_active=False;db.get(WarehouseLocation,7).warehouse_floor=None
            for item in body['jobs']:
                job=db.get(Job,item['job_id']);job.status='cancelled';db.get(IncomingReceiptItem,job.receipt_item_id).status='reversed'
            db.commit()
        saved=facts(factory)
        def no_source(*args):raise AssertionError('persisted result must not re-run source write qualification')
        with monkeypatch.context() as patch:
            patch.setattr(prep,'source',no_source);found=resolve(client,body);replay=client.post(URL,json=body)
        assert found.status_code==200 and found.json()['assembly_completion_receipt']==proof
        assert replay.json()==first.json() and facts(factory)==saved
        archive('nullable-changed-eligibility',client,body,first,found,replay)


@pytest.mark.parametrize('hint',[True,'1',0,2])
def test_strict_actor_and_body_identity(grouped,hint):
    app,factory=grouped
    with TestClient(app) as client:
        body=setup(client,app,factory);body['expected_actor_id']=hint;saved=facts(factory)
        first=client.post(URL,json=body);found=resolve(client,body)
        assert first.status_code==(409 if hint==2 else 422) and found.status_code==first.status_code
        if hint==2:assert first.headers['x-production-assembly-actor-mismatch']=='1' and 'x-production-assembly-rejected' not in first.headers
        assert facts(factory)==saved


def test_fresh_stale_rejected_and_existing_conflict_preserved(grouped):
    app,factory=grouped
    with TestClient(app) as client:
        body=setup(client,app,factory);stale=deepcopy(body);stale['jobs'][0]['output_version']+=100;saved=facts(factory)
        failed=client.post(URL,json=stale)
        assert failed.status_code==409 and failed.headers.get('x-production-assembly-rejected')=='1'
        assert 'x-production-assembly-preserve' not in failed.headers and facts(factory)==saved
        found=resolve(client,stale);assert found.json()['status']=='not_recorded'
        corrected=client.post(URL,json=body);assert corrected.status_code==200,corrected.text
        after=facts(factory);conflict=client.post(URL,json=stale)
        assert conflict.status_code==409 and conflict.headers.get('x-production-assembly-preserve')=='1'
        assert 'x-production-assembly-rejected' not in conflict.headers and facts(factory)==after
        archive('fresh-rejected',client,stale,failed,found,beforeCounts={k:len(v) for k,v in saved.items()},afterRejectionCounts={k:len(v) for k,v in saved.items()},correctedBody=body,correctedWrite=packet(corrected),existingConflict=packet(conflict))


def test_fresh_job_drift_before_lock_is_read_inside_lock(grouped,monkeypatch):
    from contextlib import contextmanager
    from app.api import stock_preparation as api
    app,factory=grouped;real=api.atomic_bom;changed=[]
    with TestClient(app) as client:
        body=setup(client,app,factory)
        @contextmanager
        def drift(db):
            if not changed:
                with factory() as other:
                    job=other.get(Job,body['jobs'][0]['job_id']);job.version+=1;other.commit()
                changed.append(facts(factory))
            with real(db):yield
        with monkeypatch.context() as patch:
            patch.setattr(api,'atomic_bom',drift);first=client.post(URL,json=body)
        assert first.status_code==409,first.text
        assert facts(factory)==changed[0]


def test_cookie_actor_role_and_anonymous_denials_preserve_business_facts(grouped):
    from app.models.user import User
    from app.models.audit import OperationLog
    from app.core.security import hash_password
    app,factory=grouped
    with TestClient(app) as client:
        body=setup(client,app,factory)
        with factory() as db:
            user=User(username='assembly-other-boss',password_hash=hash_password('SyntheticRolePass123!'),role='boss',real_name='synthetic',display_name='synthetic',must_change_password=False)
            db.add(user);db.commit();other_id=user.id
        client.post('/api/auth/login',json=dict(username='assembly-other-boss',password='SyntheticRolePass123!'))
        before=facts(factory)
        for url,request in ((URL,body),(RESOLVE,dict(operation_key=body['operation_key'],original_request=body,expected_actor_id=other_id))):
            response=client.post(url,json=request);assert response.status_code==409 and response.headers['x-production-assembly-actor-mismatch']=='1'
        assert facts(factory)==before
        with factory() as db:db.get(User,other_id).role='sales';db.commit()
        assert resolve(client,body).status_code==403 and facts(factory)==before
        with factory() as db:assert db.scalar(select(OperationLog.id).where(OperationLog.event_category=='security'))
    with TestClient(app) as anonymous:assert resolve(anonymous,body).status_code==401


def test_current_and_original_customer_scopes_are_both_checked(grouped,monkeypatch):
    from app.api import stock_preparation as api
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from fastapi import HTTPException
    app,factory=grouped
    with TestClient(app) as client:
        body=setup(client,app,factory);assert client.post(URL,json=body).status_code==200
        with factory() as db:
            for row in body['jobs']:
                receipt=db.get(IncomingReceiptItem,db.get(Job,row['job_id']).receipt_item_id)
                db.get(StockReplenishmentOrderItem,receipt.stock_replenishment_item_id).customer_id=None
            db.commit()
        calls=[];real=api.require_customer_access
        def collect(customer,*args):calls.append(customer);return real(customer,*args)
        with monkeypatch.context() as patch:patch.setattr(api,'require_customer_access',collect);found=resolve(client,body)
        assert found.status_code==200 and None in calls and 1 in calls
        # Current source and original frozen source each retain independent gates.
        for denied in (None,1):
            def deny(customer,*args):
                if customer==denied:raise HTTPException(403,'synthetic scope denial')
                return real(customer,*args)
            before=facts(factory)
            with monkeypatch.context() as patch:
                patch.setattr(api,'require_customer_access',deny);result=resolve(client,body)
            assert result.status_code==403 and result.headers['x-production-assembly-preserve']=='1' and facts(factory)==before


def test_readonly_outer_key_duplicates_and_old_owner(grouped):
    app,factory=grouped
    with TestClient(app) as client:
        body=setup(client,app,factory);before=facts(factory)
        assert client.post(RESOLVE,json=dict(operation_key='different-key-here',original_request=body,expected_actor_id=1)).status_code==409
        duplicate=deepcopy(body);duplicate['jobs'].append(duplicate['jobs'][0]);assert resolve(client,duplicate).status_code==409
        assert facts(factory)==before
        # Old clients omit actor hint; the authoritative Command actor is still checked.
        body.pop('expected_actor_id');saved=client.post(URL,json=body);assert saved.status_code==200,saved.text
        assert resolve(client,body).json()['status']=='completed'


@pytest.mark.parametrize('corruption',['null','duplicate','movement'])
def test_corrupted_proof_cannot_be_committed_as_complete(grouped,monkeypatch,corruption):
    from app.services import stock_preparation_assembly_recovery as recovery
    app,factory=grouped;real=recovery.build_receipt
    def bad(*args):
        result=real(*args)
        if corruption=='null':result['assembly_completion_receipt']=None
        elif corruption=='duplicate':result['assembly_completion_receipt']['members'].append(result['assembly_completion_receipt']['members'][0])
        else:result['assembly_completion_receipt']['output_movement_id']=999999
        return result
    with TestClient(app,raise_server_exceptions=False) as client:
        body=setup(client,app,factory);before=facts(factory)
        with monkeypatch.context() as patch:patch.setattr(recovery,'build_receipt',bad);failed=client.post(URL,json=body)
        assert failed.status_code==409 and facts(factory)==before
        assert resolve(client,body).json()['status']=='not_recorded'
