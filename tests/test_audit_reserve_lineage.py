"""A-0002: real receipt-derived reserve lots, synthetic isolated database only."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from tests.test_phase11_requisition import requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import (
    _p181_published_map_identity, _seed_material_and_staging,
    _create_frozen_sources, _freeze_receipt_fact, _receive,
    _posted_finished_quantity,
)
from app.models.order import Order, OrderItem
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.semi_finished_inventory import (
    save_order_item_semi_requirement, reserve_semi_finished_inventory,
    consume_semi_finished_reservation, SemiFinishedLotVersion,
)
from app.services.warehouse_inventory import WarehouseInventoryError


def test_receipt_derived_reserve_keeps_identity_through_later_order_consumption(requisition_app):
    app, factory = requisition_app
    _seed_material_and_staging(factory)
    with TestClient(app) as client:
        _login(client, 'admin')
        source = _create_frozen_sources(client, factory, order_quantity=500,
            purchase_total=600, order_purpose=500, stock_purpose=100)[0]
        frozen = _freeze_receipt_fact(client, source, idempotency_key='a0002-price')
        assert frozen.status_code == 200, frozen.text
        for index in (1, 2):
            response = _receive(client, source, frozen.json(), quantity=300,
                idempotency_key=f'a0002-receive-{index}',
                overrides={'surplus_disposition':'semi_finished_reserve'} if index == 2 else None)
            assert response.status_code == 200, response.text
        receipt_id = response.json()['receipt_item_id']

        with factory() as db:
            lot = db.scalar(select(InventoryLot).where(InventoryLot.inventory_type == 'semi_finished'))
            assert lot.quantity_available == 100
            lot_id, version = lot.id, lot.version
            source_identity = (lot.source_ref_type, lot.source_ref_id)
            # Seed only the later demand, never seed or edit the reserve stock.
            original = db.get(OrderItem, 1)
            order = Order(order_number='A0002-LATER', customer_id=1, order_date=date(2026,9,19),
                          status='pending_production', payment_status='unpaid', total_amount=Decimal('100'))
            db.add(order); db.flush()
            columns = {c.name:getattr(original,c.name) for c in OrderItem.__table__.columns
                       if c.name.startswith('snapshot_')}
            item = OrderItem(order_id=order.id, product_id=original.product_id,
                            quantity=100, unit_price=1, subtotal=100, material_status='pending', **columns)
            db.add(item); db.flush()
            detail = lot.semi_finished_detail
            requirement = save_order_item_semi_requirement(db, order_item_id=item.id,
                component_type='whole', board_length_mm=detail.board_length_mm,
                board_width_mm=detail.board_width_mm, material_code=original.snapshot_material,
                flute_type=detail.flute_type, pieces_per_box=1, stock_yield_per_sheet=1,
                required_piece_quantity=100, operator_id=1)
            requirement_id = requirement.id
            db.commit()

        kwargs = dict(requirement_id=requirement_id, requested_requirement_quantity=60,
            lots=[SemiFinishedLotVersion(lot_id=lot_id, expected_version=version)],
            operator_id=1, idempotency_key='a0002-reserve-later', confirmed=True)
        with factory() as db:
            with pytest.raises(WarehouseInventoryError, match='CUSTOMER_GENERIC_SEMI_FINISHED_STOCK'):
                reserve_semi_finished_inventory(db, **kwargs)
            db.rollback()
            lot = db.get(InventoryLot,lot_id)
            assert (lot.quantity_available,lot.quantity_reserved)==(100,0)
        kwargs['warning_acknowledged_codes']=['CUSTOMER_GENERIC_SEMI_FINISHED_STOCK']
        with factory() as db:
            reserve_semi_finished_inventory(db, **kwargs); db.commit()
        with factory() as db:
            with pytest.raises(WarehouseInventoryError, match='库存已被其他人修改'):
                reserve_semi_finished_inventory(db, **{**kwargs,'idempotency_key':'a0002-stale-version'})
            db.rollback()
        with factory() as db:
            reserve_semi_finished_inventory(db, **kwargs); db.commit()
            lot = db.get(InventoryLot, lot_id)
            assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(40,60,0)
            reservation = db.scalar(select(InventoryReservation).where(InventoryReservation.inventory_lot_id==lot_id))
            reservation_id, consume_version = reservation.id, lot.version
            assert db.scalar(select(func.count()).select_from(InventoryReservation).where(
                InventoryReservation.inventory_lot_id==lot_id))==1

        consume = dict(reservation_id=reservation_id, stock_quantity=60, expected_version=consume_version,
                       operator_id=1, idempotency_key='a0002-consume-later', reason='隔离核验后单耗源')
        for _ in range(2):
            with factory() as db:
                consume_semi_finished_reservation(db, **consume); db.commit()
        with factory() as db:
            lot = db.get(InventoryLot, lot_id)
            assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(40,0,60)
            assert (lot.source_ref_type,lot.source_ref_id)==source_identity
            assert db.scalar(select(func.count()).select_from(InventoryLot).where(
                InventoryLot.inventory_type=='semi_finished'))==1
            from app.services.stock_preparation import source as stock_source
            source_receipt, source_item, source_lot = stock_source(db,receipt_id)
            assert source_receipt.id == receipt_id
            assert source_item.product_id == original.product_id
            assert source_lot.id == lot_id
        blocked = client.put(f'/api/incoming/receipt-items/{receipt_id}/revert',json={})
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()['detail']['code']=='RESERVE_INVENTORY_ALREADY_USED'
    assert _posted_finished_quantity(factory)==500
    with factory() as db:
        lot = db.get(InventoryLot,lot_id)
        assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(40,0,60)
