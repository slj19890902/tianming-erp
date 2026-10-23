import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_manual_sheet_deduction_20261013 import seed, ordinary_seed, payload


@pytest.mark.parametrize('bom', [False, True])
def test_full_stock_can_reduce_and_restore_only_shortfall(composite_requisition_app, bom):
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    app, sessions = composite_requisition_app
    lot_id = seed(sessions) if bom else ordinary_seed(sessions)
    base = '/api/warehouse/semi-finished/' + ('bom-components/1' if bom else 'order-items/1')
    with TestClient(app) as client:
        _login(client)
        preview = client.get(base + '/manual-inventory').json()
        quantity = preview['remaining_required_piece_qty']
        reserved = client.post(base + '/manual-reserve', json=payload(preview, quantity))
        assert reserved.status_code == 200, reserved.text
        fresh = client.get(base + '/manual-inventory').json()
        assert fresh['remaining_required_piece_qty'] == 0
        existing = fresh['existing_reservations']
        assert len(existing) == 1 and existing[0]['remaining_stock_quantity'] == quantity
        request = {'component_type': 'whole', 'expected_adjustment_revision': fresh['adjustment_revision'],
                   'idempotency_key': 'reduce-full-stock-once',
                   'items': [{'reservation_id': existing[0]['reservation_id'], 'keep_stock_quantity': quantity - 4}]}
        response = client.post(base + '/manual-adjust', json=request)
        assert response.status_code == 200, response.text
        assert response.json()['remaining_required_piece_qty'] == 4
        assert response.json()['released_stock_quantity'] == 4
        assert client.post(base + '/manual-adjust', json=request).json()['replayed']
        changed = {**request, 'items': [{**request['items'][0], 'keep_stock_quantity': 0}]}
        assert client.post(base + '/manual-adjust', json=changed).status_code == 409
        pending = client.get('/api/requisition/pending')
        assert pending.status_code == 200, pending.text
        rows = pending.json()['items']
        assert rows, 'reduced stock left the order stranded outside pending requisition'
        current = client.get(base + '/manual-inventory').json()
        assert current['remaining_required_piece_qty'] == 4
        assert current['existing_reservations'][0]['remaining_stock_quantity'] == quantity - 4
    with sessions() as db:
        lot = db.get(InventoryLot, lot_id)
        assert lot.quantity_reserved == quantity - 4
        assert lot.quantity_available == 204 - quantity + 4
        reservation = db.scalar(select(InventoryReservation))
        assert reservation.released_stock_quantity == 4


