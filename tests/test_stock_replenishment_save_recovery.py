import copy
import hashlib

from fastapi.testclient import TestClient
from sqlalchemy import event, select, func
from sqlalchemy.exc import OperationalError

from test_stock_replenishment_flow import stock_replenishment_app, _customer_replenishment_payload
from test_p1_140_external_stock_replenishment import external_stock_app, _seed_external_warning, _login as external_login
from test_p1_40a_packaging_masterdata import p1_40a_app
from app.api import requisition
from app.models.product import Product
from app.models.stock_replenishment import StockReplenishmentOrder
from app.models.external_packaging_purchase import ExternalPackagingPurchaseBatch, ExternalPackagingPurchaseOrder
from app.models.audit import OperationLog

PATH = '/api/requisition/stock-replenishment/orders'


def login(client):
    assert client.post('/api/auth/login', json={'username': 'admin', 'password': 'RolePass123!'}).status_code == 200


def body(key='save-recovery-plain'):
    value = _customer_replenishment_payload(30)
    value['idempotency_key'] = key
    value['expected_actor_id'] = 1
    return value


def resolve(client, original, actor=1, key=None):
    return client.post('/api/requisition/stock-replenishment/save-results/' + (key or original['idempotency_key']) + '/resolve', json={'expected_actor_id': actor, 'original_request': original})


def counts(factory):
    with factory() as db:
        return tuple(db.scalar(select(func.count()).select_from(model)) for model in (StockReplenishmentOrder, ExternalPackagingPurchaseBatch, ExternalPackagingPurchaseOrder, OperationLog))


def test_plain_receipt_old_hash_and_readonly_after_void(stock_replenishment_app):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        login(client)
        original = body()
        old = dict(original); old.pop('expected_actor_id')
        saved = client.post(PATH, json=old)
        assert saved.status_code == 201, saved.text
        receipt = saved.json()['save_receipt']
        assert receipt['outcome'] == 'draft_saved'
        assert receipt['request_hash'] == requisition.canonical_purchase_purpose_hash({'actor_id': 1, 'payload': requisition.StockReplenishmentCreatePayload.model_validate(old).model_dump(mode='json', exclude_none=False)})
        assert client.post(PATH, json=original).json()['save_receipt'] == receipt
        oid = saved.json()['id']
        assert client.put(PATH + f'/{oid}/void').status_code == 200
        with factory() as db:
            db.get(Product, 1).is_active = False
            db.commit()
        before = counts(factory)
        found = resolve(client, original)
        assert found.status_code == 200, found.text
        assert found.json()['save_receipt'] == receipt
        assert found.json()['current']['status'] == 'voided'
        assert counts(factory) == before
        assert found.headers['cache-control'] == 'no-store'


def test_receipt_generation_failure_rolls_back_then_fresh_rejection(stock_replenishment_app, monkeypatch):
    app, factory = stock_replenishment_app
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client); original = body('save-generation-fail'); before = counts(factory)
        def busy(*args, **kwargs):
            raise OperationalError('synthetic receipt failure', {}, RuntimeError('busy'))
        with monkeypatch.context() as patch:
            patch.setattr(requisition, 'replenishment_order_dict', busy)
            failed = client.post(PATH, json=original)
        assert failed.status_code == 500
        assert counts(factory) == before
        assert resolve(client, original).json()['status'] == 'not_recorded'
        invalid = body('save-safe-rejected'); invalid['items'][0]['material_id'] = 99999
        rejected = client.post(PATH, json=invalid)
        assert rejected.status_code in (400, 404, 409)
        assert rejected.headers.get('x-stock-replenishment-rejected') == '1', rejected.text
        assert 'x-stock-replenishment-preserve' not in rejected.headers
        assert rejected.json()['detail']['save_result']['status'] == 'not_saved'
        assert counts(factory) == before


