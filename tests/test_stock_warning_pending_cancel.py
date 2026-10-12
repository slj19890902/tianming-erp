import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_stock_replenishment_flow import (
    stock_replenishment_app, _login, _semi_policy_payload, _customer_replenishment_payload,
)
from tests.test_p1_09c_78_stock_replenishment_void_guard import INDEX, _method_body, _run_node
from test_p1_140_external_stock_replenishment import external_stock_app, _seed_external_warning, _login as external_login
from test_p1_40a_packaging_masterdata import p1_40a_app


def test_pending_warning_has_cancel_action():
    pending = INDEX.split('<data-panel v-if="requisitionTab===\'pending\'"', 1)[1].split('</data-panel>', 1)[0]
    assert '@click="voidPendingStockReplenishment(row)"' in pending
    assert 'row.stock_replenishment_can_void && canRequisition' in pending
    assert 'stockReplenishmentVoidUncertain(row)' in pending
    assert '撤销并返回库存预警' in pending


@pytest.mark.parametrize('target', ['semi_finished', 'finished'])
def test_warning_cancel_restores_editable_draft_without_changing_inventory(stock_replenishment_app, target):
    from app.models.stock_replenishment import StockReplenishmentOrder
    from app.models.warehouse_inventory import InventoryLot
    from app.models.audit import OperationLog
    app, factory = stock_replenishment_app
    with factory() as db:
        before = list(db.execute(select(InventoryLot.id, InventoryLot.quantity_available)))
    with TestClient(app) as client:
        _login(client)
        policy_payload = _semi_policy_payload() if target == 'semi_finished' else dict(
            policy_name='成品预警', target_inventory_type='finished', product_id=1,
            customer_id=1, warning_quantity=100, target_quantity=200, active=True)
        if target == 'semi_finished':
            policy_payload['product_id'] = 1
        response = client.post('/api/requisition/stock-policies', json=policy_payload)
        assert response.status_code == 201, response.text
        policy_id = response.json()['id']
        draft_url = f'/api/requisition/stock-policies/{policy_id}/replenishment-draft'
        draft = client.get(draft_url).json()
        payload = dict(source_type='stock_warning', supplier_name=draft['supplier_name'],
                       customer_id=draft['customer_id'], stock_now=False,
                       idempotency_key='cancel-warning-original', items=draft['items'])
        created = client.post('/api/requisition/stock-replenishment/orders', json=payload)
        assert created.status_code == 201, created.text
        order = created.json()
        pending = client.get('/api/requisition/pending').json()['items']
        row = next(r for r in pending if r.get('source_id') == order['id'])
        assert row['id'].startswith('sr')  # A line ID must never become the cancellation target.
        assert row['stock_replenishment_can_void'] is True
        void_url = f"/api/requisition/stock-replenishment/orders/{order['id']}/void"
        canceled = client.put(void_url)
        assert canceled.status_code == 200, canceled.text
        assert canceled.json()['status'] == 'voided'
        assert client.put(void_url).json()['status'] == 'voided'
        assert not any(r.get('source_id') == order['id'] for r in client.get('/api/requisition/pending').json()['items'])
        restored = client.get(draft_url)
        assert restored.status_code == 200, restored.text
        assert restored.json()['draft_ready']
        assert restored.json()['items'][0]['quantity'] == draft['items'][0]['quantity']
        changed = copy.deepcopy(payload)
        changed['idempotency_key'] = 'cancel-warning-modified'
        changed['items'][0]['quantity'] += 7
        recreated = client.post('/api/requisition/stock-replenishment/orders', json=changed)
        assert recreated.status_code == 201, recreated.text
        assert recreated.json()['id'] != order['id']
        assert recreated.json()['items'][0]['quantity'] == changed['items'][0]['quantity']
        replay = client.post('/api/requisition/stock-replenishment/orders', json=payload)
        assert replay.status_code == 201 and replay.json()['id'] == order['id']
        assert replay.json()['status'] == 'voided'
    with factory() as db:
        assert list(db.execute(select(InventoryLot.id, InventoryLot.quantity_available))) == before
        assert db.get(StockReplenishmentOrder, order['id']).status == 'voided'
        logs = list(db.scalars(select(OperationLog).where(OperationLog.action == 'VOID_STOCK_REPLENISHMENT')))
        assert len(logs) == 1


