from sqlalchemy import select
from test_scoped_business_approvals import configured
from test_n028_customer_scopes import n028_customer_scope_app, _login


def test_public_fields_are_supported_by_actual_update_schema():
    from app.services.business_approvals import specs
    from app.services.business_request_fields import FIELDS
    for action, fields in FIELDS.items():
        assert set(fields) <= set(specs(action)[0].model_fields)
    assert {"customer_drawing_number", "customer_category"} <= FIELDS["product_update"].keys()


def submit(client, ids, patch, key):
    return client.post('/api/business-approvals', json={
        'action':'product_update','customer_id':ids['customer'],'target_id':ids['product'],
        'payload':patch, 'idempotency_key':key})


def test_retired_field_is_rejected(configured):
    client, ids, factory = configured
    from app.models.product import Product
    with factory() as db:
        version = db.get(Product, ids['product']).version
    result = submit(client, ids, {'customer_product_name':'retired', 'expected_version':version}, 'approval-retired-field')
    assert result.status_code == 422, result.text


def test_drawing_and_category_approval_readback(configured):
    client, ids, factory = configured
    from app.models.product import Product
    with factory() as db:
        version = db.get(Product, ids['product']).version
    patch = {'customer_drawing_number':'DRAW-009','customer_category':'测试箱型','expected_version':version}
    created = submit(client, ids, patch, 'approval-drawing-category')
    assert created.status_code == 201, created.text
    _login(client, 'n028-admin', 'AdminPass123!')
    path = f"/api/business-approvals/{created.json()['id']}/review"
    result = client.post(path, json={'approve':True,'expected_version':1})
    if result.status_code == 409 and isinstance(result.json()['detail'],dict) and result.json()['detail'].get('confirmation_token'):
        result = client.post(path, json={'approve':True,'expected_version':1,'confirmation_token':result.json()['detail']['confirmation_token']})
    assert result.status_code == 200, result.text
    assert set(result.json()['result']['applied_fields']) == {'customer_drawing_number','customer_category'}
    with factory() as db:
        p = db.get(Product, ids['product'])
        assert p.customer_drawing_number == 'DRAW-009' and p.customer_category == '测试箱型'


def test_no_change_result_is_explicit(configured):
    client, ids, factory = configured
    from app.models.product import Product
    with factory() as db:
        p = db.get(Product,ids['product'])
        patch = {'customer_drawing_number':p.customer_drawing_number,'expected_version':p.version}
    created = submit(client,ids,patch,'approval-no-change')
    assert created.status_code == 201, created.text
    _login(client,'n028-admin','AdminPass123!')
    result = client.post(f"/api/business-approvals/{created.json()['id']}/review",json={'approve':True,'expected_version':1})
    assert result.status_code == 200, result.text
    assert result.json()['result']['no_change'] is True
