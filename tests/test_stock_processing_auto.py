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


def test_auto_receipt_partial_replay_completion_and_identity(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        item,receipt=receive(client);r=row(client)
        assert (r['available'],r['reserved'],r['status'])==(0,0,'stock')
        assert len(r['jobs'])==1 and r['jobs'][0]['status']=='completed'
        assert r['jobs'][0]['product']['auto_planned'] and r['jobs'][0]['actual_output']==20
        assert client.put(f"/api/incoming/receive/sr{item['id']}",json=receipt).status_code==200
        assert len(row(client)['jobs'])==1
        changed=dict(receipt,received_quantity=19)
        assert client.put(f"/api/incoming/receive/sr{item['id']}",json=changed).status_code==409
    with factory() as db:
        batch=db.scalar(select(Job));output=db.get(InventoryLot,batch.output_lot_id)
        assert output.quantity_available==20 and output.finished_detail.product_id==batch.product_id
        source=db.get(InventoryLot,db.get(IncomingReceiptItem,batch.receipt_item_id).received_inventory_lot_id)
        assert source.quantity_consumed==20 and source.quantity_available==0
        assert len(list(db.scalars(select(Job).where(Job.status=='completed'))))==1


def test_auto_receipt_uses_cumulative_rounding_across_split_lots(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    payload=_customer_replenishment_payload(3)
    payload['items'][0]['stock_yield_per_sheet']=1
    payload['items'][0]['pieces_per_box']=3
    with TestClient(app) as client:
        item,_=receive(client,payload=payload,quantity=2)
        with factory() as db:
            assert not list(db.scalars(select(Job)))
            assert sum(l.quantity_available for l in db.scalars(select(InventoryLot)).all()
                if l.inventory_type=='semi_finished')==2
        second=client.put(f"/api/incoming/receive/sr{item['id']}",json={
            'received_quantity':1,'idempotency_key':'processing-receive-second'})
        assert second.status_code==200,second.text
        assert client.put(f"/api/incoming/receive/sr{item['id']}",json={
            'received_quantity':1,'idempotency_key':'processing-receive-second'}).status_code==200
    with factory() as db:
        jobs=list(db.scalars(select(Job).order_by(Job.id)))
        assert len(jobs)==2 and sum(j.input_quantity for j in jobs)==3
        assert sum(j.actual_output for j in jobs)==1
        output=db.get(InventoryLot,next(j.output_lot_id for j in jobs if j.output_lot_id))
        assert output.quantity_available==1
        sources=[db.get(InventoryLot,db.get(IncomingReceiptItem,j.receipt_item_id).received_inventory_lot_id) for j in jobs]
        assert sum(row.quantity_consumed for row in sources)==3


def test_partial_audit_failure_rolls_back_and_zero_rejected(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    from app.services import stock_preparation_processing as processing
    monkeypatch.setattr(processing,'auto_complete_receipt',processing.auto_plan_receipt)
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
    monkeypatch.setattr(service,'auto_complete_receipt',lambda *args:None)
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
def test_independent_components_actual_assembly_and_replay(stock_replenishment_app,source,parent_unit,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    from app.api.production import router as production_router
    app.include_router(production_router,prefix='/api/production')
    from app.services import stock_preparation_processing as processing
    monkeypatch.setattr(processing,'auto_complete_receipt',processing.auto_plan_receipt)
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
            assert pending['unit']==pending['verified_output_unit']=='套'
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
        assert plan['output_unit']=='套'
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
            # The purchase froze 套 before the later parent-unit edit.
            assert lot_display_unit(output)=='套'
            from app.services.inventory_valuation import frozen_cost
            unit,evidence=frozen_cost(output,db)
            assert unit is not None and evidence['total_cost']


def test_stocktake_component_lots_assemble_without_fake_preparation_jobs(stock_replenishment_app, monkeypatch):
    """000120 class: qualified counted child lots are inventory facts, not fake jobs."""
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    from app.api.production import router as production_router
    app.include_router(production_router, prefix='/api/production')
    from app.services import stock_preparation_processing as processing
    monkeypatch.setattr(processing, 'auto_complete_receipt', processing.auto_plan_receipt)
    from test_stock_preparation_groups import prepare
    from app.models.multilevel_bom import ProductBomProfile, ProductBomInventoryRelation
    from app.models.product_bom import ProductBomComponent
    with TestClient(app) as client:
        pid = prepare(app, factory, client)
        with factory() as db:
            db.add(ProductBomProfile(product_id=pid, source='assembled', material_mode='expand_children', delivery_mode='parent'))
            for edge in db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id == pid)):
                db.add(ProductBomInventoryRelation(bom_component_id=edge.id, relation='assembly'))
            db.commit()
        for r in client.get('/api/production/stock-preparation').json()['items']:
            response = client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",
                json=completion(r, 30, 'stocktake-child-' + str(r['receipt_item_id'])))
            assert response.status_code == 200, response.text
        with factory() as db:
            jobs = list(db.scalars(select(Job).where(Job.status == 'completed')))
            assert jobs
            for job in jobs:
                lot = db.get(InventoryLot, job.output_lot_id)
                lot.source_type = 'stocktake'
                lot.source_ref_type = 'stocktake'
                lot.source_ref_id = None
                lot.cost_snapshot_source = 'inventory_confirmed_material'
                lot.cost_snapshot_detail_json = json.dumps({
                    'currency': 'CNY', 'price_unit': '元/片',
                    'source': 'audited_stocktake_component',
                })
                db.delete(job)
            db.commit()
            assert db.scalar(select(func.count()).select_from(Job)) == 0
        plan_response = client.get(f'/api/production/stock-preparation/assembly/{pid}/preview', params={'sets': 5})
        assert plan_response.status_code == 200, plan_response.text
        plan = plan_response.json()
        assert plan['available_sets'] == 7 and all(source['job_id'] is None for source in plan['sources'])
        body = dict(action='assemble_stock', parent_id=pid, sets=5,
            basis_hash=plan['basis_hash'], jobs=plan['sources'], location_id=7,
            layout_version=1, operation_key='stocktake-inventory-first-assembly')
        saved = client.post('/api/production/stock-preparation/group-actions', json=body)
        assert saved.status_code == 200, saved.text
        assert client.post('/api/production/stock-preparation/group-actions', json=body).json() == saved.json()
        with factory() as db:
            output = db.get(InventoryLot, saved.json()['output_lot_id'])
            assert output.quantity_available == 5
            assert db.get(Command, 'stocktake-inventory-first-assembly').receipt_item_id is None


def test_pure_assembly_parent_without_own_physical_spec_uses_consumed_children(stock_replenishment_app,monkeypatch):
    """80012500 class: a pure assembly parent is not an independently made sheet."""
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    factory.class_ = ReceiptPriceGuardSession
    from app.api.production import router as production_router
    app.include_router(production_router,prefix='/api/production')
    from app.services import stock_preparation_processing as processing
    monkeypatch.setattr(processing,'auto_complete_receipt',processing.auto_plan_receipt)
    from test_stock_preparation_groups import prepare
    from app.models.multilevel_bom import ProductBomProfile,ProductBomInventoryRelation
    from app.models.product_bom import ProductBomComponent
    from app.models.product import Product
    with TestClient(app) as client:
        pid=prepare(app,factory,client)
        with factory() as db:
            parent=db.get(Product,pid)
            parent.product_code='80012500'
            parent.unit='套'
            parent.material_id=None;parent.legacy_material_text=None;parent.flute_type=None
            parent.length_mm=None;parent.width_mm=None;parent.height_mm=None
            db.add(ProductBomProfile(product_id=pid,source='assembled'))
            for edge in db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==pid)):
                db.add(ProductBomInventoryRelation(bom_component_id=edge.id,relation='assembly'))
            db.commit()
        for r in client.get('/api/production/stock-preparation').json()['items']:
            body=completion(r,30,'80012500-child-'+str(r['receipt_item_id']))
            response=client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body)
            assert response.status_code==200,response.text
        plan=client.get(f'/api/production/stock-preparation/assembly/{pid}/preview',params={'sets':5})
        assert plan.status_code==200,plan.text
        plan=plan.json()
        body=dict(action='assemble_stock',parent_id=pid,sets=5,basis_hash=plan['basis_hash'],jobs=plan['sources'],
            location_id=7,layout_version=1,operation_key='80012500-pure-assembly')
        response=client.post('/api/production/stock-preparation/group-actions',json=body)
        assert response.status_code==200,response.text
        with factory() as db:
            output=db.get(InventoryLot,response.json()['output_lot_id'])
            assert output.quantity_available==5
            assert output.cost_snapshot_at is not None
            frozen_at = output.cost_snapshot_at
            assert not json.loads(output.finished_detail.physical_basis_json)['spec']
            assert all(db.get(InventoryLot,row['lot_id']).quantity_consumed==row['quantity'] for row in plan['sources'])
        assert client.post('/api/production/stock-preparation/group-actions',json=body).json()==response.json()
        with factory() as db:
            assert db.get(InventoryLot,response.json()['output_lot_id']).cost_snapshot_at==frozen_at


