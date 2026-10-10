"""Real commands keep current authority and the original request identity."""
import copy
import hashlib
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from test_p1_140_external_stock_replenishment import external_stock_app, _seed_external_warning, _login
from test_p1_40a_packaging_masterdata import p1_40a_app
from test_stock_replenishment_flow import stock_replenishment_app, _customer_replenishment_payload
from app.api import requisition
from app.models.audit import OperationLog
from app.models.product import Product
from app.models.user import User
from app.models.supplier import Supplier
from app.models.stock_replenishment import InventoryStockPolicy, StockReplenishmentOrder, StockReplenishmentOrderItem
from app.models.external_packaging_purchase import ExternalPackagingPurchaseBatch, ExternalPackagingPurchaseOrder, ExternalPackagingPurchaseItem
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation
from app.core.security import hash_password
from app.services import external_packaging_stock_replenishment as external

PATH = '/api/requisition/stock-replenishment/orders'


def _body(client, policy_id, key):
    response = client.get(f'/api/requisition/stock-policies/{policy_id}/replenishment-draft')
    assert response.status_code == 200, response.text
    draft = response.json()
    return dict(source_type='stock_warning', idempotency_key=key, customer_id=draft['customer_id'],
                supplier_name=draft['supplier_name'], stock_now=False, items=[draft['items'][0]])


def _facts(factory):
    with factory() as db:
        models = [StockReplenishmentOrder, StockReplenishmentOrderItem, ExternalPackagingPurchaseBatch,
                  ExternalPackagingPurchaseOrder, ExternalPackagingPurchaseItem, InventoryLot,
                  InventoryMovement, InventoryReservation, OperationLog]
        return {m.__tablename__: db.scalar(select(func.count()).select_from(m)) for m in models}


def test_external_new_hash_and_frozen_exact_replay(external_stock_app, monkeypatch):
    app = external_stock_app
    policy_id, product_id = _seed_external_warning(app)
    with TestClient(app) as client:
        _login(client)
        body = _body(client, policy_id, 'reliable-external-frozen')
        first = client.post(PATH, json=body)
        assert first.status_code == 201, first.text
        with app.state.factory() as db:
            actor = db.scalar(select(User).where(User.username == 'p1-40a-admin'))
            normalized = requisition.StockReplenishmentCreatePayload.model_validate(body).model_dump(mode='json', exclude_none=False)
            expected = requisition.canonical_purchase_purpose_hash({'actor_id': actor.id, 'payload': normalized})
            order = db.get(StockReplenishmentOrder, first.json()['id'])
            assert order.request_hash == expected == first.json()['request_hash']
            assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.action_code == 'stock_replenishment.external_create')) == 1
            purchase = db.scalar(select(ExternalPackagingPurchaseOrder))
            db.get(Supplier, purchase.supplier_id).is_active = False
            db.get(InventoryStockPolicy, policy_id).active = False
            product = db.get(Product, product_id)
            product.is_active = False
            product.external_packaging_default_purchase_quantity_basis = 7
            db.commit()
        before = _facts(app.state.factory)
        monkeypatch.setattr(requisition, 'beijing_today', lambda: date.today() + timedelta(days=1))
        replay = client.post(PATH, json=body)
        assert replay.status_code == 201, replay.text
        assert replay.json() == first.json()
        assert _facts(app.state.factory) == before
        with app.state.factory() as db:
            inner = external.create_external_stock_replenishment_purchase(db,policy=db.get(InventoryStockPolicy,policy_id),
                finished_quantity=body['items'][0]['quantity'],purchase_quantity=body['items'][0]['external_purchase_quantity'],
                order_number='unused',idempotency_key=body['idempotency_key'],remark=None,
                user=db.scalar(select(User).where(User.username=='p1-40a-admin')),request_hash=expected)
            assert inner.id == first.json()['id']
            db.commit()
        assert _facts(app.state.factory) == before


def test_external_current_role_actor_and_entire_body_are_required(external_stock_app):
    app = external_stock_app
    policy_id, _ = _seed_external_warning(app)
    with TestClient(app) as client:
        _login(client)
        body = _body(client, policy_id, 'reliable-external-identity')
        first = client.post(PATH, json=body)
        assert first.status_code == 201, first.text
        before = _facts(app.state.factory)
        changed = copy.deepcopy(body)
        changed['remark'] = 'Changed original body'
        assert client.post(PATH, json=changed).status_code == 409
        with app.state.factory() as db:
            db.add(User(username='other-reliable-admin', password_hash=hash_password('RolePass123!'), role='admin', real_name='Synthetic admin', must_change_password=False))
            db.commit()
        assert client.post('/api/auth/login', json={'username':'other-reliable-admin','password':'RolePass123!'}).status_code == 200
        assert client.post(PATH, json=body).status_code == 409
        with app.state.factory() as db:
            db.scalar(select(User).where(User.username == 'other-reliable-admin')).role = 'boss'
            db.commit()
        assert client.post(PATH, json=body).status_code == 403
        after = _facts(app.state.factory)
        # The standard permission denial may add security audit, never business.
        assert after['operation_logs'] >= before['operation_logs']
        assert {k:v for k,v in after.items() if k != 'operation_logs'} == {k:v for k,v in before.items() if k != 'operation_logs'}
        with app.state.factory() as db:
            assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.action_code == 'stock_replenishment.external_create')) == 1


