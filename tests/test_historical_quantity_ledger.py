"""Audited corrections must preserve old documents and survive replay/rollback."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.delivery import DeliveryItem
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.services.historical_quantity_ledger import PREFIX, post_audited_quantity_batch
from app.services.warehouse_inventory import WarehouseInventoryError, _balances
from tests.test_delivery_physical_quantities import seed_physical
from tests.test_p1_15b_unordered_finished_delivery import (
    unordered_finished_delivery_app, _login, _short_receipt_payload,
)


def legacy_shipment(app, factory):
    customer, product, lot_id, _ = seed_physical(factory, 200, external=False)
    with TestClient(app) as client:
        _login(client)
        response = client.post('/api/deliveries', json=dict(
            customer_id=customer, delivery_date='2026-07-29', source_mode='unordered_finished',
            lines=[dict(source_type='finished_stock', product_id=product,
                delivered_quantity=100, unit_price='3.60',
                allocations=[dict(inventory_lot_id=lot_id, quantity=100)])]))
        assert response.status_code == 201, response.text
        delivery_id = response.json()['id']
        response = client.put(f'/api/deliveries/{delivery_id}/dispatch')
        assert response.status_code == 200, response.text
    with factory() as db:
        item = db.scalar(select(DeliveryItem).where(DeliveryItem.delivery_id == delivery_id))
        item.quantity_contract_json = None  # Reproduce pre-snapshot history in isolation.
        db.commit()
        lot = db.get(InventoryLot, lot_id)
        movements = db.scalars(select(InventoryMovement).where(
            InventoryMovement.inventory_lot_id == lot_id).order_by(InventoryMovement.id)).all()
        original = {row.id: {c.name: getattr(row, c.name) for c in row.__table__.columns}
                    for row in movements}
        inbound = next(row for row in movements if row.movement_type == 'manual_in')
        outbound = next(row for row in movements if row.movement_type == 'consume')
        before = _balances(lot)
        first = dict(kind='inbound_basis', quantity=200, source_movement_id=inbound.id,
            lot_id=lot_id, customer_id=customer, product_id=product,
            before=before, expected_version=lot.version)
        second = dict(first, kind='outbound_basis', quantity=100, source_movement_id=outbound.id,
            before=dict(before, available=before['available'] + 200), expected_version=lot.version + 1)
    return lot_id, delivery_id, [first, second], original


def test_separate_corrections_replay_across_sessions_and_keep_history(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    lot_id, delivery_id, events, original = legacy_shipment(app, factory)
    with factory() as db:
        result = post_audited_quantity_batch(db, events, operator_id=1)
        db.commit()
        assert all(row['created'] for row in result)
    with factory() as db:
        replay = post_audited_quantity_batch(db, deepcopy(events), operator_id=1)
        db.commit()
        assert [r['movement_id'] for r in replay] == [r['movement_id'] for r in result]
        assert not any(r['created'] for r in replay)
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_consumed) == (200, 200)
        for movement_id, values in original.items():
            row = db.get(InventoryMovement, movement_id)
            assert {c.name: getattr(row, c.name) for c in row.__table__.columns} == values
        item = db.scalar(select(DeliveryItem).where(DeliveryItem.delivery_id == delivery_id))
        assert item.delivered_quantity == 100 and item.quantity_contract_json is None
        conflicting = deepcopy(events)
        conflicting[0]['quantity'] += 1
        with pytest.raises(WarehouseInventoryError, match='不同校正'):
            post_audited_quantity_batch(db, conflicting, operator_id=1)


def test_failed_second_event_rolls_back_first_even_if_caller_commits(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    lot_id, _, events, _ = legacy_shipment(app, factory)
    invalid = deepcopy(events)
    invalid[1]['expected_version'] += 1
    with factory() as db:
        with pytest.raises(WarehouseInventoryError, match='重新只读审核'):
            post_audited_quantity_batch(db, invalid, operator_id=1)
        db.commit()
    with factory() as db:
        assert _balances(db.get(InventoryLot, lot_id)) == events[0]['before']
        assert not db.scalar(select(InventoryMovement.id).where(
            InventoryMovement.idempotency_key.startswith(PREFIX)))
        assert all(r['created'] for r in post_audited_quantity_batch(db, events, operator_id=1))
        db.commit()


def test_permission_and_source_identity_fail_closed(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    lot_id, _, events, _ = legacy_shipment(app, factory)
    with factory() as db:
        db.get(User, 1).is_active = False
        db.commit()
        with pytest.raises(WarehouseInventoryError, match='有效管理员'):
            post_audited_quantity_batch(db, events, operator_id=1)
        db.get(User, 1).is_active = True
        db.commit()
        for change, message in [({'customer_id': 999}, '身份不符'),
                                ({'source_movement_id': events[1]['source_movement_id']}, '原入库')]:
            invalid = deepcopy(events)
            invalid[0].update(change)
            with pytest.raises(WarehouseInventoryError, match=message):
                post_audited_quantity_batch(db, invalid, operator_id=1)
        db.commit()
        assert _balances(db.get(InventoryLot, lot_id)) == events[0]['before']


def test_corrected_legacy_dispatch_cancel_restores_physical_quantity(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    lot_id, delivery_id, events, _ = legacy_shipment(app, factory)
    with factory() as db:
        post_audited_quantity_batch(db, events, operator_id=1)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        cancelled = client.put(f'/api/deliveries/{delivery_id}/cancel')
        assert cancelled.status_code == 200, cancelled.text
        cancelled = client.put(f'/api/deliveries/{delivery_id}/cancel')
        assert cancelled.status_code == 409 and '已作废' in cancelled.text, cancelled.text
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_consumed) == (400, 0)
        item = db.scalar(select(DeliveryItem).where(DeliveryItem.delivery_id == delivery_id))
        assert item.delivered_quantity == 100


@pytest.mark.parametrize('received', [90, 99])
def test_corrected_legacy_partial_return_and_reconsume_cycles(unordered_finished_delivery_app, received):
    app, factory = unordered_finished_delivery_app
    lot_id, delivery_id, events, _ = legacy_shipment(app, factory)
    with factory() as db:
        post_audited_quantity_batch(db, events, operator_id=1)
        db.commit()
        item_id = db.scalar(select(DeliveryItem.id).where(DeliveryItem.delivery_id == delivery_id))
    with TestClient(app) as client:
        _login(client)
        receipt_id = None
        for _ in range(2):
            payload = _short_receipt_payload(delivery_id, item_id, quantity=received)
            if receipt_id is None:
                receipt = client.post('/api/finance/return_receipts', json=payload)
                assert receipt.status_code == 201, receipt.text
                receipt_id = receipt.json()['id']
            else:
                receipt = client.put(f'/api/finance/return_receipts/{receipt_id}', json=payload)
                assert receipt.status_code == 200, receipt.text
            with factory() as db:
                lot = db.get(InventoryLot, lot_id)
                assert (lot.quantity_available, lot.quantity_consumed) == (
                    200 + (100 - received) * 2, received * 2)
                assert db.get(DeliveryItem, item_id).delivered_quantity == 100
            cancelled = client.post(f'/api/finance/return_receipts/{receipt_id}/cancel')
            assert cancelled.status_code == 200, cancelled.text
            with factory() as db:
                lot = db.get(InventoryLot, lot_id)
                assert (lot.quantity_available, lot.quantity_consumed) == (200, 200)
        cancelled = client.put(f'/api/deliveries/{delivery_id}/cancel')
        assert cancelled.status_code == 200, cancelled.text
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_consumed) == (400, 0)


def test_cancelled_source_cannot_be_corrected_from_stale_audit(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    lot_id, delivery_id, events, _ = legacy_shipment(app, factory)
    with TestClient(app) as client:
        _login(client)
        assert client.put(f'/api/deliveries/{delivery_id}/cancel').status_code == 200
    with factory() as db:
        with pytest.raises(WarehouseInventoryError, match='已取消或存在冲回'):
            post_audited_quantity_batch(db, [events[1]], operator_id=1)
        db.commit()
        assert db.get(InventoryLot, lot_id).quantity_available == 200


@pytest.mark.parametrize('change', [dict(quantity=True), dict(quantity=2147483648),
    dict(expected_version=0), dict(before={'available': 1})])
def test_invalid_event_never_changes_stock(unordered_finished_delivery_app, change):
    app, factory = unordered_finished_delivery_app
    lot_id, _, events, _ = legacy_shipment(app, factory)
    invalid = dict(events[0], **change)
    with factory() as db:
        with pytest.raises(WarehouseInventoryError, match='格式无效'):
            post_audited_quantity_batch(db, [invalid], operator_id=1)
        db.commit()
        assert _balances(db.get(InventoryLot, lot_id)) == events[0]['before']


def test_caller_rollback_after_success_undoes_entire_batch(unordered_finished_delivery_app):
    app, factory = unordered_finished_delivery_app
    lot_id, _, events, _ = legacy_shipment(app, factory)
    with factory() as db:
        post_audited_quantity_batch(db, events, operator_id=1)
        db.rollback()  # A later cost or audit failure must still undo stock changes.
    with factory() as db:
        assert _balances(db.get(InventoryLot, lot_id)) == events[0]['before']
        assert not db.scalar(select(InventoryMovement.id).where(
            InventoryMovement.idempotency_key.startswith(PREFIX)))
