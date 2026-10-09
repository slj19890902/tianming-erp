from fastapi.testclient import TestClient
from tests.test_company_profiles import _create, _activate
from tests.test_phase7_deliveries import delivery_api_app, _login as delivery_login, _create_payload
from tests.test_fin001_invoice_tasks import fin001_app, _login as invoice_login, _complete_invoice_profile


def test_selected_company_flows_to_new_delivery_and_reprint_stays_frozen(delivery_api_app):
    from app.api.companies import router
    app, _ = delivery_api_app
    app.include_router(router, prefix='/api/system')
    with TestClient(app) as client:
        delivery_login(client, 'admin')
        b = _create(client, '虚构送货乙公司')
        assert _activate(client, b['id']).status_code == 200
        created = client.post('/api/deliveries', json=_create_payload())
        assert created.status_code == 201, created.text
        did = created.json()['id']
        first = client.get(f'/api/deliveries/{did}/print')
        assert first.status_code == 200, first.text
        assert first.json()['sender']['company_name'] == b['company_name']
        c = _create(client, '虚构送货丙公司')
        assert _activate(client, c['id']).status_code == 200
        assert client.get(f'/api/deliveries/{did}/print').json() == first.json()


def test_company_switch_preserves_explicit_invoice_seller_and_profile(fin001_app):
    from app.api.companies import router
    app, _ = fin001_app
    app.include_router(router, prefix='/api/system')
    with TestClient(app) as client:
        invoice_login(client, 'fin001-admin')
        sid = _complete_invoice_profile(client)
        original = client.get('/api/customers/1/invoice-profile').json()
        b = _create(client, '虚构开单乙公司')
        assert _activate(client, b['id']).status_code == 200
        assert client.get('/api/customers/1/invoice-profile').json() == original
        assert client.post('/api/finance/statements/1/confirm', json={'expected_version':1}).status_code == 200
        r=client.post('/api/finance/statements/1/invoice-tasks',json={'expected_version':2,'idempotency_key':'company-explicit-invoice-seller','seller_entity_id':sid})
        assert r.status_code == 201,r.text
        assert r.json()['seller_entity_id'] == sid
