"""Read-only document ordering, independent of receipt time and delivery row IDs."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.delivery import DeliveryItem
from app.models.order import OrderItem
from app.models.order_import_source import OrderImportSourceLine
from app.models.tianhua_pre_delivery import (
    PreDeliverySourceAllocation,
    TianhuaPreDeliveryDraft,
    TianhuaPreDeliveryDraftItem,
)


def sort_delivery_document_rows(db: Session, rows, *, id_key: str = "id"):
    """Use pre-delivery source rows first, otherwise the source order sequence.

    Query only identities already selected by the caller's access/current-row
    filters. Bulk queries keep delivery list reads independent of item count.
    Split pre-delivery rows retain their confirmed allocation order. Multiple
    orders keep their first appearance on the delivery, with each order grouped.
    """
    if not rows:
        return []

    def row_id(row):
        return int((row if isinstance(row, dict) else row._mapping)[id_key])

    metadata = list(db.execute(
        select(DeliveryItem.id, DeliveryItem.delivery_id, DeliveryItem.order_item_id,
               OrderItem.order_id, OrderItem.item_sequence,
               OrderImportSourceLine.source_position)
        .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .outerjoin(OrderImportSourceLine,
                   OrderImportSourceLine.order_item_id == DeliveryItem.order_item_id)
        .where(DeliveryItem.id.in_([row_id(row) for row in rows]),
               DeliveryItem.is_current.is_(True))
        .order_by(DeliveryItem.id)
    ))
    by_id = {row.id: row for row in metadata}
    delivery_ids = {row.delivery_id for row in metadata}
    source_rows = db.execute(
        select(TianhuaPreDeliveryDraft.delivery_id, TianhuaPreDeliveryDraftItem.row_no,
               TianhuaPreDeliveryDraftItem.order_item_id,
               PreDeliverySourceAllocation.order_item_id.label("allocated_order_item_id"),
               PreDeliverySourceAllocation.id.label("allocation_id"))
        .join(TianhuaPreDeliveryDraftItem,
              TianhuaPreDeliveryDraftItem.draft_id == TianhuaPreDeliveryDraft.id)
        .outerjoin(PreDeliverySourceAllocation,
                   PreDeliverySourceAllocation.import_item_id == TianhuaPreDeliveryDraftItem.import_item_id)
        .where(TianhuaPreDeliveryDraft.delivery_id.in_(delivery_ids),
               TianhuaPreDeliveryDraftItem.delivery_qty > 0)
        .order_by(TianhuaPreDeliveryDraft.id, TianhuaPreDeliveryDraftItem.row_no,
                  PreDeliverySourceAllocation.id)
    )
    source_positions = {}
    for source in source_rows:
        item_id = source.allocated_order_item_id or source.order_item_id
        source_positions.setdefault((source.delivery_id, item_id),
                                    (source.row_no, source.allocation_id or 0))
    order_groups = {}
    for row in metadata:
        order_groups.setdefault((row.delivery_id, row.order_id), row.id)

    def key(value):
        item_id = row_id(value)
        row = by_id[item_id]
        source_position = source_positions.get((row.delivery_id, row.order_item_id))
        if source_position is not None:
            return (row.delivery_id, 0, *source_position, 0, item_id)
        position = row.source_position or row.item_sequence
        return (row.delivery_id, 1 if row.order_id is not None else 2,
                order_groups[(row.delivery_id, row.order_id)],
                0 if position is not None else 1,
                position if position is not None else (row.order_item_id or item_id), item_id)

    return sorted(rows, key=key)
