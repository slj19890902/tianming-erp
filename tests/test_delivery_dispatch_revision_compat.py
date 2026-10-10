"""Explicit new external command; existing controlled revision still owns one CAS."""
import copy
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from tests.test_delivery_dispatch_commands import delivery_api_app,command,seed,state,_login,_create_payload
from tests.test_p1_137b_dispatched_delivery_revision import _revision_payload
from tests.test_p1_140_external_stock_replenishment import external_stock_app,p1_40a_app,_seed_external_warning,_login as external_login


def test_internal_revision_preserves_one_version_and_exact_replay(delivery_api_app):
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');created=seed(c,factory);did=created['id'];body=command(c,did)
        first=c.put(f'/api/deliveries/{did}/dispatch',json=body);assert first.status_code==200,first.text
        version=first.json()['dispatch_receipt']['completed_version'];payload=_revision_payload(version)
        revised=c.put(f'/api/deliveries/{did}/revision',json=payload)
        assert revised.status_code==200,revised.text
        assert revised.json()['version']==version+1 and revised.json()['total_quantity']==35 and revised.json()['status']=='dispatched'
        after=state(factory);assert c.put(f'/api/deliveries/{did}/revision',json=payload).json()==revised.json() and state(factory)==after
        invalid=copy.deepcopy(payload);invalid.update(expected_version=version+1,idempotency_key='dispatch-revision-invalid');invalid['items'][0]['delivered_quantity']=9999
        failed=c.put(f'/api/deliveries/{did}/revision',json=invalid);assert failed.status_code==409 and state(factory)==after


def test_external_receipt_unordered_dual_quantity_command(external_stock_app):
    from app.api.deliveries import router
    from app.core.time_contract import beijing_today
    from app.models.warehouse_inventory import WarehouseLocation,InventoryLot,FinishedGoodsInventoryDetail,InventoryMovement
    from app.models.order import Order
    external_stock_app.include_router(router,prefix='/api/deliveries')
    policy,product=_seed_external_warning(external_stock_app);factory=external_stock_app.state.factory
    with factory() as db:
        db.add(WarehouseLocation(location_code='F1-DISPATCH-01',location_name='合成成品待送区',warehouse_type='finished',warehouse_floor=1,area_code='DISPATCH',storage_type='temporary_aisle',source_version='P1-25C',placement_status='placed',is_active=True));db.commit()
    with TestClient(external_stock_app) as c:
        external_login(c);draft=c.get(f'/api/requisition/stock-policies/{policy}/replenishment-draft').json();line=draft['items'][0];line['external_purchase_quantity']='200'
        order=c.post('/api/requisition/stock-replenishment/orders',json=dict(source_type='stock_warning',idempotency_key='command-external-seed',customer_id=draft['customer_id'],supplier_name=draft['supplier_name'],stock_now=False,items=[line]));assert order.status_code==201,order.text
        purchase=order.json()['external_purchase_orders'][0]
        received=c.post(f"/api/external-packaging-purchases/{purchase['id']}/receipts",json=dict(idempotency_key='command-external-receipt',lines=[dict(purchase_item_id=purchase['items'][0]['id'],received_quantity='200')]));assert received.status_code==200,received.text
        with factory() as db:lot_id=db.scalar(select(InventoryLot.id).join(FinishedGoodsInventoryDetail).where(FinishedGoodsInventoryDetail.product_id==product))
        created=c.post('/api/deliveries',json=dict(customer_id=draft['customer_id'],delivery_date=str(beijing_today()),source_mode='unordered_finished',lines=[dict(source_type='finished_stock',product_id=product,delivered_quantity=100,unit_price='20',allocations=[dict(inventory_lot_id=lot_id,quantity=200)])]));assert created.status_code==201,created.text
        did=created.json()['id'];body=command(c,did,'command-unordered-dual');saved=c.put(f'/api/deliveries/{did}/dispatch',json=body);assert saved.status_code==200,saved.text
        row=saved.json()['dispatch_receipt']['final_items'][0]
        assert row['customer_quantity']==100 and row['physical_quantity']==200 and row['physical_unit']=='片'
        assert sum(r['physical_quantity'] for r in saved.json()['dispatch_receipt']['movements'])==200
        after=state(factory);assert c.put(f'/api/deliveries/{did}/dispatch',json=body).json()==saved.json() and state(factory)==after
        with factory() as db:
            lot=db.get(InventoryLot,lot_id);assert lot.quantity_available==0 and lot.quantity_consumed==200
            assert db.scalar(select(func.count()).select_from(Order))==0



from tests.test_n039_composite_bom_requisition import composite_requisition_app
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


