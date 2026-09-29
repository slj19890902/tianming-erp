import json
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app.core.receipt_price_guard import ReceiptPriceGuardSession
from app.core import inventory_entry_guard  # register production commit listeners
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.services.warehouse_inventory import manual_finished_in
from tests.test_inventory_cost_snapshot import db
from tests.test_bom_entry_cost import seed


@pytest.mark.parametrize('damage', [None, 'missing_leaf', 'wrong_product', 'wrong_quantity', 'zero_dimension', 'missing_child', 'zero_cost'])
def test_real_commit_checks_every_frozen_assembly_leaf_and_rolls_back(db, damage):
    parent, children, material, location = seed(db)
    args = dict(customer_id=parent.customer_id, product_id=parent.id, location_id=location.id,
                quantity=165, stock_date=date(2026, 9, 29), source_type='stocktake',
                remarks=None, operator_id=None, idempotency_key='assembled-guard')
    db.commit()
    with ReceiptPriceGuardSession(bind=db.get_bind()) as session:
        lot = manual_finished_in(session, **args)
        evidence = json.loads(lot.cost_snapshot_detail_json)
        assert not json.loads(lot.finished_detail.physical_basis_json)['spec']
        if damage == 'missing_leaf':
            evidence['components'][1]['evidence']['components'][0]['evidence']['components'] = []
        elif damage == 'wrong_product':
            evidence['components'][1]['evidence']['product_id'] = -1
        elif damage == 'wrong_quantity':
            evidence['components'][1]['quantity_per_set'] = '999'
        elif damage == 'zero_dimension':
            evidence['components'][1]['evidence']['components'][0]['evidence']['components'][0]['width_mm'] = '0'
        elif damage == 'missing_child':
            evidence['components'].pop()
        elif damage == 'zero_cost':
            lot.estimated_unit_cost_snapshot = 0
        lot.cost_snapshot_detail_json = json.dumps(evidence)
        if damage:
            with pytest.raises(HTTPException) as error:
                session.commit()
            assert error.value.status_code == 422
            assert error.value.detail['code'] == 'INVENTORY_ENTRY_INCOMPLETE'
            session.rollback()
            assert session.scalar(select(func.count()).select_from(InventoryLot)) == 0
            assert session.scalar(select(func.count()).select_from(InventoryMovement)) == 0
        else:
            session.commit()
            lid, frozen = lot.id, lot.cost_snapshot_detail_json
            replay = manual_finished_in(session, **args)
            session.commit()
            assert replay.id == lid and replay.quantity_available == 165
            assert replay.cost_snapshot_detail_json == frozen
            assert session.scalar(select(func.count()).select_from(InventoryMovement)) == 1
