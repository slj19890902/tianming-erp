from decimal import Decimal

from fastapi.testclient import TestClient

from test_fin001_invoice_tasks import fin001_app, _login, _complete_invoice_profile
from test_customer_invoice_merge_20261017 import setup_customer, merge
from test_partner_invoice_merge import issue_payload


def queue(client, kind='', workspace='reconciliation'):
    response = client.get('/api/finance/current-customer-months', params={
        'all_open': True, 'workspace': workspace,
        **({'balance_type': kind} if kind else {}),
    })
    assert response.status_code == 200, response.text
    return response.json()


def assert_uninvoiced_visible(client, task_id, status):
    for kind in ('', 'pending_invoice'):
        data = queue(client, kind)
        assert data['queue_counts']['pending_invoice'] == 1
        assert len(data['items']) == 1
        group = data['items'][0]
        assert group['primary_action'] == 'invoice'
        assert group['queue_status'] == '待开票'
        assert group['statements'][0]['active_invoice_tasks'] == [{'id': task_id, 'status': status}]
        detail = client.get('/api/finance/statements/1').json()
        assert detail['active_invoice_tasks'] == group['statements'][0]['active_invoice_tasks']
        assert Decimal(str(group['pending_invoice_amount'])) == Decimal('113')


def test_workbench_keeps_entire_unissued_lifecycle_and_returns_after_cancel(fin001_app):
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        before = queue(client)['items'][0]
        assert before['primary_action'] == 'reconcile'
        # Download itself is read-only; the UI's explicit confirm action follows.
        assert client.get('/api/finance/statements/1/export').status_code == 200
        assert queue(client)['items'][0]['primary_action'] == 'reconcile'
        assert client.post('/api/finance/statements/1/confirm', json={'expected_version': 1}).status_code == 200
        _complete_invoice_profile(client)
        created = client.post('/api/finance/statements/1/invoice-tasks', json={
            'expected_version': 2, 'idempotency_key': 'workbench-lifecycle'})
        assert created.status_code == 201, created.text
        task = created.json()
        assert_uninvoiced_visible(client, task['id'], task['status'])
        ready = client.post(f"/api/finance/invoice-tasks/{task['id']}/confirm", json={'expected_version': task['version']})
        assert ready.status_code == 200, ready.text
        assert_uninvoiced_visible(client, task['id'], 'ready')
        assert client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx").status_code == 200
        assert_uninvoiced_visible(client, task['id'], 'exported')
        task = client.get(f"/api/finance/invoice-tasks/{task['id']}").json()
        failed = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json={
            'status': 'failed', 'failure_reason': 'synthetic tax import rejection',
            'expected_version': task['version']})
        assert failed.status_code == 200, failed.text
        assert_uninvoiced_visible(client, task['id'], 'failed')
        cancelled = client.post(f"/api/finance/invoice-tasks/{task['id']}/void", json={
            'expected_version': failed.json()['version']})
        assert cancelled.status_code == 200, cancelled.text
        data = queue(client)
        assert data['queue_counts']['pending_invoice'] == 0
        assert data['items'][0]['primary_action'] == 'reconcile'
        assert data['items'][0]['statements'][0]['active_invoice_tasks'] == []
        assert data['items'][0]['queue_status'] == '待修改'
        assert client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx").status_code == 409


def test_merged_invoice_is_visible_for_each_source_until_real_issue(fin001_app):
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        setup_customer(client, factory)
        created, _ = merge(client, 'workbench-merged-lifecycle')
        assert created.status_code == 201, created.text
        task = created.json()
        data = queue(client, 'pending_invoice')
        assert data['queue_counts']['pending_invoice'] == 1
        assert {s['id'] for group in data['items'] for s in group['statements']} == {1, 2}
        for group in data['items']:
            assert group['primary_action'] == 'invoice'
            assert group['statements'][0]['active_invoice_tasks'] == [{'id': task['id'], 'status': task['status']}]
        assert client.get(f"/api/finance/invoice-tasks/{task['id']}/tax-template.xlsx").status_code == 200
        task = client.get(f"/api/finance/invoice-tasks/{task['id']}").json()
        issued = client.post(f"/api/finance/invoice-tasks/{task['id']}/result", json=issue_payload(task))
        assert issued.status_code == 200, issued.text
        assert queue(client, 'pending_invoice')['items'] == []
        assert queue(client)['items'] == []
        collections = queue(client, 'pending_payment', 'collections')
        assert {s['id'] for group in collections['items'] for s in group['statements']} == {1, 2}
        assert all(group['primary_action'] == 'payment' for group in collections['items'])
        # A real invoice cannot be sent back by the ordinary unissued-task action.
        blocked = client.post(f"/api/finance/invoice-tasks/{task['id']}/void", json={'expected_version': issued.json()['version']})
        assert blocked.status_code == 409


def test_real_invoice_reversal_returns_to_reconciliation_without_stale_invoice_task(fin001_app):
    from test_invoice_reversals import issue, payload
    app, _ = fin001_app
    with TestClient(app) as client:
        _login(client)
        invoice_id = issue(client)
        assert not queue(client)['items']
        assert queue(client, 'pending_payment', 'collections')['items']
        request = payload(client, invoice_id)
        reverted = client.post(f'/api/finance/invoices/{invoice_id}/void', json=request)
        assert reverted.status_code == 200, reverted.text
        assert not queue(client, 'pending_payment', 'collections')['items']
        assert not queue(client, 'pending_invoice')['items']
        group = queue(client)['items'][0]
        assert group['primary_action'] == 'reconcile'
        assert group['queue_status'] == '待修改'
        detail = client.get('/api/finance/statements/1').json()
        assert detail['active_invoice_tasks'] == []
        assert detail['invoices'][0]['invoice_status'] == 'voided'
        assert client.post(f'/api/finance/invoices/{invoice_id}/void', json=request).json() == reverted.json()
        assert len(queue(client)['items'][0]['statements']) == 1


def test_partial_customer_scope_does_not_expose_merged_task_entry(fin001_app):
    from sqlalchemy import select
    from app.models.user import User
    from app.models.access_control import UserCustomerScope
    from test_partner_invoice_merge import prepare_partner, merge as partner_merge
    app, factory = fin001_app
    with TestClient(app) as client:
        _login(client)
        prepare_partner(client, factory)
        created = partner_merge(client)
        assert created.status_code == 201, created.text
        task_id = created.json()['id']
        with factory() as db:
            user = db.scalar(select(User).where(User.username == 'fin001-finance'))
            user.customer_access_mode = 'selected'
            db.add(UserCustomerScope(user_id=user.id, customer_id=1))
            db.commit()
        assert client.get(f'/api/finance/invoice-tasks/{task_id}').status_code == 403
        groups = queue(client)['items']
        assert all(task['id'] != task_id for g in groups for s in g['statements'] for task in s['active_invoice_tasks'])
        detail = client.get('/api/finance/statements/1')
        assert detail.status_code in (200, 403)
        if detail.status_code == 200:
            assert detail.json()['active_invoice_tasks'] == []
            assert detail.json()['invoice_task_pending'] is True
