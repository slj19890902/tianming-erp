import copy
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_stock_replenishment_flow import stock_replenishment_app, _login, _customer_replenishment_payload
from app.api.stock_preparation import router
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.stock_preparation import StockPreparationJob, StockPreparationCommand
from app.models.warehouse_inventory import InventoryLot


def prepare(app,factory,client):
    with factory() as db:
        original=db.get(Product,1)
        values={column.name:getattr(original,column.name) for column in Product.__table__.columns if column.name not in {'id','product_code','customer_material_code'}}
        second=Product(**dict(values,product_code='KIT-SHORT',customer_material_code='KIT-SHORT'))
        parent=Product(**dict(values,product_code='KIT-PARENT',customer_material_code='KIT-PARENT',is_composite=True,is_virtual_composite_parent=True))
        db.add_all([second,parent]);db.flush()
        for index,(child,count) in enumerate([(original,3),(second,4)],1):
            db.add(ProductBomComponent(parent_product_id=parent.id,component_product_id=child.id,quantity_per_set=count,display_order=index,internal_component_code=f'PART-{index}',is_required=True))
        parent_id=parent.id;second_id=second.id;db.commit()
    _login(client)
    payload=_customer_replenishment_payload(30)
    payload['idempotency_key']='stock-preparation-group-draft'
    second=copy.deepcopy(payload['items'][0]);second['product_id']=second_id;payload['items'].append(second)
    response=client.post('/api/requisition/stock-replenishment/orders',json=payload);assert response.status_code==201,response.text
    created_items=response.json()['items']
    pending=client.get('/api/requisition/pending');assert pending.status_code==200,pending.text
    pending_rows=[row for row in pending.json()['items'] if row.get('stock_replenishment_item_id') in {item['id'] for item in created_items}]
    groups={}
    for row in pending_rows:
        groups.setdefault(row['supplier_name'],[]).append({
            'stock_replenishment_item_id':row['stock_replenishment_item_id'],
            'source_fingerprint':row['source_fingerprint'],
        })
    purchase=client.post('/api/requisition/supplier-orders/from-pending-selection',json={
        'supplier_groups':[{'supplier_name':name,'request_key':f'stock-preparation-group-purchase-{index}',
                            'stock_sources':sources}
                           for index,(name,sources) in enumerate(groups.items(),1)]})
    assert purchase.status_code==201,purchase.text
    for item in created_items:
        received=client.put(f"/api/incoming/receive/sr{item['id']}",json={'received_quantity':30,'idempotency_key':f"kit-receive-{item['id']}"})
        assert received.status_code==200,received.text
    return parent_id


def plan(client,pid,sets=5,key='kit-plan-unique'):
    preview=client.get(f'/api/production/stock-preparation/groups/{pid}/preview',params={'sets':sets})
    assert preview.status_code==200,preview.text
    body=dict(action='plan',parent_id=pid,sets=sets,operation_key=key,basis_hash=preview.json()['basis_hash'],location_id=7,layout_version=1)
    return client.post('/api/production/stock-preparation/group-actions',json=body),body


def action_body(client,pid,key,action):
    group=next(g for g in client.get('/api/production/stock-preparation/groups').json()['groups'] if g['status']=='pending')
    return dict(action=action,parent_id=pid,group_key=group['group']['key'],operation_key=key,location_id=7,layout_version=1,
        jobs=[dict(job_id=j['id'],job_version=j['version'],lot_version=j['lot_version'],actual_output=j['expected_output']) for j in group['jobs']])


def test_group_reserve_complete_and_frozen_bom(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        pid=prepare(app,factory,client)
        response,body=plan(client,pid);assert response.status_code==200,response.text
        assert client.post('/api/production/stock-preparation/group-actions',json=body).json()==response.json()
        rows=client.get('/api/production/stock-preparation',params={'workspace':True,'state':'pending'}).json()['items']
        assert len(rows)==1 and rows[0]['entry_type']=='group_job'
        complete=action_body(client,pid,'kit-complete-unique','complete')
        assert len(complete['jobs'])==2
        with factory() as db:
            edges=list(db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==pid)))
            for edge in edges:edge.quantity_per_set=99
            db.commit()
        response=client.post('/api/production/stock-preparation/group-actions',json=complete);assert response.status_code==200,response.text
        assert client.post('/api/production/stock-preparation/group-actions',json=complete).status_code==200
        group=client.get('/api/production/stock-preparation/groups').json()['groups'][0]
        assert group['completed_sets']==5
    with factory() as db:
        jobs=list(db.scalars(select(StockPreparationJob)))
        assert sorted(j.actual_output for j in jobs)==[15,20]
        outputs=[db.get(InventoryLot,j.output_lot_id) for j in jobs]
        assert {lot.warehouse_location_id for lot in outputs}=={7}
        assert all(lot.finished_detail.product_id!=pid for lot in outputs)