def _run_existing_receipt_source_flow(module, function, fixture, monkeypatch, *args):
    """Reuse the old real seed/receipt assertions, with explicit new HTTP entry.

    This adapter belongs only to these named new compatibility tests. Original
    tests, conftest, application routes and all old no-body checks are untouched.
    """
    attempts={}
    class CommandClient(TestClient):
        def put(self,url,**kwargs):
            if str(url).endswith('/dispatch'):
                did=int(str(url).split('/')[-2])
                body=attempts.setdefault(did,command(self,did,f'compat-source-{did}')) if did not in attempts else attempts[did]
                kwargs['json']=body
            return super().put(url,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(module,'TestClient',CommandClient)
        getattr(module,function)(fixture,*args)


def test_frozen_accompany_source_conservation(composite_requisition_app,_p181_published_map_identity,monkeypatch):
    from tests import test_bom_accompany418 as old
    _run_existing_receipt_source_flow(old,'test_separate_receipts_accompany_four_dispatch_and_reverse',composite_requisition_app,monkeypatch,_p181_published_map_identity)


def test_frozen_subkit_source_conservation(composite_requisition_app,_p181_published_map_identity,monkeypatch):
    from tests import test_bom_subkit_receipt_flow as old
    _run_existing_receipt_source_flow(old,'test_parent_and_liner_receipt_dispatch_cancel_are_separate',composite_requisition_app,monkeypatch,_p181_published_map_identity,False,False)


def test_ordinary_historical_direct_completion_coverage(delivery_api_app):
    from datetime import datetime
    from app.models.production import ProductionTask,ProductionCompletionBatch,ProductionCompletion
    app,factory=delivery_api_app
    with factory() as db:
        task=ProductionTask(order_item_id=1,status='completed',planned_quantity=80,finished_coverage_snapshot=0,
            ordered_quantity_snapshot=100,material_received_quantity=80,material_input_quantity=80,output_factor=1,version=1)
        batch=ProductionCompletionBatch(idempotency_key='command-old-direct-fact',request_hash='a'*64,item_count=1,completed_at=datetime.now())
        db.add_all([task,batch]);db.flush()
        db.add(ProductionCompletion(batch_id=batch.id,task_id=task.id,order_item_id=1,expected_version=1,quantity=80,
            completion_type='primary',material_input_quantity=80,planned_output_quantity=80,actual_output_quantity=80,defective_quantity=0,
            order_reserved_quantity=80,direct_delivery_quantity=80,stock_quantity=0,surplus_finished_quantity=0,initial_disposition='direct',status='posted',completed_at=datetime.now()))
        db.commit()
    with TestClient(app) as c:
        _login(c,'admin');payload=_create_payload();payload['items']=payload['items'][:1]
        created=c.post('/api/deliveries',json=payload);assert created.status_code==201,created.text
        did=created.json()['id'];body=command(c,did,'old-direct-command');sent=c.put(f'/api/deliveries/{did}/dispatch',json=body);assert sent.status_code==200,sent.text
        receipt=sent.json()['dispatch_receipt'];assert receipt['movements']==[] and receipt['direct_component_allocations']==[]
        coverage=receipt['direct_completion_coverage'];assert len(coverage)==1 and coverage[0]['credited_quantity']==30 and coverage[0]['available_before']==60
        after=state(factory);assert c.put(f'/api/deliveries/{did}/dispatch',json=body).json()==sent.json() and state(factory)==after
        from tests.test_delivery_dispatch_commands import resolve
        assert resolve(c,did,body).json()['status']=='completed'
        import json
        from app.models.finance import FinanceIdempotencyRecord
        with factory() as db:
            record=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key==body['idempotency_key']))
            damaged=json.loads(record.response_json);half=copy.deepcopy(damaged['dispatch_receipt']['direct_completion_coverage'][0]);half['credited_quantity']=15
            damaged['dispatch_receipt']['direct_completion_coverage']=[half,copy.deepcopy(half)]
            record.response_json=json.dumps(damaged);db.commit()
        before=state(factory);assert resolve(c,did,body).json()['status']=='trace' and state(factory)==before


def test_composite_direct_only_completion_conservation(composite_requisition_app):
    from datetime import datetime
    from app.api.deliveries import router
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.production import ProductionTask,ProductionCompletionBatch,ProductionCompletion
    from tests.test_n039_composite_bom_requisition import _login as login
    from app.core.time_contract import beijing_today
    app,factory=composite_requisition_app;app.include_router(router,prefix='/api/deliveries')
    with factory() as db:
        for component in db.scalars(select(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.sales_order_item_id==1)):
            qty=int(component.required_piece_quantity)
            task=ProductionTask(order_item_id=1,sales_order_item_bom_component_id=component.id,task_role='component_internal',status='completed',planned_quantity=qty,
                finished_coverage_snapshot=0,ordered_quantity_snapshot=qty,material_received_quantity=qty,material_input_quantity=qty,output_factor=1,version=1)
            batch=ProductionCompletionBatch(idempotency_key=f'component-direct-{component.id}',request_hash='b'*64,item_count=1,completed_at=datetime.now())
            db.add_all([task,batch]);db.flush()
            db.add(ProductionCompletion(batch_id=batch.id,task_id=task.id,order_item_id=1,expected_version=1,quantity=qty,completion_type='primary',material_input_quantity=qty,
                planned_output_quantity=qty,actual_output_quantity=qty,defective_quantity=0,order_reserved_quantity=qty,direct_delivery_quantity=qty,stock_quantity=0,
                surplus_finished_quantity=0,initial_disposition='direct',status='posted',completed_at=datetime.now()))
        db.commit()
    with TestClient(app) as c:
        login(c);created=c.post('/api/deliveries',json=dict(customer_id=1,delivery_date=beijing_today().isoformat(),items=[dict(order_item_id=1,delivered_quantity=10)]));assert created.status_code==201,created.text
        did=created.json()['id'];body=command(c,did,'component-direct-command');sent=c.put(f'/api/deliveries/{did}/dispatch',json=body);assert sent.status_code==200,sent.text
        receipt=sent.json()['dispatch_receipt'];assert not receipt['movements'] and receipt['direct_component_allocations']
        from tests.test_delivery_dispatch_commands import resolve
        assert resolve(c,did,body).json()['status']=='completed'
        import json
        from app.models.finance import FinanceIdempotencyRecord
        with factory() as db:
            record=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key==body['idempotency_key']))
            damaged=json.loads(record.response_json);rows=damaged['dispatch_receipt']['direct_component_allocations'];half=copy.deepcopy(rows[0]);half['physical_quantity']//=2
            damaged['dispatch_receipt']['direct_component_allocations']=[half,copy.deepcopy(half),*rows[1:]]
            record.response_json=json.dumps(damaged);db.commit()
        before=state(factory);assert resolve(c,did,body).json()['status']=='trace' and state(factory)==before