def test_pure_assembly_missing_child_spec_names_child_and_rolls_back(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    from app.api.production import router as production_router
    app.include_router(production_router,prefix='/api/production')
    from app.services import stock_preparation_processing as processing
    monkeypatch.setattr(processing,'auto_complete_receipt',processing.auto_plan_receipt)
    from test_stock_preparation_groups import prepare
    from app.models.multilevel_bom import ProductBomProfile,ProductBomInventoryRelation
    from app.models.product_bom import ProductBomComponent
    from app.models.warehouse_goods import WarehouseGoodsProfile
    from app.models.product import Product
    with TestClient(app) as client:
        pid=prepare(app,factory,client)
        with factory() as db:
            db.add(ProductBomProfile(product_id=pid,source='assembled'))
            for edge in db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==pid)):
                db.add(ProductBomInventoryRelation(bom_component_id=edge.id,relation='assembly'))
            db.commit()
        for r in client.get('/api/production/stock-preparation').json()['items']:
            response=client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",
                json=completion(r,30,'missing-child-output-'+str(r['receipt_item_id'])))
            assert response.status_code==200,response.text
        with factory() as db:
            job=db.scalar(select(Job).order_by(Job.id))
            output=db.get(InventoryLot,job.output_lot_id)
            profile=db.get(WarehouseGoodsProfile,output.id)
            product=db.get(Product,job.product_id)
            product.material_id=None;product.legacy_material_text=None;product.flute_type=None
            product.length_mm=None;product.width_mm=None;product.height_mm=None
            from app.services.finished_stock_identity import product_basis
            physical=json.loads(profile.data_json);physical['physical_basis']=product_basis(product)
            profile.data_json=json.dumps(physical,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            snapshot=json.loads(job.product_snapshot);snapshot['physical_basis']=physical['physical_basis']
            job.product_snapshot=json.dumps(snapshot,ensure_ascii=False)
            db.commit()
        plan=client.get(f'/api/production/stock-preparation/assembly/{pid}/preview',params={'sets':1}).json()
        before={row['lot_id']:row['available'] for row in plan['sources']}
        response=client.post('/api/production/stock-preparation/group-actions',json=dict(
            action='assemble_stock',parent_id=pid,sets=1,basis_hash=plan['basis_hash'],jobs=plan['sources'],
            location_id=7,layout_version=1,operation_key='missing-child-rollback'))
        assert response.status_code==409,response.text
        assert '子件' in response.text and '缺少实物规格' in response.text
    with factory() as db:
        assert all(db.get(InventoryLot,lot_id).quantity_available==quantity for lot_id,quantity in before.items())
        assert db.get(Command,'missing-child-rollback') is None


def test_frozen_yield_batch_units_and_continuing_after_product_change(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    from app.services import stock_preparation_processing as processing
    monkeypatch.setattr(processing,'auto_complete_receipt',processing.auto_plan_receipt)
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


def test_product_change_before_receipt_uses_purchase_identity(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    from app.services import stock_preparation_processing as processing
    monkeypatch.setattr(processing,'auto_complete_receipt',processing.auto_plan_receipt)
    def change(item):
        from app.models.product import Product
        from app.core.time_contract import utc_now_naive
        with factory() as db:
            product=db.get(Product,1);product.production_process='新的模切身份';product.updated_at=utc_now_naive();product.version+=1;db.commit()
    with TestClient(app) as client:
        receive(client,before_receive=change)
        r=row(client);assert r['available']==0 and r['reserved']==20 and len(r['jobs'])==1
        rows=client.get('/api/production/stock-preparation',params={'workspace':True,'state':'pending'}).json()['items']
        assert rows and r['jobs'][0]['product']['factor']==1
        assert '新的模切身份' not in r['jobs'][0]['product']['physical_basis']
        body=completion(r,6,'changed-product-process')
        assert client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body).status_code==200


def test_overreceipt_is_visible_raw_and_auto_plan_stays_within_purchase(stock_replenishment_app,monkeypatch):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    from app.services import stock_preparation_processing as processing
    monkeypatch.setattr(processing,'auto_complete_receipt',processing.auto_plan_receipt)
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
    from app.services import stock_preparation_processing as processing
    monkeypatch.setattr(processing,'auto_complete_receipt',processing.auto_plan_receipt)
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
