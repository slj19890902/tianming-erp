import hashlib
import json
from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_stock_replenishment_flow import stock_replenishment_app, _customer_replenishment_payload
from test_stock_processing_auto import receive, row, completion
from app.api.stock_preparation import router as preparation_router
from app.api.product_workbench import router as workbench_router
from app.models.product import Product
from app.models.stock_replenishment import StockReplenishmentOrderItem as Item
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.stock_preparation import StockPreparationJob
from app.models.warehouse_inventory import InventoryLot, SemiFinishedLotAllowedProduct
from app.services.stock_purchase_identity import resolve, validate_material, VIEW_FIELDS
from app.services.warehouse_inventory import WarehouseInventoryError


def test_frozen_purchase_2500_times_four_receipt_completion_and_search(stock_replenishment_app):
    app,factory=stock_replenishment_app
    app.include_router(preparation_router,prefix='/api/production')
    app.include_router(workbench_router,prefix='/api/product-workbench')
    from app.api.deliveries import router as deliveries_router
    app.include_router(deliveries_router,prefix='/api/deliveries')
    with factory() as db:
        p=db.get(Product,1);p.product_code='80011965';db.commit()
    def change(_):
        with factory() as db:
            p=db.get(Product,1);p.report_width_mm=900;p.length_mm=999;p.production_process='新工艺';p.version+=1;db.commit()
    with TestClient(app) as client:
        payload=_customer_replenishment_payload(2500);payload['items'][0]['stock_yield_per_sheet']=4
        receive(client,payload,before_receive=change,quantity=2500)
        r=row(client);assert r['reserved']==2500 and r['jobs'][0]['expected_output']==10000
        with factory() as db:
            # Simulate the original receipt, which had provenance but no optional binding.
            receipt=db.get(IncomingReceiptItem,r['receipt_item_id'])
            for binding in db.scalars(select(SemiFinishedLotAllowedProduct).where(SemiFinishedLotAllowedProduct.inventory_lot_id==receipt.received_inventory_lot_id)):
                db.delete(binding)
            db.commit()
        result=client.get('/api/product-workbench/products/1')
        assert result.status_code==200,result.text
        data=result.json();assert data['inventory']['summary']['semi_finished']['actual']==2500
        material=next(x for x in data['activity']['items'] if x['source']=='补库收料')
        assert material['status']=='待生产' and material['theoretical_output']==10000 and material['physical_quantity']==2500
        assert '830' in material['frozen_spec'] and material['location'] not in ('尚未收料','位置未登记')
        finished_before=data['inventory']['summary']['finished']['actual']
        search=client.get('/api/product-workbench/search',params={'q':'80011965'}).json()
        assert search['items'][0]['inventory']['semi_finished']['actual']==2500
        body=completion(r,2500,'frozen-four-completion');body['output_kind']='finished'
        saved=client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body)
        assert saved.status_code==200,saved.text
        assert client.post(f"/api/production/stock-preparation/{r['receipt_item_id']}/actions",json=body).json()==saved.json()
        after=client.get('/api/product-workbench/products/1').json()
        assert after['inventory']['summary']['finished']['actual']==finished_before+10000
        assert after['inventory']['summary']['semi_finished']['actual']==0
        with factory() as db:
            job=db.get(StockPreparationJob,saved.json()['completed_job_id']);lot=db.get(InventoryLot,job.output_lot_id)
            frozen=json.loads(job.product_snapshot)
            assert lot.finished_detail.length_mm!=999
            assert lot.finished_detail.physical_basis_json==frozen['physical_basis']
            assert db.get(Product,1).report_width_mm==900
            output_id=lot.id
        shipped=client.post('/api/deliveries',json=dict(customer_id=1,delivery_date='2026-10-10',source_mode='unordered_finished',lines=[dict(
            source_type='finished_stock',product_id=1,delivered_quantity=40,unit_price='2.00',
            allocations=[dict(inventory_lot_id=output_id,quantity=40)])]))
        assert shipped.status_code==201,shipped.text
        delivery=shipped.json()
        from app.models.delivery import DeliveryItem
        with factory() as db:
            line=db.scalar(select(DeliveryItem).where(DeliveryItem.delivery_id==delivery['id']))
            assert line.specification_snapshot==json.loads(frozen['physical_basis'])['spec']
        dispatched=client.put(f"/api/deliveries/{delivery['id']}/dispatch")
        assert dispatched.status_code==200,dispatched.text
        final=client.get('/api/product-workbench/products/1').json()
        assert final['inventory']['summary']['finished']['actual']==finished_before+9960
        assert any(d['status']=='已送货' and d['quantity']==40 for d in final['activity']['deliveries'])


