"""Real receipt/production outputs retain verifiable cost after repeated moves."""
import pytest
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.core.receipt_price_guard import ReceiptPriceGuardSession
from app.models.warehouse_inventory import InventoryLot, InventoryLotTransfer, InventoryMovement, WarehouseLocation
from app.models.stock_preparation import StockPreparationJob
from app.models.incoming_receipt import IncomingReceiptItem
from app.services.inventory_valuation import frozen_cost
from app.services.inventory_cost_listing import read_inventory_cost_page
from app.services.warehouse_inventory import transfer_finished_lot_between_locations
from app.services.warehouse_sheet_transfer import transfer_sheet_lot_between_locations
from test_stock_processing_auto import stock_replenishment_app, configure_finished_location, receive


def relocate(db, lot, target_id, quantity, key):
    target = db.get(WarehouseLocation, target_id)
    function = transfer_finished_lot_between_locations if lot.inventory_type == 'finished' else transfer_sheet_lot_between_locations
    request = dict(lot_id=lot.id, expected_version=lot.version, quantity=quantity,
        location_id=target_id, operator_id=1, idempotency_key=key,
        expected_target_layout_version=target.floor3_layout.version if target.floor3_layout else None)
    result = function(db, **request)
    db.commit()
    replay = function(db, **request)
    db.commit()
    assert replay.replayed and replay.target_lot.id == result.target_lot.id
    return result.target_lot


def assert_cost(db, lot, expected):
    assert frozen_cost(lot, db)[0] == expected
    page = read_inventory_cost_page(db, select(InventoryLot.id).where(InventoryLot.id == lot.id))
    assert page['missing_lots'] == 0
    assert Decimal(page['rows'][0]['unit_cost']) == expected.quantize(Decimal('.0001'))


def test_auto_receipt_cost_survives_partial_chained_and_full_moves(stock_replenishment_app):
    app, factory = stock_replenishment_app
    configure_finished_location(factory)
    with TestClient(app) as client:
        receive(client)
    factory.class_ = ReceiptPriceGuardSession
    with factory() as db:
        job = db.scalar(select(StockPreparationJob))
        original = db.get(InventoryLot, job.output_lot_id)
        expected = frozen_cost(original, db)[0]
        assert expected is not None
        facts = (original.cost_snapshot_detail_json, original.cost_snapshot_at)
        first = relocate(db, original, 8, 10, 'auto-split')
        assert_cost(db, first, expected)
        second = relocate(db, first, 7, 5, 'auto-chain')
        assert_cost(db, second, expected)
        whole = relocate(db, second, 8, 5, 'auto-full')
        assert_cost(db, whole, expected)
        assert (whole.cost_snapshot_detail_json, whole.cost_snapshot_at) == facts
        assert sum(row.quantity_available for row in (original, first, whole)) == 20


@pytest.mark.parametrize('tamper', ['edge', 'movement', 'cost', 'identity', 'ancestor'])
def test_copied_json_without_verified_lineage_never_proves_cost(stock_replenishment_app, tamper):
    app, factory = stock_replenishment_app
    configure_finished_location(factory)
    with TestClient(app) as client:
        receive(client)
    with factory() as db:
        original = db.get(InventoryLot, db.scalar(select(StockPreparationJob)).output_lot_id)
        child = relocate(db, original, 8, 10, 'tamper-split')
        edge = db.scalar(select(InventoryLotTransfer).where(InventoryLotTransfer.target_lot_id == child.id))
        if tamper == 'edge':
            db.delete(edge)
        elif tamper == 'movement':
            row = db.scalar(select(InventoryMovement).where(InventoryMovement.inventory_lot_id == child.id))
            row.after_available += 1
        elif tamper == 'cost':
            child.estimated_unit_cost_snapshot += 1
        elif tamper == 'identity':
            child.finished_detail.product_name_snapshot = "different physical identity"
        else:
            edge.source_lot_id = child.id
        db.flush()
        assert frozen_cost(child, db)[0] is None
        db.rollback()


def test_actual_sheet_receipt_cost_survives_sheet_moves(stock_replenishment_app, monkeypatch):
    from test_incoming_actual_specs import purchase, actual
    from app.services import stock_preparation_processing
    # Isolate the receipt's real raw-sheet writer from optional automatic production.
    monkeypatch.setattr(stock_preparation_processing, 'auto_complete_receipt', lambda *args, **kwargs: None)
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        identity = purchase(client, factory)
        spec, _ = actual(client, identity)
        response = client.put(f'/api/incoming/receive/sr{identity}', json=dict(
            received_quantity=67, actual_spec=spec, idempotency_key='sheet-receipt'))
        assert response.status_code == 200, response.text
    factory.class_ = ReceiptPriceGuardSession
    with factory() as db:
        receipt = db.scalar(select(IncomingReceiptItem))
        original = db.get(InventoryLot, receipt.received_inventory_lot_id)
        assert original.cost_snapshot_source == 'receipt_sheet_actual'
        expected = frozen_cost(original, db)[0]
        assert expected is not None
        first = relocate(db, original, 7, 10, 'sheet-split')
        assert_cost(db, first, expected)
        second = relocate(db, first, 8, 5, 'sheet-chain')
        assert_cost(db, second, expected)
        whole = relocate(db, second, 7, 5, 'sheet-full')
        assert_cost(db, whole, expected)
        assert sum(row.quantity_available + row.quantity_reserved for row in (original, first, whole)) == 67
