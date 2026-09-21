from decimal import Decimal
from fastapi.testclient import TestClient
import pytest
from test_phase8_finance import finance_api_app, _login, _receipt_payload


def prepare(client, factory):
    from app.models.delivery import DeliveryItem, Delivery
    from app.models.finance import ReturnReceipt
    _login(client, 'finance')
    assert client.post('/api/finance/return_receipts', json=_receipt_payload()).status_code == 201
    with factory() as db:
        item = db.get(DeliveryItem, 1)
        item.source_type='unordered_finished'
        item.order_item_id=None
        item.product_id=1
        item.product_code_snapshot='MISSING'
        item.product_name_snapshot='待补价纸箱'
        item.unit_snapshot='个'
        item.price_source='pending'
        item.unit_price_snapshot=None
        item.sales_contract_json=None
        delivery=db.get(Delivery,1)
        delivery.status='dispatched'
        db.commit()
        receipt=db.query(ReturnReceipt).filter_by(delivery_id=1).one()
        return dict(expected_delivery_version=delivery.version, expected_receipt_version=receipt.version,
                    unit_price='2.3100',price_tax_mode='tax_exclusive',tax_rate='0.13',
                    note='按双方确认成交价首次补录',idempotency_key='supplement-price-1')


@pytest.mark.parametrize('tax_mode,amount',[('tax_inclusive','180.18'),('tax_exclusive','203.60')])
def test_supplement_unblocks_statement_and_replays_without_changing_quantities(finance_api_app,tax_mode,amount):
    from app.models.delivery import DeliveryItem
    from app.models.finance import ReturnReceiptItem
    app,factory=finance_api_app
    with TestClient(app) as client:
        payload=prepare(client,factory)
        payload['price_tax_mode']=tax_mode
        response=client.post('/api/finance/delivery-items/1/supplement-price',json=payload)
        assert response.status_code==200,response.text
        assert client.post('/api/finance/delivery-items/1/supplement-price',json=payload).json()==response.json()
        assert client.post('/api/finance/delivery-items/1/supplement-price',json={**payload,'unit_price':'9'}).status_code==409
        assert client.post('/api/finance/delivery-items/1/supplement-price',json={**payload,'idempotency_key':'another-price-1'}).status_code==409
        pending=client.get('/api/finance/pending_statements',params={'customer_id':1,'statement_month':'2026-06'}).json()
        assert pending['deliveries'][0]['selection_blocked'] is False
        assert Decimal(str(pending['deliveries'][0]['total_receivable_amount']))==Decimal(amount)
        queue=client.get('/api/finance/current-customer-months',params={'statement_month':'2026-06'}).json()
        assert Decimal(str(queue['items'][0]['pending_reconciliation_amount']))==Decimal(amount)
        made=client.post('/api/finance/statements',json={'customer_id':1,'statement_month':'2026-06','delivery_ids':[1]})
        assert made.status_code==201,made.text
        assert Decimal(str(made.json()['total_receivable']))==Decimal(amount)
    with factory() as db:
        assert db.get(DeliveryItem,1).delivered_quantity==80
        assert db.query(ReturnReceiptItem).one().actual_received_quantity==78


@pytest.mark.parametrize('field,value',[('unit_price','0'),('unit_price','-1'),('unit_price','NaN'),('tax_rate','1.5'),('price_tax_mode','unknown')])
def test_invalid_supplement_values(finance_api_app,field,value):
    app,factory=finance_api_app
    with TestClient(app) as client:
        payload=prepare(client,factory)
        response=client.post('/api/finance/delivery-items/1/supplement-price',json={**payload,field:value})
        assert response.status_code==422,response.text


def test_supplement_versions_permissions_and_audit_rollback(finance_api_app,monkeypatch):
    import app.api.finance as finance
    from app.models.delivery import DeliveryItem,Delivery
    from app.models.finance import ReturnReceipt
    app,factory=finance_api_app
    with TestClient(app,raise_server_exceptions=False) as client:
        payload=prepare(client,factory)
        for field in ['expected_delivery_version','expected_receipt_version']:
            assert client.post('/api/finance/delivery-items/1/supplement-price',json={**payload,field:99}).status_code==409
        _login(client,'workshop')
        assert client.post('/api/finance/delivery-items/1/supplement-price',json=payload).status_code==403
        _login(client,'finance')
        def fail(*args,**kwargs): raise RuntimeError('injected audit failure')
        monkeypatch.setattr(finance,'_audit',fail)
        assert client.post('/api/finance/delivery-items/1/supplement-price',json=payload).status_code==500
    with factory() as db:
        assert db.get(DeliveryItem,1).unit_price_snapshot is None
        assert db.get(Delivery,1).version==payload['expected_delivery_version']
        assert db.query(ReturnReceipt).one().version==payload['expected_receipt_version']