from tests.test_semi_finished_order_reservation import b1_app
from tests.test_p1_40b_external_packaging_routing import routing_app


def test_legacy_liner_semi_consumption_conservation(b1_app):
    from app.api.deliveries import router
    from app.core.time_contract import beijing_today
    from app.models.product import Product
    from app.models.warehouse_inventory import InventoryLot
    from tests.test_semi_finished_order_reservation import add_semi_lot,login,post_order,order_item,semi_plan
    from tests.test_delivery_dispatch_commands import resolve
    app,factory=b1_app;app.include_router(router,prefix='/api/deliveries')
    lot_id,version=add_semi_lot(factory,quantity=12,key='command-liner-semi')
    with factory() as db:
        product=db.get(Product,1);product.box_style='衬板';product.crease_type='净料'
        db.get(InventoryLot,lot_id).semi_finished_detail.crease_type='净料';db.commit()
    with TestClient(app) as c:
        login(c,'admin');order=post_order(c,[order_item(1,5,{'semi':[semi_plan(lot_id,version,5)]})],'COMMAND-LINER')
        assert order.status_code==201,order.text
        oid=order.json()['items'][0]['id']
        created=c.post('/api/deliveries',json=dict(customer_id=1,delivery_date=beijing_today().isoformat(),items=[dict(order_item_id=oid,delivered_quantity=5)]))
        assert created.status_code==201,created.text
        did=created.json()['id'];body=command(c,did,'legacy-liner-command');sent=c.put(f'/api/deliveries/{did}/dispatch',json=body)
        assert sent.status_code==200,sent.text
        receipt=sent.json()['dispatch_receipt'];assert receipt['movements'] and all(r['source_kind']=='semi' for r in receipt['movements'])
        assert sum(r['physical_quantity'] for r in receipt['movements'])==5
        assert resolve(c,did,body).json()['status']=='completed'
        after=state(factory);assert c.put(f'/api/deliveries/{did}/dispatch',json=body).json()==sent.json() and state(factory)==after


def test_order_finished_reservation_dual_quantity_credit(routing_app):
    from app.api.deliveries import router
    from app.core.time_contract import beijing_today
    from app.models.order import OrderItem
    from tests.test_direct_external_finished import prepare,receive
    from tests.test_delivery_dispatch_commands import resolve
    routing_app.include_router(router,prefix='/api/deliveries')
    with TestClient(routing_app) as c:
        oid,pid,line=prepare(routing_app,c,ratio='2')
        assert receive(c,pid,line,200).status_code==200
        with routing_app.state.factory() as db:item_id=db.scalar(select(OrderItem.id).where(OrderItem.order_id==oid))
        created=c.post('/api/deliveries',json=dict(customer_id=routing_app.state.fixture['customer_a'],delivery_date=beijing_today().isoformat(),items=[dict(order_item_id=item_id,delivered_quantity=100)]))
        assert created.status_code==201,created.text
        did=created.json()['id'];body=command(c,did,'order-dual-credit-command');sent=c.put(f'/api/deliveries/{did}/dispatch',json=body)
        assert sent.status_code==200,sent.text
        receipt=sent.json()['dispatch_receipt'];row=receipt['final_items'][0]
        assert row['customer_quantity']==100 and row['physical_quantity']==200
        assert sum(r['physical_quantity'] for r in receipt['movements'])==200
        from fractions import Fraction
        assert sum(Fraction(r['requirement_quantity'],r['requirement_denominator']) for r in receipt['movements'])==100
        assert resolve(c,did,body).json()['status']=='completed'
        after=state(routing_app.state.factory);assert c.put(f'/api/deliveries/{did}/dispatch',json=body).json()==sent.json() and state(routing_app.state.factory)==after
