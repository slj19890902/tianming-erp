import copy
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_stock_replenishment_flow import stock_replenishment_app
from test_stock_preparation_groups import prepare, plan, action_body
from app.api.stock_preparation import router
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.models.user import User
from app.services.stock_preparation_history import rows, combined_page


def completed(app,factory,client):
    pid=prepare(app,factory,client)
    response,_=plan(client,pid);assert response.status_code==200
    body=action_body(client,pid,'history-complete-key','complete')
    assert client.post('/api/production/stock-preparation/group-actions',json=body).status_code==200
    with factory() as db:
        history=rows(db)
    assert len(history)==1
    row=history[0]
    payload=dict(operation_key='reverse-history-key',confirm_unused=True,jobs=row['reverse_versions'])
    return row,payload


def test_group_history_and_safe_reversal_keep_source_facts(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        row,payload=completed(app,factory,client)
        assert row['actual_output_quantity']==5 and row['output_unit']=='套' and row['can_revert']
        trace=client.get('/api/production/stock-preparation/history/'+row['preparation_key']).json()['items']
        assert len(trace)==2 and all(r['source_trace']['received_quantity']==30 for r in trace)
        assert all(r['source_trace']['received_at'].endswith('Z') for r in trace)
        url='/api/production/stock-preparation/completions/'+row['preparation_key']+'/revert'
        assert client.post(url,json=dict(payload,confirm_unused=False)).status_code==409
        assert client.post(url,json=dict(payload,jobs=payload['jobs'][:1])).status_code==409
        stale=copy.deepcopy(payload);stale['jobs'][0]['output_version']+=1
        assert client.post(url,json=stale).status_code==409
        result=client.post(url,json=payload);assert result.status_code==200,result.text
        assert client.post(url,json=payload).json()==result.json()
        assert client.post(url,json=dict(payload,confirm_unused=False)).status_code==409
        with factory() as db:
            history=rows(db)
            assert len(history)==1 and history[0]['status']=='reversed' and history[0]['actual_output_quantity']==5
            for job in db.scalars(select(Job)):
                assert job.status=='cancelled' and job.actual_output>0
                assert db.get(InventoryLot,job.output_lot_id).quantity_available==0
                from app.services.stock_preparation import source
                receipt,_,lot=source(db,job.receipt_item_id)
                assert receipt.received_quantity==30 and lot.quantity_available==30 and lot.quantity_consumed==0
            page,total=combined_page(db,allowed_customer_ids=None,page=1,page_size=1)
            assert total==1 and page[0]['id']==row['id']
            page,total=combined_page(db,allowed_customer_ids=set(),page=1,page_size=1)
            assert total==0 and not page
            page,total=combined_page(db,allowed_customer_ids=None,page=1,page_size=1,product_code='missing')
            assert total==0 and not page


def test_reversal_atomic_failure_and_downstream_block(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app,raise_server_exceptions=False) as client:
        row,payload=completed(app,factory,client)
        url='/api/production/stock-preparation/completions/'+row['preparation_key']+'/revert'
        import app.services.stock_preparation_history as service
        def fail(*a,**kw):raise RuntimeError('audit unavailable')
        monkeypatch.setattr(service,'append_audit_event',fail)
        assert client.post(url,json=payload).status_code==500
        with factory() as db:
            assert all(j.status=='completed' for j in db.scalars(select(Job)))
            assert db.get(Command,payload['operation_key']) is None
            job=db.scalars(select(Job)).first();lot=db.get(InventoryLot,job.output_lot_id)
            assert lot.quantity_available==job.actual_output
            lot.quantity_available-=1;lot.quantity_consumed+=1;db.commit()
        assert client.post(url,json=payload).status_code==409
        with factory() as db:
            assert '使用' in rows(db)[0]['reversal_block']
            user=db.scalars(select(User)).first();user.role='sales';user.customer_access_mode='selected';db.commit()
        assert client.get('/api/production/stock-preparation/history/'+row['preparation_key']).status_code==403
        assert client.post(url,json=payload).status_code==403


from test_p1_06_production_history_pagination import production_history_app, _login


def test_combined_history_interleaves_order_rows_without_changing_legacy(production_history_app, monkeypatch):
    app, ids = production_history_app
    import app.services.stock_preparation_history as service
    with TestClient(app) as client:
        _login(client, 'p106-admin')
        url='/api/production/completions'
        legacy=client.get(url+'?page=1&page_size=50').json()
        assert client.get(url+'?include_stock=true&page=1&page_size=50').json()==legacy
        monkeypatch.setattr(service,'rows',lambda *a,**kw:[dict(id='prep:test',completed_at='2026-07-29T00:00:00Z')])
        merged=client.get(url+'?include_stock=true&page=1&page_size=2').json()
        second=client.get(url+'?include_stock=true&page=2&page_size=2').json()
        assert merged['total']==second['total']==4
        assert [r['id'] for r in merged['items']+second['items']]==[ids['b'],'prep:test',ids['a_new'],ids['a_old']]
        assert client.get(url+'?page=1&page_size=50').json()==legacy
