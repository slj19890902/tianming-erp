"""Receipt facts, never planned quantities, determine actual stock and closure."""
from sqlalchemy import select
from app.models.incoming_receipt import IncomingReceiptItem


def short_closed_clause(item_id):
    return select(IncomingReceiptItem.id).where(
        IncomingReceiptItem.stock_replenishment_item_id == item_id,
        IncomingReceiptItem.status == 'posted',
        IncomingReceiptItem.resolution_action == 'accept_short',
        IncomingReceiptItem.resolution_status == 'resolved',
    ).exists()


def receipt_progress(db, item):
    return receipt_progress_map(db, [item])[item.id]


def receipt_progress_map(db, items):
    items = list(items)
    if not items:
        return {}
    rows = db.scalars(select(IncomingReceiptItem).where(
        IncomingReceiptItem.stock_replenishment_item_id.in_([item.id for item in items]),
        IncomingReceiptItem.status == 'posted',
    )).all()
    grouped = {}
    for row in rows:
        grouped.setdefault(row.stock_replenishment_item_id, []).append(row)
    result = {}
    for item in items:
        facts = grouped.get(item.id, [])
        actual = sum(row.received_quantity for row in facts) if facts else int(item.stocked_quantity or 0)
        short_closed = any(row.resolution_action == 'accept_short' and row.resolution_status == 'resolved' for row in facts)
        result[item.id] = dict(received_quantity=actual,
                remaining_quantity=0 if short_closed else max(0, item.quantity-actual),
                short_closed=short_closed,
                shortage_quantity=max(0, item.quantity-actual),
                over_quantity=max(0, actual-item.quantity))
    return result


def refresh_order_progress(db, order, operator_id):
    from app.core.time_contract import utc_now_naive
    db.flush()
    pending = any(receipt_progress(db, item)['remaining_quantity'] for item in order.items)
    order.status = 'partially_stocked' if pending else 'stocked'
    order.stocked_by = operator_id
    order.stocked_at = None if pending else utc_now_naive()
