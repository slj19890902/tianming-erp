import copy
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from test_stock_replenishment_flow import stock_replenishment_app,_login,_customer_replenishment_payload
from app.api.stock_preparation import router
from app.models.stock_preparation import StockPreparationJob as Job,StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot,InventoryReservation
from app.models.incoming_receipt import IncomingReceiptItem


def receive(client,payload=None,before_receive=None,quantity=20):
    _login(client)
    created=client.post('/api/requisition/stock-replenishment/orders',json=payload or _customer_replenishment_payload(30))
    assert created.status_code==201,created.text
    item=created.json()['items'][0]
    row=next(r for r in client.get('/api/requisition/pending').json()['items'] if r.get('stock_replenishment_item_id')==item['id'])
    purchase=client.post('/api/requisition/supplier-orders/from-pending-selection',json={
        'supplier_groups':[{'supplier_name':row['supplier_name'],'request_key':'processing-purchase',
            'stock_sources':[{'stock_replenishment_item_id':item['id'],'source_fingerprint':row['source_fingerprint']}]}]})
    assert purchase.status_code==201,purchase.text
    if before_receive:
        before_receive(item)
    body={'received_quantity':quantity,'idempotency_key':'processing-receive'}
    if quantity<30:body['resolution_action']='await_supplier'
    response=client.put(f"/api/incoming/receive/sr{item['id']}",json=body)
    assert response.status_code==200,response.text
    return item,body


def row(client):
    return next(r for r in client.get('/api/production/stock-preparation').json()['items'] if r['receipt_item_id'])


def completion(r,input=8,key='processing-complete'):
    job=next(j for j in r['jobs'] if j['status']=='pending')
    return dict(action='complete',operation_key=key,lot_version=r['lot_version'],job_id=job['id'],job_version=job['version'],
        actual_input_quantity=input,actual_output=input*job['product']['factor']//job['product']['pieces_per_box'],
        location_id=7,layout_version=1,output_kind='semi')


def test_auto_receipt_partial_replay_cancel_and_identity(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        item,receipt=receive(client);r=row(client)
        assert (r['available'],r['reserved'],r['status'])==(0,20,'pending')
        assert len(r['jobs'])==1 and r['jobs'][0]['product']['auto_planned']
        assert client.put(f"/api/incoming/receive/sr{item['id']}",json=receipt).status_code==200
        assert len(row(client)['jobs'])==1
        changed=dict(receipt,received_quantity=19)
        assert client.put(f"/api/incoming/receive/sr{item['id']}",json=changed).status_code==409
        body=completion(r)
        response=client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body)
        assert response.status_code==200,response.text
        result=response.json();assert result['remaining_input_quantity']==12
        assert client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body).json()==result
        r=row(client);assert (r['available'],r['reserved'])==(0,12)
        pending=next(j for j in r['jobs'] if j['status']=='pending')
        assert pending['input_quantity']==12
        cancel=dict(action='cancel',operation_key='processing-cancel',lot_version=r['lot_version'],job_id=pending['id'],job_version=pending['version'])
        assert client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=cancel).status_code==200
        assert (row(client)['available'],row(client)['reserved'])==(12,0)
    with factory() as db:
        batch=db.get(Job,result['completed_job_id']);output=db.get(InventoryLot,batch.output_lot_id)
        from app.models.warehouse_goods import WarehouseGoodsProfile
        profile=json.loads(db.get(WarehouseGoodsProfile,output.id).data_json)
        assert profile['output_piece'] and profile['dimension_basis']=='source_board'
        assert profile['quantity_unit']=='pieces' and profile['remaining_processes']==[]
        assert output.semi_finished_detail.stock_yield_per_sheet==output.semi_finished_detail.pieces_per_box==1
        source=db.get(InventoryLot,db.get(IncomingReceiptItem,batch.receipt_item_id).received_inventory_lot_id)
        assert source.quantity_consumed==8
        assert len(list(db.scalars(select(Job).where(Job.status=='completed'))))==1


def test_partial_audit_failure_rolls_back_and_zero_rejected(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app,raise_server_exceptions=False) as client:
        receive(client);r=row(client);body=completion(r)
        zero=dict(body,actual_input_quantity=0)
        assert client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=zero).status_code==422
        import app.services.stock_preparation as service
        original=service.append_audit_event
        def fault(*args,**kwargs):raise RuntimeError('audit fault')
        monkeypatch.setattr(service,'append_audit_event',fault)
        assert client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body).status_code==500
        after=row(client);assert after['reserved']==20 and len(after['jobs'])==1
        monkeypatch.setattr(service,'append_audit_event',original)
        assert client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body).status_code==200


