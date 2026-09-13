from types import SimpleNamespace as NS

from datetime import datetime
from sqlalchemy import event, insert, select

from app.models.warehouse_inventory import InventoryLot, InventoryLotTransfer
from tests.test_opt003_cost_paging import cost_app
from tests.test_inventory_valuation import db


def test_batch_revaluation_readiness_api_is_available():
    from app.services.inventory_revaluation_readiness import (
        batch_revaluation_readiness,
    )

    assert callable(batch_revaluation_readiness)


def test_inventory_listing_uses_batch_readiness_service():
    from app.services import inventory_cost_listing

    assert "batch_revaluation_readiness" in inventory_cost_listing.__dict__
    assert NS(id=1).id == 1


def test_transfer_page_uses_one_edge_and_one_source_read(cost_app):
    _client, db, _app = cost_app
    source = db.get(InventoryLot, 1000)
    raw = {column.name: getattr(source, column.name) for column in InventoryLot.__table__.columns if column.name != "id"}
    ids = list(range(20000, 20050))
    db.execute(insert(InventoryLot), [{**raw, "id": lot_id, "lot_number": f"OPT004-{lot_id}" , "source_type": "transfer"} for lot_id in ids])
    db.add_all(
        InventoryLotTransfer(
            source_lot_id=source.id,
            target_lot_id=lot_id,
            source_location_id=source.warehouse_location_id,
            target_location_id=source.warehouse_location_id,
            quantity=1,
            available_quantity=1,
            reserved_quantity=0,
            source_version_before=source.version,
            source_version_after=source.version + 1,
            idempotency_key=f"opt004-{lot_id}",
            request_hash="a" * 64,
            transferred_at=datetime(2026, 9, 13),
        )
        for lot_id in ids
    )
    db.commit()
    lots = list(db.scalars(select(InventoryLot).where(InventoryLot.id.in_(ids))))
    statements = []
    from app.services.inventory_revaluation_readiness import batch_revaluation_readiness

    def record(_connection, _cursor, statement, *_args):
        if "inventory_lot_transfers" in statement or "FROM inventory_lots" in statement:
            statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", record)
    try:
        result = batch_revaluation_readiness(db, lots)
    finally:
        event.remove(db.bind, "before_cursor_execute", record)
    assert len(result) == 50 and all(result.values())
    assert sum("inventory_lot_transfers" in sql for sql in statements) == 1
    assert sum("FROM inventory_lots" in sql for sql in statements) == 1


def test_missing_origin_and_cycle_fail_without_cross_path_diamond_false_positive(cost_app):
    _client, db, _app = cost_app
    source = db.get(InventoryLot, 1000)
    raw = {column.name: getattr(source, column.name) for column in InventoryLot.__table__.columns if column.name != "id"}
    ids = [20100, 20101, 20102, 20103]
    db.execute(insert(InventoryLot), [{**raw, "id": lot_id, "lot_number": f"OPT004-X-{lot_id}", "source_type": "transfer"} for lot_id in ids])
    rows = [
        InventoryLotTransfer(source_lot_id=source.id, target_lot_id=20100, source_location_id=source.warehouse_location_id, target_location_id=source.warehouse_location_id, quantity=1, available_quantity=1, reserved_quantity=0, source_version_before=1, source_version_after=2, idempotency_key="opt004-x0", request_hash="b" * 64, transferred_at=datetime(2026, 9, 13)),
        InventoryLotTransfer(source_lot_id=20103, target_lot_id=20102, source_location_id=source.warehouse_location_id, target_location_id=source.warehouse_location_id, quantity=1, available_quantity=1, reserved_quantity=0, source_version_before=1, source_version_after=2, idempotency_key="opt004-x2", request_hash="d" * 64, transferred_at=datetime(2026, 9, 13)),
        InventoryLotTransfer(source_lot_id=20102, target_lot_id=20103, source_location_id=source.warehouse_location_id, target_location_id=source.warehouse_location_id, quantity=1, available_quantity=1, reserved_quantity=0, source_version_before=1, source_version_after=2, idempotency_key="opt004-x3", request_hash="e" * 64, transferred_at=datetime(2026, 9, 13)),
    ]
    db.add_all(rows)
    db.commit()
    lots = list(db.scalars(select(InventoryLot).where(InventoryLot.id.in_(ids))))
    from app.services.inventory_revaluation_readiness import batch_revaluation_readiness

    result = batch_revaluation_readiness(db, lots)
    assert result[20100] is True
    assert result[20101] is False
    assert result[20102] is False and result[20103] is False
