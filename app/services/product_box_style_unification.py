"""Explicit, version-bound master-name maintenance; caller owns backup/commit.

This is not a startup hook or a public endpoint. The release operator must
freeze and verify a plan on the approved source before applying it. Historical
order, procurement, production and inventory snapshots are never rewritten.
"""
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from app.models.product import Product
from app.models.user import User
from app.services.box_type_rules import BOX_TYPE_RULES, get_box_type_rule
from app.services.bom_transactions import atomic_bom
from app.services.master_data_versioning import apply_versioned_update
from app.services.shared_product_master import prepare_sync

SCHEMA = 'tianming.product-box-style-unification.20261023.v1'
REASON = '老板确认：兼容箱型名称归入已有标准箱型，仅统一名称'


def digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _actor(db, user):
    from app.api.deps import has_permission, has_unrestricted_customer_access
    actor = db.get(User, user.id, populate_existing=True) if user is not None else None
    if (actor is None or not actor.is_active or actor.role not in {'admin', 'boss'}
            or not has_permission(actor, 'products.edit')
            or not has_unrestricted_customer_access(actor, db)):
        raise HTTPException(403, '统一箱型名称需要有全部客户范围的有效主档管理员')
    return actor


def build_plan(db, user):
    actor = _actor(db, user)
    rows = []
    for product in db.scalars(select(Product).where(
            Product.deleted_at.is_(None), Product.purged_at.is_(None)
    ).order_by(Product.id).execution_options(populate_existing=True)):
        if product.supply_mode == 'external_purchase' or product.is_virtual_composite_parent:
            continue
        rule = get_box_type_rule(product.box_style)
        if rule is None or product.box_style == rule.display_name:
            continue
        rows.append(dict(id=product.id, customer_id=product.customer_id,
                         version=product.version, before=product.box_style, after=rule.display_name))
    return dict(schema=SCHEMA, actor_id=actor.id,
                registry_sha256=digest([rule.public_dict() for rule in BOX_TYPE_RULES]), rows=rows)


def apply_plan(db, user, plan, *, approved_sha256):
    """Apply precisely the approved names with the ordinary shared-master writer.

    All changes and audit records roll back together. A stale/replayed plan is
    rejected before mutation; a new empty plan after success makes no revisions.
    """
    if (not isinstance(approved_sha256, str) or len(approved_sha256) != 64
            or approved_sha256 != digest(plan)):
        raise HTTPException(409, '箱型名称归一计划摘要不符')
    if db.new or db.dirty or db.deleted:
        raise HTTPException(409, '箱型归一必须使用无其他未提交修改的事务')
    with atomic_bom(db):
        actor = _actor(db, user)
        if build_plan(db, actor) != plan:
            raise HTTPException(409, '主档、版本、权限或箱型字典已变化，请重新核对名称归一计划')
        planned = {row['id']: row for row in plan['rows']}
        products = {pid: db.get(Product, pid) for pid in planned}
        # Preflight every ordinary shared-master propagation. The approved
        # rename must never reclassify a peer with a different physical type.
        for pid, row in planned.items():
            sync = prepare_sync(db, products[pid], {'box_style': row['after']}, actor)
            for peer in (sync or {}).get('peers', []):
                other = peer['product']
                rule = get_box_type_rule(other.box_style)
                if (rule is None or rule.display_name != row['after']
                        or peer['updates'] != {'box_style': row['after']}
                        or (other.box_style != row['after'] and other.id not in planned)):
                    raise HTTPException(409, '共用产品存在不同箱型或计划外修改，未统一任何名称')
        for pid, row in planned.items():
            product = products[pid]
            # A previous ordinary shared-master update may have renamed this
            # peer already, with its own CAS, version and audit record.
            if product.box_style == row['after'] and product.version == row['version'] + 1:
                continue
            if product.box_style != row['before'] or product.version != row['version']:
                raise HTTPException(409, '箱型归一期间主档版本变化')
            apply_versioned_update(db, object_type='product', entity=product,
                updates={'box_style': row['after']}, expected_version=row['version'],
                user=actor, reason=REASON, source='box-style-unification:' + approved_sha256,
                action='system_mapping')
        db.flush()
        for pid, row in planned.items():
            db.refresh(products[pid])
            if products[pid].box_style != row['after'] or products[pid].version != row['version'] + 1:
                raise HTTPException(409, '名称归一回读与计划不符，全部回滚')
    return dict(schema=SCHEMA, plan_sha256=approved_sha256, changed_count=len(planned),
                historical_snapshots_written=False, committed=False)