def test_legacy_get_is_read_only_and_process_owns_remainder(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    from app.services import stock_preparation_processing as service
    monkeypatch.setattr(service,'auto_plan_receipt',lambda *args:None)
    with TestClient(app) as client:
        receive(client);r=row(client)
        with factory() as db:
            count=db.scalar(select(func.count()).select_from(Command))
        pending=client.get('/api/production/stock-preparation',params={'workspace':True,'state':'pending'}).json()['items']
        assert len(pending)==1 and pending[0]['entry_type']=='legacy_material'
        assert pending[0]['processing_block'] is None and pending[0]['processing_expected_output']==20
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Job))==0
            assert db.scalar(select(func.count()).select_from(Command))==count
        body=dict(action='process',operation_key='legacy-processing',lot_version=r['lot_version'],actual_input_quantity=6,
            actual_output=6,location_id=7,layout_version=1,output_kind='semi')
        response=client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body)
        assert response.status_code==200,response.text
        assert response.json()['remaining_input_quantity']==14


@pytest.mark.parametrize('source',['legacy','assembled','manufactured'])
@pytest.mark.parametrize('parent_unit',['只','套'])
def test_independent_components_actual_assembly_and_replay(stock_replenishment_app,source,parent_unit):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    from app.api.production import router as production_router
    app.include_router(production_router,prefix='/api/production')
    from test_stock_preparation_groups import prepare
    with TestClient(app) as client:
        pid=prepare(app,factory,client)
        with factory() as db:
            from app.models.product import Product
            db.get(Product,pid).unit=parent_unit
            db.commit()
        if source!='legacy':
            from app.models.multilevel_bom import ProductBomProfile,ProductBomInventoryRelation
            from app.models.product_bom import ProductBomComponent
            with factory() as db:
                db.add(ProductBomProfile(product_id=pid,source=source))
                for edge in db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==pid)):
                    db.add(ProductBomInventoryRelation(bom_component_id=edge.id,relation='assembly'))
                db.commit()
        rows=client.get('/api/production/stock-preparation').json()['items']
        for r in rows:
            body=completion(r,30,'independent-output-'+str(r['receipt_item_id']))
            response=client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body)
            assert response.status_code==200,response.text
        if source=='assembled':
            pending_response=client.get('/api/production/pending-assemblies')
            assert pending_response.status_code==200,pending_response.text
            pending=next(r for r in pending_response.json()['items'] if r.get('parent_product_id')==pid)
            assert pending['can_assemble_stock'] and pending['available_sets']==7
            assert pending['unit']==pending['verified_output_unit']==parent_unit
        # A malformed unrelated old task must not enter the new assembly path.
        with factory() as db:
            original=db.scalar(select(Job))
            raw=db.get(InventoryReservation,original.reservation_id).inventory_lot_id
            reservation=InventoryReservation(reservation_number='BROKEN-HISTORICAL',inventory_lot_id=raw,
                reservation_type='semi_order',reserved_stock_quantity=1,released_stock_quantity=1,status='released')
            db.add(reservation);db.flush()
            db.add(Job(receipt_item_id=original.receipt_item_id,reservation_id=reservation.id,
                product_id=original.product_id,product_snapshot='bad old JSON',input_quantity=1,expected_output=1,status='cancelled'))
            db.commit()
        plan=client.get(f'/api/production/stock-preparation/assembly/{pid}/preview',params={'sets':5})
        if source=='manufactured':
            assert plan.status_code==409 and 'BOM' in plan.json()['detail']
            return
        assert plan.status_code==200,plan.text
        plan=plan.json();assert plan['available_sets']==7 and not plan['shortages']
        assert plan['output_unit']==parent_unit
        body=dict(action='assemble_stock',parent_id=pid,sets=5,basis_hash=plan['basis_hash'],jobs=plan['sources'],
            location_id=7,layout_version=1,operation_key='independent-stock-assembly')
        response=client.post('/api/production/stock-preparation/group-actions',json=body)
        assert response.status_code==200,response.text
        assert client.post('/api/production/stock-preparation/group-actions',json=body).json()==response.json()
        plan=client.get(f'/api/production/stock-preparation/assembly/{pid}/preview').json()
        assert plan['available_sets']==2
        with factory() as db:
            output=db.get(InventoryLot,response.json()['output_lot_id'])
            assert output.quantity_available==5 and output.finished_detail.product_id==pid
            from app.services.warehouse_display_units import lot_display_unit
            assert lot_display_unit(output)==parent_unit
            from app.services.inventory_valuation import frozen_cost
            unit,evidence=frozen_cost(output,db)
            assert unit is not None and evidence['total_cost']