def test_pending_sibling_cannot_offer_whole_order_cancel_after_purchase(stock_replenishment_app):
    app, _ = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        payload = _customer_replenishment_payload()
        payload['items'].append(copy.deepcopy(payload['items'][0]))
        created = client.post('/api/requisition/stock-replenishment/orders', json=payload)
        assert created.status_code == 201, created.text
        order_id = created.json()['id']
        rows = [r for r in client.get('/api/requisition/pending').json()['items'] if r.get('source_id') == order_id]
        assert len(rows) == 2 and all(r['stock_replenishment_can_void'] for r in rows)
        purchase = client.post('/api/requisition/supplier-orders/from-pending-selection', json={
            'supplier_groups': [{'supplier_name': rows[0]['supplier_name'], 'request_key': 'cancel-sibling-purchase',
                'stock_sources': [{'stock_replenishment_item_id': rows[0]['stock_replenishment_item_id'],
                                   'source_fingerprint': rows[0]['source_fingerprint']}]}]})
        assert purchase.status_code == 201, purchase.text
        remaining = [r for r in client.get('/api/requisition/pending').json()['items'] if r.get('source_id') == order_id]
        assert len(remaining) == 1 and remaining[0]['stock_replenishment_can_void'] is False
        rejected = client.put(f'/api/requisition/stock-replenishment/orders/{order_id}/void')
        assert rejected.status_code == 409 and '先撤销' in rejected.text


def test_external_physical_pending_cancel_releases_warning_coverage(external_stock_app):
    from app.models.stock_replenishment import InventoryStockPolicy
    from app.models.user import User
    from app.services.stock_warning_drafts import create_external_warning_draft, pending_physical_warning_quantity
    policy_id, product_id = _seed_external_warning(external_stock_app)
    with external_stock_app.state.factory() as db:
        policy = db.get(InventoryStockPolicy, policy_id)
        user = db.scalar(select(User).where(User.username == 'p1-40a-admin'))
        order, _ = create_external_warning_draft(db, policy=policy,
            delivery=SimpleNamespace(id=901, status='dispatched', is_historical_backfill=False,
                                     customer_id=policy.customer_id),
            physical_quantity=3, operator_id=user.id)
        order_id, customer_id = order.id, policy.customer_id
        db.commit()
        assert pending_physical_warning_quantity(db, customer_id=customer_id, product_id=product_id) == 3
    with TestClient(external_stock_app) as client:
        external_login(client)
        row = next(r for r in client.get('/api/requisition/pending').json()['items'] if r.get('source_id') == order_id)
        assert row['procurement_mode'] == 'external_purchase'
        assert row['stock_replenishment_can_void']
        assert row['external_purchase_quantity'] == '3' and row['unit'] == '片'
        response = client.put(f'/api/requisition/stock-replenishment/orders/{order_id}/void')
        assert response.status_code == 200 and response.json()['status'] == 'voided', response.text
        assert not any(r.get('source_id') == order_id for r in client.get('/api/requisition/pending').json()['items'])
    with external_stock_app.state.factory() as db:
        assert pending_physical_warning_quantity(db, customer_id=customer_id, product_id=product_id) == 0
        policy = db.get(InventoryStockPolicy, policy_id)
        user = db.scalar(select(User).where(User.username == 'p1-40a-admin'))
        revised, _ = create_external_warning_draft(db, policy=policy,
            delivery=SimpleNamespace(id=902, status='dispatched', is_historical_backfill=False,
                                     customer_id=customer_id),
            physical_quantity=5, operator_id=user.id)
        revised_id, request_hash = revised.id, revised.request_hash
        db.commit()
    with TestClient(external_stock_app) as client:
        external_login(client)
        endpoint = f'/api/requisition/stock-replenishment/orders/{revised_id}/external-purchase'
        preview = client.get(endpoint + '-preview')
        assert preview.status_code == 200, preview.text
        confirmed = client.post(endpoint, json={'expected_request_hash': request_hash,
                                 'expected_quote_hash': preview.json()['expected_quote_hash']})
        assert confirmed.status_code == 200, confirmed.text
        blocked = client.put(f'/api/requisition/stock-replenishment/orders/{revised_id}/void')
        assert blocked.status_code == 409 and '外购' in blocked.text


