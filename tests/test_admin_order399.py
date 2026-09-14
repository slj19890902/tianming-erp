import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, event
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact
from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources, _receive
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.multilevel_bom import BomAssembly


def seed(client, factory):
    mid, snapshots = seed_graph(factory)
    for index, source in enumerate(purchase_sources(client, factory, mid, snapshots)):
        fact = _freeze_receipt_fact(client, source, idempotency_key=f'admin399-price-{index}', unit_price='0.1234')
        assert fact.status_code==200, fact.text
        result = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty, idempotency_key=f'admin399-receipt-{index}')
        assert result.status_code==200, result.text
    return mid, snapshots


@pytest.mark.parametrize('mode',['delete_trial','withdraw','keep_stock'])
def test_self_service_atomic_preview_and_execute(composite_requisition_app,_p181_published_map_identity,monkeypatch,mode):
    app, factory = composite_requisition_app
    with TestClient(app) as client:
        _login(client)
        mid, snapshots = seed(client,factory)
        body = dict(order_ids=[1],mode=mode)
        def read_only(conn,cursor,statement,params,ctx,many):
            assert not statement.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','REPLACE'))
        event.listen(factory.kw['bind'],'before_cursor_execute',read_only)
        plan = client.post('/api/orders/admin-disposition/preview',json=body)
        event.remove(factory.kw['bind'],'before_cursor_execute',read_only)
        assert plan.status_code==200,plan.text
        assert not plan.json()['blockers'],plan.text
        payload={**body,'reviewed_hash':plan.json()['reviewed_hash'],'operation_key':'test-admin399-execute','trial_confirmed':True}
        if mode=='delete_trial':
            assert client.post('/api/orders/admin-disposition/execute',json={**payload,'trial_confirmed':False}).status_code==409
        assert client.post('/api/orders/admin-disposition/execute',json={**payload,'reviewed_hash':'a'*64}).status_code==409
        with factory() as db:
            db.get(User,1).role='sales';db.commit()
        assert client.post('/api/orders/admin-disposition/execute',json=payload).status_code==403
        with factory() as db:
            db.get(User,1).role='admin';db.commit()
        from app.services import audit_log
        from app.services.bom_subkits import SubkitError
        original=audit_log.append_audit_event
        with monkeypatch.context() as patch:
            def fail_audit(*args,**kwargs):
                if kwargs.get('action_code')=='order.admin_disposition':
                    raise SubkitError('模拟审计失败')
                return original(*args,**kwargs)
            patch.setattr(audit_log,'append_audit_event',fail_audit)
            result=client.post('/api/orders/admin-disposition/execute',json=payload)
            assert result.status_code==409 and '模拟审计失败' in result.text,result.text
        with factory() as db:
            assert all(r.status=='posted' for r in db.scalars(select(IncomingReceiptItem)))
            assert all(r.status=='posted' for r in db.scalars(select(BomAssembly)))
            assert db.get(Order,1).status!='cancelled'
        result=client.post('/api/orders/admin-disposition/execute',json=payload)
        assert result.status_code==200,result.text
        retry=client.post('/api/orders/admin-disposition/execute',json=payload)
        assert retry.status_code==200 and retry.json()==result.json(),retry.text
        changed=client.post('/api/orders/admin-disposition/execute',json={**payload,'reason':'不同内容'})
        assert changed.status_code==409,changed.text
        with factory() as db:
            assert db.get(Order,1).status==('pending_production' if mode=='withdraw' else 'cancelled')
            if mode=='keep_stock':
                output=db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type=='bom_assembly'))
                assert output.quantity_available==10 and output.quantity_reserved==0
                assert all(r.status=='posted' for r in db.scalars(select(IncomingReceiptItem)))
            else:
                assert all(r.status=='reversed' for r in db.scalars(select(IncomingReceiptItem)))
                assert all(r.status=='reversed' for r in db.scalars(select(BomAssembly)))
                assert all(r.quantity_available+r.quantity_reserved==0 for r in db.scalars(select(InventoryLot)))
            if mode=='withdraw':
                assert db.get(OrderItem,1).requisition_status=='未报料'
        if mode=='withdraw':
            from tests.test_n039_composite_bom_requisition import _component_payload
            saved=client.post('/api/requisition/batches',json={'request_key':'after-admin-withdraw',
                'supplier_name':'苏州纸板供应商','items':[{**_component_payload(sid),'component_type':'whole',
                'order_item_id':1,'special_process':'一开一'} for sid,pid in snapshots]})
            assert saved.status_code==201,saved.text
        else:
            assert client.get('/api/production/pending-assemblies').json()['total']==0


def test_group_rollback_and_changed_stock(composite_requisition_app,_p181_published_map_identity,monkeypatch):
    app,factory=composite_requisition_app
    with TestClient(app) as client:
        _login(client);seed(client,factory)
        with factory() as db:
            order=db.get(Order,1);order.customer_po='ADMIN-GROUP'
            db.add(Order(id=2,order_number='ADMIN-SECOND',customer_id=1,customer_po='ADMIN-GROUP',order_date=order.order_date))
            db.commit()
        body={'order_ids':[1,2],'mode':'delete_trial'}
        plan=client.post('/api/orders/admin-disposition/preview',json=body).json()
        payload={**body,'reviewed_hash':plan['reviewed_hash'],'operation_key':'admin-group-atomic','trial_confirmed':True}
        from app.services import audit_log
        from app.services.bom_subkits import SubkitError
        original=audit_log.append_audit_event
        with monkeypatch.context() as patch:
            def fail(*args,**kwargs):
                if kwargs.get('action_code')=='order.admin_disposition':
                    db=args[0]
                    assert db.get(Order,1).status==db.get(Order,2).status=='cancelled'
                    raise SubkitError('整组审计故障')
                return original(*args,**kwargs)
            patch.setattr(audit_log,'append_audit_event',fail)
            result=client.post('/api/orders/admin-disposition/execute',json=payload)
            assert result.status_code==409 and '整组审计故障' in result.text,result.text
        with factory() as db:
            assert db.get(Order,1).status!='cancelled' and db.get(Order,2).status!='cancelled'
            lot=db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type=='bom_assembly'))
            lot.version+=1;db.commit()
        assert client.post('/api/orders/admin-disposition/execute',json=payload).status_code==409
        plan=client.post('/api/orders/admin-disposition/preview',json=body).json()
        payload['reviewed_hash']=plan['reviewed_hash']
        # A source quantity cannot be erased merely because an administrator asks.
        with factory() as db:
            lot=db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type=='bom_assembly'))
            lot.quantity_reserved-=1;lot.quantity_consumed+=1;lot.version+=1;db.commit()
        payload['reviewed_hash']=client.post('/api/orders/admin-disposition/preview',json=body).json()['reviewed_hash']
        assert client.post('/api/orders/admin-disposition/execute',json=payload).status_code==409
        with factory() as db:
            assert all(r.status=='posted' for r in db.scalars(select(IncomingReceiptItem)))
            assert db.get(Order,2).status!='cancelled'
