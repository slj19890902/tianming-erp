"""Exercise the existing public reversal/re-entry chain on disposable stock.

No production identities, source database, or direct business-row repair is
used. This is a feasibility gate before operating a real historical batch.
"""
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_phase11_requisition import requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import (
    _seed_material_and_staging, _create_frozen_sources, _freeze_receipt_fact,
    _receive, _p181_published_map_identity,
    FrozenSource, _add_changed_material,
)
from tests.test_p1_80_purchase_purpose_allocation import (
    _preview_supplier_order_draft, _selection, _created_order_id,
)
from tests.test_delivery_dispatch_commands import command


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest.mark.parametrize('correct_report', [False, True])
def test_uninvoiced_same_order_can_reverse_and_rereceive_supplier_overage(
    requisition_app, _p181_published_map_identity, correct_report,
):
    from app.api.orders import router as orders
    from app.api.deliveries import router as deliveries
    from app.api.finance import router as finance
    from app.models.finance import Statement, StatementAdjustment
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder, SupplierRequisitionOrderItem,
        PurchasePurposeSourceSnapshot,
    )
    app, factory = requisition_app
    app.include_router(deliveries, prefix='/api/deliveries')
    app.include_router(finance, prefix='/api/finance')
    app.include_router(orders, prefix='/api/orders')
    _seed_material_and_staging(factory)
    changed_material_id = _add_changed_material(factory)
    with TestClient(app) as c:
        _login(c, 'admin')
        source = _create_frozen_sources(c, factory, order_quantity=200,
            purchase_total=200, order_purpose=200, stock_purpose=0)[0]
        price = ok(_freeze_receipt_fact(c, source,
            idempotency_key='history-price-original', unit_price='1.7512'))
        received = ok(_receive(c, source, price, quantity=200,
            idempotency_key='history-receipt-original'))
        delivery = ok(c.post('/api/deliveries', json=dict(customer_id=1,
            historical_backfill=True, idempotency_key='history-original-delivery',
            delivery_date='2026-09-10', items=[dict(order_item_id=1,
            delivered_quantity=200)])), 201)
        did = delivery['id']
        ok(c.put(f'/api/deliveries/{did}/dispatch', json=command(c, did)))
        delivery = ok(c.get(f'/api/deliveries/{did}'))
        receipt = ok(c.post('/api/finance/return_receipts', json=dict(
            delivery_id=did, actual_received_date='2026-09-10',
            reconciliation_month='2026-09', items=[dict(
                delivery_item_id=delivery['items'][0]['id'],
                actual_received_quantity=200)])), 201)
        statement = ok(c.post('/api/finance/statements', json=dict(
            customer_id=1, statement_month='2026-09', delivery_ids=[did])), 201)
        sid, rid = statement['id'], receipt['id']
        if correct_report:
            # Legacy delivered batches predate the cutting snapshot contract.
            # Reversal must preserve that absence; re-entry gets a new report
            # snapshot through ordinary reporting rather than fabricating one.
            with factory() as db:
                db.get(OrderItem, 1).sheet_cutting_settings_snapshot = None
                db.get(SupplierRequisitionOrderItem, source.supplier_item_id).sheet_cutting_snapshot = None
                db.commit()
        # The ordinary guards must remain closed until the downstream chain
        # has been explicitly reversed through its existing audited commands.
        assert _receive(c, source, price, quantity=10,
            idempotency_key='history-no-closed-append').status_code == 409
        assert c.post(f'/api/finance/return_receipts/{rid}/cancel').status_code == 409
        ok(c.post(f'/api/finance/statements/{sid}/cancel'))
        ok(c.post(f'/api/finance/return_receipts/{rid}/cancel'))
        ok(c.put(f'/api/deliveries/{did}/cancel'))
        ok(c.put(f"/api/incoming/receipt-items/{received['receipt_item_id']}/revert",
            json=dict(reason='核准历史供应商多送10片，原事实保留')))
        if correct_report:
            with factory() as db:
                old_line = db.get(SupplierRequisitionOrderItem, source.supplier_item_id)
                old_report_id = old_line.supplier_order_id
                old_width = old_line.report_width_mm
                item = db.get(OrderItem, 1)
                edit = dict(quantity=200, unit_price='2.9988',
                    product_code=item.snapshot_product_code or db.get(Product, item.product_id).product_code,
                    product_name=item.snapshot_product_name, material='ABABA',
                    material_id=changed_material_id, layer_count=5, flute_type='AB',
                    snapshot_report_length_mm=1500, snapshot_report_width_mm=435,
                    snapshot_crease_type='压线', snapshot_crease_left_mm=160,
                    snapshot_crease_middle_mm=115, snapshot_crease_right_mm=160)
            ok(c.put(f'/api/requisition/supplier-orders/{old_report_id}/void'))
            ok(c.put('/api/orders/items/1', json=edit))
            draft = _preview_supplier_order_draft(c, [
                _selection(report_length_mm=1500, report_width_mm=435)])
            saved = c.post('/api/requisition/supplier-orders/from-pending-selection', json=draft)
            ok(saved, 201)
            with factory() as db:
                line = db.scalar(select(SupplierRequisitionOrderItem).where(
                    SupplierRequisitionOrderItem.supplier_order_id == _created_order_id(saved)))
                snap = db.scalar(select(PurchasePurposeSourceSnapshot).where(
                    PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id == line.id))
                source = FrozenSource(source_key=snap.source_key, route_key=f'so{line.id}',
                    supplier_item_id=line.id, source_version=line.version,
                    purpose_snapshot_id=snap.id, purpose_snapshot_version=snap.snapshot_version,
                    receipt_plan_fingerprint=snap.preview_fingerprint,
                    component_type=snap.component_type, material_id=line.material_id,
                    order_purpose_sheet_qty=int(snap.order_purpose_sheet_qty),
                    reserve_purpose_sheet_qty=int(snap.reserve_purpose_sheet_qty))
                assert line.material_id == changed_material_id
                assert line.report_width_mm == 435
                assert db.get(SupplierRequisitionOrder, old_report_id).status == 'voided'
                assert db.get(SupplierRequisitionOrderItem, old_line.id).report_width_mm == old_width
                assert db.get(SupplierRequisitionOrderItem, old_line.id).sheet_cutting_snapshot is None
            # Explicitly retain the previously known per-sheet contract while
            # preserving the voided report and reversed original receipt.
            price = ok(_freeze_receipt_fact(c, source,
                idempotency_key='history-corrected-report-original-price', unit_price='1.7512'))
        corrected = ok(_receive(c, source, price, quantity=210,
            idempotency_key='history-receipt-corrected',
            overrides=dict(surplus_disposition='finished')))
        current = ok(c.get(f'/api/deliveries/{did}'))
        ok(c.put(f'/api/deliveries/{did}', json=dict(
            expected_version=current['version'], idempotency_key='history-delivery-corrected',
            items=[dict(order_item_id=1, delivered_quantity=210,
                over_delivery_confirmed=True, over_delivery_reason='供应商多送10片，客户实际签收210只')])))
        current = ok(c.get(f'/api/deliveries/{did}'))
        ok(c.put(f'/api/deliveries/{did}/actual-date', json=dict(
            actual_delivery_date='2026-09-09', expected_version=current['version'],
            idempotency_key='history-corrected-actual-date')))
        ok(c.put(f'/api/deliveries/{did}/dispatch',
            json=command(c, did, 'history-corrected-dispatch')))
        current = ok(c.get(f'/api/deliveries/{did}'))
        assert current['delivery_date'] == '2026-09-09'
        rereceipt = ok(c.post('/api/finance/return_receipts', json=dict(
            delivery_id=did, actual_received_date='2026-09-09',
            reconciliation_month='2026-09', items=[dict(
                delivery_item_id=current['items'][0]['id'],
                actual_received_quantity=210)])), 201)
        replacement = ok(c.post('/api/finance/statements', json=dict(
            customer_id=1, statement_month='2026-09', delivery_ids=[did])), 201)
        with factory() as db:
            assert db.get(Statement, sid).confirmation_status == 'cancelled'
            assert db.scalar(select(StatementAdjustment).where(
                StatementAdjustment.statement_id == sid,
                StatementAdjustment.action == 'cancel_statement')) is not None
            original = db.get(IncomingReceiptItem, received['receipt_item_id'])
            assert original.received_quantity == 200 and original.status == 'reversed'
            assert db.get(IncomingReceiptItem, corrected['receipt_item_id']).received_quantity == 210
            assert db.get(OrderItem, 1).delivered_quantity == 210
        assert replacement['id'] != sid and rereceipt['items'][0]['actual_received_quantity'] == 210
        assert Decimal(corrected['purpose_allocation']['receipt_total_cost']) == Decimal('367.752')
        if correct_report:
            assert Decimal(replacement['total_receivable']) == Decimal('629.748')
