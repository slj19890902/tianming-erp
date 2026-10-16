import copy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.customer import Customer
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.production_label_print import ProductionPackagingLabelPrintJob, ProductionPackagingLabelPrintJobTask
from app.models.procurement_source import ProcurementSourceLink
from app.models.stock_replenishment import StockReplenishmentOrderItem, StockReplenishmentOrder
from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation
from test_stock_replenishment_flow import stock_replenishment_app, _login, _customer_replenishment_payload
from tests.test_p1_32a2_requisition_production_print import production_print_app


def reported(client, factory, quantity=23):
    _login(client)
    with factory() as db:
        product = db.get(Product, 1)
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        db.get(Customer, 1).chinese_short_name = "天华"
        db.commit()
    created = client.post('/api/requisition/stock-replenishment/orders', json=_customer_replenishment_payload(quantity))
    assert created.status_code == 201, created.text
    item = created.json()['items'][0]
    row = next(r for r in client.get('/api/requisition/pending').json()['items'] if r.get('stock_replenishment_item_id') == item['id'])
    purchase = client.post('/api/requisition/supplier-orders/from-pending-selection', json={
        'supplier_groups': [{'supplier_name': row['supplier_name'], 'request_key': str(uuid4()),
            'stock_sources': [{'stock_replenishment_item_id': item['id'], 'source_fingerprint': row['source_fingerprint']}]}]})
    assert purchase.status_code == 201, purchase.text
    with factory() as db:
        link = db.scalar(select(ProcurementSourceLink).where(ProcurementSourceLink.stock_replenishment_item_id == item['id']))
        line = db.get(SupplierRequisitionOrderItem, link.supplier_item_id)
        return line.supplier_order_id, line.id, item['id']


def preview(client, order, item):
    response = client.get(f'/api/requisition/supplier-orders/{order}/production-packaging-label-package', params={'item_ids': str(item)})
    assert response.status_code == 200, response.text
    return response.json()


def request(package, key='reported-stock-label', count=None):
    return dict(idempotency_key=key, confirmed=True, plan_fingerprint=package['plan_fingerprint'],
        items=[dict(selection_key=p['selection_key'], print_label_count=p['label_count'] if count is None else count) for p in package['plans']])


def business_snapshot(factory):
    with factory() as db:
        return {m.__tablename__: list(db.execute(select(*m.__table__.columns).order_by(*m.__table__.primary_key.columns)))
            for m in (ProductionTask, StockReplenishmentOrder, StockReplenishmentOrderItem,
                SupplierRequisitionOrderItem, ProcurementSourceLink, IncomingReceiptItem,
                InventoryLot, InventoryMovement, InventoryReservation)}


def test_reported_stock_without_task_or_receipt_prints_and_freezes(stock_replenishment_app):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        order, item, _ = reported(client, factory)
        from app.core.receipt_price_guard import ReceiptPriceGuardSession
        factory.class_ = ReceiptPriceGuardSession
        before = business_snapshot(factory)
        p = preview(client, order, item)
        assert p['source_type'] == 'supplier_sources'
        assert [label['quantity'] for label in p['labels']] == [5, 5, 5, 5, 3]
        assert p['plans'][0]['production_task_id'] is None
        endpoint = f'/api/requisition/supplier-orders/{order}/production-packaging-label-jobs'
        body = request(p)
        prepared = client.post(endpoint, json=body)
        assert prepared.status_code == 200, prepared.text
        job_id = prepared.json()['job_id']
        assert client.post(endpoint, json=body).json()['job_id'] == job_id
        changed = copy.deepcopy(body); changed['items'][0]['print_label_count'] = 2
        assert client.post(endpoint, json=changed).status_code == 409
        confirmed = client.post(f'/api/requisition/production-packaging-label-jobs/{job_id}/confirm',
            json=dict(idempotency_key='reported-label-printed', confirmed=True))
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()['status'] == 'printed'
        assert business_snapshot(factory) == before
        with factory() as db:
            assert not list(db.scalars(select(ProductionTask)))
            assert not list(db.scalars(select(ProductionPackagingLabelPrintJobTask)))
            product = db.get(Product, 1); product.production_label_units_per_label = 10; product.version += 1
            db.commit()
        frozen = client.get(f'/api/requisition/production-packaging-label-jobs/{job_id}')
        assert frozen.status_code == 200, frozen.text
        assert [r['quantity'] for r in frozen.json()['package']['labels']] == [5, 5, 5, 5, 3]


