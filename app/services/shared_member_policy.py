"""Member eligibility from immutable, versioned management receipts.

The latest complete snapshot is authoritative. Original member identities and
frozen reservations are never rewritten. Reads accept either Session or the
owning flush Connection so order insertion uses the same transaction.
"""
import json
from sqlalchemy import select
from app.models.shared_finished_stock import SharedFinishedMutation

CAPABILITY = 'shared_customer_members_v1'


def states(db, group_id):
    raw = db.execute(select(SharedFinishedMutation.result_json).where(
        SharedFinishedMutation.group_id == group_id,
        SharedFinishedMutation.result_json.contains('"member_states":', autoescape=True),
    ).order_by(SharedFinishedMutation.id.desc()).limit(1)).scalar()
    if raw is None:
        return {}
    value = json.loads(raw)['member_states']
    if not isinstance(value, dict) or any(type(v) is not bool for v in value.values()):
        raise ValueError('共用成员策略损坏，停止新增共用取用')
    return value


def enabled(db, group_id, product_id):
    return states(db, group_id).get(str(product_id), True)
