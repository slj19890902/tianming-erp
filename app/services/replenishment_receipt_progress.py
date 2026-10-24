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
    from app.services.replenishment_production import coverage_map
    production = coverage_map(db,items)
    result = {}
    for item in items:
        facts = grouped.get(item.id, [])
        actual = sum(row.received_quantity for row in facts) if facts else int(item.stocked_quantity or 0)
        from app.services.incoming_actual_specs import receipt_yield
        from math import ceil
        factor=int(item.stock_yield_per_sheet or 1)
        actual_units=sum(row.received_quantity*receipt_yield(db,row.id,factor) for row in facts) if facts else actual*factor
        latest_factor=receipt_yield(db,facts[-1].id,factor) if facts else factor
        completed=production[item.id]['completed_output']*max(int(item.pieces_per_box),1)
        remaining=max(0,ceil((item.quantity*factor-actual_units-completed)/latest_factor))
        short_closed = any(row.resolution_action == 'accept_short' and row.resolution_status == 'resolved' for row in facts)
        result[item.id] = dict(received_quantity=actual,
                production_completed_quantity=production[item.id]['completed_output'],
                remaining_quantity=0 if short_closed else remaining,
                short_closed=short_closed,
                shortage_quantity=remaining,
                over_quantity=max(0,(actual_units+completed-item.quantity*factor)//factor))
    return result


def refresh_order_progress(db, order, operator_id):
    from app.core.time_contract import utc_now_naive
    db.flush()
    progress=receipt_progress_map(db,order.items)
    pending = any(value['remaining_quantity'] for value in progress.values())
    actual=any(value['received_quantity'] or value['production_completed_quantity'] for value in progress.values())
    order.status = ('partially_stocked' if actual else 'confirmed') if pending else 'stocked'
    order.stocked_by = operator_id
    order.stocked_at = None if pending else utc_now_naive()
