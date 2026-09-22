from fastapi.testclient import TestClient
from sqlalchemy import select
from test_n029_production_service import production_app, _login, _complete
from app.models.production import ProductionTask
from app.models.warehouse_inventory import InventoryReservation, InventoryLot
from app.models.audit import OperationLog


def test_inventory_only_pending_shows_input_output_and_assigned_material(production_app):
    app, factory, ids = production_app
    with factory() as db:
        task = db.get(ProductionTask, ids['cases']['stock']['task'])
        task.material_input_quantity = 0
        db.commit()
    with TestClient(app) as client:
        _login(client)
        response = client.get('/api/production/tasks', params={'status': 'pending'})
        assert response.status_code == 200
        row = next(r for r in response.json()['items'] if r['id'] == ids['cases']['stock']['task'])
        assert row['available_material_input_quantity'] == 6
        assert row['planned_output_quantity'] == 6
        assert row['posted_output_quantity'] == 0
        sources = row['customer_board_preparation_sources']
        assert sum(s['remaining_sheet_quantity'] for s in sources) == 6
        assert all(s['location_id'] and s['location_name'] and s['material_kind'] for s in sources)
        assert all('material_code' not in s and 'cost' not in s and 'supplier' not in s for s in sources)
    with factory() as db:
        assert db.get(ProductionTask, ids['cases']['stock']['task']).material_input_quantity == 0
        reservation = db.scalar(select(InventoryReservation).where(InventoryReservation.order_item_id == ids['cases']['stock']['item']))
        assert reservation.consumed_stock_quantity == 0


def test_material_list_is_scoped_grouped_readonly_and_print_audited(production_app):
    app, factory, ids = production_app
    selected = [ids['cases'][k]['task'] for k in ('stock', 'direct')]
    with TestClient(app) as client:
        assert client.post('/api/production/material-list', json={'task_ids':selected}).status_code == 401
        _login(client)
        listed = client.post('/api/production/material-list', json={'task_ids':selected + selected})
        assert listed.status_code == 200, listed.text
        assert len(listed.json()['tasks']) == 2
        assert sum(s['remaining_sheet_quantity'] for s in listed.json()['sources']) == 11
        printed = client.post('/api/production/material-list/print', json={'task_ids':selected})
        assert printed.status_code == 200, printed.text
        assert printed.json()['print_count'] == 1
        reopened = client.post('/api/production/material-list', json={'task_ids':selected}).json()
        assert reopened['print_count'] == 1
        assert all('/mobile/?task_id=' in t['task_url'] and t['task_url'].endswith('#production') for t in reopened['tasks'])
        search = client.get('/api/production/tasks', params={'status':'pending', 'q':'nothing-matches', 'page':1})
        assert search.status_code == 200 and search.json()['total'] == 0
        _login(client, 'n029-scoped')
        forbidden = client.post('/api/production/material-list', json={'task_ids':[ids['cases']['cross']['task']]})
        assert forbidden.status_code in (403,404), forbidden.text
    with factory() as db:
        assert all(r.consumed_stock_quantity == 0 for r in db.scalars(select(InventoryReservation)))
        assert db.scalar(select(OperationLog).where(OperationLog.action_code == 'production.material_list.print'))


def test_shared_lot_is_counted_once_with_each_orders_remaining_allocation(production_app):
    app, factory, ids = production_app
    selected = [ids['cases'][k]['task'] for k in ('stock', 'direct')]
    with factory() as db:
        reservations = [db.scalar(select(InventoryReservation).where(InventoryReservation.order_item_id == ids['cases'][k]['item'])) for k in ('stock','direct')]
        lot = db.get(InventoryLot, reservations[0].inventory_lot_id)
        old = db.get(InventoryLot, reservations[1].inventory_lot_id)
        lot.quantity_reserved += old.quantity_reserved
        old.quantity_reserved = 0
        reservations[1].inventory_lot_id = lot.id
        db.commit()
    with TestClient(app) as client:
        _login(client)
        listed = client.post('/api/production/material-list', json={'task_ids':selected})
        assert listed.status_code == 200, listed.text
        assert len(listed.json()['sources']) == 1
        source = listed.json()['sources'][0]
        assert source['remaining_sheet_quantity'] == 11
        assert sorted(a['quantity'] for a in source['allocations']) == [5, 6]


def test_output_preview_exact_task_qr_and_history_filters(production_app):
    app, factory, ids = production_app
    task_id = ids['cases']['stock']['task']
    with TestClient(app) as client:
        _login(client)
        payload = dict(task_id=task_id, expected_version=1, input_quantity=6, output_quantity=5)
        preview = client.post('/api/production/output-preview', json=payload)
        assert preview.status_code == 200, preview.text
        assert preview.json()['order_quantity'] == 5 and preview.json()['defective_quantity'] == 1
        assert preview.json()['allowed']
        assert not client.post('/api/production/output-preview', json=payload | {'input_quantity':7}).json()['allowed']
        assert client.post('/api/production/output-preview', json=payload | {'expected_version':99}).status_code == 409
        _login(client, 'n029-scoped')
        assert client.post('/api/production/output-preview', json=payload | {'task_id':ids['cases']['cross']['task']}).status_code == 403
        _login(client)
        completed = _complete(client, ids, 'stock', idempotency_key='workspace-history')
        assert completed.status_code == 200, completed.text
        for combined in (False, True):
            params = dict(include_stock=combined, record_type='manual', operator_name='N029 Admin', page=1)
            history = client.get('/api/production/completions', params=params)
            assert history.status_code == 200, history.text
            assert history.json()['total'] == 1
            assert history.json()['items'][0]['origin'] == 'manual'
            assert client.get('/api/production/completions', params=params | {'record_type':'receipt_auto'}).json()['total'] == 0
            assert client.get('/api/production/completions', params=params | {'operator_name':'Nobody'}).json()['total'] == 0
    from app.services.production_workflow import find_pending_production_task_lookup_rows
    with factory() as db:
        exact_id = ids['cases']['direct']['task']
        rows, total = find_pending_production_task_lookup_rows(db, allowed_customer_ids=None, keyword=f'TMPT:{exact_id}')
        assert total == 1 and rows[0]['task_id'] == exact_id
        rows, total = find_pending_production_task_lookup_rows(db, allowed_customer_ids=set(), keyword=f'TMPT:{exact_id}')
        assert rows == [] and total == 0
