from sqlalchemy import select
from app.models.order import Order
from app.services.order_pdf_import import mark_order_duplicate
from tests.test_p1_76_mobile_portal import mobile_portal_app, mobile_erp_app

def test_cancelled_order_is_preserved_and_reported_as_related(mobile_portal_app):
    _, _, factory = mobile_portal_app
    with factory() as db:
        order = db.scalar(select(Order).where(Order.customer_po == 'PO-MOBILE-001'))
        draft = {'matched_customer_id': order.customer_id, 'customer_po': order.customer_po, 'items': []}
        assert mark_order_duplicate(db, draft)['duplicate_status'] == 'existing_po_found'
        order.status = 'cancelled'
        db.flush()
        result = mark_order_duplicate(db, draft)
        assert not result.get('duplicate_status')
        assert result['cancelled_related_orders'] == [
            {'id': order.id, 'order_number': order.order_number, 'status': 'cancelled'}
        ]
        assert db.get(Order, order.id) is order
        db.delete(order)
        db.flush()
        assert not mark_order_duplicate(db, result).get('duplicate_status')
