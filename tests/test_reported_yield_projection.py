from datetime import datetime
from sqlalchemy import select
from test_p1_06_reported_documents_pagination import reported_documents_app
from test_p1_36o_reported_documents_current_page import _app_session


def test_reported_remaining_uses_frozen_purchase_yield_and_actual_receipt_yield(reported_documents_app, monkeypatch):
    from app.api.requisition import _build_reported_document_candidates
    from app.models.user import User
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.services import incoming_actual_specs
    with _app_session(reported_documents_app) as db:
        item = db.scalar(select(StockReplenishmentOrderItem).order_by(StockReplenishmentOrderItem.id))
        item.quantity = 10; item.stocked_quantity = 3; item.stock_yield_per_sheet = 4
        receipt = IncomingReceipt(receipt_number='YIELD-PROJECTION', received_at=datetime(2026,10,20), idempotency_key='yield-projection')
        db.add(receipt); db.flush()
        fact = IncomingReceiptItem(receipt_id=receipt.id, stock_replenishment_item_id=item.id,
            planned_quantity=10, received_quantity=3, cumulative_received_quantity=3,
            variance_quantity=-7, variance_type='short', resolution_status='pending', status='posted')
        db.add(fact); db.flush()
        original = incoming_actual_specs.stored
        monkeypatch.setattr(incoming_actual_specs, 'stored', lambda session, rid: {'yield':2} if rid==fact.id else original(session,rid))
        admin = db.scalar(select(User).where(User.role=='admin'))
        documents = _build_reported_document_candidates(db,admin)
        line = next(line for doc in documents if doc['source_type']=='stock_replenishment'
            for line in doc['line_items'] if line['_item_id']==item.id)
        assert line['_stocked_quantity'] == 3
        assert line['_remaining_quantity'] == 17  # (10 * 4 - 3 * 2) / 2