def test_group_missing_part_stale_and_failure_rollback(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app,raise_server_exceptions=False) as client:
        pid=prepare(app,factory,client)
        response,_=plan(client,pid,sets=100);assert response.status_code==409
        import app.services.stock_preparation as service
        original=service.append_audit_event;calls=[]
        def fault(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:raise RuntimeError('second child failure')
            return original(*args,**kwargs)
        monkeypatch.setattr(service,'append_audit_event',fault)
        response,body=plan(client,pid);assert response.status_code==500
        with factory() as db:
            assert not list(db.scalars(select(StockPreparationJob)))
            assert not list(db.scalars(select(StockPreparationCommand)))
        monkeypatch.setattr(service,'append_audit_event',original)
        response=client.post('/api/production/stock-preparation/group-actions',json=body);assert response.status_code==200,response.text
        cancel=action_body(client,pid,'kit-cancel-unique','cancel')
        missing=copy.deepcopy(cancel);missing['jobs'].pop()
        assert client.post('/api/production/stock-preparation/group-actions',json=missing).status_code==409
        row=client.get('/api/production/stock-preparation').json()['items'][0];job=row['jobs'][0]
        single=dict(action='cancel',operation_key='cannot-cancel-child',lot_version=row['lot_version'],job_id=job['id'],job_version=job['version'])
        assert client.post(f"/api/production/stock-preparation/{row['receipt_item_id']}/actions",json=single).status_code==409
        assert client.post('/api/production/stock-preparation/group-actions',json=cancel).status_code==200
        assert all(r['reserved']==0 and r['available']==30 for r in client.get('/api/production/stock-preparation').json()['items'])


def test_physical_carton_parent_does_not_steal_grid_recipe_and_partial_output(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        pid=prepare(app,factory,client)
        with factory() as db:
            grid=db.get(Product,pid)
            values={c.name:getattr(grid,c.name) for c in Product.__table__.columns if c.name not in {'id','product_code','customer_material_code'}}
            carton=Product(**dict(values,product_code='PHYSICAL-CARTON',customer_material_code='PHYSICAL-CARTON',is_virtual_composite_parent=False))
            db.add(carton);db.flush()
            for edge in db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==pid)):
                db.add(ProductBomComponent(parent_product_id=carton.id,component_product_id=edge.component_product_id,quantity_per_set=edge.quantity_per_set*5,display_order=edge.display_order,internal_component_code=edge.internal_component_code,is_required=True))
            db.commit()
        rows=client.get('/api/production/stock-preparation',params={'workspace':True}).json()['items']
        assert len(rows)==1 and rows[0]['plan']['recipe']['parent_id']==pid
        assert [c['per_set'] for c in rows[0]['plan']['recipe']['children']]==[3,4]
        response,body=plan(client,pid);assert response.status_code==200
        changed=dict(body,sets=6)
        assert client.post('/api/production/stock-preparation/group-actions',json=changed).status_code==409
        complete=action_body(client,pid,'partial-kit-output','complete')
        complete['jobs'][0]['actual_output']-=1
        assert client.post('/api/production/stock-preparation/group-actions',json=complete).status_code==200
        assert client.get('/api/production/stock-preparation/groups').json()['groups'][0]['completed_sets']==4


def test_group_preview_permission_and_stale_inventory(stock_replenishment_app):
    from app.models.user import User
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        assert client.get('/api/production/stock-preparation/groups').status_code==401
        pid=prepare(app,factory,client)
        preview=client.get(f'/api/production/stock-preparation/groups/{pid}/preview',params={'sets':5}).json()
        with factory() as db:
            from app.models.incoming_receipt import IncomingReceiptItem
            receipt=db.get(IncomingReceiptItem,preview['inputs'][0]['receipt_id'])
            db.get(InventoryLot,receipt.received_inventory_lot_id).version+=1
            db.commit()
        body=dict(action='plan',parent_id=pid,sets=5,operation_key='stale-kit-preview',basis_hash=preview['basis_hash'],location_id=7,layout_version=1)
        assert client.post('/api/production/stock-preparation/group-actions',json=body).status_code==409
        with factory() as db:
            user=db.scalar(select(User));user.role='sales';user.customer_access_mode='selected';db.commit()
        assert client.post('/api/production/stock-preparation/group-actions',json=body).status_code==403
        assert client.get(f'/api/production/stock-preparation/groups/{pid}/preview').status_code==403



def test_group_plan_needs_no_output_location_and_keeps_material_positions(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        pid=prepare(app,factory,client)
        preview=client.get(f'/api/production/stock-preparation/groups/{pid}/preview?sets=5').json()
        with factory() as db:
            before={l.id:(l.warehouse_location_id,l.quantity_available) for l in db.scalars(select(InventoryLot))}
        body=dict(action='plan',parent_id=pid,sets=5,operation_key='no-position-plan',basis_hash=preview['basis_hash'])
        response=client.post('/api/production/stock-preparation/group-actions',json=body)
        assert response.status_code==200,response.text
        assert client.post('/api/production/stock-preparation/group-actions',json=body).json()==response.json()
        with factory() as db:
            assert all(l.warehouse_location_id==before[l.id][0] and l.quantity_available+l.quantity_reserved==before[l.id][1] for l in db.scalars(select(InventoryLot)))
            assert all(j.output_lot_id is None and json.loads(j.product_snapshot)['preparation_group']['planned_location'] is None for j in db.scalars(select(StockPreparationJob)))
        rows=client.get('/api/production/stock-preparation?workspace=true&state=pending').json()['items']
        assert len(rows)==1 and rows[0]['task']['group']['sets']==5
        assert all(j['source_location'] for j in rows[0]['task']['jobs'])
