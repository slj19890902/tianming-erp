import pytest
from fastapi.testclient import TestClient
from test_fin001_invoice_tasks import fin001_app, _login, _complete_invoice_profile


@pytest.mark.parametrize('filled', [False, True])
def test_buyer_optional_fields_save_generate_and_freeze(fin001_app, filled):
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        seller = _complete_invoice_profile(client)
        optional = {'invoice_address': '测试地址', 'invoice_phone': '0512-12345678',
                    'bank_name': '测试银行', 'bank_account': '001234567890'} if filled else {}
        payload = {'invoice_title': '测试抬头', 'tax_no': '913200000000000002',
                   'default_seller_id': seller, 'confirmation_status': 'confirmed', 'expected_version': 1, **optional}
        response = client.put('/api/customers/1/invoice-profile', json=payload)
        assert response.status_code == 200, response.text
        assert client.post('/api/finance/statements/1/confirm', json={'expected_version': 1}).status_code == 200
        task = client.post('/api/finance/statements/1/invoice-tasks', json={'expected_version': 2, 'idempotency_key': 'optional-fields-task'})
        assert task.status_code == 201, task.text
        task_id = task.json()['id']
        detail = client.get(f'/api/finance/invoice-tasks/{task_id}').json()
        for key in ('invoice_address', 'invoice_phone', 'bank_name', 'bank_account'):
            assert (detail['buyer_snapshot'][key] or '') == optional.get(key, '')
        assert client.put('/api/customers/1/invoice-profile', json={**payload, 'expected_version': 2, 'invoice_address': '新地址'}).status_code == 200
        assert client.get(f'/api/finance/invoice-tasks/{task_id}').json()['buyer_snapshot'] == detail['buyer_snapshot']


@pytest.mark.parametrize('missing', ['invoice_title', 'tax_no'])
@pytest.mark.parametrize('status', ['pending', 'confirmed'])
def test_buyer_title_and_tax_number_remain_required(fin001_app, missing, status):
    app, _ = fin001_app
    with TestClient(app) as client:
        _login(client)
        seller = _complete_invoice_profile(client)
        payload = {'invoice_title': '测试抬头', 'tax_no': '913200000000000002',
                   'default_seller_id': seller, 'confirmation_status': status, 'expected_version': 1}
        payload[missing] = '  '
        response = client.put('/api/customers/1/invoice-profile', json=payload)
        assert response.status_code == 409
        assert client.get('/api/customers/1/invoice-profile').json()['version'] == 1


def test_grouped_buyer_uses_same_optional_field_rules(fin001_app):
    from app.models.finance import Statement
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        seller = _complete_invoice_profile(client)
        entity = client.post('/api/finance/settlement-entities', json={
            'entity_code': 'OPTIONAL', 'entity_name': '合作结算抬头',
            'tax_no': '913200000000000003', 'default_seller_id': seller,
            'confirmation_status': 'confirmed'})
        assert entity.status_code == 201, entity.text
        entity_id = entity.json()['id']
        profile = client.put('/api/customers/1/invoice-profile', json={
            'settlement_entity_id': entity_id, 'default_seller_id': seller,
            'confirmation_status': 'confirmed', 'expected_version': 1})
        assert profile.status_code == 200, profile.text
        with factory() as db:
            db.get(Statement, 1).settlement_entity_id = entity_id
            db.commit()
        assert client.post('/api/finance/statements/1/confirm', json={'expected_version': 1}).status_code == 200
        task = client.post('/api/finance/statements/1/invoice-tasks', json={
            'expected_version': 2, 'idempotency_key': 'grouped-optional-fields'})
        assert task.status_code == 201, task.text
        detail = client.get(f"/api/finance/invoice-tasks/{task.json()['id']}").json()
        assert detail['buyer_snapshot']['invoice_title'] == '合作结算抬头'
        assert detail['buyer_snapshot']['invoice_address'] is None