def test_frozen_yield_batch_units_and_continuing_after_product_change(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        payload=_customer_replenishment_payload(30);payload['items'][0]['stock_yield_per_sheet']=6
        receive(client,payload);r=row(client)
        assert r['jobs'][0]['expected_output']==120
        from app.models.product import Product
        with factory() as db:
            product=db.get(Product,1);product.product_name='后来改名';product.version+=1;db.commit()
        body=completion(r,8,'frozen-yield-processing')
        assert body['actual_output']==48
        response=client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body)
        assert response.status_code==200,response.text
        r=row(client);pending=next(j for j in r['jobs'] if j['status']=='pending')
        assert pending['expected_output']==72 and pending['product']['name']!='后来改名'


def test_product_change_before_receipt_stays_raw_and_read_only(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    def change(item):
        from app.models.product import Product
        from app.core.time_contract import utc_now_naive
        with factory() as db:
            product=db.get(Product,1);product.production_process='新的模切身份';product.updated_at=utc_now_naive();product.version+=1;db.commit()
    with TestClient(app) as client:
        receive(client,before_receive=change)
        r=row(client);assert r['available']==20 and not r['jobs']
        rows=client.get('/api/production/stock-preparation',params={'workspace':True,'state':'pending'}).json()['items']
        assert rows[0]['processing_block'] and rows[0]['processing_yield_per_sheet']==1
        body=dict(action='process',operation_key='changed-product-process',lot_version=r['lot_version'],actual_input_quantity=6,
            actual_output=6,location_id=7,layout_version=1,output_kind='semi')
        assert client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body).status_code==409


def test_overreceipt_is_visible_raw_and_auto_plan_stays_within_purchase(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        receive(client,quantity=35)
        r=row(client)
        assert (r['available'],r['reserved'],r['physical'])==(5,30,35)
        assert r['jobs'][0]['input_quantity']==30
        body=completion(r,30);body.pop('output_kind')
        response=client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body)
        assert response.status_code==200,response.text
        r=row(client)
        assert r['available']==5 and r['jobs'][0]['output_kind']=='semi'


@pytest.mark.parametrize('ownership',['staging','subkit','location'])
def test_stock_assembly_excludes_order_owned_and_inactive_outputs(stock_replenishment_app,monkeypatch,ownership):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    from test_stock_preparation_groups import prepare
    with TestClient(app) as client:
        pid=prepare(app,factory,client)
        for r in client.get('/api/production/stock-preparation').json()['items']:
            body=completion(r,30,'owned-output-'+str(r['receipt_item_id']))
            assert client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body).status_code==200
        plan=client.get(f'/api/production/stock-preparation/assembly/{pid}/preview').json()
        assert plan['available_sets']>0
        if ownership=='staging':
            from app.services import fixed_shelf_staging
            monkeypatch.setattr(fixed_shelf_staging,'staging_owner',lambda *args:1)
        elif ownership=='subkit':
            from app.services import bom_subkits
            monkeypatch.setattr(bom_subkits,'active_subkit_order',lambda *args:1)
        else:
            from app.models.warehouse_inventory import WarehouseLocation
            with factory() as db:
                db.get(WarehouseLocation,7).is_active=False;db.commit()
        after=client.get(f'/api/production/stock-preparation/assembly/{pid}/preview').json()
        assert after['available_sets']==0 and after['sources']==[]
        body=dict(action='assemble_stock',parent_id=pid,sets=1,basis_hash=plan['basis_hash'],jobs=plan['sources'],
            location_id=7,layout_version=1,operation_key='owned-stock-assembly')
        assert client.post('/api/production/stock-preparation/group-actions',json=body).status_code==409
