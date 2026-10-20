"""Old assembly facts remain movable without pretending to be new production."""
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app.core.receipt_price_guard import ReceiptPriceGuardSession
from app.models.warehouse_inventory import InventoryLot, InventoryLotTransfer, InventoryMovement
from test_fixed_shelf import setup, rack_factory, incoming, move


@pytest.mark.parametrize('source_ref', ['preparation_assembly', 'bom_assembly', 'stock_preparation_finish', 'inventory_onboarding_line'])
def test_legacy_split_move_preserves_cost_time_and_replays(setup, source_ref):
    factory, pid, cid, locations = setup
    with factory() as db:
        lot = incoming(db, pid, cid, locations[0], qty=125)
        lot.source_type = 'transfer'
        lot.source_ref_type = source_ref
        lot.source_ref_id = lot.id
        lot.cost_snapshot_at = None  # Older committed inventory, not a new entry.
        db.commit()
        lid = lot.id
    factory.class_ = ReceiptPriceGuardSession
    with factory() as db:
        lot = db.get(InventoryLot, lid)
        before = (lot.cost_snapshot_detail_json, lot.estimated_unit_cost_snapshot)
        moved = move(db, lot, locations[1], 50)
        db.commit()
        target = moved.target_lot
        assert (lot.quantity_available, target.quantity_available) == (75, 50)
        assert target.cost_snapshot_at is None and lot.cost_snapshot_at is None
        assert (target.cost_snapshot_detail_json, target.estimated_unit_cost_snapshot) == before
        assert (target.source_ref_type, target.source_ref_id) == (
            'inventory_onboarding_transfer' if source_ref == 'inventory_onboarding_line' else source_ref, lid)
        counts = tuple(db.scalar(select(func.count()).select_from(model)) for model in (InventoryLot, InventoryLotTransfer, InventoryMovement))
        # Replay the original request despite the now advanced source version.
        from app.services.warehouse_inventory import transfer_finished_lot_between_locations
        repeated = transfer_finished_lot_between_locations(db, lot_id=lid, expected_version=moved.transfer.source_version_before,
            quantity=50, location_id=locations[1], operator_id=1, idempotency_key='move-1',
            expected_source_location_id=locations[0], expected_source_address_version=1,
            expected_source_layout_version=1, expected_target_address_version=1, expected_target_layout_version=1)
        db.commit()
        assert repeated.target_lot.id == target.id
        assert tuple(db.scalar(select(func.count()).select_from(model)) for model in (InventoryLot, InventoryLotTransfer, InventoryMovement)) == counts


@pytest.mark.parametrize('source_ref', ['preparation_assembly', 'inventory_onboarding_line'])
@pytest.mark.parametrize('tamper', ['timestamp', 'cost', 'movement', 'transfer'])
def test_transfer_exemption_requires_unchanged_source_and_real_moves(setup, tamper, source_ref):
    factory, pid, cid, locations = setup
    with factory() as db:
        lot = incoming(db, pid, cid, locations[0], qty=125)
        lot.source_type = 'transfer'; lot.source_ref_type = source_ref; lot.source_ref_id = lot.id
        lot.cost_snapshot_at = None
        db.commit(); lid = lot.id
    factory.class_ = ReceiptPriceGuardSession
    with factory() as db:
        moved = move(db, db.get(InventoryLot, lid), locations[1], 50)
        if tamper == 'timestamp':
            from datetime import datetime
            moved.target_lot.cost_snapshot_at = datetime(2026,10,20)
            moved.target_lot.estimated_unit_cost_snapshot = None
        elif tamper == 'cost':
            moved.target_lot.estimated_unit_cost_snapshot = 999
        elif tamper == 'movement':
            row = db.scalar(select(InventoryMovement).where(InventoryMovement.inventory_lot_id == moved.target_lot.id))
            db.delete(row)
        else:
            db.delete(moved.transfer)
        with pytest.raises(HTTPException):
            db.commit()
        db.rollback()
        assert db.get(InventoryLot, lid).quantity_available == 125
        assert db.scalar(select(func.count()).select_from(InventoryLotTransfer)) == 0


def test_new_assembly_still_requires_frozen_timestamp(setup):
    factory, pid, cid, locations = setup
    factory.class_ = ReceiptPriceGuardSession
    with factory() as db:
        lot = incoming(db, pid, cid, locations[0], qty=125)
        lot.source_type='transfer'; lot.source_ref_type='preparation_assembly'
        lot.cost_snapshot_at=None
        with pytest.raises(HTTPException) as error:
            db.commit()
        assert '成本冻结时间' in error.value.detail['missing']
        db.rollback()
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0
