import copy
import json
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.test_n029_production_integration import n029_delivery_app, _login, _prepare_103_finished_stock
from tests.test_delivery_dispatch_commands import delivery_api_app, seed, command, state
from tests.test_phase7_deliveries import _login as login


def test_order_candidate_price_is_exact_and_permission_checked(n029_delivery_app):
    from decimal import Decimal
    from app.models.order import OrderItem
    app, factory, ids = n029_delivery_app
    _prepare_103_finished_stock(factory, ids)
    with factory() as db:
        item = db.get(OrderItem, ids['task_completed'])
        item.unit_price = Decimal('2.146420')
        db.commit()
    with TestClient(app) as c:
        _login(c)
        response = c.get('/api/deliveries/selection-items', params={'customer_id':ids['customer']})
        assert response.status_code == 200, response.text
        row = next(r for r in response.json()['items'] if r.get('order_item_id') == ids['task_completed'])
        assert Decimal(row['unit_price']) == Decimal('2.14642')
        with patch('app.api.orders._can_view_order_sales_amount', return_value=False):
            hidden = c.get('/api/deliveries/selection-items', params={'customer_id':ids['customer']})
            assert hidden.status_code == 200, hidden.text
            assert all('unit_price' not in r for r in hidden.json()['items'])


def test_print_preview_is_read_only_and_print_dispatch_replays_once(delivery_api_app):
    app, factory = delivery_api_app
    with TestClient(app) as c:
        login(c, 'admin')
        created = seed(c, factory);did=created['id'];before=state(factory)
        preview = c.get(f'/api/deliveries/{did}/print?show_prices=false')
        assert preview.status_code == 200, preview.text
        assert preview.json()['current_actor_id'] > 0
        assert preview.json()['delivery_version'] == created['version']
        body = command(c,did,'print-dispatch-exact-key')
        assert state(factory) == before
        posted = c.put(f'/api/deliveries/{did}/dispatch',json=body)
        assert posted.status_code == 200, posted.text
        after = state(factory)
        assert c.put(f'/api/deliveries/{did}/dispatch',json=body).json() == posted.json()
        assert c.get(f'/api/deliveries/{did}/print?show_prices=false').status_code == 200
        assert state(factory) == after


def test_cleanup_is_reviewed_atomic_audited_and_idempotent(n029_delivery_app):
    from app.models.product import Product
    from app.models.user import User
    from app.services import delivery_text_cleanup_20261017 as cleanup
    app,factory,ids=n029_delivery_app
    with factory() as db:
        from app.models.order import OrderItem
        product=db.get(Product,db.get(OrderItem,ids['task_completed']).product_id)
        product.remark='保留原文（单片尺寸未登记）'
        db.commit();pid=product.id
    with factory() as db:
        actor=db.query(User).filter_by(username='n029-admin').one()
        plan=cleanup.preview(db)
        assert any(e['table']=='products' and e['id']==pid for e in plan['entries'])
        with pytest.raises(HTTPException): cleanup.execute(db,actor=None,plan=plan)
        bad=copy.deepcopy(plan);bad['hash']='f'*64
        with pytest.raises(HTTPException): cleanup.execute(db,actor=actor,plan=bad)
        with patch.object(cleanup,'append_audit_event',side_effect=RuntimeError('fault')):
            with pytest.raises(RuntimeError):cleanup.execute(db,actor=actor,plan=plan)
        db.rollback()
        assert db.get(Product,pid).remark=='保留原文（单片尺寸未登记）'
        result=cleanup.execute(db,actor=actor,plan=plan);db.commit()
        assert db.get(Product,pid).remark=='保留原文'
        assert cleanup.execute(db,actor=actor,plan=plan)==result
        db.commit()


def test_cleaned_die_basis_keeps_legacy_stock_and_assembly_identity():
    from app.services.finished_stock_identity import matches_stock_identity, _child_basis
    from hashlib import sha256
    old={'product_id':10,'unit':'只','spec':'模切成品 CUT；模具 M1；4模（单片尺寸未登记）','assembly':[]}
    new={**old,'spec':'模切成品 CUT；模具 M1；4模'}
    encode=lambda x:json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    assert matches_stock_identity(encode(old),encode(new))
    assert _child_basis(10,1,encode(new))['basis_hash']==sha256(encode(old).encode()).hexdigest()
    assert not matches_stock_identity(encode({**old,'unit':'片'}),encode(new))


def test_identity_suffix_compatibility_does_not_relax_other_specifications():
    from app.services.finished_stock_identity import matches_stock_identity
    assert not matches_stock_identity('{"spec":"200x300"}', '{"spec":"200×300mm"}')
