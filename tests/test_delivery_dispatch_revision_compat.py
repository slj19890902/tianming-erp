"""Explicit new external command; existing controlled revision still owns one CAS."""
import copy
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from tests.test_delivery_dispatch_commands import delivery_api_app,command,seed,state,_login
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
