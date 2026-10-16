"""Owner-requested category correction for YG-20261015-001 / 80012130 only.

Caller must verify a current backup and commit the transaction. No inventory,
price, quantity, upstream order or superseded delivery revision is modified.
"""
from fastapi import HTTPException
from sqlalchemy import select, update

from app.models.delivery import Delivery, DeliveryItem
from app.models.order import OrderItem
from app.models.product import Product
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom
from app.services.customer_document_fields import decode_snapshot, encode_snapshot, supplement_delivery_category

ACTION = 'delivery_category_correction_20261016'
KEY = 'YG-20261015-001:1101:customer_category:BT:20261016'


def execute(db, *, actor):
    from app.api.deps import require_customer_access
    from app.api.deliveries import (_delivery_request_hash, _delivery_idempotency_replay,
                                     _record_delivery_idempotency)
    if actor is None or not actor.is_active or actor.role != 'admin':
        raise HTTPException(403, '仅活动管理员可执行本次送货类别更正')
    require_customer_access(137, actor, db)
    payload = {'delivery_id': 155, 'delivery_number': 'YG-20261015-001',
               'delivery_item_id': 1101, 'product_id': 3564, 'product_code': '80012130',
               'field': 'customer_category', 'before': None, 'after': 'BT',
               'expected_delivery_version': 2, 'expected_product_version': 8}
    request_hash = _delivery_request_hash(ACTION, payload)
    with atomic_bom(db):
        replay, _ = _delivery_idempotency_replay(db, idempotency_key=KEY,
                            request_hash=request_hash, action=ACTION, actor=actor)
        if replay is not None:
            return replay
        delivery = db.get(Delivery, 155)
        line = db.get(DeliveryItem, 1101)
        product = db.get(Product, 3564)
        order = db.get(OrderItem, 10610)
        if (delivery is None or delivery.delivery_number != payload['delivery_number']
                or delivery.customer_id != 137 or delivery.status != 'pending'
                or delivery.version != 2 or delivery.printed_at or delivery.ever_dispatched_at
                or delivery.total_quantity != 8550
                or line is None or line.delivery_id != 155 or not line.is_current
                or line.order_item_id != 10610 or line.product_code_snapshot != '80012130'
                or line.delivered_quantity != 30
                or order is None or order.product_id != 3564
                or product is None or product.customer_id != 137
                or product.product_code != '80012130' or product.version != 8
                or product.customer_category != 'BT' or not product.is_active or product.deleted_at):
            raise HTTPException(409, '本次更正的单据、明细或常用箱依据已变化，请重新核对')
        before_text = line.customer_document_snapshot_json
        before = decode_snapshot(before_text)
        if (not before or before.get('product_id') != 3564
                or before.get('customer_category') is not None
                or before.get('customer_drawing_number') != '7942511'
                or 'customer_category' not in before.get('conflicts', [])):
            raise HTTPException(409, '本次更正的类别空值或冲突依据已变化')
        after = supplement_delivery_category(before, product)
        after['category_source']['basis'] = 'owner_authorized_correction_20261016'
        claimed = db.execute(update(Delivery).where(Delivery.id == 155,
            Delivery.version == 2, Delivery.status == 'pending',
            Delivery.printed_at.is_(None), Delivery.ever_dispatched_at.is_(None)
        ).values(version=3))
        changed = db.execute(update(DeliveryItem).where(DeliveryItem.id == 1101,
            DeliveryItem.is_current.is_(True),
            DeliveryItem.customer_document_snapshot_json == before_text
        ).values(customer_document_snapshot_json=encode_snapshot(after)))
        if claimed.rowcount != 1 or changed.rowcount != 1:
            raise HTTPException(409, '单据并发变化，更正已停止')
        result = {**payload, 'delivery_version': 3}
        append_audit_event(db, actor=actor, event_category='business', result='success',
            source='script', module_code='delivery', action_code=ACTION,
            resource='Delivery', entity_type='delivery', entity_id=155,
            object_ref='YG-20261015-001', customer_id=137,
            description='按老板要求补齐80012130客户类别BT，保留订单及其他冻结事实',
            details={**result, 'before_snapshot': before, 'after_snapshot': after})
        _record_delivery_idempotency(db, idempotency_key=KEY, request_hash=request_hash,
                                    action=ACTION, actor=actor, delivery_id=155, response=result)
        db.flush()
        return result
