import copy
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from stock_preparation_legacy_fixture import stock_replenishment_app, base_stock_replenishment_app
from test_stock_preparation_groups import prepare,plan,action_body
from app.api.stock_preparation import router
from app.models.stock_preparation import StockPreparationJob as Job,StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot


def arranged(app,factory,client,kind='finished'):
    pid=prepare(app,factory,client)
    response,_=plan(client,pid);assert response.status_code==200
    body=action_body(client,pid,'dispose-main-key','dispose')
    body.update(disposition=kind,sets=5)
    for j in body['jobs']:j.update(location_id=7,layout_version=1)
    return pid,body


def test_finished_disposition_consumes_children_and_retains_source_trace(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        pid,body=arranged(app,factory,client)
        response=client.post('/api/production/stock-preparation/group-actions',json=body)
        assert response.status_code==200,response.text
        assert client.post('/api/production/stock-preparation/group-actions',json=body).json()==response.json()
        changed=dict(body,sets=4)
        assert client.post('/api/production/stock-preparation/group-actions',json=changed).status_code==409
        with factory() as db:
            jobs=list(db.scalars(select(Job)))
            outputs=[db.get(InventoryLot,j.output_lot_id) for j in jobs]
            assert all(l.inventory_type=='semi_finished' and l.quantity_available==0 for l in outputs)
            assert sorted(l.quantity_consumed for l in outputs)==[15,20]
            parent=db.get(InventoryLot,response.json()['assembly']['output_lot_id'])
            assert parent.inventory_type=='finished' and parent.quantity_available==5
            assert parent.finished_detail.product_id==pid
            assert len(json.loads(parent.finished_detail.physical_basis_json)['assembly'])==2
            from app.services.inventory_valuation import frozen_cost
            unit,evidence=frozen_cost(parent,db)
            assert unit is not None, evidence
            assert evidence['currency']=='CNY'
            assert evidence['basis']=='inherited_entry_cost_not_new_purchase'
            assert abs(unit*5-sum(l.estimated_unit_cost_snapshot*l.quantity_consumed for l in outputs))<__import__('decimal').Decimal('.01')


def test_semi_disposition_and_partial_later_assembly(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        pid,body=arranged(app,factory,client,'semi')
        response=client.post('/api/production/stock-preparation/group-actions',json=body)
        assert response.status_code==200,response.text
        with factory() as db:
            jobs=list(db.scalars(select(Job)))
            assert all(db.get(InventoryLot,j.output_lot_id).inventory_type=='semi_finished' for j in jobs)
            members=[dict(job_id=j.id,job_version=j.version,lot_version=1,output_version=db.get(InventoryLot,j.output_lot_id).version) for j in jobs]
        assembly=dict(action='assemble',operation_key='assemble-later-key',parent_id=pid,group_key=response.json()['group_key'],sets=3,location_id=7,layout_version=1,jobs=members)
        result=client.post('/api/production/stock-preparation/group-actions',json=assembly)
        assert result.status_code==200,result.text
        with factory() as db:
            assert sorted(db.get(InventoryLot,j.output_lot_id).quantity_available for j in db.scalars(select(Job)))==[6,8]
            assert db.get(InventoryLot,result.json()['output_lot_id']).quantity_available==3


def test_missing_second_child_cost_rolls_back_entire_assembly(stock_replenishment_app):
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    with TestClient(app) as client:
        pid, body = arranged(app, factory, client, 'semi')
        url = '/api/production/stock-preparation/group-actions'
        response = client.post(url, json=body)
        assert response.status_code == 200, response.text
        with factory() as db:
            jobs = list(db.scalars(select(Job).order_by(Job.id)))
            outputs = [db.get(InventoryLot, j.output_lot_id) for j in jobs]
            outputs[-1].estimated_unit_cost_snapshot = None
            db.commit()
            before = {l.id: (l.quantity_available, l.quantity_consumed, l.version) for l in outputs}
            count = len(list(db.scalars(select(InventoryLot))))
            members = [dict(job_id=j.id, job_version=j.version, lot_version=1,
                            output_version=db.get(InventoryLot, j.output_lot_id).version) for j in jobs]
        assembly = dict(action='assemble', operation_key='missing-child-cost', parent_id=pid,
                        group_key=response.json()['group_key'], sets=3, location_id=7,
                        layout_version=1, jobs=members)
        result = client.post(url, json=assembly)
        assert result.status_code == 409 and '成本' in result.text, result.text
        with factory() as db:
            assert len(list(db.scalars(select(InventoryLot)))) == count
            assert db.get(Command, 'missing-child-cost') is None
            for lot_id, balances in before.items():
                lot = db.get(InventoryLot, lot_id)
                assert (lot.quantity_available, lot.quantity_consumed, lot.version) == balances


def test_disposition_failure_rolls_back_all_stock(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app,raise_server_exceptions=False) as client:
        _,body=arranged(app,factory,client)
        import app.services.stock_preparation_disposition as service
        def fail(*a,**kw):raise RuntimeError('audit failure')
        monkeypatch.setattr(service,'append_audit_event',fail)
        result=client.post('/api/production/stock-preparation/group-actions',json=body)
        assert result.status_code==500,result.text
        with factory() as db:
            assert all(j.status=='pending' and j.output_lot_id is None for j in db.scalars(select(Job)))
            assert len(list(db.scalars(select(InventoryLot))))==2
            assert db.get(Command,body['operation_key']) is None



def test_unassemble_restores_only_children_with_versions_and_replay(stock_replenishment_app):
    from app.services.stock_preparation_disposition import assembly_rows
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        pid,body=arranged(app,factory,client)
        response=client.post('/api/production/stock-preparation/group-actions',json=body)
        assert response.status_code==200,response.text
        with factory() as db:row=assembly_rows(db)[0]
        undo=dict(action='unassemble',operation_key='undo-assembly-key',parent_id=pid,group_key=row['group_key'],assembly_key=row['key'],output_version=row['version'],sources=row['source_versions'],confirm_unused=True)
        url='/api/production/stock-preparation/group-actions'
        stale=copy.deepcopy(undo);stale['sources'][0]['version']+=1
        assert client.post(url,json=stale).status_code==409
        assert client.post(url,json=dict(undo,confirm_unused=False)).status_code==409
        result=client.post(url,json=undo);assert result.status_code==200,result.text
        assert client.post(url,json=undo).json()==result.json()
        with factory() as db:
            assert assembly_rows(db)[0]['reversed']
            assert db.get(InventoryLot,row['output_lot_id']).quantity_available==0
            assert sorted(db.get(InventoryLot,j.output_lot_id).quantity_available for j in db.scalars(select(Job)))==[15,20]
            assert all(j.status=='completed' for j in db.scalars(select(Job)))
            from app.services.stock_preparation_history import rows
            history=rows(db)
            assert any(r['origin']=='stock_assembly' and r['status']=='reversed' for r in history)


def test_material_storage_moves_existing_lot_and_rejects_stale(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        prepare(app,factory,client)
        row=client.get('/api/production/stock-preparation').json()['items'][0]
        payload=dict(action='keep_raw',operation_key='store-raw-location',lot_version=row['lot_version'],location_id=7,layout_version=1)
        url=f"/api/production/stock-preparation/{row['receipt_item_id']}/actions"
        result=client.post(url,json=payload);assert result.status_code==200,result.text
        assert client.post(url,json=payload).json()==result.json()
        assert client.post(url,json=dict(payload,operation_key='store-stale-location')).status_code==409
        with factory() as db:
            from app.services.stock_preparation import source
            receipt,_,lot=source(db,row['receipt_item_id'])
            assert lot.warehouse_location_id==7 and lot.quantity_available==receipt.received_quantity==30
            assert len(list(db.scalars(select(InventoryLot))))==2
        locations=client.get('/api/production/stock-preparation/locations')
        assert locations.status_code==200 and any(l['id']==7 for l in locations.json()['items'])

def test_semi_storage_replay_versions_and_overassembly(stock_replenishment_app):
    from app.models.warehouse_inventory import WarehouseLocation
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        pid,body=arranged(app,factory,client,'semi')
        url='/api/production/stock-preparation/group-actions'
        result=client.post(url,json=body);assert result.status_code==200,result.text
        target=6  # Existing published material slot with real layout.
        rows=client.get('/api/production/stock-preparation').json()['items']
        jobs=[j for r in rows for j in r['jobs']]
        assert len({j['receipt_item_id'] for j in jobs})==2
        move=dict(action='store_outputs',operation_key='move-semi-group',parent_id=pid,group_key=result.json()['group_key'],jobs=[dict(job_id=j['id'],job_version=j['version'],output_version=j['output_version'],lot_version=1,location_id=target,layout_version=1) for j in jobs])
        moved=client.post(url,json=move);assert moved.status_code==200,moved.text
        assert client.post(url,json=move).json()==moved.json()
        assert client.post(url,json=dict(move,operation_key='move-semi-stale')).status_code==409
        with factory() as db:
            assert {db.get(InventoryLot,j.output_lot_id).warehouse_location_id for j in db.scalars(select(Job))}=={target}
            assert sorted(db.get(InventoryLot,j.output_lot_id).quantity_available for j in db.scalars(select(Job)))==[15,20]
            members=[dict(job_id=j.id,job_version=j.version,lot_version=1,output_version=db.get(InventoryLot,j.output_lot_id).version) for j in db.scalars(select(Job))]
        assembly=dict(action='assemble',operation_key='assemble-too-many',parent_id=pid,group_key=result.json()['group_key'],sets=6,location_id=7,layout_version=1,jobs=members)
        rejected=client.post(url,json=assembly);assert rejected.status_code==409,rejected.text
        with factory() as db:
            assert sorted(db.get(InventoryLot,j.output_lot_id).quantity_available for j in db.scalars(select(Job)))==[15,20]
            assert db.get(Command,'assemble-too-many') is None

def test_frozen_identity_and_separate_stock_guard(stock_replenishment_app):
    from app.models.product import Product
    from app.models.user import User
    from app.models.multilevel_bom import ProductBomProfile
    from app.services.warehouse_inventory import manual_finished_in,WarehouseInventoryError
    from datetime import date
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        pid,body=arranged(app,factory,client)
        with factory() as db:
            parent=db.get(Product,pid);old_name=parent.product_name
            parent.product_name='Changed after planning';parent.version+=1
            db.add(ProductBomProfile(product_id=pid,source='separate',material_mode='expand_children',delivery_mode='parent'))
            db.commit()
            actor=db.scalar(select(User))
            with pytest.raises(WarehouseInventoryError):
                manual_finished_in(db,customer_id=parent.customer_id,product_id=pid,location_id=7,quantity=5,stock_date=date.today(),remarks='guard test',source_type='manual',operator_id=actor.id,idempotency_key='no-assembly-proof')
            with pytest.raises(WarehouseInventoryError):
                manual_finished_in(db,customer_id=parent.customer_id,product_id=pid,location_id=7,quantity=5,stock_date=date.today(),remarks='guard test',source_type='transfer',source_ref_type='preparation_assembly',operator_id=actor.id,idempotency_key='fake-assembly-proof',assembly_command_key='fake-assembly-key')
        result=client.post('/api/production/stock-preparation/group-actions',json=body)
        assert result.status_code==200,result.text
        with factory() as db:
            lot=db.get(InventoryLot,result.json()['assembly']['output_lot_id'])
            assert lot.finished_detail.product_name_snapshot==old_name+'（成套）'
            actor=db.scalar(select(User));actor.role='sales';actor.customer_access_mode='selected';db.commit()
        assert client.post('/api/production/stock-preparation/group-actions',json=body).status_code==403
        assert not client.get('/api/production/stock-preparation',params={'workspace':True,'state':'stock'}).json()['items']
