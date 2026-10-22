"""Physical fulfillment dates/quantities; never a new financial source.

Original delivery, receipt, statement and invoice identity remain authoritative
for accounting and original documents. Only the latest complete amendment is
used for physical queries. No caller may add original quantities a second time.
"""
from datetime import date
from sqlalchemy import String, case, cast, exists, func, literal, select, union_all

from app.models.delivery import Delivery, DeliveryItem
from app.models.delivery_fulfillment_fact import DeliveryFulfillmentRevision, DeliveryFulfillmentSegment


def effective_fulfillment_lines():
    """A stable SQL relation, one row per effective actual item/date segment."""
    revision = DeliveryFulfillmentRevision
    segment = DeliveryFulfillmentSegment
    latest = select(revision.delivery_id.label('delivery_id'),
                    func.max(revision.revision).label('revision')).group_by(revision.delivery_id).subquery()
    segment_count = select(segment.revision_id.label('revision_id'),
        segment.delivery_item_id.label('delivery_item_id'),func.count().label('count')).group_by(
        segment.revision_id,segment.delivery_item_id).subquery()
    amended = select(
        (literal('fulfillment:')+cast(segment.id,String)).label('segment_identity'),
        Delivery.id.label('delivery_id'), DeliveryItem.id.label('delivery_item_id'),
        DeliveryItem.order_item_id.label('order_item_id'), Delivery.customer_id.label('customer_id'),
        segment.actual_date.label('actual_date'), segment.quantity.label('quantity'),
        revision.id.label('revision_id'), revision.revision.label('revision'),
        func.coalesce(func.json_extract(DeliveryItem.quantity_contract_json, '$.physical_unit'),
            func.json_extract(revision.evidence_json, '$.physical_unit')).label('confirmed_physical_unit'),
        Delivery.delivery_date.label('original_document_date'),
        case((segment_count.c.count > 1,literal('pending_split_source_allocation')),
             else_=literal('whole_line_cost_preserved')).label('cost_allocation_status'),
    ).select_from(segment).join(revision,revision.id==segment.revision_id).join(latest,
        (latest.c.delivery_id==revision.delivery_id)&(latest.c.revision==revision.revision)).join(
        Delivery,Delivery.id==revision.delivery_id).join(DeliveryItem,
        (DeliveryItem.id==segment.delivery_item_id)&(DeliveryItem.delivery_id==Delivery.id)).join(
        segment_count,(segment_count.c.revision_id==revision.id)&(segment_count.c.delivery_item_id==DeliveryItem.id)
    ).where(DeliveryItem.is_current.is_(True))
    original = select(
        (literal('delivery:')+cast(DeliveryItem.id,String)).label('segment_identity'),
        Delivery.id.label('delivery_id'),DeliveryItem.id.label('delivery_item_id'),
        DeliveryItem.order_item_id.label('order_item_id'),Delivery.customer_id.label('customer_id'),
        Delivery.delivery_date.label('actual_date'),DeliveryItem.delivered_quantity.label('quantity'),
        literal(None).label('revision_id'),literal(0).label('revision'),
        literal(None).label('confirmed_physical_unit'),
        Delivery.delivery_date.label('original_document_date'),literal('original').label('cost_allocation_status'),
    ).select_from(DeliveryItem).join(Delivery,Delivery.id==DeliveryItem.delivery_id).where(
        DeliveryItem.is_current.is_(True),~exists(select(revision.id).where(revision.delivery_id==Delivery.id)))
    return union_all(original,amended).subquery('effective_fulfillment')


def actual_fulfillment_rows(db,*,delivery_ids=None,item_ids=None,date_from=None,date_to=None,
                            visible_customer_ids=None,corrected_only=False,dispatched_only=True):
    relation=effective_fulfillment_lines()
    query=select(relation).join(Delivery,Delivery.id==relation.c.delivery_id)
    if dispatched_only:query=query.where(Delivery.status=='dispatched')
    if delivery_ids is not None:query=query.where(relation.c.delivery_id.in_(delivery_ids))
    if item_ids is not None:query=query.where(relation.c.delivery_item_id.in_(item_ids))
    if date_from is not None:query=query.where(relation.c.actual_date>=date_from)
    if date_to is not None:query=query.where(relation.c.actual_date<=date_to)
    if visible_customer_ids is not None:query=query.where(relation.c.customer_id.in_(visible_customer_ids))
    if corrected_only:query=query.where(relation.c.revision_id.is_not(None))
    with db.no_autoflush:
        return [dict(row) for row in db.execute(query.order_by(relation.c.actual_date,
                relation.c.delivery_id,relation.c.delivery_item_id,relation.c.segment_identity)).mappings()]


def fulfillment_by_delivery(db,delivery_ids,*,date_from=None,date_to=None,matched_item_ids=None):
    """Separate physical fields; original display_quantity/receipt stay intact."""
    rows=actual_fulfillment_rows(db,delivery_ids=delivery_ids,dispatched_only=False)
    result={}
    for row in rows:
        value=result.setdefault(row['delivery_id'],dict(
            basis='actual_fulfillment',original_document_date=row['original_document_date'],
            revision_id=row['revision_id'],revision=row['revision'],
            segments=[],total_physical_quantity=0,matched_physical_quantity=0,
            signed_receipt_proven_by_segments=False,financial_source='original_return_receipt_only',
            original_document_amount_is_period_amount=False))
        value['segments'].append(row)
        value['total_physical_quantity']+=row['quantity']
        if ((date_from is None or row['actual_date']>=date_from)
                and(date_to is None or row['actual_date']<=date_to)
                and(matched_item_ids is None or row['delivery_item_id'] in matched_item_ids)):
            value['matched_physical_quantity']+=row['quantity']
    return result
