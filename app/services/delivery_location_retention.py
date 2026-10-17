"""Employee delivery-location visibility, without altering stock or audit facts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import select
from app.core.time_contract import utc_naive_to_api
from app.models.audit import OperationLog
from app.models.finance import ReturnReceiptItem, Statement, StatementItem


def policies(db, deliveries: dict, receipts: dict, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    ids = list(deliveries)
    if not ids:
        return {}
    reconciled = set(db.scalars(select(ReturnReceiptItem.return_receipt_id)
        .join(StatementItem, StatementItem.return_receipt_item_id == ReturnReceiptItem.id)
        .join(Statement, Statement.id == StatementItem.statement_id)
        .where(Statement.confirmation_status == 'confirmed',
               ReturnReceiptItem.return_receipt_id.in_([r.id for r in receipts.values()]))).all()) if receipts else set()
    confirmed = {r.id: r.created_at for r in receipts.values() if r.status == 'confirmed'}
    if confirmed:
        logs = db.scalars(select(OperationLog).where(
            OperationLog.resource == 'ReturnReceipt', OperationLog.entity_id.in_(confirmed),
            OperationLog.action.in_(['CONFIRM_RETURN_RECEIPT', 'UPDATE_RETURN_RECEIPT']),
        ).order_by(OperationLog.created_at, OperationLog.id)).all()
        for log in logs:
            try:
                before = json.loads(log.details or '{}').get('before', {})
            except (ValueError, TypeError):
                before = {}
            if log.action == 'CONFIRM_RETURN_RECEIPT' or before.get('status') == 'cancelled':
                if log.created_at and (confirmed[log.entity_id] is None or log.created_at > confirmed[log.entity_id]):
                    confirmed[log.entity_id] = log.created_at
    result = {}
    for delivery_id in ids:
        receipt = receipts.get(delivery_id)
        at = confirmed.get(receipt.id) if receipt and receipt.status == 'confirmed' else None
        expires = at + timedelta(days=1) if at else None
        reason = 'reconciled' if receipt and receipt.id in reconciled else 'receipt_expired' if expires and now >= expires else 'retained'
        result[delivery_id] = dict(visible=reason == 'retained', reason=reason,
            confirmed_at=utc_naive_to_api(at) if at else None,
            expires_at=utc_naive_to_api(expires) if expires else None,
            evaluated_at=utc_naive_to_api(now))
    return result