def test_external_legacy_null_and_inner_or_integrity_replay_never_bypass(external_stock_app, monkeypatch):
    app = external_stock_app
    policy_id, _ = _seed_external_warning(app)
    with TestClient(app) as client:
        _login(client)
        body = _body(client, policy_id, 'reliable-external-legacy')
        with app.state.factory() as db:
            actor = db.scalar(select(User).where(User.username == 'p1-40a-admin'))
            policy = db.get(InventoryStockPolicy, policy_id)
            # Insert a synthetic historical null-hash record, never erase modern proof.
            prepared = external.prepare_external_stock_purchase(db, product=db.get(Product,policy.product_id),
                finished_quantity=body['items'][0]['quantity'], purchase_quantity_override=body['items'][0]['external_purchase_quantity'])
            order = StockReplenishmentOrder(order_number=f"CBW-{requisition.beijing_today():%Y%m%d}-{hashlib.sha256(body['idempotency_key'].encode()).hexdigest()[:20].upper()}",
                supplier_name=body['supplier_name'],customer_id=body['customer_id'],source_type='stock_warning',status='confirmed',created_by=actor.id,confirmed_by=actor.id)
            item = StockReplenishmentOrderItem(stock_policy_id=policy.id,target_inventory_type='finished',procurement_route_snapshot='external_packaging',
                product_id=policy.product_id,reference_product_id=policy.product_id,customer_id=body['customer_id'],product_name_snapshot=db.get(Product,policy.product_id).product_name,product_code_snapshot=db.get(Product,policy.product_id).product_code,sheet_type='raw_board',component_type='whole',
                pieces_per_box=1,stock_yield_per_sheet=1,quantity=body['items'][0]['quantity'],stocked_quantity=0)
            order.items=[item];db.add(order);db.flush()
            external._post_external_stock_purchase(db,order=order,item=item,prepared=prepared,idempotency_key=body['idempotency_key'],fingerprint='a'*64,user=actor)
            db.commit()
            legacy_id = order.id
        before = _facts(app.state.factory)
        legacy = client.post(PATH, json=body)
        assert legacy.status_code == 409, legacy.text
        assert '采购历史' in legacy.json()['detail'] and '勿' in legacy.json()['detail']
        assert client.get(PATH + f'/{legacy_id}').status_code == 200
        with app.state.factory() as db:
            actor = db.scalar(select(User).where(User.username == 'p1-40a-admin'))
            with pytest.raises(external.ExternalPurchaseContractError):
                external.create_external_stock_replenishment_purchase(db, policy=db.get(InventoryStockPolicy, policy_id),
                    finished_quantity=body['items'][0]['quantity'], purchase_quantity=body['items'][0]['external_purchase_quantity'],
                    order_number='unused', idempotency_key=body['idempotency_key'], remark=None, user=actor, request_hash='a'*64)
            assert db.get(StockReplenishmentOrder, legacy_id).request_hash is None
        assert _facts(app.state.factory) == before
        modern = copy.deepcopy(body); modern['idempotency_key'] = 'reliable-external-modern'
        first = client.post(PATH, json=modern)
        assert first.status_code == 201, first.text
        before = _facts(app.state.factory)
        scalar, scalars = Session.scalar, Session.scalars
        hidden = {'batch':False, 'orders':False}
        def hide_batch(db, stmt, *args, **kwargs):
            sql = str(stmt)
            if not hidden['batch'] and 'external_packaging_purchase_batches' in sql and 'idempotency_key =' in sql:
                hidden['batch'] = True; return None
            return scalar(db, stmt, *args, **kwargs)
        def hide_orders(db, stmt, *args, **kwargs):
            sql = str(stmt)
            if not hidden['orders'] and 'stock_replenishment_orders' in sql and 'order_number LIKE' in sql:
                hidden['orders'] = True; return SimpleNamespace(all=lambda: [])
            return scalars(db, stmt, *args, **kwargs)
        def lost_race(*args, **kwargs):
            raise IntegrityError('synthetic concurrently committed original', {}, RuntimeError('unique'))
        monkeypatch.setattr(Session, 'scalar', hide_batch); monkeypatch.setattr(Session, 'scalars', hide_orders)
        monkeypatch.setattr(requisition, 'create_external_stock_replenishment_purchase', lost_race)
        modern['remark'] = 'Different payload after the concurrent winner'
        caught = client.post(PATH, json=modern)
        assert caught.status_code == 409, caught.text
        assert all(hidden.values())
        assert _facts(app.state.factory) == before