@pytest.mark.parametrize('corruption',['width','flute','yield','scope'])
def test_receipt_mismatch_and_customer_scope_still_rejected(stock_replenishment_app,corruption):
    app,factory=stock_replenishment_app
    app.include_router(preparation_router,prefix='/api/production')
    with TestClient(app) as client:
        item,_=receive(client);r=row(client)
    with factory() as db:
        item=db.get(Item,item['id']);receipt=db.get(IncomingReceiptItem,r['receipt_item_id'])
        lot=db.get(InventoryLot,receipt.received_inventory_lot_id)
        product=db.get(Product,1)
        field={'width':'board_width_mm','flute':'flute_type','yield':'stock_yield_per_sheet','scope':'owner_customer_id'}[corruption]
        setattr(lot.semi_finished_detail,field,'BC' if corruption=='flute' else 999)
        with pytest.raises(WarehouseInventoryError,match='原报料'):
            validate_material(db,item,lot,product)


def test_legacy_history_is_verified_and_does_not_rewrite_purchase(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(preparation_router,prefix='/api/production')
    with TestClient(app) as client:
        item,_=receive(client)
    from app.models.master_data_object_version import MasterDataObjectVersion
    with factory() as db:
        item=db.get(Item,item['id']);p=db.get(Product,1)
        fields=json.loads(item.production_snapshot_json)['fields']
        raw=json.dumps(fields,ensure_ascii=False,sort_keys=True)
        rev=MasterDataObjectVersion(object_type='product',object_id=1,version=1,action='CREATE',snapshot_json=raw,
            snapshot_sha256=hashlib.sha256(raw.encode()).hexdigest(),change_set_id='test-purchase-history',source='test',
            created_at=item.created_at-timedelta(seconds=1))
        db.add(rev);item.production_snapshot_json=None;p.report_width_mm=999;p.updated_at=item.created_at+timedelta(seconds=1);db.flush()
        identity=resolve(db,item,p)
        assert identity['source']=='master_history' and identity['fields']['report_width_mm']==830
        assert item.production_snapshot_json is None and p.report_width_mm==999
        rev.snapshot_sha256='0'*64
        with pytest.raises(WarehouseInventoryError,match='校验失败'):resolve(db,item,p)


def test_delivery_history_is_paginated_scoped_and_permission_gated(stock_replenishment_app):
    from datetime import date
    from app.models.delivery import Delivery,DeliveryItem
    from app.services.product_activity import activity
    app,factory=stock_replenishment_app
    with factory() as db:
        p=db.get(Product,1)
        for i in range(3):
            head=Delivery(customer_id=p.customer_id,delivery_number=f'FICTIONAL-DEL-{i}',delivery_date=date(2026,1,1),
                status='pending' if i==2 else 'dispatched',source_mode='unordered_finished')
            db.add(head);db.flush()
            db.add(DeliveryItem(delivery_id=head.id,source_type='unordered_finished',product_id=p.id,
                product_code_snapshot='FROZEN-CODE',product_name_snapshot='旧名称',specification_snapshot='旧规格',unit_snapshot='片',
                delivered_quantity=7,ordered_quantity_snapshot=0,order_remaining_snapshot=0,over_delivery_quantity=0,price_source='pending'))
        db.flush()
        allowed={k:True for k in ['warehouse','orders','requisition','incoming','production','deliveries']}
        one=activity(db,p,permissions=allowed,scope={p.customer_id},page_size=2)
        two=activity(db,p,permissions=allowed,scope={p.customer_id},page=2,page_size=2)
        assert one['has_more'] and len(one['deliveries'])==2 and len(two['deliveries'])==1
        assert one['deliveries'][0]['status']=='待送货'
        assert all(r['specification']=='旧规格' and r['quantity']==7 for r in one['deliveries']+two['deliveries'])
        denied=activity(db,p,permissions={k:False for k in allowed},scope={p.customer_id})
        assert denied['items']==[] and denied['deliveries']==[] and '送货记录' in denied['hidden_sections']