@pytest.mark.parametrize('change', ['product', 'source', 'link', 'customer', 'disabled', 'layout'])
def test_source_changes_reject_prepared_confirmation(stock_replenishment_app, change):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        order, item, stock = reported(client, factory)
        p = preview(client, order, item)
        result = client.post(f'/api/requisition/supplier-orders/{order}/production-packaging-label-jobs', json=request(p))
        assert result.status_code == 200, result.text
        job = result.json()['job_id']
        with factory() as db:
            if change == 'product': db.get(Product, 1).version += 1
            elif change == 'source': db.get(SupplierRequisitionOrderItem, item).version += 1
            elif change == 'link': db.scalar(select(ProcurementSourceLink)).status = 'voided'
            elif change == 'customer': db.get(Customer, 1).version += 1
            elif change == 'disabled':
                db.get(Product, 1).production_label_enabled = False
                db.get(Product, 1).production_label_units_per_label = None
            else:
                from app.services.production_packaging_label_layout import default_layout, save_draft, publish_draft
                layout = default_layout(); layout['elements'][0]['font_size_mm'] -= .1
                save_draft(db, layout=layout, expected_draft_version=0, operation_key='source-label-draft', actor_id=1)
                publish_draft(db, expected_draft_version=1, expected_release_version=0, operation_key='source-label-layout', actor_id=1)
            db.commit()
        confirm = client.post(f'/api/requisition/production-packaging-label-jobs/{job}/confirm',
            json=dict(idempotency_key='stale-label-confirm', confirmed=True))
        assert confirm.status_code == 409, confirm.text
        with factory() as db: assert db.get(ProductionPackagingLabelPrintJob, job).status == 'prepared'


def test_stock_batch_selection_counts_and_confirm(stock_replenishment_app):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        first = reported(client, factory, 23)
        second = reported(client, factory, 12)
        ids = [first[0], second[0]]
        response = client.get('/api/requisition/supplier-order-label-batches/package',
            params=dict(order_ids=','.join(map(str, ids)), item_ids=f'{first[1]},{second[1]}'))
        assert response.status_code == 200, response.text
        p = response.json(); body = request(p, count=1); body['order_ids'] = ids
        endpoint = '/api/requisition/supplier-order-label-batches/production-packaging-label-jobs'
        result = client.post(endpoint, json=body)
        assert result.status_code == 200, result.text
        assert result.json()['package']['label_count'] == 2
        replay = client.post(endpoint, json=body)
        assert replay.status_code == 200 and replay.json()['replayed']
        job_ids = [r['job_id'] for r in result.json()['jobs']]
        confirm = client.post('/api/requisition/supplier-order-label-batches/confirm',
            json=dict(job_ids=job_ids, idempotency_key='stock-batch-confirm', confirmed=True))
        assert confirm.status_code == 200, confirm.text


def test_disabled_product_does_not_open_label_page(stock_replenishment_app):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        order, item, _ = reported(client, factory)
        with factory() as db:
            db.get(Product, 1).production_label_enabled = False
            db.get(Product, 1).production_label_units_per_label = None
            db.commit()
        result = client.get(f'/api/requisition/supplier-orders/{order}/production-packaging-label-package', params=dict(item_ids=str(item)))
        assert result.status_code == 409 and '未启用' in result.text


@pytest.mark.parametrize('stock_count', [0, 2])
def test_mixed_sales_and_stock_sources_keep_only_real_task_links(production_print_app, stock_count):
    import json
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.stock_purchase_identity import capture
    from tests.test_p1_50c_task_label_plan_template import _enable_one_frozen_plan
    from tests.test_p1_32a2_requisition_production_print import _login as sales_login
    fixture = production_print_app
    task_id, _, _ = _enable_one_frozen_plan(fixture)
    with fixture['session_factory']() as db:
        product = db.get(Product, fixture['product_id'])
        order = db.get(SupplierRequisitionOrder, fixture['supplier_order_id'])
        original = next(i for i in order.items if i.order_item_id == fixture['order_item_id'])
        stock = StockReplenishmentOrder(order_number='STOCK-LABEL-MIXED', source_type='customer_request',
            status='confirmed', customer_id=product.customer_id)
        db.add(stock); db.flush()
        item = StockReplenishmentOrderItem(replenishment_order_id=stock.id, target_inventory_type='semi_finished',
            reference_product_id=product.id, customer_id=product.customer_id,
            product_code_snapshot=product.product_code, product_name_snapshot=product.product_name,
            material_code_snapshot=original.material_code_snapshot, layer_count=original.layer_count_snapshot,
            flute_type=original.flute_type_snapshot, report_length_mm=original.report_length_mm,
            report_width_mm=original.report_width_mm, quantity=23, stocked_quantity=0,
            pieces_per_box=1, stock_yield_per_sheet=4, component_type='whole', sheet_type='net_sheet')
        db.add(item); db.flush()
        item.production_snapshot_json=json.dumps(capture(product, item), ensure_ascii=False)
        line = SupplierRequisitionOrderItem(supplier_order_id=order.id, source_key=f'stock_replenishment:{item.id}',
            product_id=product.id, product_code=product.product_code, product_name=product.product_name,
            quantity=23, requisition_qty=23, stock_deduction_qty=0, required_piece_qty=92,
            material_code_snapshot=item.material_code_snapshot, layer_count_snapshot=item.layer_count,
            flute_type_snapshot=item.flute_type, report_length_mm=item.report_length_mm,
            report_width_mm=item.report_width_mm, status='active', version=1)
        db.add(line); db.flush()
        db.add(ProcurementSourceLink(supplier_item_id=line.id, stock_replenishment_item_id=item.id,
            source_quantity=23, source_snapshot_json='{}', status='active', created_by=order.created_by))
        db.commit(); order_id=order.id; selected=f'{original.id},{line.id}'
    with TestClient(fixture['app']) as client:
        sales_login(client, 'p132a2-admin')
        p=preview(client, order_id, selected)
        stock_plan=next(p for p in p['plans'] if p['production_task_id'] is None)
        assert stock_plan['total_quantity']==92
        body=request(p)
        for item in body['items']:
            item['print_label_count']=stock_count if item['selection_key'].startswith('stock-item:') else 1
        result=client.post(f'/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs', json=body)
        assert result.status_code==200,result.text
        job=result.json()['job_id']
        assert result.json()['package']['label_count']==stock_count+1
        confirmed=client.post(f'/api/requisition/production-packaging-label-jobs/{job}/confirm',
            json=dict(idempotency_key='mixed-confirm',confirmed=True))
        assert confirmed.status_code==200,confirmed.text
    with fixture['session_factory']() as db:
        assert [l.production_task_id for l in db.scalars(select(ProductionPackagingLabelPrintJobTask).where(
            ProductionPackagingLabelPrintJobTask.print_job_id==job))]==[task_id]