@pytest.mark.parametrize('gate', ['permission', 'customer_scope'])
def test_pending_cancel_preserves_permission_and_customer_gates(stock_replenishment_app, gate):
    from app.models.user import User
    from app.models.access_control import UserPermissionOverride
    from app.models.stock_replenishment import StockReplenishmentOrder
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/requisition/stock-replenishment/orders', json=_customer_replenishment_payload())
        assert created.status_code == 201, created.text
        order_id = created.json()['id']
        with factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            user.role = 'sales'
            user.customer_access_mode = 'selected' if gate == 'customer_scope' else 'all'
            db.add(UserPermissionOverride(user_id=user.id, permission_code='requisition.view', is_allowed=True))
            db.add(UserPermissionOverride(user_id=user.id, permission_code='requisition.execute',
                                         is_allowed=gate != 'permission'))
            db.commit()
        response = client.put(f'/api/requisition/stock-replenishment/orders/{order_id}/void')
        assert response.status_code == 403, response.text
        pending = client.get('/api/requisition/pending')
        assert pending.status_code == 200, pending.text
        visible = [row for row in pending.json()['items'] if row.get('source_id') == order_id]
        assert all(not row['stock_replenishment_can_void'] for row in visible)
        if gate == 'customer_scope':
            assert not visible
        else:
            assert visible
    with factory() as db:
        assert db.get(StockReplenishmentOrder, order_id).status == 'draft'


def test_pending_cancel_audit_failure_rolls_back(stock_replenishment_app, monkeypatch):
    from sqlalchemy.orm import Session
    from app.models.audit import OperationLog
    from app.models.stock_replenishment import StockReplenishmentOrder
    app, factory = stock_replenishment_app
    original = Session.add
    def fail_audit(self, instance, *args, **kwargs):
        if isinstance(instance, OperationLog) and instance.action == 'VOID_STOCK_REPLENISHMENT':
            raise RuntimeError('injected cancel audit failure')
        return original(self, instance, *args, **kwargs)
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/requisition/stock-replenishment/orders', json=_customer_replenishment_payload())
        assert created.status_code == 201, created.text
        order_id = created.json()['id']
        monkeypatch.setattr(Session, 'add', fail_audit)
        with pytest.raises(RuntimeError, match='injected cancel audit failure'):
            client.put(f'/api/requisition/stock-replenishment/orders/{order_id}/void')
        with factory() as db:
            assert db.get(StockReplenishmentOrder, order_id).status == 'draft'
            assert not db.scalar(select(OperationLog.id).where(OperationLog.action == 'VOID_STOCK_REPLENISHMENT'))
        monkeypatch.setattr(Session, 'add', original)
        assert client.put(f'/api/requisition/stock-replenishment/orders/{order_id}/void').status_code == 200