@pytest.mark.parametrize('blocked',['frozen','cancelled_receipt','not_dispatched','historical_item','statement'])
def test_supplement_never_overwrites_locked_facts(finance_api_app,blocked):
    from app.models.delivery import DeliveryItem,Delivery
    from app.models.finance import ReturnReceipt,ReturnReceiptItem,Statement,StatementItem
    from app.services.delivery_snapshots import sales_contract
    app,factory=finance_api_app
    with TestClient(app) as client:
        payload=prepare(client,factory)
        with factory() as db:
            item=db.get(DeliveryItem,1)
            if blocked=='frozen':
                item.sales_contract_json=sales_contract(unit='个',price='3',tax_mode='tax_inclusive',tax_rate='0.13',source={'kind':'original'})
            elif blocked=='cancelled_receipt': db.query(ReturnReceipt).one().status='cancelled'
            elif blocked=='not_dispatched': db.get(Delivery,1).status='pending'
            elif blocked=='historical_item': item.is_current=False
            else:
                statement=Statement(statement_number='ST-LOCK',customer_id=1,statement_month='2026-06',total_receivable=78,total_gross_profit=0,created_by=1)
                db.add(statement);db.flush()
                db.add(StatementItem(statement_id=statement.id,source_customer_id=1,
                    return_receipt_item_id=db.query(ReturnReceiptItem).one().id,actual_received_quantity=78,
                    unit_price_snapshot=1,unit_cost_snapshot=0,receivable_amount=78,gross_profit_amount=78))
            db.commit()
            original_contract=item.sales_contract_json
        response=client.post('/api/finance/delivery-items/1/supplement-price',json=payload)
        assert response.status_code==409,response.text
    with factory() as db:
        assert db.get(DeliveryItem,1).unit_price_snapshot is None
        assert db.get(DeliveryItem,1).sales_contract_json==original_contract
        assert db.get(Delivery,1).version==payload['expected_delivery_version']


def test_customer_scope_and_concurrent_retry(finance_api_app):
    from app.core.security import hash_password
    from app.models.user import User
    from app.models.audit import OperationLog
    from test_phase8_finance import _race_requests
    app,factory=finance_api_app
    with TestClient(app) as client:
        payload=prepare(client,factory)
        with factory() as db:
            db.add(User(username='scoped-finance',password_hash=hash_password('RolePass123!'),role='finance',
                real_name='scope',display_name='scope',must_change_password=False,customer_access_mode='selected'))
            db.commit()
        _login(client,'scoped-finance')
        assert client.post('/api/finance/delivery-items/1/supplement-price',json=payload).status_code==403
        _login(client,'finance')
        responses=_race_requests(*[lambda:client.post('/api/finance/delivery-items/1/supplement-price',json=payload) for _ in range(2)])
        assert all(r.status_code in (200,409) for r in responses)
        assert any(r.status_code==200 for r in responses)
        assert client.post('/api/finance/delivery-items/1/supplement-price',json=payload).status_code==200
    with factory() as db:
        assert db.query(OperationLog).filter_by(action='SUPPLEMENT_DELIVERY_SALES_PRICE').count()==1


def test_price_form_retries_one_frozen_request_and_does_not_resave_after_refresh_failure(tmp_path):
    import json
    import subprocess
    from pathlib import Path
    page=(Path(__file__).resolve().parents[1]/'static/index.html').read_text(encoding='utf-8')
    body=page.split('async saveStatementPriceSupplement() {',1)[1].split('\n          },',1)[0]
    script=f'''
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let keys=0;globalThis.createIdempotencyKey=()=>`key-${{++keys}}`;
const calls=[];globalThis.axios={{post:(url,payload)=>new Promise((resolve,reject)=>calls.push({{url,payload,resolve,reject}}))}};
const vm={{statementForm:{{customer_id:125,statement_month:'2026-08'}},modal:{{type:'statement'}},
statementPriceSupplement:{{item:{{delivery_item_id:201,delivery_version:3,receipt_version:2}},customerId:125,statementMonth:'2026-08',unit_price:'2.31',price_tax_mode:'tax_inclusive',tax_rate:'0.13'}},
showToast:()=>{{}},errorMessage:e=>String(e),loadPendingStatements:async()=>{{throw Error('refresh')}},loadFinance:async()=>{{}}}};
vm.save=new AsyncFunction({json.dumps(body)}).bind(vm);
(async()=>{{
 const first=vm.save();if(await vm.save()!==false||calls.length!==1)throw Error('duplicate click');
 calls[0].reject(Error('network'));await first;
 const second=vm.save();if(calls[1].payload!==calls[0].payload||keys!==1)throw Error('retry changed payload');
 calls[1].resolve({{data:{{}}}});if(await second!==true)throw Error('save failed');
 if(!vm.statementPriceSupplement.committed||await vm.save()!==false||calls.length!==2)throw Error('refresh failure resaved');
 vm.statementPriceSupplement.committed=false;vm.statementForm.customer_id=44;
 if(await vm.save()!==false||calls.length!==2)throw Error('cross customer submit');
}})().catch(e=>{{console.error(e);process.exit(1)}});
'''
    file=tmp_path/'price-form.cjs';file.write_text(script,encoding='utf-8')
    result=subprocess.run(['node',str(file)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
