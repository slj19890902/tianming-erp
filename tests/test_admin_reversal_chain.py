from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_phase8_finance import finance_api_app, _login, _create_statement, _mark_statement_confirmed
from tests.test_p1_03_manual_size_orders import order_api_app, _manual_payload, _make_fixture_material_valid_for_manual_a1
from tests.test_phase5_orders import _login as order_login
from app.models.finance import Statement, SettlementRecord, StatementAdjustment
from app.models.order import Order


def test_cancelled_order_can_be_saved_again_but_effective_duplicate_cannot(order_api_app):
    app, factory = order_api_app
    _make_fixture_material_valid_for_manual_a1(factory)
    with TestClient(app) as client:
        order_login(client)
        first = client.post('/api/orders', json=_manual_payload())
        assert first.status_code == 201, first.text
        with factory() as db:
            old = db.scalar(select(Order))
            old.status = 'cancelled'
            old_id = old.id
            db.commit()
        second = client.post('/api/orders', json=_manual_payload())
        assert second.status_code == 201, second.text
        assert client.post('/api/orders', json=_manual_payload()).status_code == 409
        with factory() as db:
            assert db.get(Order, old_id).status == 'cancelled'
            assert len(list(db.scalars(select(Order)))) == 2


def test_payment_reversal_replay_permissions_and_statement_history(finance_api_app):
    app, factory = finance_api_app
    with TestClient(app) as client:
        _login(client, 'admin')
        statement = _mark_statement_confirmed(factory, _create_statement(client))
        sid = statement['id']
        payment = client.put(f'/api/finance/statements/{sid}/settle', json={
            'amount':'80.80', 'settlement_date':'2026-06-14', 'account':'测试收款',
            'expected_version':statement['version'], 'expected_ledger_version':1,
            'idempotency_key':'admin-chain-payment'})
        assert payment.status_code == 200, payment.text
        with factory() as db:
            rid = db.scalar(select(SettlementRecord.id))
        body = dict(expected_version=statement['version'], expected_ledger_version=2,
            idempotency_key='admin-chain-reverse-payment')
        url = f'/api/finance/statements/{sid}/settlements/{rid}/reverse'
        _login(client, 'finance')
        assert client.post(url, json=body).status_code == 403
        _login(client, 'admin')
        assert client.post(url, json={**body, 'expected_ledger_version':1}).status_code == 409
        result = client.post(url, json=body)
        assert result.status_code == 200, result.text
        assert client.post(url, json=body).json() == result.json()
        detail = client.get(f'/api/finance/statements/{sid}').json()
        assert float(detail['settled_amount']) == 0
        assert detail['settlements'] == []
        assert float(detail['reversed_settlements'][0]['settled_amount']) == 80.8
        reopened = client.post(f'/api/finance/statements/{sid}/reopen', json={
            'expected_version':detail['version'], 'reason':'隔离回退验证'})
        assert reopened.status_code == 200, reopened.text
        cancelled = client.post(f'/api/finance/statements/{sid}/cancel')
        assert cancelled.status_code == 200, cancelled.text
        assert client.post(f'/api/finance/statements/{sid}/cancel').json() == cancelled.json()
        history = client.get(f'/api/finance/statements/{sid}').json()
        assert history['confirmation_status'] == 'cancelled'
        assert history['voided_versions'][0]['snapshot']['items']
        with factory() as db:
            assert db.get(Statement, sid) is not None
            assert db.scalar(select(StatementAdjustment).where(StatementAdjustment.action == 'cancel_statement'))