def test_pending_cancel_uses_header_id_refreshes_home_and_keeps_unknown_outcome_locked(tmp_path):
    methods = {
        'voidPendingStockReplenishment': ('async voidPendingStockReplenishment(row) {', 'stockReplenishmentVoidBusy(row) {'),
        'stockReplenishmentVoidBusy': ('stockReplenishmentVoidBusy(row) {', 'stockReplenishmentVoidUncertain(row) {'),
        'stockReplenishmentVoidUncertain': ('stockReplenishmentVoidUncertain(row) {', 'async voidReportedReplenishment(row) {'),
        'voidReportedReplenishment': ('async voidReportedReplenishment(row) {', 'async voidReportedCompositeRequisition(row) {'),
    }
    script = '''
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const requests=[],events=[],messages=[];
global.axios={put:url=>new Promise((resolve,reject)=>requests.push({url,resolve,reject}))};
const vm={canRequisition:true,user:{id:1},authGeneration:1,activePage:'requisition',requisitionTab:'pending',
  stockReplenishmentVoidState:{orderId:null,documentNumber:'',uncertainIds:{}},
  confirmOriginalBusinessAction:async text=>{events.push(text);return true;},
  loadRequisition:async()=>{events.push('pending');return true;},
  loadReportedDocuments:async()=>{throw Error('wrong tab reload');},
  loadOverview:async()=>{events.push('warnings');return true;},
  go:async function(page){events.push(page);this.activePage=page;},
  showToast:text=>messages.push(text),errorMessage:error=>error.message};
const expect=(value,message)=>{if(!value)throw Error(message)};
'''
    for name, signatures in methods.items():
        constructor = 'AsyncFunction' if name.startswith('void') else 'Function'
        script += f'vm.{name}=new {constructor}("row",{json.dumps(_method_body(*signatures), ensure_ascii=False)}).bind(vm);\n'
    script += '''
(async()=>{
  const row={id:'sr987',source_id:12,source_type:'stock_replenishment',source_snapshot:{source_type:'stock_warning'},order_number:'CBR-12',customer_id:1,stock_replenishment_can_void:true};
  const first=vm.voidPendingStockReplenishment(row);
  await Promise.resolve();await Promise.resolve();
  expect(await vm.voidPendingStockReplenishment({...row})===false,'double click was not blocked');
  expect(requests.length===1&&requests[0].url.endsWith('/12/void'),'line ID used instead of header ID');
  expect(events[0].includes('全部明细'),'confirmation concealed whole-document scope');
  requests[0].resolve({data:{id:12,status:'voided'}});await first;
  expect(events.includes('pending')&&events.includes('warnings')&&events.includes('dashboard'),'warnings were not refreshed and restored');
  expect(row.stock_replenishment_can_void===false,'canceled row stayed actionable');
  vm.activePage='requisition';
  const unknown={...row,id:'sr988',source_id:13,stock_replenishment_can_void:true};
  const request=vm.voidPendingStockReplenishment(unknown);await Promise.resolve();await Promise.resolve();
  requests[1].reject(Error('network'));await request;
  expect(vm.stockReplenishmentVoidUncertain(unknown),'pending header unknown result was not locked');
  expect(await vm.voidPendingStockReplenishment(unknown)===false&&requests.length===2,'uncertain cancellation retried');
  expect(vm.activePage==='requisition','failed cancellation navigated as success');
  expect(messages.some(s=>s.includes('待报料')),'unknown-result message omitted pending refresh');
  const denied={...unknown,source_id:14};vm.canRequisition=false;
  expect(await vm.voidPendingStockReplenishment(denied)===false&&requests.length===2,'no-permission UI sent cancel');
  vm.canRequisition=true;vm.confirmOriginalBusinessAction=async()=>false;
  expect(await vm.voidPendingStockReplenishment(denied)===false&&requests.length===2,'dismissed confirmation sent cancel');
  vm.confirmOriginalBusinessAction=async()=>{vm.authGeneration++;return true;};
  expect(await vm.voidPendingStockReplenishment(denied)===false&&requests.length===2,'stale confirmation submitted after account change');
})().catch(error=>{console.error(error);process.exit(1)});
'''
    _run_node(script, tmp_path, 'pending-warning-cancel.js')


def test_pending_refresh_reconciles_only_returned_source_ids(tmp_path):
    block = _method_body('applyRequisitionPendingResponse(data, {supplierName="", clearSelection=false}={}) {',
                         'async loadRequisitionPendingPage(')
    script = f'''
const vm={{pages:{{}},stockReplenishmentVoidState:{{orderId:null,uncertainIds:{{12:true,99:true}}}},
  clearPendingRequisitionSelection(){{}}}};
const apply=new Function('data','{{supplierName="",clearSelection=false}}={{}}', {json.dumps(block, ensure_ascii=False)}).bind(vm);
apply({{items:[{{id:'sr987',source_id:12,source_type:'stock_replenishment'}}]}},{{clearSelection:true}});
if(vm.stockReplenishmentVoidState.uncertainIds[12]||!vm.stockReplenishmentVoidState.uncertainIds[99])
  throw Error('pending page unlocked unseen sources or failed to reconcile the returned header');
'''
    _run_node(script, tmp_path, 'pending-warning-refresh.js')
