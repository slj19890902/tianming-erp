from decimal import Decimal
import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
from test_phase8_finance import finance_api_app, _login, _receipt_payload


def test_queues_show_only_matching_bills_and_completed_legacy_facts(finance_api_app):
    from app.models.finance import Statement
    app, factory = finance_api_app
    with factory() as db:
        for number, confirmation, invoice, paid in [
            ('DRAFT', 'draft', 0, 0), ('PAY', 'confirmed', 100, 20),
            ('DONE-LEGACY', 'draft', 100, 100),
        ]:
            db.add(Statement(statement_number=number, customer_id=1, statement_month='2026-08',
                total_receivable=Decimal('100'), total_gross_profit=0,
                invoiced_amount=invoice, settled_amount=paid, status='unsettled',
                confirmation_status=confirmation, created_by=1))
        db.commit()
    with TestClient(app) as client:
        _login(client, 'finance')
        for queue, number, action in [('pending_payment', 'PAY', 'payment'),
                                     ('pending_reconciliation', 'DRAFT', 'reconcile'),
                                     ('completed', 'DONE-LEGACY', 'completed')]:
            response = client.get('/api/finance/current-customer-months', params={'all_open':True,'balance_type':queue})
            assert response.status_code == 200, response.text
            data = response.json()
            assert data['queue_counts']['completed'] == 1
            assert len(data['items']) == 1
            row = data['items'][0]
            assert row['primary_action'] == action
            assert [s['statement_number'] for s in row['statements']] == [number]
            assert row['statement_count'] == 1
            if queue == 'pending_payment':
                assert row['queue_status'] == '部分收款'
                assert Decimal(str(row['settled_amount'])) == 20
                assert row['pending_confirmation_count'] == 0
    with factory() as db:
        assert db.query(Statement).filter_by(statement_number='DONE-LEGACY').one().confirmation_status == 'draft'


def test_missing_price_is_not_zero_or_selectable(finance_api_app):
    from app.models.delivery import DeliveryItem
    app, factory = finance_api_app
    with TestClient(app) as client:
        _login(client, 'finance')
        assert client.post('/api/finance/return_receipts', json=_receipt_payload()).status_code == 201
        with factory() as db:
            item = db.get(DeliveryItem, 1)
            item.source_type = 'unordered_finished'
            item.product_id = 1
            item.order_item_id = None
            item.unit_price_snapshot = None
            item.product_code_snapshot = 'MISSING'
            item.product_name_snapshot = '待补价纸箱'
            item.unit_snapshot = '个'
            item.price_source = 'pending'
            db.commit()
        response = client.get('/api/finance/pending_statements', params={'customer_id':1,'statement_month':'2026-06'})
        assert response.status_code == 200, response.text
        row = response.json()['deliveries'][0]
        assert row['selection_blocked'] is True
        assert row['missing_price_count'] == 1
        assert row['total_receivable_amount'] is None
        assert row['items'][0]['receivable_amount'] is None
        assert '单价' in row['exception_reason']
        summary = client.get('/api/finance/current-customer-months', params={'statement_month':'2026-06'}).json()['items'][0]
        assert summary['missing_price_count'] == 1
        rejected = client.post('/api/finance/statements', json={'customer_id':1,'statement_month':'2026-06','delivery_ids':[1]})
        assert rejected.status_code == 409


def test_explicit_customer_cannot_be_replaced_and_existing_bill_opens_detail(tmp_path):
    index = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text(encoding='utf-8')
    def body(start, end):
        return index.split(start,1)[1].split(end,1)[0].rsplit('}',1)[0]
    load = body('async loadStatementCustomers() {','async loadPendingStatements() {')
    # Use the exact complete method, independent of the next method name.
    action = index.split('async runFinancePrimaryAction(row) {',1)[1].split('\n          },',1)[0]
    script = f'''
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const pending=[];
globalThis.axios={{get:()=>new Promise(resolve=>pending.push(resolve))}};
const vm={{statementForm:{{customer_id:44,statement_month:'2026-08'}},statementCustomers:[],modal:{{type:'statement'}}}};
vm.load=new AsyncFunction({json.dumps(load)}).bind(vm);
(async()=>{{
 let p=vm.load();pending.shift()({{data:{{items:[{{id:125,name:'新振'}}]}}}});await p;
 if(vm.statementForm.customer_id===125)throw Error('explicit customer replaced');
 vm.statementForm={{customer_id:44,statement_month:'2026-08'}};
 p=vm.load();vm.statementForm={{customer_id:99,statement_month:'2026-09'}};
 pending.shift()({{data:{{items:[{{id:125}}]}}}});await p;
 if(vm.statementForm.customer_id!==99)throw Error('stale customer request applied');
 let opened=null;vm.openStatementDetail=async r=>opened=r.id;
 vm.openStatement=()=>{{throw Error('existing statement routed to creation')}};
 vm.run=new AsyncFunction('row',{json.dumps(action)}).bind(vm);
 await vm.run({{primary_action:'reconcile',customer_id:44,statement_month:'2026-08',pending_reconciliation_count:0,statements:[{{id:18,confirmation_status:'draft'}}]}});
 if(opened!==18)throw Error('wrong statement');
}})().catch(e=>{{console.error(e);process.exit(1)}});
'''
    file = tmp_path / 'customer.cjs'
    file.write_text(script,encoding='utf-8')
    result = subprocess.run(['node',str(file)],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr


def test_statement_requests_cannot_overwrite_new_customer_or_detail(tmp_path):
    index = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text(encoding='utf-8')
    def method(signature):
        return index.split(signature,1)[1].split('\n          },',1)[0]
    pending_body = method('async loadPendingStatements() {')
    detail_body = method('async openStatementDetail(row) {')
    script = f'''
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const requests=[];globalThis.axios={{get:()=>new Promise(resolve=>requests.push(resolve))}};
const vm={{statementForm:{{customer_id:44,statement_month:'2026-08'}},modal:{{type:'statement'}},normalizeStatementDelivery:r=>r}};
vm.pending=new AsyncFunction({json.dumps(pending_body)}).bind(vm);
vm.detail=new AsyncFunction('row',{json.dumps(detail_body)}).bind(vm);
(async()=>{{
 const old=vm.pending();vm.statementForm.customer_id=125;const fresh=vm.pending();
 requests[1]({{data:{{deliveries:[{{delivery_id:54}}]}}}});await fresh;
 requests[0]({{data:{{deliveries:[{{delivery_id:99}}]}}}});await old;
 if(vm.pendingStatements[0].delivery_id!==54)throw Error('stale pending response');
 const first=vm.detail({{id:18}});const second=vm.detail({{id:48}});
 requests[3]({{data:{{id:48,statement_number:'NEW'}}}});await second;
 requests[2]({{data:{{id:18,statement_number:'OLD'}}}});await first;
 if(vm.statementDetail.id!==48)throw Error('stale detail response');
 const closed=vm.detail({{id:18}});vm.modal={{type:''}};
 requests[4]({{data:{{id:18}}}});await closed;
 if(vm.modal.type)throw Error('closed modal reopened');
}})().catch(e=>{{console.error(e);process.exit(1)}});
'''
    file = tmp_path / 'races.cjs'
    file.write_text(script,encoding='utf-8')
    result = subprocess.run(['node',str(file)],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