def test_committed_ack_loss_resolves_and_readonly_never_writes(stock_replenishment_app, monkeypatch):
    app, factory = stock_replenishment_app
    original = body('save-commit-ack-loss')
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client)
        real_commit = requisition.commit_business_change
        def lost(db):
            real_commit(db)
            raise RuntimeError('synthetic committed ack lost')
        with monkeypatch.context() as patch:
            patch.setattr(requisition, 'commit_business_change', lost)
            failed = client.post(PATH, json=original)
        assert failed.status_code == 500
        assert failed.headers.get('x-stock-replenishment-preserve') == '1'
        assert failed.headers.get('x-stock-replenishment-rejected') is None
        before = counts(factory); statements = []
        with factory() as db: engine = db.get_bind()
        def sql(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE'): statements.append(statement)
        event.listen(engine, 'before_cursor_execute', sql)
        try:
            found = resolve(client, original)
            changed = copy.deepcopy(original); changed['items'][0]['quantity'] = 31
            conflict = resolve(client, changed)
        finally: event.remove(engine, 'before_cursor_execute', sql)
        assert found.status_code == 200 and found.json()['status'] == 'completed', found.text
        assert conflict.status_code == 409 and 'x-stock-replenishment-rejected' not in conflict.headers
        assert statements == [] and counts(factory) == before


def test_actor_key_and_schema_failures_preserve(stock_replenishment_app):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        login(client); original = body('save-identities')
        assert client.post(PATH, json=original).status_code == 201
        before = counts(factory)
        mismatch = dict(original, expected_actor_id=2)
        assert client.post(PATH, json=mismatch).headers.get('x-stock-replenishment-actor-mismatch') == '1'
        assert resolve(client, original, key='different-key').status_code == 409
        assert resolve(client, original, actor=2).headers.get('x-stock-replenishment-actor-mismatch') == '1'
        for actor in (True, '1'):
            assert client.post(PATH, json=dict(original, expected_actor_id=actor)).status_code == 422
        schema = client.post(PATH, json=dict(original, items=[]))
        assert schema.status_code == 422 and schema.headers['cache-control'] == 'no-store'
        assert schema.headers.get('x-stock-replenishment-preserve') == '1'
        assert counts(factory) == before


def test_external_override_frozen_conversion_and_supply_mode_drift(external_stock_app):
    app = external_stock_app; policy, product_id = _seed_external_warning(app)
    with TestClient(app) as client:
        external_login(client)
        draft = client.get(f'/api/requisition/stock-policies/{policy}/replenishment-draft').json()
        with app.state.factory() as db:
            from app.models.user import User
            actor = db.scalar(select(User.id).where(User.username == 'p1-40a-admin'))
        original = dict(source_type='stock_warning', idempotency_key='save-external-override', customer_id=draft['customer_id'], supplier_name=draft['supplier_name'], stock_now=False, items=[draft['items'][0]], expected_actor_id=actor)
        original['items'][0]['external_purchase_quantity'] = '11'
        original['items'][0]['external_purchase_unit'] = 'ignored-unit'
        saved = client.post(PATH, json=original)
        assert saved.status_code == 201, saved.text
        receipt = saved.json()['save_receipt']; assert receipt is not None
        assert receipt['lines'][0]['requested_quantity'] != receipt['lines'][0]['saved_quantity']
        assert receipt['external_purchase']['lines'][0]['converted_source_quantity'] == receipt['lines'][0]['saved_quantity']
        with app.state.factory() as db:
            product = db.get(Product, product_id); product.is_active = False; product.supply_mode = 'corrugated_production'
            for field in ('external_packaging_category_code','external_packaging_specification_json','external_packaging_specification_summary','external_packaging_purchase_unit','external_packaging_candidate_snapshot_json','external_packaging_default_order_quantity_basis','external_packaging_default_purchase_quantity_basis'):
                setattr(product, field, None)
            db.commit()
        before = counts(app.state.factory)
        replay = client.post(PATH, json=original)
        assert replay.status_code == 201, replay.text
        assert replay.json()['save_receipt'] == receipt
        found = resolve(client, original, actor=actor)
        assert found.status_code == 200 and found.json()['save_receipt'] == receipt
        assert counts(app.state.factory) == before


def test_same_product_lines_and_missing_source_are_not_collapsed(stock_replenishment_app):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        login(client); original = body('save-repeated-product')
        original['items'].append(dict(original['items'][0], quantity=31))
        saved = client.post(PATH, json=original)
        assert saved.status_code == 201, saved.text
        lines = saved.json()['save_receipt']['lines']
        assert [row['request_index'] for row in lines] == [0, 1]
        assert len({row['source_item_id'] for row in lines}) == 2
        with factory() as db:
            order = db.get(StockReplenishmentOrder, saved.json()['id']); db.delete(order.items[1]); db.commit()
        found = resolve(client, original)
        assert found.status_code == 200 and found.json()['status'] == 'trace', found.text


def test_namespace_and_null_hash_do_not_manufacture_proof(stock_replenishment_app):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        login(client); original = body('save-legacy-null')
        saved = client.post(PATH, json=original); assert saved.status_code == 201
        with factory() as db:
            row = db.get(StockReplenishmentOrder, saved.json()['id']); row.request_hash = None; db.commit()
        before = counts(factory)
        found = resolve(client, original)
        assert found.status_code == 200 and found.json()['status'] == 'trace'
        assert found.json()['request_match'] is False
        assert counts(factory) == before
        other = body('save-other-namespace')
        with factory() as db:
            row = StockReplenishmentOrder(order_number='SW-'+hashlib.sha256(other['idempotency_key'].encode()).hexdigest()[:20].upper(), source_type='stock_warning', status='draft', created_by=1, request_hash='f'*64)
            db.add(row); db.commit()
        assert resolve(client, other).json()['status'] == 'not_recorded'


def test_scoped_owner_inactive_restore_and_relocation_denial(stock_replenishment_app):
    from app.models.user import User
    from app.models.access_control import UserPermissionOverride, UserCustomerScope
    from app.models.customer import Customer
    from app.core.security import hash_password
    app, factory = stock_replenishment_app
    with factory() as db:
        actor = User(username='recover-scoped', real_name='合成限定客户报料员', password_hash=hash_password('ScopePass123!'), role='sales', customer_access_mode='selected', must_change_password=False)
        db.add(actor); db.flush(); actor_id = actor.id
        db.add(UserPermissionOverride(user_id=actor_id, permission_code='requisition.execute', is_allowed=True))
        db.add(UserCustomerScope(user_id=actor_id, customer_id=1))
        from app.models.stock_replenishment import InventoryStockPolicy
        policy=InventoryStockPolicy(policy_name='可空客户由产品归属',target_inventory_type='semi_finished',product_id=1,customer_id=1,warning_quantity=10,target_quantity=50)
        db.add(policy);db.flush();policy_id=policy.id
        other = Customer(name='隔离另一客户', customer_code='RECOVERY-OTHER')
        db.add(other); db.flush(); other_id=other.id; db.commit()
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username':'recover-scoped', 'password':'ScopePass123!'}).status_code == 200
        original = dict(body('save-scoped-owner'), expected_actor_id=actor_id)
        original['items'][0]['stock_policy_id']=policy_id
        saved = client.post(PATH, json=original); assert saved.status_code == 201, saved.text
        with factory() as db:
            db.get(Product,1).is_active=False
            db.get(InventoryStockPolicy,policy_id).customer_id=None
            db.commit()
        assert resolve(client, original, actor=actor_id).json()['status'] == 'completed'
        assert client.post(PATH,json=original).status_code == 201
        with factory() as db: db.get(Product,1).customer_id=other_id; db.commit()
        before=counts(factory); denied=resolve(client,original,actor=actor_id)
        assert denied.status_code == 403 and denied.headers.get('x-stock-replenishment-preserve')=='1'
        assert counts(factory)==before


def test_external_receiving_changes_current_not_original_receipt(external_stock_app):
    from app.models.warehouse_inventory import WarehouseLocation
    from app.models.user import User
    app=external_stock_app;policy,_=_seed_external_warning(app)
    with app.state.factory() as db:
        actor=db.scalar(select(User.id).where(User.username=='p1-40a-admin'))
        db.add(WarehouseLocation(location_code='F1-DISPATCH-01',location_name='合成成品待送区',warehouse_type='finished',warehouse_floor=1,area_code='DISPATCH',storage_type='temporary_aisle',source_version='P1-25C',placement_status='placed',is_active=True));db.commit()
    with TestClient(app) as client:
        external_login(client);draft=client.get(f'/api/requisition/stock-policies/{policy}/replenishment-draft').json()
        original=dict(source_type='stock_warning',idempotency_key='save-external-received',customer_id=draft['customer_id'],supplier_name=draft['supplier_name'],stock_now=False,items=[draft['items'][0]],expected_actor_id=actor)
        saved=client.post(PATH,json=original);assert saved.status_code==201,saved.text
        po=saved.json()['external_purchase_orders'][0]
        received=client.post(f"/api/external-packaging-purchases/{po['id']}/receipts",json={'idempotency_key':'recovery-receive','lines':[{'purchase_item_id':po['items'][0]['id'],'received_quantity':'2'}]})
        assert received.status_code==200,received.text
        before=counts(app.state.factory);found=resolve(client,original,actor=actor)
        assert found.status_code==200 and found.json()['save_receipt']==saved.json()['save_receipt']
        assert found.json()['current']['status']=='partially_stocked'
        assert found.json()['current']['items'][0]['receipt_progress']['received_quantity']==1
        assert found.json()['current']['external_purchase_status']['purchase_orders'][0]['status']=='confirmed'
        assert counts(app.state.factory)==before


def test_normalized_net_dimensions_and_nested_float_hash(stock_replenishment_app):
    from app.services import stock_replenishment_save_recovery as recovery
    from app.services.purchase_purpose_allocation import canonical_purchase_purpose_json
    app, factory=stock_replenishment_app
    with TestClient(app) as client:
        login(client); original=body('save-normalization')
        original['items'][0].update(report_length_mm=139,crease_type='净')
        saved=client.post(PATH,json=original); assert saved.status_code==201,saved.text
        request=saved.json()['save_receipt']['request']
        assert request['items'][0]['report_length_mm']==139
        assert request['items'][0]['sheet_type']=='net_sheet'
        assert request['items'][0]['crease_left_mm'] is None
        nested=copy.deepcopy(original);nested['idempotency_key']='save-nested-hash'
        nested['items'][0]['report_length_mm']=139.5
        nested['replenishment_plan']={'policy_id':1,'finished_quantity':1,'width':139.5,'fraction':1.25}
        model=requisition.StockReplenishmentCreatePayload.model_validate(nested)
        canonical=canonical_purchase_purpose_json({'actor_id':1,'payload':recovery.normalized_request(model)})
        assert '"width":"139.5"' in canonical and '"fraction":"1.25"' in canonical
        rejected=client.post(PATH,json=nested); assert rejected.status_code==409
        proof=rejected.json()['detail']['save_result']
        assert proof['request_hash']==hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        assert proof['status']=='not_saved'
