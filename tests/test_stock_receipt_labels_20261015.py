import copy
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.models.customer import Customer
from app.models.product import Product
from app.models.stock_preparation import StockPreparationJob
from app.models.production_label_print import ProductionPackagingLabelPrintJob
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation
from test_stock_processing_auto import receive, stock_replenishment_app


def setup(client, factory):
    receive(client, quantity=20)
    with factory() as db:
        job = db.scalar(select(StockPreparationJob))
        product = db.get(Product, job.product_id)
        product.production_label_enabled = True
        product.production_label_units_per_label = 6
        product.version += 1
        db.get(Customer, product.customer_id).chinese_short_name = '隔离研光'
        receipt_id = job.receipt_item_id
        db.commit()
    return receipt_id


def inventory(factory):
    with factory() as db:
        return {m.__tablename__: list(db.execute(select(*m.__table__.columns).order_by(*m.__table__.primary_key.columns)))
                for m in (InventoryLot, InventoryMovement, InventoryReservation, StockPreparationJob, IncomingReceiptItem)}


def preview(client, receipt):
    response = client.get(f'/api/incoming/receipt-items/{receipt}/product-label-package')
    assert response.status_code == 200, response.text
    return response.json()


def body(package):
    return dict(idempotency_key='stock-label-job-test', confirmed=True,
                plan_fingerprint=package['plan_fingerprint'],
                items=[dict(selection_key=p['selection_key'],print_label_count=p['label_count']) for p in package['plans']])


def test_actual_receipt_labels_prepare_confirm_replay_without_stock_writes(stock_replenishment_app):
    app,factory=stock_replenishment_app
    with TestClient(app) as client:
        receipt=setup(client,factory)
        from app.core.receipt_price_guard import ReceiptPriceGuardSession
        factory.class_ = ReceiptPriceGuardSession
        before=inventory(factory)
        p=preview(client,receipt)
        assert p['label_count']==4 and [x['quantity'] for x in p['labels']]==[6,6,6,2]
        endpoint=f'/api/incoming/receipt-items/{receipt}/product-label-jobs'
        request=body(p)
        result=client.post(endpoint,json=request)
        assert result.status_code==200,result.text
        job=result.json()['job_id']
        assert client.post(endpoint,json=request).json()['job_id']==job
        altered=copy.deepcopy(request);altered['items'][0]['print_label_count']=1
        assert client.post(endpoint,json=altered).status_code==409
        confirm=f'/api/requisition/production-packaging-label-jobs/{job}/confirm'
        printed=client.post(confirm,json=dict(idempotency_key='stock-label-confirm',confirmed=True))
        assert printed.status_code==200,printed.text
        assert printed.json()['status']=='printed'
        assert client.post(confirm,json=dict(idempotency_key='stock-label-confirm',confirmed=True)).status_code==200
        with factory() as db:
            product=db.get(Product,p['plans'][0]['product_id']);product.production_label_units_per_label=10;product.version+=1;db.commit()
        frozen=client.get(f'/api/requisition/production-packaging-label-jobs/{job}')
        assert frozen.status_code==200,frozen.text
        assert frozen.json()['package']['label_count']==4
        assert inventory(factory)==before


@pytest.mark.parametrize('change',['product','receipt','source','customer_scope'])
def test_stock_label_stale_and_scope_are_rejected(stock_replenishment_app, change):
    app,factory=stock_replenishment_app
    with TestClient(app) as client:
        receipt=setup(client,factory);p=preview(client,receipt)
        with factory() as db:
            if change=='product':
                product=db.get(Product,p['plans'][0]['product_id']);product.version+=1
            elif change=='receipt':db.get(IncomingReceiptItem,receipt).status='reversed'
            elif change=='source':
                from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
                db.get(SupplierRequisitionOrderItem,p['supplier_order_item_id']).version += 1
            else:
                from app.api import incoming
                def deny(*args):
                    from fastapi import HTTPException
                    raise HTTPException(403,'scope')
                # Scoped-access boundary, without inventing a second login.
                from unittest.mock import patch
                with patch.object(incoming,'require_customer_access',deny):
                    assert client.get(f'/api/incoming/receipt-items/{receipt}/product-label-package').status_code==403
                return
            db.commit()
        result=client.post(f'/api/incoming/receipt-items/{receipt}/product-label-jobs',json=body(p))
        assert result.status_code==409,result.text
        with factory() as db:assert not list(db.scalars(select(ProductionPackagingLabelPrintJob)))


def test_incoming_stock_checkbox_opens_real_receipt_label_route(tmp_path):
    import json, shutil, subprocess
    from pathlib import Path
    index=(Path(__file__).resolve().parents[1]/'static/index.html').read_text()
    def method(start,end):
        return index.split(start,1)[1].split(end,1)[0]
    can='canPrintIncomingProductLabel(row) {'+method('canPrintIncomingProductLabel(row) {','          incomingProductLabelBlockMessage(row) {')
    action='async openSelectedIncomingProductLabels() {'+method('async openSelectedIncomingProductLabels() {','          async openIncomingProductionCard(row) {')
    script=tmp_path/'stock-label-entry.mjs'
    script.write_text('''
const methods={'''+can+action+'''};
const selected={receipt_item_id:540,receipt_status:'posted',source_type:'stock_replenishment',stock_replenishment_item_id:24,receipt_number:'IR-540'};
const urls=[],windows=[];
globalThis.axios={get:async url=>{urls.push(url);return {data:{label_count:167,plans:[{}],excluded_items:[]}};}};
globalThis.confirm=()=>{throw Error("preview must not require a background confirmation")};
globalThis.window={open:url=>{const w={location:{href:url},close(){throw Error("preview closed")}};windows.push(w);return w;}};
const vm={...methods,incomingProductionCardSelections:{540:selected},incomingReceived:[selected],incomingHistory:[],incomingProductionCardBatchBusy:false,authGeneration:1,user:{id:1},activePage:'incoming',incomingTab:'received',showToast(){},errorMessage:e=>String(e)};
const enabled=vm.canPrintIncomingProductLabel(selected);
const reversed=vm.canPrintIncomingProductLabel({...selected,receipt_status:'reversed'});
const success=await vm.openSelectedIncomingProductLabels();
process.stdout.write(JSON.stringify({enabled,reversed,success,urls,windows:windows.map(w=>w.location.href)}));
''')
    result=subprocess.run([shutil.which('node'),str(script)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==dict(enabled=True,reversed=False,success=True,
        urls=[],
        windows=['/production-packaging-label.html?stock_receipt_id=540'])
