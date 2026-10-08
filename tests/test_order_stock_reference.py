from datetime import date, timedelta
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient

from app.models.delivery import Delivery, DeliveryItem
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot
from app.services.delivery_quantities import encode
from app.services.order_stock_reference import stock_reference
from tests.test_p1_18_pdf_inventory_contract import (
    inventory_preview_app, _login, _finished_plan, _preview_item, _database_snapshot,
)


def shipment(db, product, quantities, *, index=0, status='dispatched', unit='只', contract=None):
    header=Delivery(delivery_number=f'REF-{index}', customer_id=product.customer_id,
        delivery_date=date(2026,10,1)+timedelta(days=index), status=status,
        source_mode='unordered_finished', total_quantity=sum(quantities))
    db.add(header);db.flush()
    for qty in quantities:
        db.add(DeliveryItem(delivery_id=header.id, source_type='unordered_finished',
            product_id=product.id, product_code_snapshot=product.product_code,
            product_name_snapshot=product.product_name, unit_snapshot=unit,
            price_source='manual',delivered_quantity=qty,
            quantity_contract_json=encode(contract,qty) if contract else None))
    db.flush()
    return header


def reference(db, p, remaining, unit='只'):
    return stock_reference(db, customer_id=p.customer_id, product_id=p.id,
        product_code=p.product_code, quantity_unit=unit,remaining_quantity=remaining)


@pytest.mark.parametrize('remaining,status', [(29,'red'),(30,'yellow'),(49,'yellow'),(50,'green')])
def test_reference_thresholds_group_document_and_exclude_invalid_status(inventory_preview_app,remaining,status):
    _,factory,ids=inventory_preview_app
    with factory() as db:
        p=db.get(Product,ids['shared_product'])
        shipment(db,p,[10,20],index=1);shipment(db,p,[50],index=2);shipment(db,p,[45],index=3)
        shipment(db,p,[10000],index=4,status='voided');shipment(db,p,[10000],index=5,status='pending')
        db.commit()
        result=reference(db,p,remaining)
        assert (result['status'],result['min_quantity'],result['max_quantity'],result['sample_count'])==(status,30,50,3)
        assert [r['quantity'] for r in result['samples']]==[45,50,30]
        assert result['quantity_unit']=='只'


def test_reference_latest_five_and_single_equal_and_empty(inventory_preview_app):
    _,factory,ids=inventory_preview_app
    with factory() as db:
        p=db.get(Product,ids['shared_product'])
        assert reference(db,p,100)['status']=='unknown'
        shipment(db,p,[30],index=0);db.commit()
        assert reference(db,p,29)['status']=='red'
        assert reference(db,p,30)['status']=='green'
        for i in range(1,6):shipment(db,p,[50],index=i)
        db.commit()
        result=reference(db,p,49)
        assert result['status']=='red' and result['min_quantity']==result['max_quantity']==50
        assert result['sample_count']==5


def test_reference_uses_frozen_ratio_and_rejects_missing_or_damaged_units(inventory_preview_app):
    _,factory,ids=inventory_preview_app
    with factory() as db:
        p=db.get(Product,ids['shared_product'])
        basis=dict(schema=1,customer_id=p.customer_id,product_id=p.id,customer_unit='箱',
            physical_unit='片',customer_basis=1,physical_basis=2,source='order',source_version=1)
        shipment(db,p,[15],index=1,unit='箱',contract=basis);db.commit()
        assert reference(db,p,30,'片')['status']=='green'
        assert reference(db,p,29,'片')['max_quantity']==30
        # The current master never repairs a missing historical ratio.
        p.unit='主档新单位'
        assert reference(db,p,30,'片')['max_quantity']==30
        shipment(db,p,[5],index=2,unit='箱');db.commit()
        assert reference(db,p,1000,'片')['status']=='unknown'
        newest=db.scalar(select(DeliveryItem).order_by(DeliveryItem.id.desc()))
        newest.quantity_contract_json='{"schema":1}';db.commit()
        assert reference(db,p,1000,'片')['status']=='unknown'


def test_preview_batch_final_remaining_and_skipped_stock_are_read_only(inventory_preview_app):
    app,factory,ids=inventory_preview_app
    with factory() as db:
        p=db.get(Product,ids['shared_product'])
        lot=db.get(InventoryLot,ids['shared_lot'])
        lot.quantity_available=160;lot.quantity_reserved=40
        for i,qty in enumerate([30,50,45]):shipment(db,p,[qty],index=i)
        db.commit()
    before=_database_snapshot(factory)
    request=dict(customer_id=ids['customer'],items=[
        _preview_item(line='A',product_id=ids['shared_product'],quantity=70,
            finished=[_finished_plan(ids['shared_lot'],70)]),
        _preview_item(line='B',product_id=ids['shared_product'],quantity=50,
            finished=[_finished_plan(ids['shared_lot'],50)]),
        {**_preview_item(line='C',product_id=ids['shared_product'],quantity=10), 'finished_skipped':True}])
    with TestClient(app) as client:
        _login(client,'preview-allowed')
        response=client.post('/api/orders/inventory-draft-preview',json=request)
    assert response.status_code==200,response.text
    rows=response.json()['items']
    assert [r['finished_stock_batch_remaining_quantity'] for r in rows]==[40,40,40]
    assert [r['finished_stock_reference']['status'] for r in rows]==['yellow']*3
    assert rows[2]['finished_stock_available_quantity']==160
    assert rows[2]['finished_stock_on_hand_quantity']==200
    assert _database_snapshot(factory)==before
