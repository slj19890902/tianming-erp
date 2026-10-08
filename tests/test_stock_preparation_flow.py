import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from stock_preparation_legacy_fixture import stock_replenishment_app, base_stock_replenishment_app
from test_stock_replenishment_flow import _login, _customer_replenishment_payload
from app.api.stock_preparation import router
from app.models.stock_preparation import StockPreparationJob, StockPreparationCommand
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.models.incoming_receipt import IncomingReceiptItem


def setup(client):
    _login(client)
    payload=_customer_replenishment_payload(quantity=30)
    payload['idempotency_key']='prep-test-demand'
    created=client.post('/api/requisition/stock-replenishment/orders',json=payload)
    assert created.status_code==201,created.text
    item=created.json()['items'][0]
    # R05: a demand draft is not a purchase. Confirm through the real pending workflow.
    pending=client.get('/api/requisition/pending')
    assert pending.status_code==200,pending.text
    row=next(r for r in pending.json()['items'] if r.get('stock_replenishment_item_id')==item['id'])
    purchase=client.post('/api/requisition/supplier-orders/from-pending-selection',json={
        'supplier_groups':[{'supplier_name':row['supplier_name'],'request_key':'prep-test-purchase',
            'stock_sources':[{'stock_replenishment_item_id':item['id'],
                              'source_fingerprint':row['source_fingerprint']}]}]})
    assert purchase.status_code==201,purchase.text
    before=client.get('/api/production/stock-preparation').json()
    assert before['counts']['waiting']==1
    received=client.put(f"/api/incoming/receive/sr{item['id']}",json={'received_quantity':20,'idempotency_key':'prep-test-receive','resolution_action':'await_supplier'})
    assert received.status_code==200,received.text
    rows=client.get('/api/production/stock-preparation').json()
    assert rows['counts']['waiting']==1 and rows['counts']['arrange']==1
    return next(row for row in rows['items'] if row['receipt_item_id'])


def send(client,row,action,**kw):
    payload=dict(action=action,operation_key=f'test-{action}-abcdefgh',lot_version=row['lot_version'],**kw)
    return client.post(f"/api/production/stock-preparation/{row['receipt_item_id']}/actions",json=payload)


def refresh(client):
    return next(r for r in client.get('/api/production/stock-preparation').json()['items'] if r['receipt_item_id'])


def test_partial_production_replay_and_stock_conservation(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        assert client.get('/api/production/stock-preparation').status_code==401
        row=setup(client)
        assert row['available']==20
        response=send(client,row,'plan',quantity=8)
        assert response.status_code==200,response.text
        assert send(client,row,'plan',quantity=8).json()==response.json()
        assert send(client,row,'plan',quantity=9).status_code==409
        row=refresh(client); assert (row['available'],row['reserved'])==(12,8)
        job=row['jobs'][0]
        response=send(client,row,'complete',job_id=job['id'],job_version=job['version'],actual_output=job['expected_output'],location_id=7,layout_version=1)
        assert response.status_code==200,response.text
        assert send(client,row,'complete',job_id=job['id'],job_version=job['version'],actual_output=job['expected_output'],location_id=7,layout_version=1).status_code==200
        row=refresh(client);assert row['available']==12 and row['reserved']==0
        assert row['jobs'][0]['actual_output']==job['expected_output']
        assert send(client,row,'keep_semi').status_code==200
        row=refresh(client);assert row['status']=='keep' and row['available']==12
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(StockPreparationJob))==1
        receipt=db.get(IncomingReceiptItem,row['receipt_item_id']);lot=db.get(InventoryLot,receipt.received_inventory_lot_id)
        assert lot.quantity_consumed==8
        assert db.scalar(select(func.count()).select_from(StockPreparationCommand))==3


def test_missing_input_cost_cannot_create_new_priceless_output(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        row=setup(client)
        assert send(client,row,'plan',quantity=8).status_code==200
        row=refresh(client);job=row['jobs'][0]
        with factory() as db:
            stored_job=db.get(StockPreparationJob,job['id'])
            reservation=db.get(InventoryReservation,stored_job.reservation_id)
            lot=db.get(InventoryLot,reservation.inventory_lot_id)
            lot.estimated_unit_cost_snapshot=None
            db.commit()
            count=db.scalar(select(func.count()).select_from(InventoryLot))
        response=send(client,row,'complete',job_id=job['id'],job_version=job['version'],actual_output=job['expected_output'],location_id=7,layout_version=1)
        assert response.status_code==409,response.text
        assert '成本' in response.json()['detail']
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(InventoryLot))==count
            assert db.get(StockPreparationJob,job['id']).status=='pending'
            receipt=db.get(IncomingReceiptItem,row['receipt_item_id']);lot=db.get(InventoryLot,receipt.received_inventory_lot_id)
            assert lot.quantity_consumed==0 and lot.quantity_reserved==8


def test_cancel_stale_overproduction_and_atomic_failure(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app,raise_server_exceptions=False) as client:
        row=setup(client)
        assert send(client,row,'plan',quantity=21).status_code==409
        assert send(client,row,'plan',quantity=8).status_code==200
        assert send(client,row,'keep_raw').status_code==409
        row=refresh(client);job=row['jobs'][0]
        assert send(client,row,'complete',job_id=job['id'],job_version=job['version'],actual_output=999,location_id=7,layout_version=1).status_code==409
        import app.services.stock_preparation as service
        original=service.append_audit_event
        def fault(*args,**kwargs): raise RuntimeError('injected audit fault')
        monkeypatch.setattr(service,'append_audit_event',fault)
        response=send(client,row,'complete',job_id=job['id'],job_version=job['version'],actual_output=job['expected_output'],location_id=7,layout_version=1)
        assert response.status_code==500
        after=refresh(client);assert after['reserved']==8 and after['jobs'][0]['status']=='pending'
        monkeypatch.setattr(service,'append_audit_event',original)
        assert send(client,row,'cancel',job_id=job['id'],job_version=job['version']).status_code==200
        row=refresh(client);assert row['reserved']==0 and row['available']==20
        assert send(client,row,'keep_raw').status_code==200
        assert refresh(client)['status']=='keep'


def test_scope_and_admin_only(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        row=setup(client)
        from app.api import stock_preparation as api
        monkeypatch.setattr(api,'has_unrestricted_customer_access',lambda *args:False)
        monkeypatch.setattr(api,'customer_scope_ids',lambda *args:[])
        assert client.get('/api/production/stock-preparation').json()['total']==0
        from app.models.user import User
        with factory() as db:
            db.get(User,1).role='workshop';db.commit()
        assert send(client,row,'plan',quantity=1).status_code==403
