"""A reviewed sheet allocation must finish production under its saved facts."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_manual_sheet_deduction_20261013 import (
    composite_requisition_app, _login, seed, preview, payload, BASE,
)
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, SemiFinishedInventoryDetail, OrderItemSemiRequirement
from app.services.semi_finished_inventory import consume_semi_finished_reservation
from app.services.warehouse_inventory import WarehouseInventoryError
from app.services.bom_transactions import atomic_bom


def reviewed_reservation(app, sessions):
    lot_id = seed(sessions, double=True)
    with sessions() as db:
        detail = db.get(SemiFinishedInventoryDetail, lot_id)
        detail.material_code_snapshot = detail.normalized_material_code = 'X616X'
        detail.board_length_mm = 700
        detail.board_width_mm = 1000
        db.commit()
    with TestClient(app) as client:
        _login(client)
        data = client.get(BASE + '/manual-inventory?match_mode=swapped').json()
        response = client.post(BASE + '/manual-reserve', json={**payload(data, 7), 'match_mode':'swapped'})
        assert response.status_code == 200, response.text
        reservation_id = response.json()['reservation_ids'][0]
    return lot_id, reservation_id


def consume(db, lot_id, reservation_id):
    return consume_semi_finished_reservation(db, reservation_id=reservation_id, stock_quantity=7,
        expected_version=db.get(InventoryLot, lot_id).version, operator_id=1,
        idempotency_key='reviewed-production-consume', reason='实际裁切生产耗用')


def test_confirmed_material_variance_can_be_consumed_once(composite_requisition_app):
    app, sessions = composite_requisition_app
    lot_id, reservation_id = reviewed_reservation(app, sessions)
    with sessions() as db, atomic_bom(db):
        first = consume(db, lot_id, reservation_id)
        again = consume(db, lot_id, reservation_id)
        assert first.movement.id == again.movement.id
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (197, 0, 7)
        assert lot.semi_finished_detail.material_code_snapshot == 'X616X'
        assert lot.semi_finished_detail.board_width_mm == 1000


@pytest.mark.parametrize('change', ['no_actor', 'no_warning', 'new_material', 'new_flute', 'new_geometry',
    'foreign_owner', 'new_requirement_material', 'new_requirement_yield', 'new_requirement_pieces',
    'closed_lot', 'object_warnings', 'missing_contract'])
def test_saved_review_does_not_authorize_different_or_unreviewed_stock(composite_requisition_app, change):
    app, sessions = composite_requisition_app
    lot_id, reservation_id = reviewed_reservation(app, sessions)
    with sessions() as db, atomic_bom(db):
        reservation = db.get(InventoryReservation, reservation_id)
        detail = db.get(SemiFinishedInventoryDetail, lot_id)
        requirement = db.get(OrderItemSemiRequirement, reservation.semi_requirement_id)
        if change == 'no_actor': reservation.warning_acknowledged_by = None
        if change == 'no_warning': reservation.warning_codes = '[]'
        if change == 'new_material': detail.material_code_snapshot = detail.normalized_material_code = 'A999A'
        if change == 'new_flute': detail.flute_type = 'BE'
        if change == 'new_geometry': detail.board_width_mm = 730
        if change == 'foreign_owner':
            from app.models.customer import Customer
            from decimal import Decimal
            db.add(Customer(customer_number=2,customer_code='FOREIGN',name='另一客户',payment_term_days=30,credit_limit=Decimal(0)))
            db.flush(); detail.owner_customer_id = 2
        if change == 'new_requirement_material': requirement.normalized_material_code = requirement.material_code_snapshot = 'A999A'
        if change == 'new_requirement_yield': requirement.stock_yield_per_sheet = 2
        if change == 'new_requirement_pieces': requirement.pieces_per_box = 1
        if change == 'closed_lot': db.get(InventoryLot,lot_id).status = 'closed'
        if change == 'object_warnings': reservation.warning_codes = '{"MANUAL_DEDUCTION_CONFIRM_REQUIRED":false,"SEMI_SIGNATURE_OVERRIDE":false}'
        if change == 'missing_contract':
            plan = json.loads(reservation.cut_plan_json); plan.pop('review_contract',None)
            reservation.cut_plan_json = json.dumps(plan)
        db.flush()
        with pytest.raises(WarehouseInventoryError):
            consume(db, lot_id, reservation_id)
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (197, 7, 0)


def test_consumption_failure_rolls_back_production_input(composite_requisition_app):
    app, sessions = composite_requisition_app
    lot_id, reservation_id = reviewed_reservation(app, sessions)
    with sessions() as db:
        with pytest.raises(RuntimeError, match='later output failure'):
            with atomic_bom(db):
                consume(db, lot_id, reservation_id)
                raise RuntimeError('later output failure')
        db.rollback()
    with sessions() as db:
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (197, 7, 0)
        assert db.get(InventoryReservation, reservation_id).consumed_stock_quantity == 0