def test_adjustment_rejects_stale_preview_and_preserves_inventory(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot
    app, sessions = composite_requisition_app
    lot_id = ordinary_seed(sessions)
    base = '/api/warehouse/semi-finished/order-items/1'
    with TestClient(app) as client:
        _login(client)
        p = client.get(base + '/manual-inventory').json()
        assert client.post(base + '/manual-reserve', json=payload(p, 10)).status_code == 200
        p = client.get(base + '/manual-inventory').json()
        request = {'component_type': 'whole', 'expected_adjustment_revision': p['adjustment_revision'],
                   'idempotency_key': 'stale-adjustment-once',
                   'items': [{'reservation_id': p['existing_reservations'][0]['reservation_id'], 'keep_stock_quantity': 5}]}
        with sessions() as db:
            db.get(InventoryLot, lot_id).version += 1
            db.commit()
        assert client.post(base + '/manual-adjust', json=request).status_code == 409
    with sessions() as db:
        assert db.get(InventoryLot, lot_id).quantity_reserved == 10

BASE = '/api/warehouse/semi-finished/order-items/1'


def adjustment(client, keep, key='adjust-test-request', base=BASE):
    data = client.get(base + '/manual-inventory').json()
    return {'component_type': 'whole', 'expected_adjustment_revision': data['adjustment_revision'],
            'idempotency_key': key, 'items': [{'reservation_id': row['reservation_id'],
                'keep_stock_quantity': keep} for row in data['existing_reservations']]}


def test_internal_full_adoption_restores_pending_without_reviving_old_draft(composite_requisition_app):
    from tests.test_stock_v3_internal_sheet_20261014 import internal_seed
    from app.models.requisition import RequisitionItem
    from app.models.production import ProductionTask
    from app.services.production_workflow import refresh_production_task
    app, sessions = composite_requisition_app
    _, row_id = internal_seed(sessions)
    with sessions() as db:
        task = refresh_production_task(db, 1, create_if_missing=True)
        task_id = task.id
        db.commit()
    with TestClient(app) as client:
        _login(client)
        data = client.get(BASE + '/manual-inventory').json()
        assert client.post(BASE + '/manual-reserve', json=payload(data, 100)).status_code == 200
        with sessions() as db:
            assert db.get(ProductionTask, task_id).status == 'pending'
            assert db.get(RequisitionItem, row_id).status == 'voided'
        response = client.post(BASE + '/manual-adjust', json=adjustment(client, 70))
        assert response.status_code == 200, response.text
        assert response.json()['remaining_required_piece_qty'] == 30
        rows = client.get('/api/requisition/pending').json()['items']
        row = next(row for row in rows if row.get('item_id') == 1)
        assert row['remaining_required_piece_qty'] == 30, row
        assert row['requisition_qty'] == 30, row
    with sessions() as db:
        assert db.get(RequisitionItem, row_id).status == 'voided'
        assert db.get(RequisitionItem, row_id).requisition_qty == 0
        assert db.get(ProductionTask, task_id).status == 'waiting_material'
        assert len(db.scalars(select(ProductionTask)).all()) == 1


def test_official_purchase_stays_frozen_after_reducing_inventory(composite_requisition_app):
    from tests.test_stock_v3_internal_sheet_20261014 import internal_seed, supplier_seed
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
    app, sessions = composite_requisition_app
    internal_seed(sessions, qty=80)
    supplier_id = supplier_seed(sessions)
    with TestClient(app) as client:
        _login(client)
        data = client.get(BASE + '/manual-inventory').json()
        assert client.post(BASE + '/manual-reserve', json=payload(data, 80)).status_code == 200
        response = client.post(BASE + '/manual-adjust', json=adjustment(client, 50))
        assert response.status_code == 200, response.text
        assert response.json()['remaining_required_piece_qty'] == 30
        assert response.json()['committed_piece_qty'] == 20
    with sessions() as db:
        row = db.get(SupplierRequisitionOrderItem, supplier_id)
        assert (row.requisition_qty, row.version, row.status) == (20, 1, 'active')


def test_release_uses_physical_sheet_yield_not_product_piece_count(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot
    app, sessions = composite_requisition_app
    lot_id = seed(sessions, yield_factor=30)
    base = '/api/warehouse/semi-finished/bom-components/1'
    with TestClient(app) as client:
        _login(client)
        data = client.get(base + '/manual-inventory').json()
        assert data['remaining_required_piece_qty'] == 20
        assert data['items'][0]['stock_yield_per_sheet'] == 30
        assert client.post(base + '/manual-reserve', json=payload(data, 1)).status_code == 200
        response = client.post(base + '/manual-adjust', json=adjustment(client, 0, base=base))
        assert response.status_code == 200, response.text
        assert response.json()['released_stock_quantity'] == 1
        assert response.json()['remaining_required_piece_qty'] == 20
    with sessions() as db:
        assert db.get(InventoryLot, lot_id).quantity_available == 204


@pytest.mark.parametrize('invalid', ['duplicate', 'larger', 'negative', 'fraction', 'foreign'])
def test_invalid_batch_cannot_partially_release(composite_requisition_app, invalid):
    from app.models.warehouse_inventory import InventoryLot
    app, sessions = composite_requisition_app
    lot_id = ordinary_seed(sessions)
    with TestClient(app) as client:
        _login(client)
        data = client.get(BASE + '/manual-inventory').json()
        assert client.post(BASE + '/manual-reserve', json=payload(data, 10)).status_code == 200
        request = adjustment(client, 5)
        if invalid == 'duplicate': request['items'] *= 2
        elif invalid == 'foreign': request['items'].append({'reservation_id': 99999, 'keep_stock_quantity': 0})
        else: request['items'][0]['keep_stock_quantity'] = {'larger': 11, 'negative': -1, 'fraction': 1.5}[invalid]
        result = client.post(BASE + '/manual-adjust', json=request)
        assert result.status_code in (404, 409, 422), result.text
    with sessions() as db:
        assert db.get(InventoryLot, lot_id).quantity_reserved == 10


def test_consumed_stock_cannot_be_adjusted(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.semi_finished_inventory import consume_semi_finished_reservation
    app, sessions = composite_requisition_app
    lot_id = ordinary_seed(sessions)
    with TestClient(app) as client:
        _login(client)
        data = client.get(BASE + '/manual-inventory').json()
        assert client.post(BASE + '/manual-reserve', json=payload(data, 10)).status_code == 200
        with sessions() as db:
            reservation = db.scalar(select(InventoryReservation))
            consume_semi_finished_reservation(db, reservation_id=reservation.id, stock_quantity=1,
                expected_version=db.get(InventoryLot, lot_id).version, operator_id=1,
                idempotency_key='consume-before-adjust')
            db.commit()
        data = client.get(BASE + '/manual-inventory').json()
        assert data['existing_reservations'][0]['can_adjust'] is False
        assert client.post(BASE + '/manual-adjust', json=adjustment(client, 0)).status_code == 409
    with sessions() as db:
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_reserved, lot.quantity_consumed) == (9, 1)


def test_audit_failure_rolls_back_release_and_preserves_retry(composite_requisition_app, monkeypatch):
    import app.services.audit_log as audit
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    app, sessions = composite_requisition_app
    lot_id = ordinary_seed(sessions)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        data = client.get(BASE + '/manual-inventory').json()
        assert client.post(BASE + '/manual-reserve', json=payload(data, 10)).status_code == 200
        request = adjustment(client, 5)
        original = audit.append_audit_event
        def fail(*args, **kwargs): raise RuntimeError('audit unavailable')
        monkeypatch.setattr(audit, 'append_audit_event', fail)
        assert client.post(BASE + '/manual-adjust', json=request).status_code == 500
        with sessions() as db:
            assert db.get(InventoryLot, lot_id).quantity_reserved == 10
            assert db.scalar(select(InventoryReservation)).released_stock_quantity == 0
        monkeypatch.setattr(audit, 'append_audit_event', original)
        result = client.post(BASE + '/manual-adjust', json=request)
        assert result.status_code == 200, result.text


def test_two_reservations_on_same_lot_release_once_each(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot
    app, sessions = composite_requisition_app
    lot_id = ordinary_seed(sessions)
    with TestClient(app) as client:
        _login(client)
        for index in range(2):
            data = client.get(BASE + '/manual-inventory').json()
            assert client.post(BASE + '/manual-reserve', json=payload(data, 5, f'reserve-batch-{index}')).status_code == 200
        request = adjustment(client, 0)
        assert len(request['items']) == 2
        response = client.post(BASE + '/manual-adjust', json=request)
        assert response.status_code == 200, response.text
        assert response.json()['released_stock_quantity'] == 10
    with sessions() as db:
        assert db.get(InventoryLot, lot_id).quantity_available == 204


def test_adjusted_bom_shortfall_can_be_reported_without_rebuying_covered_pieces(composite_requisition_app):
    from tests.test_n039_composite_bom_requisition import _component_payload, _parent_payload
    from app.models.requisition import RequisitionItem
    app, sessions = composite_requisition_app
    seed(sessions)
    base = '/api/warehouse/semi-finished/bom-components/1'
    with TestClient(app) as client:
        _login(client)
        data = client.get(base + '/manual-inventory').json()
        assert client.post(base + '/manual-reserve', json=payload(data, 20)).status_code == 200
        result = client.post(base + '/manual-adjust', json=adjustment(client, 14, base=base))
        assert result.status_code == 200, result.text
        preview = client.post('/api/requisition/bom-sheet-cutting/preview', json=_component_payload(1))
        assert preview.status_code == 200, preview.text
        plan = preview.json()
        assert plan['remaining_required_piece_qty'] == plan['order_purpose_sheet_qty'] == 6
        request = {**_component_payload(1, requisition_qty=6), 'purchase_total_sheet_qty':6,
            'order_purpose_sheet_qty':6, 'stock_purpose_sheet_qty':0,
            'purpose_plan_version':plan['purpose_plan_version'],
            'purpose_plan_fingerprint':plan['purpose_plan_fingerprint']}
        saved = client.post('/api/requisition/batches', json={'supplier_name':'N039 供应商',
            'items':[_parent_payload(), request, _component_payload(2)]})
        assert saved.status_code == 201, saved.text
        assert client.get(base + '/manual-inventory').json()['remaining_required_piece_qty'] == 0
    with sessions() as db:
        assert [row.requisition_qty for row in db.scalars(select(RequisitionItem).order_by(RequisitionItem.id))] == [10,6,30]


@pytest.mark.parametrize('restriction', ['permission', 'customer'])
def test_adjust_and_replay_still_require_current_access(composite_requisition_app, restriction):
    from app.models.user import User
    from app.models.access_control import UserPermissionOverride
    app, sessions = composite_requisition_app
    ordinary_seed(sessions)
    with TestClient(app) as client:
        assert client.get(BASE + '/manual-inventory').status_code == 401
        _login(client)
        data = client.get(BASE + '/manual-inventory').json()
        assert client.post(BASE + '/manual-reserve', json=payload(data, 10)).status_code == 200
        request = adjustment(client, 5)
        assert client.post(BASE + '/manual-adjust', json=request).status_code == 200
        with sessions() as db:
            user = db.get(User, 1)
            user.role = 'finance'
            user.customer_access_mode = 'all' if restriction == 'permission' else 'selected'
            db.add(UserPermissionOverride(user_id=1, permission_code='warehouse.reserve',
                is_allowed=restriction != 'permission'))
            db.commit()
        assert client.post(BASE + '/manual-adjust', json=request).status_code == 403


def test_posted_production_fact_blocks_even_fresh_adjustment(composite_requisition_app):
    from datetime import datetime
    from app.models.production import ProductionCompletion, ProductionCompletionBatch
    from app.models.warehouse_inventory import InventoryLot
    from app.services.production_workflow import refresh_production_task
    app, sessions = composite_requisition_app
    lot_id = ordinary_seed(sessions)
    with TestClient(app) as client:
        _login(client)
        data = client.get(BASE + '/manual-inventory').json()
        assert client.post(BASE + '/manual-reserve', json=payload(data, 20)).status_code == 200
        with sessions() as db:
            task = refresh_production_task(db, 1, create_if_missing=True)
            batch = ProductionCompletionBatch(idempotency_key='posted-fact', request_hash='a'*64,
                item_count=1, completed_by=1, completed_at=datetime.now())
            db.add(batch); db.flush()
            db.add(ProductionCompletion(batch_id=batch.id, task_id=task.id, order_item_id=1,
                expected_version=task.version, quantity=1, initial_disposition='direct',
                completed_by=1, completed_at=datetime.now()))
            db.commit()
        data = client.get(BASE + '/manual-inventory').json()
        assert data['existing_reservations'][0]['can_adjust'] is False
        result = client.post(BASE + '/manual-adjust', json=adjustment(client, 0))
        assert result.status_code == 409, result.text
    with sessions() as db:
        assert db.get(InventoryLot, lot_id).quantity_reserved == 20


def test_concurrent_repeated_adjustment_releases_only_once(composite_requisition_app):
    from concurrent.futures import ThreadPoolExecutor
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    app, sessions = composite_requisition_app
    lot_id = ordinary_seed(sessions)
    with TestClient(app) as first, TestClient(app) as second:
        _login(first); _login(second)
        data = first.get(BASE + '/manual-inventory').json()
        assert first.post(BASE + '/manual-reserve', json=payload(data, 10)).status_code == 200
        request = adjustment(first, 5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(client.post, BASE + '/manual-adjust', json=request) for client in (first, second)]
            results = [job.result() for job in jobs]
        assert [r.status_code for r in results] == [200,200], [(r.status_code,r.text) for r in results]
        assert sum(bool(r.json().get('replayed')) for r in results) == 1
    with sessions() as db:
        assert db.get(InventoryLot, lot_id).quantity_reserved == 5
        assert db.scalar(select(InventoryReservation)).released_stock_quantity == 5