def test_external_hash_purchase_and_creation_audit_rollback_together(external_stock_app, monkeypatch):
    app = external_stock_app
    policy_id, _ = _seed_external_warning(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        body = _body(client, policy_id, 'reliable-external-rollback')
        before = _facts(app.state.factory)
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic creation audit failure')
        monkeypatch.setattr(external, 'append_audit_event', fail)
        response = client.post(PATH, json=body)
        assert response.status_code == 500, response.text
        assert _facts(app.state.factory) == before


def test_plain_fresh_inactive_deleted_reference_and_successful_replay(stock_replenishment_app):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username':'admin','password':'RolePass123!'}).status_code == 200
        body = _customer_replenishment_payload(30); body['idempotency_key'] = 'reliable-plain-original'
        first = client.post(PATH, json=body); assert first.status_code == 201, first.text
        with factory() as db:
            db.get(Product,1).is_active = False; db.commit()
        before = _facts(factory)
        replay = client.post(PATH, json=body); assert replay.status_code == 201, replay.text
        assert replay.json()['id'] == first.json()['id']
        fresh = copy.deepcopy(body); fresh['idempotency_key'] = 'reliable-plain-inactive'
        denied = client.post(PATH, json=fresh); assert denied.status_code == 409, denied.text
        with factory() as db:
            from datetime import datetime
            db.get(Product,1).deleted_at = datetime(2026,10,10); db.commit()
        fresh['idempotency_key'] = 'reliable-plain-deleted'
        assert client.post(PATH, json=fresh).status_code == 404
        general = copy.deepcopy(body); general['idempotency_key'] = 'reliable-plain-generic'
        general['items'][0]['product_id'] = None; general['items'][0]['reference_product_id'] = None
        generic = client.post(PATH, json=general); assert generic.status_code == 201, generic.text
        assert _facts(factory)['stock_replenishment_orders'] == before['stock_replenishment_orders'] + 1
        for table in ['inventory_lots','inventory_movements','inventory_reservations']:
            assert _facts(factory)[table] == before[table]


def test_external_fresh_service_requires_valid_complete_hash(external_stock_app):
    app = external_stock_app
    policy_id, _ = _seed_external_warning(app)
    before = _facts(app.state.factory)
    with app.state.factory() as db:
        actor = db.scalar(select(User).where(User.username == 'p1-40a-admin'))
        args = dict(policy=db.get(InventoryStockPolicy,policy_id),finished_quantity=40,
            order_number='CBW-SYNTHETIC-MISSING-PROOF',idempotency_key='missing-proof',remark=None,user=actor)
        with pytest.raises(TypeError):
            external.create_external_stock_replenishment_purchase(db,**args)
        for invalid in [None,'','a'*63,'g'*64,True]:
            with pytest.raises(external.ExternalPurchaseContractError):
                external.create_external_stock_replenishment_purchase(db,**args,request_hash=invalid)
            db.rollback()
    assert _facts(app.state.factory) == before


def test_source_classification_preserves_all_physical_contract_members(external_stock_app):
    app=external_stock_app
    policy, _ = _seed_external_warning(app)
    with TestClient(app) as client:
        _login(client)
        body=_body(client,policy,'classification-direct-external')
        first=client.post(PATH,json=body);assert first.status_code==201,first.text
        oid=first.json()['id'];item_id=first.json()['items'][0]['id']
        assert client.get(PATH+f'/{oid}/print').status_code==200
        def reported_ids():
            response=client.get('/api/requisition/reported-documents?source_type=stock_replenishment')
            assert response.status_code==200,response.text
            return [row['id'] for row in response.json()['items']]
        assert oid in reported_ids()
        with app.state.factory() as db:
            item=db.get(StockReplenishmentOrderItem,item_id)
            # Synthetic persisted classifier boundary: one physical contract plus
            # one nullable member must preserve the original physical-source rule.
            item.quantity_contract_json='{}'
            db.add(StockReplenishmentOrderItem(**{column.name:getattr(item,column.name)
                for column in StockReplenishmentOrderItem.__table__.columns
                if column.name not in ['id','quantity_contract_json']}))
            db.commit()
        assert client.get(PATH+f'/{oid}/print').status_code==409
        assert oid not in reported_ids()
        with app.state.factory() as db:
            item=db.get(StockReplenishmentOrderItem,item_id)
            unpurchased=StockReplenishmentOrder(order_number='CBW-SYNTHETIC-UNPURCHASED-PHYSICAL',supplier_name=body['supplier_name'],
                customer_id=body['customer_id'],source_type='stock_warning',status='draft',created_by=1,request_hash='b'*64)
            unpurchased.items=[StockReplenishmentOrderItem(**{column.name:getattr(item,column.name)
                for column in StockReplenishmentOrderItem.__table__.columns if column.name not in ['id','replenishment_order_id']})]
            db.add(unpurchased);db.commit();uid=unpurchased.id
        assert client.get(PATH+f'/{uid}/print').status_code==409
        assert uid not in reported_ids()
