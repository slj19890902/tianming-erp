from sqlalchemy import select
from app.models.order import Order
from app.services.order_pdf_import import mark_order_duplicate
from tests.test_p1_76_mobile_portal import mobile_portal_app, mobile_erp_app

def test_cancelled_order_is_not_deleted_and_still_blocks(mobile_portal_app):
    _, _, factory = mobile_portal_app
    with factory() as db:
        order = db.scalar(select(Order).where(Order.customer_po == 'PO-MOBILE-001'))
        draft = {'matched_customer_id': order.customer_id, 'customer_po': order.customer_po, 'items': []}
        assert mark_order_duplicate(db, draft)['duplicate_status'] == 'duplicate_skipped'
        order.status = 'cancelled'
        db.flush()
        result = mark_order_duplicate(db, draft)
        assert result['duplicate_status'] == 'duplicate_skipped'
        assert '已取消，但未删除' in result['duplicate_reason']
        assert '受控删除' in result['duplicate_reason']
        db.delete(order)
        db.flush()
        assert not mark_order_duplicate(db, result).get('duplicate_status')