def test_stock_label_access_and_audit_failure_are_atomic(stock_replenishment_app, monkeypatch):
    from app.api import requisition
    from fastapi import HTTPException
    app,factory=stock_replenishment_app
    with TestClient(app, raise_server_exceptions=False) as client:
        order,item,_=reported(client,factory)
        p=preview(client,order,item)
        endpoint=f'/api/requisition/supplier-orders/{order}/production-packaging-label-jobs'
        original=requisition._require_supplier_order_customer_access
        def deny(*args): raise HTTPException(403,'scope')
        monkeypatch.setattr(requisition,'_require_supplier_order_customer_access',deny)
        assert client.post(endpoint,json=request(p)).status_code==403
        monkeypatch.setattr(requisition,'_require_supplier_order_customer_access',original)
        def audit_failure(*args,**kwargs): raise RuntimeError('injected audit failure')
        monkeypatch.setattr(requisition,'append_audit_event',audit_failure)
        assert client.post(endpoint,json=request(p)).status_code==500
        with factory() as db: assert not list(db.scalars(select(ProductionPackagingLabelPrintJob)))


def test_supplier_page_sends_source_keys_instead_of_fake_task_ids(tmp_path):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    page=(Path(__file__).resolve().parents[1]/'static/production-packaging-label.html').read_text()
    function='async function preparePrintJob() {'+page.split('async function preparePrintJob() {',1)[1].split('      async function startPrint()',1)[0]
    script=tmp_path/'source-label.mjs'
    script.write_text('''
let prepareAttempt=null;
const supplierOrderId=211,stockReceiptId=0,deliveryId=0,compositeBatchId=0,supplierOrderIds=[],compositeItemIds=[625];
const plannedPackage={plans:[{selection_key:'stock-item:625',production_task_id:null,label_count:47},{selection_key:'task:17',production_task_id:17,label_count:3}]};
const loadedPackage={template_version:'current_40x30_v2'};
const printCounts=new Map([['stock-item:625',2],['task:17',1]]);
const planSelectionKey=p=>p.selection_key;
const parsePrintCount=Number;
const planFingerprint=()=> 'a'.repeat(64);
const newIdempotencyKey=()=> 'source-ui-test';
const deepClone=x=>JSON.parse(JSON.stringify(x));
const setPrepareAttemptLocked=()=>{};
const positiveInteger=x=>Number.isInteger(x)&&x>0;
const normalizeResponse=x=>x;
let observed;
const requestJson=async(url,options)=>{observed={url,body:JSON.parse(options.body)};return {job:{id:1}};};
'''+function+'''\nawait preparePrintJob();process.stdout.write(JSON.stringify(observed));''')
    result=subprocess.run([shutil.which('node'),str(script)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    observed=json.loads(result.stdout)
    assert observed['url']=='/api/requisition/supplier-orders/211/production-packaging-label-jobs'
    assert observed['body']['items']==[
        dict(selection_key='stock-item:625',print_label_count=2),
        dict(selection_key='task:17',print_label_count=1)]
